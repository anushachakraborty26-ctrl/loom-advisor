"""Loom Advisor: air-jet loom efficiency advisor.

Digitises the diagnostic method of a four-loom weft-breakage study
(Alok Industries terry weaving, June 2025): read the loom's spec and daily
CMPX report, find out-of-band settings and symptomatic break patterns, and
suggest corrections with evidence-based expected effects.
"""

from .engine import advise, load_config
from .schema import AdviceReport, LoomRecord

__all__ = ["AdviceReport", "LoomRecord", "advise", "load_config"]
__version__ = "0.1.0"
