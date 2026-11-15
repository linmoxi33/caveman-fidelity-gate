#!/usr/bin/env python3
"""Behaviour tests for caveman-fidelity-gate (zero third-party dependencies).

Run:  python3 tests/test_gate.py
Exit: 0 all pass, 1 any failure.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "caveman-fidelity-gate")
sys.path.insert(0, ROOT)

import caveman_fidelity_gate as gate  # noqa: E402

CASES = os.path.join(ROOT, "examples", "in", "cases.json")
REPLIES = os.path.join(ROOT, "examples", "in", "replies.json")
ORIGINAL = os.path.join(ROOT, "examples", "in", "original.txt")
COMPRESSED = os.path.join(ROOT, "examples", "in", "compressed.txt")


def run(*args):
    proc = subprocess.run(
        [sys.executable, CLI, *args], capture_output=True, text=True, cwd=ROOT
    )
    return proc


class TestCheckSemantics(unittest.TestCase):
    def setUp(self):
        self.case = gate.compile_case(
            {
                "id": "c",
                "category": "x",
                "checks": {
                    "must_include": [r"\b3\b"],
                    "must_not_include": [r"\bat least 3\b"],
                },
            }
        )

    def test_must_include_hit_passes(self):  # behaviour
        self.assertEqual(gate.check_output("max 3 tries", self.case), [])

    def test_must_include_miss_reports_nearest(self):  # behaviour
        failures = gate.check_output("max three tries", self.case)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["kind"], "missing")
        self.assertIsNotNone(failures[0]["nearest"])

    def test_must_not_include_hit_carries_span(self):  # behaviour
        out = "retry at least 3 times"
        failures = gate.check_output(out, self.case)
        kinds = {f["kind"] for f in failures}
        self.assertIn("forbidden", kinds)
        forbidden = [f for f in failures if f["kind"] == "forbidden"][0]
        start, end = forbidden["span"]
        self.assertEqual(out[start:end].lower(), "at least 3")
        self.assertEqual(forbidden["line"], 1)

    def test_case_insensitive_match(self):
        self.assertEqual(gate.check_output("MAX 3", self.case), [])


class TestSpanExtraction(unittest.TestCase):
    def test_extracts_numbers_and_units(self):
        kinds = {s["kind"] for s in gate.extract_spans("wait 250 ms and 3 retries")}
        self.assertIn("number", kinds)

    def test_extracts_negations(self):
        found = [s["text"].lower() for s in gate.extract_spans("never log the token")]
        self.assertIn("never", found)

    def test_extracts_paths_urls_fences(self):
        text = "see /var/log/auth.log and https://a.example/x\n```py\nx=1\n```"
        kinds = {s["kind"] for s in gate.extract_spans(text)}
        self.assertEqual({"path", "url", "code_fence"} - kinds, set())

    def test_diff_flags_dropped_negation(self):  # behaviour
        report = gate.diff_reports("Do not rotate the token.", "Rotate the token.")
        dropped = {d["text"].lower() for d in report["dropped"]}
        self.assertIn("not", dropped)
        self.assertEqual(report["preserved"], 0)

    def test_diff_counts_preserved_spans(self):  # behaviour
        report = gate.diff_reports("wait 250 ms", "wait 250 ms now")
        self.assertEqual(report["dropped"], [])
        self.assertGreaterEqual(report["preserved"], 1)

    def test_diff_offsets_point_at_original_text(self):
        text = "Keep 42 items"
        report = gate.diff_reports(text, "Keep items")
        number = [d for d in report["dropped"] if d["kind"] == "number"][0]
        self.assertEqual(text[number["offset"] : number["offset"] + 2], "42")


class TestCliContract(unittest.TestCase):
    def test_score_json_is_parseable(self):  # behaviour
        proc = run("score", "--cases", CASES, "--replies", REPLIES, "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["mode"], "score")
        self.assertEqual(proc.returncode, 1)  # naive-compressor violates both cases

    def test_score_exit_zero_when_all_pass(self):  # behaviour
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"arm": {"retry-policy": "Retry at most 3 times. 250 ms. except 429.",
                               "cache-safety": "Token never logged. Rotation not automatic."}}, fh)
            path = fh.name
        try:
            proc = run("score", "--cases", CASES, "--replies", path, "--json")
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertEqual(json.loads(proc.stdout)["summary"]["failed"], 0)
        finally:
            os.unlink(path)

    def test_selftest_reproduces_committed_examples(self):  # behaviour
        proc = run("selftest", "--cases", CASES, "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["summary"]["disagreements"], 0)
        self.assertGreaterEqual(payload["summary"]["total_examples"], 4)

    def test_diff_exit_one_on_loss(self):  # behaviour
        proc = run("diff", "--original", ORIGINAL, "--compressed", COMPRESSED)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("dropped", proc.stdout)

    def test_missing_file_is_exit_two(self):  # behaviour
        proc = run("score", "--cases", "/nonexistent-cases.json", "--replies", REPLIES)
        self.assertEqual(proc.returncode, 2)

    def test_mismatched_output_count_is_exit_two(self):  # behaviour
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"arm": ["only one output"]}, fh)
            path = fh.name
        try:
            proc = run("score", "--cases", CASES, "--replies", path)
            self.assertEqual(proc.returncode, 2)
        finally:
            os.unlink(path)

    def test_bad_replies_shape_is_exit_two(self):  # behaviour
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("[1,2,3]")
            path = fh.name
        try:
            proc = run("score", "--cases", CASES, "--replies", path)
            self.assertEqual(proc.returncode, 2)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
