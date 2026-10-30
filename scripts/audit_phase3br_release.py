"""Read-only evidence collection and isolated release acceptance, never production writes."""

import argparse
import contextlib
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ["RUN_MIGRATIONS_ON_STARTUP"] = "false"

BASE = "https://japanese-learning-kasuga72.onrender.com"
BASELINE = "69207c261d84a0fdb21f967a68cfa2e643713951"


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def canonical(text):
    from app import normalize_vocab_key
    return normalize_vocab_key(re.split(r"[（(｜|]| - ", str(text or ""), maxsplit=1)[0].strip())


def material_projection(payload, material_key):
    from app import first_text
    rows = {"vocab": [], "verbs": []}
    for kind, entries in (("word", payload.get("vocab") or payload.get("vocabulary") or []),
                          ("verb", payload.get("verbs") or [])):
        for item in entries:
            fields = (["word", "surface", "term", "vocab_word", "base_form"] if kind == "word"
                      else ["dictionary_form", "base_form", "surface", "word", "term", "base"])
            value = first_text(item, fields) or str(item.get("normalized_key") or "")
            if not value and kind == "verb" and isinstance(item.get("forms"), dict):
                value = str(item["forms"].get("dictionary") or "")
            key = canonical(value)
            if key:
                rows["vocab" if kind == "word" else "verbs"].append(
                    {"word" if kind == "word" else "dictionary_form": key,
                     "jlpt_level": item.get("jlpt_level", "")})
    return {"date": material_key[:10], "material_key": material_key,
            "version_no": payload.get("version_no", 1), "grammar_points": [], **rows}


def capture_history(output):
    requests = []
    def get(path):
        url = BASE + path
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = response.read()
            requests.append({"url": url, "status": response.status,
                             "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                             "sha256": hashlib.sha256(raw).hexdigest()})
            return json.loads(raw)
    archive = get("/api/archive-dates?limit=365")
    versions = [v for day in archive["dates"] for v in day["versions"]]
    latest_key = archive["dates"][0]["latest_material_key"]
    latest = get("/api/materials?" + urllib.parse.urlencode({"material_key": latest_key}))
    embedded = []
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "material_history_keys" and isinstance(child, list):
                    embedded.append(set(child))
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(latest.get("metadata") or {})
    if not embedded or any(keys != embedded[0] for keys in embedded):
        raise RuntimeError("No consistent embedded pre-generation history snapshot")
    latest_keys = {canonical(row.get("word")) for row in latest.get("vocabulary") or []}
    latest_keys |= {canonical(row.get("dictionary_form") or row.get("base_form") or row.get("surface"))
                    for row in latest.get("verbs") or []}
    expected = embedded[0] | (latest_keys - {""})
    items = []
    for kind in ("word", "verb"):
        offset = 0
        while True:
            page = get("/api/history/items?" + urllib.parse.urlencode({"type": kind, "limit": 100, "offset": offset}))
            if not page.get("ok"):
                raise RuntimeError("History API failed")
            items.extend(page["items"])
            offset += len(page["items"])
            if offset >= page["total"]:
                break
            if not page["items"]:
                raise RuntimeError("Incomplete history pagination")
    actual = {canonical(row.get("word") if row["type"] == "word" else row.get("dictionary_form")) for row in items} - {""}
    archive_after = get("/api/archive-dates?limit=365")
    if archive_after != archive:
        raise RuntimeError("Material archive changed during capture")
    # History presentation normalizes some old surfaces. Verify the raw material
    # projection instead of silently replacing those lexical identities.
    raw_materials = []
    for index, version in enumerate(versions):
        key = version["material_key"]
        payload = latest if key == latest_key else get("/api/materials?" + urllib.parse.urlencode({"material_key": key}))
        if payload.get("material_key") != key:
            raise RuntimeError(f"Material response key mismatch: {key}")
        raw_materials.append(material_projection(payload, key))
        if (index + 1) % 25 == 0:
            print(f"[readonly-history] material_projection={index + 1}/{len(versions)}", flush=True)
    raw_keys = {canonical(row.get("word") or row.get("dictionary_form"))
                for material in raw_materials for kind in ("vocab", "verbs") for row in material[kind]} - {""}
    archive_final = get("/api/archive-dates?limit=365")
    if archive_final != archive:
        raise RuntimeError("Material archive changed during raw capture")
    verified = raw_keys == expected
    result = {"scope": "production GET-only history projection; NOT vocabulary_pool snapshot",
              "production_pool_verified": False, "exact_history_set_consistent": verified,
              "archive": archive, "latest_key": latest_key, "version_count": len(versions),
              "embedded_pre_generation_history_count": len(embedded[0]), "canonical_history_count": len(raw_keys),
              "presentation_history_key_count": len(actual),
              "expected_history_count": len(expected), "expected_only": sorted(expected - actual),
              "api_only": sorted(actual - expected), "history_keys": sorted(raw_keys), "items": items,
              "raw_material_projection": raw_materials, "raw_expected_only": sorted(expected - raw_keys),
              "raw_api_only": sorted(raw_keys - expected),
              "requests": requests, "production_writes": 0, "real_gemini_calls": 0}
    write(output, result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"archive", "items", "history_keys", "requests", "raw_material_projection"}}, ensure_ascii=False))
    if not verified:
        raise RuntimeError("Cannot treat history projection as a consistent canonical snapshot")


def legacy_module():
    source = subprocess.run(["git", "show", f"{BASELINE}:services/verb_candidate_audit.py"],
                            cwd=ROOT, check=True, capture_output=True).stdout.decode("utf-8-sig")
    module = types.ModuleType("phase3br_legacy_classifier")
    module.__file__ = f"git:{BASELINE}:services/verb_candidate_audit.py"
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def inventory(pool, history, old=None):
    import app
    counts = {level: Counter() for level in app.LEVELS}
    for row in pool:
        level = row["jlpt_level"]
        counts[level]["source_rows"] += 1
        key = canonical(row["surface"])
        if old:
            status = "verified_verb" if old.verified_verb_evidence(row["surface"])["accepted"] else "verified_word" if old.verified_word_evidence(row["surface"]) else "unknown"
        else:
            status = app.classify_daily_fresh_pool_row(row)["status"]
        if key in history:
            counts[level]["history_excluded"] += 1
            counts[level]["history_excluded_" + status] += 1
            continue
        counts[level][status] += 1
    return {level: {key: count[key] for key in ("source_rows", "history_excluded", "verified_verb", "verified_word", "unknown", "rejected_invalid",
                                              "history_excluded_verified_verb", "history_excluded_verified_word", "history_excluded_unknown", "history_excluded_rejected_invalid")}
            for level, count in counts.items()}


def seeded_materials(snapshot):
    if "raw_material_projection" in snapshot:
        return snapshot["raw_material_projection"]
    materials = {}
    for item in snapshot["items"]:
        kind = item["type"]
        surface = canonical(item.get("word") if kind == "word" else item.get("dictionary_form"))
        if not surface:
            continue
        for seen in item.get("seen_records") or []:
            key = seen["material_key"]
            day = seen["material_date"]
            material = materials.setdefault(key, {"date": day, "material_key": key, "version_no": seen.get("version_no", 1),
                                                 "vocab": [], "verbs": [], "grammar_points": []})
            material["vocab" if kind == "word" else "verbs"].append(
                {"word" if kind == "word" else "dictionary_form": surface, "jlpt_level": item.get("jlpt_level", "")})
    return list(materials.values())


def install_projection(harness, snapshot):
    import app
    records = seeded_materials(snapshot)
    if app.DATABASE_URL:
        with app.get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO materials (date,material_date,material_key,version_no,material_json) VALUES (%s,%s,%s,%s,%s)",
                                [(m["date"], m["date"], m["material_key"], m["version_no"], json.dumps(m, ensure_ascii=False)) for m in records])
    else:
        harness.saved = records
    keys = app.gemini_bank_material_history_keys_for_item_type("verb")
    if keys != set(snapshot["history_keys"]):
        raise AssertionError("Actual materials history reader differs from captured projection")
    return len(records)


def run_simulation(levels, days, snapshot, postgres=False):
    from test_daily_fresh_generation import Harness, app
    fixture = None
    if postgres:
        from test_selector_scan_regression import PostgreSQLRegression
        fixture = PostgreSQLRegression("test_real_prepare_enrich_finalize_save_reload")
        fixture.setUp()
        harness = fixture.h
    else:
        harness = Harness()
    try:
        initial_count = install_projection(harness, snapshot) if snapshot else 0
        pool = harness.sql("SELECT * FROM vocabulary_pool ORDER BY id")
        old = legacy_module()
        source_comparison = []
        for candidate in pool:
            if candidate["jlpt_level"] != "N5":
                continue
            legacy = old.verified_verb_evidence(candidate["surface"])
            current = app.classify_daily_fresh_pool_row(candidate)
            if legacy["accepted"] != (current["status"] == "verified_verb"):
                source_comparison.append({"surface": candidate["surface"], "reading": candidate.get("reading_hiragana", ""),
                                          "legacy": legacy, "current": current})
        records = []
        used = app.gemini_bank_material_history_keys_for_item_type("verb")
        initial_history = set(used)
        for index in range(days):
            before = inventory(pool, used)
            calls_before, saved_before = len(harness.calls), len(harness.saved)
            day = (dt.date(2026, 11, 1) + dt.timedelta(days=index)).isoformat()
            job, steps = harness.job(levels, day)
            result = harness.complete(job, steps)
            row = {"index": index + 1, "day": day, "before": before, "ok": result["ok"], "error": result.get("error"),
                   "missing": result.get("missing", []), "mock_calls": len(harness.calls) - calls_before}
            if result["ok"]:
                material = harness.saved[-1]
                assert [len(material[k]) for k in ("vocab", "verbs", "grammar_points")] == [8, 5, 5]
                assert material["metadata"]["fallback_count"] == {"word": 0, "verb": 0, "grammar": 0}
                projection = material_projection(material, result["material_key"])
                consumed = {canonical(item.get("word") or item.get("dictionary_form"))
                            for kind in ("vocab", "verbs") for item in projection[kind]} - {""}
                assert not consumed & used
                used |= consumed
                saved_count = len(harness.saved)
                assert saved_count == saved_before + 1
                assert app.gemini_bank_material_history_keys_for_item_type("verb") == used
                quota = app.build_level_quota(levels, 5, item_type="verb")
                assert dict(Counter(v["jlpt_level"] for v in material["verbs"])) == quota
                row["verb_consumption"] = dict(Counter(v["jlpt_level"] for v in material["verbs"]))
            else:
                assert len(harness.calls) == calls_before, "No enrichment before sufficient primary candidates"
                assert len(harness.saved) == saved_before
                assert app.gemini_bank_material_history_keys_for_item_type("verb") == used
                row["legacy_same_history_inventory"] = inventory(pool, used, old)
            row["after"] = inventory(pool, used)
            records.append(row)
        remaining = []
        if levels == ["N5"]:
            for candidate in pool:
                if candidate["jlpt_level"] == "N5" and canonical(candidate["surface"]) not in used:
                    remaining.append({"surface": candidate["surface"], "reading": candidate.get("reading_hiragana", ""),
                                      "classification": app.classify_daily_fresh_pool_row(candidate),
                                      "legacy": old.verified_verb_evidence(candidate["surface"])})
        return {"backend": "PostgreSQL17-isolated" if postgres else "SQLite", "levels": levels, "days": days,
                "history_source": "production GET lexical projection" if snapshot else "empty isolation fixture",
                "production_pool_verified": False, "initial_history_keys": len(initial_history), "initial_material_rows": initial_count,
                "n5_same_source_classification_changes": source_comparison,
                "successful_days": sum(row["ok"] for row in records), "daily": records, "remaining_n5": remaining,
                "real_gemini_calls": 0}
    finally:
        if fixture:
            fixture.doCleanups()
        else:
            harness.close()


def diagnose(output, history_file):
    from test_daily_fresh_generation import LEVELS
    snapshot = json.loads(history_file.read_text(encoding="utf-8")) if history_file else None
    if snapshot and not snapshot["exact_history_set_consistent"]:
        raise RuntimeError("Unverified history input")
    scenarios = []
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        scenarios.append(run_simulation(["N5"], 30, None))
        if snapshot:
            for postgres in (False, True):
                for days in (7, 30):
                    scenarios.append(run_simulation(LEVELS, days, snapshot, postgres))
    result = {"scenarios": scenarios, "formal_2026_10_30_pool_replay": "NOT VERIFIED: pool snapshot unavailable",
              "last_logs": log.getvalue().splitlines()[-80:], "production_writes": 0, "real_gemini_calls": 0}
    write(output, result)
    with output.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["backend", "scenario_days", "history_source", "day", "level", "initial_effective_verb", "consumed_verb",
                         "remaining_effective_verb", "unknown", "history_excluded", "history_excluded_verified_verb", "quota_satisfied", "error"])
        for scenario in scenarios:
            for day in scenario["daily"]:
                for level, before in day["before"].items():
                    writer.writerow([scenario["backend"], scenario["days"], scenario["history_source"], day["day"], level,
                                     before["verified_verb"], day.get("verb_consumption", {}).get(level, 0), day["after"][level]["verified_verb"],
                                     before["unknown"], before["history_excluded"], before["history_excluded_verified_verb"], day["ok"], day["error"]])
    print(json.dumps([{k: s[k] for k in ("backend", "levels", "days", "history_source", "initial_history_keys", "successful_days")}
                      for s in scenarios], ensure_ascii=False))


def evidence_checks(output):
    from test_daily_fresh_generation import Harness, LEVELS, app
    from services.verb_candidate_audit import classify_lexeme, CLASSIFIER_VERSION
    results = []
    logs = io.StringIO()
    for postgres in (False, True):
        fixture = None
        with contextlib.redirect_stdout(logs):
            if postgres:
                from test_selector_scan_regression import PostgreSQLRegression
                fixture = PostgreSQLRegression("test_real_prepare_enrich_finalize_save_reload")
                fixture.setUp()
                harness = fixture.h
            else:
                harness = Harness()
            try:
                job, _ = harness.job(LEVELS, "2026-11-01")
                for _ in range(20):
                    loaded = app.load_gemini_generation_job(job["job_id"])
                    app.prepare_all_gemini_daily_fresh_candidates(job["job_id"], loaded,
                        app.gemini_job_json(loaded["settings_json"], {}), "2026-11-01", time.perf_counter())
                    loaded = app.load_gemini_generation_job(job["job_id"])
                    if app.gemini_daily_fresh_all_candidates_ready(loaded):
                        break
                else:
                    raise AssertionError("Candidate preparation did not finish")
                candidates = []
                for step in app.gemini_daily_fresh_steps(loaded):
                    if step["item_type"] in {"word", "verb"}:
                        state = app.gemini_daily_fresh_pack_state(loaded, step)[2]
                        candidates.extend(state["candidate_plan"]["primary_items"])
                assert len(candidates) == 13
                assert all(c["classification_evidence"]["classifier_version"] == CLASSIFIER_VERSION for c in candidates)
                fingerprint = candidates[0]["classification_evidence"]["source_fingerprint"]
                assert fingerprint
                classify_lexeme.cache_clear()
                loaded_again = app.load_gemini_generation_job(job["job_id"])
                assert loaded_again["vocab_json"] == loaded["vocab_json"]
                second, _ = harness.job(LEVELS, "2026-11-02")
                second = app.load_gemini_generation_job(second["job_id"])
                second_state = app.gemini_daily_fresh_pack_state(second, app.gemini_daily_fresh_steps(second)[0])[2]
                assert not second_state.get("candidate_plan"), "New jobs must not inherit prior classification plans"
                surface = candidates[0].get("w") or candidates[0].get("d")
                classify_lexeme(surface)
                hits = classify_lexeme.cache_info().hits
                classify_lexeme(surface)
                assert classify_lexeme.cache_info().hits == hits + 1
                assert not app.gemini_bank_material_history_keys_for_item_type("verb")
                assert not harness.calls
                results.append({"backend": "PostgreSQL17-isolated" if postgres else "SQLite",
                                "candidate_evidence_persisted_in_job": 13, "same_job_survives_process_cache_clear": True,
                                "new_job_does_not_inherit_old_plan": True, "same_process_lru_hit": True,
                                "history_keys_after_preparation": 0, "mock_gemini_calls": 0,
                                "classifier_version": CLASSIFIER_VERSION})
            finally:
                fixture.doCleanups() if fixture else harness.close()
    write(output, {"checks": results, "production_writes": 0, "real_gemini_calls": 0})
    print(json.dumps(results, ensure_ascii=False))


def summarize(output):
    report_dir = output.parent
    history = json.loads((report_dir / "release_history_snapshot.json").read_text(encoding="utf-8"))
    diagnostics = json.loads((report_dir / "release_diagnostics.json").read_text(encoding="utf-8"))
    before = json.loads((report_dir / "before.json").read_text(encoding="utf-8"))
    after = json.loads((report_dir / "after.json").read_text(encoding="utf-8"))
    source_files = []
    for path in sorted((ROOT / "data/external_lexicon").glob("openjlpt-vocab-n*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
        source_files.append({"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                             "raw_rows": len(rows), "columns": reader.fieldnames,
                             "missing_reading": sum(not r.get("reading", "").strip() for r in rows)})
    old = {r["canonical_key"]: r for r in before["records"] if r["level"] == "N5"}
    new = {r["canonical_key"]: r for r in after["records"] if r["level"] == "N5"}
    used = set(history["history_keys"])
    differences = [{"surface": r["surface"], "line": r["line"], "meaning": r["meaning"],
                    "old_verb": old[key]["verb"]["accepted"], "new_status": r["classification"]["status"],
                    "new_reason": r["classification"]["reason"], "in_captured_history": key in used}
                   for key, r in new.items() if old[key]["verb"]["accepted"] != (r["classification"]["status"] == "verified_verb")]
    tests = {name: json.loads((report_dir / f"final_acceptance_{name}_tests.json").read_text(encoding="utf-8"))
             for name in ("generation", "selector", "lexical")}
    result = {"release_recommendation": "NOT READY", "baseline": "69207c261d84a0fdb21f967a68cfa2e643713951",
              "production_pool_replay": "NOT VERIFIED", "production_writes": 0, "real_gemini_calls": 0,
              "fixture_files": source_files, "fixture_unique_inventory": diagnostics["scenarios"][0]["daily"][0]["before"],
              "history_keys": len(used), "history_material_rows": history["version_count"],
              "n5_same_source_changes": differences,
              "n5_same_source_totals": {"old_verified": sum(r["verb"]["accepted"] for r in old.values()),
                                         "new_verified": sum(r["classification"]["status"] == "verified_verb" for r in new.values()),
                                         "old_unused_with_captured_history": sum(r["verb"]["accepted"] and key not in used for key, r in old.items()),
                                         "new_unused_with_captured_history": sum(r["classification"]["status"] == "verified_verb" and key not in used for key, r in new.items())},
              "test_results": {name: {key: test[key] for key in ("tests_run", "failures", "errors", "successful")}
                               for name, test in tests.items()},
              "skips": tests["selector"]["skipped"],
              "scenarios": [{key: s[key] for key in ("backend", "levels", "days", "history_source", "initial_history_keys", "successful_days")}
                            for s in diagnostics["scenarios"]],
              "production_file_sha256": {str(p): hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                                         for p in ("app.py", "services/verb_candidate_audit.py", "services/candidate_selector.py")}}
    write(output, result)
    print(json.dumps({"n5_comparison": result["n5_same_source_totals"], "tests": result["test_results"],
                      "fixture_raw_rows": {r["path"]: r["raw_rows"] for r in source_files}}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-history", type=Path)
    parser.add_argument("--diagnose", type=Path)
    parser.add_argument("--history", type=Path)
    parser.add_argument("--evidence-checks", type=Path)
    parser.add_argument("--summarize", type=Path)
    args = parser.parse_args()
    if args.capture_history:
        capture_history(args.capture_history)
    elif args.diagnose:
        diagnose(args.diagnose, args.history)
    elif args.evidence_checks:
        evidence_checks(args.evidence_checks)
    elif args.summarize:
        summarize(args.summarize)
    else:
        parser.error("Choose --capture-history or --diagnose")
