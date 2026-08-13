# Agent guide

A self-hosted AI voice receptionist. One YAML in `server/businesses/` describes
a business; the core knows nothing about any industry. Python 3.11+, Pipecat
cascade (STT → LLM → TTS), FastAPI dashboard, SQLite.

Read `docs/STATE.md` before changing anything — it is the honest ledger of
what exists, what is actually verified, and what is next. Keep it that way:
when you ship or verify something, update it in the same change.

Touching the Pipecat layer (pipeline, transports, frames)? Do not trust your
priors — `docs/building-on-pipecat.md` explains how to look up the current
APIs instead of guessing.

Tests: `cd server && python -m pytest`. They need no API keys and no network.

@docs/STATE.md
