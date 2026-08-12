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
from string import Formatter

import yaml

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LANGUAGE = "uk"

#: Every key the code asks for. A pack missing one of these fails on load with
#: the full list, rather than at three in the morning mid-call when some branch
#: finally reaches it. This is also the contract: adding a language means
#: filling in exactly this list and nothing else.
REQUIRED_KEYS = (
    "settings.default_country",
    "settings.default_tz",
    "settings.time_format",
    # BCP-47 tags handed to the speech services. Without these the text layer
    # speaks one language while the microphone and the voice speak another.
    "settings.speech_language",
    "settings.default_voice_id",
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
    "spoken.slot_window",
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
    "prompt.state.no_availability",
    "prompt.state.fee_unsaid",
    "prompt.intake.header",
    "prompt.intake.item_required",
    "prompt.intake.item_optional",
    "prompt.intake.footer",
    "prompt.booking.party_suffix",
    "prompt.booking.collect_slots",
    "prompt.booking.collect_nights",
    "prompt.booking.recap_fallback",
    "prompt.booking.body",
    "speech.digit_words",
    "speech.goodbye_words",
    "tools.claim_retracted",
    "errors.phone.missing",
    "errors.phone.not_spoken",
    "errors.phone.no_digits",
    "errors.phone.too_short",
    "errors.phone.too_long",
    "errors.phone.invalid",
    "errors.phone.wrong_digits",
    "errors.storage_unavailable",
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
    "errors.intake_refused",
    "errors.giving_up",
    "errors.address.not_found",
    "errors.address.out_of_area",
    "errors.no_space",
    "errors.message_no_topic",
    "errors.tool_unknown",
    "errors.nothing_booked",
    "errors.booking_not_found",
    "errors.booking_already_cancelled",
    "notes.message_recorded",
    "notes.call_time_limit",
    "ui.duration_min",
    "dashboard.title",
    "dashboard.months_short",
    "fixtures.first_names",
    "fixtures.last_names",
    "ui.per_unit",
    "spoken.datetime",
    # Defaults for business-config fields. They belong to the language: a config
    # that omits `unit:` must not inherit another language's word for it.
    "defaults.booking.action",
    "defaults.booking.action_gen",
    "defaults.booking.verb",
    "defaults.booking.resource_label",
    "defaults.booking.unit",
    "defaults.booking.start_label",
    "defaults.booking.end_label",
    "defaults.booking.party_label",
    "defaults.booking.ref_prefix",
    "defaults.business.agent_name",
    "defaults.business.persona",
    "defaults.business.tone",
    "defaults.business.currency",
    "defaults.business.customer",
    "defaults.business.escalation_examples",
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
    "tools.find_booking",
    "tools.cancel_booking",
    "tools.reschedule_booking",
    "tools.ref_code_arg",
    "prompt.booking.changes",
    "turn_taking.continuations",
    "turn_taking.sentence_end",
)


class MissingLocaleKey(KeyError):
    """A key the code asks for is absent from the language pack."""


class Locale:
    """One language pack, loaded once per process."""

    def __init__(self, lang: str, data: dict):
        self.lang = lang
        self._data = data

    _MISSING = object()

    def raw(self, key: str, default=_MISSING):
        """The value as it is in the YAML: a string, a list or a mapping.

        A sentinel rather than None, so that a falsy default ([], "", 0) is
        honoured instead of raising — the old check was `if default is not None`.
        """
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is not Locale._MISSING:
                    return default
                raise MissingLocaleKey(f"{self.lang}: no key {key!r} in the language pack")
            node = node[part]
        return node

    def broken_templates(self) -> list[str]:
        """Templates that `str.format` cannot even parse.

        A stray brace — an example containing JSON, a smiley — is invisible to a
        key-completeness check and raises only on the call that renders that one
        string, which might be the booking confirmation. Cheap to catch on load.
        """
        bad = []
        for key in REQUIRED_KEYS:
            try:
                value = self.raw(key)
            except MissingLocaleKey:
                continue
            if not isinstance(value, str):
                continue
            try:
                fields = [f for _, f, _, _ in Formatter().parse(value) if f is not None]
            except ValueError as e:
                bad.append(f"{key}: {e}")
                continue
            bad += [f"{key}: {{{f}}} is not a name" for f in fields if not f.isidentifier()]
        return bad

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
            try:
                hour_s, _, minute_s = value.partition(":")
                hour, minute = int(hour_s), int(minute_s or 0)
            except ValueError as e:
                raise ValueError(f"{self.lang}: cannot read a time from {value!r}") from e
        else:
            hour, minute = value.hour, value.minute
        if not (0 <= hour < 24 and 0 <= minute < 60):
            # Usually a typo in a business config's `hours:` — which would
            # otherwise take down the demo card and the "we are closed" message.
            raise ValueError(f"{self.lang}: {value!r} is not a valid time of day")
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
        except (KeyError, IndexError, ValueError) as e:
            # KeyError  — an argument the caller did not pass.
            # IndexError — a positional {} or {0} in the pack.
            # ValueError — a stray brace, e.g. someone put JSON in an example.
            # All three used to surface differently; only the first was caught,
            # so a malformed pack failed with a bare ValueError mid-call.
            raise MissingLocaleKey(
                f"{self.lang}: cannot render template {key!r} — {type(e).__name__}: {e}"
            ) from e


def load_locale(lang: str | None = None) -> Locale:
    """Load a pack, cached per process.

    The default is resolved before the cache, not inside it: `lru_cache`
    distinguishes `f()` from `f(None)` from `f("uk")`, so caching the raw
    argument gave three separate objects for one language.

    The file is read once per process — editing a YAML under a running bot has
    no effect until it restarts.
    """
    return _load_locale(lang or DEFAULT_LANGUAGE)


@lru_cache(maxsize=None)
def _load_locale(lang: str) -> Locale:
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
    broken = pack.broken_templates()
    if broken:
        raise MissingLocaleKey(
            f"language pack {lang!r} has unusable templates: " + "; ".join(broken)
        )
    return pack
