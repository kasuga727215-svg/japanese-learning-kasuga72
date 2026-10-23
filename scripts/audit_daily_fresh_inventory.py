"""Read-only inventory audit. Never invokes Gemini or imports candidates."""

import argparse
import ast
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["RUN_MIGRATIONS_ON_STARTUP"] = "false"
import app


def legacy_audit():
    source = subprocess.check_output(["git", "show", "HEAD:app.py"], cwd=ROOT).decode("utf-8")
    names = {"audit_verb_candidate", "verb_pos_allows_candidate", "is_daily_fresh_polite_phrase_surface"}
    tree = ast.Module(body=[n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
    namespace = dict(vars(app))
    exec(compile(tree, "baseline-audit", "exec"), namespace)
    return namespace["audit_verb_candidate"]


def audit_rows(rows, history, before=None):
    reasons = Counter()
    categories = Counter()
    old_selected, valid, fresh, blocked_valid = [], [], [], []
    for raw in rows:
        row = app.sanitize_vocabulary_pool_candidate_row(raw)
        surface = app.vocabulary_pool_candidate_surface(row)
        key = app.vocabulary_pool_candidate_key(row)
        category = row.get("category", "")
        if category in app.DAILY_FRESH_BLOCKED_CATEGORIES:
            categories[(category, row.get("source", ""))] += 1
            if app.audit_verb_candidate(row, row=row)["accepted"]:
                blocked_valid.append(surface)
        reason = app.daily_fresh_candidate_rejection_reason(row, "verb", "N5", history)
        reasons[reason or "selected"] += 1
        if app.audit_verb_candidate(row, row=row)["accepted"]:
            valid.append(key)
        if not reason:
            fresh.append(key)
        if before and category not in app.DAILY_FRESH_BLOCKED_CATEGORIES and before(row, row=row)["accepted"]:
            old_selected.append(surface)
    return {
        "n5_rows": len(rows), "verified_dictionary_verbs": len(set(valid)),
        "eligible_fresh_verbs": len(set(fresh)), "fresh_keys": sorted(set(fresh)),
        "rejected_by_reason": dict(reasons),
        "blocked_category_sources": [{"category": k[0], "source": k[1], "count": v} for k, v in categories.items()],
        "blocked_category_verified_verbs": blocked_valid,
        "legacy_audit_non_category_surfaces": old_selected,
        "category_samples": [{k: r.get(k) for k in ("id", "surface", "category", "source", "part_of_speech")}
                             for r in rows if r.get("category") in app.DAILY_FRESH_BLOCKED_CATEGORIES][:12],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path)
    parser.add_argument("--compare-head", action="store_true")
    args = parser.parse_args()
    if app.DATABASE_URL and not args.sqlite:
        with app.get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                fields = ["id", "surface", "base_form", "reading_hiragana", "part_of_speech", "jlpt_level", "normalized_key", "category", "source", "status", "quality", "commonness_score", "is_active"]
                columns = ["id", *[app.postgres_text_as_bytea(f) if f not in {"commonness_score", "is_active"} else f for f in fields[1:]]]
                cur.execute(f"SELECT {','.join(columns)} FROM vocabulary_pool WHERE jlpt_level = %s", ("N5",))
                rows = [dict(zip(fields, row)) for row in cur.fetchall()]
        database = "configured_postgres_read_only"
    else:
        path = (args.sqlite or Path(app.SQLITE_SETTINGS_FILE)).resolve()
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute("SELECT * FROM vocabulary_pool WHERE jlpt_level = ?", ("N5",))]
        database = str(path)
    history = app.gemini_bank_material_history_keys_for_item_type("verb")
    report = audit_rows(rows, history, legacy_audit() if args.compare_head else None)
    report.update({"database": database, "production_verified": bool(app.DATABASE_URL and not args.sqlite),
                   "history_keys": len(history), "note": "Counts describe only the selected database and its materials source."})
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
