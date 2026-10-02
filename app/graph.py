"""The LangGraph agent.

    understand ──┬─ out_of_scope ───────────────► respond_out_of_scope
                 ├─ unknown activity ───────────► respond_unknown_activity
                 ├─ missing city / activity ────► ask_clarification
                 ├─ LLM or policy-file error ───► respond_error
                 └─ ok ─► resolve_location ─┬─ fail ─► respond_data_unavailable
                                            └─ ok ─► fetch_weather ─┬─ fail ─► respond_data_unavailable
                                                                    └─ ok ─► derive_facts ─┬─ fail ─► respond_data_unavailable
                                                                                           └─ ok ─► match_sops ─┬─ none ─► respond_no_policy
                                                                                                                └─ match ─► compose ─┬─ LLM down ─► respond_templated
                                                                                                                                     └─ draft ─► validate ─┬─ pass ─► finalize
                                                                                                                                                           ├─ fail, retry ─► compose
                                                                                                                                                           └─ fail x3 ─► respond_templated

Only two nodes call the LLM: `understand` (labels the question) and `compose`
(words the answer). Everything that decides facts or advice is deterministic.
Dependencies (weather client, LLM callables, SOP folder) are injected, so evals
can swap in fixtures, a simulated outage or a scripted LLM without patching.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel

from app import responses
from app.compose import build_compose_prompt
from app.facts import TimeWindow, derive_facts
from app.grounding import render_answer, validate_draft
from app.intent import (apply_assumptions, build_intent_model, build_intent_prompt, location_candidates,
                        merge_with_session)
from app.matcher import Intent, SopMatch, match
from app.sop_loader import OTHER, SOPS_DIR, SopLibrary, SopValidationError, load_sops
from app.weather import Location, OpenMeteoClient, WeatherClient, WeatherDataError

MAX_COMPOSE_ATTEMPTS = 3  # first draft + 2 retries, then the templated answer
INTENT_ATTEMPTS = 3


@dataclass
class Deps:
    weather: WeatherClient
    structured: Callable[[str, str, type[BaseModel]], Any]  # (system, user, schema) -> model instance / dict
    complete: Callable[[str, str], str]                       # (system, user) -> text
    sops_dir: Path = SOPS_DIR


def groq_deps(weather: WeatherClient | None = None, sops_dir: Path = SOPS_DIR) -> Deps:
    from app.config import get_llm

    llm = get_llm()

    def structured(system: str, user: str, schema: type[BaseModel]):
        # Strict JSON-schema mode: output is constrained to the schema while it's generated.
        return llm.with_structured_output(schema, method="json_schema", strict=True).invoke(
            [SystemMessage(system), HumanMessage(user)])

    def complete(system: str, user: str) -> str:
        return llm.invoke([SystemMessage(system), HumanMessage(user)]).content

    return Deps(weather or OpenMeteoClient(), structured, complete, sops_dir)


class State(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    session: dict          # persists across turns (via the checkpointer)
    # per-turn scratch, reset by turn_input()
    route: str
    intent: dict
    notes: list[str]
    location: dict
    forecast: dict
    facts: dict
    primary: list[dict]
    also: list[dict]
    skipped: list[dict]
    draft: str
    attempts: int
    errors: list[str]
    answer: str
    kind: str              # answered | templated | no_policy | unknown_activity | out_of_scope | data_unavailable | clarify | error
    error: str
    trace: list[str]


def turn_input(text: str) -> dict:
    return {
        "messages": [HumanMessage(text)], "route": "", "intent": {}, "notes": [], "location": {},
        "forecast": {}, "facts": {}, "primary": [], "also": [], "skipped": [], "draft": "",
        "attempts": 0, "errors": [], "answer": "", "kind": "", "error": "", "trace": [],
    }


def _serialize(m: SopMatch, lib: SopLibrary) -> dict:
    return {
        "id": m.id, "title": m.sop.title, "severity": m.severity, "grade": m.grade,
        "guidance": m.guidance, "override": m.sop.override, "version": m.sop.version,
        "must_mention": list(m.sop.must_mention), "facts": sorted(m.sop.referenced_facts()),
        "reasons": [str(c) for c in m.reasons],
        "passed_factors": m.passed_factors, "failed_factors": m.failed_factors,
        "source": lib.sources.get(m.id),
    }


def _context(state: State) -> str:
    """Short context for the intent labeller: what memory holds + the last exchange."""
    s = state.get("session") or {}
    known = {k: s.get(k) for k in ("location_query", "activity", "audiences", "day", "part") if s.get(k)}
    lines = [f"Known so far: {known}" if known else "Known so far: nothing"]
    if s.get("pending"):
        lines.append(f"The assistant just asked the user for: {s['pending']}")
    for msg in state["messages"][-5:-1]:
        role = "User" if isinstance(msg, HumanMessage) else "Assistant"
        lines.append(f"{role}: {str(msg.content)[:300]}")
    return "\n".join(lines)


def build_graph(deps: Deps, checkpointer=None):
    def step(state: State, name: str, **update) -> dict:
        update["trace"] = state.get("trace", []) + [name]
        return update

    def reply(state: State, name: str, kind: str, text: str, **extra) -> dict:
        return step(state, name, messages=[AIMessage(text)], answer=text, kind=kind, forecast={}, **extra)

    def understand(state: State) -> dict:
        text = str(state["messages"][-1].content)
        session = dict(state.get("session") or {})
        try:
            lib = load_sops(deps.sops_dir)
        except SopValidationError as exc:
            return step(state, "understand", route="error", error=f"policy files invalid: {exc}")
        system, user = build_intent_prompt(text, _context(state), lib)
        parsed, last_exc = None, None
        for _ in range(INTENT_ATTEMPTS):
            try:
                parsed = deps.structured(system, user, build_intent_model(lib))
                parsed = parsed.model_dump() if isinstance(parsed, BaseModel) else dict(parsed)
                break
            except Exception as exc:  # LLM down, rate-limited, or returned something off-schema
                last_exc = exc
        if parsed is None:
            return step(state, "understand", route="error", error=f"intent extraction failed: {last_exc!r}")

        notes = apply_assumptions(text, parsed, lib)
        supplies_detail = any(parsed.get(k) for k in ("location", "activity", "day", "part"))
        if not parsed.get("in_scope") and not (session.get("pending") and supplies_detail):
            return step(state, "understand", route="out_of_scope", intent=parsed)

        merged = merge_with_session(parsed, session)
        if merged["location"] != session.get("location_query"):
            session.pop("location", None)  # new place -> geocode again
        session.update({k: merged[k] for k in ("activity", "activity_text", "audiences", "day", "part")})
        session["location_query"] = merged["location"]

        if merged["activity"] == OTHER:
            route = "unknown_activity"
        elif not merged["location"]:
            route, session["pending"] = "clarify", "location"
        elif not merged["activity"]:
            route, session["pending"] = "clarify", "activity"
        else:
            route = "ok"
            session.pop("pending", None)
        return step(state, "understand", route=route, intent=merged, notes=notes, session=session)

    def resolve_location(state: State) -> dict:
        session = dict(state["session"])
        query = state["intent"]["location"]
        cached = session.get("location")
        if cached and cached.get("query") == query:
            loc = {k: v for k, v in cached.items() if k != "query"}
            return step(state, "resolve_location(cached)", route="ok", location=loc)
        loc, last_exc = None, None
        for candidate in location_candidates(query):  # "Lodhi Garden, Delhi" -> also try "Delhi"
            try:
                loc = deps.weather.geocode(candidate).__dict__
                break
            except WeatherDataError as exc:
                last_exc = exc
        if loc is None:
            return step(state, "resolve_location", route="fail", error=last_exc.user_message)
        session["location"] = {**loc, "query": query}
        return step(state, "resolve_location", route="ok", location=loc, session=session)

    def fetch_weather(state: State) -> dict:
        try:
            forecast = deps.weather.forecast(Location(**state["location"]))
        except WeatherDataError as exc:
            return step(state, "fetch_weather", route="fail", error=exc.user_message)
        return step(state, "fetch_weather", route="ok", forecast=forecast)

    def derive(state: State) -> dict:
        intent = state["intent"]
        try:
            facts = derive_facts(state["forecast"], TimeWindow(intent["day"], intent["part"]),
                                 Location(**state["location"]).label)
        except WeatherDataError as exc:
            return step(state, "derive_facts", route="fail", error=exc.user_message, forecast={})
        return step(state, "derive_facts", route="ok", facts=facts, forecast={})

    def match_sops(state: State) -> dict:
        lib = load_sops(deps.sops_dir)
        intent = state["intent"]
        result = match(Intent(intent["activity"], tuple(intent["audiences"])), state["facts"], lib)
        return step(
            state, "match_sops",
            route="no_match" if result.no_match else "match",
            primary=[_serialize(m, lib) for m in result.primary],
            also=[_serialize(m, lib) for m in result.also_applies],
            skipped=[{"id": s.sop_id, "reason": s.reason} for s in result.skipped],
        )

    def compose(state: State) -> dict:
        question = str(next(m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)).content)
        system, user = build_compose_prompt(question, state["primary"], state["facts"],
                                            previous=state["session"].get("last"), feedback=state.get("errors"))
        try:
            draft = deps.complete(system, user)
        except Exception as exc:
            return step(state, "compose", route="llm_failed", error=f"composer failed: {exc!r}")
        return step(state, "compose", route="ok", draft=str(draft), attempts=state.get("attempts", 0) + 1)

    def validate(state: State) -> dict:
        cited = [m["id"] for m in state["primary"] + state["also"]]
        errors = validate_draft(state["draft"], state["primary"], cited, state["facts"])
        if not errors:
            route = "pass"
        elif state["attempts"] < MAX_COMPOSE_ATTEMPTS:
            route = "retry"
        else:
            route = "give_up"
        return step(state, "validate", route=route, errors=errors)

    def _remember(state: State, cited: list[str]) -> dict:
        session = dict(state["session"])
        session["last"] = {
            "cited": cited, "activity": state["intent"].get("activity"),
            "window_label": state["facts"].get("window_label"), "location": state["facts"].get("location_name"),
        }
        return session

    def _with_footer(state: State, body: str) -> str:
        return body + "\n" + responses.footer(state["primary"], state["also"], state["facts"], state.get("notes", []))

    def finalize(state: State) -> dict:
        text = _with_footer(state, render_answer(state["draft"], state["facts"]))
        cited = [m["id"] for m in state["primary"] + state["also"]]
        return reply(state, "finalize", "answered", text, session=_remember(state, cited))

    def respond_templated(state: State) -> dict:
        text = _with_footer(state, responses.templated_answer(state["primary"], state["facts"]))
        cited = [m["id"] for m in state["primary"] + state["also"]]
        return reply(state, "respond_templated", "templated", text, session=_remember(state, cited))

    def respond_no_policy(state: State) -> dict:
        text = responses.no_policy(state["intent"]["activity"], state["facts"])
        if state.get("notes"):
            text += "\n\n" + "\n".join(state["notes"])
        return reply(state, "respond_no_policy", "no_policy", text, session=_remember(state, []))

    def respond_unknown_activity(state: State) -> dict:
        lib = load_sops(deps.sops_dir)
        return reply(state, "respond_unknown_activity", "unknown_activity",
                     responses.unknown_activity(state["intent"].get("activity_text"), lib.activities))

    def respond_out_of_scope(state: State) -> dict:
        return reply(state, "respond_out_of_scope", "out_of_scope", responses.out_of_scope())

    def respond_data_unavailable(state: State) -> dict:
        return reply(state, "respond_data_unavailable", "data_unavailable", responses.data_unavailable(state["error"]))

    def ask_clarification(state: State) -> dict:
        pending = state["session"].get("pending")
        text = responses.ask_location() if pending == "location" else responses.ask_activity()
        return reply(state, "ask_clarification", "clarify", text)

    def respond_error(state: State) -> dict:
        return reply(state, "respond_error", "error", responses.intent_error())

    g = StateGraph(State)
    for name, fn in [
        ("understand", understand), ("resolve_location", resolve_location), ("fetch_weather", fetch_weather),
        ("derive_facts", derive), ("match_sops", match_sops), ("compose", compose), ("validate", validate),
        ("finalize", finalize), ("respond_templated", respond_templated), ("respond_no_policy", respond_no_policy),
        ("respond_unknown_activity", respond_unknown_activity), ("respond_out_of_scope", respond_out_of_scope),
        ("respond_data_unavailable", respond_data_unavailable), ("ask_clarification", ask_clarification),
        ("respond_error", respond_error),
    ]:
        g.add_node(name, fn)

    by_route = lambda state: state["route"]  # noqa: E731
    g.add_edge(START, "understand")
    g.add_conditional_edges("understand", by_route, {
        "ok": "resolve_location", "out_of_scope": "respond_out_of_scope", "unknown_activity": "respond_unknown_activity",
        "clarify": "ask_clarification", "error": "respond_error"})
    g.add_conditional_edges("resolve_location", by_route, {"ok": "fetch_weather", "fail": "respond_data_unavailable"})
    g.add_conditional_edges("fetch_weather", by_route, {"ok": "derive_facts", "fail": "respond_data_unavailable"})
    g.add_conditional_edges("derive_facts", by_route, {"ok": "match_sops", "fail": "respond_data_unavailable"})
    g.add_conditional_edges("match_sops", by_route, {"match": "compose", "no_match": "respond_no_policy"})
    g.add_conditional_edges("compose", by_route, {"ok": "validate", "llm_failed": "respond_templated"})
    g.add_conditional_edges("validate", by_route, {"pass": "finalize", "retry": "compose", "give_up": "respond_templated"})
    for terminal in ("finalize", "respond_templated", "respond_no_policy", "respond_unknown_activity",
                     "respond_out_of_scope", "respond_data_unavailable", "ask_clarification", "respond_error"):
        g.add_edge(terminal, END)
    return g.compile(checkpointer=checkpointer or MemorySaver())


def new_session_id() -> str:
    return uuid.uuid4().hex


def run_turn(graph, session_id: str, text: str) -> dict:
    """One user message in, final state out (answer, kind, primary, facts, trace, ...)."""
    return graph.invoke(turn_input(text), config={"configurable": {"thread_id": session_id}})
