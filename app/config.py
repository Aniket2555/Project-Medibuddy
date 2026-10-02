"""Environment + LLM factory. The model/provider is a config choice, not a code one."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class LLMNotConfigured(RuntimeError):
    pass


def get_llm(model: str | None = None):
    """Groq chat model, temperature 0 (we want consistent extraction, not creativity).
    `model` overrides GROQ_MODEL (the eval judge uses a different model on purpose)."""
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key or key == "your_groq_api_key_here":
        raise LLMNotConfigured(
            "GROQ_API_KEY is not set. Copy .env.example to .env and add your key "
            "(https://console.groq.com/keys)."
        )
    from langchain_groq import ChatGroq

    extra = {}
    # gpt-oss models are reasoning models. "low" measured ~3x fewer tokens per question
    # AND fewer rejected drafts than the default (see PROGRESS / evals notes).
    if os.getenv("GROQ_REASONING_EFFORT"):
        extra["reasoning_effort"] = os.getenv("GROQ_REASONING_EFFORT")
    return ChatGroq(
        model=model or os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
        temperature=0,
        max_retries=2,
        timeout=float(os.getenv("LLM_TIMEOUT", "30")),
        api_key=key,
        **extra,
    )
