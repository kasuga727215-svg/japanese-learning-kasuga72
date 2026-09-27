import copy
import datetime as dt
import importlib.util
import sys
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


def simulate_three_days(levels):
    history = {"word": set(), "verb": set()}
    grammar_rotation_seen_by_level = {level: [] for level in levels}
    start = dt.date(2026, 9, 1)
    for day_offset in range(3):
        material_date = start + dt.timedelta(days=day_offset)
        word_quota = {level: 1 for level in levels}
        while sum(word_quota.values()) < 8:
            for level in levels:
                word_quota[level] += 1
                if sum(word_quota.values()) >= 8:
                    break
        verb_quota = {level: 1 for level in levels[:5]}
        grammar_quota = {level: 1 for level in levels[:5]}

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
                assert len(selected_keys) == sum(quota.values())
                assert len(selected_keys) == len(set(selected_keys))
                history[item_type].update(selected_keys)
            else:
                normalized = app.ensure_daily_fresh_candidate_plan_contract(copy.deepcopy(plan), quota, item_type, "grammar_mock")
                selected = normalized["items"]
                assert len(selected) == sum(quota.values())
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


def main():
    assert_primary_reserve_contract()
    assert_verb_filter_contract()
    assert_word_filter_contract()
    assert_same_level_reserve_activation_contract()
    assert_three_day_mock_contracts()
    print("[daily-fresh-contracts] PASS primary_reserve=true")
    print("[daily-fresh-contracts] PASS verb_filter=true")
    print("[daily-fresh-contracts] PASS word_filter=true")
    print("[daily-fresh-contracts] PASS same_level_reserve_activation=true")
    print("[daily-fresh-contracts] PASS three_day_mock all_levels=true n5_only=true n1_only=true")


if __name__ == "__main__":
    main()
