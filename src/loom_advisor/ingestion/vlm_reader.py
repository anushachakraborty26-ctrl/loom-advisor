"""VLM document readers: sheet/report photo -> validated data.

The AI stays at the edges: these functions turn messy photos into the same
validated pydantic contract the Excel adapter feeds. Nothing downstream can
tell which door the data came through, and no LLM ever decides a setting.

Two self-defence layers, both born from hand-extraction of the real
documents:
- extraction prompts forbid guessing (illegible -> null), and
- every CMPX report row is audited against the report's own arithmetic
  (CMPX = breaks / kilopicks x 100); rows that fail are flagged for a human
  instead of entering the database.
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..schema import Article, Construction, WeftSpin

DEFAULT_MODEL = os.environ.get("LOOM_VLM_MODEL", "claude-opus-4-8")

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


# --- Extraction contracts (what the VLM must return) -------------------------


class DesignSheetExtraction(BaseModel):
    """Fields read off one loom programme/design sheet photo."""

    loom_no: str | None = None
    design_no: str | None = None
    material_code: str | None = None
    party_name: str | None = None
    quality: str | None = None
    body_weft: str | None = None
    border_weft: str | None = None
    ground_warp: str | None = None
    pile_warp: str | None = None
    pile_ratio: float | None = Field(
        None, description="Handwritten correction overrides the printed value"
    )
    fpl_height_mm: float | None = None
    grey_picks_per_cm: float | None = None
    reed_count_per_inch: float | None = None
    finish_gsm: float | None = None
    finished_size: str | None = None
    finish_wt_per_pc_gms: float | None = None
    total_pcs: int | None = None
    notes: str | None = Field(
        None, description="Anything ambiguous or illegible worth flagging to a human"
    )


class CmpxRow(BaseModel):
    """One loom's row on the daily Break CMPX report."""

    loom_no: str
    loom_type: str | None = None
    efficiency_pct: float | None = None
    rpm: float | None = None
    pile_breaks: int | None = None
    pile_cmpx: float | None = None
    ground_breaks: int | None = None
    ground_cmpx: float | None = None
    weft_breaks: int | None = None
    weft_cmpx: float | None = None
    breaks_per_hour: float | None = None
    total_kilopicks: float | None = None


class CmpxReportExtraction(BaseModel):
    """One page of the daily Break CMPX report."""

    report_date: str | None = Field(None, description="ISO date from the title, if visible")
    rows: list[CmpxRow] = []
    avg_row: CmpxRow | None = None


# --- Wire models for structured outputs ---------------------------------------
#
# The structured-outputs schema compiler allows at most 16 union-typed
# (nullable) parameters per schema; the extraction contracts above exceed
# that. So the API speaks all-required-strings wire models (zero unions;
# "" means illegible/absent) and we convert to the typed contract here.


class _DesignSheetWire(BaseModel):
    loom_no: str
    design_no: str
    material_code: str
    party_name: str
    quality: str
    body_weft: str
    border_weft: str
    ground_warp: str
    pile_warp: str
    pile_ratio: str
    fpl_height_mm: str
    grey_picks_per_cm: str
    reed_count_per_inch: str
    finish_gsm: str
    finished_size: str
    finish_wt_per_pc_gms: str
    total_pcs: str
    notes: str


class _CmpxRowWire(BaseModel):
    loom_no: str
    loom_type: str
    efficiency_pct: str
    rpm: str
    pile_breaks: str
    pile_cmpx: str
    ground_breaks: str
    ground_cmpx: str
    weft_breaks: str
    weft_cmpx: str
    breaks_per_hour: str
    total_kilopicks: str


class _CmpxReportWire(BaseModel):
    report_date: str
    rows: list[_CmpxRowWire]


def _text(value: str) -> str | None:
    value = value.strip()
    return value or None


def _number(value: str) -> float | None:
    value = value.strip().replace(",", "").replace("%", "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _count(value: str) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _sheet_from_wire(wire: _DesignSheetWire) -> DesignSheetExtraction:
    return DesignSheetExtraction(
        loom_no=_text(wire.loom_no),
        design_no=_text(wire.design_no),
        material_code=_text(wire.material_code),
        party_name=_text(wire.party_name),
        quality=_text(wire.quality),
        body_weft=_text(wire.body_weft),
        border_weft=_text(wire.border_weft),
        ground_warp=_text(wire.ground_warp),
        pile_warp=_text(wire.pile_warp),
        pile_ratio=_number(wire.pile_ratio),
        fpl_height_mm=_number(wire.fpl_height_mm),
        grey_picks_per_cm=_number(wire.grey_picks_per_cm),
        reed_count_per_inch=_number(wire.reed_count_per_inch),
        finish_gsm=_number(wire.finish_gsm),
        finished_size=_text(wire.finished_size),
        finish_wt_per_pc_gms=_number(wire.finish_wt_per_pc_gms),
        total_pcs=_count(wire.total_pcs),
        notes=_text(wire.notes),
    )


def _row_from_wire(wire: _CmpxRowWire) -> CmpxRow:
    return CmpxRow(
        loom_no=wire.loom_no.strip(),
        loom_type=_text(wire.loom_type),
        efficiency_pct=_number(wire.efficiency_pct),
        rpm=_number(wire.rpm),
        pile_breaks=_count(wire.pile_breaks),
        pile_cmpx=_number(wire.pile_cmpx),
        ground_breaks=_count(wire.ground_breaks),
        ground_cmpx=_number(wire.ground_cmpx),
        weft_breaks=_count(wire.weft_breaks),
        weft_cmpx=_number(wire.weft_cmpx),
        breaks_per_hour=_number(wire.breaks_per_hour),
        total_kilopicks=_number(wire.total_kilopicks),
    )


def _report_from_wire(wire: _CmpxReportWire) -> CmpxReportExtraction:
    rows = [_row_from_wire(r) for r in wire.rows]
    avg = next((r for r in rows if r.loom_no.upper() == "AVG"), None)
    return CmpxReportExtraction(
        report_date=_text(wire.report_date),
        rows=[r for r in rows if r.loom_no.upper() != "AVG"],
        avg_row=avg,
    )


# --- The identity check: rows must prove themselves ---------------------------

Consistency = Literal["pass", "fail", "insufficient"]


def check_row_consistency(row: CmpxRow, tolerance: float = 0.15) -> Consistency:
    """Audit a report row against the report's own arithmetic.

    CMPX is breaks per 100,000 picks and TOTAL KP is kilopicks, so each
    category must satisfy: cmpx ~= breaks / total_kilopicks * 100. A row can
    only pass on all three categories if every cell was read correctly AND
    belongs to the same loom — which is exactly what photo skew and shadows
    break. Verified against the 24-06-2026 report during hand-extraction.
    """
    if not row.total_kilopicks:
        return "insufficient"
    checked = 0
    for category in ("pile", "ground", "weft"):
        breaks = getattr(row, f"{category}_breaks")
        cmpx = getattr(row, f"{category}_cmpx")
        if breaks is None or cmpx is None:
            continue
        checked += 1
        expected = breaks / row.total_kilopicks * 100.0
        if expected == 0.0:
            if cmpx != 0.0:
                return "fail"
            continue
        if abs(cmpx - expected) / expected > tolerance:
            return "fail"
    return "pass" if checked else "insufficient"


# --- Translating extractions into the core contract ---------------------------


def parse_weft_spec(spec: str | None) -> tuple[float | None, WeftSpin | None, str | None]:
    """'14s OE RFD WHITE' -> (14.0, oe, 'cotton'). Never guesses: unknown
    spin or material stays None so the engine can refuse profiles honestly."""
    if not spec:
        return None, None, None
    s = spec.lower()
    count_match = re.search(r"(\d+(?:\.\d+)?)\s*s", s)
    count = float(count_match.group(1)) if count_match else None
    spin = None
    if re.search(r"\d\s*s\s*oe\b|\boe\b|open[ -]?end|rotor", s):
        spin = WeftSpin.oe
    elif "filament" in s:
        spin = WeftSpin.filament
    material = None
    if "cot" in s or spin == WeftSpin.oe or re.search(r"\bk[cw]\b", s):
        material = "cotton"
    elif "poly" in s:
        material = "polyester"
    return count, spin, material


def design_sheet_to_article(extraction: DesignSheetExtraction) -> Article:
    """Map a sheet extraction onto the same Article contract the Excel
    adapter produces."""
    count, spin, material = parse_weft_spec(extraction.body_weft)
    construction = Construction(
        weft_count_ne=count,
        weft_spin=spin,
        weft_material=material,
        warp_count=extraction.ground_warp,
        reed_count=(
            f"{extraction.reed_count_per_inch:g}/inch"
            if extraction.reed_count_per_inch is not None
            else None
        ),
        pile_ratio=(
            f"{extraction.pile_ratio:g}" if extraction.pile_ratio is not None else None
        ),
        gsm=extraction.finish_gsm,
    )
    article_id = extraction.design_no or extraction.quality
    if article_id:
        article_id = re.sub(r"\.xls[x]?$", "", article_id.strip(), flags=re.IGNORECASE)
    else:
        article_id = f"LOOM-{extraction.loom_no or 'UNKNOWN'}"
    return Article(article_id=article_id, construction=construction)


# --- The VLM calls -------------------------------------------------------------

DESIGN_SHEET_PROMPT = """\
This is a photo of a loom programme/design sheet posted in a terry-towel
weaving mill (Alok Industries designing-department format).

Extract the requested fields. Rules:
- Handwritten corrections OVERRIDE printed values. A struck-through printed
  number next to a handwritten one means the handwritten value is current
  (this applies especially to PILE RATIO and the reed/picks values near the
  top of the sheet).
- If a field is illegible, obscured (string/thread across the sheet, glare),
  or absent, return the empty string "". NEVER guess. Mention what was
  illegible in `notes` (or return "" for notes if nothing to flag).
- LOOM NO is printed large in the upper right area.
- BODY WEFT, BORDER WEFT, GROUND WARP, PILE WARP are labelled rows; copy
  their values verbatim including counts like '14s OE' or '2/20 CRD (1288)'.
- F.PL.HEIGHT is in millimetres. FINISH G.S.M is the fabric GSM.
- REED COUNT shows a per-inch value (often handwritten, e.g. 39.4).
- GREY PICKS / CM is picks per centimetre.
"""

CMPX_REPORT_PROMPT = """\
This is a photo of one page of a daily 'BREAK CMPX REPORT' table from a
weaving mill. Columns, left to right: LOOM NO, LOOM (machine type,
jacquard/dobby), EFFI (%), RPM, PILE BREAK, PILE CMPX, GRD BREAK, GRD CMPX,
WEFT BREAK, WEFT CMPX, BKG/HOURS, TOTAL KP.

Extract EVERY data row on the page. Rules:
- The print may be slightly skewed so value rows can appear offset from the
  loom-number column. Use this arithmetic to keep each row's cells together:
  each CMPX value satisfies  CMPX = BREAKS / TOTAL_KP * 100  for its
  category. If a candidate reading violates that badly, re-examine the
  alignment before writing the row.
- If a cell is illegible (shadow, fold, ink defect), return the empty
  string "" for that cell. NEVER guess digits.
- loom_type is lowercase 'jacquard' or 'dobby' ("" if unreadable).
- The report date appears in the page title (DD.MM.YYYY); return ISO
  YYYY-MM-DD in report_date, or "" if not on this page.
- If the page ends with an AVG/average row, include it as the last row with
  loom_no 'AVG'.
"""


def _image_block(path: Path) -> dict[str, Any]:
    media_type = _MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise ValueError(f"unsupported image type: {path.name}")
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def _default_client() -> Any:
    import anthropic

    return anthropic.Anthropic()


def _extract(
    path: Path,
    prompt: str,
    output_format: type[BaseModel],
    client: Any,
    model: str,
    max_tokens: int,
) -> Any:
    response = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {
                "role": "user",
                "content": [_image_block(path), {"type": "text", "text": prompt}],
            }
        ],
        output_format=output_format,
    )
    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        raise ValueError(f"extraction returned no parsed output for {path.name}")
    return parsed


def read_design_sheet(
    path: Path | str, client: Any = None, model: str | None = None
) -> DesignSheetExtraction:
    """Photo of a design sheet -> DesignSheetExtraction."""
    wire = _extract(
        Path(path),
        DESIGN_SHEET_PROMPT,
        _DesignSheetWire,
        client or _default_client(),
        model or DEFAULT_MODEL,
        max_tokens=4096,
    )
    return _sheet_from_wire(wire)


def read_cmpx_report(
    path: Path | str, client: Any = None, model: str | None = None
) -> CmpxReportExtraction:
    """Photo of one Break CMPX report page -> CmpxReportExtraction.

    Run check_row_consistency on every returned row before trusting it;
    only 'pass' rows should enter the database unreviewed.
    """
    wire = _extract(
        Path(path),
        CMPX_REPORT_PROMPT,
        _CmpxReportWire,
        client or _default_client(),
        model or DEFAULT_MODEL,
        max_tokens=16000,
    )
    return _report_from_wire(wire)
