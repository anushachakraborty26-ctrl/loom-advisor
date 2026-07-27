"""Sync ERP CSV exports into the database.

    uv run python scripts/sync_erp.py --status-csv export.csv
    uv run python scripts/sync_erp.py --settings-csv settings.csv
    uv run python scripts/sync_erp.py --status-csv a.csv --settings-csv b.csv \
        --mapping my_plant_mapping.yaml

Status rows pass through the same verify gate as photo uploads; settings
land as dated events. After settings sync, attribution runs automatically
so any change with before/after reports becomes new prediction evidence.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from loom_advisor.attribution import detect_interventions
from loom_advisor.ingestion.erp import (
    CSVERPAdapter,
    load_mapping,
    sync_settings_csv,
    sync_status_csv,
)

ROOT = Path(__file__).resolve().parent.parent
DB_URL = os.environ.get("LOOM_DB_URL", f"sqlite:///{ROOT / 'loom_advisor.db'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-csv", type=Path)
    parser.add_argument("--settings-csv", type=Path)
    parser.add_argument("--mapping", type=Path, help="Column-mapping YAML (default: built-in)")
    args = parser.parse_args()
    if not args.status_csv and not args.settings_csv:
        parser.error("provide --status-csv and/or --settings-csv")

    adapter = CSVERPAdapter(load_mapping(args.mapping) if args.mapping else None)
    engine = create_engine(DB_URL)
    with sessionmaker(bind=engine)() as session:
        if args.status_csv:
            summary = sync_status_csv(session, args.status_csv, adapter)
            print(
                f"status: {summary.verified} verified, "
                f"{summary.queued_for_review} to review, {summary.duplicates} duplicates"
            )
        if args.settings_csv:
            count = sync_settings_csv(session, args.settings_csv, adapter)
            print(f"settings: {count} snapshots recorded")
        attributed = detect_interventions(session)
        if attributed:
            print(f"attribution: {attributed} new intervention(s) added to the evidence base")


if __name__ == "__main__":
    main()
