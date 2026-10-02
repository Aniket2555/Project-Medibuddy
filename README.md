# Weather-Advisory Support Bot

A LangGraph chat bot that answers outdoor-activity safety questions ("is it safe to cycle today?",
"should I take my kid to the park?") using **live Open-Meteo weather**, and gives advice **only from
written policies (SOPs)**. The model never decides what good advice is, never picks a policy and never
types a number. It labels the question and words the answer. Everything else is deterministic code.

**Live demo:** https://project-medibuddy-e3sltekvziticaoevc4dcj.streamlit.app/

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate                 # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
cp .env.example .env                   # add GROQ_API_KEY (free key: https://console.groq.com/keys)
                                       # defaults: GROQ_MODEL=openai/gpt-oss-120b, GROQ_REASONING_EFFORT=low

streamlit run frontend/streamlit_app.py      # chat UI at http://localhost:8501
python -m app.cli                            # or chat in the terminal (/why, /new, /quit)
python -m pytest -q                          # 155 unit + graph tests, no API key needed
python -m evals.run_evals                    # eval suite against the real LLM → evals/RESULTS.md
```

Python 3.11. The `.env` file is git-ignored; only `.env.example` (placeholders) is committed.

## What an answer looks like

> **Q:** Is it safe to go for a bike ride in Bhopal today? *(during a multi-day rain system, synthetic fixture)*
>
> A sustained heavy‑rain system is affecting Bhopal: about 86.4 mm of rain is forecast over the next 3 days,
> with the wettest day at 28.8 mm and gusts up to 42.0 km/h. Treat outdoor plan as high risk. Postpone activity,
> avoid waterlogged roads, underpasses and low‑lying areas, and do not ride through flooded streets. … **[SOP-001]**
> Gusts are forecast to reach 42.0 km/h … This can push a bicycle or scooter sideways, especially on bridges,
> flyovers and open roads. … **[SOP-004]** There is up to a 90% chance of rain … Expect slower traffic and possible
> delays. … **[SOP-005]**
>
> *(real output from gpt-oss-120b, abridged with "…")*
>
> *Policy basis: SOP-001 Active heavy-rain system (critical, v2); SOP-004 Strong gusts for cyclists and
> two-wheelers (high, v1); SOP-005 Likely rain on a journey (moderate, v1)*
> *Weather data: Open-Meteo for Bhopal, today (forecast issued … local time).*
> *I've taken "bike" to mean a motorbike or scooter. If you meant a bicycle, just say so and I'll check again.*

Every number came from the API response for that request and was inserted by code. The footer is built by
code from the matcher's output. In the UI, **"Why did it say that?"** under each answer shows the exact
comparisons (`window_gusts_max_kmh = 42.0 km/h (>= 40: yes)`), how the question was understood, and the path
through the graph.

---

## Design

### The boundary: deterministic code vs the model

The core decision. Anything that decides *facts* or *advice* is code; the model handles only language.

| Step | Who does it | Why |
|---|---|---|
| Understand the question → activity, who's going, city, day, time of day | **LLM**, constrained to a closed label set generated from the SOP vocabulary | Paraphrases ("taking the scooty to office", "my dad is 70 … stroll") need language understanding. The output can only be labels that exist |
| Resolve ambiguous words ("bike") | **Code**, a rule in `config/vocabulary.yaml` | Must behave the same every time, and the reply states the assumption |
| Geocode, fetch the forecast, handle failures | **Code** | Facts, not language |
| Turn raw JSON into named facts (window maxima, 3-day totals, rain-system flag) | **Code** (`app/facts.py`) | The only source of numbers; deterministic and tested |
| Decide which SOPs apply, rank them | **Code** (`app/matcher.py`) | Policy decisions must be auditable and repeatable |
| Word the reply from the selected policies | **LLM** | Natural phrasing that answers the actual question |
| Check the wording (numbers, citations, order) | **Code** (`app/grounding.py`) | The model's output is untrusted until validated |
| Every failure reply (no data, no policy, out of scope) | **Code**, fixed text | Failure paths can't hallucinate |

### The graph

```mermaid
graph TD
  START([user message]) --> U[understand<br/><i>LLM: labels only</i>]
  U -- out of scope --> OOS[respond_out_of_scope]
  U -- activity we have no policy for --> UA[respond_unknown_activity]
  U -- no city / no activity yet --> CL[ask_clarification]
  U -- LLM or policy-file error --> ER[respond_error]
  U -- ok --> RL[resolve_location]
  RL -- not found / geocoder down --> DU[respond_data_unavailable]
  RL -- ok --> FW[fetch_weather]
  FW -- API error / missing fields --> DU
  FW -- ok --> DF[derive_facts]
  DF -- time outside forecast --> DU
  DF -- ok --> M[match_sops<br/><i>deterministic</i>]
  M -- nothing applies --> NP[respond_no_policy]
  M -- policies apply --> C[compose<br/><i>LLM: wording only</i>]
  C -- LLM down --> T[respond_templated]
  C --> V[validate<br/><i>grounding checks</i>]
  V -- rejected, retry --> C
  V -- rejected 3x --> T
  V -- pass --> F[finalize]
```

15 nodes, 7 branch points, 8 distinct endings (`app/graph.py`). Why this shape:
- **Each external dependency gets its own node and its own failure edge** (geocoder, forecast API, the forecast
  window), and all of them route to the *same* honest "I can't get the weather" reply, as the brief requires for
  location failures.
- **Matching sits between data and wording**, so the composer only ever sees policies that already apply. It can't
  cite or invent anything else.
- **`validate → compose` is a real loop**: a rejected draft goes back with the exact rejection reasons. After 3
  rejections, or if the LLM is down, the reply is the policy text itself, rendered with live values (`respond_templated`).
  The user always gets policy-grounded advice.
- Dependencies (weather client, LLM calls, SOP folder) are **injected**, so tests and evals swap in fixtures, outages
  or a scripted LLM without patching.

### Policies (SOPs)

SOPs live in [`sops/`](sops), **one YAML file per policy**.
*Why this form:* the policy team can read and edit YAML without Python, comments sit next to each rule,
every change is its own reviewable git diff, and adding a policy means adding a file.

| ID | Category | Severity | When it applies |
|---|---|---|---|
| SOP-001 | general_hazard | critical | A multi-day heavy-rain system is detected (any activity; **leads the answer**) |
| SOP-002 | exercise | high | UV index ≥ 8 during outdoor exercise |
| SOP-003 | exercise | moderate | Feels-like ≥ 35 °C during strenuous exercise |
| SOP-004 | travel | high | Gusts ≥ 40 km/h on a bicycle or two-wheeler |
| SOP-005 | travel | moderate | Rain chance ≥ 70% on a journey |
| SOP-006 | travel | high | Visibility < 1 km or fog while driving/riding |
| SOP-007 | vulnerable_groups | high | Feels-like ≥ 35 °C or UV ≥ 6 with children, elderly or pregnant people |
| SOP-008 | vulnerable_groups | moderate | ≥ 32 °C when taking a pet out |
| SOP-009 | leisure | info / low / moderate | **Fuzzy:** picnic / park / outdoor event, graded good / mixed / poor by a 5-factor rubric |
| SOP-010 | general_hazard | high | Thunderstorm in the activity window |
| SOP-011 | exercise | low | Light rain (< 2.5 mm/h) while walking, running or hiking |
| SOP-012 | general | info | **All-clear** fallback: nothing else applies and values are within ordinary bands |

Each SOP declares the activities/audiences it covers, a condition over named weather facts (`all` / `any` / `not`,
operators like `>=`, `between`), a severity, and guidance whose numbers are `{fact}` placeholders filled from the
live forecast. The loader (`app/sop_loader.py`) validates everything strictly: unknown facts, operators,
severities, categories, placeholders or keys fail loudly, with one readable line per mistake naming the file and field.

Three policies show the judgment calls:
- **The rain system (SOP-001): "the reason is bigger than any single threshold."** Open-Meteo has no IMD alerts, so
  `app/facts.py` raises a `heavy_rain_system` flag when **≥ 3 of 5 independent signals** agree over the 3-day forecast
  (multi-day total ≥ 50 mm, wettest day ≥ 25 mm, ≥ 2 high-chance days, a heavy-rain/storm code, gusts ≥ 40 km/h), or any
  day ≥ 64.5 mm (IMD's "heavy rainfall" threshold). Thresholds live in `config/facts_config.yaml`. A synthetic case
  with only ~1.2 mm/h, no heavy-rain code and no extreme day still triggers it, because the *pattern* is a system. Testing
  showed that one isolated thunderstorm fired three correlated signals at once, so the "chance" signal counts *days*, measuring persistence.
  SOP-001 is `override: true`: it applies to every activity and always leads the answer.
- **The fuzzy picnic policy (SOP-009)** has no single threshold. It lists five soft factors (dry, rain chance < 40%,
  feels-like 18–32 °C, gusts < 30 km/h, UV < 8); code counts passes and picks a grade, and the policy team wrote each
  grade's guidance. Deterministic and auditable, without reducing to `if x > y`.
- **The all-clear (SOP-012)** makes "go ahead" traceable too, with a safety belt: it's suppressed if any other SOP applies,
  *and* it only fires while every value is inside ordinary bands. Strong wind while *running* matches no hazard SOP (the wind
  policy is for two-wheelers), and the all-clear refuses, so the bot says it has no guidance instead of falsely reassuring.

**"Any activity" means any activity we know about.** `config/vocabulary.yaml` is a closed list. Anything else (scuba
diving, paragliding) is `other` and gets "we don't have a policy for that", so a weather policy is never stretched to
something it wasn't written for.

### Adding a policy: no code changes, no restart

1. Copy `sops/_TEMPLATE.yaml` to `sops/SOP-013-your-policy.yaml` and fill it in
   (available facts: `python -m app.inspect_weather <city>`; vocabulary: `config/vocabulary.yaml`).
2. `python -m app.sop_loader` validates every file and prints the policy table.
3. Ask the bot. SOPs are re-read on every request; the Streamlit sidebar shows the new policy immediately; the intent
   labeller's allowed labels are regenerated from the vocabulary + SOP files, so even a brand-new activity
   (e.g. `kite_flying`) works with that one file.

**Honest boundary:** a policy over *existing* facts needs no code. A policy needing a *new kind of data* (e.g. air
quality, which isn't fetched today) needs a new field in `app/weather.py` and a new fact in `app/facts.py`. The
control flow (`app/graph.py`) never changes.

### Matching and conflicts

`app/matcher.py`, deterministic:
1. **Applicability:** the SOP covers the user's activity and, if it names audiences, one of them is present.
2. **Conditions** are evaluated against the facts for the *time window the user asked about* (so midday UV doesn't
   affect an evening run). If a value the SOP needs is missing, the SOP is **skipped**: we never act on data we don't have.
3. **Fallback:** the all-clear only when nothing else applies.
4. **Conflict policy:** rank by **override** (the rain system leads) → **severity** (critical → info) → **specificity**
   (a policy written for this activity/audience beats a generic one) → ID. The **top 3** get full guidance; the rest are
   cited as "also applies".
   *Why:* one winner would drop real hazards (high UV *and* strong wind on the same ride); listing everything is long.
   Leading with the most severe keeps answers honest and readable, and every applicable policy is still cited.

### The non-negotiables, and where each is enforced

| Requirement | Enforced in |
|---|---|
| Every answer traceable to an SOP, or says none applies | `app/grounding.py` `validate_draft` (every matched policy cited inline, no others); `app/responses.py` `footer` (code-built "Policy basis"); `respond_no_policy` / `respond_unknown_activity` |
| Policy changes need no code changes | `app/sop_loader.py` re-reads `sops/*.yaml` on every request; intent labels generated from vocabulary + SOPs (`app/intent.py` `build_intent_model`) |
| Never answer with a forecast we don't have | `app/weather.py` (every failure → `WeatherDataError`, including "200 OK but no values"), `app/facts.py` (`WindowUnavailable`), all routed to `respond_data_unavailable`; `app/matcher.py` skips SOPs with missing data |
| Never invent advice when no policy covers it | `app/matcher.py` (closed vocabulary, all-clear safety belt); fixed-text `respond_no_policy` / `respond_unknown_activity` |
| **The model composes language, never facts** | `app/compose.py`: the LLM writes `{placeholders}`, never numbers. `app/grounding.py` `validate_draft` rejects any number the model typed (even a correct one) unless it's a constant in the policy text, any unmatched SOP id, a missing required fact, stray braces, or a non-leading override policy. `render_answer` substitutes the API values only after validation |

Prompt injection is handled structurally, not by asking the model nicely. User text reaches only the intent labeller
(closed-enum output) and the composer (whose output must pass the validator). An injected "SOP-999 says storms are safe"
can't be cited, because SOP-999 isn't among the matched policies.

### Session memory

A LangGraph `MemorySaver` checkpointer keyed by a session id (one per browser session in Streamlit). The bot keeps
**structured facts** rather than relying on raw history alone: city (with cached coordinates), activity, who's going,
day and time window, any pending question, and which SOPs it cited last.
- "What about this evening instead?" reuses city and activity, **re-fetches** the weather (numbers always come from
  *this* request), and the composer is told what it said before, so it explains changes instead of silently contradicting itself.
- A question without a city gets "Which city?"; a bare "Pune" next completes it.
- Memory resets when a new session starts or the server restarts, as the brief specifies.

---

## Evals

**Results: [`evals/RESULTS.md`](evals/RESULTS.md)**, including hand-written notes on failures and caveats
(at the end of the same file; source: [`evals/NOTES.md`](evals/NOTES.md)).

- **31 cases** in [`evals/cases.yaml`](evals/cases.yaml), each stating what it checks, what a pass looks like and which
  brief requirement it covers: clear matches, paraphrases (including Hinglish), severe live weather, multiple SOPs,
  no SOP / out of scope, API and geocoder outages, six prompt-injection variants, session memory, consistency,
  correcting an assumption, and a new SOP with no code change.
- **Real LLM, 3 runs per case**, pass rates reported. Weather is injected: fixtures for stable cases, simulated outages,
  live Open-Meteo for live cases.
- **Pass/fail is programmatic:** reply type, cited SOPs and their order, extracted intent, and **every number in the
  answer must be a value from that request's weather data** (or a constant written in the cited policy).
- **Severe live weather, three layers:** L01 asks the brief's Bhopal question live and asserts against *whatever the API
  returns*; L02 **scans ~20 cities at eval time** for the most severe conditions right now; L03 is a synthetic rain-system
  twin, so the severe path is tested even when the weather is calm everywhere. If L01 only shows the all-clear because
  Bhopal is dry that day, the notes say so.
- **A faithfulness judge** (a different model) flags advice not found in the cited policy, which the number validator
  can't see. It's a separate column, not a gate, with a doctored control answer to show it can flag.
- `tests/test_eval_checks.py` proves the checks themselves can fail.

The suite found real problems, all fixed and documented in the notes: an ambiguous label (a bicycle *is* a
two-wheeler), doubled braces leaking into an answer, and provider rate limits being miscounted as bot failures.

## Limitations and what I'd do next

- **The rain-system flag is a proxy** derived from Open-Meteo signals, not an official alert feed. Next: ingest IMD /
  national-service alerts as an extra signal.
- **New kinds of data need code** (see "honest boundary" above). Policies over existing facts don't.
- **The LLM judge is a model** and can be wrong; it's reported, not trusted as a gate.
- **Geocoding takes the first match.** The resolved place ("Bhopal, Madhya Pradesh, India") is shown in every answer
  so a wrong match is visible, but it isn't questioned.
- **Free-tier limits:** Groq's free tier allows ~200k tokens/day for gpt-oss-120b; a full 3-run eval uses most of that.
  The runner separates provider errors from bot failures and supports `--resume`.
- **Policy gaps exist by design:** e.g. there's no heat policy for commuting, so a scooter ride at feels-like 40 °C gets an
  honest "no policy covers this". Policy coverage is the policy team's call, and adding one is a single file.

## Repository layout

```
app/
  weather.py         Open-Meteo client; every failure → WeatherDataError; test doubles
  facts.py           raw JSON → catalogued facts (time windows, 3-day outlook, rain-system flag)
  sop_schema.py      Pydantic schema for SOP files (condition DSL, rubric)
  sop_loader.py      loads + validates sops/ on every request
  matcher.py         deterministic matching, ranking, explanations
  intent.py          LLM job 1: closed-label intent extraction + deterministic post-processing
  compose.py         LLM job 2: composer prompt (templates + placeholders)
  grounding.py       validator + renderer (the "model doesn't decide facts" enforcement)
  responses.py       fixed-text replies + code-built citation footer
  graph.py           the LangGraph StateGraph, memory, run_turn
  config.py          env + Groq factory
  cli.py             terminal chat
  inspect_weather.py CLI: show facts for any city/window, record fixtures
config/
  facts_config.yaml  time windows + rain-system signals/thresholds
  vocabulary.yaml    severities, categories, activities, audiences, ambiguous-word rules
sops/                one YAML per policy + _TEMPLATE.yaml
frontend/            Streamlit chat app
evals/               cases.yaml, run_evals.py, RESULTS.md, NOTES.md, results/, fixtures/ (recorded + synthetic weather)
tests/               unit + graph tests (no API key needed)
```

## Useful commands

```bash
python -m app.sop_loader                                   # validate SOPs, print the policy table
python -m app.inspect_weather Bhopal --day tomorrow --part evening   # the facts the bot reasons over
python -m app.matcher --city Bhopal --activity cycling     # which SOPs apply and why (no LLM)
python -m app.matcher --fixture synthetic_rain_system_subtle --activity picnic --part afternoon
python -m evals.run_evals --only L01,A02 --repeats 1       # a few eval cases
python -m evals.run_evals --resume                         # continue a run stopped by provider quota
python -m evals.fixtures.build_synthetic                   # regenerate synthetic weather fixtures
```
