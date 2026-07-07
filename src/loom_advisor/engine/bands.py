"""Band checks: is a setting outside its known-good range for this yarn?

Pure functions — no I/O, no state. Same input, same output, forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..schema import Construction, Settings


@dataclass(frozen=True)
class Violation:
    setting: str
    value: float
    low: float
    high: float
    unit: str
    direction: str  # "above" | "below"


def select_profile(
    construction: Construction, profiles: dict[str, Any]
) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    """Match a construction to a yarn profile by material, spin and count range."""
    for name, prof in profiles.items():
        match = prof.get("match", {})
        want_material = match.get("weft_material")
        if want_material is not None:
            have = (construction.weft_material or "").lower()
            if have != str(want_material).lower():
                continue
        want_spin = match.get("weft_spin")
        if want_spin is not None:
            have_spin = construction.weft_spin.value if construction.weft_spin else None
            if have_spin != str(want_spin).lower():
                continue
        count_range = match.get("weft_count_ne")
        if count_range is not None:
            count = construction.weft_count_ne
            if count is None or not (count_range[0] <= count <= count_range[1]):
                continue
        return name, prof
    return None, None


def check_bands(settings: Settings, bands: dict[str, Any]) -> list[Violation]:
    """Compare each configured band against the same-named Settings field.

    Settings the loom record does not report (None) are skipped — the engine
    only reasons about what it can see.
    """
    violations: list[Violation] = []
    for setting_name, band in bands.items():
        value = getattr(settings, setting_name, None)
        if value is None:
            continue
        low, high = band["min"], band["max"]
        if value < low:
            direction = "below"
        elif value > high:
            direction = "above"
        else:
            continue
        violations.append(
            Violation(
                setting=setting_name,
                value=value,
                low=low,
                high=high,
                unit=band.get("unit", ""),
                direction=direction,
            )
        )
    return violations
