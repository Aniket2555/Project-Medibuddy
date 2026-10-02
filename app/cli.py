"""Terminal chat with the real graph (Groq + live Open-Meteo).

    python -m app.cli
    python -m app.cli "Is it safe to cycle in Bhopal today?"   # one-shot

Commands: /why (policies, reasons and path for the last answer), /new (new session), /quit
"""

from __future__ import annotations

import sys

from app.graph import build_graph, groq_deps, new_session_id, run_turn


def explain(state: dict) -> str:
    lines = [f"kind: {state.get('kind')} | path: {' -> '.join(state.get('trace', []))}"]
    if state.get("intent"):
        lines.append(f"intent: {state['intent']}")
    for m in state.get("primary", []):
        lines.append(f"{m['id']} ({m['severity']}{', ' + m['grade'] if m.get('grade') else ''}) from sops/{m['source']}")
        lines += [f"    because {r}" for r in m["reasons"][:6]]
    for m in state.get("also", []):
        lines.append(f"{m['id']} also applies ({m['severity']})")
    if state.get("errors"):
        lines.append(f"last validation errors: {state['errors']}")
    if state.get("error"):
        lines.append(f"error: {state['error']}")
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    graph = build_graph(groq_deps())
    sid, last = new_session_id(), {}

    if len(sys.argv) > 1:
        last = run_turn(graph, sid, " ".join(sys.argv[1:]))
        print(last["answer"] + "\n\n[why] " + explain(last))
        return 0

    print("Weather-safety assistant. Ask about an outdoor activity. /why  /new  /quit")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if not text:
            continue
        if text == "/quit":
            return 0
        if text == "/new":
            sid, last = new_session_id(), {}
            print("(new session)")
            continue
        if text == "/why":
            print(explain(last) if last else "(nothing yet)")
            continue
        last = run_turn(graph, sid, text)
        print(f"\nbot> {last['answer']}")


if __name__ == "__main__":
    raise SystemExit(main())
