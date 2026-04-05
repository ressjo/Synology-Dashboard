"""
HyperBackup integration.
Primär via DSM API (synology.py), SSH als Fallback.
"""
import paramiko
import threading
from typing import Optional

_ssh_client: Optional[paramiko.SSHClient] = None
_ssh_lock = threading.Lock()


def _get_ssh_cfg() -> dict:
    """Liest SSH-Konfiguration aus DB; fällt auf config.yaml zurück."""
    from app.services_db import get_service
    cfg = get_service("ssh")
    if cfg:
        return cfg
    from app.config import config
    return config.get("ssh", {})


def _ssh_connect() -> paramiko.SSHClient:
    cfg = _get_ssh_cfg()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    key_path = cfg.get("key_path")
    if key_path:
        client.connect(
            hostname=cfg["host"],
            port=int(cfg.get("port", 22)),
            username=cfg["username"],
            key_filename=key_path,
            timeout=10,
        )
    else:
        client.connect(
            hostname=cfg["host"],
            port=int(cfg.get("port", 22)),
            username=cfg["username"],
            password=cfg["password"],
            timeout=10,
        )
    return client


def get_backup_sizes_ssh(tasks: list[dict]) -> dict[int, str]:
    """
    Liest Backup-Größen per SSH aus den HBK-Cache-Verzeichnissen.
    Gibt {task_id: "3.1G"} zurück.
    """
    result = {}
    try:
        client = _ssh_connect()
        # Alle .hbk Dateien/Verzeichnisse auf allen Volumes finden und Größe messen
        cmd = r"find /volume* -maxdepth 8 -name '*.hbk' 2>/dev/null | xargs -I{} du -sh {} 2>/dev/null"
        _, stdout, _ = client.exec_command(cmd, timeout=20)
        lines = stdout.read().decode().strip().splitlines()
        client.close()

        for line in lines:
            if "\t" not in line:
                continue
            size, path = line.split("\t", 1)
            for task in tasks:
                target = task.get("target_id", "")
                if target and target in path:
                    result[task["task_id"]] = size
    except Exception:
        pass
    return result


def _get_ssh() -> paramiko.SSHClient:
    """Gibt eine gecachte SSH-Verbindung zurück, stellt sie bei Bedarf neu her (thread-safe)."""
    global _ssh_client
    with _ssh_lock:
        try:
            if _ssh_client and _ssh_client.get_transport() and _ssh_client.get_transport().is_active():
                return _ssh_client
        except Exception:
            pass
        _ssh_client = _ssh_connect()
        return _ssh_client


def trigger_backup_ssh(task_id: int) -> tuple[bool, str]:
    """HyperBackup Aufgabe via SSH starten (Fallback)."""
    try:
        client = _get_ssh()
        cmd = f"/var/packages/HyperBackup/target/bin/dsmbackup --backup {task_id}"
        _, stdout, stderr = client.exec_command(cmd, timeout=30)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()

        if exit_code != 0:
            return False, err or f"Exit code {exit_code}"
        return True, out or "Backup gestartet"
    except Exception as e:
        _ssh_client = None  # Bei Fehler Cache leeren
        return False, str(e)


_PORT_NAMES = {
    "21": "FTP", "22": "SSH", "80": "HTTP", "139": "NetBIOS",
    "443": "HTTPS", "445": "SMB", "548": "AFP", "873": "rsync",
    "2049": "NFS", "5000": "DSM", "5001": "DSM-S", "6690": "Drive",
    "8080": "HTTP-Alt", "8443": "HTTPS-Alt", "32400": "Plex",
    "5005": "Video", "1194": "VPN",
}


_BACKUP_LOG = "/var/log/synolog/synobackup.log"


def parse_backup_log(task_names: list[str]) -> dict[str, dict]:
    """
    Liest synobackup.log via SSH und extrahiert pro Task:
    last_time, last_result, duration, size.
    """
    try:
        ssh = _get_ssh()
        _, stdout, _ = ssh.exec_command(
            f"tail -n 2000 {_BACKUP_LOG} 2>/dev/null", timeout=8
        )
        raw = stdout.read().decode(errors="replace")
    except Exception as e:
        return {"_error": str(e)}

    import re
    from datetime import datetime

    # Normalisiert Task-Name → key
    results: dict[str, dict] = {}

    lines = raw.splitlines()

    # Muster die Synology benutzt (flexibel):
    # "2024/01/15 02:00:01 ..." oder "Jan 15 02:00:01 ..."
    # Timestamp-Formate probieren
    ts_patterns = [
        r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})",  # 2024/01/15 02:00:01
        r"(\w{3}\s+\d{1,2} \d{2}:\d{2}:\d{2})",     # Jan 15 02:00:01
    ]

    def try_parse_ts(s: str) -> str | None:
        for fmt in ("%Y/%m/%d %H:%M:%S", "%b %d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                if dt.year == 1900:          # kein Jahr im Format → aktuelles Jahr
                    dt = dt.replace(year=datetime.now().year)
                return dt.strftime("%d.%m.%Y %H:%M")
            except ValueError:
                continue
        return None

    for line in lines:
        for task_name in task_names:
            if task_name not in line:
                continue

            # Timestamp extrahieren
            ts_str = None
            for pat in ts_patterns:
                m = re.search(pat, line)
                if m:
                    ts_str = try_parse_ts(m.group(1))
                    break

            line_lower = line.lower()

            # Ergebnis
            if any(w in line_lower for w in ("finish", "success", "completed", "erfolgreich", "done")):
                result = "success"
            elif any(w in line_lower for w in ("fail", "error", "fehler", "abort")):
                result = "error"
            else:
                continue  # Nur Start/Ende-Zeilen interessant

            entry = results.setdefault(task_name, {})

            # Nur aktualisieren wenn neuer Eintrag
            if "last_time" not in entry or ts_str:
                entry["last_time"]   = ts_str
                entry["last_result"] = result

            # Dauer: "Duration: 01:23:45" oder "duration: 1h23m"
            dur_m = re.search(r"[Dd]uration[:\s]+(\d+):(\d+):(\d+)", line)
            if dur_m:
                h, m_, s = int(dur_m.group(1)), int(dur_m.group(2)), int(dur_m.group(3))
                total_s = h * 3600 + m_ * 60 + s
                if total_s >= 3600:
                    entry["duration"] = f"{h}h {m_}m"
                elif total_s >= 60:
                    entry["duration"] = f"{m_}m {s}s"
                else:
                    entry["duration"] = f"{total_s}s"

            # Größe: "1.23 GB", "456 MB", "transferred: 1.2 GB"
            size_m = re.search(
                r"(\d+(?:\.\d+)?)\s*(GB|MB|KB|TB|GiB|MiB|KiB)",
                line, re.IGNORECASE
            )
            if size_m:
                val, unit = size_m.group(1), size_m.group(2).upper()
                unit = unit.replace("IB", "B")  # GiB → GB
                entry["size"] = f"{val} {unit}"

    return results


def get_nas_uptime() -> str | None:
    """Liest /proc/uptime via SSH und gibt lesbare Uptime zurück."""
    try:
        ssh = _get_ssh()
        _, stdout, _ = ssh.exec_command("cat /proc/uptime", timeout=5)
        seconds = float(stdout.read().decode().split()[0])
        days  = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        mins  = int((seconds % 3600) // 60)
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {mins}m"
        return f"{mins}m"
    except Exception:
        return None


def get_memory_detail() -> dict:
    """/proc/meminfo via SSH → Apps / Cache / Free Aufschlüsselung in MB."""
    try:
        ssh = _get_ssh()
        _, stdout, _ = ssh.exec_command(
            "awk '/^(MemTotal|MemFree|Buffers|Cached|SReclaimable):/{print $1, $2}' /proc/meminfo",
            timeout=5,
        )
        data: dict[str, int] = {}
        for line in stdout.read().decode().splitlines():
            parts = line.split()
            if len(parts) == 2:
                data[parts[0].rstrip(":")] = int(parts[1])  # kB

        total      = data.get("MemTotal", 0)
        free       = data.get("MemFree", 0)
        buffers    = data.get("Buffers", 0)
        cached     = data.get("Cached", 0)
        sreclaimable = data.get("SReclaimable", 0)

        cache_kb = buffers + cached + sreclaimable
        app_kb   = max(total - free - cache_kb, 0)

        return {
            "app_mb":   app_kb   // 1024,
            "cache_mb": cache_kb // 1024,
            "free_mb":  free     // 1024,
        }
    except Exception:
        return {}


def get_top_processes(n: int = 5) -> list[dict]:
    """Top-N Prozesse nach CPU-Last via SSH."""
    try:
        ssh = _get_ssh()
        # -o pid,pcpu,pmem,comm ohne Pfad; --sort nicht überall verfügbar → sort via awk
        cmd = "ps -eo pid,pcpu,pmem,comm 2>/dev/null | sort -k2 -rn | head -6 | tail -5"
        _, stdout, _ = ssh.exec_command(cmd, timeout=8)
        lines = stdout.read().decode().strip().splitlines()
        result = []
        for line in lines:
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            try:
                result.append({
                    "pid":  parts[0],
                    "cpu":  float(parts[1]),
                    "mem":  float(parts[2]),
                    "cmd":  parts[3].strip()[:32],
                })
            except ValueError:
                continue
        return result
    except Exception:
        return []


def get_network_connections() -> dict:
    """Aktive TCP-Verbindungen via SSH. Probiert ss → netstat → /proc/net/tcp."""
    try:
        ssh = _get_ssh()
        lines: list[str] = []
        proc_net_mode = False

        # 1) ss: $(NF-1) und $NF sind immer Local und Peer, unabhängig vom ss-Format
        _, stdout, _ = ssh.exec_command(
            "ss -tn 2>/dev/null | grep -i ESTAB | awk '{print $(NF-1), $NF}'",
            timeout=6,
        )
        lines = [l.strip() for l in stdout.read().decode().splitlines() if l.strip()]

        # 2) netstat
        if not lines:
            _, stdout, _ = ssh.exec_command(
                'netstat -tn 2>/dev/null | awk \'NR>2 && $6=="ESTABLISHED"{print $4, $5}\'',
                timeout=6,
            )
            lines = [l.strip() for l in stdout.read().decode().splitlines() if l.strip()]

        # 3) /proc/net/tcp – immer vorhanden, Adressen in Hex
        if not lines:
            proc_net_mode = True
            _, stdout, _ = ssh.exec_command(
                'awk \'NR>1 && $4=="01"{print $2, $3}\' /proc/net/tcp /proc/net/tcp6 2>/dev/null',
                timeout=6,
            )
            lines = [l.strip() for l in stdout.read().decode().splitlines() if l.strip()]

        def parse_addr(addr: str, is_hex: bool) -> tuple[str, str]:
            """Gibt (ip, port_str) zurück."""
            if is_hex:
                # Format: "0F02A8C0:1389" (little-endian IP, hex port)
                if ":" in addr:
                    hex_ip, hex_port = addr.rsplit(":", 1)
                    try:
                        port_n = str(int(hex_port, 16))
                    except ValueError:
                        port_n = hex_port
                    # IPv4: 4 Bytes little-endian
                    if len(hex_ip) == 8:
                        b = bytes.fromhex(hex_ip)
                        ip = f"{b[3]}.{b[2]}.{b[1]}.{b[0]}"
                    else:
                        ip = hex_ip  # IPv6: vereinfacht
                    return ip, port_n
                return addr, "?"
            else:
                # Format: "192.168.1.1:5000" oder "[::1]:5000"
                raw_port = addr.rsplit(":", 1)[-1] if ":" in addr else "?"
                ip = addr.rsplit(":", 1)[0].strip("[]") if ":" in addr else addr
                return ip, raw_port

        total = 0
        # {label: {"count": int, "ips": set}}
        port_data: dict[str, dict] = {}

        for line in lines:
            parts = line.split()
            if not parts:
                continue
            local = parts[0]
            peer  = parts[1] if len(parts) > 1 else ""

            _, local_port_str = parse_addr(local, proc_net_mode)
            remote_ip, _      = parse_addr(peer,  proc_net_mode)

            # Localhost-Verbindungen überspringen
            _loopback = ("127.", "::1", "0.0.0.0", "::")
            if any(remote_ip.startswith(p) for p in _loopback):
                continue

            try:
                port_n = str(int(local_port_str, 16) if proc_net_mode and len(local_port_str) <= 4 else int(local_port_str))
            except ValueError:
                port_n = local_port_str

            label = _PORT_NAMES.get(port_n, port_n)
            if label not in port_data:
                port_data[label] = {"count": 0, "ips": set()}
            port_data[label]["count"] += 1
            port_data[label]["ips"].add(remote_ip)
            total += 1

        # Sortiert nach Anzahl, max 8, Sets → Listen
        sorted_ports = sorted(port_data.items(), key=lambda x: -x[1]["count"])[:8]
        by_port = {
            label: {"count": d["count"], "ips": sorted(d["ips"])}
            for label, d in sorted_ports
        }
        return {"total": total, "by_port": by_port}
    except Exception as e:
        return {"total": 0, "by_port": {}, "error": str(e)}


def get_shared_folder_sizes() -> dict:
    """Größe der Shared Folders via SSH (du). Systemordner (@ prefix) werden übersprungen."""
    try:
        ssh = _get_ssh()

        # Volumen-Gesamtgröße per df (in MB)
        _, stdout, _ = ssh.exec_command("df -m /volume1 2>/dev/null | tail -1", timeout=6)
        df_line = stdout.read().decode().strip()
        volume_total_mb = 0
        if df_line:
            parts = df_line.split()
            if len(parts) >= 2:
                try:
                    volume_total_mb = int(parts[1])
                except ValueError:
                    pass

        _, stdout, _ = ssh.exec_command(
            "du -sm /volume1/[!@]* 2>/dev/null | sort -rn",
            timeout=15,
        )
        lines = stdout.read().decode().strip().splitlines()
        folders = []
        for line in lines:
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            try:
                size_mb = int(parts[0])
                name = parts[1].rsplit("/", 1)[-1]
                folders.append({"name": name, "size_mb": size_mb})
            except ValueError:
                continue
        return {"folders": folders, "volume_total_mb": volume_total_mb}
    except Exception as e:
        return {"folders": [], "volume_total_mb": 0, "error": str(e)}


def get_syslog(lines: int = 300) -> list[dict]:
    """Liest /var/log/messages via sudo, filtert auf Fehler/Warnungen."""
    import re
    try:
        ssh = _get_ssh()
        cmd = (
            f"sudo tail -n {lines} /var/log/messages 2>/dev/null"
            r" | grep -iE 'err|warn|crit|fail|alert'"
        )
        _, stdout, _ = ssh.exec_command(cmd, timeout=10)
        raw = stdout.read().decode(errors="replace").strip().splitlines()
        # Format: "2026-04-05T10:40:05+02:00 hostname process[pid]: message"
        pattern = re.compile(r"^(\S+)\s+\S+\s+([^\s:\[]+)(?:\[\d+\])?:\s*(.*)")
        result = []
        for line in reversed(raw):
            m = pattern.match(line)
            if not m:
                continue
            ts, proc, msg = m.groups()
            severity = "error" if re.search(r"err|crit|fail", msg, re.I) else "warning"
            result.append({
                "time":     ts[:19].replace("T", " "),
                "process":  proc,
                "message":  msg.strip(),
                "severity": severity,
            })
        return result[:150]
    except Exception as e:
        return [{"error": str(e)}]


def get_backup_status_ssh() -> list[dict]:
    """HyperBackup Aufgaben-Status via SSH auslesen."""
    try:
        client = _ssh_connect()
        _, stdout, _ = client.exec_command("synobackup --status", timeout=10)
        out = stdout.read().decode().strip()
        client.close()
        if out:
            return [{"raw": out}]
        return []
    except Exception as e:
        return [{"error": str(e)}]
