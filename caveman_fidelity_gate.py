#!/usr/bin/env python3
"""caveman-fidelity-gate - offline, span-level fidelity gate for caveman-style replies.

Why: JuliusBrussee/caveman measures output token reduction but its own
evals/HONEST-NUMBERS.md states "no reviewed fidelity snapshot is committed, so no
quality claim is published", and evals/score_fidelity.py can only score snapshots
that evals/llm_run.py produced through live LLM calls. The committed corpus
evals/prompts/fidelity.json ships hand-written `examples.pass` / `examples.fail`
per case, but nothing in the repository consumes them, so the checker's own
semantics are never self-verified without the API.

This tool is a zero-dependency, offline, independent re-implementation of that
fidelity contract plus two things upstream does not do:
  1. selftest - replay every committed example against the checker and report
     exact disagreements (the missing offline self-verification).
  2. diff     - report which protected spans (numbers, negations, identifiers,
     paths, URLs, code fences) were dropped between an original and a compressed
     reply, with byte offsets.

Exit codes: 0 pass, 1 violations found, 2 usage or input error.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys

TOOL = "caveman-fidelity-gate"
VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# fidelity contract (mirrors evals/score_fidelity.py semantics)
# ---------------------------------------------------------------------------


def compile_case(case: dict) -> dict:
    checks = case.get("checks") or {}
    return {
        "id": case["id"],
        "category": case.get("category", "uncategorized"),
        "must_include": checks.get("must_include", []),
        "must_not_include": checks.get("must_not_include", []),
    }


def literal_hint(pattern: str) -> str:
    """Best-effort literal core of a regex, used to locate near-misses."""
    text = re.sub(r"\(\?[a-zA-Z-]*:?", "", pattern)
    text = re.sub(r"\\b|\\s\*|\\s\+|\^|\$|\(\?:|\)", "", text)
    text = re.sub(r"[\[\]\(\)\|\?\*\+]", " ", text)
    text = re.sub(r"\\(.)", r"\1", text)
    words = [w for w in text.split() if len(w) >= 2]
    if not words:  # short literals such as \b3\b still deserve a near-miss hint
        words = [w for w in text.split() if w]
    return " ".join(words[:4]).strip()


def nearest_line(output: str, pattern: str) -> dict | None:
    """Closest line to the literal core of a failing pattern, with a ratio."""
    hint = literal_hint(pattern).lower()
    if not hint:
        return None
    best = None
    for idx, line in enumerate(output.splitlines(), start=1):
        ratio = difflib.SequenceMatcher(None, hint, line.lower()).ratio()
        if best is None or ratio > best["ratio"]:
            best = {"line": idx, "ratio": round(ratio, 4), "text": line[:160]}
    if best and best["ratio"] > 0.3:
        return best
    return best


def check_output(output: str, case: dict) -> list:
    """Failure records for one output; empty means the case passed."""
    failures = []
    for pattern in case["must_include"]:
        if not re.search(pattern, output, re.IGNORECASE):
            failures.append(
                {
                    "kind": "missing",
                    "pattern": pattern,
                    "literal_hint": literal_hint(pattern),
                    "nearest": nearest_line(output, pattern),
                }
            )
    for pattern in case["must_not_include"]:
        m = re.search(pattern, output, re.IGNORECASE)
        if m:
            line = output.count("\n", 0, m.start()) + 1
            failures.append(
                {
                    "kind": "forbidden",
                    "pattern": pattern,
                    "span": [m.start(), m.end()],
                    "line": line,
                    "excerpt": output[max(0, m.start() - 40) : m.end() + 40],
                }
            )
    return failures


# ---------------------------------------------------------------------------
# information-loss diff
# ---------------------------------------------------------------------------

NEGATION_WORDS = (
    "no", "not", "never", "none", "cannot", "can't", "don't", "doesn't",
    "won't", "isn't", "aren't", "without", "except", "unless", "excluding",
    "only", "at most", "at least", "maximum", "minimum", "must not",
)

SPAN_PATTERNS = (
    ("code_fence", re.compile(r"```[\s\S]*?```")),
    ("url", re.compile(r"https?://[^\s)\]}>,;\"']+")),
    ("inline_code", re.compile(r"`[^`\n]+`")),
    ("path", re.compile(r"(?<![\w.])/[\w.@~+-]+(?:/[\w.@~+-]+)+/?")),
    ("number", re.compile(r"\b\d+(?:[.,]\d+)*(?:\s?(?:ms|milliseconds?|s|sec|seconds?|m|min|minutes?|h|hrs?|hours?|d|days?|%|kb|mb|gb|tb|x))?\b", re.IGNORECASE)),
    ("hex", re.compile(r"\b0x[0-9a-fA-F]+\b")),
    ("identifier", re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b")),
    ("negation", re.compile(r"\b(?:" + "|".join(re.escape(w) for w in NEGATION_WORDS) + r")\b", re.IGNORECASE)),
)

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def extract_spans(text: str) -> list:
    spans = []
    for kind, rx in SPAN_PATTERNS:
        for m in rx.finditer(text):
            spans.append(
                {
                    "kind": kind,
                    "text": m.group(0),
                    "offset": m.start(),
                    "line": text.count("\n", 0, m.start()) + 1,
                }
            )
    spans.sort(key=lambda s: (s["offset"], s["kind"]))
    return spans


def diff_reports(original: str, compressed: str) -> dict:
    spans = extract_spans(original)
    haystack = _norm(compressed)
    dropped, preserved = [], 0
    seen = set()
    for span in spans:
        key = (span["kind"], _norm(span["text"]))
        if key in seen:
            preserved += 1
            continue
        seen.add(key)
        if key[1] in haystack:
            preserved += 1
        else:
            dropped.append(span)
    total = len(spans)
    return {
        "protected_spans": total,
        "unique_spans": len(seen),
        "preserved": preserved,
        "dropped": dropped,
        "drop_rate": round(len(dropped) / total, 4) if total else 0.0,
        "by_kind": {
            kind: sum(1 for d in dropped if d["kind"] == kind)
            for kind in sorted({d["kind"] for d in dropped})
        },
    }


# ---------------------------------------------------------------------------
# io helpers
# ---------------------------------------------------------------------------


def load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(_usage_error(f"cannot read JSON {path}: {exc}"))


def load_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise SystemExit(_usage_error(f"cannot read {path}: {exc}"))


def _usage_error(msg: str) -> int:
    print(json.dumps({"tool": TOOL, "error": msg}, ensure_ascii=False))
    return 2


def load_cases(path: str) -> list:
    data = load_json(path)
    cases = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(cases, list) or not cases:
        raise SystemExit(_usage_error(f"{path}: expected a non-empty 'cases' array"))
    out = []
    for case in cases:
        if not isinstance(case, dict) or "id" not in case or "checks" not in case:
            raise SystemExit(_usage_error(f"{path}: case needs 'id' and 'checks'"))
        out.append(case)
    return out


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def cmd_score(args) -> int:
    cases = [compile_case(c) for c in load_cases(args.cases)]
    replies = load_json(args.replies)
    arms = {}
    if isinstance(replies, dict) and "arms" in replies:
        arms = replies["arms"]
    elif isinstance(replies, dict):
        arms = replies
    else:
        raise SystemExit(_usage_error(f"{args.replies}: expected an object of arm -> outputs"))

    results, total_pass, total = [], 0, 0
    for arm, payload in arms.items():
        if isinstance(payload, dict):
            outputs = [payload.get(c["id"], "") for c in cases]
        elif isinstance(payload, list):
            if len(payload) != len(cases):
                raise SystemExit(
                    _usage_error(
                        f"arm '{arm}' has {len(payload)} outputs for {len(cases)} cases"
                    )
                )
            outputs = payload
        else:
            raise SystemExit(_usage_error(f"arm '{arm}': expected list or id->output map"))
        for case, output in zip(cases, outputs):
            failures = check_output(str(output), case)
            total += 1
            if not failures:
                total_pass += 1
            results.append(
                {
                    "arm": arm,
                    "case": case["id"],
                    "category": case["category"],
                    "passed": not failures,
                    "failures": failures,
                }
            )

    failed = total - total_pass
    rate = (total_pass / total) if total else 0.0
    threshold = args.min_rate
    code = 0
    if failed or (threshold is not None and rate < threshold):
        code = 1
    report = {
        "tool": TOOL,
        "version": VERSION,
        "mode": "score",
        "cases": len(cases),
        "arms": list(arms),
        "summary": {
            "checks": total,
            "passed": total_pass,
            "failed": failed,
            "pass_rate": round(rate, 4),
            "min_rate": threshold,
        },
        "results": results,
        "exit_code": code,
    }
    emit(report, args)
    return code


def cmd_selftest(args) -> int:
    cases = load_cases(args.cases)
    disagreements, n_pass_ex, n_fail_ex = [], 0, 0
    for raw in cases:
        case = compile_case(raw)
        examples = raw.get("examples") or {}
        for output in examples.get("pass", []) or []:
            n_pass_ex += 1
            failures = check_output(output, case)
            if failures:
                disagreements.append(
                    {
                        "case": case["id"],
                        "kind": "pass_example_rejected",
                        "output": output[:200],
                        "failures": [f["pattern"] for f in failures],
                    }
                )
        for output in examples.get("fail", []) or []:
            n_fail_ex += 1
            if not check_output(output, case):
                disagreements.append(
                    {
                        "case": case["id"],
                        "kind": "fail_example_accepted",
                        "output": output[:200],
                        "failures": [],
                    }
                )
    # cases with no committed examples at all
    bare = [c["id"] for c in cases if not (c.get("examples") or {}).get("pass")]
    ok = not disagreements
    code = 0 if ok else 1
    total_ex = n_pass_ex + n_fail_ex
    report = {
        "tool": TOOL,
        "version": VERSION,
        "mode": "selftest",
        "summary": {
            "cases": len(cases),
            "pass_examples": n_pass_ex,
            "fail_examples": n_fail_ex,
            "total_examples": total_ex,
            "examples_reproduced": total_ex - len(disagreements),
            "agreement_rate": round((total_ex - len(disagreements)) / total_ex, 4)
            if total_ex
            else 0.0,
            "disagreements": len(disagreements),
            "cases_without_pass_example": bare,
        },
        "disagreement_detail": disagreements,
        "exit_code": code,
    }
    emit(report, args)
    return code


def cmd_diff(args) -> int:
    original = load_text(args.original)
    compressed = load_text(args.compressed)
    report = diff_reports(original, compressed)
    code = 1 if report["dropped"] else 0
    report.update(
        {
            "tool": TOOL,
            "version": VERSION,
            "mode": "diff",
            "original": os.path.basename(args.original),
            "compressed": os.path.basename(args.compressed),
        }
    )
    report = {k: report[k] for k in ("tool", "version", "mode", "original", "compressed") if k in report} | {
        "summary": {
            "protected_spans": report["protected_spans"],
            "unique_spans": report["unique_spans"],
            "preserved": report["preserved"],
            "dropped": len(report["dropped"]),
            "drop_rate": report["drop_rate"],
            "by_kind": report["by_kind"],
        },
        "dropped": report["dropped"],
        "exit_code": code,
    }
    emit(report, args)
    return code


def emit(report: dict, args) -> None:
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return
    mode = report["mode"]
    if mode == "diff":
        s = report["summary"]
        print(
            f"{TOOL}: {s['dropped']}/{s['protected_spans']} protected spans dropped "
            f"(rate {s['drop_rate']:.2%})"
        )
        for d in report["dropped"]:
            print(f"  L{d['line']} {d['kind']}: {d['text'][:80]!r}")
    elif mode == "selftest":
        s = report["summary"]
        print(
            f"{TOOL} selftest: {s['examples_reproduced']}/{s['total_examples']} committed "
            f"examples reproduced ({s['agreement_rate']:.2%}); "
            f"{s['disagreements']} disagreement(s)"
        )
        for d in report["disagreement_detail"]:
            print(f"  {d['case']}: {d['kind']} :: {d['output'][:100]!r}")
    else:
        s = report["summary"]
        print(
            f"{TOOL}: {s['passed']}/{s['checks']} checks passed "
            f"({s['pass_rate']:.2%}), {s['failed']} failed"
        )
        for r in report["results"]:
            if not r["passed"]:
                kinds = ", ".join(f["kind"] for f in r["failures"])
                print(f"  {r['arm']} / {r['case']}: {kinds}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"{TOOL} {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("score", help="score reply outputs against the fidelity cases")
    s.add_argument("--cases", required=True)
    s.add_argument("--replies", required=True)
    s.add_argument("--min-rate", type=float, default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_score)

    s = sub.add_parser("selftest", help="replay committed examples.pass/fail against the checker")
    s.add_argument("--cases", required=True)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_selftest)

    s = sub.add_parser("diff", help="report protected spans dropped by compression")
    s.add_argument("--original", required=True)
    s.add_argument("--compressed", required=True)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_diff)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
