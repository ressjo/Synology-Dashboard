"""
AdGuard Home API-Client — liest Statistiken aus der AdGuard-REST-API.
"""
import requests
from app.services_db import get_service


def get_stats() -> dict:
    """Gibt AdGuard-Statistiken zurück oder {} bei Fehler / nicht konfiguriert."""
    cfg = get_service("adguard")
    if not cfg or not cfg.get("url"):
        return {}
    url = cfg["url"].rstrip("/")
    auth = None
    if cfg.get("username") and cfg.get("password"):
        auth = (cfg["username"], cfg["password"])
    try:
        r = requests.get(f"{url}/control/stats", auth=auth, timeout=5, verify=False)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}
