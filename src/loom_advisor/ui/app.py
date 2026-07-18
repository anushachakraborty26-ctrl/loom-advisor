"""Loom Advisor UI — one brain, two doors.

Office: shed overview (KPIs, movers, worst offenders) and loom lookup with
trends and advice. Shed: point a phone camera at the design sheet on the
loom; the VLM reads the loom number and the same advice appears.

    uv run streamlit run src/loom_advisor/ui/app.py
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from loom_advisor.db import repo
from loom_advisor.engine import advise, load_config
from loom_advisor.schema import AdviceReport

ROOT = Path(__file__).resolve().parents[3]
DB_URL = os.environ.get("LOOM_DB_URL", f"sqlite:///{ROOT / 'loom_advisor.db'}")

CONFIDENCE_BADGE = {"high": "🔴 HIGH", "medium": "🟠 MEDIUM", "low": "🔵 LOW"}

st.set_page_config(page_title="Loom Advisor", page_icon="🧵", layout="wide")


@st.cache_resource
def get_sessionmaker():
    engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine)


@st.cache_resource
def get_config():
    return load_config()


@st.cache_data(ttl=60)
def load_shed() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(status history dataframe, looms dataframe)."""
    with get_sessionmaker()() as session:
        status = pd.DataFrame(
            [
                {
                    "loom": loom_id,
                    "date": event.report_date,
                    "efficiency": event.efficiency_pct,
                    "weft_cmpx": event.weft_cmpx,
                    "pile_cmpx": event.pile_cmpx,
                    "ground_cmpx": event.ground_cmpx,
                    "weft_breaks": event.weft_breaks,
                }
                for loom_id, event in repo.list_shed_status(session)
            ]
        )
        looms = pd.DataFrame(
            [
                {"loom": loom_id, "machine": machine or "?"}
                for loom_id, machine in repo.list_looms(session)
            ]
        )
    return status, looms


def loom_advice(loom_id: str) -> AdviceReport:
    with get_sessionmaker()() as session:
        record = repo.get_loom_record(session, loom_id)
    return advise(record, get_config())


def loom_record(loom_id: str):
    with get_sessionmaker()() as session:
        return repo.get_loom_record(session, loom_id)


def render_advice(report: AdviceReport) -> None:
    for note in report.notes:
        (st.warning if "ALERT" in note or "above the plant target" in note else st.caption)(note)
    if not report.suggestions:
        st.success("No suggestions — monitored settings in band and no elevated breakage.")
    for s in report.suggestions:
        with st.container(border=True):
            st.markdown(f"**{CONFIDENCE_BADGE[s.confidence.value]}** · `{s.rule_id}`")
            st.markdown(f"**Do:** {s.action}")
            st.markdown(f"**Why:** {s.reasoning}")
            if s.expected_effect:
                eff = s.expected_effect
                line = f"Expect: {eff.direction}"
                if eff.historical_range:
                    line += f" — {eff.historical_range} (n={eff.n_cases}; {eff.source})"
                st.caption(line)
    st.caption(f"profile: {report.profile} · rules v{report.rules_version}")


def render_loom_detail(loom_id: str, status: pd.DataFrame) -> None:
    record = loom_record(loom_id)
    construction = record.construction
    st.subheader(f"Loom {loom_id}")
    bits = [record.machine_type.value if record.machine_type else "machine type unknown"]
    if record.article_id:
        bits.append(f"article {record.article_id}")
    if construction.weft_count_ne:
        spin = f" {construction.weft_spin.value.upper()}" if construction.weft_spin else ""
        bits.append(f"weft {construction.weft_count_ne:g}s{spin}")
    if construction.gsm:
        bits.append(f"GSM {construction.gsm:g}")
    if construction.pile_ratio:
        bits.append(f"pile ratio {construction.pile_ratio}")
    st.caption(" · ".join(bits))

    history = status[status["loom"] == loom_id].sort_values("date")
    if history.empty:
        st.info("No verified status reports for this loom yet.")
    else:
        latest = history.iloc[-1]
        previous = history.iloc[-2] if len(history) > 1 else None
        columns = st.columns(4)
        thresholds = get_config().bands["thresholds"]
        for column, (label, field, target) in zip(
            columns,
            [
                ("Weft CMPX", "weft_cmpx", thresholds["weft_cmpx_target"]),
                ("Pile CMPX", "pile_cmpx", thresholds["pile_cmpx_target"]),
                ("Ground CMPX", "ground_cmpx", thresholds["ground_cmpx_target"]),
                ("Efficiency %", "efficiency", None),
            ],
            strict=True,
        ):
            value = latest[field]
            delta = None
            if previous is not None and pd.notna(previous[field]) and pd.notna(value):
                delta = round(float(value) - float(previous[field]), 2)
            help_text = f"plant target ≤ {target:g}" if target else None
            column.metric(
                label,
                "—" if pd.isna(value) else f"{value:g}",
                delta=delta,
                delta_color=("normal" if field == "efficiency" else "inverse"),
                help=help_text,
            )
        if len(history) > 1:
            chart = history.set_index("date")[["weft_cmpx", "pile_cmpx", "ground_cmpx"]]
            st.line_chart(chart, height=220)

    st.markdown("#### Advice")
    render_advice(loom_advice(loom_id))


def page_overview(status: pd.DataFrame, looms: pd.DataFrame) -> None:
    st.title("🧵 Shed overview")
    if status.empty:
        st.info("No status data ingested yet — run scripts/ingest_extractions.py")
        return
    dates = sorted(status["date"].unique())
    latest_date = dates[-1]
    latest = status[status["date"] == latest_date]
    thresholds = get_config().bands["thresholds"]

    columns = st.columns(4)
    previous = status[status["date"] == dates[-2]] if len(dates) > 1 else None
    for column, (label, field) in zip(
        columns,
        [
            ("Plant weft CMPX", "weft_cmpx"),
            ("Plant pile CMPX", "pile_cmpx"),
            ("Plant ground CMPX", "ground_cmpx"),
            ("Plant efficiency %", "efficiency"),
        ],
        strict=True,
    ):
        value = latest[field].mean()
        delta = None
        if previous is not None:
            delta = round(float(value - previous[field].mean()), 2)
        column.metric(
            label,
            f"{value:.2f}",
            delta=delta,
            delta_color=("normal" if field == "efficiency" else "inverse"),
        )
    over_target = latest[latest["weft_cmpx"] >= thresholds["weft_cmpx_target"]]
    st.caption(
        f"Report day {latest_date} · {len(latest)} looms verified · "
        f"{len(over_target)} above the weft target of {thresholds['weft_cmpx_target']:g} "
        f"(plant means over verified looms)"
    )

    left, right = st.columns(2)
    with left:
        st.markdown(f"#### Worst weft CMPX — {latest_date}")
        worst = (
            latest.merge(looms, on="loom", how="left")
            .sort_values("weft_cmpx", ascending=False)
            .head(10)[["loom", "machine", "weft_cmpx", "efficiency", "weft_breaks"]]
        )
        st.dataframe(worst, hide_index=True, width="stretch")
    with right:
        if len(dates) > 1:
            st.markdown(f"#### Movers, {dates[-2]} → {latest_date}")
            pivot = status[status["date"].isin(dates[-2:])].pivot_table(
                index="loom", columns="date", values="weft_cmpx"
            )
            pivot = pivot.dropna()
            pivot["Δ weft CMPX"] = pivot[latest_date] - pivot[dates[-2]]
            movers = pivot.sort_values("Δ weft CMPX", ascending=False)
            movers.columns = [str(c) for c in movers.columns]
            st.dataframe(
                pd.concat([movers.head(5), movers.tail(5)]).round(2),
                width="stretch",
            )
        else:
            st.info("One report day so far — movers appear after the next report is ingested.")


def page_lookup(status: pd.DataFrame, looms: pd.DataFrame) -> None:
    st.title("🔎 Loom lookup")
    loom_ids = sorted(looms["loom"], key=int)
    default_index = 0
    query_loom = st.query_params.get("loom")
    if query_loom in loom_ids:
        default_index = loom_ids.index(query_loom)
    loom_id = st.selectbox("Loom number", loom_ids, index=default_index)
    if loom_id:
        st.query_params["loom"] = loom_id
        render_loom_detail(loom_id, status)


def page_shed_camera(status: pd.DataFrame) -> None:
    st.title("📷 Shed mode")
    st.markdown(
        "Point the camera at the **design sheet posted on the loom** "
        "(or upload a photo). The loom number is read off the sheet; "
        "everything else comes from the database."
    )
    image = st.camera_input("Scan the sheet")
    upload = st.file_uploader("…or upload a sheet photo", type=["jpg", "jpeg", "png", "webp"])
    photo = image or upload
    if photo is None:
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        st.error("ANTHROPIC_API_KEY is not set — camera lookup needs the vision model.")
        return
    from loom_advisor.ingestion import read_design_sheet

    with st.spinner("Reading the sheet…"):
        suffix = ".png" if photo.type == "image/png" else ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(photo.getvalue())
            temp_path = handle.name
        try:
            extraction = read_design_sheet(temp_path)
        finally:
            os.unlink(temp_path)
    loom_id = (extraction.loom_no or "").strip()
    if not loom_id:
        st.error("Could not read a loom number off this sheet — try a straighter shot.")
        if extraction.notes:
            st.caption(f"Reader notes: {extraction.notes}")
        return
    st.success(f"Sheet identifies **loom {loom_id}**")
    known = {loom for loom, _ in [(str(x), None) for x in status['loom'].unique()]}
    if loom_id not in known:
        st.warning("That loom has no verified data in the database yet — showing what exists.")
    try:
        render_loom_detail(loom_id, status)
    except repo.NotFoundError:
        st.error(f"Loom {loom_id} is not in the database.")


def main() -> None:
    status, looms = load_shed()
    page = st.sidebar.radio(
        "View",
        ["Shed overview", "Loom lookup", "Shed mode (camera)"],
        label_visibility="collapsed",
    )
    st.sidebar.caption(
        "Advice is deterministic and traceable: every suggestion carries the "
        "rule and config version that produced it. CMPX rows enter the "
        "database only after passing the identity check."
    )
    if page == "Shed overview":
        page_overview(status, looms)
    elif page == "Loom lookup":
        page_lookup(status, looms)
    else:
        page_shed_camera(status)


main()
