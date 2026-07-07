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
    number can point back to the document it came from."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String)  # 'design_sheet' | 'cmpx_report'
    file_path: Mapped[str] = mapped_column(String)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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
    filling_cmpx: Mapped[float | None] = mapped_column(nullable=True)
    breakages_per_day: Mapped[int | None] = mapped_column(nullable=True)
    efficiency_pct: Mapped[float | None] = mapped_column(nullable=True)
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
