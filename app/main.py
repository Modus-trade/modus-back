import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from .routers import admin, analytics, assist, auth, tickets, problems, notifications, knowledge, knowledge_editor
from .services.deadlines import deadline_notification_loop
from .services.integrations import integration_loop
from .routers import integrations, bitrix_bot

@asynccontextmanager
async def lifespan(app):
    stop = asyncio.Event()
    worker = asyncio.create_task(deadline_notification_loop(stop))
    integration_worker = asyncio.create_task(integration_loop(stop))
    try:
        yield
    finally:
        stop.set()
        await worker
        await integration_worker


app = FastAPI(title="Модус · Система обратной связи", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def protect_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    allowed = os.getenv(
        "ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if (
        request.method not in ("GET", "HEAD", "OPTIONS")
        and origin
        and origin not in allowed
    ):
        return JSONResponse(
            {"detail": "Недопустимый источник запроса"}, status_code=403
        )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


for router in (bitrix_bot.router, integrations.router, auth.router, tickets.router, admin.router, analytics.router, problems.router, notifications.router, notifications.live, assist.router, knowledge_editor.router, knowledge.router):
    app.include_router(router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
