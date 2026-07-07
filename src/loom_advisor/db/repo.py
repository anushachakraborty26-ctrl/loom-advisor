"""Repository: the only module that touches SQL.

Translates between the relational rows and the pydantic contract, so the
engine and API never see SQLAlchemy objects.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..schema import (
    Article,
    Construction,
    LoomRecord,
    MachineType,
    Settings,
    StatusEvent,
)
from .models import ArticleRow, Loom, LoomAssignment, SettingsEventRow, StatusEventRow


class NotFoundError(Exception):
    pass


class DuplicateStatusError(Exception):
    pass


def create_loom(session: Session, loom_id: str, machine_type: str | None = None) -> None:
    if session.get(Loom, loom_id) is not None:
        return
    session.add(Loom(loom_id=loom_id, machine_type=machine_type))
    session.commit()


def upsert_article(session: Session, article: Article, source_doc_id: int | None = None) -> None:
    row = session.get(ArticleRow, article.article_id)
    construction = article.construction.model_dump(mode="json", exclude_none=True)
    if row is None:
        session.add(
            ArticleRow(
                article_id=article.article_id,
                construction=construction,
                source_doc_id=source_doc_id,
            )
        )
    else:
        row.construction = construction
        if source_doc_id is not None:
            row.source_doc_id = source_doc_id
    session.commit()


def assign_article(session: Session, loom_id: str, article_id: str, start_date: date) -> None:
    _require_loom(session, loom_id)
    if session.get(ArticleRow, article_id) is None:
        raise NotFoundError(f"article {article_id} not found")
    open_assignment = session.scalar(
        select(LoomAssignment).where(
            LoomAssignment.loom_id == loom_id, LoomAssignment.end_date.is_(None)
        )
    )
    if open_assignment is not None:
        open_assignment.end_date = start_date
    session.add(
        LoomAssignment(loom_id=loom_id, article_id=article_id, start_date=start_date)
    )
    session.commit()


def set_settings(session: Session, loom_id: str, settings: Settings) -> None:
    _require_loom(session, loom_id)
    session.add(
        SettingsEventRow(
            loom_id=loom_id,
            snapshot=settings.model_dump(mode="json", exclude_none=True),
        )
    )
    session.commit()


def add_status(
    session: Session, loom_id: str, event: StatusEvent, source_doc_id: int | None = None
) -> None:
    _require_loom(session, loom_id)
    session.add(
        StatusEventRow(
            loom_id=loom_id,
            report_date=event.report_date,
            filling_cmpx=event.filling_cmpx,
            breakages_per_day=event.breakages_per_day,
            efficiency_pct=event.efficiency_pct,
            break_type=event.break_type.value if event.break_type else None,
            source_doc_id=source_doc_id,
        )
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise DuplicateStatusError(
            f"status for loom {loom_id} on {event.report_date} already recorded"
        ) from exc


def get_history(session: Session, loom_id: str) -> list[StatusEvent]:
    _require_loom(session, loom_id)
    rows = session.scalars(
        select(StatusEventRow)
        .where(StatusEventRow.loom_id == loom_id)
        .order_by(StatusEventRow.report_date)
    ).all()
    return [_status_from_row(r) for r in rows]


def get_loom_record(session: Session, loom_id: str) -> LoomRecord:
    """Assemble the engine's input: identity + active article's construction
    + latest settings snapshot + status history for the active assignment."""
    loom = _require_loom(session, loom_id)

    assignment = session.scalar(
        select(LoomAssignment).where(
            LoomAssignment.loom_id == loom_id, LoomAssignment.end_date.is_(None)
        )
    )
    construction = Construction()
    article_id = None
    status_since: date | None = None
    if assignment is not None:
        article_id = assignment.article_id
        status_since = assignment.start_date
        article_row = session.get(ArticleRow, assignment.article_id)
        if article_row is not None:
            construction = Construction.model_validate(article_row.construction)

    latest_settings = session.scalar(
        select(SettingsEventRow)
        .where(SettingsEventRow.loom_id == loom_id)
        .order_by(SettingsEventRow.recorded_at.desc(), SettingsEventRow.id.desc())
        .limit(1)
    )
    settings = (
        Settings.model_validate(latest_settings.snapshot) if latest_settings else Settings()
    )

    status_query = select(StatusEventRow).where(StatusEventRow.loom_id == loom_id)
    if status_since is not None:
        # Only the current article's history: CMPX across different articles
        # is apples-to-oranges.
        status_query = status_query.where(StatusEventRow.report_date >= status_since)
    rows = session.scalars(status_query.order_by(StatusEventRow.report_date)).all()

    return LoomRecord(
        loom_id=loom_id,
        machine_type=MachineType(loom.machine_type) if loom.machine_type else None,
        article_id=article_id,
        construction=construction,
        settings=settings,
        status_log=[_status_from_row(r) for r in rows],
    )


def _require_loom(session: Session, loom_id: str) -> Loom:
    loom = session.get(Loom, loom_id)
    if loom is None:
        raise NotFoundError(f"loom {loom_id} not found")
    return loom


def _status_from_row(row: StatusEventRow) -> StatusEvent:
    return StatusEvent(
        report_date=row.report_date,
        filling_cmpx=row.filling_cmpx,
        breakages_per_day=row.breakages_per_day,
        efficiency_pct=row.efficiency_pct,
        break_type=row.break_type,
    )
