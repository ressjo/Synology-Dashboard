import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.config import config

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def collect_stats():
    """Sammelt System-Stats und speichert sie in der DB."""
    try:
        from app import synology, database

        util = await synology.get_utilization()
        if not util:
            return

        cpu = util.get("cpu", {}).get("user_load", 0)

        mem = util.get("memory", {})
        mem_total_kb = mem.get("total_real", 0)
        mem_avail_kb = mem.get("avail_real", 0)
        mem_used = (mem_total_kb - mem_avail_kb) * 1024  # KB → Bytes
        mem_total = mem_total_kb * 1024

        net = util.get("network", [])
        rx = sum(n.get("rx", 0) for n in net if isinstance(n, dict) and n.get("device") != "total")
        tx = sum(n.get("tx", 0) for n in net if isinstance(n, dict) and n.get("device") != "total")

        storage, temp_info = await asyncio.gather(
            synology.get_storage_info(),
            synology.get_system_temp(),
        )
        sys_temp = temp_info.get("sys_temp")

        volumes = storage.get("volumes", [])
        disk_info = [
            {
                "name": v.get("vol_path", v.get("id", "?")),
                "used": int(v.get("size", {}).get("used", 0)),
                "total": int(v.get("size", {}).get("total", 0)),
            }
            for v in volumes
        ]

        database.save_stats(cpu, mem_used, mem_total, disk_info, rx, tx, sys_temp)
        log.debug("Stats gespeichert: CPU=%.1f%%, RAM=%dMB/%dMB", cpu, mem_used // 1024 // 1024, mem_total // 1024 // 1024)
    except Exception as e:
        log.warning("Stats-Sammlung fehlgeschlagen: %s", e)


async def check_anomalies():
    """Prüft Container-CPU und Zustandsänderungen auf Anomalien."""
    try:
        from app.anomaly import check_container_anomalies
        await check_container_anomalies()
    except Exception as e:
        log.debug("Anomalie-Job fehlgeschlagen: %s", e)


def start_scheduler():
    interval = config["dashboard"].get("stats_interval_seconds", 60)
    scheduler.add_job(collect_stats, "interval", seconds=interval, id="collect_stats")
    # Anomalie-Check alle 2 Minuten (unabhängig vom Stats-Intervall)
    scheduler.add_job(check_anomalies, "interval", seconds=120, id="check_anomalies",
                      misfire_grace_time=30)
    scheduler.start()
    log.info("Scheduler gestartet (Stats: %ds, Anomalien: 120s)", interval)


def stop_scheduler():
    scheduler.shutdown(wait=False)
