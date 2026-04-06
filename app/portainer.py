"""
Portainer API Client für Container-Management.
Wird als primäre Quelle verwendet; Docker-Socket als Fallback.
"""
import httpx
from typing import Optional

# Zwischengespeichertes JWT von Portainer (kein Passwort — wird bei 401 neu geholt)
_jwt_token: Optional[str] = None
_endpoint_id_cache: Optional[int] = None


def _get_cfg() -> dict:
    """Liest Konfiguration aus DB; fällt auf config.yaml zurück (Abwärtskompatibilität)."""
    from app.services_db import get_service
    cfg = get_service("portainer")
    if cfg:
        return cfg
    from app.config import config
    return config.get("portainer", {})


def _base() -> str:
    return _get_cfg().get("url", "").rstrip("/")


def _headers() -> dict:
    return {"Authorization": f"Bearer {_jwt_token}"} if _jwt_token else {}


def login() -> bool:
    global _jwt_token
    cfg = _get_cfg()
    base = cfg.get("url", "").rstrip("/")
    if not base or not cfg.get("password"):
        return False
    try:
        resp = httpx.post(
            f"{base}/api/auth",
            json={"username": cfg.get("username", "admin"), "password": cfg["password"]},
            timeout=8,
        )
        resp.raise_for_status()
        _jwt_token = resp.json().get("jwt")
        return bool(_jwt_token)
    except Exception:
        return False


def _endpoint_id() -> int:
    """Endpoint-ID einmalig ermitteln und cachen."""
    global _endpoint_id_cache
    if _endpoint_id_cache is not None:
        return _endpoint_id_cache
    try:
        resp = httpx.get(f"{_base()}/api/endpoints", headers=_headers(), timeout=8)
        resp.raise_for_status()
        endpoints = resp.json()
        _endpoint_id_cache = endpoints[0]["Id"] if endpoints else 1
        return _endpoint_id_cache
    except Exception:
        return 1


def _get(path: str):
    resp = httpx.get(f"{_base()}{path}", headers=_headers(), timeout=8)
    if resp.status_code == 401:
        login()
        resp = httpx.get(f"{_base()}{path}", headers=_headers(), timeout=8)
    resp.raise_for_status()
    return resp.json()


def _post(path: str):
    resp = httpx.post(f"{_base()}{path}", headers=_headers(), timeout=15)
    if resp.status_code == 401:
        login()
        resp = httpx.post(f"{_base()}{path}", headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp


def list_containers() -> list[dict]:
    eid = _endpoint_id()
    raw = _get(f"/api/endpoints/{eid}/docker/containers/json?all=1")
    result = []
    for c in raw:
        ports = []
        for p in c.get("Ports", []):
            if p.get("PublicPort"):
                ports.append(f"{p['PublicPort']}→{p['PrivatePort']}/{p.get('Type','tcp')}")

        name = c.get("Names", ["?"])[0].lstrip("/")
        image = c.get("Image", "?")
        status = c.get("State", "unknown")

        from datetime import datetime, timezone
        try:
            created_str = datetime.fromtimestamp(c.get("Created", 0), tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            created_str = "?"

        result.append({
            "id": c["Id"][:12],
            "full_id": c["Id"],
            "name": name,
            "image": image,
            "status": c.get("Status", status),
            "state": _map_state(status),
            "ports": ", ".join(ports) if ports else "-",
            "created": created_str,
        })
    result.sort(key=lambda x: (x["state"] != "running", x["name"]))
    return result


def container_action(container_id: str, action: str) -> tuple[bool, str]:
    eid = _endpoint_id()
    try:
        _post(f"/api/endpoints/{eid}/docker/containers/{container_id}/{action}")
        return True, "OK"
    except httpx.HTTPStatusError as e:
        return False, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        return False, str(e)


def _map_state(status: str) -> str:
    s = status.lower()
    if s == "running":
        return "running"
    if s in ("exited", "dead"):
        return "stopped"
    if s == "paused":
        return "paused"
    return "other"


def get_container_stats_batch() -> dict[str, dict]:
    """CPU% + RAM für alle laufenden Container via Docker Stats API."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    eid = _endpoint_id()
    try:
        raw = _get(f"/api/endpoints/{eid}/docker/containers/json?all=0")
    except Exception:
        return {}

    results: dict[str, dict] = {}

    def fetch(container_id: str, short_id: str) -> tuple[str, dict]:
        try:
            data = _get(
                f"/api/endpoints/{eid}/docker/containers/{container_id}"
                f"/stats?stream=false&one-shot=true"
            )
            cpu_delta = (
                data["cpu_stats"]["cpu_usage"]["total_usage"]
                - data["precpu_stats"]["cpu_usage"]["total_usage"]
            )
            sys_delta = (
                data["cpu_stats"].get("system_cpu_usage", 0)
                - data["precpu_stats"].get("system_cpu_usage", 0)
            )
            num_cpus = data["cpu_stats"].get("online_cpus") or len(
                data["cpu_stats"]["cpu_usage"].get("percpu_usage") or [1]
            )
            cpu_pct = (cpu_delta / sys_delta * num_cpus * 100) if sys_delta > 0 else 0.0

            mem = data.get("memory_stats", {})
            cache = mem.get("stats", {}).get("cache", 0)
            used = max(mem.get("usage", 0) - cache, 0)
            limit = mem.get("limit", 1) or 1
            mem_pct = used / limit * 100

            return short_id, {
                "cpu_pct": round(cpu_pct, 1),
                "mem_mb":  round(used / 1024 / 1024, 1),
                "mem_pct": round(mem_pct, 1),
            }
        except Exception:
            return short_id, {"cpu_pct": None, "mem_mb": None, "mem_pct": None}

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch, c["Id"], c["Id"][:12]) for c in raw}
        for future in as_completed(futures, timeout=8):
            try:
                short_id, stats = future.result()
                results[short_id] = stats
            except Exception:
                pass

    return results


def is_available() -> bool:
    cfg = _get_cfg()
    return bool(cfg.get("url") and cfg.get("password"))
