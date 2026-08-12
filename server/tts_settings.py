"""tts_settings.py — the synthesiser's knobs, in one place.

These four numbers decide what the agent sounds like, and they were written out
twice: once in bot.py for the live call, once in voice_lab.py for auditioning
voices. The copies drifted — the lab synthesised at style 0.0 while the bot ran
at 1.0 — so voices were compared flat and then shipped expressive. A voice
chosen that way is chosen against the wrong recording.

Every value can be overridden through the environment, which is how the
experiments were run in the first place; the point is only that both callers
read the same variable and the same default.
"""

from __future__ import annotations

import os

#: Low latency is the whole reason for flash: it is the difference between a
#: reply that starts in a tenth of a second and one that starts in half.
MODEL = os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")

#: Slightly under natural pace. A receptionist reading a price or a reference
#: number at full speed is asking to be made to repeat it.
SPEED = float(os.getenv("ELEVENLABS_SPEED", "0.95"))

#: Chosen by ear over three takes each at 0.4, 0.5 and 0.6 on the shipping
#: voice, reading a line with a date, a price and a phone number in it. 0.6 is
#: the bottom of the range ElevenLabs describe as "more consistent but
#: potentially monotonous"; 0.4 sits in "more emotional, dynamic… but may
#: occasionally sound unstable". For a receptionist dictating a booking code, a
#: take that wanders costs more than one that is a shade flat.
#:
#: Judged on three generations per setting rather than one, because stability IS
#: the variation between generations — a single clip shows one roll of the dice
#: and nothing about the spread.
STABILITY = float(os.getenv("ELEVENLABS_STABILITY", "0.6"))

#: At the top of the range, and this is a DELIBERATE departure from the vendor's
#: advice: "In general, we recommend keeping this setting at 0 at all times",
#: because style exaggeration "has shown to make the model slightly less stable"
#: and "might increase latency if set to anything other than 0".
#:
#: Kept at 1.0 because it won a listening comparison on this voice, and because
#: the documented latency cost did not reproduce here — measured across four
#: configurations, time-to-first-byte ranged 232–348 ms with no ordering by
#: style at all, i.e. inside the network noise. The stability cost is real, and
#: is paid for by raising stability to 0.6 above.
#:
#: If the voice changes, re-run the comparison before assuming this still holds:
#:     python voice_lab.py --lang en style --only <voice> --values 1.0@0.6,0.0@0.5
STYLE = float(os.getenv("ELEVENLABS_STYLE", "1.0"))

#: How closely the output sticks to the original speaker. ElevenLabs' own
#: default; never tuned here, kept explicit so it is not silently different
#: between two call sites.
SIMILARITY_BOOST = float(os.getenv("ELEVENLABS_SIMILARITY", "0.75"))


def as_dict() -> dict:
    """The knobs as the REST API's `voice_settings` object."""
    return {
        "stability": STABILITY,
        "similarity_boost": SIMILARITY_BOOST,
        "style": STYLE,
        "speed": SPEED,
    }


def summary() -> str:
    """One line, for a log or a comparison page — so what you heard is on record."""
    return (
        f"{MODEL} · speed {SPEED} · stability {STABILITY} "
        f"· style {STYLE} · similarity {SIMILARITY_BOOST}"
    )
