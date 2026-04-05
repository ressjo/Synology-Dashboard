import sqlite3
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

DB_PATH = Path(os.getenv("DB_PATH", "data/dashboard.db"))


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                cpu_usage REAL,
                sys_temp INTEGER,
                memory_used INTEGER,
                memory_total INTEGER,
                disk_info TEXT,
                network_rx REAL,
                network_tx REAL
            )
        """)
        # Migration: sys_temp Spalte nachrüsten falls DB bereits existiert
        try:
            conn.execute("ALTER TABLE stats ADD COLUMN sys_temp INTEGER")
            conn.commit()
        except Exception:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backup_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                task_id INTEGER,
                task_name TEXT,
                status TEXT,
                message TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()


def _now_local() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_stats(cpu: float, mem_used: int, mem_total: int, disk_info: dict, net_rx: float, net_tx: float, sys_temp: int = None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO stats (timestamp, cpu_usage, sys_temp, memory_used, memory_total, disk_info, network_rx, network_tx) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_now_local(), cpu, sys_temp, mem_used, mem_total, json.dumps(disk_info), net_rx, net_tx),
        )
        conn.commit()


def get_stats_history(hours: int = 24) -> list[dict]:
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM stats WHERE timestamp >= ? ORDER BY timestamp ASC",
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_storage_growth() -> list[dict]:
    """Calculate storage growth over 7 and 30 days with forecast per volume."""
    now = datetime.now()
    results = []

    with get_db() as conn:
        latest = conn.execute(
            "SELECT disk_info, timestamp FROM stats WHERE disk_info IS NOT NULL ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if not latest:
            return []

        current_disks = json.loads(latest["disk_info"]) or []

        for period_days in (7, 30):
            cutoff = (now - timedelta(days=period_days)).strftime("%Y-%m-%d %H:%M:%S")
            old_row = conn.execute(
                "SELECT disk_info, timestamp FROM stats "
                "WHERE timestamp <= ? AND disk_info IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT 1",
                (cutoff,),
            ).fetchone()

            # Fallback: ältesten verfügbaren Eintrag nehmen wenn Periode noch nicht erreicht
            if not old_row and period_days == 7:
                old_row = conn.execute(
                    "SELECT disk_info, timestamp FROM stats "
                    "WHERE disk_info IS NOT NULL "
                    "ORDER BY timestamp ASC LIMIT 1"
                ).fetchone()
                # Nur verwenden wenn der Eintrag wirklich älter als 1 Stunde ist
                if old_row:
                    old_ts_check = datetime.strptime(old_row["timestamp"], "%Y-%m-%d %H:%M:%S")
                    if (now - old_ts_check).total_seconds() < 3600:
                        old_row = None

            if not old_row:
                continue

            old_disks = json.loads(old_row["disk_info"]) or []
            old_ts = datetime.strptime(old_row["timestamp"], "%Y-%m-%d %H:%M:%S")
            days_elapsed = max((now - old_ts).total_seconds() / 86400, 1)

            for cur in current_disks:
                vol = cur["name"]
                old = next((d for d in old_disks if d["name"] == vol), None)
                if not old:
                    continue

                growth_gb = round(cur["used_gb"] - old["used_gb"], 2)
                daily_gb = round(growth_gb / days_elapsed, 3)
                free_gb = cur["total_gb"] - cur["used_gb"]
                days_until_full = int(free_gb / daily_gb) if daily_gb > 0 else None

                results.append({
                    "volume": vol,
                    "period_days": period_days,
                    "growth_gb": growth_gb,
                    "daily_growth_gb": daily_gb,
                    "used_gb": cur["used_gb"],
                    "total_gb": cur["total_gb"],
                    "pct": cur["pct"],
                    "days_until_full": days_until_full,
                    "since": old_row["timestamp"],
                })

    return results


def log_backup(task_id: int, task_name: str, status: str, message: str = ""):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backup_log (timestamp, task_id, task_name, status, message) VALUES (?, ?, ?, ?, ?)",
            (_now_local(), task_id, task_name, status, message),
        )
        conn.commit()


def get_last_backup_per_task() -> dict[int, dict]:
    """Gibt den letzten Dashboard-Trigger pro task_id zurück."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT task_id, task_name, timestamp, status, message "
            "FROM backup_log "
            "GROUP BY task_id HAVING MAX(timestamp) "
            "ORDER BY task_id"
        ).fetchall()
    return {r["task_id"]: dict(r) for r in rows}


def get_backup_logs(limit: int = 20) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM backup_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
