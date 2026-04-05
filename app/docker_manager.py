"""
Container-Management: Portainer API (primär) → Docker Socket (Fallback).
"""
from __future__ import annotations
from app import portainer


def _use_portainer() -> bool:
    return portainer.is_available()


def list_containers(all_containers: bool = True) -> list[dict]:
    if _use_portainer():
        try:
            if not portainer._jwt_token:
                portainer.login()
            return portainer.list_containers()
        except Exception as e:
            return [{"error": f"Portainer: {e}"}]
    return _docker_list(all_containers)


def container_action(container_id: str, action: str) -> tuple[bool, str]:
    if _use_portainer():
        try:
            if not portainer._jwt_token:
                portainer.login()
            return portainer.container_action(container_id, action)
        except Exception as e:
            return False, str(e)
    return _docker_action(container_id, action)


def get_container_stats() -> dict[str, dict]:
    """CPU% + RAM pro Container (nur Portainer-Pfad unterstützt)."""
    if _use_portainer():
        try:
            if not portainer._jwt_token:
                portainer.login()
            return portainer.get_container_stats_batch()
        except Exception:
            return {}
    return {}


def get_container_count() -> dict:
    containers = list_containers()
    if containers and "error" in containers[0]:
        return {"total": 0, "running": 0, "stopped": 0}
    running = sum(1 for c in containers if c.get("state") == "running")
    return {"total": len(containers), "running": running, "stopped": len(containers) - running}


# ── Docker Socket Fallback ────────────────────────────────────

def _docker_list(all_containers: bool = True) -> list[dict]:
    try:
        import docker
        client = docker.from_env()
        containers = client.containers.list(all=all_containers)
        result = []
        for c in containers:
            ports = []
            for internal, bindings in (c.ports or {}).items():
                if bindings:
                    for b in bindings:
                        ports.append(f"{b['HostPort']}→{internal}")
                else:
                    ports.append(internal)
            result.append({
                "id": c.short_id,
                "full_id": c.id,
                "name": c.name,
                "image": c.image.tags[0] if c.image.tags else c.image.short_id,
                "status": c.status,
                "state": _map_state(c.status),
                "ports": ", ".join(ports) if ports else "-",
                "created": c.attrs.get("Created", "")[:10],
            })
        result.sort(key=lambda x: (x["state"] != "running", x["name"]))
        return result
    except Exception as e:
        return [{"error": str(e)}]


def _docker_action(container_id: str, action: str) -> tuple[bool, str]:
    try:
        import docker
        client = docker.from_env()
        containers = client.containers.list(all=True)
        target = next((c for c in containers if c.short_id == container_id or c.name == container_id), None)
        if not target:
            return False, "Container nicht gefunden"
        getattr(target, action)()
        return True, "OK"
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
