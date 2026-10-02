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

Run instructions for the backend, frontend and evals will be added as those phases land.
