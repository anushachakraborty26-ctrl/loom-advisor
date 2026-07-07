"""Golden tests: the engine must reproduce the manual fixes of the
June 2025 plant study from each loom's before-state. If a config or code
change ever breaks these, the engine no longer matches the evidence."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from loom_advisor.engine import advise, load_config
from loom_advisor.schema import (
    BreakType,
    Construction,
    LoomRecord,
    Settings,
    StatusEvent,
    WeftSpin,
    YarnQuality,
)

GOLDEN = Path(__file__).parent / "golden"
CFG = load_config()


def load_golden(name: str) -> LoomRecord:
    return LoomRecord.model_validate(json.loads((GOLDEN / name).read_text()))


def rule_ids(report) -> list[str]:
    return [s.rule_id for s in report.suggestions]


# --- The four looms of the plant study -------------------------------------


def test_loom47_reproduces_manual_fix():
    """Dobby, 12s OE: the study lowered main 3.6->3.4, raised sub 5.5->5.9,
    delayed crossing 302->312. The engine must recover all three moves."""
    report = advise(load_golden("loom47.json"), CFG)
    ids = rule_ids(report)
    assert "BAND-MAIN-HIGH" in ids
    assert "REBALANCE" in ids
    assert "TIMING-DELAY" in ids
    # tandem 3.2 sits inside its band (3.0-3.2): must NOT be flagged
    assert "BAND-TANDEM-HIGH" not in ids
    # highest-confidence suggestions come first
    assert report.suggestions[0].confidence.value == "high"


def test_loom12_leaves_main_alone():
    """Jacquard, 12s OE: main was 3.4 (in band) and the study kept it.
    Tandem was 3.5 (above band) and the study lowered it. The engine must
    make exactly that distinction."""
    report = advise(load_golden("loom12.json"), CFG)
    ids = rule_ids(report)
    assert "BAND-TANDEM-HIGH" in ids
    assert "BAND-MAIN-HIGH" not in ids


def test_loom03_matches_study():
    report = advise(load_golden("loom03.json"), CFG)
    ids = rule_ids(report)
    assert "BAND-TANDEM-HIGH" in ids
    assert "BAND-MAIN-HIGH" not in ids  # main 3.5 is the band edge, kept


def test_loom17_matches_study():
    report = advise(load_golden("loom17.json"), CFG)
    ids = rule_ids(report)
    assert "BAND-MAIN-HIGH" in ids
    assert "REBALANCE" in ids
    assert "TIMING-DELAY" in ids


@pytest.mark.parametrize("name", ["loom47.json", "loom17.json", "loom12.json", "loom03.json"])
def test_all_study_looms_get_hygiene_and_alert(name):
    """All four started with CMPX above the alert threshold."""
    report = advise(load_golden(name), CFG)
    assert "HYGIENE" in rule_ids(report)
    assert any("ALERT" in note for note in report.notes)


# --- Honesty properties -----------------------------------------------------


def test_expected_effect_never_a_point_estimate():
    """Predictions must be ranges with sample size, never a single number."""
    for name in ["loom47.json", "loom12.json"]:
        report = advise(load_golden(name), CFG)
        for s in report.suggestions:
            if s.expected_effect and s.expected_effect.historical_range:
                assert s.expected_effect.n_cases is not None
                assert "to" in s.expected_effect.historical_range


def test_unknown_yarn_refuses_to_guess():
    """A construction with no matching profile must produce PROFILE-MISSING,
    not borrowed bands from another yarn."""
    record = LoomRecord(
        loom_id="X",
        construction=Construction(weft_material="silk", weft_count_ne=40),
        settings=Settings(main_pressure=9.9),
        status_log=[StatusEvent(report_date=date(2025, 6, 20), filling_cmpx=80)],
    )
    report = advise(record, CFG)
    assert report.profile == "none"
    assert "PROFILE-MISSING" in rule_ids(report)
    assert "BAND-MAIN-HIGH" not in rule_ids(report)


def test_filament_profile_has_no_bands_yet():
    """The study claims filament results without tabulated data; the engine
    must match the profile but skip band checks and say why."""
    record = LoomRecord(
        loom_id="F",
        construction=Construction(weft_material="polyester", weft_spin=WeftSpin.filament),
        settings=Settings(main_pressure=3.6),
        status_log=[StatusEvent(report_date=date(2025, 6, 20), filling_cmpx=50)],
    )
    report = advise(record, CFG)
    assert report.profile == "polyester_filament"
    assert "BAND-MAIN-HIGH" not in rule_ids(report)
    assert any("no bands yet" in n for n in report.notes)


# --- Symptom and meta rules --------------------------------------------------


def test_bend_symptom_fires():
    record = load_golden("loom12.json")
    record.status_log[0].break_type = BreakType.bend
    report = advise(record, CFG)
    assert "SYM-BEND" in rule_ids(report)


def test_yarn_gate_fires_on_weak_yarn():
    record = load_golden("loom47.json")
    record.yarn_quality = YarnQuality(rkm=10.5)
    report = advise(record, CFG)
    assert "YARN-GATE" in rule_ids(report)


def test_no_rebalance_when_sub_already_at_max():
    record = load_golden("loom47.json")
    record.settings.sub_pressure = 6.0
    report = advise(record, CFG)
    assert "REBALANCE" not in rule_ids(report)


def test_quiet_loom_gets_no_noise():
    """A loom in band with low CMPX should produce no suggestions."""
    record = load_golden("loom47.json")
    record.settings.main_pressure = 3.4
    record.settings.shed_crossing_deg = 312
    record.status_log = [
        StatusEvent(report_date=date(2025, 6, 20), filling_cmpx=25, efficiency_pct=80)
    ]
    report = advise(record, CFG)
    assert report.suggestions == []


# --- Engineering properties ---------------------------------------------------


def test_determinism():
    a = advise(load_golden("loom47.json"), CFG)
    b = advise(load_golden("loom47.json"), CFG)
    assert a == b


def test_every_suggestion_is_traceable():
    report = advise(load_golden("loom47.json"), CFG)
    assert report.rules_version
    for s in report.suggestions:
        assert s.rule_id
        assert s.reasoning
