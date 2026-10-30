"""Source-to-selector and real persistence tests; no external Gemini requests."""

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
import time
import unittest
import uuid
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from test_daily_fresh_generation import Harness, LEVELS, app
from services.candidate_selector import select_slice

EVIDENCE = {}


class SelectorRegression(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.h = Harness()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.addCleanup(self.h.close)

    def tearDown(self):
        EVIDENCE[f"{type(self).__name__}.{self._testMethodName}"] = self.output.getvalue().splitlines()[-45:]

    def late_fixture(self, level, count, category, surface_prefix="compound"):
        valid = [row for row in self.h.sql("SELECT * FROM vocabulary_pool WHERE jlpt_level=?", (level,))
                 if app.is_daily_fresh_word_pool_row(row)][:8]
        self.assertGreaterEqual(len(valid), 3)
        self.h.sql("DELETE FROM vocabulary_pool")
        with sqlite3.connect(self.h.path) as conn:
            conn.executemany("""INSERT INTO vocabulary_pool
                (id,surface,base_form,normalized_key,jlpt_level,category,source,status,is_active)
                VALUES (?,?,?,?,?,?,'openjlpt','active',1)""",
                [(i, f"{surface_prefix}({i})", f"{surface_prefix}({i})", f"reject-{i}", level, category)
                 for i in range(1, count + 1)])
            conn.executemany("""INSERT INTO vocabulary_pool
                (id,surface,base_form,normalized_key,jlpt_level,category,source,status,is_active)
                VALUES (?,?,?,?,?,'general','openjlpt','active',1)""",
                [(count + i, r["surface"], r["base_form"], r["normalized_key"], level) for i, r in enumerate(valid, 1)])
        return valid

    def test_n2_blocked_2400_then_openjlpt(self):
        self.late_fixture("N2", 2400, "business")
        state = {}
        started = time.perf_counter()
        rows = app.fetch_daily_fresh_candidate_rows("word", "N2", 3, set(), source_state=state)
        self.assertEqual(len(rows[:-1]), 3)
        self.assertLess(state["scanned_ids"], 20, "forbidden rows must be filtered before hydration")
        self.assertLess(time.perf_counter() - started, 5)

    def test_n1_blocked_2400_then_openjlpt(self):
        self.late_fixture("N1", 2400, "advanced")
        rows = app.fetch_daily_fresh_candidate_rows("word", "N1", 3, set())
        self.assertEqual(len(rows[:-1]), 3)

    def test_keyset_continues_after_slice_limit(self):
        self.late_fixture("N2", 4000, "general", "食べる")
        self.h.sql("DELETE FROM vocabulary_pool WHERE id=100")
        self.h.sql("UPDATE vocabulary_pool SET id=100 WHERE id=4001")
        first = app.select_daily_fresh_candidates("word", {"N2": 3}, set(), "word_advanced")
        self.assertFalse(first["preparation_complete"])
        self.assertFalse(first["missing"])
        self.assertEqual(len(first["items"]), 1, "partial selected candidates must survive continuation")
        self.assertEqual(first["source_state_by_level"]["N2"]["last_id"], 3200)
        cached = app.cached_daily_fresh_candidate_plan({"candidate_plan": json.loads(json.dumps(first))})
        second = app.select_daily_fresh_candidates("word", {"N2": 3}, set(), "word_advanced", previous_plan=cached)
        self.assertEqual(len(second["items"]), 3)
        self.assertTrue(second["preparation_complete"])
        self.assertLess(second["source_state_by_level"]["N2"]["scanned_ids"], 4020)
        self.assertEqual(second["items"][0], first["items"][0])

    def test_refill_continuation_is_not_candidate_exhaustion(self):
        self.late_fixture("N1", 4000, "general", "食べる")
        failed = {"w": "失敗候補", "level": "N1", "normalized_key": "失敗候補"}
        plan = {"items": [failed], "effective_quota_by_level": {"N1": 1}, "material_history_keys": []}
        first = app.activate_daily_fresh_reserve_candidates(plan, "word", {"N1": 1}, "refill-scan", {0}, 2)
        self.assertEqual(first, 0)
        self.assertTrue(plan["replacement_scan_pending"]["N1"])
        self.assertFalse((plan.get("reserve_exhausted_by_level") or {}).get("N1"))
        second = app.activate_daily_fresh_reserve_candidates(plan, "word", {"N1": 1}, "refill-scan", {0}, 2)
        self.assertEqual(second, 1)
        self.assertEqual(plan["items"][-1]["level"], "N1")
        self.assertFalse(plan["replacement_scan_pending"])
        self.assertEqual(self.h.calls, [])

    def test_n5_word_primary_reserve_and_isu(self):
        self.assertTrue(app.is_daily_fresh_word_pool_row({"surface": "いす"}))
        for text in ("だます", "食べる", "コピーする"):
            self.assertFalse(app.is_daily_fresh_word_pool_row({"surface": text}), text)
        plan = app.select_daily_fresh_candidates("word", {"N5": 4}, set(), "word_basic")
        plan = app.ensure_daily_fresh_candidate_plan_contract(plan, {"N5": 2}, "word", "word_basic")
        self.assertEqual(len(plan["primary_items"]), 2)
        self.assertEqual(len(plan["reserve_items"]), 2)

    def test_prepare_pending_cache_and_zero_api(self):
        self.late_fixture("N5", 4000, "general", "食べる")
        job, steps = self.h.job(["N5"], "2026-10-24")
        payload, _ = app.run_gemini_daily_fresh_pack(job["job_id"], steps[0]["pack_type"])
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(payload["continue_same_step"])
        self.assertEqual(self.h.calls, [])
        saved = app.load_gemini_generation_job(job["job_id"])
        _, _, state, _ = app.gemini_daily_fresh_pack_state(saved, steps[0])
        self.assertEqual(state["candidate_plan"]["source_state_by_level"]["N5"]["last_id"], 3200)
        self.assertFalse(app.gemini_daily_fresh_all_candidates_ready(saved))
        # A continued scan will discover missing required verbs, still before API use.
        result = self.h.complete(job, steps)
        self.assertEqual(result["error"], "classifier_uncertainty", result)
        self.assertFalse(result["missing"][0]["candidate_exhausted"])
        self.assertEqual(result["missing"][0]["classifier_unknown_count"], 4000)
        self.assertEqual(result["gemini_call_count"], 0)
        self.assertEqual(self.h.calls, [])

    def test_timeout_progress_preserved_and_terminal_classification(self):
        state = {}
        for _ in range(3):
            rows = app.fetch_daily_fresh_candidate_rows("word", "N5", 4, set(), source_state=state,
                                                       deadline=time.perf_counter() - 1)
        self.assertEqual(rows[-1]["selector_error"], "selector_timeout")
        self.assertFalse(rows[-1]["candidate_exhausted"])
        plan = {"items": [], "selector_error": "selector_timeout"}
        self.assertEqual(app.gemini_daily_fresh_plan_error(plan, "word", 4), "selector_timeout")
        rows = app.fetch_daily_fresh_candidate_rows("word", "N5", 4, set(), source_state=state)
        self.assertEqual(len(rows[:-1]), 4)

    def test_bad_utf8_row_does_not_erase_good_rows(self):
        self.h.sql("DELETE FROM vocabulary_pool")
        self.h.sql("""INSERT INTO vocabulary_pool (surface,base_form,normalized_key,jlpt_level,category,source,status,is_active)
                      VALUES (CAST(x'e5' AS TEXT),'broken','broken','N5','general','openjlpt','active',1)""")
        for surface in ("いす", "時間", "天気"):
            self.h.sql("""INSERT INTO vocabulary_pool (surface,base_form,normalized_key,jlpt_level,category,source,status,is_active)
                          VALUES (?,?,?,'N5','general','openjlpt','active',1)""", (surface, surface, surface))
        state = {}
        rows = app.fetch_daily_fresh_candidate_rows("word", "N5", 2, set(), source_state=state)
        self.assertEqual(len(rows[:-1]), 2)
        self.assertEqual(len(state["bad_rows"]), 1)
        self.assertIn("bad_candidate_row id=", self.output.getvalue())

    def test_recursive_hydration_isolates_driver_decode_error(self):
        # Fault injection is only for the driver decoding fault; SQL still runs on SQLite.
        self.late_fixture("N5", 1, "business")
        class Cursor:
            def __init__(self, cursor):
                self.inner = cursor
            def execute(self, sql, params):
                self.hydration = "vp.id IN" in sql
                self.params = params
                return self.inner.execute(sql, params)
            @property
            def description(self):
                return self.inner.description
            def fetchall(self):
                if self.hydration and 2 in self.params:
                    raise UnicodeDecodeError("utf-8", b"\xe5", 0, 1, "injected driver decode")
                return self.inner.fetchall()
            def close(self):
                self.inner.close()
        class Connection:
            def __init__(self, conn):
                self.conn = conn
            def cursor(self):
                return Cursor(self.conn.cursor())
            def set_progress_handler(self, *args):
                self.conn.set_progress_handler(*args)
        with sqlite3.connect(self.h.path) as conn:
            state = {}
            rows = select_slice(Connection(conn), postgres=False, item_type="word", level="N5", limit=3,
                                state=state, excluded=set(), blocked_categories=app.DAILY_FRESH_BLOCKED_CATEGORIES,
                                blocked_sources=app.DAILY_FRESH_BLOCKED_SOURCES,
                                min_commonness=app.VOCABULARY_POOL_MIN_COMMONNESS_SCORE,
                                sanitize=app.sanitize_vocabulary_pool_candidate_row,
                                reject=app.daily_fresh_candidate_rejection_reason,
                                candidate_key=app.vocabulary_pool_candidate_key)
        self.assertEqual(len(rows[:-1]), 3)
        self.assertEqual(state["bad_rows"], [2])

    def test_material_history_only_canonical_terms(self):
        self.h.saved = [{"date": "2026-10-22", "vocab": [{"word": "いす（椅子）", "normalized_key": "wrong-label", "meaning": "椅子"}],
                         "verbs": [{"dictionary_form": "だます", "meaning_zh": "欺騙", "example_sentence": "未使用"}],
                         "grammar_points": [{"title": "文法"}], "metadata": {"failed_candidate": "時間"}}]
        self.assertEqual(app.gemini_bank_material_history_keys_for_item_type("word"), {"いす", "だます"})

    def test_real_prepare_enrich_finalize_save_reload(self):
        job, steps = self.h.job(LEVELS, "2026-10-24")
        result = self.h.complete(job, steps)
        self.assertTrue(result["ok"], result)
        material = app.material_by_key(result["material_key"])
        self.assertEqual([len(material[k]) for k in ("vocabulary", "verbs", "grammar_points")], [8, 5, 5])
        self.assertEqual(material["metadata"]["fallback_count"], {"word": 0, "verb": 0, "grammar": 0})
        client = app.app.test_client()
        response = client.get("/api/materials", query_string={"material_key": result["material_key"]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_json)
        before = len(self.h.calls)
        replay = app.finalize_gemini_generation_job(job["job_id"])[0]
        self.assertEqual(replay["material_key"], result["material_key"])
        self.assertEqual(len(self.h.calls), before)
        self.assertEqual(len(self.h.saved), 1)


class PostgreSQLRegression(SelectorRegression):
    """Run actual PostgreSQL queries, never mock SQL execution or response rows."""

    def setUp(self):
        dsn = os.environ.get("TEST_SELECTOR_POSTGRES_DSN")
        if not dsn:
            self.skipTest("TEST_SELECTOR_POSTGRES_DSN not supplied; actual PostgreSQL NOT verified")
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        if conninfo_to_dict(dsn).get("host") not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("Regression only accepts an isolated local PostgreSQL instance")
        super().setUp()
        pool = self.h.sql("SELECT * FROM vocabulary_pool ORDER BY id")
        grammar = self.h.sql("SELECT * FROM grammar_pool ORDER BY id")
        schema = "selector_test_" + uuid.uuid4().hex
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f"CREATE SCHEMA {schema}")
        def cleanup():
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(f"DROP SCHEMA {schema} CASCADE")
        self.addCleanup(cleanup)
        self.h.stack.enter_context(patch.object(app, "DATABASE_URL", dsn))
        self.h.stack.enter_context(patch.object(app, "get_db_connection", lambda: psycopg.connect(dsn, options=f"-c search_path={schema}")))
        for name in ("_GEMINI_JOBS_SCHEMA_READY", "_GEMINI_BANK_SCHEMA_READY", "_SETTINGS_SCHEMA_READY"):
            self.h.stack.enter_context(patch.object(app, name, False))
        app._ensure_database_uncached()
        app.ensure_gemini_item_bank_store()
        with app.get_db_connection() as conn:
            for table, records in (("vocabulary_pool", pool), ("grammar_pool", grammar)):
                columns = list(records[0])
                with conn.cursor() as cur:
                    cur.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))})",
                                    [tuple(bool(row[c]) if c == "is_active" else row[c] or None if c in {"last_used_at", "created_at", "updated_at"} else row[c] for c in columns) for row in records])
                    cur.execute(f"SELECT setval(pg_get_serial_sequence('{table}','id'), (SELECT MAX(id) FROM {table}))")
        def sql(sql, params=()):
            with app.get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql.replace("?", "%s"), params)
                    return [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()] if cur.description else []
        self.h.sql = sql

    def late_fixture(self, level, count, category, surface_prefix="compound"):
        valid = [r for r in self.h.sql("SELECT * FROM vocabulary_pool WHERE jlpt_level=?", (level,))
                 if app.is_daily_fresh_word_pool_row(r)][:8]
        self.h.sql("DELETE FROM vocabulary_pool")
        with app.get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.executemany("""INSERT INTO vocabulary_pool
                    (id,surface,base_form,normalized_key,jlpt_level,category,source,status,is_active)
                    VALUES (%s,%s,%s,%s,%s,%s,'openjlpt','active',TRUE)""",
                    [(i, f"{surface_prefix}({i})", f"{surface_prefix}({i})", f"reject-{i}", level, category)
                     for i in range(1, count + 1)])
                cur.executemany("""INSERT INTO vocabulary_pool
                    (id,surface,base_form,normalized_key,jlpt_level,category,source,status,is_active)
                    VALUES (%s,%s,%s,%s,%s,'general','openjlpt','active',TRUE)""",
                    [(count+i, r["surface"], r["base_form"], r["normalized_key"], level) for i, r in enumerate(valid, 1)])
        return valid

    # SQLite corruption/fault-injection and CSV-specific setup are not PostgreSQL tests.
    def test_bad_utf8_row_does_not_erase_good_rows(self):
        self.skipTest("PostgreSQL UTF8 rejects invalid text on insert; SQLite corruption test is separate")

    def test_recursive_hydration_isolates_driver_decode_error(self):
        self.skipTest("Driver fault injection is explicitly a SQLite-backed unit test")

    def test_material_history_only_canonical_terms(self):
        self.skipTest("Covered by SQLite real material CSV; PG E2E separately reads actual materials table")

    def test_actual_postgres_statement_timeout_then_resume(self):
        self.h.sql("ALTER TABLE vocabulary_pool RENAME TO pool_original")
        self.h.sql("CREATE VIEW vocabulary_pool AS SELECT * FROM pool_original WHERE pg_sleep(0.01) IS NOT NULL")
        state = {}
        with app.app.test_request_context("/api/generate/gemini-step"):
            rows = app.fetch_daily_fresh_candidate_rows("word", "N5", 2, set(), source_state=state,
                                                       deadline=time.perf_counter() + 1.0)
        self.assertTrue(rows[-1]["selector_timeout_warning"])
        self.assertEqual(rows[-1]["timeout_kind"], "database_statement_timeout")
        self.assertFalse(rows[-1]["candidate_exhausted"])
        self.h.sql("DROP VIEW vocabulary_pool")
        self.h.sql("ALTER TABLE pool_original RENAME TO vocabulary_pool")
        resumed = app.fetch_daily_fresh_candidate_rows("word", "N5", 2, set(), source_state=state)
        self.assertEqual(len(resumed[:-1]), 2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args, remaining = parser.parse_known_args()
    program = unittest.main(argv=[sys.argv[0], *remaining], verbosity=2, exit=False)
    if args.report:
        args.report.write_text(json.dumps({
            "tests_run": program.result.testsRun, "failures": len(program.result.failures), "errors": len(program.result.errors),
            "skipped": [(str(t), reason) for t, reason in program.result.skipped],
            "successful": program.result.wasSuccessful(), "real_gemini_calls": 0,
            "local_postgresql_requested": bool(os.environ.get("TEST_SELECTOR_POSTGRES_DSN")),
            "production_postgresql_verified": False, "evidence": EVIDENCE,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.exit(0 if program.result.wasSuccessful() else 1)
