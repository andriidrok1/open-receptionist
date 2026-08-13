"""voice_lab.py — picking a voice for the agent (ElevenLabs).

The problem it solves: a premade ENGLISH voice reading another language. The
model carries the intonation, but the accent and the melody stay foreign. The
real fix is a native voice from the ElevenLabs library (needs a paid plan and a
key with voices_read/write).

Commands:
    python voice_lab.py list                 # find candidate voices in the library
    python voice_lab.py build                # add to the collection, synthesise, build HTML
    python voice_lab.py build --limit 8 --model eleven_multilingual_v2
    python voice_lab.py serve                # serve the comparison page on :8080
    python voice_lab.py cleanup --keep <id>  # drop every lab voice but the chosen one

The comparison is fair: every voice reads THE SAME two lines, with the same
model and speed as bot.py. The first line is a greeting, the second is a booking
confirmation full of numbers — the hardest thing for TTS to get right.

Which language is being auditioned comes from `--lang` (default en), and with it
the sample phrases and which voices the library is searched for: both live in
`locales/<lang>.yaml` under `voice_lab`. That section is optional — this is a
development tool, so a language pack that omits it still runs a bot, it just
cannot audition voices until someone writes two sentences.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

import tts_settings
from locale_pack import load_locale

SERVER_DIR = Path(__file__).resolve().parent
# Defaults only — the lab exists to try values other than the configured
# ones, and .env winning would make every override silently a no-op.
load_dotenv(SERVER_DIR / ".env", override=False)

API = "https://api.elevenlabs.io/v1"
KEY = os.getenv("ELEVENLABS_API_KEY", "")

OUT_DIR = SERVER_DIR / "voice_lab_out"
STATE_FILE = SERVER_DIR / "voice_lab_state.json"

# The same as in bot.py, so what you hear equals what the agent will say — and
# from the same module, because "the same" written out twice stopped being true.
# The lab used to synthesise at style 0.0 while calls ran at 1.0, which is the
# difference between a flat read and a lively one: voices were being judged on a
# recording the product never makes.
DEFAULT_MODEL = tts_settings.MODEL
DEFAULT_SPEED = tts_settings.SPEED

# Filled in by _use_language() before any command runs. Module-level because the
# HTML page and the accent experiment both quote the phrases back, and threading
# them through six commands buys nothing.
LANG = "en"
SAMPLES: list[tuple[str, str]] = []
#: The voice currently configured for this language, so the question is "is this
#: better than what we ship?" rather than "is this nice?" — the second question
#: has no answer.
BASELINE: dict = {}


def _use_language(lang: str) -> None:
    """Point the lab at one language: its phrases, its voices, its baseline."""
    global LANG, SAMPLES, BASELINE
    pack = load_locale(lang)
    conf = pack.raw("voice_lab", None)
    if not conf:
        sys.exit(
            f"the {lang} pack has no `voice_lab:` section — add search_language, "
            "search_terms and two samples (greeting, booking) to "
            f"locales/{lang}.yaml, or use --lang on a pack that has them"
        )
    samples = conf.get("samples") or {}
    missing = [k for k in ("greeting", "booking") if not samples.get(k)]
    if missing:
        sys.exit(f"locales/{lang}.yaml → voice_lab.samples is missing: {', '.join(missing)}")

    LANG = lang
    SAMPLES = [("greeting", samples["greeting"]), ("booking", samples["booking"])]
    BASELINE = {
        "voice_id": pack.raw("settings.default_voice_id"),
        "name": f"currently shipping ({lang})",
        "baseline": True,
    }


# Ukrainian-only: the same phrases with stress marks (U+0301, combining acute).
# The hypothesis was that the model reads the mark and stops guessing. It did not
# hold — see the note at the top of speech_text.py — but the fixture is kept so
# the experiment can be repeated if the voice or the model ever changes. There is
# no English equivalent, because English orthography does not mark stress.
SAMPLES_ACCENTED = [
    (
        "greeting",
        "До́брий день! Готе́ль «Рів'є́ра», мене́ зва́ти Окса́на. Чим мо́жу допомогти́?",
    ),
    (
        "booking",
        "О́тже, но́мер Покра́щений на двох, з чотирна́дцятого по сімна́дцяте ли́пня, "
        "три но́чі. Ра́зом п'ять ти́сяч чоти́риста гри́вень. Підтве́рджуєте бронюва́ння?",
    ),
]


def _req(method: str, path: str, body=None, raw=False):
    """An API call. Returns (status, payload|bytes|error_text)."""
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"xi-api-key": KEY}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = r.read()
            return r.status, payload if raw else json.loads(payload or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:400]
    except Exception as e:  # noqa: BLE001 — network or timeout, shown as is
        return 0, str(e)[:300]


def check_key():
    if not KEY:
        sys.exit("ELEVENLABS_API_KEY is not set in server/.env")
    st, body = _req("GET", "/user/subscription")
    if st == 401:
        sys.exit(
            "The key does not have the required permissions (401).\n"
            "Create a new one at elevenlabs.io → API Keys with:\n"
            "  Text to Speech, Voices (Read and Write), User (Read)\n"
            "and put it in server/.env → ELEVENLABS_API_KEY\n"
            f"API response: {body}"
        )
    if st != 200:
        sys.exit(f"Could not check the subscription: {st} {body}")
    tier = body.get("tier")
    used, limit = body.get("character_count"), body.get("character_limit")
    print(f"Plan: {tier} | characters: {used}/{limit} | voice limit: {body.get('voice_limit')}")
    return body


def _speaks(v: dict, code: str) -> bool:
    """Whether this library voice is verified in the language being auditioned.

    Two places carry it — the voice's own `language` and the per-language
    verification list — and a voice is worth hearing if either says yes.
    """
    code = code.lower()
    if (v.get("language") or "").lower() == code:
        return True
    return any(
        (lang.get("language") or "").lower() == code
        for lang in v.get("verified_languages") or []
    )


def find_voices(limit: int = 12) -> list[dict]:
    """Candidates from the library: filter by language first, then search by word.

    The word searches are there because the language filter alone misses voices
    whose owner never verified a language but whose description says plainly
    what they are.
    """
    conf = load_locale(LANG).raw("voice_lab")
    code = conf.get("search_language", LANG)
    found: dict[str, dict] = {}
    queries = ["/shared-voices?" + urllib.parse.urlencode({"language": code, "page_size": 100})]
    queries += [
        "/shared-voices?" + urllib.parse.urlencode({"search": term, "page_size": 100})
        for term in conf.get("search_terms") or []
    ]
    for q in queries:
        st, body = _req("GET", q)
        if st != 200 or not isinstance(body, dict):
            print(f"  (query {q.split('?')[1][:40]}… → {st})")
            continue
        for v in body.get("voices", []):
            if _speaks(v, code) and v.get("voice_id") not in found:
                found[v["voice_id"]] = v

    voices = list(found.values())
    # Female voices first (the persona is female), then by popularity.
    voices.sort(
        key=lambda v: (
            0 if (v.get("gender") or "").lower() == "female" else 1,
            -(v.get("cloned_by_count") or 0),
        )
    )
    return voices[:limit]


def cmd_list(args):
    check_key()
    voices = find_voices(args.limit)
    if not voices:
        print("\nNo voices found — check the key permissions or try again later.")
        return
    print(f"\nFound {len(voices)} voices:\n")
    for i, v in enumerate(voices, 1):
        langs = ",".join(
            sorted({(x.get("language") or "") for x in (v.get("verified_languages") or [])})
        )
        print(
            f"{i:2}. {v.get('name'):<22} {(v.get('gender') or '?'):<7}"
            f" {(v.get('age') or '?'):<12} clones: {v.get('cloned_by_count') or 0:<6}"
            f" languages: {langs or v.get('language')}"
        )
        if v.get("description"):
            print(f"    {v['description'][:110]}")


def _pick_voice(spec: str, state: dict) -> dict:
    """One voice from the collection, named by id or by part of its name.

    Refuses to guess. `--only Eryn` matched "Kat-eryn-a Pavlenko" first, so two
    whole rounds of style comparison were synthesised on a Ukrainian voice
    reading English — and nothing said so, because the filenames carry ids and
    the page carries settings. A silently wrong subject is the worst outcome an
    experiment can have: the numbers look fine and the conclusion is rubbish.

    A prefix match wins outright, since that is what someone typing a name
    means. Anything else must be unambiguous.
    """
    voices = list(state["added"].values())
    low = spec.lower()

    exact = [v for v in voices if v["voice_id"] == spec]
    prefix = [v for v in voices if v["name"].lower().startswith(low)]
    partial = [v for v in voices if low in v["name"].lower()]

    for group in (exact, prefix, partial):
        if len(group) == 1:
            return group[0]
        if len(group) > 1:
            names = "\n  ".join(f'{v["name"]}  ({v["voice_id"]})' for v in group)
            sys.exit(f'"{spec}" matches more than one voice — be more specific:\n  {names}')

    sys.exit(f'"{spec}" is not in the collection. Known:\n  ' +
             "\n  ".join(v["name"] for v in voices))


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"added": {}}


def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def add_to_collection(v: dict, state: dict) -> str | None:
    """A library voice has to be added to your own collection first — only then
    does it get a voice_id usable with /text-to-speech."""
    public_owner = v.get("public_owner_id")
    lib_id = v.get("voice_id")
    if lib_id in state["added"]:
        return state["added"][lib_id]["voice_id"]

    name = f"lab_{v.get('name', 'voice')}"[:40]
    # Adding sometimes hits a timeout (ElevenLabs copies the voice into the
    # account) — try three times, otherwise a voice silently drops out of the
    # comparison.
    for attempt in range(3):
        st, body = _req("POST", f"/voices/add/{public_owner}/{lib_id}", {"new_name": name})
        if st == 200 and isinstance(body, dict):
            break
        print(f"  … {v.get('name')}: attempt {attempt + 1}/3 failed ({st})")
    if st != 200 or not isinstance(body, dict):
        print(f"  ! {v.get('name')}: could not be added ({st}) {body if isinstance(body, str) else ''}")
        return None
    own_id = body.get("voice_id")
    state["added"][lib_id] = {"voice_id": own_id, "name": v.get("name")}
    _save_state(state)
    return own_id


def synth(
    voice_id: str,
    text: str,
    model: str,
    speed: float,
    dest: Path,
    stability: float | None = None,
    style: float | None = None,
) -> bool:
    """Synthesise one sample.

    stability and style default to what a real call uses. They used to default
    to 0.5 and 0.0 — plausible numbers, and neither of them the product's — so
    the whole comparison was made on a voice nobody would ever hear. The
    experiment commands below still pass their own values on purpose; `build`
    must not.
    """
    settings = tts_settings.as_dict()
    settings["speed"] = speed
    if stability is not None:
        settings["stability"] = stability
    if style is not None:
        settings["style"] = style
    st, body = _req(
        "POST",
        f"/text-to-speech/{voice_id}?output_format=mp3_44100_128",
        {"text": text, "model_id": model, "voice_settings": settings},
        raw=True,
    )
    if st != 200:
        msg = body.decode(errors="replace")[:200] if isinstance(body, bytes) else body
        print(f"  ! TTS {voice_id[:8]}… → {st} {msg}")
        return False
    dest.write_bytes(body)
    return True


def _account_voices(spec: str | None) -> list[dict]:
    """Voices already in the account, named by id or by part of their name.

    Matching by name is what makes this usable from a terminal: `--also
    jessica,bella` reads, `--also cgSgspJ2msm6clMCkdW9,hpp4J3VqNfWAUOO0d1Us`
    does not, and a typo in a raw id fails silently as a 404 during synthesis.
    """
    if not spec:
        return []
    st, body = _req("GET", "/voices")
    if st != 200 or not isinstance(body, dict):
        print(f"  ! could not read the account's voices ({st}) — skipping --also")
        return []
    have = body.get("voices", [])
    out, seen = [], set()
    for want in [s.strip() for s in spec.split(",") if s.strip()]:
        low = want.lower()
        hit = next(
            (v for v in have if v["voice_id"] == want or low in (v.get("name") or "").lower()),
            None,
        )
        if not hit:
            print(f"  ! no voice in the account matches {want!r}")
            continue
        if hit["voice_id"] in seen:
            continue
        seen.add(hit["voice_id"])
        labels = hit.get("labels") or {}
        out.append(
            {
                "voice_id": hit["voice_id"],
                "name": hit.get("name"),
                "gender": labels.get("gender", ""),
                "age": labels.get("age", ""),
                "accent": labels.get("accent", ""),
                "description": hit.get("description") or labels.get("description") or "",
                "in_account": True,
            }
        )
    return out


def cmd_build(args):
    check_key()
    state = _load_state()
    OUT_DIR.mkdir(exist_ok=True)

    # --pick refers to the numbering from `list` (the same sort order), so pull
    # the wide list first and narrow it down afterwards.
    if args.pick:
        idx = [int(x) for x in args.pick.split(",")]
        pool = find_voices(max(idx))
        voices = [pool[i - 1] for i in idx if 0 < i <= len(pool)]
    elif args.limit == 0:
        voices = []
    else:
        voices = find_voices(args.limit)

    # Voices that are ALREADY in the account — the premade set, or ones a
    # previous audition kept. They cost no slot, and plans cap how many custom
    # voices may be held at once: with the cap nearly full, adding one more from
    # the library means deleting a voice another language is using. So they are
    # auditioned in the same comparison rather than being off limits.
    voices = _account_voices(args.also) + voices
    if not voices:
        sys.exit("No candidates — run `python voice_lab.py list` first")

    print(f"\nSynthesising {len(voices)} voices + baseline\n  {tts_settings.summary()}\n")
    rows = []

    for v in [BASELINE] + voices:
        name = v.get("name")
        if v.get("baseline") or v.get("in_account"):
            vid = v["voice_id"]
        else:
            vid = add_to_collection(v, state)
            if not vid:
                continue
        files = {}
        ok = True
        for key, text in SAMPLES:
            fname = f"{vid}_{key}.mp3"
            if not synth(vid, text, args.model, args.speed, OUT_DIR / fname):
                ok = False
                break
            files[key] = fname
        if not ok:
            continue
        rows.append(
            {
                "name": name,
                "voice_id": vid,
                "gender": v.get("gender", ""),
                "age": v.get("age", ""),
                "accent": v.get("accent", ""),
                "description": (v.get("description") or "")[:160],
                "baseline": bool(v.get("baseline")),
                "files": files,
            }
        )
        print(f"  ✓ {name}")

    if not rows:
        sys.exit("Nothing was synthesised — see the errors above.")

    _write_html(rows, args.model, args.speed)
    print(f"\nDone: {len(rows)} voices → {OUT_DIR / 'index.html'}")
    print("Listen:  python voice_lab.py serve   → http://localhost:8080")


def _write_html(rows, model, speed):
    # On the page, not just in the terminal: a comparison is only meaningful
    # alongside the settings it was made at, and those are what made the first
    # version of this page misleading.
    settings = f"{model} · speed {speed} · stability {tts_settings.STABILITY} · style {tts_settings.STYLE}"

    def card(r):
        tag = (
            '<span class="badge base">current</span>'
            if r["baseline"]
            else f'<span class="badge">{r["gender"]} · {r["age"]}</span>'
        )
        players = "".join(
            f'<div class="row"><span class="lbl">{lbl}</span>'
            f'<audio controls preload="none" src="{r["files"][key]}"></audio></div>'
            for key, lbl in (("greeting", "greeting"), ("booking", "booking + numbers"))
        )
        return f"""
        <div class="card{' baseline' if r['baseline'] else ''}">
          <div class="head"><h2>{r['name']}</h2>{tag}</div>
          <div class="desc">{r['description'] or '&nbsp;'}</div>
          {players}
          <code class="vid">{r['voice_id']}</code>
        </div>"""

    html = f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<title>Voice comparison</title>
<style>
  body {{ font: 15px/1.5 -apple-system, system-ui, sans-serif; background:#0f1115; color:#e7e9ee;
         margin:0; padding:32px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:#8b93a7; margin-bottom:24px; font-size:13px; }}
  .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); }}
  .card {{ background:#171a21; border:1px solid #242938; border-radius:12px; padding:16px; }}
  .card.baseline {{ border-color:#4a3a1f; background:#1b1810; }}
  .head {{ display:flex; align-items:center; justify-content:space-between; gap:8px; }}
  h2 {{ font-size:16px; margin:0; }}
  .badge {{ font-size:11px; color:#8b93a7; background:#222736; padding:3px 8px; border-radius:20px; }}
  .badge.base {{ color:#e0b155; background:#332a16; }}
  .desc {{ color:#8b93a7; font-size:12px; margin:8px 0 12px; min-height:2.4em; }}
  .row {{ display:flex; align-items:center; gap:10px; margin-bottom:8px; }}
  .lbl {{ font-size:11px; color:#8b93a7; width:96px; flex:none; }}
  audio {{ width:100%; height:32px; }}
  .vid {{ display:block; margin-top:10px; font-size:11px; color:#5c6479; }}
  .txt {{ background:#171a21; border:1px solid #242938; border-radius:12px; padding:14px 16px;
          margin-bottom:20px; font-size:13px; color:#b9c0d0; }}
  .txt b {{ color:#e7e9ee; }}
</style></head><body>
<h1>Voice comparison</h1>
<div class="sub">{settings} · every voice reads the same text</div>
<div class="txt">
  <b>greeting:</b> {SAMPLES[0][1]}<br>
  <b>booking + numbers:</b> {SAMPLES[1][1]}
</div>
<div class="grid">{''.join(card(r) for r in rows)}</div>
</body></html>"""
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")


# Two axes of intonation: how the text is written, and how the voice is tuned.
# "formal" is what an LLM writes by default: one long sentence, all commas.
# "live" is the same content the way a receptionist actually says it: short
# phrases, an ellipsis for a pause, a dash for a change of intonation, and a
# short question at the end.
PROSODY_TEXTS = {
    "formal": (
        "Отже, номер Покращений на двох, з чотирнадцятого по сімнадцяте липня, "
        "три ночі. Разом п'ять тисяч чотириста гривень. Підтверджуєте бронювання?"
    ),
    "live": (
        "Так, дивлюся... Покращений на двох є — з чотирнадцятого по сімнадцяте. "
        "Три ночі, п'ять тисяч чотириста. Бронюємо?"
    ),
}

# stability: lower = livelier and more emotional, higher = flat newsreader.
# style: expression above the default (and more expensive in latency).
PROSODY_SETTINGS = [
    ("stab 0.3", 0.3, 0.0),
    ("stab 0.5 (default)", 0.5, 0.0),
    ("stab 0.8", 0.8, 0.0),
    ("stab 0.4 + style", 0.4, 0.35),
]


# Rhythm is punctuation. The model does not understand meaning, it voices the
# marks: a comma is a micro-pause, a full stop a longer one, a dash or an
# ellipsis a change of intonation. "The pause is in the wrong place" almost
# always means "the comma is in the wrong place". Four ways to control it:
PAUSE_TEXTS = {
    "comma": (
        "Записую: Покращений, на двох, з чотирнадцятого по сімнадцяте, три ночі, "
        "п'ять тисяч чотириста гривень, телефон нуль шістдесят сім, двісті тридцять чотири, "
        "п'ятдесят шість, сімдесят вісім."
    ),
    "clean": (
        "Записую Покращений на двох з чотирнадцятого по сімнадцяте. Три ночі "
        "п'ять тисяч чотириста гривень. Телефон нуль шістдесят сім двісті тридцять чотири "
        "п'ятдесят шість сімдесят вісім."
    ),
    "dash": (
        "Записую: Покращений на двох з чотирнадцятого по сімнадцяте — три ночі. "
        "П'ять тисяч чотириста гривень... Телефон: нуль шістдесят сім — двісті тридцять чотири — "
        "п'ятдесят шість — сімдесят вісім."
    ),
    "break": (
        'Записую: Покращений на двох з чотирнадцятого по сімнадцяте.<break time="0.4s" /> '
        'Три ночі, п\'ять тисяч чотириста гривень.<break time="0.5s" /> Телефон: '
        'нуль шістдесят сім<break time="0.3s" /> двісті тридцять чотири<break time="0.3s" /> '
        'п\'ятдесят шість<break time="0.3s" /> сімдесят вісім.'
    ),
}

PAUSE_LABELS = {
    "comma": "how an LLM writes by default — everything through commas",
    "clean": "minimal commas: digit groups joined, a full stop between blocks",
    "dash": "dashes and ellipses — a change of intonation instead of a pause",
    "break": "explicit break tags in exact places",
}


def cmd_models(args):
    """The same voice, the same line, on several synthesis models.

    Flash is chosen for latency, and the question this answers is what that
    costs in quality — and whether a heavier model is worth it when latency is
    not the binding constraint. Since our replies carry pause markup, it also
    shows whether break tags survive: they are documented for some models and
    silently ignored by others, and "silently" means the only way to find out is
    to listen.
    """
    check_key()
    state = _load_state()
    target = _pick_voice(args.only, state)
    OUT_DIR.mkdir(exist_ok=True)

    text = dict(SAMPLES)["booking"]
    if args.breaks:
        # Our own pause layer inserts these on every booking recap, so the
        # comparison should contain one — a model that reads "break time zero
        # point four s" aloud is unusable for us whatever it sounds like.
        text = text.replace(", ", ',<break time="0.4s" /> ', 1)

    rows = []
    for model in args.models.split(","):
        model = model.strip()
        fname = f"model_{target['voice_id']}_{model}.mp3"
        # speed is not accepted by every model (v3 has no such parameter), so a
        # rejection is reported rather than silently retried without it: a
        # comparison at a different speed is not a comparison.
        if not synth(target["voice_id"], text, model, args.speed, OUT_DIR / fname):
            rows.append({"model": model, "file": None, "ttfb": None, "note": "refused"})
            print(f"  ✗ {model} — refused, see the error above")
            continue
        runs = [
            _ttfb_ms(target["voice_id"], text, model, args.speed,
                     tts_settings.STABILITY, tts_settings.STYLE)
            for _ in range(3)
        ]
        runs = [r for r in runs if r]
        med = sorted(runs)[len(runs) // 2] if runs else None
        rows.append({"model": model, "file": fname, "ttfb": med, "note": ""})
        print(f"  ✓ {model}" + (f" — median TTFB {med:.0f}ms" if med else ""))

    cards = "".join(
        f'<div class=card><div class=k>{r["model"]}'
        + (f' <span>· TTFB {r["ttfb"]:.0f}ms</span>' if r["ttfb"] else "")
        + (f' <span>· {r["note"]}</span>' if r["note"] else "")
        + "</div>"
        + (f'<audio controls preload="none" src="{r["file"]}"></audio>' if r["file"] else "")
        + "</div>"
        for r in rows
    )
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>synthesis models</title>
<style>
  body {{ font:15px/1.5 -apple-system, system-ui, sans-serif; background:#0f1115; color:#e7e9ee;
         margin:0; padding:32px; max-width:760px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:#8b93a7; font-size:13px; margin-bottom:8px; }}
  .txt {{ background:#171a21; border:1px solid #242938; border-radius:12px; padding:14px 16px;
          margin-bottom:20px; font-size:13px; color:#b9c0d0; }}
  .card {{ background:#171a21; border:1px solid #242938; border-radius:12px; padding:14px 16px;
           margin-bottom:12px; }}
  .k {{ font-size:14px; margin-bottom:8px; }} .k span {{ color:#8b93a7; font-size:12px; }}
  audio {{ width:100%; height:34px; }}
</style></head><body>
<h1>synthesis models</h1>
<div class="sub">{target['name']} · speed {args.speed} · stability {tts_settings.STABILITY}
 · style {tts_settings.STYLE}</div>
<div class="txt">{text.replace("<", "&lt;")}</div>
{cards}</body></html>"""
    (OUT_DIR / "models.html").write_text(html, encoding="utf-8")
    print(f"\nDone → {OUT_DIR / 'models.html'}")


def _ttfb_ms(voice_id: str, text: str, model: str, speed: float, stability: float, style: float):
    """Time to the first chunk of audio — what is actually felt in conversation."""
    import time

    body = {
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": 0.75,
            "style": style,
            "speed": speed,
        },
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{API}/text-to-speech/{voice_id}/stream?output_format=mp3_22050_32",
        data=data,
        headers={"xi-api-key": KEY, "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read(1024)
            first = (time.perf_counter() - t0) * 1000
            r.read()
        return first
    except Exception:  # noqa: BLE001
        return None


def cmd_final(args):
    """The summary: production settings from .env, lines before and after the
    text layer.

    On the left is the raw text from the LLM, on the right the same text after
    speech_text has processed it.
    """
    check_key()
    sys.path.insert(0, str(SERVER_DIR))
    from speech_text import SpeechTextFilter

    state = _load_state()
    target = _pick_voice(args.only, state)
    OUT_DIR.mkdir(exist_ok=True)

    stab, style = tts_settings.STABILITY, tts_settings.STYLE
    speech = SpeechTextFilter()

    lines = [
        "Готель «Рів'єра», мене звати Оксана. Чим можу допомогти?",
        "Так, дивлюся — Покращений на двох є, з чотирнадцятого по сімнадцяте. "
        "Три ночі, п'ять тисяч чотириста. Бронюємо?",
        'Телефон повторюю: нуль шість сім <break time="0.4s" /> сім сім сім '
        "чотири п'ять шість сім. Все вірно?",
        "Все готово — номер броні шістсот п'ятдесят дві тисячі триста п'ятдесят два. "
        "Чекаємо вас чотирнадцятого!",
    ]

    rows = []
    for i, raw in enumerate(lines):
        prepared = speech.prepare(raw)
        pair = {}
        for key, text in (("raw", raw), ("prepared", prepared)):
            fname = f"final_{i}_{key}.mp3"
            if synth(target["voice_id"], text, args.model, args.speed,
                     OUT_DIR / fname, stab, style):
                pair[key] = fname
        rows.append({"raw": raw, "prepared": prepared, "files": pair})
        print(f"  ✓ line {i + 1}")

    def card(r):
        def side(key, label, cls):
            f = r["files"].get(key)
            txt = (r["raw"] if key == "raw" else r["prepared"]).replace("<", "&lt;")
            return (
                f'<div class="side {cls}"><div class=lb>{label}</div>'
                f'<div class=tx>{txt}</div>'
                + (f'<audio controls preload="none" src="{f}"></audio>' if f else "—")
                + "</div>"
            )

        return (
            '<div class=card>'
            + side("raw", "as the LLM writes it", "a")
            + side("prepared", "after the text layer", "b")
            + "</div>"
        )

    html = f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><title>Summary — before and after</title>
<style>
  body {{ font:15px/1.5 -apple-system, system-ui, sans-serif; background:#0f1115; color:#e7e9ee;
         margin:0; padding:32px; max-width:1000px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:#8b93a7; font-size:13px; margin-bottom:24px; }}
  .card {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:16px; }}
  .side {{ background:#171a21; border:1px solid #242938; border-radius:12px; padding:14px 16px; }}
  .side.b {{ background:#141b16; border-color:#25402c; }}
  .lb {{ font-size:11px; color:#8b93a7; margin-bottom:6px; }}
  .tx {{ font-size:13px; color:#b9c0d0; margin-bottom:10px; min-height:3em; }}
  audio {{ width:100%; height:32px; }}
  @media (max-width:760px) {{ .card {{ grid-template-columns:1fr; }} }}
</style></head><body>
<h1>Summary — before and after</h1>
<div class="sub">{target['name']} · stability {stab} · style {style} · {args.model}</div>
{''.join(card(r) for r in rows)}
</body></html>"""
    (OUT_DIR / "final.html").write_text(html, encoding="utf-8")
    print(f"\nDone → {OUT_DIR / 'final.html'}")


def cmd_style(args):
    """How much expression `style` buys — and what it costs in milliseconds."""
    check_key()
    state = _load_state()
    target = _pick_voice(args.only, state)
    OUT_DIR.mkdir(exist_ok=True)

    # The booking sample, not the greeting: ElevenLabs' own documentation says
    # style above 0 makes the model "slightly less stable", and instability
    # surfaces where there is no meaning to fall back on — a date, a price, a
    # reference code. A greeting would hide exactly the failure being tested.
    text = PAUSE_TEXTS["break"] if args.breaks else dict(SAMPLES)["booking"]
    rows = []
    # A value is either a plain style ("0.7") or a style@stability pair
    # ("1.0@0.55"): high style with low stability tends to wander, so it is worth
    # hearing both.
    for item in args.values.split(","):
        style_s, _, stab_s = item.partition("@")
        style = float(style_s)
        stab = float(stab_s) if stab_s else args.stability
        # Several takes of the SAME setting, because stability is defined as the
        # randomness BETWEEN generations. One clip per setting shows you one roll
        # of the dice and tells you nothing about the spread — which is the whole
        # quantity being set. ElevenLabs say as much: "generate a few times until
        # you find a performance you like". A low-stability setting that sounds
        # perfect once and wanders on the next take is a bad setting, and a
        # single sample cannot show that.
        files = []
        for take in range(args.takes):
            fname = f"style_{target['voice_id']}_{style}_{stab}_{take + 1}.mp3"
            if synth(
                target["voice_id"], text, args.model, args.speed, OUT_DIR / fname, stab, style
            ):
                files.append(fname)
        if not files:
            continue
        runs = [
            _ttfb_ms(target["voice_id"], text, args.model, args.speed, stab, style)
            for _ in range(3)
        ]
        runs = [r for r in runs if r]
        med = sorted(runs)[len(runs) // 2] if runs else None
        rows.append({"style": style, "stab": stab, "file": files[0], "files": files, "ttfb": med})
        print(
            f"  ✓ style {style} / stab {stab} — {len(files)} takes"
            + (f", median TTFB {med:.0f}ms" if med else "")
        )

    if not rows:
        sys.exit("Nothing was synthesised.")

    base = rows[0]["ttfb"] or 0
    cards = "".join(
        f'<div class=card><div class=k>style {r["style"]} · stab {r["stab"]}'
        + (
            f' <span>· TTFB {r["ttfb"]:.0f}ms'
            + (f' (+{r["ttfb"] - base:.0f}ms)' if r["style"] and base else "")
            + "</span>"
            if r["ttfb"]
            else ""
        )
        + "</div>"
        + "".join(
            f'<audio controls preload="none" src="{f}"></audio>' for f in r.get("files", [r["file"]])
        )
        + "</div>"
        for r in rows
    )
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>style — expression vs latency</title>
<style>
  body {{ font:15px/1.5 -apple-system, system-ui, sans-serif; background:#0f1115; color:#e7e9ee;
         margin:0; padding:32px; max-width:760px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:#8b93a7; font-size:13px; margin-bottom:8px; }}
  .txt {{ background:#171a21; border:1px solid #242938; border-radius:12px; padding:14px 16px;
          margin-bottom:20px; font-size:13px; color:#b9c0d0; }}
  .card {{ background:#171a21; border:1px solid #242938; border-radius:12px; padding:14px 16px;
           margin-bottom:12px; }}
  .k {{ font-size:14px; margin-bottom:8px; }} .k span {{ color:#8b93a7; font-size:12px; }}
  audio {{ width:100%; height:34px; }}
</style></head><body>
<h1>style — expression vs latency</h1>
<div class="sub">{target['name']} · {args.model} · speed {args.speed} · {args.takes} takes per setting</div>
<div class="txt" style="border-color:#3a3050">Several takes of the SAME setting on purpose: stability
IS the randomness between generations, so the spread is the thing being set. Judge a setting by its
WORST take, not its best.</div>
<div class="txt">{text.replace("<", "&lt;")}</div>
{cards}</body></html>"""
    (OUT_DIR / "style.html").write_text(html, encoding="utf-8")
    print(f"\nDone → {OUT_DIR / 'style.html'}")


def cmd_words(args):
    """Hunting down "broken" words by separating the likely causes one by one.

    The hypotheses, in order of likelihood:
      1. Too many break tags — ElevenLabs itself warns that an excess makes
         speech race and produces artifacts.
      2. The apostrophe: the text uses the ASCII variant while the typographic
         one is different, and the model may read the first as a word break.
      3. Low stability — at 0.4 the model occasionally wanders on one word.
      4. flash is the fast, less accurate model; multilingual_v2 is tidier.
    """
    check_key()
    state = _load_state()
    target = _pick_voice(args.only, state)
    OUT_DIR.mkdir(exist_ok=True)

    base = PAUSE_TEXTS["break"]
    few_breaks = base.replace('<break time="0.3s" /> ', "")  # only the two long ones remain
    typographic = base.replace("'", "’")

    variants = [
        ("baseline", base, args.model, 0.4, "what you heard: five break tags, stability 0.4"),
        ("apostrophe", typographic, args.model, 0.4, "the same text with a typographic apostrophe (U+2019)"),
        ("few_breaks", few_breaks, args.model, 0.4, "only two break tags instead of five"),
        ("stable", base, args.model, 0.65, "stability 0.65 — less wandering on individual words"),
        ("multilingual", base, "eleven_multilingual_v2", 0.4, "a more accurate model, +200-300 ms"),
    ]

    rows = []
    for key, text, model, stab, note in variants:
        fname = f"words_{target['voice_id']}_{key}.mp3"
        if synth(target["voice_id"], text, model, args.speed, OUT_DIR / fname, stab, 0.0):
            rows.append({"key": key, "note": note, "file": fname, "text": text})
            print(f"  ✓ {key}")

    if not rows:
        sys.exit("Nothing was synthesised.")

    cards = "".join(
        f'<div class=card><div class=k>{r["key"]} — <span>{r["note"]}</span></div>'
        f'<div class=tx>{r["text"].replace("<", "&lt;")}</div>'
        f'<audio controls preload="none" src="{r["file"]}"></audio></div>'
        for r in rows
    )
    html = f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><title>Broken words — diagnostics</title>
<style>
  body {{ font:15px/1.5 -apple-system, system-ui, sans-serif; background:#0f1115; color:#e7e9ee;
         margin:0; padding:32px; max-width:820px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:#8b93a7; font-size:13px; margin-bottom:24px; }}
  .card {{ background:#171a21; border:1px solid #242938; border-radius:12px; padding:16px;
           margin-bottom:14px; }}
  .k {{ font-size:13px; margin-bottom:8px; }} .k span {{ color:#8b93a7; font-weight:400; }}
  .tx {{ font-size:12px; color:#8b93a7; margin-bottom:10px; }}
  audio {{ width:100%; height:34px; }}
</style></head><body>
<h1>Broken words — diagnostics</h1>
<div class="sub">{target['name']} · listen to the same words that broke in the baseline</div>
{cards}</body></html>"""
    (OUT_DIR / "words.html").write_text(html, encoding="utf-8")
    print(f"\nDone → {OUT_DIR / 'words.html'}")


def cmd_pause(args):
    """Comparing the ways of controlling rhythm on a single voice."""
    check_key()
    state = _load_state()
    target = _pick_voice(args.only, state)
    OUT_DIR.mkdir(exist_ok=True)

    rows = []
    for key, text in PAUSE_TEXTS.items():
        fname = f"pause_{target['voice_id']}_{key}.mp3"
        if synth(
            target["voice_id"], text, args.model, args.speed,
            OUT_DIR / fname, args.stability, 0.0,
        ):
            rows.append({"key": key, "text": text, "file": fname})
            print(f"  ✓ {key}")

    if not rows:
        sys.exit("Nothing was synthesised.")

    cards = "".join(
        f'<div class=card><div class=k>{r["key"]} — <span>{PAUSE_LABELS[r["key"]]}</span></div>'
        f'<div class=tx>{r["text"].replace("<", "&lt;")}</div>'
        f'<audio controls preload="none" src="{r["file"]}"></audio></div>'
        for r in rows
    )
    html = f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><title>Pauses — comparison</title>
<style>
  body {{ font:15px/1.5 -apple-system, system-ui, sans-serif; background:#0f1115; color:#e7e9ee;
         margin:0; padding:32px; max-width:820px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:#8b93a7; font-size:13px; margin-bottom:24px; }}
  .card {{ background:#171a21; border:1px solid #242938; border-radius:12px; padding:16px;
           margin-bottom:14px; }}
  .k {{ font-size:13px; margin-bottom:8px; }} .k span {{ color:#8b93a7; font-weight:400; }}
  .tx {{ font-size:13px; color:#b9c0d0; margin-bottom:10px; }}
  audio {{ width:100%; height:34px; }}
</style></head><body>
<h1>Pauses — comparison</h1>
<div class="sub">{target['name']} · {args.model} · speed {args.speed}</div>
{cards}</body></html>"""
    (OUT_DIR / "pause.html").write_text(html, encoding="utf-8")
    print(f"\nDone → {OUT_DIR / 'pause.html'}")


def cmd_prosody(args):
    """An intonation matrix: voice × settings × text style."""
    check_key()
    state = _load_state()
    only = set(args.only.split(","))
    OUT_DIR.mkdir(exist_ok=True)

    targets = [i for i in state["added"].values() if any(o.lower() in i["name"].lower() for o in only)]
    if not targets:
        sys.exit(f"No voices matched {args.only} — see `voice_lab.py list`")

    rows = []
    for info in targets:
        vid, name = info["voice_id"], info["name"]
        cells = {}
        for label, stab, style in PROSODY_SETTINGS:
            for tkey, text in PROSODY_TEXTS.items():
                fname = f"pros_{vid}_{stab}_{style}_{tkey}.mp3"
                if synth(vid, text, args.model, args.speed, OUT_DIR / fname, stab, style):
                    cells[f"{label}|{tkey}"] = fname
        if cells:
            rows.append({"name": name, "cells": cells})
            print(f"  ✓ {name} — {len(cells)} variants")

    if not rows:
        sys.exit("Nothing was synthesised.")
    _write_prosody_html(rows, args)
    print(f"\nDone → {OUT_DIR / 'prosody.html'}")
    print("Listen:  python voice_lab.py serve   → http://localhost:8080/prosody.html")


def _write_prosody_html(rows, args):
    def block(r):
        lines = ""
        for label, _stab, _style in PROSODY_SETTINGS:
            cells = ""
            for tkey, tname in (("formal", "formal"), ("live", "lively")):
                f = r["cells"].get(f"{label}|{tkey}")
                cells += (
                    f'<td class="{tkey}"><span class=t>{tname}</span>'
                    + (f'<audio controls preload="none" src="{f}"></audio>' if f else "—")
                    + "</td>"
                )
            lines += f"<tr><td class=set>{label}</td>{cells}</tr>"
        return f'<h2>{r["name"]}</h2><table>{lines}</table>'

    html = f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<title>Intonation — matrix</title>
<style>
  body {{ font:15px/1.5 -apple-system, system-ui, sans-serif; background:#0f1115; color:#e7e9ee;
         margin:0; padding:32px; max-width:900px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  h2 {{ font-size:16px; margin:28px 0 8px; }}
  .sub {{ color:#8b93a7; font-size:13px; margin-bottom:20px; }}
  .txt {{ background:#171a21; border:1px solid #242938; border-radius:12px; padding:14px 16px;
          margin-bottom:8px; font-size:13px; color:#b9c0d0; }}
  .txt b {{ color:#e7e9ee; }} .txt .l {{ color:#7fd18a; }}
  table {{ border-collapse:collapse; width:100%; }}
  td {{ padding:8px 10px; border-bottom:1px solid #1e2331; vertical-align:middle; }}
  td.set {{ font-size:12px; color:#8b93a7; white-space:nowrap; width:130px; }}
  td.live {{ background:#141b16; }}
  span.t {{ display:block; font-size:10px; color:#5c6479; margin-bottom:2px; }}
  audio {{ width:230px; height:32px; }}
</style></head><body>
<h1>Intonation — matrix</h1>
<div class="sub">model {args.model} · speed {args.speed} · no stress marks, so the effects do not mix</div>
<div class="txt"><b>formal:</b> {PROSODY_TEXTS['formal']}</div>
<div class="txt"><b class=l>lively:</b> <span class=l>{PROSODY_TEXTS['live']}</span></div>
{''.join(block(r) for r in rows)}
</body></html>"""
    (OUT_DIR / "prosody.html").write_text(html, encoding="utf-8")


def cmd_accent(args):
    """A matrix for hunting stress placement: voice × model × (marked / raw).

    Listen to the line with numbers and dates in it — that is where TTS guesses
    stress worst.
    """
    check_key()
    state = _load_state()
    if not state["added"]:
        sys.exit("No voices added yet — run `python voice_lab.py build` first")

    models = args.models.split(",")
    only = set(args.only.split(",")) if args.only else None
    OUT_DIR.mkdir(exist_ok=True)

    plain = dict(SAMPLES)
    accented = dict(SAMPLES_ACCENTED)
    variants = [("plain", plain), ("accent", accented)]

    rows = []
    for info in state["added"].values():
        if only and not any(o.lower() in info["name"].lower() for o in only):
            continue
        vid, name = info["voice_id"], info["name"]
        cells = {}
        for model in models:
            for vkey, texts in variants:
                fname = f"acc_{vid}_{model}_{vkey}.mp3"
                if synth(vid, texts[args.sample], model, args.speed, OUT_DIR / fname):
                    cells[f"{model}|{vkey}"] = fname
        if cells:
            rows.append({"name": name, "voice_id": vid, "cells": cells})
            print(f"  ✓ {name} — {len(cells)} variants")

    if not rows:
        sys.exit("Nothing was synthesised — see the errors above.")

    _write_accent_html(rows, models, args)
    print(f"\nDone → {OUT_DIR / 'accent.html'}")
    print("Listen:  python voice_lab.py serve   → http://localhost:8080/accent.html")


def _write_accent_html(rows, models, args):
    plain_text = dict(SAMPLES)[args.sample]
    acc_text = dict(SAMPLES_ACCENTED)[args.sample]

    def cell(r, model, vkey):
        f = r["cells"].get(f"{model}|{vkey}")
        return f'<audio controls preload="none" src="{f}"></audio>' if f else "—"

    head_cells = "".join(
        f"<th>{m.replace('eleven_', '')}<br><span>no stress marks</span></th>"
        f"<th class=acc>{m.replace('eleven_', '')}<br><span>with stress marks</span></th>"
        for m in models
    )
    body = "".join(
        f"<tr><td class=nm>{r['name']}</td>"
        + "".join(
            f"<td>{cell(r, m, 'plain')}</td><td class=acc>{cell(r, m, 'accent')}</td>"
            for m in models
        )
        + "</tr>"
        for r in rows
    )

    html = f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<title>Stress marks — matrix</title>
<style>
  body {{ font:15px/1.5 -apple-system, system-ui, sans-serif; background:#0f1115; color:#e7e9ee;
         margin:0; padding:32px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:#8b93a7; font-size:13px; margin-bottom:20px; }}
  .txt {{ background:#171a21; border:1px solid #242938; border-radius:12px; padding:14px 16px;
          margin-bottom:20px; font-size:13px; color:#b9c0d0; }}
  .txt b {{ color:#e7e9ee; }} .txt .a {{ color:#7fd18a; }}
  table {{ border-collapse:collapse; width:100%; }}
  th, td {{ padding:10px 12px; border-bottom:1px solid #242938; text-align:left;
            vertical-align:middle; }}
  th {{ font-size:12px; color:#8b93a7; font-weight:600; }}
  th span {{ font-weight:400; font-size:11px; }}
  td.nm {{ font-size:13px; white-space:nowrap; padding-right:20px; }}
  .acc {{ background:#141b16; }}
  audio {{ width:220px; height:32px; }}
</style></head><body>
<h1>Stress marks — matrix</h1>
<div class="sub">speed {args.speed} · the green column is the same text with stress marks</div>
<div class="txt">
  <b>plain:</b> {plain_text}<br>
  <b class="a">with stress marks:</b> <span class="a">{acc_text}</span>
</div>
<table><thead><tr><th>voice</th>{head_cells}</tr></thead><tbody>{body}</tbody></table>
</body></html>"""
    (OUT_DIR / "accent.html").write_text(html, encoding="utf-8")


def cmd_serve(args):
    import functools
    import http.server
    import socketserver

    if not (OUT_DIR / "index.html").exists():
        sys.exit("No page yet — run `python voice_lab.py build` first")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(OUT_DIR))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", args.port), handler) as httpd:
        print(f"http://localhost:{args.port}  (Ctrl+C to stop)")
        httpd.serve_forever()


def cmd_cleanup(args):
    """Drop every lab voice from the collection but the chosen one (plans cap
    how many voices you may keep)."""
    check_key()
    state = _load_state()
    keep = args.keep
    for lib_id, info in list(state["added"].items()):
        if info["voice_id"] == keep:
            print(f"  keeping {info['name']} ({keep})")
            continue
        st, body = _req("DELETE", f"/voices/{info['voice_id']}")
        if st == 200:
            print(f"  removed {info['name']}")
            del state["added"][lib_id]
        else:
            print(f"  ! {info['name']}: {st} {body}")
    _save_state(state)


def main():
    p = argparse.ArgumentParser(description="Voice selection lab (ElevenLabs)")
    p.add_argument(
        "--lang",
        default=os.getenv("VOICE_LAB_LANG", "en"),
        help="which language pack to audition voices for (default: en)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="find candidate voices in the library")
    pl.add_argument("--limit", type=int, default=12)
    pl.set_defaults(func=cmd_list)

    pb = sub.add_parser("build", help="add, synthesise and build the page")
    pb.add_argument("--limit", type=int, default=6, help="how many library voices (each costs ~360 characters); 0 for none")
    pb.add_argument(
        "--also",
        help="voices already in the account, by id or by part of the name "
        "(e.g. jessica,bella) — they cost no slot against the plan's voice cap",
    )
    pb.add_argument("--pick", help="comma-separated numbers from `list`, e.g. 1,5,7,8,10,13")
    pb.add_argument("--model", default=DEFAULT_MODEL)
    pb.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    pb.set_defaults(func=cmd_build)

    pf = sub.add_parser("final", help="summary: production settings, text before and after the text layer")
    pf.add_argument("--only", default="Pavlenko")
    pf.add_argument("--model", default=DEFAULT_MODEL)
    pf.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    pf.set_defaults(func=cmd_final)

    py = sub.add_parser("style", help="expression vs latency: style × TTFB")
    py.add_argument("--only", default="Pavlenko")
    py.add_argument("--values", default="0,0.3,0.5,0.7", help="comma-separated style values")
    py.add_argument("--stability", type=float, default=0.4)
    py.add_argument("--model", default=DEFAULT_MODEL)
    py.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    py.add_argument("--breaks", action="store_true", help="use the text with break tags instead of the lively one")
    py.add_argument(
        "--takes",
        type=int,
        default=3,
        help="how many generations per setting — stability IS the variation between "
        "them, so one clip cannot show what you are setting (default 3)",
    )
    py.set_defaults(func=cmd_style)

    pw = sub.add_parser("words", help="diagnose broken words: breaks / apostrophe / stability / model")
    pw.add_argument("--only", default="Pavlenko")
    pw.add_argument("--model", default=DEFAULT_MODEL)
    pw.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    pw.set_defaults(func=cmd_words)

    pz = sub.add_parser("pause", help="compare the ways of controlling rhythm and pauses")
    pz.add_argument("--only", default="Pavlenko", help="part of the voice name")
    pz.add_argument("--model", default=DEFAULT_MODEL)
    pz.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    pz.add_argument("--stability", type=float, default=0.4)
    pz.set_defaults(func=cmd_pause)

    pp = sub.add_parser("prosody", help="intonation matrix: stability/style × text style")
    pp.add_argument("--only", default="Solomiya,Kateryna", help="comma-separated parts of voice names")
    pp.add_argument("--model", default=DEFAULT_MODEL)
    pp.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    pp.set_defaults(func=cmd_prosody)

    pa = sub.add_parser("accent", help="stress matrix: voice × model × stress marks")
    pa.add_argument(
        "--models",
        default="eleven_flash_v2_5,eleven_multilingual_v2",
        help="comma-separated models (eleven_v3 works but is not realtime)",
    )
    pa.add_argument("--sample", default="booking", choices=["booking", "greeting"])
    pa.add_argument("--only", help="comma-separated name parts, to narrow the voice list")
    pa.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    pa.set_defaults(func=cmd_accent)

    pm = sub.add_parser("models", help="one voice, one line, several synthesis models")
    pm.add_argument("--only", required=True, help="part of the voice name, or its id")
    pm.add_argument(
        "--models",
        default="eleven_flash_v2_5,eleven_turbo_v2_5,eleven_multilingual_v2,eleven_v3",
        help="comma-separated model ids to compare",
    )
    pm.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    pm.add_argument(
        "--breaks",
        action="store_true",
        help="include a pause tag, to see whether the model honours it or reads it aloud",
    )
    pm.set_defaults(func=cmd_models)

    ps = sub.add_parser("serve", help="serve the comparison page")
    ps.add_argument("--port", type=int, default=8080)
    ps.set_defaults(func=cmd_serve)

    pc = sub.add_parser("cleanup", help="remove the lab voices from the collection")
    pc.add_argument("--keep", required=True, help="the voice_id to keep")
    pc.set_defaults(func=cmd_cleanup)

    args = p.parse_args()
    # Before anything else: every command below reads SAMPLES or BASELINE, and
    # both are empty until a language is chosen.
    _use_language(args.lang)
    args.func(args)


if __name__ == "__main__":
    main()
