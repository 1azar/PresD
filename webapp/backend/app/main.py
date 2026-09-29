from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import router
from .config import settings
from .database import Base, engine
from .errors import APIError, install_handlers


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.allowed_clients  # Validate the access list before accepting traffic.
    settings.data_root.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Presentation Studio API", version="1.0.0", lifespan=lifespan)
install_handlers(app)


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if not origin or origin.rstrip("/") not in settings.origins:
            return JSONResponse(status_code=403, content={"code": "invalid_origin", "message": "Запрос с этого источника запрещён"})
    return await call_next(request)


@app.get("/api/health")
def health():
    return {"status": "ok"}


app.include_router(router)
