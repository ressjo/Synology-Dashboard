from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Annotated
from app import auth

router = APIRouter()
templates = Jinja2Templates(directory="templates")


# ── Setup (Ersteinrichtung) ───────────────────────────────────
@router.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request):
    if auth.is_setup_done():
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("setup.html", {"request": request})


@router.post("/setup", response_class=HTMLResponse)
async def setup_post(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password2: Annotated[str, Form()],
):
    if auth.is_setup_done():
        return RedirectResponse("/", status_code=302)

    error = None
    if not username or len(username) < 2:
        error = "Benutzername muss mindestens 2 Zeichen haben."
    elif not password or len(password) < 6:
        error = "Passwort muss mindestens 6 Zeichen haben."
    elif password != password2:
        error = "Passwörter stimmen nicht überein."

    if error:
        return templates.TemplateResponse(
            "setup.html", {"request": request, "error": error, "username": username}
        )

    auth.set_credentials(username, password)
    token = auth.create_session()
    response = RedirectResponse("/", status_code=302)
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=auth.SESSION_TTL)
    return response


# ── Login ─────────────────────────────────────────────────────
@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request, next: str = "/"):
    if auth.validate_session(request.cookies.get("session")):
        return RedirectResponse(next or "/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "next": next})


@router.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
):
    creds = auth.get_credentials()
    if creds and creds[0] == username and auth.verify_password(password, creds[1]):
        token = auth.create_session()
        redirect_to = next if next and next.startswith("/") and not next.startswith("//") else "/"
        response = RedirectResponse(redirect_to, status_code=302)
        response.set_cookie("session", token, httponly=True, samesite="lax", max_age=auth.SESSION_TTL)
        return response

    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Benutzername oder Passwort falsch.", "username": username, "next": next},
        status_code=401,
    )


# ── Logout ────────────────────────────────────────────────────
@router.post("/logout")
async def logout(request: Request):
    auth.revoke_session(request.cookies.get("session"))
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session")
    return response


# ── Einstellungen: Passwort ändern ────────────────────────────
@router.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request):
    creds = auth.get_credentials()
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "username": creds[0] if creds else ""},
    )


@router.post("/settings", response_class=HTMLResponse)
async def settings_post(
    request: Request,
    username: Annotated[str, Form()],
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    new_password2: Annotated[str, Form()],
):
    creds = auth.get_credentials()
    error = None
    success = None

    if not creds or not auth.verify_password(current_password, creds[1]):
        error = "Aktuelles Passwort ist falsch."
    elif not username or len(username) < 2:
        error = "Benutzername muss mindestens 2 Zeichen haben."
    elif not new_password or len(new_password) < 6:
        error = "Neues Passwort muss mindestens 6 Zeichen haben."
    elif new_password != new_password2:
        error = "Neue Passwörter stimmen nicht überein."
    else:
        auth.set_credentials(username, new_password)
        success = "Anmeldedaten erfolgreich geändert."

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "username": username,
            "error": error,
            "success": success,
        },
    )
