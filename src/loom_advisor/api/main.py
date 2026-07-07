"""REST API: one brain, many thin clients.

The shed camera app and the office dashboard both end up at
GET /looms/{id}/suggestions — they only differ in how they obtain the id.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .. import __version__
from ..db import repo
from ..db.models import Base
from ..engine import advise, load_config
from ..schema import AdviceReport, Article, MachineType, Settings, StatusEvent


class LoomIn(BaseModel):
    loom_id: str
    machine_type: MachineType | None = None


class AssignmentIn(BaseModel):
    article_id: str
    start_date: date


def create_app(db_url: str | None = None) -> FastAPI:
    url = db_url or os.environ.get("LOOM_DB_URL", "sqlite:///loom_advisor.db")
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    cfg = load_config()

    app = FastAPI(
        title="Loom Advisor",
        version=__version__,
        description="Air-jet loom efficiency advisor: diagnoses weft-breakage causes "
        "and suggests setting corrections with evidence-based expected effects.",
    )

    def get_session() -> Iterator[Session]:
        with SessionLocal() as session:
            yield session

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "rules_version": cfg.version}

    @app.post("/looms", status_code=201)
    def create_loom(body: LoomIn, session: Session = Depends(get_session)) -> dict:
        repo.create_loom(
            session, body.loom_id, body.machine_type.value if body.machine_type else None
        )
        return {"loom_id": body.loom_id}

    @app.post("/articles", status_code=201)
    def upsert_article(body: Article, session: Session = Depends(get_session)) -> dict:
        repo.upsert_article(session, body)
        return {"article_id": body.article_id}

    @app.post("/looms/{loom_id}/assignments", status_code=201)
    def assign_article(
        loom_id: str, body: AssignmentIn, session: Session = Depends(get_session)
    ) -> dict:
        try:
            repo.assign_article(session, loom_id, body.article_id, body.start_date)
        except repo.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"loom_id": loom_id, "article_id": body.article_id}

    @app.put("/looms/{loom_id}/settings")
    def set_settings(
        loom_id: str, body: Settings, session: Session = Depends(get_session)
    ) -> dict:
        try:
            repo.set_settings(session, loom_id, body)
        except repo.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"loom_id": loom_id, "recorded": True}

    @app.post("/looms/{loom_id}/status", status_code=201)
    def add_status(
        loom_id: str, body: StatusEvent, session: Session = Depends(get_session)
    ) -> dict:
        try:
            repo.add_status(session, loom_id, body)
        except repo.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except repo.DuplicateStatusError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"loom_id": loom_id, "report_date": body.report_date.isoformat()}

    @app.get("/looms/{loom_id}/history")
    def history(loom_id: str, session: Session = Depends(get_session)) -> list[StatusEvent]:
        try:
            return repo.get_history(session, loom_id)
        except repo.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/looms/{loom_id}/suggestions")
    def suggestions(loom_id: str, session: Session = Depends(get_session)) -> AdviceReport:
        try:
            record = repo.get_loom_record(session, loom_id)
        except repo.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return advise(record, cfg)

    return app


app = create_app()
