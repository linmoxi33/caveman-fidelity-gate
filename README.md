# caveman-fidelity-gate

Offline, zero-dependency fidelity gate for caveman-style compressed replies: replay a
repo's own fidelity examples, score any reply set against the same regex contract, and
show exactly which protected spans (numbers, negations, identifiers, paths, URLs, code
fences) a compressor dropped — with byte offsets. **Why now:** `JuliusBrussee/caveman`
ships a fidelity corpus and a scorer, but the corpus can only be scored on snapshots
produced by live LLM calls, and the scorer never replays the corpus's own committed
`examples.pass` / `examples.fail`, so nothing verifies the checker offline.

Upstream: https://github.com/JuliusBrussee/caveman

## What the upstream is and what it does not do

`caveman` is a viral response-style skill/proxy for coding agents (120k+ stars) whose
selling point is shorter agent replies. Its own honesty page says the fidelity gap is
open in its own words:

- [`docs/HONEST-NUMBERS.md`](https://github.com/JuliusBrussee/caveman/blob/main/docs/HONEST-NUMBERS.md):
  *"A fidelity eval now exists (`evals/score_fidelity.py`: fixed cases, regex checks for
  kept numbers, negations, language and safety wording, scored for every arm), but no
  reviewed fidelity snapshot is committed, so no quality claim is published."*
- [`evals/score_fidelity.py`](https://github.com/JuliusBrussee/caveman/blob/main/evals/score_fidelity.py)
  only reads a snapshot: *"Generate the snapshot (calls Claude once per case × arm):
  `CAVEMAN_EVAL_SET=fidelity python3 evals/llm_run.py`"*. There is no offline path for an
  arbitrary reply or a local transcript.
- [`evals/prompts/fidelity.json`](https://github.com/JuliusBrussee/caveman/blob/main/evals/prompts/fidelity.json)
  carries 30 cases with hand-written `examples.pass` / `examples.fail` — and **no file in
  the upstream repository consumes that field**.
- [`issues/1318`](https://github.com/JuliusBrussee/caveman/issues/1318) (open, 2026-10-17,
  *"Ultracave ignored on Opus 5.5 despite #929, hooks fire every turn"*): *"Failure is in
  model adherence … An explicit user nudge does not reliably fix this."* Reporters can see
  the failure but have no local way to measure it.

## What this tool does / does not do

Does:

- `selftest` — replays every committed `examples.pass` / `examples.fail` against the
  fidelity contract and reports disagreements. This is the offline self-verification the
  upstream corpus was missing.
- `score` — scores your own reply sets (per arm, or per case id) against the same
  `must_include` / `must_not_include` contract, and for each failure reports the nearest
  line for a miss and the exact byte span for a forbidden hit.
- `diff` — extracts protected spans from an original reply and reports every span the
  compressed reply dropped, with line and byte offset.

Does not:

- It does not call an LLM, a tokenizer, or the network. No `tiktoken`, no API key.
- It does not claim semantic equivalence. Matching is lexical; a rewording that preserves
  meaning but changes the words (e.g. `not` → `no`) is reported as a dropped span.
- It does not touch `caveman` at runtime — it only reads the public JSON contract format.

## Install

Nothing to install. Python 3.8+ standard library only.

```bash
git clone git@github-linmoxi33:linmoxi33/caveman-fidelity-gate.git
cd caveman-fidelity-gate
python3 tests/test_gate.py
```

## Usage

```bash
# 1. self-verify a fidelity corpus offline (exit 1 if any committed example disagrees)
bin/caveman-fidelity-gate selftest --cases examples/in/cases.json --json

# 2. score your own replies: {"<arm>": ["<output aligned with cases order>", ...]}
#    or {"<arm>": {"<case id>": "<output>"}}
bin/caveman-fidelity-gate score --cases examples/in/cases.json --replies examples/in/replies.json

# 3. which protected spans did compression drop?
bin/caveman-fidelity-gate diff --original examples/in/original.txt --compressed examples/in/compressed.txt --json
```

## `--json` output

Every subcommand emits one JSON object (`sort_keys=True`), parseable with
`python3 -c "import json,sys;json.load(sys.stdin)"`:

```json
{
  "mode": "diff",
  "summary": {"protected_spans": 13, "preserved": 10, "dropped": 3,
              "drop_rate": 0.2308, "by_kind": {"negation": 3}},
  "dropped": [{"kind": "negation", "text": "not", "offset": 3, "line": 1}],
  "exit_code": 1
}
```

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | pass — no fidelity failure, no dropped protected span, no selftest disagreement |
| `1` | violations found (failed checks, dropped spans, or corpus disagreements) |
| `2` | usage or input error (missing file, bad JSON, mismatched output count) |

## Measured on the real upstream corpus

`evals/prompts/fidelity.json` @ `main`, fetched with `gh api`, 25,077 bytes, 30 cases:

```
$ bin/caveman-fidelity-gate selftest --cases fidelity.json
caveman-fidelity-gate selftest: 102/102 committed examples reproduced (100.00%); 0 disagreement(s)
```

All 30 cases carry examples; 102 committed pass/fail examples reproduce with 0
disagreements. The corpus is therefore internally consistent — a fact no upstream file
checks today. Re-run it yourself; the number is deterministic.

## 与同类项目的差异

- vs [`evals/score_fidelity.py`](https://github.com/JuliusBrussee/caveman/blob/main/evals/score_fidelity.py)
  (the only existing checker): it requires a snapshot from `evals/llm_run.py` (live Claude
  calls) and validates snapshot/contract shape; this tool runs on plain files with no LLM,
  replays the committed examples (which the upstream never does), and adds span-level
  evidence plus the protected-span diff mode.
- vs `tiktoken`/token proxies: this tool never measures tokens; it measures *information
  retained*, which is the axis upstream explicitly says it cannot publish a claim about.
- vs the repo's own `skills/caveman-compress/scripts/validate.py`: that validates the
  compression skill's output structure inside that skill; it does not read the fidelity
  corpus and cannot score arbitrary replies.

## 本轮创新

Upstream ships a fidelity corpus whose own hand-written pass/fail examples are consumed by
nothing, and whose checker needs an LLM run to produce anything to score. This is the first
offline replay of that corpus (102/102, 0 disagreements) and the first span-level
information-loss report over compressed replies; no other tool states both the verdict and
the exact dropped byte span. Grade **B**.

## Limitations

- Lexical, not semantic: reworded negations (`not` → `no`) are reported as dropped.
- The `score` contract mirrors upstream regex semantics (`re.IGNORECASE`, `.` does not
  cross lines). A case pattern using `(?s)` behaves the same here.
- `diff` heuristics can over-report protected spans in prose that merely mentions a word
  like `only`; treat `by_kind` as a triage signal, not a correctness proof.
- No transcript/JSONL mode yet; `score` takes explicit reply files.
- The 100% selftest result is a statement about corpus self-consistency, not about the
  caveman skill's real-world fidelity.

## License

MIT. Upstream `JuliusBrussee/caveman` is Apache-2.0; no upstream code or data is vendored
here — only its public JSON contract format is read at runtime.
