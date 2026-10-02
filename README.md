# Weather-Advisory Support Bot

A LangGraph chat bot that answers outdoor-activity safety questions using live
Open-Meteo weather data, and gives advice **only** from written policy rules (SOPs).

> 🚧 Work in progress — being built phase by phase.

## Setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env      # then put your GROQ_API_KEY in .env
```

## Useful commands (so far)

```bash
# Unit tests
python -m pytest -q

# See the live weather "facts" the bot would reason over, for any city / time window
python -m app.inspect_weather Bhopal
python -m app.inspect_weather "New Delhi" --day tomorrow --part afternoon

# Regenerate the synthetic weather fixtures used by tests/evals
python -m evals.fixtures.build_synthetic

# Validate all SOPs and print the policy table
python -m app.sop_loader
```

Rain-system detection thresholds and time-window hours are in `config/facts_config.yaml`.

## The policies (SOPs)

SOPs live in `sops/`, **one YAML file per policy**.
*Why YAML files:* a policy team can read and edit them without knowing Python, comments explain
intent next to the rule, each policy change is its own reviewable git diff, and adding a policy
means adding one file.

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

Each SOP declares which activities/audiences it covers, a condition over named weather facts
(`all` / `any` / `not` with operators like `>=`, `between`), a severity, and guidance text whose
numbers are `{fact}` placeholders filled from the live forecast.

### Adding a policy (no code changes, no restart)

1. Copy `sops/_TEMPLATE.yaml` to `sops/SOP-013-your-policy.yaml` and fill it in.
   Available facts: `python -m app.inspect_weather <city>`. Vocabulary: `config/vocabulary.yaml`.
2. Run `python -m app.sop_loader`: it validates every file and reports mistakes by file and field.
3. Ask the bot a matching question. SOPs are re-read on every request.

Run instructions for the chat backend, frontend and evals will be added as those phases land.
