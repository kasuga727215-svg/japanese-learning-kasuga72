"""Offline integration regressions: real selectors, SQLite jobs, parser and finalize."""

import contextlib
import copy
import csv
import datetime as dt
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["RUN_MIGRATIONS_ON_STARTUP"] = "false"
import app

LEVELS = ["N5", "N4", "N3", "N2", "N1"]
SCENARIOS = []
EVIDENCE = {}


class Harness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = str(Path(self.tmp.name) / "test.sqlite3")
        self.saved, self.calls = [], []
        self.reject_once, self.truncate_once = set(), set()
        self.stack = contextlib.ExitStack()
        for key, value in {
            "DATABASE_URL": "", "SQLITE_SETTINGS_FILE": self.path,
            "_GEMINI_JOBS_SCHEMA_READY": False, "_GEMINI_BANK_SCHEMA_READY": False,
            "_SETTINGS_SCHEMA_READY": False,
            "GEMINI_API_KEY": "mock-no-network", "RUN_MIGRATIONS_ON_STARTUP": False,
        }.items():
            self.stack.enter_context(patch.object(app, key, value))
        self.stack.enter_context(patch.object(app, "read_database", self.history_frame))
        self.stack.enter_context(patch.object(app, "save_material_for_date", self.save))
        self.stack.enter_context(patch.object(app, "call_gemini", self.gemini))
        self.stack.enter_context(patch.object(app.urllib.request, "urlopen", side_effect=AssertionError("network forbidden in regression")))
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            app.migrate_vocabulary_pool_sqlite(conn)
            app.migrate_grammar_pool_sqlite(conn)
            conn.commit()
        app.ensure_gemini_item_bank_store()
        self.seed()

    def close(self):
        self.stack.close()
        self.tmp.cleanup()

    def sql(self, sql, params=()):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
            return [dict(row) for row in rows]

    def seed(self):
        self.sql("DELETE FROM vocabulary_pool")
        self.sql("DELETE FROM gemini_item_bank")
        self.sql("DELETE FROM grammar_pool")
        seen = set()
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            for level in LEVELS:
                with (ROOT / "data" / "external_lexicon" / f"openjlpt-vocab-{level.lower()}.csv").open(encoding="utf-8-sig", newline="") as handle:
                    for row in csv.DictReader(handle):
                        surface = row["word"].strip()
                        key = app.normalize_vocab_key(surface)
                        if not key or key in seen:
                            continue
                        seen.add(key)
                        conn.execute("""INSERT INTO vocabulary_pool
                            (surface,base_form,normalized_key,jlpt_level,part_of_speech,category,source,status,is_active,priority)
                            VALUES (?,?,?,?,?,'general','openjlpt','active',1,1)""",
                                     (surface, surface, key, level, ""))
            for row in json.loads((ROOT / "data" / "grammar_seed_n5_n1.json").read_text(encoding="utf-8")):
                conn.execute("""INSERT INTO grammar_pool
                    (grammar_key,pattern,display_name,jlpt_level,meaning_seed,connection_seed,category,source,status,used_count)
                    VALUES (?,?,?,?,?,?,?,?,?,0)""", tuple(row.get(k, "") for k in
                        ["grammar_key", "pattern", "display_name", "jlpt_level", "meaning_seed", "connection_seed", "category", "source", "status"]))
            conn.commit()

    def history_frame(self):
        return app.pd.DataFrame([{"material_json": json.dumps(m, ensure_ascii=False), "material_date": m["date"]}
                                 for m in self.saved], columns=["material_json", "material_date"])

    def save(self, day, material, settings, **kwargs):
        day = str(day)
        material = copy.deepcopy(material)
        material["date"] = day
        self.saved.append(material)
        return {"material_key": f"{day}-1", "material_date": day, "version_no": 1, "selection_log_warnings": []}

    def gemini(self, prompt, **config):
        raw = prompt.split("Input candidates: ", 1)[1]
        inputs = json.JSONDecoder().raw_decode(raw)[0]["items"]
        item_type = "verb" if "d" in inputs[0] else "grammar" if "grammar_key" in inputs[0] else "word"
        self.calls.append((item_type, copy.deepcopy(inputs)))
        failure_key = (item_type, inputs[0]["level"])
        if failure_key in self.truncate_once:
            self.truncate_once.remove(failure_key)
            return '{"items": [['
        if failure_key in self.reject_once:
            self.reject_once.remove(failure_key)
            return json.dumps({"items": [], "rejected": [[inputs[0].get("d", inputs[0].get("w", "")), "not_verb", "word", "測試拒絕"]]})
        items = []
        for candidate in inputs:
            if item_type == "word":
                items.append([candidate["w"], "よみ", "意思", "名詞・形容動詞", "短い例です。", "簡短例句。"])
            elif item_type == "verb":
                d = candidate["d"]
                items.append([d, "よみ", "意思", 1, d + "ます", d + "て", d + "ない", d + "た", "短い例です。", "簡短例句。"])
            else:
                k = candidate["grammar_key"]
                items.append([k, candidate.get("pattern") or k, "意思", "接續", "結構", "用法", "短い例です。", "みじかいれいです。", "簡短例句。", "注意"])
        return json.dumps({"items": items}, ensure_ascii=False)

    def job(self, levels, day):
        settings = {"target_levels": levels, "target_level": levels[0], "vocab_count": 8,
                    "verb_count": 5, "grammar_count": 5, "mcq_count": 5, "fill_count": 3}
        steps = app.gemini_daily_pack_steps(settings)
        job = app.create_gemini_generation_job(settings, day, steps)
        return job, [step for step in steps if step.get("stage") == "daily_fresh"]

    def complete(self, job, steps):
        for step in steps:
            for _ in range(80):
                payload, status = app.run_gemini_daily_fresh_pack(job["job_id"], step["pack_type"])
                assert status == 200, payload
                if not payload.get("ok"):
                    return payload
                if not payload.get("continue_same_step"):
                    break
            else:
                raise AssertionError("unbounded micro-step loop")
        return app.finalize_gemini_generation_job(job["job_id"])[0]


class DailyFreshRegression(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.h = Harness()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.addCleanup(self.h.close)

    def tearDown(self):
        prefixes = ("[verb-replacement]", "[grammar-replacement]", "[gemini-finalize] final selected",
                    "[gemini-finalize] fallback_selected", "[gemini-cost] job_failed_before_enrich",
                    "[vocabulary-pool] source_scan_already_exhausted", "[vocabulary-pool] selector_id_query_ms=")
        lines = [line for line in self.output.getvalue().splitlines() if line.startswith(prefixes)]
        EVIDENCE[self._testMethodName] = lines[-30:]

    def assert_material(self, job, levels):
        material = self.h.saved[-1]
        self.assertEqual([len(material[k]) for k in ("vocab", "verbs", "grammar_points")], [8, 5, 5])
        self.assertFalse(material["metadata"]["bank_fallback_allowed"])
        self.assertEqual(material["metadata"]["fallback_count"], {"word": 0, "verb": 0, "grammar": 0})
        grammar = app.select_gemini_bank_items_by_batch("grammar", app.fixed_grammar_quota_for_settings(app.gemini_job_settings(job)), job["job_id"])
        self.assertEqual(len({r["normalized_key"] for r in grammar}), 5)
        summary = app.gemini_daily_batch_summary(job["job_id"])
        settings = app.gemini_job_settings(job)
        for item_type, quota in [
            ("word", app.build_level_quota(levels, 8, item_type="word")),
            ("verb", app.build_level_quota(levels, 5, item_type="verb")),
            ("grammar", app.fixed_grammar_quota_for_settings(settings)),
        ]:
            self.assertEqual(summary[item_type], quota)
            field = {"word": "vocab", "verb": "verbs", "grammar": "grammar_points"}[item_type]
            self.assertEqual(dict(Counter(row["jlpt_level"] for row in material[field])), quota)

    def sequence(self, levels, days, allow_exhaustion=False):
        histories = {"word": set(), "verb": set()}
        succeeded = 0
        calls_at_start = len(self.h.calls)
        for index in range(days):
            job, steps = self.h.job(levels, (dt.date(2026, 10, 1) + dt.timedelta(days=index)).isoformat())
            before = len(self.h.calls)
            payload = self.h.complete(job, steps)
            if not payload.get("ok"):
                self.assertTrue(allow_exhaustion, payload)
                self.assertEqual(payload["error"], "fresh_candidate_pool_insufficient", payload)
                self.assertEqual(len(self.h.calls), before, "exhaustion must be caught before enrichment")
                self.assertTrue(payload.get("missing"), payload)
                continue
            self.assert_material(job, levels)
            usage = app.gemini_bank_extract_material_usage_keys(self.h.saved[-1])
            for item_type in ("word", "verb"):
                overlap = usage[item_type] & (histories["word"] | histories["verb"])
                self.assertFalse(overlap, {"day": index, "item_type": item_type, "overlap": sorted(overlap),
                                           "vocab": self.h.saved[-1]["vocab"], "verbs": self.h.saved[-1]["verbs"]})
                histories[item_type].update(usage[item_type])
            succeeded += 1
        SCENARIOS.append({"levels": levels, "attempted_days": days, "successful_days": succeeded,
                          "exhausted_days": days - succeeded, "external_gemini_calls": 0,
                          "mock_calls_by_type": dict(Counter(t for t, _ in self.h.calls[calls_at_start:]))})
        return succeeded

    def test_A_all_levels_three_days(self):
        self.assertEqual(self.sequence(LEVELS, 3), 3)

    def test_B_n5_three_days(self):
        self.assertEqual(self.sequence(["N5"], 3), 3)

    def test_C_n1_three_days(self):
        self.assertEqual(self.sequence(["N1"], 3), 3)

    def test_D_thirty_days_with_real_source_exhaustion(self):
        completed = self.sequence(["N5"], 30, allow_exhaustion=True)
        self.assertGreaterEqual(completed, 3)
        print(f"[regression] thirty_day_n5 completed={completed} exhausted={completed < 30}", file=sys.__stdout__)

    def test_D_all_levels_thirty_days(self):
        completed = self.sequence(LEVELS, 30, allow_exhaustion=True)
        self.assertGreaterEqual(completed, 3)
        print(f"[regression] thirty_day_all_levels completed={completed} exhausted={completed < 30}", file=sys.__stdout__)

    def test_D_n1_thirty_days(self):
        completed = self.sequence(["N1"], 30, allow_exhaustion=True)
        self.assertGreaterEqual(completed, 3)
        print(f"[regression] thirty_day_n1 completed={completed} exhausted={completed < 30}", file=sys.__stdout__)

    def test_E_primary_rejected_same_level_reserve(self):
        self.h.reject_once.add(("verb", "N5"))
        job, steps = self.h.job(LEVELS, "2026-10-22")
        result = self.h.complete(job, steps)
        self.assertTrue(result["ok"], result)
        self.assert_material(job, LEVELS)
        self.assertIn("[verb-replacement] use_reserve level=N5", self.output.getvalue())

    def test_F_reserve_empty_source_refill(self):
        plan = app.select_daily_fresh_candidates("verb", {"N5": 1}, set(), "verb")
        plan = app.ensure_daily_fresh_candidate_plan_contract(plan, {"N5": 1}, "verb", "verb")
        original = app.daily_fresh_candidate_identity(plan["items"][0], "verb")
        plan = app.cached_daily_fresh_candidate_plan({"candidate_plan": json.loads(json.dumps(plan))})
        added = app.activate_daily_fresh_reserve_candidates(plan, "verb", {"N5": 1}, "empty", {0}, 1)
        self.assertEqual(added, 1)
        self.assertNotEqual(app.daily_fresh_candidate_identity(plan["items"][-1], "verb"), original)
        self.assertEqual(plan["items"][-1]["level"], "N5")

    def test_G_real_exhaustion_stops_before_gemini(self):
        verb_ids = [r["id"] for r in self.h.sql("SELECT * FROM vocabulary_pool WHERE jlpt_level = 'N5'")
                    if app.audit_verb_candidate(r, row=r)["accepted"]]
        self.h.sql(f"DELETE FROM vocabulary_pool WHERE id IN ({','.join('?' for _ in verb_ids)})", verb_ids)
        job, steps = self.h.job(LEVELS, "2026-10-22")
        payload = self.h.complete(job, steps)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["item_type"], "verb")
        self.assertEqual(payload["gemini_call_count"], 0)
        self.assertEqual(len(self.h.calls), 0)
        self.assertEqual(self.h.saved, [])
        self.assertTrue(any(m["level"] == "N5" and m["available"] == 0 for m in payload["missing"]))

    def test_H_refill_never_rescans_rejected_source(self):
        self.h.sql("DELETE FROM vocabulary_pool WHERE jlpt_level = 'N5'")
        self.h.sql("""INSERT INTO vocabulary_pool(surface,base_form,normalized_key,jlpt_level,category,source,status,is_active)
                      VALUES ('向こう','向こう','向こう','N5','general','openjlpt','active',1)""")
        state = {}
        first = app.fetch_daily_fresh_candidate_rows("verb", "N5", 1, set(), source_state=state)
        self.assertTrue(state["source_exhausted"])
        with patch.object(app.sqlite3, "connect", side_effect=AssertionError("exhausted source must not query again")):
            second = app.fetch_daily_fresh_candidate_rows("verb", "N5", 1, set(), source_state=state)
        self.assertTrue(first[-1]["candidate_exhausted"])
        self.assertEqual(second[-1]["rescanned_ids"], 0)
        plan = {"source_state_by_level": {"N5": state}, "material_history_keys": []}
        with patch.object(app, "fetch_daily_fresh_candidate_rows", side_effect=AssertionError("no exhausted-source refill")):
            for _ in range(3):
                self.assertIsNone(app.refill_daily_fresh_candidate_from_source(plan, "verb", "N5", set()))

    def test_I_verified_verb_positive_negative_damasu(self):
        for text in ["向こう", "遠く", "ずるずる", "どうぞ宜しく", "或る", "お邪魔します", "ほんとう"]:
            self.assertFalse(app.audit_verb_candidate({"d": text, "verb_group": 1})["accepted"], text)
        for text in ["だます", "コピーする", "あいさつする", "引っ掻く", "食べる", "待つ"]:
            self.assertTrue(app.audit_verb_candidate({"d": text})["accepted"], text)
        self.assertFalse(app.verb_pos_allows_candidate("adverb"))
        self.assertFalse(app.verb_pos_allows_candidate("形容動詞"))

    def test_J_word_compound_pos_cache_strict_fresh(self):
        for pos in ["名詞・形容動詞", "名詞/副詞", "形容動詞・副詞"]:
            self.assertTrue(app.word_pos_allows_enriched_item(pos))
        plan = app.select_daily_fresh_candidates("word", {"N5": 1}, set(), "word_basic")
        candidate = plan["items"][0]
        payload = json.dumps({"items": [[candidate["w"], "よみ", "意思", "名詞", "例です。", "例句。"]]}, ensure_ascii=False)
        rows, _ = app.parse_gemini_bank_items_with_stats("word", "N5", payload)
        app.upsert_gemini_bank_items(rows)
        second = app.select_daily_fresh_candidates("word", {"N5": 1}, set(), "word_basic")
        self.assertEqual(second["items"][0]["w"], candidate["w"], "bank is only a completion cache")
        reused = app.tag_existing_bank_cache_item_for_daily_batch("word", candidate, "cache-test", "2026-10-22")
        self.assertEqual(reused["reused"], 1)
        self.assertFalse(reused["gemini_call"])
        third = app.select_daily_fresh_candidates("word", {"N5": 1}, {candidate["normalized_key"]}, "word_basic")
        self.assertNotEqual(third["items"][0]["w"], candidate["w"])

    def test_K_n4_grammar_truncated_same_level_rotation(self):
        self.h.truncate_once.add(("grammar", "N4"))
        job, steps = self.h.job(LEVELS, "2026-10-22")
        result = self.h.complete(job, steps)
        self.assertTrue(result["ok"], result)
        self.assert_material(job, LEVELS)
        self.assertIn("[grammar-replacement] use_reserve level=N4", self.output.getvalue())
        self.assertIn("json_truncated", self.output.getvalue())

    def test_L_failed_job_does_not_mark_history_used(self):
        job, steps = self.h.job(LEVELS, "2026-10-22")
        with patch.object(app, "save_material_for_date", side_effect=RuntimeError("simulated save failure")):
            result = self.h.complete(job, steps)
        self.assertFalse(result["ok"])
        self.assertEqual(self.h.saved, [])
        self.assertEqual(self.h.sql("SELECT COUNT(*) n FROM gemini_item_bank WHERE used_count > 0")[0]["n"], 0)
        self.assertEqual(self.h.sql("SELECT COUNT(*) n FROM grammar_pool WHERE used_count > 0")[0]["n"], 0)

    def test_M_no_cross_level_primary_or_reserve(self):
        plan = {"candidates_by_level": {"N5": [], "N4": [{"d": "食べる", "level": "N4"}]},
                "requested_by_level": {"N5": 2, "N4": 1}}
        result = app.ensure_daily_fresh_candidate_plan_contract(plan, {"N5": 1, "N4": 1}, "verb", "verb")
        self.assertEqual(result["effective_quota_by_level"], {"N5": 1, "N4": 1})
        self.assertEqual(result["missing"][0]["level"], "N5")
        self.assertTrue(result["reserve_shortages"])
        cached_payload = app.bank_rows_to_payload([{"jlpt_level": "N1", "payload_json": '{"jlpt_level":"N5","l":"N5"}'}])[0]
        self.assertEqual(cached_payload, {"jlpt_level": "N1", "l": "N1"})

    def test_N_materials_only_all_history_no_recent_blacklist(self):
        self.h.saved = [{"date": "2000-01-01", "vocab": [{"word": "過去語"}], "verbs": []}]
        self.h.saved += [{"date": "2026-10-01", "vocab": [], "verbs": []} for _ in range(601)]
        keys = app.gemini_bank_material_history_keys_for_item_type("word")
        self.assertIn(app.normalize_vocab_key("過去語"), keys)
        with patch.object(app, "gemini_bank_recent_usage_keys", side_effect=AssertionError("recent exclusion forbidden")), \
             patch.object(app, "gemini_daily_duplicate_blacklist_keys", side_effect=AssertionError("blacklist exclusion forbidden")):
            plan = app.select_daily_fresh_candidates("verb", {"N5": 2}, keys, "verb")
        self.assertEqual(len(plan["items"]), 2)
        annotated = {"surface": "あげる (=やる)", "normalized_key": "あげる(=やる)", "jlpt_level": "N3"}
        self.assertEqual(app.daily_fresh_candidate_rejection_reason(annotated, "word", "N3", {"あげる"}),
                         "excluded_material_history_or_current_batch")
        self.assertFalse(app.is_daily_fresh_word_pool_row(annotated))

    def test_O_replay_job_no_duplicate_calls_or_materials(self):
        job, steps = self.h.job(LEVELS, "2026-10-22")
        result = self.h.complete(job, steps)
        self.assertTrue(result["ok"], result)
        calls = len(self.h.calls)
        usage = self.h.sql("SELECT id,used_count FROM gemini_item_bank ORDER BY id")
        for step in steps:
            payload, _ = app.run_gemini_daily_fresh_pack(job["job_id"], step["pack_type"])
            self.assertTrue(payload["already_done"])
        replay, _ = app.finalize_gemini_generation_job(job["job_id"])
        self.assertEqual(replay["material_key"], result["material_key"])
        self.assertEqual(len(self.h.calls), calls)
        self.assertEqual(len(self.h.saved), 1)
        self.assertEqual(self.h.sql("SELECT id,used_count FROM gemini_item_bank ORDER BY id"), usage)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args, remaining = parser.parse_known_args()
    program = unittest.main(argv=[sys.argv[0], *remaining], verbosity=2, exit=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({
            "tests_run": program.result.testsRun, "failures": len(program.result.failures),
            "errors": len(program.result.errors), "successful": program.result.wasSuccessful(),
            "scope": "temporary SQLite + checked-in OpenJLPT files; model output and material persistence mocked",
            "production_postgres_verified": False, "real_gemini_calls": 0,
            "scenarios": SCENARIOS, "evidence": EVIDENCE,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.exit(0 if program.result.wasSuccessful() else 1)
