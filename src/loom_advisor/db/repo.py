"""Repository: the only module that touches SQL.

Translates between the relational rows and the pydantic contract, so the
engine and API never see SQLAlchemy objects.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..schema import (
    Article,
    Construction,
    LoomRecord,
    MachineType,
    ReviewItemView,
    Settings,
    StatusEvent,
)
from .models import (
    ArticleRow,
    Document,
    Loom,
    LoomAssignment,
    ReviewItem,
    SettingsEventRow,
    StatusEventRow,
    utcnow,
)


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
            efficiency_pct=event.efficiency_pct,
            rpm=event.rpm,
            pile_breaks=event.pile_breaks,
            pile_cmpx=event.pile_cmpx,
            ground_breaks=event.ground_breaks,
            ground_cmpx=event.ground_cmpx,
            weft_breaks=event.weft_breaks,
            weft_cmpx=event.weft_cmpx,
            breaks_per_hour=event.breaks_per_hour,
            total_kilopicks=event.total_kilopicks,
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


def add_document(
    session: Session, kind: str, file_path: str, uploaded_by: str | None = None
) -> int:
    document = Document(kind=kind, file_path=str(file_path), uploaded_by=uploaded_by)
    session.add(document)
    session.commit()
    return document.id


def add_review_item(
    session: Session,
    *,
    kind: str,
    reason: str,
    payload: dict,
    report_date: date | None = None,
    loom_id: str | None = None,
    source_doc_id: int | None = None,
) -> int:
    item = ReviewItem(
        kind=kind,
        reason=reason,
        payload=payload,
        report_date=report_date,
        loom_id=loom_id,
        source_doc_id=source_doc_id,
    )
    session.add(item)
    session.commit()
    return item.id


def list_review_items(session: Session, status: str = "pending") -> list[ReviewItemView]:
    items = session.scalars(
        select(ReviewItem).where(ReviewItem.status == status).order_by(ReviewItem.id)
    ).all()
    views = []
    for item in items:
        doc = session.get(Document, item.source_doc_id) if item.source_doc_id else None
        views.append(
            ReviewItemView(
                id=item.id,
                kind=item.kind,
                report_date=item.report_date,
                loom_id=item.loom_id,
                payload=item.payload,
                reason=item.reason,
                status=item.status,
                source_doc_path=doc.file_path if doc else None,
                uploaded_by=doc.uploaded_by if doc else None,
                submitted_at=item.submitted_at,
            )
        )
    return views


def count_review_items(session: Session, status: str = "pending") -> int:
    return len(
        session.scalars(select(ReviewItem.id).where(ReviewItem.status == status)).all()
    )


def resolve_review_item(
    session: Session,
    item_id: int,
    *,
    approve: bool,
    reviewer: str,
    corrected: dict | None = None,
) -> None:
    """Approve (writing a verified status event) or reject a quarantined row.

    On approval the status event is written FIRST — if it collides with an
    existing row for that loom/day, the item stays pending and the error
    surfaces, so history can never be silently overwritten."""
    item = session.get(ReviewItem, item_id)
    if item is None:
        raise NotFoundError(f"review item {item_id} not found")
    if item.status != "pending":
        raise ValueError(f"review item {item_id} is already {item.status}")
    if approve:
        payload = corrected if corrected is not None else item.payload
        loom_id = (item.loom_id or str(payload.get("loom_no", ""))).strip()
        if not loom_id or item.report_date is None:
            raise ValueError("cannot approve without a loom number and report date")
        create_loom(session, loom_id)
        event = StatusEvent(
            report_date=item.report_date,
            efficiency_pct=payload.get("efficiency_pct"),
            rpm=payload.get("rpm"),
            pile_breaks=payload.get("pile_breaks"),
            pile_cmpx=payload.get("pile_cmpx"),
            ground_breaks=payload.get("ground_breaks"),
            ground_cmpx=payload.get("ground_cmpx"),
            weft_breaks=payload.get("weft_breaks"),
            weft_cmpx=payload.get("weft_cmpx"),
            breaks_per_hour=payload.get("breaks_per_hour"),
            total_kilopicks=payload.get("total_kilopicks"),
        )
        add_status(session, loom_id, event, source_doc_id=item.source_doc_id)
        item.payload = payload
    item.status = "approved" if approve else "rejected"
    item.reviewed_by = reviewer
    item.reviewed_at = utcnow()
    session.commit()


def latest_status_date(session: Session) -> date | None:
    return session.scalar(select(func.max(StatusEventRow.report_date)))


def list_looms(session: Session) -> list[tuple[str, str | None]]:
    """All looms as (loom_id, machine_type), for shed-wide views."""
    looms = session.scalars(select(Loom)).all()
    return [(loom.loom_id, loom.machine_type) for loom in looms]


def list_shed_status(session: Session) -> list[tuple[str, StatusEvent]]:
    """Every status event in the shed as (loom_id, event), date-ordered."""
    rows = session.scalars(
        select(StatusEventRow).order_by(StatusEventRow.report_date, StatusEventRow.loom_id)
    ).all()
    return [(row.loom_id, _status_from_row(row)) for row in rows]


def _require_loom(session: Session, loom_id: str) -> Loom:
    loom = session.get(Loom, loom_id)
    if loom is None:
        raise NotFoundError(f"loom {loom_id} not found")
    return loom


def _status_from_row(row: StatusEventRow) -> StatusEvent:
    return StatusEvent(
        report_date=row.report_date,
        efficiency_pct=row.efficiency_pct,
        rpm=row.rpm,
        pile_breaks=row.pile_breaks,
        pile_cmpx=row.pile_cmpx,
        ground_breaks=row.ground_breaks,
        ground_cmpx=row.ground_cmpx,
        weft_breaks=row.weft_breaks,
        weft_cmpx=row.weft_cmpx,
        breaks_per_hour=row.breaks_per_hour,
        total_kilopicks=row.total_kilopicks,
        break_type=row.break_type,
    )
