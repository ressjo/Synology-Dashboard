# Synology Dashboard — CLAUDE.md

## Projektübersicht

FastAPI-basiertes Web-Dashboard für eine Synology NAS (DS224+). Läuft als Docker-Container direkt auf dem NAS (`network_mode: host`). Zeigt Echtzeit-Statistiken, Container-Status, HyperBackup-Zustand und Logs.

## Stack

- **Backend:** Python 3.12, FastAPI, APScheduler, SQLite (via stdlib `sqlite3`)
- **Templates:** Jinja2 (Server-Side Rendering), HTMX für partielle Updates
- **HTTP-Client:** `httpx` (async für Synology/AdGuard, sync für Portainer)
- **SSH:** `paramiko` (für HyperBackup-Trigger, Syslog, Prozess-Infos)
- **Containerisierung:** Docker, `docker-compose.yml`

## Architektur

```
app/
├── main.py              # FastAPI App, Middleware (Auth, Security Headers, GZip)
├── config.py            # config.yaml laden + Env-Overrides (_ENV_OVERRIDES)
├── database.py          # SQLite-Zugriff via @contextmanager get_db()
├── auth.py              # PBKDF2-SHA256 Passwort-Hashing, In-Memory Sessions
├── scheduler.py         # APScheduler: Stats (60s), Anomalien (120s), Cleanup (03:00)
├── synology.py          # Async DSM-API Client (httpx), Auto-Relogin bei 401
├── hyperbackup.py       # SSH-basierte NAS-Infos (Backup, Syslog, Prozesse, Netzwerk)
├── docker_manager.py    # Portainer (primär) → Docker-Socket (Fallback)
├── portainer.py         # Synchroner Portainer-API Client, ThreadPoolExecutor für Stats
├── anomaly.py           # Container-CPU Anomalie-Erkennung (EMA-Baseline), State-Alerts
├── paperless_client.py  # Paperless-ngx API Client
├── adguard.py           # AdGuard Home API Client
├── services_db.py       # Service-Konfiguration (DB-CRUD), SERVICE_DEFINITIONS
└── routers/
    ├── api.py           # /api/* Endpunkte (Stats, Logs, Container, Backup, etc.)
    ├── auth.py          # /login, /logout, /setup, /settings
    ├── pages.py         # HTML-Seitenrouten
    └── services_settings.py  # UI für Service-Konfiguration
```

## Konfiguration

- `config.yaml` — Primäre Konfiguration (Synology-Host, SSH, Quick Links, Dashboard-Einstellungen)
- `.env` — Secrets (Passwörter via Env-Variablen: `SYNO_PASSWORD`, `SSH_PASSWORD`, `PAPERLESS_TOKEN`, `PORTAINER_PASSWORD`)
- **Services** (Portainer, Paperless, AdGuard, Photos) werden über die Dashboard-UI konfiguriert und in SQLite gespeichert — nicht in `config.yaml`
- `config.yaml` hat Vorrang vor DB für Synology/SSH; DB hat Vorrang für alle anderen Services

## Datenbank

SQLite unter `data/dashboard.db`. Schema:

| Tabelle | Inhalt |
|---|---|
| `stats` | Zeitreihen-Daten (CPU, RAM, Disk, Netzwerk, Temp) — alle 60s |
| `backup_log` | Dashboard-seitige Backup-Trigger-Historie |
| `settings` | Key-Value Store (Admin-Credentials, Service-Konfigurationen) |
| `notifications` | System-Benachrichtigungen (Container-Anomalien, etc.) |
| `container_baselines` | EMA-Baselines für CPU-Anomalie-Erkennung |

`get_db()` ist ein `@contextmanager` der die Verbindung nach dem `with`-Block immer schließt.

## Auth-System

- Einmaliges Setup über `/setup` (Credentials in DB als PBKDF2-Hash)
- Sessions: In-Memory Dict `{token: expires_at}`, TTL 8h, verlieren sich bei Neustart
- Cookie: `httponly=True`, `samesite="lax"`, kein `secure`-Flag (LAN-only)
- `AuthMiddleware` in `main.py` schützt alle Routen außer `/login`, `/setup`, `/static`

## Wichtige Konventionen

**Imports:** Alle Imports gehören an den Dateianfang — keine Inline-Imports innerhalb von Funktionen (außer für zirkuläre Abhängigkeiten, die lazy aufgelöst werden müssen).

**Async vs. Sync:** 
- Synology API: vollständig async (`httpx.AsyncClient`)
- Portainer/Docker: sync, wird von FastAPI in Thread-Pool ausgeführt (sync-Routen mit `def`)
- SSH-Calls (hyperbackup.py): sync, werden mit `asyncio.to_thread()` gewrappt

**Konfiguration lesen:** Immer `_get_cfg()` nutzen (liest aus DB, fällt auf `config.yaml` zurück) — nie direkt `from app.config import config` in Service-Clients.

**SSL:** `verify_ssl` in der Synology-Config steuerbar (default `false` für selbstsignierte Zertifikate).

## Lokale Entwicklung

```bash
pip install -r requirements.txt
cp .env.example .env  # Secrets eintragen
# config.yaml anpassen (NAS-IP etc.)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Docker Deploy (auf dem NAS)

```bash
docker build -t synology-dashboard .
docker-compose up -d
```

Volumes: `config.yaml` und `.env` werden read-only eingebunden, `data/` ist persistent.

## API-Endpunkte (Übersicht)

| Endpunkt | Beschreibung |
|---|---|
| `GET /api/stats/live` | Echtzeit CPU/RAM/Disk/Netz/Temp |
| `GET /api/stats/history` | Zeitreihen-History (default 24h) |
| `GET /api/stats/storage-history` | Speicher-Verlauf + 30-Tage-Prognose |
| `GET /api/containers/list` | Container-Liste (Portainer/Docker) |
| `POST /api/containers/{id}/{action}` | start/stop/restart |
| `GET /api/backup/summary` | HyperBackup-Zusammenfassung |
| `POST /api/backup/run/{task_id}` | Backup triggern (DSM API → SSH Fallback) |
| `GET /api/logs` | System- + Security-Logs |
| `GET /api/logs/syslog` | /var/log/messages (SSH) |
| `GET /api/notifications` | Dashboard-Benachrichtigungen |
| `GET /api/adguard/stats` | AdGuard Home Statistiken |
| `POST /api/paperless/upload` | Dokument in Paperless hochladen |
