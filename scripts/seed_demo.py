"""Seed a demo database with the four looms of the June 2025 plant study
in their before-study state, then print the advisor's output for each.

    uv run python scripts/seed_demo.py
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from loom_advisor.api.main import create_app

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"
DB_PATH = ROOT / "loom_advisor.db"


def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    app = create_app(db_url=f"sqlite:///{DB_PATH}")
    client = TestClient(app)

    articles_seen: set[str] = set()
    for fixture in sorted(GOLDEN.glob("loom*.json")):
        record = json.loads(fixture.read_text())
        loom_id = record["loom_id"]
        article_id = record["article_id"]

        client.post(
            "/looms", json={"loom_id": loom_id, "machine_type": record["machine_type"]}
        )
        if article_id not in articles_seen:
            client.post(
                "/articles",
                json={"article_id": article_id, "construction": record["construction"]},
            )
            articles_seen.add(article_id)
        client.post(
            f"/looms/{loom_id}/assignments",
            json={"article_id": article_id, "start_date": "2025-06-01"},
        )
        client.put(f"/looms/{loom_id}/settings", json=record["settings"])
        for event in record["status_log"]:
            client.post(f"/looms/{loom_id}/status", json=event)

        report = client.get(f"/looms/{loom_id}/suggestions").json()
        ids = [s["rule_id"] for s in report["suggestions"]]
        print(f"Loom {loom_id:>2} ({record['machine_type']:>8}): {', '.join(ids)}")

    print(f"\nSeeded {DB_PATH.name}. Run the API against it:")
    print("  LOOM_DB_URL=sqlite:///loom_advisor.db uv run uvicorn loom_advisor.api.main:app")


if __name__ == "__main__":
    main()
