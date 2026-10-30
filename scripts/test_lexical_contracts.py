"""Real dictionary/source/selector regressions, isolated persistence, no Gemini quota."""

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import sys
import unittest
from unittest.mock import patch
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from audit_lexical_contracts import protected_hashes
from test_daily_fresh_generation import Harness, LEVELS, app
from test_selector_scan_regression import PostgreSQLRegression
from services.verb_candidate_audit import CLASSIFIER_VERSION, classify_lexeme, _full_entries, _dictionary_form

EVIDENCE = {}
CAPACITY = []


class LexicalContracts(unittest.TestCase):
    backend = "SQLite"

    def setUp(self):
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.h = Harness()
        self.addCleanup(self.h.close)

    def tearDown(self):
        EVIDENCE[f"{self.backend}.{self._testMethodName}"] = self.output.getvalue().splitlines()[-25:]

    def test_attested_verbs_not_suffix_guesses(self):
        expected_lemmas = {"あびる": "浴びる", "かける": "掛ける", "だます": "騙す", "いただく": "頂く",
                           "コピーする": "コピー-copy", "あいさつする": "挨拶", "引っ掻く": "引っ掻く"}
        for surface in ("あびる", "かける", "だます", "いただく", "コピーする", "あいさつする", "引っ掻く"):
            with self.subTest(surface=surface):
                result = classify_lexeme(surface)
                self.assertEqual(result["status"], "verified_verb", result)
                self.assertTrue(result["evidence"])
                self.assertTrue(any(e["pos"][0] == "動詞" for e in result["evidence"]))
                self.assertIn(expected_lemmas[surface], {e["lemma"] for e in result["evidence"]})
                self.assertEqual(result["classifier_version"], CLASSIFIER_VERSION)
                self.assertFalse(app.is_daily_fresh_word_pool_row({"surface": surface}))
                self.assertTrue(app.audit_verb_candidate(surface)["accepted"])
        # The dictionary has exact kana entries; no handcrafted kana -> kanji alias.
        self.assertTrue(any(_dictionary_form(n) for n in _full_entries("だます")))

    def test_nonverbs_keep_their_attested_senses(self):
        expected_pos = {"いす": "名詞", "向こう": "名詞", "遠く": "名詞", "こんにちは": "感動詞",
                        "或る": "連体詞", "ずるずる": "副詞", "大切": "形状詞", "不便": "名詞"}
        for surface in ("いす", "向こう", "遠く", "こんにちは", "或る", "ずるずる", "大切", "不便"):
            with self.subTest(surface=surface):
                result = classify_lexeme(surface)
                self.assertEqual(result["status"], "verified_word", result)
                self.assertIn(expected_pos[surface], {e["pos"][0] for e in result["evidence"]})
                self.assertFalse(app.audit_verb_candidate(surface)["accepted"])
                self.assertTrue(app.is_daily_fresh_word_pool_row({"surface": surface}))
        self.assertIn("形状詞可能", classify_lexeme("不便")["evidence"][0]["pos"])
        self.assertEqual(classify_lexeme("どうぞ宜しく")["status"], "unknown")

    def test_unknown_not_suggested_word_and_reading_is_not_identity(self):
        result = app.audit_verb_candidate("unattested_lexeme_123")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["suggested_type"], "")
        self.assertTrue(result["retryable"])
        self.assertFalse(app.is_daily_fresh_word_pool_row({"surface": "unattested_lexeme_123"}))
        self.assertEqual(classify_lexeme("いす", "イス")["status"], "verified_word")
        self.assertEqual(classify_lexeme("いす", "あびる")["status"], "unknown")
        self.assertEqual(classify_lexeme("ある")["status"], "unknown")
        self.assertEqual(classify_lexeme("ある", source_pos="動詞")["status"], "verified_verb")
        self.assertEqual(classify_lexeme("あびる", source_pos="名詞")["status"], "unknown")

    def test_pos_contract_does_not_accept_unknown_or_verb_aliases_as_word(self):
        for pos in ("動詞", "五段動詞", "動詞（自動詞）", "他動詞", "verb_godan", "名詞/動詞", "unknown", "general", ""):
            self.assertFalse(app.word_pos_allows_enriched_item(pos), pos)
        for pos in ("形容動詞", "形容詞", "名詞・形容動詞", "名詞・サ変接続", "副詞", "連体詞", "na-adjective"):
            self.assertTrue(app.word_pos_allows_enriched_item(pos), pos)

    def test_unknown_selector_keeps_ids_without_false_exhaustion(self):
        self.h.sql("DELETE FROM vocabulary_pool")
        self.h.sql("""INSERT INTO vocabulary_pool (surface,base_form,normalized_key,jlpt_level,category,source,status,is_active)
                      VALUES ('unattested_lexeme_123','unattested_lexeme_123','unknown','N5','general','openjlpt','active',TRUE)""")
        state = {}
        rows = app.fetch_daily_fresh_candidate_rows("verb", "N5", 1, set(), source_state=state)
        self.assertEqual(len(rows), 1)
        meta = rows[-1]
        self.assertFalse(meta["candidate_exhausted"])
        self.assertTrue(meta["source_scan_complete"])
        self.assertEqual(meta["classifier_unknown_count"], 1)
        self.assertEqual(len(state["unknown_candidate_ids"]), 1)
        replay = app.fetch_daily_fresh_candidate_rows("verb", "N5", 1, set(), source_state=state)[-1]
        self.assertEqual(replay["rescanned_ids"], 0)
        job, steps = self.h.job(LEVELS, "2026-10-30")
        result = self.h.complete(job, steps)
        self.assertEqual(result["error"], "classifier_uncertainty")
        self.assertEqual(self.h.calls, [])
        self.assertEqual(self.h.saved, [])

    def test_missing_reserve_does_not_change_primary_quota(self):
        plan = app.select_daily_fresh_candidates("verb", {"N5": 1}, set(), "verb")
        candidate = plan["items"][0]
        self.h.sql("DELETE FROM vocabulary_pool WHERE normalized_key<>?", (candidate["normalized_key"],))
        plan = app.select_daily_fresh_candidates("verb", {"N5": 3}, set(), "verb")
        plan = app.ensure_daily_fresh_candidate_plan_contract(plan, {"N5": 1}, "verb", "verb")
        self.assertEqual(plan["effective_quota_by_level"], {"N5": 1})
        self.assertEqual(len(plan["primary_items"]), 1)
        self.assertEqual(plan["reserve_items"], [])
        self.assertTrue(plan["reserve_shortages"])
        self.assertEqual(app.gemini_daily_fresh_plan_error(plan, "verb", 1), "")
        # Once the sole verified entry is saved, this fixture is genuinely exhausted.
        exhausted = app.select_daily_fresh_candidates("verb", {"N5": 1}, {candidate["normalized_key"]}, "verb")
        exhausted = app.ensure_daily_fresh_candidate_plan_contract(exhausted, {"N5": 1}, "verb", "verb")
        self.assertEqual(app.gemini_daily_fresh_plan_error(exhausted, "verb", 1), "candidate_pool_exhausted")
        self.assertEqual(self.h.calls, [])

    def test_candidate_cache_preserves_source_sense_evidence(self):
        row = {"id": 123, "surface": "ある", "normalized_key": "ある", "jlpt_level": "N5",
               "part_of_speech": "動詞", "reading_hiragana": "ある"}
        self.assertEqual(app.classify_daily_fresh_pool_row(row)["status"], "verified_verb")
        candidate = app.build_daily_fresh_candidate_payload(row, "verb", "N5")
        candidate = json.loads(json.dumps(candidate))
        self.assertTrue(app.audit_verb_candidate(candidate)["accepted"])
        self.assertEqual(candidate["classification_evidence"]["classifier_version"], CLASSIFIER_VERSION)
        fingerprint = candidate["classification_evidence"]["source_fingerprint"]
        candidate["part_of_speech"] = "名詞"
        self.assertNotEqual(app.classify_daily_fresh_pool_row({**candidate, "surface": candidate["d"]})["source_fingerprint"],
                            fingerprint)

    def test_gemini_rejection_without_suggested_type_stays_unknown(self):
        original = app.call_gemini
        rejected = []
        def mock_response(prompt, **config):
            inputs = json.JSONDecoder().raw_decode(prompt.split("Input candidates: ", 1)[1])[0]["items"]
            if not rejected and "d" in inputs[0] and inputs[0]["level"] == "N5":
                rejected.append(inputs[0]["d"])
                self.h.calls.append(("verb", inputs))
                return json.dumps({"items": [], "rejected": [[rejected[0], "uncertain", "", ""]]})
            return original(prompt, **config)
        job, steps = self.h.job(LEVELS, "2026-10-30")
        with patch.object(app, "call_gemini", mock_response):
            result = self.h.complete(job, steps)
        self.assertTrue(result["ok"], result)
        self.assertTrue(rejected)
        self.assertIn(f"candidate={rejected[0]} reason=uncertain suggested_type=unknown", self.output.getvalue())
        self.assertNotIn(f"candidate={rejected[0]} from=verb to=word", self.output.getvalue())

    def test_same_level_rejected_verb_and_truncated_grammar(self):
        self.h.reject_once.add(("verb", "N5"))
        self.h.truncate_once.add(("grammar", "N4"))
        job, steps = self.h.job(LEVELS, "2026-10-30")
        result = self.h.complete(job, steps)
        self.assertTrue(result["ok"], result)
        self.assertIn("[verb-replacement] use_reserve level=N5", self.output.getvalue())
        self.assertIn("[grammar-replacement] use_reserve level=N4", self.output.getvalue())
        self.assertEqual([len(self.h.saved[-1][k]) for k in ("vocab", "verbs", "grammar_points")], [8, 5, 5])
        self.assertEqual(self.h.saved[-1]["metadata"]["fallback_count"], {"word": 0, "verb": 0, "grammar": 0})
        self.assertEqual(app.gemini_daily_batch_summary(job["job_id"])["verb"], dict.fromkeys(LEVELS, 1))

    def test_protected_functions_unchanged(self):
        before = json.loads((ROOT / "reports/phase3br_2026_10_30/before.json").read_text(encoding="utf-8"))
        self.assertEqual(protected_hashes(), before["protected_function_hashes"])

    def inventory(self, pool, used):
        counts = {level: Counter() for level in LEVELS}
        for row, evidence in pool:
            key = app.vocabulary_pool_candidate_key(row)
            if key not in used:
                counts[row["jlpt_level"]][evidence["status"]] += 1
        return {level: {status: counts[level][status] for status in ("verified_word", "verified_verb", "unknown", "rejected_invalid")}
                for level in LEVELS}

    def simulate(self, days):
        pool = [(row, app.classify_daily_fresh_pool_row(row)) for row in self.h.sql("SELECT * FROM vocabulary_pool ORDER BY id")]
        used, daily, success = set(), [], 0
        for index in range(days):
            before = self.inventory(pool, used)
            calls_before = len(self.h.calls)
            day = (dt.date(2026, 10, 1) + dt.timedelta(days=index)).isoformat()
            job, steps = self.h.job(LEVELS, day)
            result = self.h.complete(job, steps)
            record = {"day": day, "before": before, "ok": result["ok"], "error": result.get("error"),
                      "mock_calls": len(self.h.calls) - calls_before, "missing": result.get("missing", [])}
            if result["ok"]:
                material = self.h.saved[-1]
                self.assertEqual([len(material[k]) for k in ("vocab", "verbs", "grammar_points")], [8, 5, 5])
                self.assertEqual(material["metadata"]["fallback_count"], {"word": 0, "verb": 0, "grammar": 0})
                self.assertFalse(material["metadata"]["bank_fallback_allowed"])
                usage = app.gemini_bank_extract_material_usage_keys(material)
                today = usage["word"] | usage["verb"]
                self.assertFalse(today & used)
                self.assertEqual(Counter(r["jlpt_level"] for r in material["verbs"]), Counter(dict.fromkeys(LEVELS, 1)))
                self.assertEqual(Counter(r["jlpt_level"] for r in material["grammar_points"]), Counter(dict.fromkeys(LEVELS, 1)))
                for row in material["vocab"]:
                    self.assertEqual(classify_lexeme(row["word"])["status"], "verified_word", row)
                for row in material["verbs"]:
                    self.assertEqual(classify_lexeme(row["dictionary_form"])["status"], "verified_verb", row)
                used.update(today)
                record["quota"] = {"word": 8, "verb": 5, "grammar": 5}
                record["fallback"] = 0
                success += 1
                reload = app.material_by_key(result["material_key"])
                self.assertEqual([len(reload[k]) for k in ("vocabulary", "verbs", "grammar_points")], [8, 5, 5])
            else:
                self.assertIn(result["error"], {"candidate_pool_exhausted", "classifier_uncertainty"})
                self.assertEqual(len(self.h.calls), calls_before)
                self.assertEqual(len(self.h.saved), success)
            record["after"] = self.inventory(pool, used)
            daily.append(record)
        CAPACITY.append({"backend": self.backend, "source": "checked-in OpenJLPT only, no additional candidates, empty initial history",
                         "production_inventory_verified": False, "days": days, "successful_days": success,
                         "capacity_acceptance": "PASS" if success == days else "FAIL", "real_gemini_calls": 0, "daily": daily})

    def test_capacity_seven_days(self):
        self.simulate(7)

    def test_capacity_thirty_days(self):
        self.simulate(30)


class PostgreSQLLexicalContracts(LexicalContracts):
    backend = "PostgreSQL17-isolated"

    def setUp(self):
        fixture = PostgreSQLRegression("test_real_prepare_enrich_finalize_save_reload")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.h, self.output = fixture.h, fixture.output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args, remaining = parser.parse_known_args()
    program = unittest.main(argv=[sys.argv[0], *remaining], verbosity=2, exit=False)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"tests_run": program.result.testsRun, "failures": len(program.result.failures),
                                      "errors": len(program.result.errors), "skipped": [(str(t), r) for t, r in program.result.skipped],
                                      "successful": program.result.wasSuccessful(), "capacity": CAPACITY,
                                      "production_postgres_verified": False, "real_gemini_calls": 0, "evidence": EVIDENCE},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
    sys.exit(0 if program.result.wasSuccessful() else 1)
