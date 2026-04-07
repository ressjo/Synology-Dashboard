import sqlite3
import json
import os
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone

DB_PATH = Path(os.getenv("DB_PATH", "data/dashboard.db"))


@contextmanager
def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


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
        except sqlite3.OperationalError:
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                type      TEXT NOT NULL,
                title     TEXT NOT NULL,
                message   TEXT NOT NULL,
                severity  TEXT DEFAULT 'warning',
                read      INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS container_baselines (
                container_name TEXT PRIMARY KEY,
                avg_cpu        REAL DEFAULT 0,
                samples        INTEGER DEFAULT 0,
                last_alert     DATETIME
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
            "FROM backup_log b1 "
            "WHERE timestamp = ("
            "  SELECT MAX(timestamp) FROM backup_log b2 WHERE b2.task_id = b1.task_id"
            ") "
            "ORDER BY task_id"
        ).fetchall()
    return {r["task_id"]: dict(r) for r in rows}


def get_backup_logs(limit: int = 20) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM backup_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Notifications ─────────────────────────────────────────────

def add_notification(type_: str, title: str, message: str, severity: str = "warning") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO notifications (timestamp, type, title, message, severity) VALUES (?, ?, ?, ?, ?)",
            (_now_local(), type_, title, message, severity),
        )
        conn.commit()


def get_notifications(limit: int = 30) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_unread_notifications() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM notifications WHERE read=0").fetchone()[0]


def mark_all_notifications_read() -> None:
    with get_db() as conn:
        conn.execute("UPDATE notifications SET read=1")
        conn.commit()


def delete_old_notifications(days: int = 7) -> None:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute("DELETE FROM notifications WHERE timestamp < ?", (cutoff,))
        conn.commit()


# ── Container baselines ───────────────────────────────────────

def get_container_baseline(name: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM container_baselines WHERE container_name=?", (name,)
        ).fetchone()
    return dict(row) if row else None


def upsert_container_baseline(name: str, new_cpu: float, alpha: float = 0.15) -> float:
    """Exponential moving average update. Returns new avg_cpu."""
    existing = get_container_baseline(name)
    if existing and existing["samples"] >= 3:
        new_avg = alpha * new_cpu + (1 - alpha) * existing["avg_cpu"]
        samples = existing["samples"] + 1
    else:
        new_avg = new_cpu if not existing else (existing["avg_cpu"] + new_cpu) / 2
        samples = (existing["samples"] + 1) if existing else 1

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO container_baselines (container_name, avg_cpu, samples, last_alert) "
            "VALUES (?, ?, ?, (SELECT last_alert FROM container_baselines WHERE container_name=?))",
            (name, new_avg, samples, name),
        )
        conn.commit()
    return new_avg


def get_last_alert_time(name: str) -> datetime | None:
    row = get_container_baseline(name)
    if row and row.get("last_alert"):
        try:
            return datetime.strptime(row["last_alert"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return None


def set_last_alert_time(name: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE container_baselines SET last_alert=? WHERE container_name=?",
            (_now_local(), name),
        )
        conn.commit()


# ── Storage history for chart ─────────────────────────────────

def get_storage_history(hours: int = 168) -> list[dict]:
    """Returns timestamp + disk_info rows for the chart."""
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT timestamp, disk_info FROM stats "
            "WHERE timestamp >= ? AND disk_info IS NOT NULL ORDER BY timestamp ASC",
            (since,),
        ).fetchall()
    result = []
    for r in rows:
        try:
            disks = json.loads(r["disk_info"])
            result.append({"timestamp": r["timestamp"], "disks": disks})
        except Exception:
            pass
    return result
