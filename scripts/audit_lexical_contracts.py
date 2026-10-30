"""Offline, reproducible lexical evidence audit. Never connects to production."""

import argparse
import ast
import csv
import difflib
import hashlib
import json
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services import verb_candidate_audit as classifier

CASES = ["あびる", "かける", "だます", "いただく", "コピーする", "あいさつする",
         "引っ掻く", "いす", "向こう", "遠く", "こんにちは", "或る", "ずるずる", "どうぞ宜しく"]


def protected_hashes():
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8-sig"))
    names = {"call_gemini", "call_gemini_json_enrich", "save_material_for_date", "api_materials",
             "build_gemini_daily_enrich_prompt", "prepare_all_gemini_daily_fresh_candidates",
             "finalize_gemini_generation_job", "gemini_bank_material_history_keys_for_item_type"}
    return {node.name: hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
            for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names}


def audit(output):
    records, seen = [], set()
    totals = defaultdict(Counter)
    for level in ("N5", "N4", "N3", "N2", "N1"):
        path = ROOT / "data" / "external_lexicon" / f"openjlpt-vocab-{level.lower()}.csv"
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for line, row in enumerate(csv.DictReader(handle), 2):
                word = row["word"].strip()
                key = unicodedata.normalize("NFKC", word).lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                verb = classifier.verified_verb_evidence(word)
                word_ok = classifier.verified_word_evidence(word)
                evidence = (classifier.classify_lexeme(word, row.get("reading", ""))
                            if hasattr(classifier, "classify_lexeme") else None)
                status = evidence["status"] if evidence else "verified_verb" if verb["accepted"] else "verified_word" if word_ok else "unknown"
                totals[level][status] += 1
                records.append({"source": str(path.relative_to(ROOT)), "line": line, "level": level,
                                "surface": word, "reading": row.get("reading", ""), "meaning": row.get("meanings", ""),
                                "canonical_key": key, "verb": verb, "word_accepted": word_ok, "classification": evidence})
    cases = [{"surface": word, "best_path": classifier._nodes(word),
              "verb": classifier.verified_verb_evidence(word), "word_accepted": classifier.verified_word_evidence(word),
              "classification": classifier.classify_lexeme(word) if hasattr(classifier, "classify_lexeme") else None}
             for word in CASES]
    result = {"scope": "checked-in OpenJLPT CSV only; empty isolated history; NOT production inventory",
              "production_postgresql": "NOT VERIFIED", "production_244_rows": "NOT AVAILABLE",
              "production_374_history_matches": "NOT AVAILABLE", "by_level": totals,
              "protected_function_hashes": protected_hashes(), "cases": cases, "records": records}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"scope": result["scope"], "by_level": totals, "output": str(output)}, ensure_ascii=False))


def summarize(directory):
    read = lambda name: json.loads((directory / name).read_text(encoding="utf-8"))
    before, after = read("before.json"), read("after.json")
    gold = {word: "verified_verb" for word in CASES[:7]}
    gold.update({word: "verified_word" for word in CASES[7:13]})
    gold["どうぞ宜しく"] = "unknown"
    matrices = {}
    for name, data in (("before", before), ("after", after)):
        matrix = Counter()
        for row in data["cases"]:
            predicted = (row["classification"]["status"] if row.get("classification") else
                         "verified_verb" if row["verb"]["accepted"] else "verified_word" if row["word_accepted"] else "unknown")
            matrix[f"{gold[row['surface']]} -> {predicted}"] += 1
        matrices[name] = dict(matrix)
    files = ["app.py", "services/verb_candidate_audit.py", "services/candidate_selector.py",
             "scripts/test_daily_fresh_generation.py", "scripts/test_selector_scan_regression.py",
             "scripts/test_lexical_contracts.py", "scripts/audit_lexical_contracts.py"]
    patch, changed_functions = [], {}
    for file in files:
        current = (ROOT / file).read_text(encoding="utf-8-sig")
        baseline = ROOT / "tmp-starter-db/phase3br-baseline" / file
        original = baseline.read_text(encoding="utf-8-sig") if baseline.exists() else ""
        patch.extend(difflib.unified_diff(original.splitlines(True), current.splitlines(True),
                                         fromfile=f"a/{file}" if baseline.exists() else "/dev/null", tofile=f"b/{file}"))
        if baseline.exists():
            def functions(text):
                return {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(text).body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            old, new = functions(original), functions(current)
            changed_functions[file] = [name for name in new if old.get(name) != new[name]]
    (directory / "repair.patch").write_text("".join(patch), encoding="utf-8")
    tests = {name: read(name) for name in ("generation_tests.json", "selector_tests.json", "lexical_tests.json", "lexical_final_checks.json")}
    with (directory / "daily_inventory.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["backend", "scenario_days", "day", "level", "status", "before", "after", "generation_ok", "error"])
        for scenario in tests["lexical_tests.json"]["capacity"]:
            for day in scenario["daily"]:
                for level in day["before"]:
                    for status in day["before"][level]:
                        writer.writerow([scenario["backend"], scenario["days"], day["day"], level, status,
                                         day["before"][level][status], day["after"][level][status], day["ok"], day["error"]])
    summary = {"baseline": "69207c261d84a0fdb21f967a68cfa2e643713951",
               "production_inventory": "NOT VERIFIED", "production_244_analysis": "NOT VERIFIED",
               "production_history_374_audit": "NOT VERIFIED", "production_capacity_7_30": "NOT VERIFIED",
               "local_source_before": before["by_level"], "local_source_after": after["by_level"],
               "curated_sense_matrix_only_not_full_pool_gold": matrices, "changed_functions": changed_functions,
               "protected_functions_unchanged": before["protected_function_hashes"] == protected_hashes(),
               "tests": {name: {key: data.get(key) for key in ("tests_run", "failures", "errors", "skipped", "successful")}
                         for name, data in tests.items()},
               "new_contract_unique_cases": len(set(tests["lexical_tests.json"]["evidence"]) | set(tests["lexical_final_checks.json"]["evidence"])),
               "isolated_capacity": [{key: s[key] for key in ("backend", "days", "successful_days", "capacity_acceptance", "source")}
                                     for s in tests["lexical_tests.json"]["capacity"]],
               "real_gemini_calls": 0, "formal_database_writes": 0, "committed": False, "deployed": False}
    identities = defaultdict(set)
    for row in after["records"]:
        evidence = row.get("classification") or {}
        if evidence.get("status") != "verified_verb" or evidence.get("reason") == "verified_suru_stem":
            continue
        for entry in evidence.get("evidence") or []:
            identities[(row["level"], entry["lemma"], entry["lemma_reading"])].add(row["surface"])
    summary["possible_variant_collisions_not_auto_merged"] = [
        {"level": level, "lemma": lemma, "reading": reading, "surfaces": sorted(surfaces)}
        for (level, lemma, reading), surfaces in identities.items() if len(surfaces) > 1]
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path)
    group.add_argument("--summarize", type=Path)
    args = parser.parse_args()
    summarize(args.summarize) if args.summarize else audit(args.output)
