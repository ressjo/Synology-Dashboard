import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, RedirectResponse
from app.database import init_db
from app.scheduler import start_scheduler, stop_scheduler
from app.routers import api, pages
from app.routers import auth as auth_router
from app.routers import services_settings as services_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s – %(message)s")

# Pfade die ohne Login erreichbar sind
_PUBLIC_PATHS = {"/login", "/setup", "/static"}


# ── Auth-Middleware ───────────────────────────────────────────
class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Statische Dateien + Auth-Routen immer erlauben
        if any(path.startswith(p) for p in _PUBLIC_PATHS):
            return await call_next(request)

        from app import auth
        # Setup-Pflicht wenn noch keine Credentials gesetzt
        if not auth.is_setup_done():
            return RedirectResponse("/setup", status_code=302)

        # Session prüfen
        token = request.cookies.get("session")
        if not auth.validate_session(token):
            return RedirectResponse(f"/login?next={path}", status_code=302)

        return await call_next(request)


# ── Security Headers Middleware ───────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Synology Dashboard", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(auth_router.router)
app.include_router(services_router.router)
app.include_router(pages.router)
app.include_router(api.router)
