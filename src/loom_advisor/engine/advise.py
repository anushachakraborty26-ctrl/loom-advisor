"""The advisor engine.

Deterministic and auditable: it interprets config/bands.yaml and
config/rules.yaml against one LoomRecord and emits ranked Suggestions, each
carrying the rule_id and config version that produced it. There is no LLM in
this loop — messy inputs are handled at the edges (ingestion); decisions are
pure logic.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..schema import (
    AdviceReport,
    Confidence,
    ExpectedEffect,
    LoomRecord,
    Suggestion,
)
from .bands import Violation, check_bands, select_profile

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

_CONFIDENCE_ORDER = {Confidence.high: 0, Confidence.medium: 1, Confidence.low: 2}


@dataclass(frozen=True)
class Config:
    bands: dict[str, Any]
    rules: dict[str, Any]
    version: str


def load_config(config_dir: Path | None = None) -> Config:
    cdir = config_dir or DEFAULT_CONFIG_DIR
    bands_raw = (cdir / "bands.yaml").read_bytes()
    rules_raw = (cdir / "rules.yaml").read_bytes()
    version = hashlib.sha256(bands_raw + rules_raw).hexdigest()[:8]
    return Config(
        bands=yaml.safe_load(bands_raw),
        rules=yaml.safe_load(rules_raw),
        version=version,
    )


def _study_effect(cfg: Config) -> ExpectedEffect:
    ev = cfg.bands["evidence"]["plant_study"]
    return ExpectedEffect(
        direction="CMPX down, efficiency up",
        historical_range=(
            f"{ev['efficiency_gain_points']} efficiency points; "
            f"CMPX {ev['cmpx_reduction_pct']}"
        ),
        n_cases=ev["n_cases"],
        source=ev["source"],
    )


def _direction_only(direction: str) -> ExpectedEffect:
    return ExpectedEffect(direction=direction)


def _band_suggestion(v: Violation, cfg: Config, effect: ExpectedEffect) -> Suggestion | None:
    rule = cfg.rules["band_rules"].get(v.setting, {}).get(v.direction)
    if rule is None:
        return None
    confidence = Confidence(rule["confidence"])
    return Suggestion(
        rule_id=rule["rule_id"],
        setting=v.setting,
        action=rule["action"].format(value=v.value, min=v.low, max=v.high, unit=v.unit).strip(),
        reasoning=rule["reasoning"].strip(),
        confidence=confidence,
        expected_effect=effect if confidence == Confidence.high else _direction_only("CMPX down"),
    )


def advise(record: LoomRecord, cfg: Config | None = None) -> AdviceReport:
    cfg = cfg or load_config()
    notes: list[str] = []
    suggestions: list[Suggestion] = []

    profile_name, profile = select_profile(record.construction, cfg.bands["profiles"])
    latest = record.latest_status
    thresholds = cfg.bands["thresholds"]
    cmpx = latest.weft_cmpx if latest else None
    alert = thresholds["weft_cmpx_target"]
    cmpx_high = cmpx is not None and cmpx >= alert
    study_effect = _study_effect(cfg)

    if latest is not None:
        status_bits = []
        if latest.weft_cmpx is not None:
            status_bits.append(f"weft CMPX {latest.weft_cmpx:g}")
        if latest.weft_breaks is not None:
            status_bits.append(f"{latest.weft_breaks} weft breaks")
        if latest.efficiency_pct is not None:
            status_bits.append(f"efficiency {latest.efficiency_pct:g}%")
        line = f"Latest report {latest.report_date.isoformat()}: " + " | ".join(status_bits)
        if cmpx_high:
            line += f" [ALERT: weft CMPX at or above target {alert:g}]"
        notes.append(line)

        # Pile/ground breaks are warp-side problems (warp preparation,
        # sizing, shed geometry) — flag them, but don't let them trigger
        # weft-insertion tuning.
        for category in ("pile", "ground"):
            value = getattr(latest, f"{category}_cmpx")
            target = thresholds[f"{category}_cmpx_target"]
            if value is not None and value >= target:
                notes.append(
                    f"{category.capitalize()} CMPX {value:g} is above the plant target "
                    f"{target:g} — warp-side issue (warp prep, sizing, shed geometry), "
                    "outside weft-insertion tuning scope."
                )
    else:
        notes.append("No status reports on record — advice is settings-only.")

    if profile is None:
        notes.append(
            "No settings profile matches this construction; band checks skipped. "
            "Define bands for this yarn in config/bands.yaml before trusting advice."
        )
        suggestions.append(
            Suggestion(
                rule_id="PROFILE-MISSING",
                action=(
                    "Collect trial data for this yarn and add a profile with bands "
                    "to config/bands.yaml. The engine does not guess outside its evidence."
                ),
                reasoning=(
                    "Golden values are yarn-specific: hairiness, count and spin change "
                    "how much air the weft needs. Applying another yarn's bands would "
                    "be a false precision."
                ),
                confidence=Confidence.low,
            )
        )
        violations: list[Violation] = []
    else:
        bands = profile.get("bands", {})
        if not bands:
            notes.append(
                f"Profile '{profile_name}' matched but has no bands yet "
                "(no tabulated trial data). Band checks skipped."
            )
        violations = check_bands(record.settings, bands)
        for v in violations:
            s = _band_suggestion(v, cfg, study_effect)
            if s is not None:
                suggestions.append(s)

        # REBALANCE: launch over-driven + breakage elevated -> shift energy
        # to the relay chain instead of only weakening the launch.
        launch_over = any(
            v.setting in ("main_pressure", "tandem_pressure") and v.direction == "above"
            for v in violations
        )
        sub_band = bands.get("sub_pressure")
        sub_val = record.settings.sub_pressure
        if launch_over and cmpx_high and sub_band and sub_val is not None:
            if sub_val < sub_band["max"]:
                rule = cfg.rules["meta_rules"]["rebalance"]
                suggestions.append(
                    Suggestion(
                        rule_id=rule["rule_id"],
                        setting="sub_pressure",
                        action=rule["action"]
                        .format(value=sub_val, max=sub_band["max"], unit=sub_band.get("unit", ""))
                        .strip(),
                        reasoning=rule["reasoning"].strip(),
                        confidence=Confidence(rule["confidence"]),
                        expected_effect=study_effect,
                    )
                )

        # TIMING: breakage elevated and crossing sits early in its band ->
        # a slightly later crossing is a known (small) lever.
        shed_band = bands.get("shed_crossing_deg")
        shed_val = record.settings.shed_crossing_deg
        if cmpx_high and shed_band and shed_val is not None and shed_val < shed_band["max"]:
            rule = cfg.rules["meta_rules"]["timing"]
            suggestions.append(
                Suggestion(
                    rule_id=rule["rule_id"],
                    setting="shed_crossing_deg",
                    action=rule["action"]
                    .format(value=shed_val, max=shed_band["max"], unit=shed_band.get("unit", ""))
                    .strip(),
                    reasoning=rule["reasoning"].strip(),
                    confidence=Confidence(rule["confidence"]),
                    expected_effect=_direction_only("CMPX down (small lever)"),
                )
            )

    # Symptom rules: the reported break type points at its cause.
    if latest is not None and latest.break_type is not None:
        rule = cfg.rules["symptom_rules"].get(latest.break_type.value)
        if rule is not None:
            suggestions.append(
                Suggestion(
                    rule_id=rule["rule_id"],
                    action=rule["action"].strip(),
                    reasoning=rule["reasoning"].strip(),
                    confidence=Confidence(rule["confidence"]),
                    expected_effect=_direction_only("CMPX down"),
                )
            )

    # YARN-GATE: weak yarn caps what settings can achieve. Say so.
    gate = cfg.rules["meta_rules"]["yarn_gate"]
    yq = record.yarn_quality
    if yq is not None and (
        (yq.rkm is not None and yq.rkm < gate["rkm_min"])
        or (yq.csp is not None and yq.csp < gate["csp_min"])
    ):
        suggestions.append(
            Suggestion(
                rule_id=gate["rule_id"],
                action=gate["action"].strip(),
                reasoning=gate["reasoning"].strip(),
                confidence=Confidence(gate["confidence"]),
                expected_effect=_direction_only("caps achievable improvement"),
            )
        )

    # HYGIENE: always worth running when breakage is elevated.
    if cmpx_high:
        rule = cfg.rules["meta_rules"]["hygiene"]
        suggestions.append(
            Suggestion(
                rule_id=rule["rule_id"],
                action=rule["action"].strip(),
                reasoning=rule["reasoning"].strip(),
                confidence=Confidence(rule["confidence"]),
                expected_effect=_direction_only("CMPX down"),
            )
        )

    suggestions.sort(key=lambda s: _CONFIDENCE_ORDER[s.confidence])
    return AdviceReport(
        loom_id=record.loom_id,
        profile=profile_name or "none",
        rules_version=cfg.version,
        notes=notes,
        suggestions=suggestions,
    )
