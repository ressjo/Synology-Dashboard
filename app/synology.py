import httpx
import asyncio
from typing import Optional

_session_id: Optional[str] = None
_lock = asyncio.Lock()


def _get_cfg() -> dict:
    """Liest DSM-Konfiguration aus DB; fällt auf config.yaml zurück."""
    from app.services_db import get_service
    cfg = get_service("synology")
    if cfg:
        return cfg
    from app.config import config
    return config.get("synology", {})


def _base() -> str:
    cfg = _get_cfg()
    use_https_raw = cfg.get("use_https", True)
    if isinstance(use_https_raw, str):
        use_https = use_https_raw.lower() == "true"
    else:
        use_https = bool(use_https_raw)
    protocol = "https" if use_https else "http"
    host = cfg.get("host", "")
    port = int(cfg.get("port", 5001))
    return f"{protocol}://{host}:{port}/webapi"


async def _request(params: dict, timeout: int = 10) -> dict:
    async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
        resp = await client.get(f"{_base()}/entry.cgi", params=params)
        resp.raise_for_status()
        return resp.json()


async def login() -> str:
    cfg = _get_cfg()
    data = await _request({
        "api": "SYNO.API.Auth",
        "version": "6",
        "method": "login",
        "account": cfg["username"],
        "passwd": cfg["password"],
        "session": "dashboard",
        "format": "sid",
    })
    if not data.get("success"):
        raise RuntimeError(f"Synology login failed: {data.get('error')}")
    return data["data"]["sid"]


async def get_sid() -> str:
    global _session_id
    async with _lock:
        if not _session_id:
            _session_id = await login()
    return _session_id


async def _authed_request(params: dict) -> dict:
    global _session_id
    sid = await get_sid()
    params["_sid"] = sid
    data = await _request(params)
    # Session expired → re-login once
    if not data.get("success") and data.get("error", {}).get("code") in (105, 106, 107, 119):
        async with _lock:
            _session_id = await login()
        params["_sid"] = _session_id
        data = await _request(params)
    return data


async def get_system_info() -> dict:
    import re
    data = await _authed_request({"api": "SYNO.DSM.Info", "version": "2", "method": "getinfo"})
    d = data.get("data", {})
    # Extrahiere saubere Version z.B. "7.3.2" aus "DSM 7.3.2-86009 Update 3"
    m = re.search(r"(\d+\.\d+\.\d+)", d.get("version_string", ""))
    d["dsm_version"] = m.group(1) if m else d.get("version_string", "")
    return d


async def get_update_status() -> dict:
    """Prüft ob ein DSM-Update verfügbar ist."""
    data = await _authed_request({"api": "SYNO.Core.Upgrade", "version": "2", "method": "status"})
    d = data.get("data", {})
    status = d.get("status", "none")
    return {
        "update_available": status not in ("none", ""),
        "status": status,
    }


async def get_active_sessions() -> list[dict]:
    """Aktive DSM/SMB/FTP-Sitzungen via SYNO.Core.CurrentConnection."""
    data = await _authed_request({"api": "SYNO.Core.CurrentConnection", "version": "1", "method": "list"})
    items = data.get("data", {}).get("items", [])
    # Deduplizieren: pro User+Protokoll nur einen Eintrag
    seen: set = set()
    result = []
    for item in items:
        key = (item.get("who"), item.get("type"))
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "user":     item.get("who", ""),
            "protocol": item.get("type", ""),
            "from_ip":  item.get("from", ""),
            "time":     item.get("time", ""),
        })
    return result


async def get_disk_health() -> list[dict]:
    """Disk-Gesundheit (SMART, Temperatur, SSD-Lebensdauer) aus Storage-API."""
    data = await _authed_request({
        "api": "SYNO.Storage.CGI.Storage",
        "version": "1",
        "method": "load_info",
        "loadRXInfo": "true",
    })
    disks = data.get("data", {}).get("disks", [])
    result = []
    for d in disks:
        if d.get("temp") is None and not d.get("smart_status"):
            continue
        remain = d.get("remain_life", {})
        result.append({
            "name":         d.get("longName", d.get("name", "")),
            "model":        d.get("model", ""),
            "is_ssd":       d.get("isSsd", False),
            "temp":         d.get("temp"),
            "smart_status": d.get("smart_status", ""),
            "status":       d.get("status", ""),
            "remain_life":  remain.get("value") if remain.get("trustable") else None,
            "size_gb":      round(int(d.get("size_total", 0)) / 1e9, 0),
        })
    return result


async def get_system_temp() -> dict:
    """CPU/System-Temperatur via SYNO.Core.System."""
    data = await _authed_request({"api": "SYNO.Core.System", "version": "2", "method": "info"})
    d = data.get("data", {})
    return {
        "sys_temp": d.get("sys_temp"),
        "temp_warn": d.get("sys_tempwarn", False),
    }


async def get_utilization() -> dict:
    """CPU, RAM, Netzwerk, Disk I/O"""
    data = await _authed_request({
        "api": "SYNO.Core.System.Utilization",
        "version": "1",
        "method": "get",
    })
    return data.get("data", {})


async def get_storage_info() -> dict:
    """Volumes und Disk-Status"""
    data = await _authed_request({
        "api": "SYNO.Storage.CGI.Storage",
        "version": "1",
        "method": "load_info",
        "loadRXInfo": "true",
    })
    return data.get("data", {})


async def get_backup_tasks() -> list[dict]:
    """HyperBackup Aufgaben via DSM API"""
    data = await _authed_request({
        "api": "SYNO.Backup.Task",
        "version": "1",
        "method": "list",
    })
    if not data.get("success"):
        return []
    return data.get("data", {}).get("task_list", [])


async def get_system_logs(limit: int = 30) -> list[dict]:
    """DSM Systemlog – alle Levels."""
    data = await _authed_request({
        "api": "SYNO.Core.SyslogClient.Log",
        "version": "1",
        "method": "list",
        "limit": limit,
        "offset": 0,
    })
    return data.get("data", {}).get("items", [])


async def get_security_events(limit: int = 20) -> list[dict]:
    """Security Advisor Login-Aktivitäten."""
    data = await _authed_request({
        "api": "SYNO.SecurityAdvisor.LoginActivity",
        "version": "1",
        "method": "list",
        "limit": limit,
        "offset": 0,
    })
    return data.get("data", {}).get("items", [])


async def get_task_status(task_id: int) -> dict:
    """Status einer einzelnen Backup-Aufgabe (state + evtl. Fortschritt)."""
    data = await _authed_request({
        "api": "SYNO.Backup.Task",
        "version": "1",
        "method": "status",
        "task_id": task_id,
    })
    return data.get("data", {})


async def get_task_detail(task_id: int) -> dict:
    """Detailinfos einer Backup-Aufgabe: letztes Ergebnis, Größe, Dauer, Zeitplan."""
    data = await _authed_request({
        "api": "SYNO.Backup.Task",
        "version": "1",
        "method": "get",
        "task_id": task_id,
    })
    return data.get("data", {})


async def trigger_backup(task_id: int) -> bool:
    """HyperBackup Aufgabe starten"""
    data = await _authed_request({
        "api": "SYNO.Backup.Task",
        "version": "1",
        "method": "backup",
        "task_id": task_id,
    })
    return data.get("success", False)
