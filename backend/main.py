from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.registry import api_router
from api.v1.system import HealthResponse, health
from core.paths import resolve_app_paths
from core.settings import Settings, get_settings
from db.sqlite import initialize_database


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    paths = resolve_app_paths(resolved_settings.data_dir)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        paths.ensure()
        initialize_database(paths.database)
        yield

    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.version,
        debug=resolved_settings.debug,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.paths = paths
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
    application.add_api_route("/healthz", health, response_model=HealthResponse, tags=["system"])
    application.include_router(api_router)
    return application


app = create_app()
