from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.services_db import SERVICE_DEFINITIONS, get_all_services, set_service, delete_service

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _invalidate_cache(name: str) -> None:
    """Leert In-Memory-Caches der jeweiligen Service-Clients nach Konfigurationsänderung."""
    if name == "portainer":
        import app.portainer as _pt
        _pt._jwt_token = None
        _pt._endpoint_id_cache = None
    elif name == "synology":
        import app.synology as _syn
        _syn._session_id = None
    elif name == "ssh":
        import app.hyperbackup as _hb
        _hb._ssh_client = None


@router.get("/services-config", response_class=HTMLResponse)
async def services_get(request: Request):
    return templates.TemplateResponse(
        "services_config.html",
        {
            "request": request,
            "title": "Dienste",
            "defs": SERVICE_DEFINITIONS,
            "services": get_all_services(),
            "saved": request.query_params.get("saved"),
            "deleted": request.query_params.get("deleted"),
        },
    )


@router.post("/services-config/{name}/save")
async def services_save(name: str, request: Request):
    if name not in SERVICE_DEFINITIONS:
        return RedirectResponse("/services-config", status_code=302)

    form = await request.form()
    existing = get_all_services().get(name) or {}
    data: dict = {}
    for field in SERVICE_DEFINITIONS[name]["fields"]:
        value = (form.get(field["key"]) or "").strip()
        if value:
            data[field["key"]] = value
        elif field["type"] == "password" and existing.get(field["key"]):
            # Passwort leer gelassen → altes beibehalten
            data[field["key"]] = existing[field["key"]]
        elif value == "" and not field["required"]:
            pass  # Optionales leeres Feld weglassen
        else:
            data[field["key"]] = value
    set_service(name, data)

    # Caches invalidieren damit neue Credentials sofort gelten
    _invalidate_cache(name)

    return RedirectResponse(f"/services-config?saved={name}", status_code=302)


@router.post("/services-config/{name}/delete")
async def services_delete(name: str):
    if name in SERVICE_DEFINITIONS:
        delete_service(name)
        _invalidate_cache(name)
    return RedirectResponse(f"/services-config?deleted={name}", status_code=302)
