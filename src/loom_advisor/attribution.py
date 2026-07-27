"""Automatic before/after attribution.

Scans each loom's settings history for changes, finds the nearest verified
status report before and after the change, and records the pair as an
observed intervention — the mechanism that makes predictions tighten on
their own as the plant runs. PROJECT 1's manual before/after study,
continuous and unattended.
"""

from __future__ import annotations

from itertools import pairwise

from sqlalchemy.orm import Session

from .db import repo


def detect_interventions(
    session: Session, gap_days: int = 1, window_days: int = 14
) -> int:
    """Attribute settings changes to outcomes; returns how many new
    interventions were recorded."""
    added = 0
    for loom_id, _machine in repo.list_looms(session):
        settings_events = repo.list_settings_events(session, loom_id)
        if len(settings_events) < 2:
            continue
        history = repo.get_history(session, loom_id)
        measured = [
            event
            for event in history
            if event.weft_cmpx is not None and event.efficiency_pct is not None
        ]
        if not measured:
            continue
        for (_, previous), (changed_at, current) in pairwise(settings_events):
            if previous.model_dump(exclude_none=True) == current.model_dump(
                exclude_none=True
            ):
                continue
            change_date = changed_at.date()
            before = next(
                (
                    event
                    for event in reversed(measured)
                    if event.report_date <= change_date
                    and (change_date - event.report_date).days <= window_days
                ),
                None,
            )
            after = next(
                (
                    event
                    for event in measured
                    if (event.report_date - change_date).days >= gap_days
                    and (event.report_date - change_date).days <= window_days
                ),
                None,
            )
            if before is None or after is None:
                continue
            recorded = repo.add_intervention(
                session,
                loom_id=loom_id,
                change_date=change_date,
                source="observed",
                settings_before=previous.model_dump(exclude_none=True),
                settings_after=current.model_dump(exclude_none=True),
                cmpx_before=before.weft_cmpx,
                cmpx_after=after.weft_cmpx,
                eff_before=before.efficiency_pct,
                eff_after=after.efficiency_pct,
                notes=(
                    f"auto-attributed: status {before.report_date.isoformat()} "
                    f"-> {after.report_date.isoformat()}"
                ),
            )
            added += int(recorded)
    return added
