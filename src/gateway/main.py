from fastapi import FastAPI

from gateway.api.routes import router as api_router
from gateway.config import settings

app = FastAPI(title=settings.app_name, version=settings.app_version)

app.include_router(api_router)
