"""Command-line interface.

    loom-advisor advise tests/golden/loom47.json
    loom-advisor advise tests/golden/loom47.json --json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .engine import advise, load_config
from .schema import LoomRecord


def _print_report(report) -> None:
    print(f"Loom {report.loom_id} | profile: {report.profile} | rules v{report.rules_version}")
    for note in report.notes:
        print(f"  {note}")
    print()
    if not report.suggestions:
        print("No suggestions — all monitored settings in band and no elevated breakage.")
        return
    for i, s in enumerate(report.suggestions, 1):
        print(f"{i}. [{s.confidence.value.upper()}] {s.rule_id}")
        print(f"   do:  {s.action}")
        print(f"   why: {s.reasoning}")
        if s.expected_effect:
            eff = s.expected_effect
            line = f"   expect: {eff.direction}"
            if eff.historical_range:
                line += f" | plant history: {eff.historical_range} (n={eff.n_cases})"
            print(line)
            if eff.projected_weft_cmpx:
                print(f"   this loom, weft CMPX: {eff.projected_weft_cmpx}")
            if eff.projected_efficiency:
                print(f"   this loom, efficiency: {eff.projected_efficiency}")
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loom-advisor")
    sub = parser.add_subparsers(dest="command", required=True)
    p_advise = sub.add_parser("advise", help="Advise on a loom record JSON file")
    p_advise.add_argument("record", type=Path)
    p_advise.add_argument("--json", action="store_true", help="Emit the raw AdviceReport JSON")
    args = parser.parse_args(argv)

    record = LoomRecord.model_validate_json(args.record.read_text())
    report = advise(record, load_config())
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
