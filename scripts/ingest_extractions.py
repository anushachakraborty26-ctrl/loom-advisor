"""Pour the VLM-extracted shed into the database.

Sources (all produced by earlier steps, no API calls here):
- data/incoming/extracted/*.json        design-sheet extractions (one per photo)
- data/incoming/eval_results/eval_*.json  CMPX report page extractions (latest)

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
REPORT_DATE = date(2026, 6, 24)
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


def load_cmpx_rows() -> tuple[list[CmpxRow], str]:
    latest = max((INCOMING / "eval_results").glob("eval_*.json"))
    data = json.loads(latest.read_text())
    rows = [
        CmpxRow.model_validate(raw)
        for page in data["cmpx_pages"]
        for raw in page["extraction"]["rows"]
    ]
    return rows, latest.name


def main() -> None:
    sheets_by_loom, flagged = dedupe_sheets(load_sheets())
    cmpx_rows, eval_file = load_cmpx_rows()

    verified_rows: list[CmpxRow] = []
    for row in cmpx_rows:
        verdict = check_row_consistency(row)
        if verdict == "pass":
            verified_rows.append(row)
        else:
            flagged.append(
                {
                    "loom_no": row.loom_no,
                    "reason": f"CMPX row {verdict}s the identity check",
                    "row": row.model_dump(),
                }
            )

    if DB_PATH.exists():
        DB_PATH.unlink()
    engine = create_engine(f"sqlite:///{DB_PATH}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    machine_types = {
        r.loom_no.strip(): r.loom_type
        for r in cmpx_rows
        if r.loom_type in MACHINE_TYPES and r.loom_no.strip().isdigit()
    }
    status_looms = {r.loom_no.strip() for r in verified_rows if r.loom_no.strip().isdigit()}
    all_looms = sorted(set(sheets_by_loom) | status_looms, key=int)

    with session_factory() as session:
        for loom in all_looms:
            repo.create_loom(session, loom, machine_types.get(loom))

        articles = 0
        for loom, extraction in sheets_by_loom.items():
            article = design_sheet_to_article(extraction)
            repo.upsert_article(session, article)
            repo.assign_article(session, loom, article.article_id, REPORT_DATE)
            articles += 1

        statuses = 0
        for row in verified_rows:
            loom = row.loom_no.strip()
            if not loom.isdigit():
                continue
            event = StatusEvent(
                report_date=REPORT_DATE,
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
                    {"loom_no": loom, "reason": "duplicate CMPX row", "row": row.model_dump()}
                )

        flagged_path = INCOMING / "flagged_for_review.json"
        flagged_path.write_text(json.dumps(flagged, indent=2))

        print(f"Ingested from {eval_file} + {len(sheets_by_loom)} deduped sheets:")
        print(f"  looms:        {len(all_looms)}")
        print(f"  articles:     {articles} (assigned as of {REPORT_DATE.isoformat()})")
        print(f"  status rows:  {statuses} verified (identity check)")
        print(f"  flagged:      {len(flagged)} items -> {flagged_path.relative_to(ROOT)}")

        # Show the advisor working on the shed's worst weft offenders.
        cfg = load_config()
        worst = sorted(
            (r for r in verified_rows if r.weft_cmpx is not None),
            key=lambda r: r.weft_cmpx or 0,
            reverse=True,
        )[:5]
        print("\nWorst weft CMPX on the shed, per the advisor:")
        for row in worst:
            record = repo.get_loom_record(session, row.loom_no.strip())
            report = advise(record, cfg)
            rules = ", ".join(s.rule_id for s in report.suggestions) or "none"
            print(
                f"  loom {row.loom_no:>3} ({record.machine_type or '?':>8}) "
                f"weft CMPX {row.weft_cmpx:>6.2f} | profile {report.profile:<18} | {rules}"
            )

        print("\nFull advice for the worst loom:\n")
        worst_record = repo.get_loom_record(session, worst[0].loom_no.strip())
        _print_report(advise(worst_record, cfg))


if __name__ == "__main__":
    main()
