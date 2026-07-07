"""Excel adapter tests: a studio-style sheet must round-trip into a
validated Article."""

from __future__ import annotations

import pytest
from openpyxl import Workbook

from loom_advisor.ingestion import parse_design_sheet
from loom_advisor.schema import WeftSpin

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
    ("Some Unrelated Row", "ignored"),
]


def write_sheet(path, rows):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def test_parse_design_sheet(tmp_path):
    path = tmp_path / "design.xlsx"
    write_sheet(path, ROWS)
    article = parse_design_sheet(path)
    assert article.article_id == "TERRY-12OE"
    c = article.construction
    assert c.weft_count_ne == 12
    assert c.weft_spin == WeftSpin.oe  # "Open End" normalised
    assert c.weft_material == "cotton"
    assert c.pile_ratio == "1:5.2"
    assert c.gsm == 480


def test_missing_article_id_rejected(tmp_path):
    path = tmp_path / "bad.xlsx"
    write_sheet(path, [("Weft count (Ne)", 12)])
    with pytest.raises(ValueError, match="Article ID"):
        parse_design_sheet(path)


def test_parsed_article_drives_correct_profile(tmp_path):
    """The whole point: a studio sheet must land the loom in the right
    yarn profile so band checks use the right golden values."""
    from loom_advisor.engine import advise, load_config
    from loom_advisor.schema import LoomRecord, Settings

    path = tmp_path / "design.xlsx"
    write_sheet(path, ROWS)
    article = parse_design_sheet(path)
    record = LoomRecord(
        loom_id="47",
        article_id=article.article_id,
        construction=article.construction,
        settings=Settings(main_pressure=3.6),
    )
    report = advise(record, load_config())
    assert report.profile == "coarse_oe_cotton"
    assert "BAND-MAIN-HIGH" in [s.rule_id for s in report.suggestions]
