"""Batch-extract every design-sheet photo in data/incoming/design_sheets/.

    uv run python scripts/extract_all_sheets.py [--limit N]

Resumable: each photo's extraction is saved to data/incoming/extracted/ and
already-extracted photos are skipped on re-runs. Failures are recorded, not
fatal. Needs ANTHROPIC_API_KEY (or an ant auth profile).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from loom_advisor.ingestion import read_design_sheet

ROOT = Path(__file__).resolve().parent.parent
SHEETS = ROOT / "data" / "incoming" / "design_sheets"
OUT = ROOT / "data" / "incoming" / "extracted"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Extract at most N new photos")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    photos = sorted(SHEETS.glob("*.jpeg")) + sorted(SHEETS.glob("*.jpg"))
    done = failed = skipped = 0
    for photo in photos:
        out_path = OUT / f"{photo.stem}.json"
        if out_path.exists():
            skipped += 1
            continue
        if args.limit is not None and done + failed >= args.limit:
            break
        try:
            extraction = read_design_sheet(photo)
        except Exception as exc:  # record and continue: one bad photo must not stop the batch
            failed += 1
            out_path.with_suffix(".error.txt").write_text(f"{type(exc).__name__}: {exc}")
            print(f"FAIL {photo.name}: {exc}")
            continue
        out_path.write_text(json.dumps(
            {"source_photo": photo.name, **extraction.model_dump()}, indent=2
        ))
        done += 1
        print(f"loom {extraction.loom_no or '?':>4} <- {photo.name}")

    print(f"\nExtracted {done}, failed {failed}, already done {skipped}, "
          f"total photos {len(photos)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
