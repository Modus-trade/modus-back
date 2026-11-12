"""Клиент к ИИ-модели (отдельный сервис, адрес в AI_URL).

ИИ необязателен: без AI_URL или при любой ошибке функции возвращают None,
и вызывающий код переходит на правила (services/hints.py).
"""
import logging
import os
import time

import httpx

log = logging.getLogger("modus.ai")

# После сбоя не дёргаем ИИ какое-то время, чтобы форма не ждала таймаут на каждый запрос.
_down_until = 0.0
COOLDOWN = 30


def enabled() -> bool:
    return bool(os.getenv("AI_URL"))


def _call(path: str, payload: dict, timeout: float) -> dict | None:
    global _down_until
    url = os.getenv("AI_URL", "").rstrip("/")
    if not url or time.monotonic() < _down_until:
        return None
    try:
        r = httpx.post(
            f"{url}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {os.getenv('AI_KEY', '')}"},
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()
    except Exception as err:  # сеть, таймаут, 5xx, кривой JSON — для пользователя одинаково
        _down_until = time.monotonic() + COOLDOWN
        log.warning("ИИ недоступен (%s): %s", path, type(err).__name__)
        return None


def classify(title: str, description: str, image_base64: str | None = None) -> dict | None:
    data = _call(
        "/classify",
        {"title": title, "description": description, "image_base64": image_base64},
        float(os.getenv("AI_TIMEOUT", "4")),
    )
    if not data:
        return None
    try:
        category_id = int(data["category_id"])
        kind = str(data["type"])
        confidence = float(data.get("confidence", 0))
    except (KeyError, TypeError, ValueError):
        return None
    if kind not in ("PROBLEM", "PROPOSAL", "PRAISE") or not 1 <= category_id <= 10_000:
        return None
    return {
        "type": kind,
        "category_id": category_id,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(data.get("reason") or "")[:200],
    }


def solve(ticket: dict, similar: list[dict]) -> dict | None:
    data = _call("/solve", {"ticket": ticket, "similar": similar}, float(os.getenv("AI_SOLVE_TIMEOUT", "25")))
    if not data or not isinstance(data.get("steps"), list):
        return None
    return {
        "summary": str(data.get("summary") or "")[:600],
        "steps": [str(s)[:400] for s in data["steps"][:6]],
        "based_on": [str(n) for n in (data.get("based_on") or [])][:5],
        "reply_draft": str(data.get("reply_draft") or "")[:1500],
    }
