import httpx


def _get_cfg() -> dict:
    """Liest Konfiguration aus DB; fällt auf config.yaml zurück (Abwärtskompatibilität)."""
    from app.services_db import get_service
    cfg = get_service("paperless")
    if cfg:
        return cfg
    from app.config import config
    return config.get("paperless", {})


def _base() -> str:
    return _get_cfg().get("url", "").rstrip("/")


def _token() -> str:
    return _get_cfg().get("token", "")


def _headers() -> dict:
    return {"Authorization": f"Token {_token()}"}


def is_available() -> bool:
    return bool(_base() and _token())


def upload_document(filename: str, content: bytes, content_type: str) -> dict:
    """Lädt ein Dokument in Paperless-ngx hoch."""
    if not is_available():
        return {"error": "Nicht konfiguriert"}
    try:
        resp = httpx.post(
            f"{_base()}/api/documents/post_document/",
            headers=_headers(),
            files={"document": (filename, content, content_type)},
            timeout=60,
        )
        resp.raise_for_status()
        return {"success": True, "detail": resp.text}
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def get_stats() -> dict:
    """Gesamtanzahl Dokumente + neueste 5 Dokumente."""
    if not is_available():
        return {"error": "Nicht konfiguriert"}

    try:
        resp = httpx.get(
            f"{_base()}/api/documents/",
            headers=_headers(),
            params={"page_size": 5, "ordering": "-created"},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "total": data.get("count", 0),
            "recent": [
                {
                    "id": doc["id"],
                    "title": doc.get("title", "Ohne Titel"),
                    "created": (doc.get("created") or "")[:10],
                    "correspondent": doc.get("correspondent"),
                    "url": f"{_base()}/documents/{doc['id']}/details",
                }
                for doc in data.get("results", [])
            ],
        }
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}"}
    except Exception as e:
        return {"error": str(e)}
