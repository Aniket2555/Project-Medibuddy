"""Minimal chat frontend.

    streamlit run frontend/streamlit_app.py

Each browser session gets its own LangGraph thread id, so memory is per session
and a "New session" button starts fresh. Under every answer, an expander shows
why: the policies applied, the exact weather comparisons that triggered them,
the path through the graph and the facts used. The sidebar lists the SOPs as
currently loaded from sops/; it re-reads them on every interaction, so a newly
added policy file shows up without restarting.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `app` importable

from app.config import LLMNotConfigured  # noqa: E402
from app.graph import build_graph, groq_deps, new_session_id, run_turn  # noqa: E402
from app.sop_loader import SopValidationError, load_sops  # noqa: E402

st.set_page_config(page_title="Weather-Safety Assistant", page_icon="🌦️")

KIND_LABELS = {
    "answered": "Answered from policy",
    "templated": "Answered with the policy text (model draft rejected or unavailable)",
    "no_policy": "No policy applies",
    "unknown_activity": "No policy for this activity",
    "out_of_scope": "Out of scope",
    "data_unavailable": "Weather data unavailable",
    "clarify": "Needs more detail",
    "error": "Error",
}
FACTS_SHOWN = [
    "location_name", "window_label", "window_start", "window_end", "window_description",
    "window_temp_max_c", "window_apparent_temp_max_c", "window_precip_prob_max", "window_precip_total_mm",
    "window_gusts_max_kmh", "window_uv_max", "window_visibility_min_km",
    "precip_3day_mm", "precip_max_day_mm", "heavy_rain_system", "heavy_rain_signals", "data_time",
]


def md_escape(text: str) -> str:
    """Show model/policy text literally: no accidental markdown, LaTeX ($) or HTML."""
    for ch in "\\`*_{}[]<>()#+-.!|$~":
        text = text.replace(ch, "\\" + ch)
    return text


def show_answer(text: str) -> None:
    """Body as escaped markdown; the code-built footer (policy basis, data source,
    assumption notes) as captions, so it's visually separate from the advice."""
    body, _, footer = text.partition("\n---\n")
    st.markdown(md_escape(body.strip()).replace("\n", "  \n"))
    for line in footer.strip().splitlines():
        st.caption(md_escape(line))


@st.cache_resource
def get_graph():
    """One graph per server process; its MemorySaver holds every session's memory."""
    return build_graph(groq_deps())


def why_panel(state: dict) -> None:
    kind = state.get("kind", "")
    with st.expander(f"Why did it say that?  ·  {KIND_LABELS.get(kind, kind)}"):
        primary, also = state.get("primary", []), state.get("also", [])
        if primary:
            st.markdown("**Policies applied** (ranked: override → severity → specificity)")
            for m in primary:
                grade = f", grade *{m['grade']}*" if m.get("grade") else ""
                lead = " · **leads**" if m.get("override") else ""
                st.markdown(f"- **{m['id']}** {m['title']} — *{m['severity']}*{grade}{lead} · `sops/{m['source']}`")
                if m.get("grade"):
                    st.caption(f"passed: {', '.join(m['passed_factors']) or '—'} · "
                               f"not ideal: {', '.join(m['failed_factors']) or '—'}")
                else:
                    for r in m["reasons"]:
                        st.caption(f"because {r}")
        for m in also:
            st.markdown(f"- {m['id']} {m['title']} — *{m['severity']}* (also applies, not expanded)")
        if not primary and state.get("skipped"):
            st.markdown("**Why no policy applied**")
            for s in state["skipped"]:
                st.caption(f"{s['id']}: {s['reason']}")
        if state.get("intent"):
            i = state["intent"]
            st.markdown(f"**Understood as:** activity `{i.get('activity')}`, who `{i.get('audiences')}`, "
                        f"where `{i.get('location')}`, when `{i.get('day')} / {i.get('part')}`")
        st.markdown(f"**Path through the graph:** {' → '.join(state.get('trace', []))}")
        if state.get("attempts", 0) > 1 or state.get("errors"):
            st.markdown(f"**Composer drafts:** {state.get('attempts')} · last rejection: {state.get('errors') or '—'}")
        if state.get("facts"):
            st.markdown("**Weather facts used** (from Open-Meteo, this request)")
            st.json({k: state["facts"].get(k) for k in FACTS_SHOWN}, expanded=False)
        if state.get("error"):
            st.caption(f"error detail: {state['error']}")


def sidebar() -> None:
    with st.sidebar:
        st.header("Session")
        if st.button("New session", use_container_width=True):
            st.session_state.session_id = new_session_id()
            st.session_state.history = []
            st.rerun()
        st.caption(f"id `{st.session_state.session_id[:8]}` · memory lasts for this session only")

        st.header("Policies loaded")
        try:
            lib = load_sops()
        except SopValidationError as exc:
            st.error("Policy files are invalid; the bot will refuse to answer until fixed.")
            st.code("\n".join(exc.problems))
            return
        st.caption(f"{len(lib.sops)} SOPs from `sops/` (re-read on every message)")
        for s in lib.sops:
            st.markdown(f"**{s.id}** {s.title}  \n*{s.category} · {'/'.join(s.severities())}*")
        for w in lib.warnings:
            st.caption(f"⚠ {w}")


def main() -> None:
    st.title("🌦️ Weather-Safety Assistant")
    st.caption("Ask whether an outdoor activity is safe. Answers come only from written policies "
               "and live Open-Meteo data, e.g. *“Is it safe to cycle in Bhopal this evening?”*")

    st.session_state.setdefault("session_id", new_session_id())
    st.session_state.setdefault("history", [])  # [(role, text, state | None)]
    sidebar()

    try:
        graph = get_graph()
    except LLMNotConfigured as exc:
        st.error(str(exc))
        st.stop()

    for role, text, state in st.session_state.history:
        with st.chat_message(role):
            show_answer(text) if role == "assistant" else st.markdown(md_escape(text))
            if state:
                why_panel(state)

    if prompt := st.chat_input("Ask about an outdoor activity…"):
        with st.chat_message("user"):
            st.markdown(md_escape(prompt))
        with st.chat_message("assistant"):
            with st.spinner("Checking the weather and our policies…"):
                state = run_turn(graph, st.session_state.session_id, prompt)
            show_answer(state["answer"])
            why_panel(state)
        st.session_state.history += [("user", prompt, None), ("assistant", state["answer"], state)]


main()
