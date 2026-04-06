"""
Anomalie-Erkennung für Container-CPU und Container-Status.

Läuft als Hintergrundjob im Scheduler.
Erstellt Benachrichtigungen in der DB wenn Anomalien erkannt werden.
"""
import logging
from datetime import datetime, timedelta
from app import database

log = logging.getLogger(__name__)

# ── Schwellwerte ──────────────────────────────────────────────
CPU_MULTIPLIER   = 3.0   # Alert wenn CPU > Baseline * 3
CPU_MIN_ABSOLUTE = 20.0  # Alert nur wenn CPU auch absolut > 20% (kein Lärm bei Idle)
CPU_MIN_BASELINE = 5.0   # Baseline muss mind. 5% sein um sinnvoll zu vergleichen
ALERT_COOLDOWN_MIN = 30  # Kein zweiter Alert für selben Container innerhalb 30 Min
MIN_SAMPLES      = 5     # Erst nach N Samples alertieren (Einlernphase)

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
