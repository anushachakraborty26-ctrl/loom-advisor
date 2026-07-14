"""Pour the VLM-extracted shed into the database.

Sources (all produced by earlier steps, no API calls here):
- data/incoming/extracted/*.json           design-sheet extractions (one per photo)
- data/incoming/extracted_reports/<date>.json  Break CMPX report days (extract_report.py)

Rules:
- Duplicate sheets for one loom: keep the richest extraction, flag the rest.
- CMPX rows enter the database ONLY if they pass the identity check
  (cmpx ~= breaks / kilopicks * 100 on every category); fail/insufficient
  rows go to data/incoming/flagged_for_review.json for a human.
- The database file is rebuilt from scratch — it is a derived artifact.

    uv run python scripts/ingest_extractions.py
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from loom_advisor.cli import _print_report
from loom_advisor.db import repo
from loom_advisor.db.models import Base
from loom_advisor.engine import advise, load_config
from loom_advisor.ingestion import (
    CmpxRow,
    DesignSheetExtraction,
    check_row_consistency,
    design_sheet_to_article,
)
from loom_advisor.schema import StatusEvent

ROOT = Path(__file__).resolve().parent.parent
INCOMING = ROOT / "data" / "incoming"
DB_PATH = ROOT / "loom_advisor.db"
# Sheets and reports are all from June 2026; assignments open at month start
# so every report day falls inside the article's window.
ASSIGNMENT_START = date(2026, 6, 1)
MACHINE_TYPES = {"jacquard", "dobby"}


def load_sheets() -> list[tuple[str, DesignSheetExtraction]]:
    sheets = []
    for path in sorted((INCOMING / "extracted").glob("*.json")):
        sheets.append((path.name, DesignSheetExtraction.model_validate_json(path.read_text())))
    return sheets


def dedupe_sheets(
    sheets: list[tuple[str, DesignSheetExtraction]],
) -> tuple[dict[str, DesignSheetExtraction], list[dict]]:
    """One extraction per loom: keep the read with the most populated fields."""
    best: dict[str, tuple[int, DesignSheetExtraction, str]] = {}
    flagged: list[dict] = []
    for name, extraction in sheets:
        loom = (extraction.loom_no or "").strip()
        if not loom.isdigit():
            flagged.append({"file": name, "reason": f"unusable loom_no: {extraction.loom_no!r}"})
            continue
        richness = sum(1 for v in extraction.model_dump().values() if v not in (None, ""))
        incumbent = best.get(loom)
        duplicate_reason = f"duplicate sheet for loom {loom} (poorer read)"
        if incumbent is None or richness > incumbent[0]:
            if incumbent is not None:
                flagged.append({"file": incumbent[2], "reason": duplicate_reason})
            best[loom] = (richness, extraction, name)
        else:
            flagged.append({"file": name, "reason": duplicate_reason})
    return {loom: extraction for loom, (_, extraction, _) in best.items()}, flagged


def load_report_days() -> list[tuple[date, list[CmpxRow]]]:
    days = []
    for path in sorted((INCOMING / "extracted_reports").glob("*.json")):
        data = json.loads(path.read_text())
        rows = [CmpxRow.model_validate(raw) for raw in data["rows"]]
        days.append((date.fromisoformat(data["report_date"]), rows))
    return days


def main() -> None:
    sheets_by_loom, flagged = dedupe_sheets(load_sheets())
    report_days = load_report_days()

    verified: dict[date, dict[str, CmpxRow]] = {}
    for report_date, rows in report_days:
        verified[report_date] = {}
        for row in rows:
            loom = row.loom_no.strip()
            verdict = check_row_consistency(row)
            if verdict == "pass" and loom.isdigit():
                verified[report_date][loom] = row
            else:
                flagged.append(
                    {
                        "loom_no": row.loom_no,
                        "report_date": report_date.isoformat(),
                        "reason": f"CMPX row {verdict}s the identity check"
                        if verdict != "pass"
                        else "unusable loom_no",
                        "row": row.model_dump(),
                    }
                )

    if DB_PATH.exists():
        DB_PATH.unlink()
    engine = create_engine(f"sqlite:///{DB_PATH}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    machine_types: dict[str, str] = {}
    for _, rows in report_days:
        for row in rows:
            loom = row.loom_no.strip()
            if row.loom_type in MACHINE_TYPES and loom.isdigit():
                machine_types.setdefault(loom, row.loom_type)

    status_looms = {loom for day in verified.values() for loom in day}
    all_looms = sorted(set(sheets_by_loom) | status_looms, key=int)

    with session_factory() as session:
        for loom in all_looms:
            repo.create_loom(session, loom, machine_types.get(loom))

        articles = 0
        for loom, extraction in sheets_by_loom.items():
            article = design_sheet_to_article(extraction)
            repo.upsert_article(session, article)
            repo.assign_article(session, loom, article.article_id, ASSIGNMENT_START)
            articles += 1

        statuses = 0
        for report_date, day_rows in sorted(verified.items()):
            for loom, row in day_rows.items():
                event = StatusEvent(
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
                try:
                    repo.add_status(session, loom, event)
                    statuses += 1
                except repo.DuplicateStatusError:
                    flagged.append(
                        {
                            "loom_no": loom,
                            "report_date": report_date.isoformat(),
                            "reason": "duplicate CMPX row",
                            "row": row.model_dump(),
                        }
                    )

        flagged_path = INCOMING / "flagged_for_review.json"
        flagged_path.write_text(json.dumps(flagged, indent=2))

        day_list = ", ".join(d.isoformat() for d in sorted(verified))
        print(f"Ingested {len(sheets_by_loom)} sheets + report days [{day_list}]:")
        print(f"  looms:        {len(all_looms)}")
        print(f"  articles:     {articles} (assigned as of {ASSIGNMENT_START.isoformat()})")
        print(f"  status rows:  {statuses} verified (identity check)")
        print(f"  flagged:      {len(flagged)} items -> {flagged_path.relative_to(ROOT)}")

        # --- Trend: compare the two most recent report days -----------------
        if len(verified) >= 2:
            earlier_date, later_date = sorted(verified)[-2:]
            earlier, later = verified[earlier_date], verified[later_date]
            movers = []
            for loom in sorted(set(earlier) & set(later), key=int):
                before, after = earlier[loom].weft_cmpx, later[loom].weft_cmpx
                if before is not None and after is not None:
                    movers.append((loom, before, after, after - before))
            movers.sort(key=lambda m: m[3])
            span = f"{earlier_date.isoformat()} -> {later_date.isoformat()}"
            print(f"\nWeft CMPX movement, {span} ({len(movers)} looms on both days):")
            print("  biggest deteriorations:")
            for loom, before, after, delta in movers[-5:][::-1]:
                print(f"    loom {loom:>3}: {before:>6.2f} -> {after:>6.2f}  ({delta:+.2f})")
            print("  biggest improvements:")
            for loom, before, after, delta in movers[:5]:
                print(f"    loom {loom:>3}: {before:>6.2f} -> {after:>6.2f}  ({delta:+.2f})")

        # --- Advice for the current worst weft offender ----------------------
        cfg = load_config()
        latest_rows = verified[max(verified)]
        worst_loom, worst_row = max(
            ((loom, row) for loom, row in latest_rows.items() if row.weft_cmpx is not None),
            key=lambda item: item[1].weft_cmpx,
        )
        print(f"\nFull advice for the worst loom on {max(verified).isoformat()}:\n")
        _print_report(advise(repo.get_loom_record(session, worst_loom), cfg))


if __name__ == "__main__":
    main()
