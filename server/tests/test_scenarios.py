"""The simulator's caller scenarios, one file per language.

The interesting test here is the last one. A fabrication check can only tell
"the agent heard this" from "the agent copied it out of an example" if the two
sets of data are disjoint — and nothing enforces that by itself.
"""

import re
import sys
from pathlib import Path

import pytest
import yaml

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

SCENARIOS_DIR = SERVER / "scenarios"
LOCALES_DIR = SERVER / "locales"
BUSINESSES_DIR = SERVER / "businesses"

ALL_SCENARIOS = sorted(p.stem for p in SCENARIOS_DIR.glob("*.yaml"))

REQUIRED = ("facts", "caller_prompt", "greeting_hint", "end_marker", "personas",
            "claim_words", "distinctive")
REQUIRED_FACTS = ("name", "phone", "address")


def load(lang: str) -> dict:
    return yaml.safe_load((SCENARIOS_DIR / f"{lang}.yaml").read_text(encoding="utf-8"))


def test_every_language_pack_has_scenarios():
    """A language with no callers cannot be tested, which is worse than not
    having the language: it looks supported."""
    languages = {p.stem for p in LOCALES_DIR.glob("*.yaml")}
    assert languages <= set(ALL_SCENARIOS), f"no scenarios for {languages - set(ALL_SCENARIOS)}"


@pytest.mark.parametrize("lang", ALL_SCENARIOS)
def test_shape(lang):
    data = load(lang)
    assert not set(REQUIRED) - set(data), f"missing: {set(REQUIRED) - set(data)}"
    assert not set(REQUIRED_FACTS) - set(data["facts"])
    assert data["claim_words"], "without these the success-claim check does nothing"


@pytest.mark.parametrize("lang", ALL_SCENARIOS)
def test_personas_are_complete(lang):
    for name, body in load(lang)["personas"].items():
        assert {"title", "style", "goal"} <= set(body), f"{lang}/{name} is incomplete"


@pytest.mark.parametrize("lang", ALL_SCENARIOS)
def test_the_same_personas_exist_in_every_language(lang):
    """Otherwise a language quietly has weaker coverage than another."""
    reference = set(load(ALL_SCENARIOS[0])["personas"])
    assert set(load(lang)["personas"]) == reference


@pytest.mark.parametrize("lang", ALL_SCENARIOS)
def test_caller_prompt_uses_every_fact(lang):
    """A fact the caller prompt never mentions can never be said, so a check
    that looks for it would pass for the wrong reason."""
    prompt = load(lang)["caller_prompt"]
    for fact in REQUIRED_FACTS:
        assert "{" + fact + "}" in prompt, f"{lang}: the caller prompt never uses {fact}"


@pytest.mark.parametrize("lang", ALL_SCENARIOS)
def test_placeholders_are_ones_the_code_fills(lang):
    known = {"business_name", "style", "goal", "persona", "service", "service_alt", *REQUIRED_FACTS}
    data = load(lang)
    texts = [data["caller_prompt"], data["greeting_hint"]]
    texts += [p["style"] for p in data["personas"].values()]
    texts += [p["goal"] for p in data["personas"].values()]
    for text in texts:
        for found in re.findall(r"\{(\w+)\}", text):
            if re.fullmatch(r"(plus|minus)_\d+", found):
                continue
            assert found in known, f"{lang}: unknown placeholder {found!r}"


@pytest.mark.parametrize("lang", ALL_SCENARIOS)
def test_caller_facts_appear_nowhere_the_agent_can_read_them(lang):
    """The caller's name, phone and address must not exist in any prompt.

    This is the invariant the whole fabrication check rests on. It was violated
    before anyone noticed: the Ukrainian scenarios used the same address that
    sat in a config's example line, so "the agent invented the address" could
    never be distinguished from "the agent repeated what it was told" — and the
    check was blind exactly where it mattered.
    """
    facts = load(lang)["facts"]
    haystack = "\n".join(
        p.read_text(encoding="utf-8")
        for p in [*LOCALES_DIR.glob("*.yaml"), *BUSINESSES_DIR.glob("*.yaml")]
    ).lower()

    distinctive = load(lang)["distinctive"]
    leaked = [token for token in distinctive if token.lower() in haystack]
    assert not leaked, f"{lang}: caller facts leak into prompts — {leaked}"

    # And the list has to actually cover the facts, or it proves nothing.
    joined = " ".join(str(v) for v in facts.values()).lower()
    for token in distinctive:
        assert token.lower() in joined, f"{lang}: {token!r} is not part of any fact"
