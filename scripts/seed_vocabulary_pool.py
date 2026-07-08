import argparse
import json
import os
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DEFAULT_SQLITE_DB = ROOT / "state.sqlite3"
SEED_FILES = (
    ("basic", DATA_DIR / "vocabulary_seed_n5_n3.json"),
    ("advanced", DATA_DIR / "vocabulary_seed_advanced.json"),
)

TEXT_COLUMNS = {
    "surface",
    "base_form",
    "normalized_key",
    "reading_hiragana",
    "meaning_zh",
    "part_of_speech",
    "jlpt_level",
    "conjugation_type",
    "quality",
    "category",
    "example_sentence",
    "example_translation_zh",
    "source",
    "last_used_at",
    "created_at",
    "updated_at",
    "status",
}
INTEGER_COLUMNS = {
    "verb_group",
    "cooldown_days",
    "priority",
    "is_active",
    "enabled",
    "used_in_material_count",
}
PREFERRED_COLUMNS = (
    "surface",
    "base_form",
    "normalized_key",
    "reading_hiragana",
    "meaning_zh",
    "part_of_speech",
    "jlpt_level",
    "verb_group",
    "conjugation_type",
    "quality",
    "category",
    "cooldown_days",
    "example_sentence",
    "example_translation_zh",
    "source",
    "priority",
    "is_active",
    "enabled",
    "status",
    "used_in_material_count",
    "last_used_at",
    "created_at",
    "updated_at",
)


def configure_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value):
    if value is None:
        return ""
    text = str(value).strip()
    return text


def timestamp_or_none(value):
    text = clean_text(value)
    return text or None


def normalize_vocab_key(value):
    text = unicodedata.normalize("NFKC", clean_text(value))
    return text.lower()


def bool_value(value, default=True):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def int_or_none(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def first_text(raw, keys):
    for key in keys:
        value = raw.get(key)
        text = clean_text(value)
        if text:
            return text
    return ""


def load_seed_rows():
    loaded = []
    seed_counts = {}
    for seed_kind, path in SEED_FILES:
        if not path.exists():
            seed_counts[str(path)] = {"exists": False, "count": 0}
            continue
        with path.open("r", encoding="utf-8") as handle:
            rows = json.load(handle)
        if not isinstance(rows, list):
            rows = []
        seed_counts[str(path)] = {"exists": True, "count": len(rows)}
        for row in rows:
            if isinstance(row, dict):
                copied = dict(row)
                copied["_seed_kind"] = seed_kind
                copied["_seed_file"] = path.name
                loaded.append(copied)
    return loaded, seed_counts


def normalize_seed_item(raw):
    seed_kind = raw.get("_seed_kind", "")
    surface = first_text(raw, ("surface", "term", "word", "vocab_word", "base_form"))
    base_form = first_text(raw, ("base_form", "dictionary_form", "surface", "term", "word", "vocab_word")) or surface
    normalized_key = normalize_vocab_key(
        first_text(raw, ("normalized_key", "normalized_term", "base_form", "surface", "term", "word"))
        or base_form
        or surface
    )
    jlpt_level = first_text(raw, ("jlpt_level", "target_level", "level")).upper()
    if not surface or not base_form or not normalized_key:
        return None

    category = first_text(raw, ("category",))
    source = first_text(raw, ("source",))
    if seed_kind == "basic":
        category = category or "general"
        source = "seed_basic"
    else:
        category = category or "advanced"
        source = source or "seed_advanced"

    quality = first_text(raw, ("quality",))
    if not quality:
        quality = "core" if source == "seed_basic" else "supplemental"

    now = utc_now_iso()
    return {
        "surface": surface,
        "base_form": base_form,
        "normalized_key": normalized_key,
        "reading_hiragana": first_text(raw, ("reading_hiragana", "reading", "kana")),
        "meaning_zh": first_text(raw, ("meaning_zh", "meaning_zh_tw", "meaning", "vocab_meaning")),
        "part_of_speech": first_text(raw, ("part_of_speech", "pos")),
        "jlpt_level": jlpt_level,
        "verb_group": int_or_none(raw.get("verb_group")),
        "conjugation_type": first_text(raw, ("conjugation_type", "inflection_type")),
        "quality": quality,
        "category": category,
        "cooldown_days": int_or_none(raw.get("cooldown_days")) or 14,
        "example_sentence": first_text(raw, ("example_sentence", "example_japanese")),
        "example_translation_zh": first_text(raw, ("example_translation_zh", "example_chinese", "example_translation")),
        "source": source,
        "priority": int_or_none(raw.get("priority")) or (5 if source == "seed_basic" else 1),
        "is_active": bool_value(raw.get("is_active", raw.get("enabled", True))),
        "enabled": bool_value(raw.get("enabled", raw.get("is_active", True))),
        "status": first_text(raw, ("status",)) or "active",
        "used_in_material_count": int_or_none(raw.get("used_in_material_count")) or 0,
        "last_used_at": timestamp_or_none(raw.get("last_used_at")),
        "created_at": timestamp_or_none(raw.get("created_at")) or now,
        "updated_at": now,
    }


def dedupe_seed_items(rows):
    items = []
    seen = set()
    duplicate_count = 0
    invalid_count = 0
    for raw in rows:
        item = normalize_seed_item(raw)
        if not item:
            invalid_count += 1
            continue
        key = (item["normalized_key"], item["jlpt_level"])
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        items.append(item)
    return items, duplicate_count, invalid_count


class Database:
    def __init__(self, apply):
        self.apply = apply
        self.database_url = os.environ.get("DATABASE_URL", "").strip()
        self.kind = "postgres" if self.database_url else "sqlite"
        self.conn = None
        self.param = "%s" if self.kind == "postgres" else "?"

    def __enter__(self):
        if self.kind == "postgres":
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError("psycopg is required for PostgreSQL seeding") from exc
            self.conn = psycopg.connect(self.database_url, connect_timeout=5)
        else:
            db_path = Path(os.environ.get("SQLITE_DB_PATH", "").strip() or DEFAULT_SQLITE_DB)
            if self.apply:
                self.conn = sqlite3.connect(str(db_path), timeout=10)
            else:
                self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
            self.conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.conn:
            if exc_type:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
            self.conn.close()

    def fetchall(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        if self.kind == "postgres":
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in rows]
        return [dict(row) for row in rows]

    def fetchone(self, sql, params=()):
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, tuple(params))
        return cur

    def commit(self):
        if self.apply:
            self.conn.commit()

    def rollback(self):
        if self.apply:
            self.conn.rollback()

    def table_exists(self):
        if self.kind == "postgres":
            row = self.fetchone("SELECT to_regclass(%s) AS table_name", ("vocabulary_pool",))
            return bool(row and row.get("table_name"))
        row = self.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("vocabulary_pool",),
        )
        return bool(row)

    def columns(self):
        if self.kind == "postgres":
            rows = self.fetchall(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = %s
                """,
                ("vocabulary_pool",),
            )
            return {row["column_name"] for row in rows}
        return {row["name"] for row in self.fetchall("PRAGMA table_info(vocabulary_pool)")}


def count_total(db):
    row = db.fetchone("SELECT COUNT(*) AS count FROM vocabulary_pool")
    return int(row.get("count") or 0) if row else 0


def group_counts(db, column):
    rows = db.fetchall(
        f"""
        SELECT COALESCE(NULLIF({column}, ''), '__empty__') AS key, COUNT(*) AS count
        FROM vocabulary_pool
        GROUP BY key
        ORDER BY count DESC, key
        """
    )
    return {row["key"]: int(row["count"] or 0) for row in rows}


def safe_pool_count(db):
    columns = db.columns()
    active_clauses = []
    if "is_active" in columns:
        active_clauses.append(
            "COALESCE(is_active, TRUE) = TRUE"
            if db.kind == "postgres"
            else "COALESCE(is_active, 1) = 1"
        )
    if "enabled" in columns:
        active_clauses.append(
            "COALESCE(enabled, TRUE) = TRUE"
            if db.kind == "postgres"
            else "COALESCE(enabled, 1) = 1"
        )
    active_sql = " AND ".join(active_clauses) or "1 = 1"
    row = db.fetchone(
        f"""
        SELECT COUNT(*) AS count
        FROM vocabulary_pool
        WHERE jlpt_level IN ('N5', 'N4', 'N3', 'N2', 'N1')
          AND {active_sql}
          AND COALESCE(NULLIF(meaning_zh, ''), '') <> ''
          AND COALESCE(NULLIF(reading_hiragana, ''), '') <> ''
          AND LOWER(COALESCE(quality, 'normal')) NOT IN ('rejected', 'experimental', 'low_quality')
          AND (
            LOWER(COALESCE(NULLIF(category, ''), 'general')) IN ('general', 'common', 'daily', 'jlpt_core', 'seed')
            OR COALESCE(NULLIF(category, ''), '') = ''
          )
          AND (
            COALESCE(NULLIF(source, ''), '') = ''
            OR LOWER(COALESCE(source, '')) NOT IN ('seed_advanced', 'auto_generated', 'synthetic', 'seed_advanced_synthetic')
          )
        """
    )
    return int(row.get("count") or 0) if row else 0


def count_where(db, where_sql):
    row = db.fetchone(f"SELECT COUNT(*) AS count FROM vocabulary_pool WHERE {where_sql}")
    return int(row.get("count") or 0) if row else 0


def load_existing_maps(db, columns):
    select_columns = [column for column in ("id", "normalized_key", "jlpt_level", "base_form", "surface") if column in columns]
    if "id" not in select_columns:
        raise RuntimeError("vocabulary_pool.id column is required")
    rows = db.fetchall(f"SELECT {', '.join(select_columns)} FROM vocabulary_pool")
    by_norm_level = {}
    by_base_level = {}
    for row in rows:
        level = clean_text(row.get("jlpt_level")).upper()
        normalized = normalize_vocab_key(row.get("normalized_key"))
        base = normalize_vocab_key(row.get("base_form") or row.get("surface"))
        if normalized:
            by_norm_level[(normalized, level)] = row
        if base:
            by_base_level[(base, level)] = row
    return by_norm_level, by_base_level


def existing_row_for_item(db, columns, item, existing_by_norm_level, existing_by_base_level):
    key = (item["normalized_key"], item["jlpt_level"])
    row = existing_by_norm_level.get(key) or existing_by_base_level.get((normalize_vocab_key(item["base_form"]), item["jlpt_level"]))
    if row and len(row.keys()) > 5:
        return row
    if not row:
        return None
    select_columns = [column for column in PREFERRED_COLUMNS if column in columns]
    select_columns = ["id", *[column for column in select_columns if column != "id"]]
    return db.fetchone(f"SELECT {', '.join(select_columns)} FROM vocabulary_pool WHERE id = {db.param}", (row["id"],))


def is_empty_existing_value(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def missing_update_columns(existing, item, columns):
    missing = []
    for column in PREFERRED_COLUMNS:
        if column in {"id", "created_at", "updated_at", "last_used_at", "used_in_material_count"}:
            continue
        if column not in columns or column not in item:
            continue
        value = item.get(column)
        if value in (None, ""):
            continue
        if is_empty_existing_value(existing.get(column)):
            missing.append(column)
    return missing


def insert_item(db, columns, item):
    writable = [column for column in PREFERRED_COLUMNS if column in columns and column in item]
    placeholders = ", ".join([db.param] * len(writable))
    sql = f"INSERT INTO vocabulary_pool ({', '.join(writable)}) VALUES ({placeholders})"
    db.execute(sql, [item[column] for column in writable])


def update_item(db, columns_to_update, item, row_id, columns):
    assignments = []
    params = []
    for column in columns_to_update:
        assignments.append(f"{column} = {db.param}")
        params.append(item[column])
    if "updated_at" in columns:
        assignments.append(f"updated_at = {db.param}")
        params.append(item["updated_at"])
    if not assignments:
        return
    params.append(row_id)
    db.execute(
        f"UPDATE vocabulary_pool SET {', '.join(assignments)} WHERE id = {db.param}",
        params,
    )


def classify_and_optionally_apply(db, items, apply):
    columns = db.columns()
    existing_by_norm_level, existing_by_base_level = load_existing_maps(db, columns)
    stats = {
        "inserted_count": 0,
        "updated_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
    }
    for item in items:
        try:
            existing = existing_row_for_item(db, columns, item, existing_by_norm_level, existing_by_base_level)
            if not existing:
                stats["inserted_count"] += 1
                if apply:
                    insert_item(db, columns, item)
                continue
            update_columns = missing_update_columns(existing, item, columns)
            if update_columns:
                stats["updated_count"] += 1
                if apply:
                    update_item(db, update_columns, item, existing["id"], columns)
            else:
                stats["skipped_count"] += 1
        except Exception:
            stats["failed_count"] += 1
            if apply:
                db.rollback()
            print(f"[seed-vocabulary-pool] item failed key={item.get('normalized_key')} level={item.get('jlpt_level')}", file=sys.stderr)
            print_exception()
    if apply:
        db.commit()
    return stats


def print_exception():
    import traceback

    print(traceback.format_exc(), file=sys.stderr)


def summarize_items(items):
    return {
        "by_level": dict(Counter(item["jlpt_level"] or "__empty__" for item in items).most_common()),
        "by_category": dict(Counter(item["category"] or "__empty__" for item in items).most_common()),
        "by_source": dict(Counter(item["source"] or "__empty__" for item in items).most_common()),
    }


def build_report(db, items, seed_counts, dedupe_count, invalid_count, apply):
    if not db.table_exists():
        raise RuntimeError("vocabulary_pool table does not exist; refusing to create schema")
    before_total = count_total(db)
    stats = classify_and_optionally_apply(db, items, apply)
    after_total = count_total(db) if apply else before_total + stats["inserted_count"]
    return {
        "mode": "apply" if apply else "dry_run",
        "database": db.kind,
        "seed_files": seed_counts,
        "seed_items_after_dedupe": len(items),
        "seed_duplicate_input_count": dedupe_count,
        "seed_invalid_input_count": invalid_count,
        "before_total": before_total,
        "after_total": after_total,
        "inserted_count": stats["inserted_count"],
        "updated_count": stats["updated_count"],
        "skipped_count": stats["skipped_count"],
        "failed_count": stats["failed_count"],
        "seed_summary": summarize_items(items),
        "db_by_level": group_counts(db, "jlpt_level") if apply else None,
        "db_by_category": group_counts(db, "category") if apply else None,
        "db_by_source": group_counts(db, "source") if apply else None,
        "safe_pool_estimated_count": safe_pool_count(db) if apply else None,
        "advanced_count": count_where(db, "LOWER(COALESCE(category, '')) = 'advanced'") if apply else None,
        "business_count": count_where(db, "LOWER(COALESCE(category, '')) = 'business'") if apply else None,
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Safely seed vocabulary_pool from local seed JSON files.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write seed rows into vocabulary_pool.")
    mode.add_argument("--dry-run", action="store_true", help="Preview changes without writing. Default.")
    return parser.parse_args()


def main():
    configure_stdout()
    args = parse_args()
    apply = bool(args.apply)
    raw_rows, seed_counts = load_seed_rows()
    items, duplicate_count, invalid_count = dedupe_seed_items(raw_rows)
    with Database(apply=apply) as db:
        report = build_report(db, items, seed_counts, duplicate_count, invalid_count, apply)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if report["failed_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
