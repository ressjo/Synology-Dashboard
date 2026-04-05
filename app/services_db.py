"""
Service-Konfiguration (Paperless, Portainer, Photos, AdGuard) in SQLite.
Wird über die UI verwaltet — keine manuelle Dateibearbeitung nötig.
"""
import json
from typing import Optional
from app.database import get_db

# Felddefinitionen pro Service-Typ — legt fest, welche Felder im UI erscheinen.
# quick_link=False → erscheint nicht als Sidebar-Link (reine Backend-Verbindung).
SERVICE_DEFINITIONS: dict[str, dict] = {
    "synology": {
        "label": "Synology DSM",
        "description": "NAS-Verbindung & API (benötigt für alle Dashboard-Daten)",
        "icon": "server",
        "color": "#3b82f6",
        "quick_link": False,
        "fields": [
            {"key": "host",      "label": "IP-Adresse / Hostname", "type": "text",     "placeholder": "192.168.1.100",  "required": True},
            {"key": "port",      "label": "Port",                  "type": "number",   "placeholder": "5001",           "required": True,  "default": "5001"},
            {"key": "username",  "label": "Benutzername",          "type": "text",     "placeholder": "dashboard",      "required": True},
            {"key": "password",  "label": "Passwort",              "type": "password", "placeholder": "",               "required": True},
            {"key": "use_https", "label": "Protokoll",             "type": "select",   "required": True,
             "options": [{"value": "true", "label": "HTTPS"}, {"value": "false", "label": "HTTP"}]},
        ],
    },
    "ssh": {
        "label": "SSH",
        "description": "Sicherer Shell-Zugang (Syslog, Ordnergrößen, Backup-Trigger)",
        "icon": "terminal",
        "color": "#64748b",
        "quick_link": False,
        "fields": [
            {"key": "host",     "label": "IP-Adresse / Hostname", "type": "text",     "placeholder": "192.168.1.100", "required": True},
            {"key": "port",     "label": "Port",                  "type": "number",   "placeholder": "22",            "required": True,  "default": "22"},
            {"key": "username", "label": "Benutzername",          "type": "text",     "placeholder": "dashboard",     "required": True},
            {"key": "password", "label": "Passwort",              "type": "password", "placeholder": "",              "required": False},
            {"key": "key_path", "label": "SSH-Key Pfad",          "type": "text",     "placeholder": "/config/id_rsa","required": False,
             "hint": "Passwort oder Key — mindestens eines angeben"},
        ],
    },
    "portainer": {
        "label": "Portainer",
        "description": "Container-Verwaltung",
        "icon": "box",
        "color": "#f59e0b",
        "quick_link": True,
        "fields": [
            {"key": "url",      "label": "URL",          "type": "url",      "placeholder": "http://192.168.1.1:9000", "required": True},
            {"key": "username", "label": "Benutzername", "type": "text",     "placeholder": "admin",                   "required": True},
            {"key": "password", "label": "Passwort",     "type": "password", "placeholder": "",                        "required": True},
        ],
    },
    "paperless": {
        "label": "Paperless-ngx",
        "description": "Dokumenten-Management",
        "icon": "file-text",
        "color": "#22c55e",
        "quick_link": True,
        "fields": [
            {"key": "url",   "label": "URL",       "type": "url",      "placeholder": "http://192.168.1.1:8010", "required": True},
            {"key": "token", "label": "API Token", "type": "password", "placeholder": "",                       "required": True},
        ],
    },
    "photos": {
        "label": "Synology Photos",
        "description": "Foto-Galerie",
        "icon": "image",
        "color": "#a855f7",
        "quick_link": True,
        "fields": [
            {"key": "url", "label": "URL", "type": "url", "placeholder": "https://192.168.1.1:5001/?launchApp=SYNO.Foto.AppInstance", "required": True},
        ],
    },
    "adguard": {
        "label": "AdGuard Home",
        "description": "DNS-Werbeblocker",
        "icon": "shield",
        "color": "#ef4444",
        "quick_link": True,
        "fields": [
            {"key": "url",      "label": "URL",          "type": "url",      "placeholder": "http://192.168.1.1:3000", "required": True},
            {"key": "username", "label": "Benutzername", "type": "text",     "placeholder": "admin",                   "required": False},
            {"key": "password", "label": "Passwort",     "type": "password", "placeholder": "",                        "required": False},
        ],
    },
}


def _key(name: str) -> str:
    return f"service_{name}"


def get_service(name: str) -> Optional[dict]:
    """Gibt die gespeicherte Konfiguration eines Service zurück, oder None."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (_key(name),)
            ).fetchone()
            if row:
                return json.loads(row["value"])
    except Exception:
        pass
    return None


def set_service(name: str, data: dict) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (_key(name), json.dumps(data)),
        )
        conn.commit()


def delete_service(name: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (_key(name),))
        conn.commit()


def get_all_services() -> dict[str, Optional[dict]]:
    """Gibt für jeden bekannten Service-Typ die gespeicherte Konfig zurück (None = nicht konfiguriert)."""
    return {name: get_service(name) for name in SERVICE_DEFINITIONS}


def get_sidebar_links() -> list[dict]:
    """Generiert Quick-Link-Einträge für die Sidebar aus konfigurierten Services (nur quick_link=True)."""
    links = []
    for name, defn in SERVICE_DEFINITIONS.items():
        if not defn.get("quick_link", True):
            continue
        cfg = get_service(name)
        if cfg and cfg.get("url"):
            links.append({
                "name":  defn["label"],
                "url":   cfg["url"],
                "icon":  defn["icon"],
                "color": defn["color"],
            })
    return links
