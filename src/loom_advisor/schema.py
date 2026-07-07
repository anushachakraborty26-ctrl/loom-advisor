"""The data contract.

Every component — Excel adapter, VLM reader, database, rules engine, API —
speaks these models and nothing else. If a value gets past validation here,
the rest of the system can trust it.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field


class MachineType(StrEnum):
    dobby = "dobby"
    jacquard = "jacquard"


class WeftSpin(StrEnum):
    oe = "oe"  # open-end / rotor spun
    ring = "ring"
    filament = "filament"


class BreakType(StrEnum):
    broken = "broken"
    bend = "bend"
    short = "short"
    bunch = "bunch"
    entanglement = "entanglement"
    selvedge = "selvedge"


class Construction(BaseModel):
    """Fabric construction for one article (order spec). Owned by the order,
    not the loom — a loom weaves different articles over time."""

    warp_count: str | None = None
    weft_count_ne: float | None = Field(None, description="English cotton count (higher = finer)")
    weft_denier: float | None = Field(None, description="For filament yarns (higher = coarser)")
    weft_spin: WeftSpin | None = None
    weft_material: str | None = None
    epi: int | None = Field(None, description="Ends per inch")
    ppi: int | None = Field(None, description="Picks per inch")
    reed_count: str | None = None
    pile_ratio: str | None = None
    gsm: float | None = None
    width_cm: float | None = None
    selvedge_type: str | None = None


class Article(BaseModel):
    article_id: str
    construction: Construction = Construction()


class ShedHeights(BaseModel):
    selvedge_mm: float | None = None
    pile_mm: float | None = None
    ground_mm: float | None = None


class Settings(BaseModel):
    """Current knob positions on one loom. Field names double as the keys
    used in config/bands.yaml — the band checker looks settings up by name."""

    main_pressure: float | None = Field(None, description="kg/cm²")
    tandem_pressure: float | None = Field(None, description="kg/cm²")
    sub_pressure: float | None = Field(None, description="kg/cm²")
    main_nozzle_height_mm: float | None = None
    sub_nozzle_spacing_mm: float | None = None
    sub_to_reed_gap_mm: float | None = None
    shed_crossing_deg: float | None = None
    shed_heights: ShedHeights | None = None
    backrest_roller: str | None = None
    speed_rpm: float | None = None


class StatusEvent(BaseModel):
    """One row of the daily CMPX / breakage report for one loom."""

    report_date: date
    filling_cmpx: float | None = None
    breakages_per_day: int | None = None
    efficiency_pct: float | None = None
    break_type: BreakType | None = Field(
        None, description="Dominant break type observed, if the operator noted one"
    )


class YarnQuality(BaseModel):
    rkm: float | None = None
    csp: float | None = None
    hairiness_index: float | None = None


class LoomRecord(BaseModel):
    """Everything the advisor engine needs to reason about one loom:
    who it is, what it is weaving, how its knobs are set, and how it has
    been behaving."""

    loom_id: str
    machine_type: MachineType | None = None
    article_id: str | None = None
    construction: Construction = Construction()
    settings: Settings = Settings()
    status_log: list[StatusEvent] = []
    yarn_quality: YarnQuality | None = None

    @property
    def latest_status(self) -> StatusEvent | None:
        if not self.status_log:
            return None
        return max(self.status_log, key=lambda e: e.report_date)


class Confidence(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class ExpectedEffect(BaseModel):
    """The honest prediction: a direction always, a range only when history
    backs it, and the sample size stated so the claim can never outrun the
    data behind it."""

    direction: str
    historical_range: str | None = None
    n_cases: int | None = None
    source: str | None = None


class Suggestion(BaseModel):
    rule_id: str
    setting: str | None = None
    action: str
    reasoning: str
    confidence: Confidence
    expected_effect: ExpectedEffect | None = None


class AdviceReport(BaseModel):
    loom_id: str
    profile: str
    rules_version: str = Field(description="Hash of the config files that produced this advice")
    notes: list[str] = []
    suggestions: list[Suggestion] = []
