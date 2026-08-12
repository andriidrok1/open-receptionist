"""The showcase page — that it speaks the visitor's language and only that.

demo.html shipped for months with every label written straight into the markup
in Ukrainian, so an American opening the English dental practice read "Клієнт"
and "Сума" around it. The strings now come from the language packs; these tests
keep them there.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from locale_pack import LOCALES_DIR, load_locale  # noqa: E402

DEMO_HTML = Path(__file__).resolve().parent.parent / "static" / "demo.html"
PACKS = sorted(p.stem for p in LOCALES_DIR.glob("*.yaml"))
REFERENCE = "en"

#: Any letter from an alphabet no English page would contain. Cyrillic is what
#: this project actually leaks, being the language it was first written in.
NON_LATIN = re.compile(r"[Ѐ-ӿ]")


def _flat(node, prefix=""):
    """Every leaf in the section, as dotted keys."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            out |= _flat(v, f"{prefix}{k}.")
        return out
    return {prefix.rstrip("."): node}


def test_the_page_carries_no_language_of_its_own():
    """Not one visible word hard-coded into the markup.

    The check is deliberately blunt — no Cyrillic anywhere in the file, comments
    included. A softer rule that only looked at strings would have missed the
    original bug, since half of it lived in template literals inside the script.
    """
    found = NON_LATIN.findall(DEMO_HTML.read_text(encoding="utf-8"))
    assert not found, (
        f"{len(found)} non-Latin characters left in demo.html — page text belongs "
        "in locales/*.yaml under `demo:`, and comments are English like the rest "
        "of the project"
    )


@pytest.mark.parametrize("lang", [x for x in PACKS if x != REFERENCE])
def test_every_pack_translates_the_whole_page(lang):
    """A pack either dresses the page completely or not at all.

    Half a translation is worse than none: the visitor gets a page in two
    languages and concludes the thing is unfinished. A pack with no `demo:`
    section is fine — it simply is not offered in the picker.
    """
    other = load_locale(lang).raw("demo", None)
    if not other:
        pytest.skip(f"the {lang} pack does not translate the page")
    reference = _flat(load_locale(REFERENCE).raw("demo"))
    missing = sorted(set(reference) - set(_flat(other)))
    assert not missing, f"{lang} is missing: {', '.join(missing)}"


@pytest.mark.parametrize("lang", PACKS)
def test_a_translated_page_names_its_own_language(lang):
    """The picker lists packs by `language_name`, and skips those without one.

    Without this a pack could translate all forty strings and still never
    appear as an option.
    """
    demo = load_locale(lang).raw("demo", None)
    if not demo:
        pytest.skip(f"the {lang} pack does not translate the page")
    assert demo.get("language_name"), f"{lang} translates the page but never names itself"


@pytest.mark.parametrize("lang", PACKS)
def test_placeholders_the_page_actually_fills(lang):
    """Every {name} in the section is one demo.html passes in.

    A typo here is invisible in review and shows up in the browser as a literal
    "{agent}" in the middle of a sentence.
    """
    demo = load_locale(lang).raw("demo", None)
    if not demo:
        pytest.skip(f"the {lang} pack does not translate the page")
    known = {"agent", "language", "action", "port", "n"}
    for key, value in _flat(demo).items():
        if not isinstance(value, str):
            continue
        unknown = set(re.findall(r"\{(\w+)\}", value)) - known
        assert not unknown, f"{lang}: demo.{key} uses {unknown}, which nothing fills in"
