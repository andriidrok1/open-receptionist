"""Checks that read the code rather than run it.

bot.py is the one module the test suite never executes: building the pipeline
needs a live WebRTC transport, API keys and a person to talk into a microphone.
So the code path that every single call goes through is the least tested in the
project — and it showed. `recorder` was used thirty lines before it was created,
which Python only notices at runtime, and the result was that every call died
the moment the browser connected. 117 green tests said nothing about it.

A linter reads the module instead of running it, which is exactly the leverage
needed here. `used-before-assignment` and `undefined-variable` are the two rules
that catch "this crashes as soon as the function is entered" — the class of bug
that a suite full of unit tests can miss entirely because it never enters the
function.

Kept narrow on purpose: two rules, no style opinions. A test that fails over
line length teaches people to skip it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent
RULES = "used-before-assignment,undefined-variable"


#: The knobs that decide what the agent sounds like. Only tts_settings.py may
#: read them: they were written out in bot.py and again in voice_lab.py, the two
#: copies disagreed on `style` (1.0 against 0.0), and voices were auditioned flat
#: and then shipped expressive — a comparison of recordings the product never
#: makes. Nothing in the output says which numbers produced a sample, so the
#: mismatch is invisible until someone thinks to ask.
TTS_KNOBS = ("ELEVENLABS_STABILITY", "ELEVENLABS_STYLE", "ELEVENLABS_SPEED",
             "ELEVENLABS_TTS_MODEL", "ELEVENLABS_SIMILARITY")


def test_only_one_module_decides_how_the_voice_sounds():
    """Only READING them counts. Naming one in a comment is how the rule gets
    explained, and a check that cannot tell the two apart makes writing the
    explanation an error."""
    offenders = {}
    for path in sorted(SERVER.glob("*.py")):
        if path.name == "tts_settings.py":
            continue
        text = path.read_text(encoding="utf-8")
        found = [
            knob
            for knob in TTS_KNOBS
            if re.search(rf'getenv\(\s*["\']{knob}["\']', text)
            or re.search(rf'environ\[\s*["\']{knob}["\']', text)
        ]
        if found:
            offenders[path.name] = found
    assert not offenders, (
        f"{offenders} read the synthesiser's settings directly — import "
        "tts_settings instead, so the live call and the voice lab cannot drift"
    )


@pytest.mark.skipif(
    shutil.which("pylint") is None
    and not (Path(sys.executable).parent / "pylint").exists(),
    reason="pylint is not installed",
)
def test_no_names_used_before_they_exist():
    pylint = Path(sys.executable).parent / "pylint"
    result = subprocess.run(
        [str(pylint) if pylint.exists() else "pylint",
         "--disable=all", f"--enable={RULES}", "--score=n",
         *sorted(str(p) for p in SERVER.glob("*.py"))],
        capture_output=True,
        text=True,
        cwd=SERVER.parent,
    )
    assert result.returncode == 0, (
        "a name is used before it exists — this crashes at runtime, not at "
        f"import:\n{result.stdout}{result.stderr}"
    )


def test_an_exported_variable_beats_dotenv():
    """`X=value python bot.py` must actually set X.

    .env used to be loaded with override=True, so it won over the environment.
    That cost real work three times before anyone named the pattern: a business
    chosen on the command line was ignored, a database path set by the simulator
    was ignored (pointing a wiping run at the demo data), and a synthesis model
    set for one experiment was ignored while the run reported nothing unusual.
    The first two were patched one variable at a time, which leaves the trap set
    for whatever variable is added next.

    Run in a subprocess with a real environment, because that is the only place
    the bug exists — import order inside one process can hide it.
    """
    env = {
        **os.environ,
        "BUSINESS": "cleaning",  # .env pins a different one
        "ELEVENLABS_TTS_MODEL": "test_model_that_is_not_in_dotenv",
    }
    result = subprocess.run(
        [sys.executable, "-c",
         "import business, tts_settings;"
         "print(business.BUSINESS.key);"
         "print(tts_settings.MODEL)"],
        capture_output=True, text=True, cwd=SERVER, env=env,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[-2] == "cleaning", f"BUSINESS was overridden by .env: {lines}"
    assert lines[-1] == "test_model_that_is_not_in_dotenv", (
        f"the synthesis model was overridden by .env: {lines}"
    )
