"""Documentation routes.

The dashboard's Documentation page reads the docs that ship with the
checkout, through these three routes, instead of framing a website. The
reading and rendering live in ``janus_cli/dashboard_docs.py``; this file
only maps them onto HTTP.

Like every ``/api/`` route they need the session. Images therefore cannot
be plain ``<img src>`` URLs: the page fetches each one from
``/api/docs/asset`` with the session attached and shows the bytes.
"""

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from janus_cli import dashboard_docs

router = APIRouter()

# An SVG is a document. Opened on its own (not through <img>) it could run
# script with the dashboard's session, so every asset is sent sandboxed and
# with no permission to load or run anything.
_ASSET_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "Cache-Control": "private, max-age=3600",
}


def _site() -> Optional[Path]:
    """The ``website`` directory of this install, or ``None`` (a wheel)."""
    # Imported here, not at the top: the web server includes this router
    # while it is still loading, so a top-level import would be circular
    # for anything that imports this module first.
    from janus_cli import web_server

    return dashboard_docs.find_site(web_server.PROJECT_ROOT)


@router.get("/api/docs/tree")
def get_docs_tree() -> Dict[str, Any]:
    """The navigation, and the page to open first. ``available`` is false
    when this install has no docs, so the page can say so."""
    site = _site()
    tree = dashboard_docs.build_tree(site) if site is not None else []
    first = next(dashboard_docs.iter_docs(tree), None)
    return {"available": first is not None, "home": first["id"] if first else None, "tree": tree}


@router.get("/api/docs/page")
def get_docs_page(id: str) -> Dict[str, Any]:
    """One page, rendered: ``{id, title, description, html, toc}``."""
    site = _site()
    page = dashboard_docs.render_page(site, id) if site is not None else None
    if page is None:
        raise HTTPException(status_code=404, detail="No such documentation page")
    return page


@router.get("/api/docs/asset")
def get_docs_asset(path: str) -> FileResponse:
    """An image a page shows, from ``website/static``."""
    site = _site()
    found = dashboard_docs.resolve_asset(site, path) if site is not None else None
    if found is None:
        raise HTTPException(status_code=404, detail="No such documentation image")
    return FileResponse(
        found,
        media_type=dashboard_docs.IMAGE_TYPES[found.suffix.lower()],
        headers=_ASSET_HEADERS,
    )
