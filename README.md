# Weather-Advisory Support Bot

A LangGraph chat bot that answers outdoor-activity safety questions using live
Open-Meteo weather data, and gives advice **only** from written policy rules (SOPs).

> 🚧 Work in progress — built phase by phase. See `PLAN.md` for the design and
> `PROGRESS.md` for what each phase delivered.

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
```

Rain-system detection thresholds and time-window hours are in `config/facts_config.yaml`.

Run instructions for the chat backend, frontend and evals will be added as those phases land.
