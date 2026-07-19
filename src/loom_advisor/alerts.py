"""Deadline checking: is the data contract being honoured?

expectations.yaml declares which documents are due and when; this module
compares the contract against what the database has actually received.
Consumed by the in-app banner and by scripts/check_expectations.py (cron).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from .db import repo

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent / "config"


@dataclass(frozen=True)
class Overdue:
    name: str
    description: str
    expected_date: date
    last_date: date | None
    days_overdue: int
    owners: list[dict] = field(default_factory=list)

    def headline(self) -> str:
        last = self.last_date.isoformat() if self.last_date else "never"
        return (
            f"{self.description} is OVERDUE — expected through {self.expected_date.isoformat()}, "
            f"last received: {last} ({self.days_overdue} day(s) missing)"
        )


def load_expectations(config_dir: Path | None = None) -> dict[str, Any]:
    cdir = config_dir or DEFAULT_CONFIG_DIR
    return yaml.safe_load((cdir / "expectations.yaml").read_bytes())


def overdue_expectations(
    session: Session,
    now: datetime | None = None,
    cfg: dict[str, Any] | None = None,
) -> list[Overdue]:
    """Daily documents: yesterday's report must be in by `due_by` today."""
    cfg = cfg or load_expectations()
    now = now or datetime.now()
    overdue: list[Overdue] = []
    for name, spec in cfg.get("expected_documents", {}).items():
        if spec.get("cadence") != "daily":
            continue
        due_by = time.fromisoformat(spec.get("due_by", "10:00"))
        lag = 1 if now.time() >= due_by else 2
        expected = now.date() - timedelta(days=lag)
        last = repo.latest_status_date(session)
        if last is None or last < expected:
            days = (expected - last).days if last else lag
            overdue.append(
                Overdue(
                    name=name,
                    description=spec.get("description", name),
                    expected_date=expected,
                    last_date=last,
                    days_overdue=days,
                    owners=spec.get("owners", []),
                )
            )
    return overdue
