import copy
import datetime as dt
import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"
sys.path.insert(0, str(ROOT))


spec = importlib.util.spec_from_file_location("daily_fresh_app", APP_PATH)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def candidate(level, surface, item_type):
    if item_type == "verb":
        return {"level": level, "d": surface, "normalized_key": app.normalize_vocab_key(surface)}
    if item_type == "grammar":
        return {"level": level, "grammar_key": surface, "normalized_key": app.normalize_vocab_key(surface)}
    return {"level": level, "w": surface, "normalized_key": app.normalize_vocab_key(surface)}


def build_plan(levels, quota_by_level, reserve_per_level, item_type, seed=""):
    candidates_by_level = {}
    for level in levels:
        total = int(quota_by_level.get(level, 0)) + reserve_per_level
        candidates_by_level[level] = [
            candidate(level, f"{item_type}_{seed}_{level}_{index}", item_type)
            for index in range(total)
        ]
    return {"candidates_by_level": candidates_by_level, "items": [item for row in candidates_by_level.values() for item in row], "missing": []}


def quota_for_total(levels, total):
    quota = {level: 0 for level in levels}
    index = 0
    while sum(quota.values()) < total:
        quota[levels[index % len(levels)]] += 1
        index += 1
    return quota


def assert_primary_reserve_contract():
    levels = ["N5", "N4", "N3", "N2", "N1"]
    quota = {level: 1 for level in levels}
    for item_type in ["word", "verb", "grammar"]:
        plan = build_plan(levels, quota, 2, item_type)
        plan = app.ensure_daily_fresh_candidate_plan_contract(plan, quota, item_type, f"{item_type}_mock")
        assert len(plan["items"]) == 5, f"{item_type} primary should be quota only"
        assert len(plan["reserve_items"]) == 10, f"{item_type} reserve should stay inactive"
        assert {level: len(items) for level, items in plan["primary_candidates_by_level"].items()} == quota
        assert {level: len(items) for level, items in plan["reserve_candidates_by_level"].items()} == {level: 2 for level in levels}


def assert_word_advanced_required_level_reserve():
    steps = app.gemini_daily_pack_steps(
        {
            "target_levels": ["N5", "N4", "N3", "N2", "N1"],
            "target_level": "N5",
            "vocab_count": 8,
            "verb_count": 5,
        }
    )
    word_advanced = next(step for step in steps if step.get("pack_type") == "word_advanced")
    quota = word_advanced["quota_by_level"]
    requested = word_advanced["requested_by_level"]
    for level, count in quota.items():
        assert requested.get(level, 0) >= count + app.GEMINI_DAILY_WORD_RESERVE_PER_LEVEL
    assert requested.get("N1", 0) >= quota.get("N1", 0) + app.GEMINI_DAILY_WORD_RESERVE_PER_LEVEL


def assert_verb_filter_contract():
    blocked = [
        "お早う",
        "おはよう",
        "お目出度う",
        "おめでとう",
        "ありがとう",
        "すみません",
        "いただきます",
        "ごちそうさま",
        "こんにちは",
        "こんばんは",
        "さようなら",
        "お邪魔します",
        "お願いします",
        "失礼します",
        "ございます",
    ]
    for surface in blocked:
        audit = app.audit_verb_candidate({"d": surface})
        assert not audit["accepted"], f"{surface} must not be a verb candidate"
    allowed_suru = ["あいさつする", "挨拶する", "コピーする", "勉強する", "確認する", "準備する", "連絡する", "発表する"]
    for surface in allowed_suru:
        audit = app.audit_verb_candidate({"d": surface})
        assert audit["accepted"], f"{surface} should be accepted as サ変動詞 candidate"


def assert_word_filter_contract():
    verb_like = ["あげる", "食べる", "行く", "見る", "コピーする"]
    for surface in verb_like:
        row = {"surface": surface, "normalized_key": app.normalize_vocab_key(surface), "jlpt_level": "N5", "part_of_speech": ""}
        assert not app.is_daily_fresh_word_pool_row(row), f"{surface} should not enter word candidates"
    word_like = ["大切", "不便", "丁寧", "きれい", "あいまい"]
    for surface in word_like:
        row = {"surface": surface, "normalized_key": app.normalize_vocab_key(surface), "jlpt_level": "N5", "part_of_speech": "形容動詞"}
        assert app.is_daily_fresh_word_pool_row(row), f"{surface} should remain valid word material"
    compound_pos = ["名詞・形容動詞", "名詞/形容動詞", "名詞・副詞", "形容動詞・副詞", "名詞・サ変接続"]
    for pos in compound_pos:
        assert app.word_pos_allows_enriched_item(pos), f"{pos} should be valid word POS"
    assert not app.word_pos_allows_enriched_item("動詞")


def assert_same_level_reserve_activation_contract():
    quota = {"N5": 1}
    plan = build_plan(["N5"], quota, 2, "verb", seed="reserve")
    plan = app.ensure_daily_fresh_candidate_plan_contract(plan, quota, "verb", "verb_mock")
    primary = list(plan["items"])
    assert len(primary) == 1
    original_summary = app.gemini_daily_batch_summary
    try:
        app.gemini_daily_batch_summary = lambda _batch_id: {"verb": {"N5": 0}}
        added = app.activate_daily_fresh_reserve_candidates(
            plan,
            "verb",
            quota,
            "mock-batch",
            completed_chunks={0},
            chunk_size=1,
            pack_type="verb",
            last_result={"inserted": 0, "candidate_rejected": 1, "rejection_reason": "rejected_by_gemini"},
            last_chunk_candidates=primary,
        )
        assert added == 1
        assert len(plan["items"]) == 2, "only one same-level reserve should be activated"
        assert len(plan["reserve_items"]) == 1, "remaining reserve should stay inactive"
        app.gemini_daily_batch_summary = lambda _batch_id: {"verb": {"N5": 1}}
        added = app.activate_daily_fresh_reserve_candidates(
            plan,
            "verb",
            quota,
            "mock-batch",
            completed_chunks={0, 1},
            chunk_size=1,
            pack_type="verb",
            last_result={"inserted": 1},
            last_chunk_candidates=[plan["items"][1]],
        )
        assert added == 0, "quota-satisfied level must not activate extra reserve"
        assert len(plan["items"]) == 2
    finally:
        app.gemini_daily_batch_summary = original_summary


def assert_refill_from_source_contract():
    quota = {"N1": 1}
    plan = build_plan(["N1"], quota, 0, "word", seed="refill")
    plan = app.ensure_daily_fresh_candidate_plan_contract(plan, quota, "word", "word_mock")
    primary = list(plan["items"])
    original_summary = app.gemini_daily_batch_summary
    original_fetch = app.fetch_daily_fresh_candidate_rows
    try:
        app.gemini_daily_batch_summary = lambda _batch_id: {"word": {"N1": 0}}
        app.fetch_daily_fresh_candidate_rows = lambda item_type, level, limit, excluded_keys, current_batch_keys=None: [
            {
                "id": 1,
                "surface": "補充語",
                "normalized_key": app.normalize_vocab_key("補充語"),
                "jlpt_level": level,
                "part_of_speech": "名詞",
                "category": "general",
                "source": "mock",
                "status": "active",
            }
        ]
        added = app.activate_daily_fresh_reserve_candidates(
            plan,
            "word",
            quota,
            "mock-batch",
            completed_chunks={0},
            chunk_size=2,
            pack_type="word_advanced",
            last_result={"inserted": 0, "candidate_rejected": 1, "rejection_reason": "invalid_pos"},
            last_chunk_candidates=primary,
        )
        assert added == 1
        assert len(plan["items"]) == 2
        assert plan["items"][-1]["w"] == "補充語"
        assert plan["reserve_refill_attempts_by_level"]["N1"] == 1
    finally:
        app.gemini_daily_batch_summary = original_summary
        app.fetch_daily_fresh_candidate_rows = original_fetch


def assert_bank_cache_reuse_contract():
    original_db_url = app.DATABASE_URL
    original_settings = app.SQLITE_SETTINGS_FILE
    original_ready = app._GEMINI_BANK_SCHEMA_READY
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = str(Path(tmpdir) / "cache-test.sqlite3")
            app.DATABASE_URL = ""
            app.SQLITE_SETTINGS_FILE = db_path
            app._GEMINI_BANK_SCHEMA_READY = False
            app.ensure_gemini_item_bank_store()
            payload = {
                "w": "確認",
                "r": "かくにん",
                "m": "確認",
                "p": "名詞",
                "l": "N3",
                "ex": "内容を確認します。",
                "ex_zh": "確認內容。",
                "word": "確認",
                "reading": "かくにん",
                "meaning": "確認",
                "part_of_speech": "名詞",
                "jlpt_level": "N3",
                "normalized_key": app.normalize_vocab_key("確認"),
            }
            now = app.utc_now_iso()
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO gemini_item_bank (
                        item_type, normalized_key, display_text, reading, jlpt_level, category, source,
                        status, payload_json, used_count, first_used_at, last_used_at, created_at, updated_at,
                        daily_batch_id, generated_for_date, generated_source
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, '', NULL, '')
                    """,
                    (
                        "word",
                        app.normalize_vocab_key("確認"),
                        "確認",
                        "かくにん",
                        "N3",
                        "general",
                        "mock",
                        "unused",
                        json.dumps(payload, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                conn.commit()
            result = app.tag_existing_bank_cache_item_for_daily_batch(
                "word",
                {"level": "N3", "w": "確認", "normalized_key": app.normalize_vocab_key("確認")},
                daily_batch_id="mock-batch",
                generated_for_date="2026-09-01",
            )
            assert result["reused"] == 1
            assert result["gemini_call"] is False
            with sqlite3.connect(db_path) as conn:
                row = conn.execute("SELECT daily_batch_id, generated_source FROM gemini_item_bank WHERE normalized_key = ?", (app.normalize_vocab_key("確認"),)).fetchone()
            assert row[0] == "mock-batch"
            assert row[1] == "word_bank_cache_reuse"
    finally:
        app.DATABASE_URL = original_db_url
        app.SQLITE_SETTINGS_FILE = original_settings
        app._GEMINI_BANK_SCHEMA_READY = original_ready


def simulate_three_days(levels):
    history = {"word": set(), "verb": set()}
    grammar_rotation_seen_by_level = {level: [] for level in levels}
    start = dt.date(2026, 9, 1)
    for day_offset in range(3):
        material_date = start + dt.timedelta(days=day_offset)
        word_quota = quota_for_total(levels, 8)
        verb_quota = quota_for_total(levels[:5], 5)
        grammar_quota = quota_for_total(levels[:5], 5)

        for item_type, quota in [("word", word_quota), ("verb", verb_quota), ("grammar", grammar_quota)]:
            plan = build_plan(list(quota.keys()), quota, 2, item_type, seed=f"d{day_offset}")
            if item_type in {"word", "verb"}:
                for level, rows in plan["candidates_by_level"].items():
                    for row in rows:
                        key = app.daily_fresh_candidate_identity(row, item_type)
                        assert key not in history[item_type]
                selected_keys = []
                normalized = app.ensure_daily_fresh_candidate_plan_contract(copy.deepcopy(plan), quota, item_type, f"{item_type}_mock")
                for row in normalized["items"]:
                    selected_keys.append(app.daily_fresh_candidate_identity(row, item_type))
                expected_total = 8 if item_type == "word" else 5
                assert len(selected_keys) == expected_total
                assert len(selected_keys) == len(set(selected_keys))
                history[item_type].update(selected_keys)
            else:
                normalized = app.ensure_daily_fresh_candidate_plan_contract(copy.deepcopy(plan), quota, item_type, "grammar_mock")
                selected = normalized["items"]
                assert len(selected) == 5
                assert len(selected) == len({app.daily_fresh_candidate_identity(row, item_type) for row in selected})
                for row in selected:
                    grammar_rotation_seen_by_level[row["level"]].append((material_date.isoformat(), row["grammar_key"]))

    return history, grammar_rotation_seen_by_level


def assert_three_day_mock_contracts():
    scenarios = {
        "all_levels": ["N5", "N4", "N3", "N2", "N1"],
        "n5_only": ["N5"],
        "n1_only": ["N1"],
    }
    for name, levels in scenarios.items():
        history, grammar_seen = simulate_three_days(levels)
        assert history["word"], f"{name} word history should be populated"
        assert history["verb"], f"{name} verb history should be populated"
        for level in levels:
            assert grammar_seen[level], f"{name} grammar rotation should select {level}"


def assert_grammar_rotation_reuse_contract():
    quota = {"N5": 5}
    plan = {
        "candidates_by_level": {
            "N5": [candidate("N5", f"n5:grammar_{index % 2}", "grammar") for index in range(7)]
        },
        "items": [],
        "missing": [],
    }
    normalized = app.ensure_daily_fresh_candidate_plan_contract(plan, quota, "grammar", "grammar_mock")
    assert len(normalized["items"]) == 5
    assert len(normalized["reserve_items"]) == 2


def main():
    assert_primary_reserve_contract()
    assert_word_advanced_required_level_reserve()
    assert_verb_filter_contract()
    assert_word_filter_contract()
    assert_same_level_reserve_activation_contract()
    assert_refill_from_source_contract()
    assert_bank_cache_reuse_contract()
    assert_grammar_rotation_reuse_contract()
    assert_three_day_mock_contracts()
    print("[daily-fresh-contracts] PASS primary_reserve=true")
    print("[daily-fresh-contracts] PASS word_advanced_required_level_reserve=true")
    print("[daily-fresh-contracts] PASS verb_filter=true")
    print("[daily-fresh-contracts] PASS word_filter=true")
    print("[daily-fresh-contracts] PASS same_level_reserve_activation=true")
    print("[daily-fresh-contracts] PASS refill_from_source=true")
    print("[daily-fresh-contracts] PASS bank_cache_reuse=true")
    print("[daily-fresh-contracts] PASS grammar_rotation_reuse=true")
    print("[daily-fresh-contracts] PASS three_day_mock all_levels=true n5_only=true n1_only=true")


if __name__ == "__main__":
    main()
