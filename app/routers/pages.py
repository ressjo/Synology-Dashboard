import asyncio
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.config import config
from app import synology, docker_manager, database, paperless_client

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _base_ctx(request: Request) -> dict:
    from app.services_db import get_sidebar_links
    # Statische Links aus config.yaml + verwaltete Service-Links aus DB
    # Service-Links überschreiben/ersetzen statische Links mit gleicher URL
    static_links = config.get("quick_links", [])
    service_links = get_sidebar_links()
    service_urls = {lnk["url"] for lnk in service_links}
    merged = [lnk for lnk in static_links if lnk["url"] not in service_urls] + service_links
    return {
        "request": request,
        "title": config["dashboard"].get("title", "Synology Dashboard"),
        "quick_links": merged,
        "refresh": config["dashboard"].get("refresh_interval_seconds", 5),
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ctx = _base_ctx(request)
    try:
        ctx["sys_info"], ctx["update_info"] = await asyncio.gather(
            synology.get_system_info(),
            synology.get_update_status(),
        )
    except Exception:
        ctx["sys_info"] = {}
        ctx["update_info"] = {"update_available": False}
    ctx["docker_counts"] = docker_manager.get_container_count()
    from app.services_db import get_service as _get_svc
    _pl = _get_svc("paperless") or config.get("paperless", {})
    ctx["paperless_url"] = _pl.get("url", "#")
    ctx["has_paperless"] = bool(_pl.get("url") and (_pl.get("token") or config.get("paperless", {}).get("token")))
    _docker = _get_svc("portainer") or config.get("portainer", {})
    ctx["has_docker"] = bool(_docker and _docker.get("url"))
    _ag = _get_svc("adguard")
    ctx["has_adguard"] = bool(_ag and _ag.get("url"))
    return templates.TemplateResponse("index.html", ctx)


@router.get("/containers", response_class=HTMLResponse)
async def containers_page(request: Request):
    ctx = _base_ctx(request)
    ctx["containers"] = docker_manager.list_containers()
    return templates.TemplateResponse("containers.html", ctx)


@router.get("/backup", response_class=HTMLResponse)
async def backup_page(request: Request):
    ctx = _base_ctx(request)
    ctx["backup_logs"] = database.get_backup_logs(10)
    return templates.TemplateResponse("backup.html", ctx)


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    ctx = _base_ctx(request)
    return templates.TemplateResponse("stats.html", ctx)


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    ctx = _base_ctx(request)
    return templates.TemplateResponse("logs.html", ctx)
