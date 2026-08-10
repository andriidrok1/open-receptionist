"""locale_pack.py — the language pack: every string the agent says or reads.

The split is deliberate and worth stating once:

    locales/<lang>.yaml   — the LANGUAGE. How to talk, how to read a phone
                            number out loud, what the error messages say, the
                            names of months and weekdays.
    businesses/<key>.yaml — the BUSINESS. Its persona, prices, hours, the
                            things it knows about itself.

Before this existed, the language lived inside Python: the prompt scaffolding
was a set of f-strings in business.py, the error strings the model reads out
loud were scattered through crm.py, and month names sat next to the SQL. That is
fine for exactly one language and impossible for two.

Adding a language is a new file here plus a business config that names it. No
Python is touched.

Keys are addressed with dots: `L.text("prompt.phone_rules", customer_nom="гість")`.
Placeholders are `str.format` style. A missing key raises immediately at
startup — a prompt silently missing a paragraph is far worse than a crash.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LANGUAGE = "uk"

#: Every key the code asks for. A pack missing one of these fails on load with
#: the full list, rather than at three in the morning mid-call when some branch
#: finally reaches it. This is also the contract: adding a language means
#: filling in exactly this list and nothing else.
REQUIRED_KEYS = (
    "settings.default_country",
    "settings.time_format",
    "calendar.weekdays",
    "calendar.weekdays_short",
    "calendar.closed",
    "calendar.round_clock",
    "numbers.plural_rule",
    "numbers.months",
    "numbers.units",
    "spoken.money",
    "spoken.date",
    "spoken.slot",
    "spoken.nights",
    "placeholder_names",
    "prompt.identity",
    "prompt.speech_rules",
    "prompt.phone_rules",
    "prompt.date_hint",
    "prompt.date_hint_slots_suffix",
    "prompt.about",
    "prompt.escalation",
    "prompt.escalation_scope_fallback",
    "prompt.greeting_hint",
    "prompt.intake.header",
    "prompt.intake.item_required",
    "prompt.intake.item_optional",
    "prompt.intake.footer",
    "prompt.booking.party_suffix",
    "prompt.booking.collect_slots",
    "prompt.booking.collect_nights",
    "prompt.booking.recap_fallback",
    "prompt.booking.body",
    "errors.phone.missing",
    "errors.phone.no_digits",
    "errors.phone.too_short",
    "errors.phone.too_long",
    "errors.phone.invalid",
    "errors.name.missing",
    "errors.name.empty",
    "errors.name.placeholder",
    "errors.nights.bad_format",
    "errors.nights.end_before_start",
    "errors.nights.too_long",
    "errors.nights.in_past",
    "errors.nights.beyond_horizon",
    "errors.slot.bad_format",
    "errors.slot.in_past",
    "errors.slot.beyond_horizon",
    "errors.slot.closed_day",
    "errors.slot.before_opening",
    "errors.slot.after_closing",
    "errors.resource_unknown",
    "errors.party_too_large",
    "errors.intake_missing",
    "errors.no_space",
    "errors.message_no_topic",
    "errors.tool_unknown",
    "notes.message_recorded",
    "tools.phone_arg",
    "tools.date_arg",
    "tools.time_arg",
    "tools.start_arg",
    "tools.end_arg",
    "tools.name_arg",
    "tools.topic_arg",
    "tools.check_availability",
    "tools.check_availability_slots_suffix",
    "tools.create_booking",
    "tools.take_message",
)


class MissingLocaleKey(KeyError):
    """A key the code asks for is absent from the language pack."""


class Locale:
    """One language pack, loaded once per process."""

    def __init__(self, lang: str, data: dict):
        self.lang = lang
        self._data = data

    def raw(self, key: str, default=None):
        """The value as it is in the YAML: a string, a list or a mapping."""
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is not None:
                    return default
                raise MissingLocaleKey(f"{self.lang}: no key {key!r} in the language pack")
            node = node[part]
        return node

    def missing_keys(self) -> list[str]:
        """Which of REQUIRED_KEYS this pack does not define."""
        out = []
        for key in REQUIRED_KEYS:
            try:
                self.raw(key)
            except MissingLocaleKey:
                out.append(key)
        return out

    def time(self, value) -> str:
        """A clock time the way this language says it out loud.

        Accepts "HH:MM" or a datetime/time object. The 24-hour form is right for
        most of Europe; English needs "10 AM", and reading "22:00" to an
        American caller is exactly the kind of small wrongness that gives a
        product away as translated.
        """
        if isinstance(value, str):
            hour, _, minute = value.partition(":")
            hour, minute = int(hour), int(minute or 0)
        else:
            hour, minute = value.hour, value.minute
        if self.raw("settings.time_format") == "12h":
            suffix = "AM" if hour < 12 else "PM"
            h12 = hour % 12 or 12
            return f"{h12}:{minute:02d} {suffix}" if minute else f"{h12} {suffix}"
        return f"{hour:02d}:{minute:02d}"

    def text(self, key: str, **kwargs) -> str:
        """A string with `{placeholders}` substituted.

        Whitespace is NOT touched: use `|-` in the YAML for a block that should
        not end in a newline and `|` for one that should. Trimming here instead
        looked tidier and silently ate a newline that mattered — the intake list
        ended up glued onto the line above it.
        """
        value = self.raw(key)
        if not isinstance(value, str):
            raise MissingLocaleKey(f"{self.lang}: key {key!r} is not text")
        try:
            # Always format, even with no arguments: `... if kwargs else value`
            # looked like a harmless shortcut and meant that calling a template
            # without its arguments returned the template — so a booking
            # confirmation could be read out with "{now}" in it instead of
            # raising. Caught by tests/test_locale.py.
            return value.format(**kwargs)
        except KeyError as e:
            raise MissingLocaleKey(
                f"{self.lang}: template {key!r} wants {e} which was not passed"
            ) from e


@lru_cache(maxsize=None)
def load_locale(lang: str | None = None) -> Locale:
    lang = lang or DEFAULT_LANGUAGE
    path = LOCALES_DIR / f"{lang}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in LOCALES_DIR.glob("*.yaml"))
        raise FileNotFoundError(f"no language pack {path}; available: {available}")
    pack = Locale(lang, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    missing = pack.missing_keys()
    if missing:
        raise MissingLocaleKey(
            f"language pack {lang!r} is incomplete, missing: " + ", ".join(missing)
        )
    return pack
