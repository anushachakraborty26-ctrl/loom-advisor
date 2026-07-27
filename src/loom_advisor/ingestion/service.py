"""Upload ingestion service: what happens after a user submits a document.

Every upload follows the same integrity walls:
1. the original file is recorded as a document with the uploader's name,
2. the machine verifies (identity check for report rows),
3. anything unverified is quarantined for a named reviewer — it never
   silently enters the database.

The `reader` parameters exist so tests can inject stub extractors; in
production they default to the VLM readers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import repo
from ..schema import StatusEvent
from .excel_adapter import parse_design_sheet
from .vlm_reader import (
    CmpxRow,
    check_row_consistency,
    design_sheet_to_article,
    read_cmpx_report,
    read_design_sheet,
)

MACHINE_TYPES = {"jacquard", "dobby"}


class IngestSummary(BaseModel):
    verified: int = 0
    queued_for_review: int = 0
    duplicates: int = 0
    notes: list[str] = []


def _event_from_row(report_date: date, row: CmpxRow) -> StatusEvent:
    return StatusEvent(
        report_date=report_date,
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
    )


def process_rows(
    session: Session,
    rows: list[CmpxRow],
    report_date: date,
    doc_id: int,
    summary: IngestSummary,
) -> None:
    """The verify gate, shared by every status source (photos, ERP, review).
    Pass rows are stored with provenance; everything else is quarantined."""
    for row in rows:
        loom_id = row.loom_no.strip()
        verdict = check_row_consistency(row)
        if verdict == "pass" and loom_id.isdigit():
            machine = row.loom_type if row.loom_type in MACHINE_TYPES else None
            repo.create_loom(session, loom_id, machine)
            try:
                repo.add_status(
                    session,
                    loom_id,
                    _event_from_row(report_date, row),
                    source_doc_id=doc_id,
                )
                summary.verified += 1
            except repo.DuplicateStatusError:
                summary.duplicates += 1
        else:
            reason = (
                f"CMPX row {verdict}s the identity check"
                if verdict != "pass"
                else f"unusable loom number: {row.loom_no!r}"
            )
            repo.add_review_item(
                session,
                kind="cmpx_row",
                reason=reason,
                payload=row.model_dump(),
                report_date=report_date,
                loom_id=loom_id if loom_id.isdigit() else None,
                source_doc_id=doc_id,
            )
            summary.queued_for_review += 1


def ingest_report_upload(
    session: Session,
    page_paths: list[Path],
    report_date: date,
    uploaded_by: str,
    reader: Callable | None = None,
) -> IngestSummary:
    """One day's Break CMPX report photos -> verified rows + review queue."""
    reader = reader or read_cmpx_report
    summary = IngestSummary()
    for path in page_paths:
        doc_id = repo.add_document(session, "cmpx_report", str(path), uploaded_by)
        extraction = reader(path)
        process_rows(session, extraction.rows, report_date, doc_id, summary)
    return summary


def ingest_design_sheet_photo(
    session: Session,
    path: Path,
    uploaded_by: str,
    assign_date: date,
    reader: Callable | None = None,
) -> IngestSummary:
    """One design-sheet photo -> article (+ loom assignment when readable)."""
    reader = reader or read_design_sheet
    summary = IngestSummary()
    doc_id = repo.add_document(session, "design_sheet", str(path), uploaded_by)
    extraction = reader(path)
    article = design_sheet_to_article(extraction)
    repo.upsert_article(session, article, source_doc_id=doc_id)
    summary.verified += 1
    loom_id = (extraction.loom_no or "").strip()
    if loom_id.isdigit():
        repo.create_loom(session, loom_id)
        repo.assign_article(session, loom_id, article.article_id, assign_date)
        summary.notes.append(
            f"article {article.article_id} assigned to loom {loom_id} from {assign_date}"
        )
    else:
        summary.notes.append(
            f"article {article.article_id} saved, but no loom number could be read — "
            "assign it manually or re-photograph"
        )
    if extraction.notes:
        summary.notes.append(f"reader flagged: {extraction.notes}")
    return summary


def ingest_design_excel(session: Session, path: Path, uploaded_by: str) -> IngestSummary:
    """A design studio Excel -> article (no loom on Excel sheets)."""
    summary = IngestSummary()
    doc_id = repo.add_document(session, "design_sheet", str(path), uploaded_by)
    article = parse_design_sheet(path)
    repo.upsert_article(session, article, source_doc_id=doc_id)
    summary.verified += 1
    summary.notes.append(
        f"article {article.article_id} saved from Excel — assign to a loom when it goes on"
    )
    return summary
