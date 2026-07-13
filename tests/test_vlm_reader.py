"""VLM reader tests that need no network: the identity check, the weft
parser, the contract mapping, and the call plumbing via a stub client."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from loom_advisor.engine import advise, load_config
from loom_advisor.ingestion import (
    CmpxRow,
    DesignSheetExtraction,
    check_row_consistency,
    design_sheet_to_article,
    parse_weft_spec,
    read_design_sheet,
)
from loom_advisor.ingestion.vlm_reader import (
    _CmpxReportWire,
    _CmpxRowWire,
    _DesignSheetWire,
    _report_from_wire,
)
from loom_advisor.schema import LoomRecord, Settings, WeftSpin

# --- Identity check, seeded with verified rows from the 24-06-2026 report ---

LOOM_1 = CmpxRow(
    loom_no="1", loom_type="jacquard", efficiency_pct=70, rpm=425,
    pile_breaks=7, pile_cmpx=1.78, ground_breaks=11, ground_cmpx=2.80,
    weft_breaks=151, weft_cmpx=36.05, breaks_per_hour=7, total_kilopicks=410,
)
LOOM_90 = CmpxRow(
    loom_no="90", loom_type="dobby", efficiency_pct=78, rpm=486,
    pile_breaks=32, pile_cmpx=6.31, ground_breaks=30, ground_cmpx=5.66,
    weft_breaks=82, weft_cmpx=15.75, breaks_per_hour=6, total_kilopicks=528,
)


def test_verified_rows_pass():
    assert check_row_consistency(LOOM_1) == "pass"
    assert check_row_consistency(LOOM_90) == "pass"


def test_misread_cell_fails():
    """Loom 104's hand-extraction: ground and weft matched exactly but the
    pile count didn't - the identity check must catch exactly that."""
    row = LOOM_1.model_copy(update={"pile_breaks": 7, "pile_cmpx": 3.17, "total_kilopicks": 362})
    assert check_row_consistency(row) == "fail"


def test_misaligned_row_fails():
    """Values from one loom paired with another loom's kilopicks."""
    row = LOOM_1.model_copy(update={"total_kilopicks": 200})
    assert check_row_consistency(row) == "fail"


def test_missing_kilopicks_is_insufficient():
    row = LOOM_1.model_copy(update={"total_kilopicks": None})
    assert check_row_consistency(row) == "insufficient"


def test_stopped_loom_zeros_pass():
    row = CmpxRow(
        loom_no="28", pile_breaks=0, pile_cmpx=0.0, ground_breaks=0,
        ground_cmpx=0.0, weft_breaks=0, weft_cmpx=0.0, total_kilopicks=100,
    )
    assert check_row_consistency(row) == "pass"


# --- Weft spec parsing (formats seen on the real sheets) ---------------------


@pytest.mark.parametrize(
    ("spec", "count", "spin", "material"),
    [
        ("14s OE RFD WHITE", 14.0, WeftSpin.oe, "cotton"),
        ("12sOE", 12.0, WeftSpin.oe, "cotton"),
        ("12s KC/14.2 TPI/COT100", 12.0, None, "cotton"),
        ("30 Denier polyester filament", None, WeftSpin.filament, "polyester"),
        (None, None, None, None),
    ],
)
def test_parse_weft_spec(spec, count, spin, material):
    assert parse_weft_spec(spec) == (count, spin, material)


# --- Extraction -> Article -> engine, end to end ------------------------------


def test_extraction_drives_correct_profile():
    extraction = DesignSheetExtraction(
        loom_no="90", quality="PDD - PLAIN 1PLY", body_weft="12sOE",
        pile_ratio=8.1, finish_gsm=600, reed_count_per_inch=50.3,
    )
    article = design_sheet_to_article(extraction)
    assert article.article_id == "PDD - PLAIN 1PLY"
    assert article.construction.weft_count_ne == 12
    assert article.construction.weft_spin == WeftSpin.oe

    record = LoomRecord(
        loom_id="90", construction=article.construction,
        settings=Settings(main_pressure=3.6),
    )
    report = advise(record, load_config())
    assert report.profile == "coarse_oe_cotton"


def test_design_no_strips_xls_suffix():
    extraction = DesignSheetExtraction(design_no="S8054_58reed.xls")
    assert design_sheet_to_article(extraction).article_id == "S8054_58reed"


# --- Wire model conversion (the ""-means-unknown convention) ------------------

_BLANK_ROW = dict.fromkeys(
    ["loom_type", "efficiency_pct", "rpm", "pile_breaks", "pile_cmpx",
     "ground_breaks", "ground_cmpx", "weft_breaks", "weft_cmpx",
     "breaks_per_hour", "total_kilopicks"], "")


def test_report_wire_conversion_and_avg_split():
    wire = _CmpxReportWire(
        report_date="2026-06-24",
        rows=[
            _CmpxRowWire(**{**_BLANK_ROW, "loom_no": "1", "loom_type": "jacquard",
                            "efficiency_pct": "70", "weft_breaks": "151",
                            "weft_cmpx": "36.05", "total_kilopicks": "410"}),
            _CmpxRowWire(**{**_BLANK_ROW, "loom_no": "AVG", "efficiency_pct": "77"}),
        ],
    )
    report = _report_from_wire(wire)
    assert report.report_date == "2026-06-24"
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.weft_breaks == 151 and row.weft_cmpx == 36.05
    assert row.rpm is None  # "" -> None
    assert report.avg_row is not None and report.avg_row.efficiency_pct == 77


# --- Call plumbing via stub client (no SDK, no network) ------------------------


class StubClient:
    def __init__(self, parsed):
        self._parsed = parsed
        self.kwargs = None
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(parsed_output=self._parsed)


def _wire_sheet(**overrides) -> _DesignSheetWire:
    fields = dict.fromkeys(_DesignSheetWire.model_fields, "")
    fields.update(overrides)
    return _DesignSheetWire(**fields)


def test_read_design_sheet_plumbing(tmp_path):
    photo = tmp_path / "sheet.jpeg"
    photo.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    stub = StubClient(_wire_sheet(loom_no="106", finish_gsm="380"))

    result = read_design_sheet(photo, client=stub)

    assert result == DesignSheetExtraction(loom_no="106", finish_gsm=380)
    assert stub.kwargs["output_format"] is _DesignSheetWire
    content = stub.kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert "NEVER guess" in content[1]["text"]


def test_failed_parse_raises(tmp_path):
    photo = tmp_path / "sheet.png"
    photo.write_bytes(b"fake-png")
    with pytest.raises(ValueError, match="no parsed output"):
        read_design_sheet(photo, client=StubClient(None))
