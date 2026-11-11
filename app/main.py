import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from .routers import admin, analytics, auth, tickets, problems, notifications

app = FastAPI(title="Модус · Система обратной связи", version="1.0.0")


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


for router in (auth.router, tickets.router, admin.router, analytics.router, problems.router, notifications.router, notifications.live):
    app.include_router(router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
