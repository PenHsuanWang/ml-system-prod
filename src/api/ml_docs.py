"""Serve the ML API reference with versioned assets from the application image."""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import FileResponse, HTMLResponse

_DIRECTORY = Path(__file__).with_name("docs_assets")
_SWAGGER_JS = "swagger-ui-bundle.js"
_SWAGGER_CSS = "swagger-ui.css"
_REDOC_JS = "redoc.standalone.js"
_ASSETS = {
    _SWAGGER_JS: "text/javascript",
    _SWAGGER_CSS: "text/css",
    _REDOC_JS: "text/javascript",
}


def ml_url_path(request: Request, route: str, **parameters: str) -> str:
    prefix = str(request.scope.get("root_path", "")).rstrip("/")
    return prefix + str(request.app.url_path_for(route, **parameters))


def install_ml_docs(app: FastAPI) -> None:
    @app.get("/docs/assets/{name}", include_in_schema=False)
    def ml_doc_asset(name: str) -> FileResponse:
        media_type = _ASSETS.get(name)
        if media_type is None:
            raise HTTPException(404, "Documentation asset not found")
        return FileResponse(
            _DIRECTORY / name,
            media_type=media_type,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"},
        )

    @app.get("/docs", include_in_schema=False)
    def swagger_ui_html(request: Request) -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=ml_url_path(request, "openapi"),
            title=f"{app.title} - Swagger UI",
            swagger_js_url=ml_url_path(request, "ml_doc_asset", name=_SWAGGER_JS),
            swagger_css_url=ml_url_path(request, "ml_doc_asset", name=_SWAGGER_CSS),
            swagger_favicon_url="data:,",
            oauth2_redirect_url=ml_url_path(request, "swagger_ui_redirect"),
            swagger_ui_parameters={"validatorUrl": None, "persistAuthorization": False},
        )

    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    def swagger_ui_redirect() -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()

    @app.get("/redoc", include_in_schema=False)
    def redoc_html(request: Request) -> HTMLResponse:
        return get_redoc_html(
            openapi_url=ml_url_path(request, "openapi"),
            title=f"{app.title} - ReDoc",
            redoc_js_url=ml_url_path(request, "ml_doc_asset", name=_REDOC_JS),
            redoc_favicon_url="data:,",
            with_google_fonts=False,
        )
