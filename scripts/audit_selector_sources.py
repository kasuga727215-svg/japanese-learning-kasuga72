"""Read-only local/source audit. This does not claim access to Render data."""

import argparse
import contextlib
import csv
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["RUN_MIGRATIONS_ON_STARTUP"] = "false"
import app


def summarize(rows, history):
    result = {}
    for level in ("N5", "N4", "N3", "N2", "N1"):
        level_rows = [r for r in rows if str(r.get("jlpt_level") or "").upper().strip() == level]
        stats = {"rows": len(level_rows), "category_source_counts": [], "blocked_samples": []}
        categories = Counter((r.get("category", ""), r.get("source", "")) for r in level_rows)
        stats["category_source_counts"] = [{"category": c, "source": s, "count": n} for (c, s), n in categories.items()]
        stats["blocked_samples"] = [{k: r.get(k) for k in ("id", "surface", "source", "category", "part_of_speech")}
                                    for r in level_rows if r.get("category") in app.DAILY_FRESH_BLOCKED_CATEGORIES][:8]
        for kind in ("word", "verb"):
            reasons, keys = Counter(), set()
            for row in level_rows:
                reason = app.daily_fresh_candidate_rejection_reason(row, kind, level, history)
                reasons[reason or "eligible"] += 1
                if not reason:
                    keys.add(app.vocabulary_pool_candidate_key(row))
            stats[kind] = {"eligible_unique_remaining": len(keys), "rejected_by_reason": dict(reasons)}
        result[level] = stats
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, default=ROOT / "state.sqlite3")
    parser.add_argument("--materials-csv", type=Path, default=ROOT / "database.csv")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    with patch.object(app, "DATABASE_URL", ""), patch.object(app, "DATABASE_FILE", str(args.materials_csv)), patch.object(app, "_MATERIALS_SCHEMA_READY", True):
        history = app.gemini_bank_material_history_keys_for_item_type("word")
        frame = app.read_database()
        old = set()
        for raw in frame.get("material_json", []):
            if str(raw).strip():
                usage = app.gemini_bank_extract_material_usage_keys(json.loads(raw))
                old.update(usage["word"] | usage["verb"])
    with contextlib.closing(sqlite3.connect(args.sqlite.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
        local = [app.sanitize_vocabulary_pool_candidate_row(dict(row)) for row in conn.execute("SELECT * FROM vocabulary_pool")]
    source = []
    for level in ("N5", "N4", "N3", "N2", "N1"):
        with (ROOT / "data/external_lexicon" / f"openjlpt-vocab-{level.lower()}.csv").open(encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                surface = raw["word"].strip()
                source.append({"surface": surface, "base_form": surface, "normalized_key": app.normalize_vocab_key(surface),
                               "jlpt_level": level, "category": "general", "source": "openjlpt", "status": "active"})
    report = {
        "production_postgresql_verified": False,
        "local_sqlite": str(args.sqlite.resolve()), "local_materials_csv": str(args.materials_csv.resolve()),
        "local_inventory_total": len(local), "local_saved_material_history_keys": len(history),
        "legacy_alias_key_count": len(old), "removed_display_alias_samples": sorted(old - history)[:20],
        "local_inventory": summarize(local, history),
        "checked_in_openjlpt_not_imported": summarize(source, history),
        "limitations": ["Render inventory 18636 and history count 3237 were not accessible",
                        "File-level JLPT labels are source labels, not independently certified JLPT classifications",
                        "POS evidence uses existing UniDic dictionary; source-file counts do not prove production availability"],
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"local_inventory_total": len(local), "history_keys": len(history),
                      "source_remaining": {level: {kind: stats[kind]["eligible_unique_remaining"] for kind in ("word", "verb")}
                                           for level, stats in report["checked_in_openjlpt_not_imported"].items()}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
