"""Живые события через WebSocket.

Синхронные обработчики FastAPI работают в пуле потоков, поэтому события копятся
в сессии (db.info["outbox"]) и отправляются только после успешного commit —
клиент не увидит изменение, которого нет в базе.
"""
import asyncio
from collections import defaultdict
from fastapi import WebSocket
from sqlalchemy import event
from sqlalchemy.orm import Session


class Hub:
    def __init__(self):
        self.connections: dict[int, set[WebSocket]] = defaultdict(set)
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self, user_id: int, ws: WebSocket):
        self.loop = asyncio.get_running_loop()
        self.connections[user_id].add(ws)

    def disconnect(self, user_id: int, ws: WebSocket):
        self.connections[user_id].discard(ws)
        if not self.connections[user_id]:
            self.connections.pop(user_id, None)

    async def _send(self, user_ids: set[int], message: dict):
        for uid in user_ids:
            for ws in list(self.connections.get(uid, ())):
                try:
                    await ws.send_json(message)
                except Exception:
                    self.disconnect(uid, ws)

    def publish(self, user_ids: set[int], message: dict):
        if not self.loop or not user_ids:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.loop.create_task(self._send(set(user_ids), message))
        else:
            asyncio.run_coroutine_threadsafe(self._send(set(user_ids), message), self.loop)


hub = Hub()


def queue(db, user_ids, message: dict):
    db.info.setdefault("outbox", []).append((set(user_ids), message))


@event.listens_for(Session, "after_commit")
def _flush(session):
    for user_ids, message in session.info.pop("outbox", []):
        hub.publish(user_ids, message)


@event.listens_for(Session, "after_rollback")
def _drop(session):
    session.info.pop("outbox", None)
