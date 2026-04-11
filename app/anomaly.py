"""
Anomalie-Erkennung für Container-CPU, Container-Status und System-Metriken.

Läuft als Hintergrundjob im Scheduler.
Erstellt Benachrichtigungen in der DB wenn Anomalien erkannt werden.
"""
import logging
from datetime import datetime, timedelta
from app import database

log = logging.getLogger(__name__)

# ── Container-Schwellwerte ────────────────────────────────────
CPU_MULTIPLIER   = 3.0   # Alert wenn CPU > Baseline * 3
CPU_MIN_ABSOLUTE = 20.0  # Alert nur wenn CPU auch absolut > 20% (kein Lärm bei Idle)
CPU_MIN_BASELINE = 5.0   # Baseline muss mind. 5% sein um sinnvoll zu vergleichen
ALERT_COOLDOWN_MIN = 30  # Kein zweiter Alert für selben Container innerhalb 30 Min
MIN_SAMPLES      = 5     # Erst nach N Samples alertieren (Einlernphase)

# ── System-Schwellwerte ───────────────────────────────────────
TEMP_WARN_C        = 60    # °C — Warnung
TEMP_CRIT_C        = 70    # °C — Kritisch
TEMP_COOLDOWN_MIN  = 60

NET_MULTIPLIER     = 8.0         # Alert wenn > Baseline * 8
NET_MIN_ABS_BYTES  = 20_000_000  # 20 MB/s Minimum (kein Lärm bei Idle-Spitzen)
NET_COOLDOWN_MIN   = 15
NET_MIN_SAMPLES    = 10          # 10 Min Einlernphase

DISK_GROWTH_GB_H   = 3.0   # GB pro Stunde → Alert
DISK_GROWTH_WINDOW = 1.0   # Vergleichsfenster in Stunden
DISK_COOLDOWN_MIN  = 120

# Letzte bekannte Container-States (in-memory, für Status-Alerts)
_last_states: dict[str, str] = {}


async def check_container_anomalies() -> None:
    """Holt aktuelle Container-Stats und prüft auf Anomalien."""
    try:
        from app import docker_manager
        import asyncio

        # Container-Stats im Thread holen (blockierender HTTP-Call)
        stats = await asyncio.to_thread(docker_manager.get_container_stats)
        containers = await asyncio.to_thread(docker_manager.list_containers)

        _check_cpu_anomalies(stats)
        _check_state_changes(containers)

    except Exception as e:
        log.debug("Anomalie-Check fehlgeschlagen: %s", e)


def _check_cpu_anomalies(stats: dict[str, dict]) -> None:
    for container_id, data in stats.items():
        cpu = data.get("cpu_pct")
        if cpu is None:
            continue

        # Baseline aktualisieren (EMA)
        baseline = database.upsert_container_baseline(container_id, cpu)

        # Einlernphase: erst nach MIN_SAMPLES alertieren
        info = database.get_container_baseline(container_id)
        if not info or info.get("samples", 0) < MIN_SAMPLES:
            continue

        # Schwellwert-Check
        if (baseline >= CPU_MIN_BASELINE
                and cpu > baseline * CPU_MULTIPLIER
                and cpu > CPU_MIN_ABSOLUTE):

            # Cooldown prüfen
            last_alert = database.get_last_alert_time(container_id)
            if last_alert and (datetime.now() - last_alert) < timedelta(minutes=ALERT_COOLDOWN_MIN):
                continue

            database.set_last_alert_time(container_id)
            database.add_notification(
                type_="container_cpu",
                title=f"Hohe CPU-Last: {container_id}",
                message=(
                    f"Container «{container_id}» verwendet {cpu:.1f}% CPU "
                    f"(Normalwert: ~{baseline:.1f}%). "
                    f"Das ist {cpu / baseline:.1f}× über dem Durchschnitt."
                ),
                severity="warning",
            )
            log.info("CPU-Anomalie erkannt: %s (%.1f%% vs. Baseline %.1f%%)", container_id, cpu, baseline)


def _check_state_changes(containers: list[dict]) -> None:
    global _last_states

    current_states = {c["name"]: c["state"] for c in containers if "error" not in c}

    for name, state in current_states.items():
        prev = _last_states.get(name)
        if prev is None:
            # Erster Check — States einlesen, noch kein Alert
            _last_states[name] = state
            continue

        if prev == "running" and state != "running":
            database.add_notification(
                type_="container_down",
                title=f"Container gestoppt: {name}",
                message=f"Container «{name}» ist nicht mehr aktiv (Status: {state}).",
                severity="error",
            )
            log.info("Container gestoppt erkannt: %s (%s → %s)", name, prev, state)

        elif prev != "running" and state == "running":
            database.add_notification(
                type_="container_up",
                title=f"Container gestartet: {name}",
                message=f"Container «{name}» ist wieder aktiv.",
                severity="info",
            )

    _last_states = current_states


# ── System-Anomalien ──────────────────────────────────────────

def check_system_anomalies(
    sys_temp: int | None,
    net_rx: float,
    net_tx: float,
    disk_info: list[dict],
) -> None:
    """Wird nach jedem Stats-Collect aufgerufen. Prüft Temp, Netzwerk, Disk."""
    try:
        if sys_temp is not None:
            _check_temp(sys_temp)
        _check_network_spike(net_rx, net_tx)
        _check_disk_growth(disk_info)
    except Exception as e:
        log.debug("System-Anomalie-Check fehlgeschlagen: %s", e)


def _check_temp(temp: int) -> None:
    if temp < TEMP_WARN_C:
        return
    last_alert = database.get_last_alert_time("sys:temp")
    if last_alert and (datetime.now() - last_alert) < timedelta(minutes=TEMP_COOLDOWN_MIN):
        return
    severity = "error" if temp >= TEMP_CRIT_C else "warning"
    database.set_last_alert_time("sys:temp")
    database.add_notification(
        type_="sys_temp",
        title=f"Hohe Systemtemperatur: {temp} °C",
        message=(
            f"Die NAS-Systemtemperatur beträgt {temp} °C "
            f"(Warnung ab {TEMP_WARN_C} °C, Kritisch ab {TEMP_CRIT_C} °C)."
        ),
        severity=severity,
    )
    log.info("Temperatur-Anomalie: %d °C (severity=%s)", temp, severity)


def _check_network_spike(net_rx: float, net_tx: float) -> None:
    for key, value, label in (
        ("sys:net_rx", net_rx, "Download (RX)"),
        ("sys:net_tx", net_tx, "Upload (TX)"),
    ):
        baseline = database.upsert_container_baseline(key, value)
        info = database.get_container_baseline(key)
        if not info or info.get("samples", 0) < NET_MIN_SAMPLES:
            continue
        if baseline < 1_000:  # Baseline unter 1 KB/s → kein sinnvoller Vergleich
            continue
        if value > baseline * NET_MULTIPLIER and value > NET_MIN_ABS_BYTES:
            last_alert = database.get_last_alert_time(key)
            if last_alert and (datetime.now() - last_alert) < timedelta(minutes=NET_COOLDOWN_MIN):
                continue
            database.set_last_alert_time(key)
            mb = value / 1_000_000
            mb_base = baseline / 1_000_000
            database.add_notification(
                type_="sys_net_spike",
                title=f"Netzwerk-Spike: {label} {mb:.1f} MB/s",
                message=(
                    f"Ungewöhnlich hoher {label}-Durchsatz: {mb:.1f} MB/s "
                    f"(Normalwert: ~{mb_base:.2f} MB/s, {value / baseline:.1f}× Baseline)."
                ),
                severity="warning",
            )
            log.info("Netzwerk-Spike: %s %.1f MB/s (Baseline: %.2f MB/s)", label, mb, mb_base)


def _check_disk_growth(disk_info: list[dict]) -> None:
    if not disk_info:
        return
    ref = database.get_disk_info_before(DISK_GROWTH_WINDOW)
    if not ref:
        return
    old_disks = ref["disks"]
    old_ts = datetime.strptime(ref["timestamp"], "%Y-%m-%d %H:%M:%S")
    hours_elapsed = max((datetime.now() - old_ts).total_seconds() / 3600, 0.1)

    for cur in disk_info:
        vol = cur["name"]
        old = next((d for d in old_disks if d["name"] == vol), None)
        if not old:
            continue
        growth_gb = (cur["used"] - old["used"]) / 1e9
        rate_gb_h = growth_gb / hours_elapsed
        if rate_gb_h < DISK_GROWTH_GB_H:
            continue
        key = f"sys:disk:{vol}"
        last_alert = database.get_last_alert_time(key)
        if last_alert and (datetime.now() - last_alert) < timedelta(minutes=DISK_COOLDOWN_MIN):
            continue
        database.set_last_alert_time(key)
        database.add_notification(
            type_="sys_disk_growth",
            title=f"Schnelles Disk-Wachstum: {vol}",
            message=(
                f"Volume {vol} wuchs in {hours_elapsed:.1f} h um {growth_gb:.1f} GB "
                f"({rate_gb_h:.1f} GB/h). Möglicherweise läuft ein unerwarteter Prozess."
            ),
            severity="warning",
        )
        log.info("Disk-Wachstum: %s +%.1f GB in %.1fh (%.1f GB/h)", vol, growth_gb, hours_elapsed, rate_gb_h)
