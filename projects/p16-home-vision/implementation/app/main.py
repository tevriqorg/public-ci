from fastapi import FastAPI

from app.api.consumer import router as consumer_router
from app.api.archive_internal import router as archive_internal_router
from app.api.health import router as health_router
from app.api.runtime_status import router as runtime_router
from app.api.normalize_internal import router as normalize_internal_router
from app.api.r2_assets import router as r2_assets_router
from app.web.runtime import router as web_router

app = FastAPI(title="Home Vision", version="0.2.0-poc")
app.include_router(health_router)
app.include_router(runtime_router)
app.include_router(normalize_internal_router)
app.include_router(consumer_router)
app.include_router(archive_internal_router)
app.include_router(r2_assets_router)
app.include_router(web_router)
