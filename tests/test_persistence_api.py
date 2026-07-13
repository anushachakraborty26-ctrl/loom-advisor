"""Persistence + API tests: the full path a real deployment takes —
register loom, ingest article, assign, record settings and daily status,
then ask for advice."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from loom_advisor.api.main import create_app

LOOM_47 = {"loom_id": "47", "machine_type": "dobby"}
ARTICLE = {
    "article_id": "TERRY-12OE",
    "construction": {"weft_count_ne": 12, "weft_spin": "oe", "weft_material": "cotton"},
}
SETTINGS = {
    "main_pressure": 3.6,
    "tandem_pressure": 3.2,
    "sub_pressure": 5.5,
    "main_nozzle_height_mm": 139,
    "shed_crossing_deg": 302,
    "speed_rpm": 413,
}
STATUS = {
    "report_date": "2025-06-13",
    "weft_cmpx": 88,
    "weft_breaks": 235,
    "efficiency_pct": 50,
}


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_url=f"sqlite:///{tmp_path}/test.db")
    return TestClient(app)


@pytest.fixture()
def seeded(client):
    assert client.post("/looms", json=LOOM_47).status_code == 201
    assert client.post("/articles", json=ARTICLE).status_code == 201
    assert (
        client.post(
            "/looms/47/assignments",
            json={"article_id": "TERRY-12OE", "start_date": "2025-06-01"},
        ).status_code
        == 201
    )
    assert client.put("/looms/47/settings", json=SETTINGS).status_code == 200
    assert client.post("/looms/47/status", json=STATUS).status_code == 201
    return client


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["rules_version"]


def test_end_to_end_advice(seeded):
    report = seeded.get("/looms/47/suggestions").json()
    assert report["loom_id"] == "47"
    assert report["profile"] == "coarse_oe_cotton"
    ids = [s["rule_id"] for s in report["suggestions"]]
    assert "BAND-MAIN-HIGH" in ids
    assert "REBALANCE" in ids
    assert "TIMING-DELAY" in ids


def test_duplicate_status_rejected(seeded):
    response = seeded.post("/looms/47/status", json=STATUS)
    assert response.status_code == 409


def test_unknown_loom_404(client):
    assert client.get("/looms/99/suggestions").status_code == 404
    assert client.post("/looms/99/status", json=STATUS).status_code == 404


def test_history_ordered(seeded):
    earlier = dict(STATUS, report_date="2025-06-12", weft_cmpx=90)
    assert seeded.post("/looms/47/status", json=earlier).status_code == 201
    history = seeded.get("/looms/47/history").json()
    dates = [row["report_date"] for row in history]
    assert dates == sorted(dates)


def test_article_change_segments_history(seeded):
    """After a new assignment, advice must only see the new article's
    status rows — CMPX across articles is not comparable."""
    new_article = {
        "article_id": "TERRY-14OE",
        "construction": {"weft_count_ne": 14, "weft_spin": "oe", "weft_material": "cotton"},
    }
    assert seeded.post("/articles", json=new_article).status_code == 201
    assert (
        seeded.post(
            "/looms/47/assignments",
            json={"article_id": "TERRY-14OE", "start_date": "2025-07-01"},
        ).status_code
        == 201
    )
    report = seeded.get("/looms/47/suggestions").json()
    # the June status rows belong to the previous article
    assert not any("2025-06" in note for note in report["notes"])
    assert any("No status reports" in note for note in report["notes"])


def test_settings_are_event_sourced(seeded):
    """A new snapshot must not erase the old one; advice uses the latest."""
    corrected = dict(SETTINGS, main_pressure=3.4, sub_pressure=5.9, shed_crossing_deg=312)
    assert seeded.put("/looms/47/settings", json=corrected).status_code == 200
    report = seeded.get("/looms/47/suggestions").json()
    ids = [s["rule_id"] for s in report["suggestions"]]
    assert "BAND-MAIN-HIGH" not in ids  # corrected main no longer flagged
