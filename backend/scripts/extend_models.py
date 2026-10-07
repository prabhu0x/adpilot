"""One-off schema alignment: add the columns the engine layer requires.

The engines were written against a richer contract than the first-pass model
draft, so this script appends the missing columns. It is idempotent and is kept
in the repo as documentation of the change (the Alembic migration generated from
the models is the authoritative artefact).
"""
from __future__ import annotations

import re
import sys

MODELS = "app/db/models.py"

ADDITIONS: dict[str, list[str]] = {
    "Opportunity": [
        'title: Mapped[str] = mapped_column(String(190), default="")',
        'expected_revenue_delta: Mapped[float] = mapped_column(Float, default=0.0)',
        'expected_spend_delta: Mapped[float] = mapped_column(Float, default=0.0)',
        'horizon_days: Mapped[int] = mapped_column(Integer, default=90)',
        'constraints: Mapped[dict] = mapped_column(JSON, default=dict)',
        'evidence: Mapped[dict] = mapped_column(JSON, default=dict)',
    ],
    "Recommendation": [
        'action: Mapped[str] = mapped_column(Text, default="")',
        'expected_spend_delta: Mapped[float] = mapped_column(Float, default=0.0)',
        'urgency: Mapped[float] = mapped_column(Float, default=0.5)',
        'reversible: Mapped[bool] = mapped_column(Boolean, default=True)',
        'evidence: Mapped[dict] = mapped_column(JSON, default=dict)',
        'requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)',
        'autonomy_reason: Mapped[str] = mapped_column(Text, default="")',
        'dedupe_key: Mapped[str] = mapped_column(String(40), default="", index=True)',
        'narrative: Mapped[str] = mapped_column(Text, default="")',
        'execution_id: Mapped[int | None] = mapped_column(Integer, nullable=True)',
        'executed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)',
    ],
    "Execution": [
        'kind: Mapped[str] = mapped_column(String(40), default="")',
        'entity_type: Mapped[str] = mapped_column(String(24), default="")',
        'entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)',
        'entity_name: Mapped[str] = mapped_column(String(190), default="")',
        'actor: Mapped[str] = mapped_column(String(120), default="system")',
        'pre_state: Mapped[dict] = mapped_column(JSON, default=dict)',
        'payload: Mapped[list] = mapped_column(JSON, default=list)',
        'push_response: Mapped[dict] = mapped_column(JSON, default=dict)',
        'rollback_plan: Mapped[str] = mapped_column(Text, default="")',
        'notes: Mapped[str] = mapped_column(Text, default="")',
        'measurement_window_start: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)',
        'measurement_window_end: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)',
    ],
    "Outcome": [
        'window_days: Mapped[int] = mapped_column(Integer, default=14)',
        'ci_low: Mapped[float] = mapped_column(Float, default=0.0)',
        'ci_high: Mapped[float] = mapped_column(Float, default=0.0)',
        'detail: Mapped[dict] = mapped_column(JSON, default=dict)',
    ],
    "ScenarioStatus": [
        'metric: Mapped[str] = mapped_column(String(60), default="")',
        'entity: Mapped[str] = mapped_column(String(190), default="")',
        'platform: Mapped[str | None] = mapped_column(String(40), nullable=True)',
        'detection: Mapped[str] = mapped_column(Text, default="")',
    ],
}

# non-null unique column needs a default so engines may omit it
DEFAULTS = [
    ('idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)',
     'idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, default=lambda: "")'),
]


def main() -> int:
    source = open(MODELS).read()
    for cls, lines in ADDITIONS.items():
        match = re.search(rf"class {cls}\(.*?\n\n\n", source, re.S)
        if not match:
            print(f"class {cls} not found", file=sys.stderr)
            return 1
        body = match.group(0)
        missing = [line for line in lines if line.split(":")[0].strip() not in body]
        if not missing:
            continue
        block = "".join(f"    {line}\n" for line in missing)
        if "__table_args__" in body:
            anchor = f"class {cls}(Base, SerialMixin, TimestampMixin):" if "TimestampMixin" in body.split("\n")[0] else f"class {cls}(Base, SerialMixin):"
            new_body = body.replace("    __table_args__", block + "    __table_args__", 1)
        else:
            new_body = body.rstrip("\n") + "\n" + block + "\n\n"
        source = source.replace(body, new_body, 1)
        print(f"{cls}: added {[line.split(':')[0] for line in missing]}")
    for old, new in DEFAULTS:
        if old in source:
            source = source.replace(old, new, 1)
            print("Execution.idempotency_key: default added")
    open(MODELS, "w").write(source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())