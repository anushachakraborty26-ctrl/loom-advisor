"""Generate a sample design-sheet Excel matching the ingestion template.

    uv run python scripts/make_sample_sheet.py
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

OUT = Path(__file__).resolve().parent.parent / "data" / "samples" / "design_sheet_sample.xlsx"

ROWS = [
    ("Article ID", "TERRY-12OE"),
    ("Warp count", "2/20s"),
    ("Weft count (Ne)", 12),
    ("Weft spin", "Open End"),
    ("Weft material", "Cotton"),
    ("EPI", 56),
    ("PPI", 42),
    ("Reed count", "48"),
    ("Pile ratio", "1:5.2"),
    ("GSM", 480),
    ("Width (cm)", 76),
    ("Selvedge type", "leno"),
]


def main() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Design Sheet"
    for row in ROWS:
        sheet.append(row)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(OUT)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
