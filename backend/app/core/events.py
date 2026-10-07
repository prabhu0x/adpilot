"""In-process pub/sub used to drive the live SSE channel and job status UI.

The bus is deliberately dependency-free (no Redis required) which keeps the
sandbox deployment to a single process, while remaining a drop-in place to
swap in Redis pub/sub for multi-worker deployments.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import json
from collections import deque
from typing import Any

_MAX_HISTORY = 400
_seq = itertools.count(1)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=_MAX_HISTORY)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # -- producer ---------------------------------------------------------
    def publish(self, channel: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "id": next(_seq),
            "channel": channel,
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "payload": payload or {},
        }
        self._history.append(event)
        for queue in list(self._subscribers):
            try:
                if self._loop and self._loop.is_running():
                    self._loop.call_soon_threadsafe(queue.put_nowait, event)
                else:  # pragma: no cover - synchronous fallback
                    queue.put_nowait(event)
            except Exception:  # pragma: no cover - slow/broken consumer
                self._subscribers.discard(queue)
        return event

    # -- consumer ---------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def history(self, limit: int = 60) -> list[dict[str, Any]]:
        return list(self._history)[-limit:]

    def format_sse(self, event: dict[str, Any]) -> str:
        return f"id: {event['id']}\nevent: {event['channel']}\ndata: {json.dumps(event)}\n\n"


bus = EventBus()
_ = bus.publish  # re-export convenience


# --------------------------------------------------------------------------- #
# Background job registry (surfaced in the UI job status widget)
# --------------------------------------------------------------------------- #
class JobRegistry:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}

    def start(self, name: str, kind: str = "job", total: float = 100.0) -> str:
        job_id = f"{name}-{dt.datetime.now(dt.timezone.utc).strftime('%H%M%S')}-{next(_seq)}"
        self.jobs[job_id] = {
            "id": job_id,
            "name": name,
            "kind": kind,
            "status": "running",
            "progress": 0.0,
            "total": total,
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "finished_at": None,
            "message": "started",
            "result": None,
        }
        bus.publish("job", {"job_id": job_id, "name": name, "status": "running", "progress": 0.0})
        return job_id

    def update(self, job_id: str, progress: float, message: str = "") -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        job["progress"] = round(progress, 1)
        if message:
            job["message"] = message
        bus.publish(
            "job",
            {"job_id": job_id, "name": job["name"], "status": "running", "progress": job["progress"], "message": message},
        )

    def finish(self, job_id: str, result: Any = None, status: str = "completed", message: str = "") -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        job["status"] = status
        job["progress"] = 100.0 if status == "completed" else job["progress"]
        job["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        job["result"] = result
        if message:
            job["message"] = message
        bus.publish(
            "job",
            {"job_id": job_id, "name": job["name"], "status": status, "progress": job["progress"], "message": message},
        )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        items = sorted(self.jobs.values(), key=lambda j: j["started_at"], reverse=True)
        return items[:limit]


jobs = JobRegistry()