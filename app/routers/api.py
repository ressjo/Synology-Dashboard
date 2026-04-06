import asyncio
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app import synology, docker_manager, database, paperless_client
from app.hyperbackup import trigger_backup_ssh, get_top_processes, get_network_connections, get_memory_detail, get_nas_uptime, get_shared_folder_sizes, get_syslog
from app.config import config

templates = Jinja2Templates(directory="templates")

router = APIRouter(prefix="/api")


@router.get("/stats/live")
async def stats_live():
    try:
        util, storage, temp_info, mem_detail = await asyncio.gather(
            synology.get_utilization(),
            synology.get_storage_info(),
            synology.get_system_temp(),
            asyncio.to_thread(get_memory_detail),
        )

        cpu = util.get("cpu", {}).get("user_load", 0)
        sys_temp = temp_info.get("sys_temp")
        temp_warn = temp_info.get("temp_warn", False)

        # Leistungsschätzung: DS224+ Basis ~5W + J4125 TDP 10W * Last + SSDs ~3W
        # Quelle: Synology Spec Sheet (Access: 8.89W), kein Sensor vorhanden
        power_est = round(5 + (cpu / 100 * 10) + 3, 1)

        mem = util.get("memory", {})
        mem_total_kb = mem.get("total_real", 0)
        mem_avail_kb = mem.get("avail_real", 0)
        mem_used_kb  = mem_total_kb - mem_avail_kb
        mem_used_mb  = mem_used_kb // 1024
        mem_total_mb = mem_total_kb // 1024
        mem_pct      = round(mem_used_mb / mem_total_mb * 100, 1) if mem_total_mb else 0

        net = util.get("network", [])
        rx = sum(n.get("rx", 0) for n in net if isinstance(n, dict) and n.get("device") != "total")
        tx = sum(n.get("tx", 0) for n in net if isinstance(n, dict) and n.get("device") != "total")

        # Volumes (Speicherplatz) + physische Disks (Temperaturen)
        volumes = storage.get("volumes", [])
        disks = []
        for v in volumes:
            size = v.get("size", {})
            total = int(size.get("total", 0))
            used  = int(size.get("used", 0))
            disks.append({
                "name": v.get("vol_path", v.get("id", "?")),
                "used_gb": round(used / 1024 / 1024 / 1024, 1),
                "total_gb": round(total / 1024 / 1024 / 1024, 1),
                "pct": round(used / total * 100, 1) if total else 0,
            })

        phys_disks = [
            {
                "name": d.get("longName", d.get("name", "?")),
                "temp": d.get("temp"),
                "status": d.get("status", ""),
                "model": d.get("model", ""),
            }
            for d in storage.get("disks", [])
            if d.get("diskType") in ("SATA", "SAS", "NVMe", "M.2")
               or d.get("type") == "internal"
               or d.get("temp") is not None
        ]

        return {
            "cpu": cpu,
            "sys_temp": sys_temp,
            "temp_warn": temp_warn,
            "power_est": power_est,
            "mem_used_mb": mem_used_mb,
            "mem_total_mb": mem_total_mb,
            "mem_pct": mem_pct,
            "mem_app_mb":   mem_detail.get("app_mb"),
            "mem_cache_mb": mem_detail.get("cache_mb"),
            "mem_free_mb":  mem_detail.get("free_mb"),
            "net_rx_kb": round(rx / 1024, 1),
            "net_tx_kb": round(tx / 1024, 1),
            "disks": disks,
            "phys_disks": phys_disks,
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))



@router.get("/stats/history")
def stats_history(hours: int = 24):
    rows = database.get_stats_history(hours)
    return rows


@router.get("/stats/storage-history")
def storage_history(hours: int = 168):
    """
    Gibt Speicherbelegung als Zeitreihe zurück — inkl. linearer Prognose für 30 Tage.
    """
    rows = database.get_storage_history(hours)
    if not rows:
        return {"labels": [], "volumes": []}

    # Alle Volumes sammeln
    vol_names: list[str] = []
    for row in rows:
        for d in row["disks"]:
            if d["name"] not in vol_names:
                vol_names.append(d["name"])

    COLORS = ["#3b82f6", "#8b5cf6", "#22c55e", "#f59e0b", "#ef4444"]

    # Actual-Zeitreihe aufbauen
    actual_labels = [r["timestamp"][5:16].replace(" ", " ") for r in rows]  # "MM-DD HH:MM"
    actual_ts = [datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S") for r in rows]

    volumes_out = []
    for i, vol in enumerate(vol_names):
        actual_gb: list[float | None] = []
        total_gb = 0.0
        for row in rows:
            match = next((d for d in row["disks"] if d["name"] == vol), None)
            if match and match.get("total", 0) > 0:
                used_gb = round(match["used"] / 1e9, 2)
                total_gb = round(match["total"] / 1e9, 1)
                actual_gb.append(used_gb)
            else:
                actual_gb.append(None)

        # Lineare Regression für Prognose (nur nicht-None Punkte)
        valid = [(actual_ts[j], actual_gb[j]) for j in range(len(actual_gb)) if actual_gb[j] is not None]
        forecast_labels: list[str] = []
        forecast_gb: list[float | None] = []

        if len(valid) >= 2:
            t0, v0 = valid[0]
            t1, v1 = valid[-1]
            days_elapsed = max((t1 - t0).total_seconds() / 86400, 0.001)
            daily_growth = (v1 - v0) / days_elapsed  # GB/day

            # 30 Tage Prognose, tägliche Punkte
            last_ts = actual_ts[-1]
            last_val = v1
            for day in range(1, 31):
                ft = last_ts + timedelta(days=day)
                fv = round(last_val + daily_growth * day, 2)
                if total_gb > 0 and fv >= total_gb:
                    forecast_labels.append(ft.strftime("%m-%d"))
                    forecast_gb.append(round(total_gb, 2))
                    break
                forecast_labels.append(ft.strftime("%m-%d"))
                forecast_gb.append(max(fv, 0))

        # Forecast-Array muss gleich lang sein wie combined labels
        # actual_gb hat None für Forecast-Bereich → Chart.js verbindet nicht
        n_actual = len(actual_gb)
        n_forecast = len(forecast_gb)
        combined_actual   = actual_gb + [None] * n_forecast
        combined_forecast = [None] * (n_actual - 1) + [actual_gb[-1] if actual_gb else None] + forecast_gb
        combined_labels   = actual_labels + forecast_labels

        volumes_out.append({
            "name":     vol,
            "total_gb": total_gb,
            "color":    COLORS[i % len(COLORS)],
            "actual":   combined_actual,
            "forecast": combined_forecast,
            "labels":   combined_labels,
        })

    # Gemeinsame Labels (alle Volumes zusammenführen — normalerweise gleich)
    all_labels = volumes_out[0]["labels"] if volumes_out else []
    return {"labels": all_labels, "volumes": volumes_out}


# ── Notifications ─────────────────────────────────────────────

@router.get("/notifications")
def get_notifications():
    return {
        "notifications": database.get_notifications(30),
        "unread": database.count_unread_notifications(),
    }


@router.post("/notifications/read-all")
def read_all_notifications():
    database.mark_all_notifications_read()
    return {"status": "ok"}


@router.get("/stats/storage-growth")
def storage_growth():
    return database.get_storage_growth()


@router.get("/stats/processes")
def top_processes():
    return get_top_processes(5)


@router.get("/stats/connections")
def network_connections():
    return get_network_connections()


@router.get("/stats/sessions")
async def active_sessions():
    return await synology.get_active_sessions()


@router.get("/stats/shares")
async def shared_folders():
    return await asyncio.to_thread(get_shared_folder_sizes)


@router.get("/stats/disk-health")
async def disk_health():
    return await synology.get_disk_health()


@router.get("/logs/syslog", response_class=HTMLResponse)
async def syslog_entries(request: Request):
    entries = await asyncio.to_thread(get_syslog)
    return templates.TemplateResponse(
        "partials/syslog.html",
        {"request": request, "entries": entries},
    )


@router.get("/system/uptime")
async def system_uptime():
    uptime = await asyncio.to_thread(get_nas_uptime)
    return {"uptime": uptime}


@router.get("/links/status")
async def links_status():
    from app.services_db import get_sidebar_links
    service_links = get_sidebar_links()
    service_urls = {lnk["url"] for lnk in service_links}
    static_links = [lnk for lnk in config.get("quick_links", []) if lnk["url"] not in service_urls]
    links = static_links + service_links
    results: dict[str, bool] = {}

    async def check(url: str) -> bool:
        try:
            async with httpx.AsyncClient(verify=False, timeout=3) as client:
                r = await client.head(url, follow_redirects=True)
                return r.status_code < 500
        except Exception:
            return False

    statuses = await asyncio.gather(*[check(lnk["url"]) for lnk in links])
    for lnk, ok in zip(links, statuses):
        results[lnk["url"]] = ok
    return results


@router.post("/paperless/upload")
async def paperless_upload(file: UploadFile = File(...)):
    content = await file.read()
    result = paperless_client.upload_document(
        file.filename or "upload",
        content,
        file.content_type or "application/octet-stream",
    )
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@router.get("/containers/stats")
def container_stats():
    return docker_manager.get_container_stats()


@router.get("/containers/list")
def containers_list():
    """JSON-Liste aller Container (für Dashboard-Widget)."""
    return docker_manager.list_containers()


@router.get("/adguard/stats")
def adguard_stats():
    """AdGuard Home Statistiken."""
    from app import adguard
    return adguard.get_stats()


@router.get("/containers", response_class=HTMLResponse)
def containers(request: Request):
    items = docker_manager.list_containers()
    return templates.TemplateResponse(
        "partials/container_rows.html",
        {"request": request, "containers": items},
    )


@router.post("/containers/{container_id}/{action}")
def container_action(container_id: str, action: str):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="Ungültige Aktion")
    ok, msg = docker_manager.container_action(container_id, action)
    if not ok:
        raise HTTPException(status_code=500, detail=msg)
    return {"status": "ok", "message": msg}


@router.get("/paperless", response_class=HTMLResponse)
def paperless(request: Request):
    data = paperless_client.get_stats()
    return templates.TemplateResponse(
        "partials/paperless_widget.html",
        {"request": request, "paperless": data},
    )


@router.get("/logs", response_class=HTMLResponse)
async def logs(request: Request):
    sys_logs_raw, sec_events_raw = await asyncio.gather(
        synology.get_system_logs(40),
        synology.get_security_events(20),
        return_exceptions=True,
    )

    entries = []

    # Systemlogs
    if isinstance(sys_logs_raw, list):
        for e in sys_logs_raw:
            level = e.get("level", "info").lower()
            entries.append({
                "time": e.get("time", ""),
                "source": e.get("logtype", "System"),
                "message": e.get("descr", ""),
                "user": e.get("who", ""),
                "severity": "error" if level in ("err", "error", "crit") else "warning" if level == "warn" else "info",
                "icon": "alert-circle" if level in ("err", "error") else "alert-triangle" if level == "warn" else "info",
                "category": "system",
            })

    # Security Events
    if isinstance(sec_events_raw, list):
        for e in sec_events_raw:
            severity = e.get("severity", "low")
            args = e.get("str_args", {})
            str_id = e.get("str_id", "")
            msg = _format_security_event(str_id, args)
            entries.append({
                "time": e.get("create_time", ""),
                "source": "Sicherheit",
                "message": msg,
                "user": e.get("user", args.get("user", "")),
                "severity": "error" if severity == "high" else "warning" if severity == "medium" else "info",
                "icon": "shield",
                "category": "security",
            })

    # Nach Zeit sortieren (neueste zuerst)
    def _parse_time(t: str) -> str:
        return t or ""
    entries.sort(key=lambda x: _parse_time(x["time"]), reverse=True)

    return templates.TemplateResponse(
        "partials/logs.html",
        {"request": request, "entries": entries[:50]},
    )


def _format_security_event(str_id: str, args: dict) -> str:
    templates_map = {
        "abnormal_login":    "Unbekannter Login von {ip} ({protocol})",
        "login_success":     "Erfolgreicher Login von {ip} ({protocol})",
        "login_fail":        "Fehlgeschlagener Login von {ip} ({protocol})",
        "auto_block":        "IP {ip} automatisch blockiert",
        "auto_unblock":      "IP {ip} entsperrt",
        "user_locked":       "Benutzer {user} gesperrt",
    }
    tpl = templates_map.get(str_id, str_id.replace("_", " ").capitalize())
    try:
        return tpl.format(**args)
    except KeyError:
        return tpl


@router.get("/backup/summary", response_class=HTMLResponse)
async def backup_summary(request: Request):
    try:
        tasks = await synology.get_backup_tasks()
    except Exception:
        tasks = []

    # Status + DB-Logs parallel
    results = await asyncio.gather(
        asyncio.to_thread(database.get_last_backup_per_task),
        *[synology.get_task_status(t["task_id"]) for t in tasks],
        return_exceptions=True,
    )
    last_runs   = results[0] if isinstance(results[0], dict) else {}
    status_list = [r if isinstance(r, dict) else {} for r in results[1:]]

    summaries = []
    for task, status in zip(tasks, status_list):
        name       = task.get("name", f"Task {task['task_id']}")
        tid        = task["task_id"]
        last_run   = last_runs.get(tid, {})
        is_running = status.get("status") == "backup"

        summaries.append({
            "name":       name,
            "task_id":    tid,
            "is_running": is_running,
            "last_time":  last_run.get("timestamp"),   # aus backup_log
            "last_status": last_run.get("status", ""), # "gestartet" / "fehler"
        })
    summaries.sort(key=lambda x: x["task_id"])

    return templates.TemplateResponse(
        "partials/backup_summary.html",
        {"request": request, "summaries": summaries},
    )


@router.get("/backup/tasks", response_class=HTMLResponse)
async def backup_tasks(request: Request):
    try:
        tasks = await synology.get_backup_tasks()
    except Exception as e:
        return templates.TemplateResponse(
            "partials/backup_tasks.html",
            {"request": request, "tasks": [], "sizes": {}, "statuses": {}, "error": str(e)},
        )

    # Status aller Tasks parallel abfragen
    statuses = {}
    async def fetch_status(t):
        try:
            s = await synology.get_task_status(t["task_id"])
            statuses[t["task_id"]] = s
        except Exception:
            statuses[t["task_id"]] = {}
    await asyncio.gather(*[fetch_status(t) for t in tasks])

    return templates.TemplateResponse(
        "partials/backup_tasks.html",
        {"request": request, "tasks": tasks, "sizes": {}, "statuses": statuses, "error": None},
    )


@router.post("/backup/run/{task_id}")
async def run_backup(task_id: int):
    try:
        ok = await synology.trigger_backup(task_id)
        if ok:
            database.log_backup(task_id, f"Task {task_id}", "gestartet", "Via DSM API")
            return {"status": "ok", "method": "dsm_api"}
    except Exception:
        pass

    ok, msg = trigger_backup_ssh(task_id)
    if ok:
        database.log_backup(task_id, f"Task {task_id}", "gestartet", f"Via SSH: {msg}")
        return {"status": "ok", "method": "ssh", "message": msg}

    database.log_backup(task_id, f"Task {task_id}", "fehler", msg)
    raise HTTPException(status_code=500, detail=msg)


@router.get("/backup/logs", response_class=HTMLResponse)
def backup_logs(request: Request):
    logs = database.get_backup_logs()
    return templates.TemplateResponse(
        "partials/backup_logs.html",
        {"request": request, "backup_logs": logs},
    )
