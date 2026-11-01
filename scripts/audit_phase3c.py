"""Replay an explicitly supplied formal N5 export in isolated databases only."""

import argparse
import contextlib
import csv
import datetime as dt
import hashlib
import io
import json
import os
import subprocess
import sys
import types
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ["RUN_MIGRATIONS_ON_STARTUP"] = "false"
SNAPSHOT_SHA = "334ea156d466a359b8297b100e8e29c28208ce8e6d7cdc2c65c52c6c33420b7a"
BASELINE = "69207c261d84a0fdb21f967a68cfa2e643713951"
PARENT = "3c6b2a6e08364b3bc11c90aced1f81ff47139a41"


def load_snapshot(path):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SNAPSHOT_SHA:
        raise ValueError("Formal snapshot SHA256 mismatch; no substitute corpus allowed")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    assert len(rows) == 1950 and Counter(r["category"] for r in rows) == {"general": 669, "business": 651, "advanced": 630}
    return rows


def baseline_app(revision=BASELINE):
    source = subprocess.check_output(["git", "show", f"{revision}:app.py"], cwd=ROOT).decode("utf-8-sig")
    module = types.ModuleType(f"phase3c_baseline_{revision[:7]}")
    module.__file__ = str(ROOT / "app.py")
    sys.modules[module.__name__] = module
    exec(compile(source, f"git:{revision}:app.py", "exec"), module.__dict__)
    if revision == BASELINE:
        from audit_phase3br_release import legacy_module
        classifier = legacy_module()
        module.verified_verb_evidence = classifier.verified_verb_evidence
        module.verified_word_evidence = classifier.verified_word_evidence
    return module


def history_materials(snapshot):
    from audit_phase3br_release import canonical
    materials = json.loads(json.dumps(snapshot["raw_material_projection"]))
    by_key = {m["material_key"]: m for m in materials}
    for item in snapshot["items"]:
        surface = canonical(item.get("word") if item["type"] == "word" else item.get("dictionary_form"))
        reading = item.get("reading_hiragana") or item.get("reading") or ""
        for seen in item.get("seen_records") or []:
            material = by_key.get(seen["material_key"])
            if not material:
                continue
            for kind in ("vocab", "verbs"):
                for row in material[kind]:
                    if canonical(row.get("word") or row.get("dictionary_form")) == surface:
                        row["reading_hiragana"] = reading
    return materials


def install(harness, rows, snapshot):
    from test_daily_fresh_generation import app
    # Keep the other levels' public fixture separate and do not claim it is formal.
    harness.sql("DELETE FROM vocabulary_pool WHERE jlpt_level='N5'")
    harness.sql("UPDATE vocabulary_pool SET id=id+100000")
    columns = list(rows[0])
    values = []
    for row in rows:
        values.append(tuple(int(row[c]) if c in {"id", "used_in_material_count"} else row[c].lower() == "t" if c == "is_active"
                            else float(row[c]) if c == "commonness_score" and row[c] else None if c == "commonness_score" else row[c]
                            for c in columns))
    if app.DATABASE_URL:
        with app.get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(f"INSERT INTO vocabulary_pool ({','.join(columns)}) VALUES ({','.join(['%s']*len(columns))})", values)
                for material in history_materials(snapshot):
                    cur.execute("INSERT INTO materials (date,material_date,material_key,version_no,material_json) VALUES (%s,%s,%s,%s,%s)",
                                (material["date"], material["date"], material["material_key"], material["version_no"], json.dumps(material, ensure_ascii=False)))
    else:
        import sqlite3
        with sqlite3.connect(harness.path) as conn:
            conn.executemany(f"INSERT INTO vocabulary_pool ({','.join(columns)}) VALUES ({','.join(['?']*len(columns))})", values)
        harness.saved = history_materials(snapshot)
    assert app.gemini_bank_material_history_keys_for_item_type("verb") == set(snapshot["history_keys"])


def audit(rows, snapshot, private_dir):
    from test_daily_fresh_generation import Harness, app
    from services.lexical_evidence import ExclusionKeys
    from services.candidate_selector import id_query
    old = baseline_app()
    parent = baseline_app(PARENT)
    harness = Harness()
    try:
        install(harness, rows, snapshot)
        history = set(snapshot["history_keys"])
        exclusions = ExclusionKeys(history, app.daily_fresh_history_readings(history))
        eligible = set()
        for tier in (0, 1):
            params = ("N5", app.VOCABULARY_POOL_MIN_COMMONNESS_SCORE, *sorted(app.DAILY_FRESH_BLOCKED_CATEGORIES),
                      *sorted(app.DAILY_FRESH_BLOCKED_SOURCES), 0, 3000)
            eligible |= {r["id"] for r in harness.sql(id_query(False, sorted(app.DAILY_FRESH_BLOCKED_CATEGORIES), sorted(app.DAILY_FRESH_BLOCKED_SOURCES), tier), params)}
        from services.verb_candidate_audit import classify_lexeme
        from services.lexical_evidence import history_overlap
        old_counts, parent_counts, new_counts = Counter(), Counter(), Counter()
        before_status, after_status, old244_status = Counter(), Counter(), Counter()
        details, available = [], []
        for row in rows:
            old_reason = "sql_category" if int(row["id"]) not in eligible else old.daily_fresh_candidate_rejection_reason(row, "verb", "N5", history)
            new_reason = "sql_category" if int(row["id"]) not in eligible else app.daily_fresh_candidate_rejection_reason(row, "verb", "N5", exclusions)
            parent_reason = "sql_category" if int(row["id"]) not in eligible else parent.daily_fresh_candidate_rejection_reason(row, "verb", "N5", history)
            old_counts[old_reason or "selected"] += 1
            parent_counts[parent_reason or "selected"] += 1
            new_counts[new_reason or "selected"] += 1
            evidence = app.classify_daily_fresh_pool_row(row)
            if int(row["id"]) in eligible:
                before_status[classify_lexeme(row["surface"], row["reading_hiragana"], row["part_of_speech"])["status"]] += 1
                after_status[evidence["status"]] += 1
            if old_reason == "non_verb:not_verified_dictionary_verb":
                old244_status[evidence["status"]] += 1
            matches = [m["material_key"] for m in snapshot["raw_material_projection"]
                       if any((r.get("word") or r.get("dictionary_form")) in {row["surface"], row["normalized_key"]}
                              for k in ("vocab", "verbs") for r in m[k])]
            overlap = history_overlap(row, exclusions)
            if overlap:
                matches += [m["material_key"] for m in snapshot["raw_material_projection"]
                            if any((r.get("word") or r.get("dictionary_form")) == overlap["matched_alias"]
                                   for k in ("vocab", "verbs") for r in m[k])]
            details.append({"pool_id": row["id"], "surface": row["surface"], "old_reason": old_reason,
                            "parent_reason": parent_reason, "new_reason": new_reason,
                            "material_matches": sorted(set(matches)), "history_identity": overlap,
                            "evidence": evidence})
            if not new_reason:
                available.append({"surface": row["surface"], "reading": row["reading_hiragana"], "source": row["source"],
                                  "verified_readings": sorted({m["reading"] for m in evidence.get("source_sense_evidence", {}).get("matches", [])}),
                                  "evidence_reason": evidence["reason"]})
        private_dir.mkdir(parents=True, exist_ok=True)
        (private_dir / "formal_n5_row_audit.json").write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"snapshot_sha256": SNAPSHOT_SHA, "snapshot_rows": len(rows), "sql_eligible": len(eligible),
                "old_exclusions": dict(old_counts), "new_exclusions": dict(new_counts), "available": available,
                "repair_parent_exclusions": dict(parent_counts),
                "eligible_classification_before_source_sense": dict(before_status),
                "eligible_classification_after_source_sense": dict(after_status),
                "old244_current_classification": dict(old244_status),
                "available_count": len(available), "historical_log_targets": {"sql_category": 1281, "history": 374, "not_verified_dictionary_verb": 244},
                "transaction_consistency": "Pool exported 2026-11-01; history captured 2026-10-30, archive unchanged 2026-11-01; NOT one transaction snapshot",
                "raw_row_audit_committed": False}
    finally:
        harness.close()


def simulate(rows, snapshot, days, postgres):
    from test_daily_fresh_generation import Harness, LEVELS, app
    fixture = None
    if postgres:
        from test_selector_scan_regression import PostgreSQLRegression
        fixture = PostgreSQLRegression("test_real_prepare_enrich_finalize_save_reload")
        fixture.setUp()
        harness = fixture.h
    else:
        harness = Harness()
    try:
        install(harness, rows, snapshot)
        records, used = [], set(snapshot["history_keys"])

        def inventory():
            from services.lexical_evidence import ExclusionKeys
            keys = app.gemini_bank_material_history_keys_for_item_type("verb")
            exclusions = ExclusionKeys(keys, app.daily_fresh_history_readings(keys))
            reasons = Counter(app.daily_fresh_candidate_rejection_reason(r, "verb", "N5", exclusions)
                              for r in rows if r["category"] == "general")
            return {"verified_fresh_N5_verbs": reasons[""],
                    "unknown_pos_N5": sum(n for r, n in reasons.items() if r.startswith("classifier_unknown:") and "identity" not in r),
                    "history_identity_pending_N5": sum(n for r, n in reasons.items() if "identity_uncertain" in r),
                    "exact_history_excluded_N5": reasons["excluded_material_history_or_current_batch"],
                    "saved_history_key_count": len(keys)}

        for i in range(days):
            saved, calls = len(harness.saved), len(harness.calls)
            before = inventory()
            day = (dt.date(2026, 11, 1) + dt.timedelta(days=i)).isoformat()
            job, steps = harness.job(LEVELS, day)
            result = harness.complete(job, steps)
            item = {"day": day, "ok": result["ok"], "error": result.get("error"), "missing": result.get("missing", []),
                    "mock_calls": len(harness.calls) - calls, "inventory_before": before}
            if result["ok"]:
                material = harness.saved[-1]
                assert [len(material[k]) for k in ("vocab", "verbs", "grammar_points")] == [8, 5, 5]
                assert material["metadata"]["fallback_count"] == {"word": 0, "verb": 0, "grammar": 0}
                assert Counter(v["jlpt_level"] for v in material["verbs"]) == dict.fromkeys(LEVELS, 1)
                assert Counter(v["jlpt_level"] for v in material["grammar_points"]) == dict.fromkeys(LEVELS, 1)
                fresh = app.gemini_bank_material_history_keys_for_item_type("verb")
                assert len(fresh) > len(used) and used <= fresh
                used = fresh
                reload = app.material_by_key(result["material_key"])
                assert [len(reload[k]) for k in ("vocabulary", "verbs", "grammar_points")] == [8, 5, 5]
                item["counts"] = {"word": 8, "verb": 5, "grammar": 5, "fallback": 0}
            else:
                assert len(harness.calls) == calls, "Insufficient primary must stop before Gemini"
                assert len(harness.saved) == saved
                assert app.gemini_bank_material_history_keys_for_item_type("verb") == used
            item["inventory_after"] = inventory()
            records.append(item)
        return {"backend": "PostgreSQL17" if postgres else "SQLite", "days": days,
                "successful_days": sum(r["ok"] for r in records), "daily": records,
                "N5_source": "formal SHA-verified export", "other_levels_source": "public CSV fixtures, NOT formal snapshots",
                "real_gemini_calls": 0, "capacity_acceptance": "PASS" if all(r["ok"] for r in records) else "FAIL"}
    finally:
        fixture.doCleanups() if fixture else harness.close()


def main(args):
    rows = load_snapshot(args.snapshot)
    snapshot = json.loads(args.history.read_text(encoding="utf-8"))
    assert snapshot["exact_history_set_consistent"]
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        result = audit(rows, snapshot, args.private_dir)
        if args.simulate:
            result["supply"] = [simulate(rows, snapshot, days, pg) for pg in (False, True) for days in (7, 30)]
    result.update(history_keys=len(snapshot["history_keys"]), material_versions=snapshot["version_count"],
                  formal_writes=0, real_gemini_calls=0, deployment="NOT AUTHORIZED", release="NOT READY")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result if not args.simulate else {k:v for k,v in result.items() if k!='supply'}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--simulate", action="store_true")
    main(parser.parse_args())
