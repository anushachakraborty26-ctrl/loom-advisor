"""Extract one day's Break CMPX report photos into a dated extraction file.

    ANTHROPIC_API_KEY=... uv run python scripts/extract_report.py \
        --date 2026-06-18 data/incoming/cmpx_reports/2026-06-18_*.jpg

Each page is read by the VLM; every row is audited with the identity check
(cmpx ~= breaks / kilopicks * 100). The merged result lands in
data/incoming/extracted_reports/<date>.json for ingest_extractions.py.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from loom_advisor.ingestion import check_row_consistency, read_cmpx_report

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "incoming" / "extracted_reports"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Report date, YYYY-MM-DD")
    parser.add_argument("pages", nargs="+", type=Path, help="Report page photos, in order")
    args = parser.parse_args()
    report_date = date.fromisoformat(args.date)

    rows, avg_row = [], None
    for page in args.pages:
        print(f"reading {page.name} ...")
        extraction = read_cmpx_report(page)
        rows.extend(r.model_dump() for r in extraction.rows)
        if extraction.avg_row is not None:
            avg_row = extraction.avg_row.model_dump()

    verdicts = {"pass": 0, "fail": 0, "insufficient": 0}
    from loom_advisor.ingestion import CmpxRow

    for raw in rows:
        verdicts[check_row_consistency(CmpxRow.model_validate(raw))] += 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{report_date.isoformat()}.json"
    out.write_text(
        json.dumps(
            {"report_date": report_date.isoformat(), "rows": rows, "avg_row": avg_row},
            indent=2,
        )
    )
    print(f"{len(rows)} rows -> {out.relative_to(ROOT)} | identity check: {verdicts}")


if __name__ == "__main__":
    main()
