"""Relational schema.

Event-sourced where it matters: daily status and settings changes are
append-only rows, never overwrites — cause lives in what changed and what
happened next. Invariants the database can express live in the database
(one status row per loom per day), not in application code.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Loom(Base):
    __tablename__ = "looms"

    loom_id: Mapped[str] = mapped_column(String, primary_key=True)
    machine_type: Mapped[str | None] = mapped_column(String, nullable=True)


class Document(Base):
    """Raw uploaded files (design sheets, CMPX reports). Every extracted
    number can point back to the document it came from — and to the
    person who submitted it."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String)  # 'design_sheet' | 'cmpx_report'
    file_path: Mapped[str] = mapped_column(String)
    uploaded_by: Mapped[str | None] = mapped_column(String, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReviewItem(Base):
    """Quarantine for extracted rows that failed machine verification.

    Nothing here is ever displayed as truth. A named reviewer corrects and
    approves (writing a status event) or rejects. Uploaders submit
    documents, the machine verifies, reviewers resolve — segregation of
    duties, so data entry is never compromised."""

    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String)  # 'cmpx_row'
    report_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    loom_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(String)
    source_doc_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="pending")
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ArticleRow(Base):
    """One order spec from the design studio. Construction belongs to the
    article, not the loom — looms change articles as orders change."""

    __tablename__ = "articles"

    article_id: Mapped[str] = mapped_column(String, primary_key=True)
    construction: Mapped[dict] = mapped_column(JSON, default=dict)
    source_doc_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id"), nullable=True
    )


class LoomAssignment(Base):
    """Which loom is weaving which article, and when. end_date NULL means
    currently running. CMPX comparisons only make sense within one
    assignment — different articles have different natural break rates."""

    __tablename__ = "loom_assignments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    loom_id: Mapped[str] = mapped_column(ForeignKey("looms.loom_id"))
    article_id: Mapped[str] = mapped_column(ForeignKey("articles.article_id"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class StatusEventRow(Base):
    """One row of the daily CMPX/breakage report for one loom.
    Append-only; the unique constraint makes duplicate uploads impossible
    at the database level."""

    __tablename__ = "status_events"
    __table_args__ = (UniqueConstraint("loom_id", "report_date"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    loom_id: Mapped[str] = mapped_column(ForeignKey("looms.loom_id"))
    report_date: Mapped[date] = mapped_column(Date)
    efficiency_pct: Mapped[float | None] = mapped_column(nullable=True)
    rpm: Mapped[float | None] = mapped_column(nullable=True)
    pile_breaks: Mapped[int | None] = mapped_column(nullable=True)
    pile_cmpx: Mapped[float | None] = mapped_column(nullable=True)
    ground_breaks: Mapped[int | None] = mapped_column(nullable=True)
    ground_cmpx: Mapped[float | None] = mapped_column(nullable=True)
    weft_breaks: Mapped[int | None] = mapped_column(nullable=True)
    weft_cmpx: Mapped[float | None] = mapped_column(nullable=True)
    breaks_per_hour: Mapped[float | None] = mapped_column(nullable=True)
    total_kilopicks: Mapped[float | None] = mapped_column(nullable=True)
    break_type: Mapped[str | None] = mapped_column(String, nullable=True)
    source_doc_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id"), nullable=True
    )


class SettingsEventRow(Base):
    """Full settings snapshot at each change, append-only — so any day's
    CMPX can be attributed to the knobs in force that day."""

    __tablename__ = "settings_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    loom_id: Mapped[str] = mapped_column(ForeignKey("looms.loom_id"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
