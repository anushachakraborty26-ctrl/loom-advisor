"""Quantified predictions, self-tightening evidence, automatic
attribution, and the ERP connector socket."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from loom_advisor.attribution import detect_interventions
from loom_advisor.db import repo
from loom_advisor.db.models import Base, SettingsEventRow
from loom_advisor.engine import advise, evidence_from_cases, load_config
from loom_advisor.ingestion.erp import CSVERPAdapter, sync_settings_csv, sync_status_csv
from loom_advisor.schema import EffectCase, LoomRecord, Settings, StatusEvent

GOLDEN = Path(__file__).parent / "golden"
CFG = load_config()


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/evidence.db")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


# --- Quantified projections ---------------------------------------------------


def test_loom47_projection_gives_actual_numbers():
    """The study-style output: which knob, by how much, and the numbers
    this loom should expect — computed from the four real study cases."""
    record = LoomRecord.model_validate_json((GOLDEN / "loom47.json").read_text())
    report = advise(record, CFG)
    main_fix = next(s for s in report.suggestions if s.rule_id == "BAND-MAIN-HIGH")
    effect = main_fix.expected_effect
    assert "3.4-3.5" in main_fix.action  # by how much
    assert "88.0 → ~41" in effect.projected_weft_cmpx  # predicted CMPX
    assert "(range 34–45)" in effect.projected_weft_cmpx
    assert "50% → ~58%" in effect.projected_efficiency  # predicted efficiency
    assert effect.n_cases == 4


def test_projection_scales_with_the_looms_own_numbers():
    record = LoomRecord.model_validate_json((GOLDEN / "loom03.json").read_text())
    report = advise(record, CFG)
    effect = next(s for s in report.suggestions if s.expected_effect.projected_weft_cmpx)
    # loom 03 starts at 97.27, so its projection must differ from loom 47's
    assert effect.expected_effect.projected_weft_cmpx.startswith("97.3 →")


def test_evidence_tightens_with_more_cases():
    """More recorded interventions -> narrower range, higher n. The
    self-strengthening the user asked for, as arithmetic."""
    wide = evidence_from_cases(
        [
            EffectCase(cmpx_before=100, cmpx_after=20, eff_before=50, eff_after=80),
            EffectCase(cmpx_before=100, cmpx_after=90, eff_before=50, eff_after=52),
        ],
        "test",
    )
    tight = evidence_from_cases(
        [
            EffectCase(cmpx_before=100, cmpx_after=48, eff_before=50, eff_after=60),
            EffectCase(cmpx_before=100, cmpx_after=50, eff_before=50, eff_after=61),
            EffectCase(cmpx_before=100, cmpx_after=52, eff_before=50, eff_after=59),
        ],
        "test",
    )
    wide_span = wide.cmpx_reduction_pct[2] - wide.cmpx_reduction_pct[0]
    tight_span = tight.cmpx_reduction_pct[2] - tight.cmpx_reduction_pct[0]
    assert tight_span < wide_span
    assert tight.n_cases == 3


def test_engine_uses_passed_evidence():
    record = LoomRecord.model_validate_json((GOLDEN / "loom47.json").read_text())
    single = evidence_from_cases(
        [EffectCase(cmpx_before=100, cmpx_after=50, eff_before=50, eff_after=60)],
        "one observed case",
    )
    report = advise(record, CFG, evidence=single)
    effect = report.suggestions[0].expected_effect
    assert effect.n_cases == 1
    assert effect.source == "one observed case"
    assert "88.0 → ~44" in effect.projected_weft_cmpx  # 50% reduction of 88


# --- Automatic attribution ----------------------------------------------------


def seed_settings(session, loom_id, at, **knobs):
    session.add(
        SettingsEventRow(
            loom_id=loom_id,
            recorded_at=at,
            snapshot=Settings(**knobs).model_dump(mode="json", exclude_none=True),
        )
    )
    session.commit()


def test_attribution_detects_and_dedupes(session):
    repo.create_loom(session, "9")
    seed_settings(session, "9", datetime(2026, 7, 1, 9, 0), main_pressure=3.6)
    seed_settings(session, "9", datetime(2026, 7, 10, 9, 0), main_pressure=3.4)
    repo.add_status(
        session, "9", StatusEvent(report_date=date(2026, 7, 8), weft_cmpx=80, efficiency_pct=50)
    )
    repo.add_status(
        session, "9", StatusEvent(report_date=date(2026, 7, 12), weft_cmpx=40, efficiency_pct=60)
    )
    assert detect_interventions(session) == 1
    cases = repo.effect_cases(session)
    assert len(cases) == 1
    assert cases[0].cmpx_before == 80 and cases[0].cmpx_after == 40
    # re-running never double-counts evidence
    assert detect_interventions(session) == 0


def test_attribution_ignores_unchanged_settings(session):
    repo.create_loom(session, "9")
    seed_settings(session, "9", datetime(2026, 7, 1, 9, 0), main_pressure=3.4)
    seed_settings(session, "9", datetime(2026, 7, 10, 9, 0), main_pressure=3.4)
    repo.add_status(
        session, "9", StatusEvent(report_date=date(2026, 7, 8), weft_cmpx=80, efficiency_pct=50)
    )
    repo.add_status(
        session, "9", StatusEvent(report_date=date(2026, 7, 12), weft_cmpx=40, efficiency_pct=60)
    )
    assert detect_interventions(session) == 0


# --- ERP connector -------------------------------------------------------------

STATUS_CSV = (
    "LOOM NO,DATE,LOOM,EFFI,RPM,PILE BREAK,PILE CMPX,GRD BREAK,"
    "GRD CMPX,WEFT BREAK,WEFT CMPX,BKG/HOURS,TOTAL KP\n"
    "7,01.07.2026,DOBBY,80,450,5,1.0,10,2.0,50,10.0,3,500\n"
    "9,01.07.2026,DOBBY,70,440,5,1.0,10,2.0,50,99.0,3,500\n"
)

SETTINGS_CSV = """LOOM NO,DATE,MAIN PRESSURE,TANDEM PRESSURE,SUB PRESSURE,SHED CROSSING,SPEED
7,01.07.2026,3.6,3.2,5.5,302,450
"""


def test_erp_status_sync_uses_verify_gate(session, tmp_path):
    csv_path = tmp_path / "status.csv"
    csv_path.write_text(STATUS_CSV)
    summary = sync_status_csv(session, csv_path)
    assert summary.verified == 1  # loom 7 passes the identity check
    assert summary.queued_for_review == 1  # loom 9's weft_cmpx contradicts breaks/KP
    assert repo.get_history(session, "7")[0].weft_cmpx == pytest.approx(10.0)


def test_erp_settings_sync_enables_band_advice(session, tmp_path):
    csv_path = tmp_path / "settings.csv"
    csv_path.write_text(SETTINGS_CSV)
    assert sync_settings_csv(session, csv_path) == 1
    record = repo.get_loom_record(session, "7")
    assert record.settings.main_pressure == 3.6
    # main 3.6 is above the coarse-OE band: with a matching construction the
    # engine would now emit BAND-MAIN-HIGH — the ERP data feeds the same brain.


def test_erp_mapping_is_configurable(tmp_path):
    mapping = {
        "date_format": "%Y/%m/%d",
        "status": {
            "loom_no": "Machine",
            "date": "Day",
            "weft_breaks": "WB",
            "weft_cmpx": "WC",
            "total_kilopicks": "KP",
        },
    }
    csv_path = tmp_path / "custom.csv"
    csv_path.write_text("Machine,Day,WB,WC,KP\n12,2026/07/01,50,10.0,500\n")
    by_date = CSVERPAdapter(mapping).read_status(csv_path)
    rows = by_date[date(2026, 7, 1)]
    assert rows[0].loom_no == "12"
    assert rows[0].weft_cmpx == 10.0

