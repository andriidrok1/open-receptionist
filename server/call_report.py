"""call_report.py — a folder of call traces, folded into one table.

Every call leaves `logs/calls/<call_id>.jsonl` (see observability.py). This
reads a folder of them and answers the questions one asks about a week of
traffic: how long do callers wait, at p50 and at p95; which stage is the slow
one; which tools run and how often they say no; what breaks; what it all cost.

Usage:
    python call_report.py                      # logs/calls/, every call
    python call_report.py path/to/dir          # another folder
    python call_report.py --business dental_us # one business only
    python call_report.py --last 20            # the twenty most recent calls
    python call_report.py --calls              # one line per call as well
    python call_report.py --json               # the aggregate as JSON

Per-call numbers are recomputed from the events, never read off the summary
row, so a call the process died on is counted as what it was: cut, with the
turns it did have. Money comes from pricing.yaml through the same code that
prices a live call; without that file the usage is reported and the cost is
not guessed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from observability import LOGS_DIR, STAGES, load_call, percentile


def _dist(values) -> dict:
    v = [x for x in values if x is not None]
    return {
        "n": len(v),
        "p50": percentile(v, 50),
        "p95": percentile(v, 95),
        "max": round(max(v), 3) if v else None,
    }


def aggregate(calls: list[dict]) -> dict:
    """Fold per-call summaries into one. Latency and stage distributions are
    over TURNS, not over calls: a two-turn call and a twenty-turn call
    contribute what they have, and a p95 is a p95 of what callers felt."""
    turns = [t for c in calls for t in c.get("per_turn", [])]
    answered = [t for t in turns if t["answered"]]
    with_latency = [t for t in answered if t["latency_s"] is not None]

    tools: dict[str, dict] = {}
    durations: dict[str, list[float]] = {}
    for c in calls:
        for name, row in c.get("tools", {}).items():
            agg = tools.setdefault(name, {"calls": 0, "ok": 0, "rejected": 0, "failed": 0})
            for k in ("calls", "ok", "rejected", "failed"):
                agg[k] += row.get(k, 0)
            if row.get("p50_s") is not None:
                durations.setdefault(name, []).append(row["p50_s"])
    for name, agg in tools.items():
        agg["p50_s"] = percentile(durations.get(name, []), 50)
    tools = dict(sorted(tools.items(), key=lambda kv: -kv[1]["calls"]))

    errors = Counter()
    for c in calls:
        for msg in c.get("error_messages", []):
            errors[msg] += 1

    priced = [c["cost"] for c in calls if c.get("cost")]
    minutes = sum(c["duration_s"] for c in calls) / 60
    cost = None
    if priced:
        total = sum(x["total"] for x in priced)
        by_part = {k: sum(x[k] for x in priced) for k in ("llm", "tts", "stt", "telephony")}
        cost = {
            "total": round(total, 4),
            "median_per_call": percentile([x["total"] for x in priced], 50),
            "per_minute": round(total / minutes, 4) if minutes else None,
            "share": {k: round(v / total, 3) if total else 0 for k, v in by_part.items()},
            "priced_on": priced[0].get("priced_on"),
            "currency": priced[0].get("currency", "USD"),
            "priced_calls": len(priced),
        }

    return {
        "calls": len(calls),
        "completed": sum(1 for c in calls if c.get("ended") in ("ended", "cancelled")),
        "cut": sum(1 for c in calls if c.get("ended") == "cut"),
        "businesses": dict(Counter(c.get("business") for c in calls).most_common()),
        "minutes": round(minutes, 1),
        "duration_median_s": percentile([c["duration_s"] for c in calls], 50),
        "turns": len(answered),
        "turns_with_tool": sum(1 for t in answered if t["kind"] == "tool"),
        "interruptions": sum(1 for t in turns if t["interrupted"]),
        "latency": _dist(t["latency_s"] for t in with_latency),
        "latency_plain": _dist(t["latency_s"] for t in with_latency if t["kind"] == "plain"),
        "latency_tool": _dist(t["latency_s"] for t in with_latency if t["kind"] == "tool"),
        "stages": {key: _dist(t[key] for t in turns) for key, _ in STAGES if key != "latency_s"},
        "tool_calls": sum(a["calls"] for a in tools.values()),
        "tools": tools,
        "errors": sum(errors.values()),
        "error_messages": dict(errors.most_common(10)),
        "usage": {
            k: sum(c.get("usage", {}).get(k, 0) for c in calls)
            for k in ("prompt_tokens", "completion_tokens", "tts_characters")
        },
        "cost": cost,
    }


def _s(v) -> str:
    return "   –" if v is None else f"{v:6.2f}"


def format_report(agg: dict, calls: list[dict] | None = None) -> str:
    """The aggregate as text for a terminal."""
    if not agg["calls"]:
        return "no calls found"
    lines = []
    biz = " · ".join(f"{k} ×{n}" for k, n in agg["businesses"].items())
    lines.append(
        f"calls            {agg['calls']} ({agg['completed']} completed, {agg['cut']} cut)"
        f"   {biz}"
    )
    lines.append(
        f"talk time        {agg['minutes']} min total · "
        f"{agg['duration_median_s'] or 0:.0f}s median per call"
    )
    lines.append(
        f"turns            {agg['turns']} ({agg['turns_with_tool']} with a tool call)"
        f" · {agg['interruptions']} interrupted"
    )
    lines.append("")
    lines.append(f"{'latency (caller stops → bot speaks)':<40}{'p50':>7}{'p95':>7}{'max':>7}{'n':>6}")
    for label, key in (("all turns", "latency"), ("plain", "latency_plain"),
                       ("with a tool call", "latency_tool")):
        d = agg[key]
        lines.append(f"  {label:<38}{_s(d['p50']):>7}{_s(d['p95']):>7}{_s(d['max']):>7}{d['n']:>6}")
    lines.append(f"{'stages (seconds)':<40}{'p50':>7}{'p95':>7}{'max':>7}{'n':>6}")
    for key, label in STAGES:
        if key == "latency_s":
            continue
        d = agg["stages"][key]
        if not d["n"]:
            continue
        lines.append(f"  {label:<38}{_s(d['p50']):>7}{_s(d['p95']):>7}{_s(d['max']):>7}{d['n']:>6}")
    lines.append("")
    if agg["tools"]:
        lines.append(f"{'tools':<32}{'calls':>7}{'ok':>6}{'rejected':>10}{'failed':>8}{'p50 s':>8}")
        for name, row in agg["tools"].items():
            lines.append(
                f"  {name:<30}{row['calls']:>7}{row['ok']:>6}{row['rejected']:>10}"
                f"{row['failed']:>8}{_s(row['p50_s']):>8}"
            )
    else:
        lines.append("tools            none called")
    lines.append("")
    lines.append(f"errors           {agg['errors']}")
    for msg, n in agg["error_messages"].items():
        lines.append(f"  {msg}  ×{n}")
    lines.append("")
    u = agg["usage"]
    lines.append(
        f"usage            {u['prompt_tokens']} in + {u['completion_tokens']} out tokens, "
        f"{u['tts_characters']} synthesised characters"
    )
    cost = agg["cost"]
    if cost:
        share = cost["share"]
        lines.append(
            f"cost             {cost['total']:.3f} {cost['currency']} total · "
            f"{cost['median_per_call']:.3f} median per call · "
            f"{cost['per_minute']:.3f}/min   (rates checked {cost['priced_on']})"
        )
        lines.append(
            f"  of which       tts {share['tts']:.0%} · stt {share['stt']:.0%} · "
            f"llm {share['llm']:.0%} · phone {share['telephony']:.0%}"
        )
    else:
        lines.append("cost             not computed — no pricing.yaml")
    if calls:
        lines.append("")
        lines.append(
            f"{'call':<14}{'business':<14}{'started':<22}{'s':>6}{'turns':>6}{'p50':>7}"
            f"{'p95':>7}{'tools':>6}{'err':>4}{'cost':>8}"
        )
        for c in calls:
            started = (c.get("started_at") or "")[:19].replace("T", " ")
            cst = c.get("cost", {}).get("total")
            lines.append(
                f"{(c.get('call_id') or '?'):<14}{(c.get('business') or '?'):<14}{started:<22}"
                f"{c['duration_s']:>6.0f}{c['turns']:>6}{_s(c['latency']['p50']):>7}"
                f"{_s(c['latency']['p95']):>7}{c['tool_calls']:>6}{c['errors']:>4}"
                f"{('   –' if cst is None else f'{cst:.3f}'):>8}"
                + ("  cut" if c.get("ended") == "cut" else "")
            )
    return "\n".join(lines)


def load_dir(folder: Path, business: str | None = None, last: int | None = None) -> list[dict]:
    files = sorted(Path(folder).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if last:
        files = files[-last:]
    calls = [load_call(p) for p in files]
    if business:
        calls = [c for c in calls if c.get("business") == business]
    return calls


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folder", nargs="?", default=str(LOGS_DIR))
    ap.add_argument("--business", help="only calls of this business key")
    ap.add_argument("--last", type=int, help="only the N most recent calls")
    ap.add_argument("--calls", action="store_true", help="also one line per call")
    ap.add_argument("--json", action="store_true", help="the aggregate as JSON")
    args = ap.parse_args(argv)

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"no such folder: {folder}", file=sys.stderr)
        return 2
    calls = load_dir(folder, business=args.business, last=args.last)
    agg = aggregate(calls)
    if args.json:
        if args.calls:
            agg["per_call"] = [{k: v for k, v in c.items() if k != "per_turn"} for c in calls]
        print(json.dumps(agg, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(agg, calls if args.calls else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
