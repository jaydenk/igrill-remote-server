"""Minimal web dashboard route."""

import pathlib

from aiohttp import web

STATIC_DIR = pathlib.Path(__file__).parent / "static"


def setup_dashboard(app: web.Application) -> None:
    """Register the dashboard index route and static file serving."""
    app.router.add_get("/", dashboard_handler)
    app.router.add_static("/static", STATIC_DIR, name="static")


async def dashboard_handler(request: web.Request) -> web.FileResponse:
    """Serve the single-page dashboard HTML."""
    return web.FileResponse(STATIC_DIR / "index.html")
