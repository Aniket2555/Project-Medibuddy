"""Fixed-text replies and the citation footer. No LLM is involved here.

Every failure path (no weather data, no policy, out of scope, LLM trouble) ends
in one of these templates, so those paths can't hallucinate. The footer that
names the policies and the data source is also built here, from the matcher's
output and the facts, never from model text.
"""

from __future__ import annotations

from typing import Any

from app.matcher import render


def out_of_scope() -> str:
    return ("I can only help with questions about doing outdoor activities safely in the current weather "
            "(for example: \"Is it safe to cycle in Pune this evening?\"). I don't have guidance for that request.")


def unknown_activity(activity_text: str | None, known: dict[str, str]) -> str:
    what = activity_text or "that activity"
    examples = ", ".join(k.replace("_", " ") for k in list(known)[:8])
    return (f"Sorry, we don't have a safety policy for {what}, so I can't give advice on it, and I won't guess. "
            f"I can help with activities such as {examples}.")


def data_unavailable(user_message: str) -> str:
    return (f"{user_message} I can't give weather-based advice without live data, and I won't guess a forecast. "
            "Please try again in a few minutes, or check the name of the place.")


def no_policy(activity: str, facts: dict[str, Any]) -> str:
    return render(
        f"None of our safety policies cover {activity.replace('_', ' ')} in the current conditions "
        "for {location_name} ({window_label}), so I don't have advice to give, and I won't make some up. "
        "For reference, the forecast for that time: {window_description}, up to {window_temp_max_c} °C, "
        "gusts up to {window_gusts_max_kmh} km/h, UV index up to {window_uv_max}, "
        "{window_precip_prob_max}% chance of rain.",
        facts,
    )


def ask_location() -> str:
    return "Which city or town are you asking about?"


def ask_activity() -> str:
    return ("What are you planning to do? For example: a run, a bike ride, a walk, a picnic, "
            "taking the kids to the park, or commuting.")


def intent_error() -> str:
    return "Sorry, I couldn't process that just now. Could you rephrase your question?"


def templated_answer(primary: list[dict[str, Any]], facts: dict[str, Any]) -> str:
    """The policy text itself, rendered with live values. Used when the LLM draft
    fails validation twice or the LLM is unavailable."""
    return "\n\n".join(f"[{m['id']}] {render(m['guidance'], facts)}" for m in primary)


def footer(primary: list[dict], also: list[dict], facts: dict[str, Any], notes: list[str]) -> str:
    lines = ["", "---"]
    lines.append("Policy basis: " + "; ".join(
        f"{m['id']} {m['title']} ({m['severity']}{', ' + m['grade'] if m.get('grade') else ''}, v{m['version']})"
        for m in primary))
    if also:
        lines.append("Also applies (not expanded): " + "; ".join(f"{m['id']} {m['title']} ({m['severity']})" for m in also))
    lines.append(render("Weather data: Open-Meteo for {location_name}, {window_label} "
                        "(forecast issued {data_time} local time).", facts))
    lines += notes
    return "\n".join(lines)
