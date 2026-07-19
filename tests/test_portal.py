"""Portal tests: upload -> verify gate -> review queue -> named approval,
plus the deadline checker against the data contract."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from loom_advisor.alerts import overdue_expectations
from loom_advisor.db import repo
from loom_advisor.db.models import Base
from loom_advisor.ingestion.service import ingest_report_upload
from loom_advisor.ingestion.vlm_reader import CmpxReportExtraction, CmpxRow
from loom_advisor.schema import StatusEvent

EXPECTATIONS = {
    "expected_documents": {
        "break_cmpx_report": {
            "description": "Daily Break CMPX report",
            "cadence": "daily",
            "due_by": "10:00",
            "owners": [{"name": "Weaving office", "email": ""}],
        }
    }
}


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/portal.db")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


def good_row(loom: str, breaks: int = 50, kp: float = 500.0) -> CmpxRow:
    """A row whose CMPX values satisfy the identity exactly."""
    return CmpxRow(
        loom_no=loom,
        loom_type="dobby",
        efficiency_pct=80.0,
        rpm=450.0,
        pile_breaks=5,
        pile_cmpx=5 / kp * 100,
        ground_breaks=10,
        ground_cmpx=10 / kp * 100,
        weft_breaks=breaks,
        weft_cmpx=breaks / kp * 100,
        breaks_per_hour=3.0,
        total_kilopicks=kp,
    )


def bad_row(loom: str) -> CmpxRow:
    """weft_cmpx wildly inconsistent with breaks/KP -> must be quarantined."""
    row = good_row(loom)
    return row.model_copy(update={"weft_cmpx": 99.0})


def stub_reader(rows):
    return lambda path: CmpxReportExtraction(report_date=None, rows=rows)


REPORT_DATE = date(2026, 7, 1)


def test_upload_routes_rows_by_verification(session, tmp_path):
    page = tmp_path / "page1.jpg"
    page.write_bytes(b"fake")
    summary = ingest_report_upload(
        session,
        [page],
        REPORT_DATE,
        uploaded_by="Anusha",
        reader=stub_reader([good_row("7"), good_row("8"), bad_row("9")]),
    )
    assert summary.verified == 2
    assert summary.queued_for_review == 1
    assert summary.duplicates == 0
    # verified rows are in the database, tied to the document
    history = repo.get_history(session, "7")
    assert history[0].weft_cmpx == pytest.approx(10.0)
    # quarantined row is NOT in the database
    with pytest.raises(repo.NotFoundError):
        repo.get_history(session, "9")
    items = repo.list_review_items(session)
    assert len(items) == 1
    assert items[0].loom_id == "9"
    assert items[0].uploaded_by == "Anusha"


def test_duplicate_upload_cannot_double_history(session, tmp_path):
    page = tmp_path / "page1.jpg"
    page.write_bytes(b"fake")
    reader = stub_reader([good_row("7")])
    ingest_report_upload(session, [page], REPORT_DATE, "A", reader=reader)
    summary = ingest_report_upload(session, [page], REPORT_DATE, "B", reader=reader)
    assert summary.verified == 0
    assert summary.duplicates == 1
    assert len(repo.get_history(session, "7")) == 1


def test_review_approval_writes_verified_event(session, tmp_path):
    page = tmp_path / "page1.jpg"
    page.write_bytes(b"fake")
    ingest_report_upload(
        session, [page], REPORT_DATE, "A", reader=stub_reader([bad_row("9")])
    )
    item = repo.list_review_items(session)[0]
    corrected = dict(item.payload, weft_cmpx=10.0)
    repo.resolve_review_item(
        session, item.id, approve=True, reviewer="Supervisor", corrected=corrected
    )
    history = repo.get_history(session, "9")
    assert history[0].weft_cmpx == pytest.approx(10.0)
    assert repo.list_review_items(session) == []  # no longer pending
    assert repo.count_review_items(session, status="approved") == 1


def test_review_reject_keeps_data_out(session, tmp_path):
    page = tmp_path / "page1.jpg"
    page.write_bytes(b"fake")
    ingest_report_upload(
        session, [page], REPORT_DATE, "A", reader=stub_reader([bad_row("9")])
    )
    item = repo.list_review_items(session)[0]
    repo.resolve_review_item(session, item.id, approve=False, reviewer="Supervisor")
    with pytest.raises(repo.NotFoundError):
        repo.get_history(session, "9")
    assert repo.count_review_items(session, status="rejected") == 1


def test_resolved_items_cannot_be_resolved_twice(session, tmp_path):
    page = tmp_path / "page1.jpg"
    page.write_bytes(b"fake")
    ingest_report_upload(
        session, [page], REPORT_DATE, "A", reader=stub_reader([bad_row("9")])
    )
    item = repo.list_review_items(session)[0]
    repo.resolve_review_item(session, item.id, approve=False, reviewer="S")
    with pytest.raises(ValueError, match="already"):
        repo.resolve_review_item(session, item.id, approve=True, reviewer="S")


def test_deadline_checker(session):
    repo.create_loom(session, "7")
    repo.add_status(session, "7", StatusEvent(report_date=date(2026, 7, 1), weft_cmpx=10))

    # 09:00 on July 2: yesterday's report not yet due -> nothing overdue
    early = overdue_expectations(
        session, now=datetime(2026, 7, 2, 9, 0), cfg=EXPECTATIONS
    )
    assert early == []

    # 10:30 on July 2: July 1 is on record -> honoured
    on_time = overdue_expectations(
        session, now=datetime(2026, 7, 2, 10, 30), cfg=EXPECTATIONS
    )
    assert on_time == []

    # 10:30 on July 3: July 2 is missing -> overdue, owners attached
    late = overdue_expectations(
        session, now=datetime(2026, 7, 3, 10, 30), cfg=EXPECTATIONS
    )
    assert len(late) == 1
    assert late[0].expected_date == date(2026, 7, 2)
    assert late[0].days_overdue == 1
    assert late[0].owners[0]["name"] == "Weaving office"
