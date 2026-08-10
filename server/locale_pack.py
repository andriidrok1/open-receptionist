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

from pathlib import Path

import yaml

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LANGUAGE = "uk"


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
            return value.format(**kwargs) if kwargs else value
        except KeyError as e:
            raise MissingLocaleKey(
                f"{self.lang}: template {key!r} wants {e} which was not passed"
            ) from e


def load_locale(lang: str | None = None) -> Locale:
    lang = lang or DEFAULT_LANGUAGE
    path = LOCALES_DIR / f"{lang}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in LOCALES_DIR.glob("*.yaml"))
        raise FileNotFoundError(f"no language pack {path}; available: {available}")
    return Locale(lang, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
