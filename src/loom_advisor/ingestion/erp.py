"""ERP connector socket.

Factories keep this data in an ERP already; this module is the socket the
app plugs into it with. The v1 adapter reads CSV exports (every ERP can
produce one) using a column-mapping config, so connecting to a specific
plant's system means editing config/erp_mapping.yaml — not code. A REST
adapter for a live ERP API is a subclass with the same two methods.

ERP rows flow through the SAME verify gate as photo extractions
(service.process_rows): identity-checked when the columns allow it,
quarantined when not, always tied to a source document.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from ..db import repo
from ..schema import Settings
from .service import IngestSummary, process_rows
from .vlm_reader import CmpxRow

DEFAULT_MAPPING = Path(__file__).resolve().parent.parent / "config" / "erp_mapping.yaml"


def load_mapping(path: Path | None = None) -> dict[str, Any]:
    return yaml.safe_load((path or DEFAULT_MAPPING).read_bytes())


class CSVERPAdapter:
    """Reads status and settings rows from ERP CSV exports."""

    def __init__(self, mapping: dict[str, Any] | None = None):
        self.mapping = mapping or load_mapping()

    def _parse_date(self, raw: str) -> date:
        return datetime.strptime(raw.strip(), self.mapping.get("date_format", "%Y-%m-%d")).date()

    @staticmethod
    def _number(row: dict, column: str | None) -> float | None:
        if not column:
            return None
        raw = (row.get(column) or "").strip().replace(",", "").replace("%", "")
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    def read_status(self, csv_path: Path) -> dict[date, list[CmpxRow]]:
        """CSV -> rows grouped by report date."""
        columns = self.mapping["status"]
        by_date: dict[date, list[CmpxRow]] = defaultdict(list)
        with open(csv_path, newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                loom_type = (row.get(columns.get("loom_type", ""), "") or "").strip().lower()
                by_date[self._parse_date(row[columns["date"]])].append(
                    CmpxRow(
                        loom_no=(row.get(columns["loom_no"]) or "").strip(),
                        loom_type=loom_type or None,
                        efficiency_pct=self._number(row, columns.get("efficiency_pct")),
                        rpm=self._number(row, columns.get("rpm")),
                        pile_breaks=_as_int(self._number(row, columns.get("pile_breaks"))),
                        pile_cmpx=self._number(row, columns.get("pile_cmpx")),
                        ground_breaks=_as_int(self._number(row, columns.get("ground_breaks"))),
                        ground_cmpx=self._number(row, columns.get("ground_cmpx")),
                        weft_breaks=_as_int(self._number(row, columns.get("weft_breaks"))),
                        weft_cmpx=self._number(row, columns.get("weft_cmpx")),
                        breaks_per_hour=self._number(row, columns.get("breaks_per_hour")),
                        total_kilopicks=self._number(row, columns.get("total_kilopicks")),
                    )
                )
        return dict(by_date)

    def read_settings(self, csv_path: Path) -> list[tuple[str, date, Settings]]:
        """CSV -> (loom_id, effective_date, settings snapshot) triples."""
        columns = self.mapping["settings"]
        out: list[tuple[str, date, Settings]] = []
        with open(csv_path, newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                settings = Settings(
                    main_pressure=self._number(row, columns.get("main_pressure")),
                    tandem_pressure=self._number(row, columns.get("tandem_pressure")),
                    sub_pressure=self._number(row, columns.get("sub_pressure")),
                    main_nozzle_height_mm=self._number(row, columns.get("main_nozzle_height_mm")),
                    sub_nozzle_spacing_mm=self._number(row, columns.get("sub_nozzle_spacing_mm")),
                    sub_to_reed_gap_mm=self._number(row, columns.get("sub_to_reed_gap_mm")),
                    shed_crossing_deg=self._number(row, columns.get("shed_crossing_deg")),
                    speed_rpm=self._number(row, columns.get("speed_rpm")),
                )
                out.append(
                    (
                        (row.get(columns["loom_no"]) or "").strip(),
                        self._parse_date(row[columns["date"]]),
                        settings,
                    )
                )
        return out


def _as_int(value: float | None) -> int | None:
    return int(value) if value is not None else None


def sync_status_csv(
    session: Session, csv_path: Path, adapter: CSVERPAdapter | None = None
) -> IngestSummary:
    """Ingest an ERP status export through the standard verify gate."""
    adapter = adapter or CSVERPAdapter()
    summary = IngestSummary()
    doc_id = repo.add_document(session, "erp_status_export", str(csv_path), "erp")
    for report_date, rows in sorted(adapter.read_status(csv_path).items()):
        process_rows(session, rows, report_date, doc_id, summary)
    return summary


def sync_settings_csv(
    session: Session, csv_path: Path, adapter: CSVERPAdapter | None = None
) -> int:
    """Ingest an ERP settings export as dated settings events."""
    adapter = adapter or CSVERPAdapter()
    repo.add_document(session, "erp_settings_export", str(csv_path), "erp")
    count = 0
    for loom_id, _effective, settings in adapter.read_settings(csv_path):
        if not loom_id.isdigit():
            continue
        repo.create_loom(session, loom_id)
        repo.set_settings(session, loom_id, settings, recorded_by="erp")
        count += 1
    return count
