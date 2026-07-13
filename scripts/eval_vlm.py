"""Score the VLM extractors against the curated ground truth.

    uv run python scripts/eval_vlm.py

Needs ANTHROPIC_API_KEY (or an ant auth profile) and the factory photos in
data/incoming/. Prints field-level accuracy and writes a JSON result file —
run it after every prompt change so extraction quality is a number, not a
feeling.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from loom_advisor.ingestion import check_row_consistency, read_cmpx_report, read_design_sheet

ROOT = Path(__file__).resolve().parent.parent
INCOMING = ROOT / "data" / "incoming"
GT_DIR = INCOMING / "ground_truth"
RESULTS_DIR = INCOMING / "eval_results"

# ground-truth key -> extraction attribute, comparison kind
SHEET_FIELDS = {
    "loom_no": ("loom_no", "text"),
    "party_name": ("party_name", "text"),
    "material_code": ("material_code", "text"),
    "pile_ratio_handwritten": ("pile_ratio", "number"),
    "fpl_height_mm": ("fpl_height_mm", "number"),
    "grey_picks_per_cm": ("grey_picks_per_cm", "number"),
    "finish_gsm": ("finish_gsm", "number"),
    "finish_wt_per_pc_gms": ("finish_wt_per_pc_gms", "number"),
}
ROW_FIELDS = [
    "efficiency_pct", "rpm", "pile_breaks", "pile_cmpx", "ground_breaks",
    "ground_cmpx", "weft_breaks", "weft_cmpx", "breaks_per_hour", "total_kilopicks",
]


def matches(kind: str, expected, actual) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    if kind == "number":
        try:
            return abs(float(expected) - float(actual)) <= max(0.01, 0.005 * abs(float(expected)))
        except (TypeError, ValueError):
            return False
    return str(expected).strip().lower() == str(actual).strip().lower()


def eval_design_sheets(results: dict) -> tuple[int, int]:
    gt = json.loads((GT_DIR / "design_sheets_gt.json").read_text())
    correct = total = 0
    for sheet in gt["sheets"]:
        path = INCOMING / sheet["file"]
        if not path.exists():
            print(f"  SKIP (photo missing): {sheet['file']}")
            continue
        extraction = read_design_sheet(path)
        mismatches = {}
        for gt_key, (attr, kind) in SHEET_FIELDS.items():
            if gt_key not in sheet:
                continue
            total += 1
            actual = getattr(extraction, attr)
            if matches(kind, sheet[gt_key], actual):
                correct += 1
            else:
                mismatches[gt_key] = {"expected": sheet[gt_key], "got": actual}
        label = f"loom {extraction.loom_no or '?'}"
        print(f"  {label:>10}: {'OK' if not mismatches else 'MISMATCH ' + json.dumps(mismatches)}")
        results["design_sheets"].append(
            {"file": sheet["file"], "extraction": extraction.model_dump(), "mismatches": mismatches}
        )
    return correct, total


def eval_cmpx_report(results: dict) -> tuple[int, int]:
    gt = json.loads((GT_DIR / "cmpx_report_2026-06-24_gt.json").read_text())
    gt_rows = {row["loom_no"]: row for row in gt["sample_rows" if "sample_rows" in gt else "rows"]}
    correct = total = 0
    for page in sorted((INCOMING / "cmpx_reports").glob("*.jpeg")):
        extraction = read_cmpx_report(page)
        consistency = [check_row_consistency(r) for r in extraction.rows]
        summary = {
            "page": page.name,
            "rows": len(extraction.rows),
            "pass": consistency.count("pass"),
            "fail": consistency.count("fail"),
            "insufficient": consistency.count("insufficient"),
        }
        print(f"  {page.name}: {summary['rows']} rows | identity check: "
              f"{summary['pass']} pass / {summary['fail']} fail / "
              f"{summary['insufficient']} insufficient")
        extracted_by_loom = {r.loom_no: r for r in extraction.rows}
        for loom_no, gt_row in gt_rows.items():
            row = extracted_by_loom.get(loom_no)
            if row is None:
                continue
            mismatches = {}
            for field in ROW_FIELDS:
                if gt_row.get(field) is None:
                    continue
                total += 1
                actual = getattr(row, field)
                if matches("number", gt_row[field], actual):
                    correct += 1
                else:
                    mismatches[field] = {"expected": gt_row[field], "got": actual}
            verdict = "OK" if not mismatches else "MISMATCH " + json.dumps(mismatches)
            print(f"    loom {loom_no:>3}: {verdict}")
        summary["extraction"] = extraction.model_dump()
        results["cmpx_pages"].append(summary)
    return correct, total


def main() -> int:
    results: dict = {"design_sheets": [], "cmpx_pages": []}
    print("Design sheets:")
    sheet_correct, sheet_total = eval_design_sheets(results)
    print("CMPX report pages:")
    row_correct, row_total = eval_cmpx_report(results)

    def pct(c: int, t: int) -> str:
        return f"{c}/{t} ({100 * c / t:.0f}%)" if t else "n/a"

    print(f"\nField accuracy — design sheets: {pct(sheet_correct, sheet_total)}"
          f" | report rows: {pct(row_correct, row_total)}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"eval_{time.strftime('%Y%m%d_%H%M%S')}.json"
    results["summary"] = {
        "sheet_fields_correct": sheet_correct, "sheet_fields_total": sheet_total,
        "row_fields_correct": row_correct, "row_fields_total": row_total,
    }
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"Full results: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
