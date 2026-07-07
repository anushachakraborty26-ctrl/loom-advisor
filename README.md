# Loom Advisor

An air-jet loom efficiency advisor for terry-towel weaving. It reads each
loom's order spec (design sheet) and daily CMPX/breakage report, diagnoses
*why* the weft is breaking, and suggests setting corrections — each with the
rule that produced it and an evidence-based expected effect.

Built by digitising a real four-loom optimisation study (Alok Industries
air-jet weaving shed, June 2025) in which manual tuning of nozzle pressures
and shed timing produced:

| Loom | Machine | Weft | Efficiency | Filling CMPX | Breaks/day |
|------|---------|------|-----------|--------------|------------|
| 12 | Jacquard | 12s OE | 61.4 → 66.2% | 51.3 → 26.2 | 202 → 95 |
| 03 | Jacquard | 14s OE | 57 → 62% | 97.3 → 42.8 | 314 → 164 |
| 47 | Dobby | 12s OE | 50 → **82%** | 88 → 34 | 235 → 162 |
| 17 | Dobby | 14s OE | 57 → 68% | 60 → 30 | 184 → 121 |

The engine's golden tests require it to reproduce those manual fixes from
each loom's before-state.

## How it works

```
ingestion (many doors)          one store              one brain          clients
Excel watcher ──┐
upload form  ───┼── validation ── articles/assignments ── rules engine ── shed app
photo + VLM  ───┘   (pydantic)     status_events (daily     (bands +       office app
                                   CMPX), settings_events    symptoms)
```

Design principles:

- **Schema first.** `schema.py` is the contract; every component speaks it.
- **Deterministic brain, AI at the edges.** The advisor is pure rules — no
  LLM decides a setting. Vision/LLM handles messy inputs (sheet photos) only.
- **Config is data.** Golden bands and diagnostic rules live in
  `config/*.yaml`, cited to their source, editable without touching code.
- **Event logs, not snapshots.** Daily status and every settings change are
  append-only rows, so any day's CMPX can be attributed to the knobs in
  force that day.
- **Honest predictions.** Expected effects are historical ranges with sample
  size (`+5 to +32 efficiency points, n=4`), never point estimates.
- **Everything traceable.** Every suggestion carries its `rule_id` and the
  hash of the config version that produced it; every status row can carry a
  `source_doc_id` back to the uploaded report it came from.

## Quickstart

```bash
uv sync --extra dev          # install
uv run pytest                # golden tests: engine must reproduce the study
uv run loom-advisor advise tests/golden/loom47.json   # advise on a loom
uv run uvicorn loom_advisor.api.main:app --reload     # run the API -> /docs
```

Seed a demo database with the four study looms:

```bash
uv run python scripts/seed_demo.py
```

## The diagnostic model

Two cooperating checks, straight from the study:

1. **Band check** — is a setting outside its known-good range for this yarn
   profile? (e.g. main nozzle 3.6 kg/cm² vs the 3.4–3.5 band for coarse OE
   cotton → over-driven launch → broken picks/bunching.)
2. **Symptom rules** — the reported break type points at its cause: broken
   pick → too much launch stress; bend/short → too little air or feeder
   fault; bunching → excess pressure or nozzle gauging; entanglement → shed
   geometry; selvedge stops → draw-in/leno.

Plus meta-rules that encode the study's judgment: **REBALANCE** (never just
weaken the launch — move energy to the relay chain), **TIMING** (small
lever), **HYGIENE** (cutter, nozzle and air-pipe checks), **YARN-GATE**
(weak yarn caps what settings can achieve).

## Repository layout

```
src/loom_advisor/
  schema.py        # the data contract (pydantic)
  config/          # bands.yaml + rules.yaml — the knowledge base
  engine/          # pure-function rules engine
  db/              # SQLAlchemy models + repository (event-sourced)
  api/             # FastAPI app
  ingestion/       # Excel design-sheet adapter (VLM adapter: roadmap)
  cli.py
tests/
  golden/          # the four study looms as fixtures = the test oracle
scripts/seed_demo.py
```

## Roadmap

- [x] Phase 0–1: schema, knowledge base, rules engine, golden tests
- [x] Phase 2: event-sourced persistence (SQLite; swap URL for Postgres)
- [x] Phase 3: REST API
- [x] Phase 4a: Excel design-sheet adapter
- [ ] Phase 4b: VLM sheet reader (photo → spec; camera loom-ID lookup)
- [ ] Phase 5: shed (camera) and office (loom no.) client views
- [ ] Phase 6: trend-drift alerts, automatic before/after attribution,
      self-tightening expected-effect ranges
