"""Source-sense and SHA-pinned private-snapshot regressions; no real Gemini."""

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from test_daily_fresh_generation import Harness, LEVELS, app
from test_selector_scan_regression import PostgreSQLRegression
from audit_phase3c import audit, install, load_snapshot
from services import lexical_evidence as evidence


def candidate(surface, reading=""):
    return {"surface": surface, "d": surface, "normalized_key": surface, "level": "N5", "jlpt_level": "N5",
            "source": "openjlpt", "reading_hiragana": reading}


class SourceEvidence(unittest.TestCase):
    def test_source_pos_recovers_attested_senses_not_suffixes(self):
        for surface in ("ある", "かかる", "はく", "あびる", "かける"):
            result = app.classify_daily_fresh_pool_row(candidate(surface))
            self.assertEqual(result["status"], "verified_verb", surface)
            self.assertTrue(result["source_sense_evidence"]["matches"])
            self.assertTrue(result["source_sense_evidence"]["dictionary_sha256"])
        self.assertEqual(app.classify_daily_fresh_pool_row(candidate("いす"))["status"], "verified_word")
        self.assertFalse(app.audit_verb_candidate(candidate("向こう"))["accepted"])
        self.assertTrue(app.audit_verb_candidate("だます")["accepted"])

    def test_wrong_reading_level_or_source_does_not_use_index(self):
        for row in (candidate("ある", "つく"), {**candidate("ある"), "jlpt_level": "N4"},
                    {**candidate("ある"), "source": "unreviewed"}):
            self.assertIsNone(evidence.source_record(row))
        self.assertEqual(app.classify_daily_fresh_pool_row({**candidate("ある"), "source": "unreviewed"})["status"], "unknown")
        self.assertEqual(app.classify_daily_fresh_pool_row(candidate("未確認語る"))["status"], "unknown")

    def test_alias_history_and_homographs_are_not_sound_merged(self):
        bath = evidence.ExclusionKeys({"浴びる"}, {"浴びる": ["あびる"]})
        self.assertIsNotNone(evidence.history_overlap(candidate("あびる"), bath))
        arrival = evidence.ExclusionKeys({"着く"}, {"着く": ["つく"]})
        self.assertIsNone(evidence.history_overlap(candidate("はく"), arrival))
        same_sound = evidence.ExclusionKeys({"書ける"}, {"書ける": ["かける"]})
        self.assertIsNone(evidence.history_overlap(candidate("かける"), same_sound))
        uncertain = evidence.ExclusionKeys({"掛ける"})
        self.assertEqual(evidence.history_overlap(candidate("かける"), uncertain)["reason"], "history_lexical_identity_uncertain")

    def test_reviewed_distinct_sense_does_not_become_duplicate(self):
        history = evidence.ExclusionKeys({"掛ける"}, {"掛ける": ["かける"]})
        self.assertIsNone(evidence.history_overlap(candidate("かける"), history, {"掛ける": [("1207610", 1)]}))
        self.assertEqual(evidence.history_overlap(candidate("かける"), history, {"掛ける": [("1207610", 4)]})["reason"],
                         "history_lexical_identity_overlap")

    def test_manifest_source_and_alignment_invalidation(self):
        original = evidence.PATH
        raw = json.loads(original.read_text(encoding="utf-8"))
        for field in ("source_sha256", "alignment_sha256", "version"):
            changed = {**raw, field: "unreviewed"}
            fake = Mock()
            fake.parents = original.parents
            fake.read_bytes.return_value = json.dumps(changed, ensure_ascii=False).encode()
            fake.read_text.return_value = json.dumps(changed, ensure_ascii=False)
            evidence.manifest.cache_clear()
            try:
                with patch.object(evidence, "PATH", fake), self.assertRaisesRegex(ValueError, "version_mismatch"):
                    evidence.manifest()
            finally:
                evidence.manifest.cache_clear()
        self.assertEqual(evidence.manifest()["version"], evidence.VERSION)

    def test_public_text_fingerprints_survive_windows_linux_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            lf, crlf = Path(folder) / "lf.txt", Path(folder) / "crlf.txt"
            lf.write_bytes("表記\nreading\n".encode())
            crlf.write_bytes(b"\xef\xbb\xbf" + "表記\r\nreading\r\n".encode())
            self.assertEqual(evidence.text_digest(lf), evidence.text_digest(crlf))

    def test_source_hints_preserve_compact_protocol(self):
        original = app.build_gemini_daily_enrich_prompt("word_basic", "verb", {"N5": [candidate("はく")]})
        prompt = evidence.append_source_sense_hints(original, [candidate("はく")])
        self.assertTrue(prompt.startswith(original))
        hints = json.loads(prompt.split("Source sense hints: ", 1)[1])
        self.assertIn("trousers", hints[0]["source_gloss"])
        self.assertEqual(hints[0]["readings"], ["はく"])
        self.assertEqual(evidence.append_source_sense_hints(original, [{"grammar_key": "n5:です"}]), original)

    def test_wrong_enriched_reading_is_rejected_before_bank(self):
        c = candidate("はく")
        for reading, expected in (("つく", 0), ("はく", 1)):
            raw = json.dumps({"items": [["はく", reading, "穿褲子", 1, "はきます", "はいて", "はかない", "はいた", "靴をはきます。", "穿鞋。"]]}, ensure_ascii=False)
            items, _ = app.parse_gemini_bank_items_with_stats("verb", "N5", raw)
            matched, stats = app.filter_enriched_items_to_candidates(items, "verb", [c])
            self.assertEqual(stats["matched"], expected)
            self.assertEqual(len(matched), expected)

    def test_wrong_snapshot_digest_never_substitutes_public_csv(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "wrong.csv"
            path.write_bytes(b"surface\nwrong\n")
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                load_snapshot(path)


class FormalSnapshot(unittest.TestCase):
    backend = "SQLite"

    def setUp(self):
        pool, history = os.getenv("N5_POOL_SNAPSHOT"), os.getenv("N5_HISTORY_SNAPSHOT")
        if not pool or not history:
            self.skipTest("Formal snapshot paths unavailable: NOT VERIFIED")
        self.rows = load_snapshot(Path(pool))
        self.snapshot = json.loads(Path(history).read_text(encoding="utf-8"))
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        install(self.h, self.rows, self.snapshot)

    def test_real_source_selector_finds_unused_attested_verb(self):
        history = app.gemini_bank_material_history_keys_for_item_type("verb")
        self.assertEqual(history, set(self.snapshot["history_keys"]))
        state = {}
        rows = app.fetch_daily_fresh_candidate_rows("verb", "N5", 1, history, source_state=state)
        self.assertEqual([r["surface"] for r in rows[:-1]], ["はく"])
        self.assertEqual(self.h.calls, [])
        self.assertIn("selector_sql_mode=id_batch_safe_hydration", self.output.getvalue())

    def test_save_reload_then_stop_before_api_without_fresh_primary(self):
        count = len(self.h.saved)
        job, steps = self.h.job(LEVELS, "2026-11-01")
        result = self.h.complete(job, steps)
        self.assertTrue(result["ok"], result)
        material = app.material_by_key(result["material_key"])
        self.assertEqual([len(material[k]) for k in ("vocabulary", "verbs", "grammar_points")], [8, 5, 5])
        self.assertEqual(self.h.saved[-1]["metadata"]["fallback_count"], {"word": 0, "verb": 0, "grammar": 0})
        calls, used = len(self.h.calls), app.gemini_bank_material_history_keys_for_item_type("verb")
        job, steps = self.h.job(LEVELS, "2026-11-02")
        failed = self.h.complete(job, steps)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"], "classifier_uncertainty")
        self.assertEqual(len(self.h.calls), calls)
        self.assertEqual(len(self.h.saved), count + 1)
        self.assertEqual(app.gemini_bank_material_history_keys_for_item_type("verb"), used)
        self.assertTrue(any(r["level"] == "N5" and r["available"] == 0 for r in failed["missing"]))

    def test_formal_failure_counts_replayed_not_inferred(self):
        with tempfile.TemporaryDirectory() as folder:
            result = audit(self.rows, self.snapshot, Path(folder))
        self.assertEqual(result["old_exclusions"], {
            "sql_category": 1281, "excluded_material_history_or_current_batch": 374,
            "non_verb:not_verified_dictionary_verb": 244, "non_verb:variant_separator": 11,
            "non_verb:known_non_verb": 4, "non_verb:counter_or_number": 36})
        self.assertEqual(result["available_count"], 1)
        self.assertEqual(result["repair_parent_exclusions"]["selected"], 2)


class PostgreSQLFormalSnapshot(FormalSnapshot):
    backend = "PostgreSQL17-isolated"

    def setUp(self):
        pool, history = os.getenv("N5_POOL_SNAPSHOT"), os.getenv("N5_HISTORY_SNAPSHOT")
        if not pool or not history:
            self.skipTest("Formal snapshot paths unavailable: NOT VERIFIED")
        self.rows = load_snapshot(Path(pool))
        self.snapshot = json.loads(Path(history).read_text(encoding="utf-8"))
        fixture = PostgreSQLRegression("test_real_prepare_enrich_finalize_save_reload")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.h, self.output = fixture.h, fixture.output
        install(self.h, self.rows, self.snapshot)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args, tests = parser.parse_known_args()
    program = unittest.main(argv=[sys.argv[0], *tests], verbosity=2, exit=False)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"tests_run": program.result.testsRun, "failures": len(program.result.failures),
                                      "errors": len(program.result.errors), "skips": len(program.result.skipped),
                                      "real_gemini_calls": 0}, indent=2) + "\n", encoding="utf-8")
    sys.exit(not program.result.wasSuccessful())
