"""Package-owned production UI routes for the local TraceMotive server."""

from __future__ import annotations

try:
    from importlib.resources.abc import Traversable
except ModuleNotFoundError:  # Python 3.10
    from importlib.abc import Traversable
from typing import Any

from starlette.responses import Response

from . import get_ui_root


class PackagedUIError(RuntimeError):
    """Raised when the installed production UI cannot be served safely."""


_MEDIA_TYPES = {
    ".css": "text/css",
    ".html": "text/html",
    ".js": "text/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def validate_ui_root(ui_root: Traversable) -> None:
    """Validate the minimum package-data contract before binding a server."""

    try:
        index = ui_root.joinpath("index.html")
        assets = ui_root.joinpath("assets")
        if not index.is_file():
            raise PackagedUIError("packaged production UI index.html is missing")
        with index.open("rb") as stream:
            if not stream.read(1):
                raise PackagedUIError("packaged production UI index.html is empty")
        asset_files = [item for item in assets.iterdir() if item.is_file()]
    except PackagedUIError:
        raise
    except (OSError, ValueError) as exc:
        raise PackagedUIError("packaged production UI is unavailable") from exc

    if not asset_files:
        raise PackagedUIError("packaged production UI assets are missing")
    if not any(item.name.endswith((".js", ".css")) for item in asset_files):
        raise PackagedUIError("packaged production UI has no JavaScript or CSS assets")


def _safe_asset_parts(asset_path: str) -> tuple[str, ...] | None:
    if not asset_path or asset_path.startswith("/") or "\\" in asset_path:
        return None
    parts = tuple(asset_path.split("/"))
    if any(not part or part in {".", ".."} or "\x00" in part for part in parts):
        return None
    if parts[-1].lower().endswith(".map"):
        return None
    return parts


def _asset_resource(ui_root: Traversable, asset_path: str) -> Traversable | None:
    parts = _safe_asset_parts(asset_path)
    if parts is None:
        return None
    resource = ui_root.joinpath("assets", *parts)
    try:
        return resource if resource.is_file() else None
    except (OSError, ValueError):
        return None


def _resource_response(resource: Traversable) -> Response:
    try:
        with resource.open("rb") as stream:
            content = stream.read()
    except (OSError, ValueError) as exc:
        raise PackagedUIError("packaged production UI resource cannot be read") from exc

    suffix = "." + resource.name.rsplit(".", 1)[-1].lower()
    return Response(
        content=content,
        media_type=_MEDIA_TYPES.get(suffix, "application/octet-stream"),
        headers={"cache-control": "no-cache"},
    )


def add_ui_routes(app: Any, ui_root: Traversable | None = None) -> Any:
    """Register fixed package-resource routes on an existing FastAPI app."""

    root = get_ui_root() if ui_root is None else ui_root
    validate_ui_root(root)

    @app.get("/", include_in_schema=False)
    async def packaged_index() -> Response:
        return _resource_response(root.joinpath("index.html"))

    @app.get("/assets/{asset_path:path}", include_in_schema=False)
    async def packaged_asset(asset_path: str) -> Response:
        resource = _asset_resource(root, asset_path)
        if resource is None:
            return Response(status_code=404)
        return _resource_response(resource)

    return app


__all__ = ["PackagedUIError", "add_ui_routes", "validate_ui_root"]
