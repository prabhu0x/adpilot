"""Structured JSON logging plus lightweight in-process metrics."""
from __future__ import annotations

import json
import logging
import sys
import time
from collections import defaultdict

from .config import settings

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "method", "path", "status", "duration_ms", "actor", "extra"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG if settings.environment == "development" else logging.INFO)
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


class Metrics:
    """Tiny Prometheus-friendly registry (counters + gauges + latency histograms)."""

    def __init__(self) -> None:
        self.counters: dict[str, float] = defaultdict(float)
        self.gauges: dict[str, float] = {}
        self.latency_sum: dict[str, float] = defaultdict(float)
        self.latency_count: dict[str, int] = defaultdict(int)
        self.labels: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    def inc(self, name: str, value: float = 1.0) -> None:
        self.counters[name] += value

    def observe(self, name: str, duration_ms: float) -> None:
        self.latency_sum[name] += duration_ms
        self.latency_count[name] += 1

    def gauge(self, name: str, value: float) -> None:
        self.gauges[name] = value

    def label_inc(self, name: str, label: str) -> None:
        self.labels[name][label] += 1

    def render(self) -> str:
        lines: list[str] = []
        for name, value in sorted(self.counters.items()):
            lines.append(f"# TYPE adpilot_{name} counter")
            lines.append(f"adpilot_{name} {value}")
        for name, value in sorted(self.gauges.items()):
            lines.append(f"# TYPE adpilot_{name} gauge")
            lines.append(f"adpilot_{name} {value}")
        for name, total in sorted(self.latency_sum.items()):
            count = self.latency_count[name] or 1
            lines.append(f"# TYPE adpilot_{name}_ms summary")
            lines.append(f'adpilot_{name}_ms_sum {total}')
            lines.append(f'adpilot_{name}_ms_count {self.latency_count[name]}')
            lines.append(f'adpilot_{name}_ms_avg {total / count:.2f}')
        for name, buckets in sorted(self.labels.items()):
            for label, value in sorted(buckets.items()):
                lines.append(f'adpilot_{name}{{label="{label}"}} {value}')
        return "\n".join(lines) + "\n"


metrics = Metrics()