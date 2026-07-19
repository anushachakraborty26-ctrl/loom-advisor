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

from loom_advisor.alerts import overdue_expectations
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
    counts = status.groupby("loom").size() if not status.empty else pd.Series(dtype=int)

    def label(loom: str) -> str:
        days = int(counts.get(loom, 0))
        if days == 0:
            return f"{loom} — no verified data yet"
        return f"{loom} — {days} report day{'s' if days > 1 else ''}"

    default_index = 0
    query_loom = st.query_params.get("loom")
    if query_loom in loom_ids:
        default_index = loom_ids.index(query_loom)
    elif not status.empty:
        # Land on the shed's current worst weft offender, not on loom 1.
        latest = status[status["date"] == status["date"].max()]
        ranked = latest.sort_values("weft_cmpx", ascending=False)
        if not ranked.empty and ranked["loom"].iloc[0] in loom_ids:
            default_index = loom_ids.index(ranked["loom"].iloc[0])
    loom_id = st.selectbox("Loom number", loom_ids, index=default_index, format_func=label)
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


def _save_upload(upload, subdir: str) -> Path:
    target_dir = ROOT / "data" / "uploads" / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / upload.name
    target.write_bytes(upload.getvalue())
    return target


def page_data_entry() -> None:
    from datetime import date, timedelta

    from loom_advisor.ingestion.service import (
        ingest_design_excel,
        ingest_design_sheet_photo,
        ingest_report_upload,
    )

    st.title("📤 Data entry")
    st.caption(
        "Uploaders submit documents — never numbers. The machine verifies "
        "every row; anything it cannot prove goes to the Review queue for a "
        "named supervisor. Originals are stored with your name and timestamp."
    )
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    tab_report, tab_design = st.tabs(
        ["Weaving room — daily report", "Design studio — order sheet"]
    )

    with tab_report:
        report_date = st.date_input(
            "Report date", value=date.today() - timedelta(days=1), key="rep_date"
        )
        pages = st.file_uploader(
            "Report page photos (all pages)",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True,
            key="rep_pages",
        )
        uploader = st.text_input("Your name", key="rep_name")
        if st.button("Submit report", disabled=not (pages and uploader.strip())):
            if not has_key:
                st.error("ANTHROPIC_API_KEY is not set — photo extraction needs the vision model.")
            else:
                with st.spinner(f"Extracting and verifying {len(pages)} page(s)…"):
                    paths = [_save_upload(p, report_date.isoformat()) for p in pages]
                    with get_sessionmaker()() as session:
                        summary = ingest_report_upload(
                            session, paths, report_date, uploader.strip()
                        )
                load_shed.clear()
                st.success(
                    f"**{summary.verified} rows verified** and stored · "
                    f"{summary.queued_for_review} sent to review · "
                    f"{summary.duplicates} duplicates skipped (already on record)"
                )
                if summary.queued_for_review:
                    st.warning(
                        "Quarantined rows are waiting in the Review queue — "
                        "a supervisor should resolve them today."
                    )

    with tab_design:
        sheet = st.file_uploader(
            "Order sheet (studio Excel, or a photo of the printed sheet)",
            type=["xlsx", "jpg", "jpeg", "png", "webp"],
            key="design_file",
        )
        assign_date = st.date_input("Running on the loom from", value=date.today(), key="ds_date")
        uploader_d = st.text_input("Your name", key="ds_name")
        if st.button("Submit sheet", disabled=not (sheet and uploader_d.strip())):
            path = _save_upload(sheet, "design_sheets")
            with get_sessionmaker()() as session:
                if sheet.name.lower().endswith(".xlsx"):
                    summary = ingest_design_excel(session, path, uploader_d.strip())
                elif not has_key:
                    st.error(
                        "ANTHROPIC_API_KEY is not set — photo extraction needs the vision model."
                    )
                    summary = None
                else:
                    with st.spinner("Reading the sheet…"):
                        summary = ingest_design_sheet_photo(
                            session, path, uploader_d.strip(), assign_date
                        )
            if summary is not None:
                load_shed.clear()
                st.success("Sheet ingested.")
                for note in summary.notes:
                    st.caption(note)


def page_review_queue() -> None:
    st.title("✅ Review queue")
    with get_sessionmaker()() as session:
        items = repo.list_review_items(session)
    if not items:
        st.success("Queue is empty — every stored value is machine-verified or human-approved.")
        return
    st.caption(
        f"{len(items)} quarantined row(s). Nothing here is shown on dashboards. "
        "Check each against the original photo, correct if needed, then approve or reject."
    )
    reviewer = st.text_input("Reviewer name (recorded on every decision)")
    for item in items:
        title = f"{item.report_date} · loom {item.loom_id or '?'} — {item.reason}"
        with st.expander(title):
            if item.uploaded_by:
                st.caption(f"uploaded by {item.uploaded_by} · {item.submitted_at:%Y-%m-%d %H:%M}")
            if item.source_doc_path and Path(item.source_doc_path).exists():
                st.image(item.source_doc_path, caption="original document", width=420)
            edited = st.data_editor(
                pd.DataFrame([item.payload]),
                hide_index=True,
                key=f"edit_{item.id}",
                width="stretch",
            )
            col_a, col_b = st.columns(2)
            decided = None
            if col_a.button("Approve → store", key=f"ok_{item.id}", disabled=not reviewer.strip()):
                decided = True
            if col_b.button("Reject", key=f"no_{item.id}", disabled=not reviewer.strip()):
                decided = False
            if decided is not None:
                corrected = {
                    k: (None if pd.isna(v) else v) for k, v in edited.iloc[0].to_dict().items()
                }
                try:
                    with get_sessionmaker()() as session:
                        repo.resolve_review_item(
                            session,
                            item.id,
                            approve=decided,
                            reviewer=reviewer.strip(),
                            corrected=corrected if decided else None,
                        )
                    load_shed.clear()
                    st.rerun()
                except (repo.DuplicateStatusError, ValueError) as exc:
                    st.error(str(exc))


def main() -> None:
    status, looms = load_shed()
    with get_sessionmaker()() as session:
        pending = repo.count_review_items(session)
        overdue = overdue_expectations(session)
    review_label = f"Review queue ({pending})" if pending else "Review queue"
    page = st.sidebar.radio(
        "View",
        ["Shed overview", "Loom lookup", "Shed mode (camera)", "Data entry", review_label],
        label_visibility="collapsed",
    )
    st.sidebar.caption(
        "Advice is deterministic and traceable: every suggestion carries the "
        "rule and config version that produced it. CMPX rows enter the "
        "database only after passing the identity check."
    )
    for item in overdue:
        st.error(f"⏰ {item.headline()} — owners: "
                 + ", ".join(o.get("name", "?") for o in item.owners))
    if page == "Shed overview":
        page_overview(status, looms)
    elif page == "Loom lookup":
        page_lookup(status, looms)
    elif page == "Shed mode (camera)":
        page_shed_camera(status)
    elif page == "Data entry":
        page_data_entry()
    else:
        page_review_queue()


main()
