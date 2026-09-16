import argparse
import csv
import json
import os
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEXICON_DIR = ROOT / "data" / "external_lexicon"
DEFAULT_SQLITE_DB = ROOT / "state.sqlite3"


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
    "source_name",
    "source_license",
    "domain_tags",
    "status",
    "last_used_at",
    "created_at",
    "updated_at",
}
INTEGER_COLUMNS = {
    "verb_group",
    "cooldown_days",
    "priority",
    "used_in_material_count",
    "frequency_rank",
}
REAL_COLUMNS = {"commonness_score"}
BOOLEAN_COLUMNS = {"is_active", "enabled"}
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
    "source_name",
    "source_license",
    "domain_tags",
    "status",
    "frequency_rank",
    "commonness_score",
    "priority",
    "is_active",
    "used_in_material_count",
    "last_used_at",
    "created_at",
    "updated_at",
)
BLOCKED_CATEGORIES = {
    "business",
    "advanced",
    "generated_compound",
    "unknown",
    "named_entity",
    "proper_noun",
    "person_name",
    "place_name",
    "company",
    "brand",
    "legal",
    "medical",
    "finance",
    "financial",
    "archaic",
    "classical",
    "specialized",
    "technical",
    "sensitive",
    "typo_or_noise",
}


def configure_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value):
    return "" if value is None else str(value).strip()


def normalize_vocab_key(value):
    return unicodedata.normalize("NFKC", clean_text(value)).lower()


def first_text(raw, keys):
    for key in keys:
        text = clean_text(raw.get(key))
        if text:
            return text
    return ""


def int_or_none(value):
    text = clean_text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def float_or_none(value):
    text = clean_text(value)
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def bool_value(value, default=True):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return clean_text(value).lower() not in {"0", "false", "no", "off", "disabled"}


def load_rows_from_file(path):
    inferred_level = infer_level_from_filename(path.name)
    if path.suffix.lower() == ".json":
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            payload = payload.get("items") or payload.get("data") or payload.get("words") or []
        return [dict(item, _source_file=path.name, _source_level=inferred_level) for item in payload if isinstance(item, dict)]
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row, _source_file=path.name, _source_level=inferred_level) for row in csv.DictReader(handle)]
    return []


def load_external_rows(paths=None):
    files = [Path(path) for path in (paths or [])]
    if not files:
        files = sorted(LEXICON_DIR.glob("*.json")) + sorted(LEXICON_DIR.glob("*.csv"))
    rows = []
    file_counts = {}
    for path in files:
        if not path.exists():
            file_counts[str(path)] = {"exists": False, "count": 0}
            continue
        loaded = load_rows_from_file(path)
        rows.extend(loaded)
        file_counts[str(path)] = {"exists": True, "count": len(loaded)}
    return rows, file_counts


def infer_level_from_filename(filename):
    lowered = clean_text(filename).lower()
    for level in ("n5", "n4", "n3", "n2", "n1"):
        if level in lowered:
            return level.upper()
    if "sns" in lowered:
        return "SNS"
    return ""


def is_openjlpt_row(raw):
    source_file = clean_text(raw.get("_source_file")).lower()
    return source_file.startswith("openjlpt-") or "openjlpt" in clean_text(raw.get("source_name") or raw.get("source")).lower()


def normalize_item(raw):
    surface = first_text(raw, ("surface", "term", "word", "w", "vocab_word", "dictionary_form", "base_form"))
    base_form = first_text(raw, ("base_form", "dictionary_form", "surface", "term", "word", "w")) or surface
    normalized_key = normalize_vocab_key(first_text(raw, ("normalized_key", "normalized_term", "base_form", "surface", "term", "word", "w")) or base_form)
    level = first_text(raw, ("jlpt_level", "level", "l", "target_level", "_source_level")).upper()
    if level and not level.startswith("N") and level in {"1", "2", "3", "4", "5"}:
        level = f"N{level}"
    if not surface or not base_form or not normalized_key or level not in {"N5", "N4", "N3", "N2", "N1", "SNS"}:
        return None
    openjlpt = is_openjlpt_row(raw)
    category = first_text(raw, ("category", "c")) or ("sns" if level == "SNS" else "general")
    status = first_text(raw, ("status",)) or "active"
    quality = first_text(raw, ("quality",)) or "core"
    source_name = "openjlpt" if openjlpt else (first_text(raw, ("source_name", "source", "source_file")) or first_text(raw, ("_source_file",)) or "external_lexicon")
    source = "openjlpt" if openjlpt else (first_text(raw, ("source",)) or "external_lexicon")
    domain_tags = first_text(raw, ("domain_tags", "tags", "domain"))
    if category.lower() in BLOCKED_CATEGORIES:
        return None
    now = utc_now_iso()
    meaning_zh = first_text(raw, ("meaning_zh", "meaning_zh_tw"))
    if not openjlpt:
        meaning_zh = meaning_zh or first_text(raw, ("meaning", "meanings", "m"))
    return {
        "surface": surface,
        "base_form": base_form,
        "normalized_key": normalized_key,
        "reading_hiragana": first_text(raw, ("reading_hiragana", "reading", "r", "kana")),
        "meaning_zh": meaning_zh,
        "part_of_speech": first_text(raw, ("part_of_speech", "pos", "p")),
        "jlpt_level": level,
        "verb_group": int_or_none(raw.get("verb_group") or raw.get("g")),
        "conjugation_type": first_text(raw, ("conjugation_type", "inflection_type")),
        "quality": quality,
        "category": category,
        "cooldown_days": int_or_none(raw.get("cooldown_days")) or 14,
        "example_sentence": first_text(raw, ("example_sentence", "example_japanese", "example_ja", "ex")),
        "example_translation_zh": first_text(raw, ("example_translation_zh", "example_zh", "ex_zh")),
        "source": source,
        "source_name": source_name,
        "source_license": "CC BY-SA 4.0" if openjlpt else first_text(raw, ("source_license", "license")),
        "domain_tags": domain_tags,
        "status": status,
        "frequency_rank": int_or_none(raw.get("frequency_rank") or raw.get("rank")),
        "commonness_score": float_or_none(raw.get("commonness_score") or raw.get("commonness")),
        "priority": int_or_none(raw.get("priority")) or 3,
        "is_active": bool_value(raw.get("is_active", raw.get("enabled", True))),
        "used_in_material_count": int_or_none(raw.get("used_in_material_count")) or 0,
        "last_used_at": clean_text(raw.get("last_used_at")) or None,
        "created_at": clean_text(raw.get("created_at")) or now,
        "updated_at": now,
    }


def dedupe_items(rows):
    items = []
    seen = set()
    stats = Counter()
    for row in rows:
        item = normalize_item(row)
        if not item:
            stats["skipped_low_quality"] += 1
            continue
        key = (item["normalized_key"], item["jlpt_level"])
        if key in seen:
            stats["skipped_duplicate_input"] += 1
            continue
        seen.add(key)
        items.append(item)
    return items, stats


class Database:
    def __init__(self, apply):
        self.apply = apply
        self.database_url = os.environ.get("DATABASE_URL", "").strip()
        self.kind = "postgres" if self.database_url else "sqlite"
        self.param = "%s" if self.kind == "postgres" else "?"
        self.conn = None

    def __enter__(self):
        if self.kind == "postgres":
            import psycopg

            self.conn = psycopg.connect(self.database_url, connect_timeout=5)
        else:
            db_path = Path(os.environ.get("SQLITE_DB_PATH", "").strip() or DEFAULT_SQLITE_DB)
            uri = f"file:{db_path}?mode=ro" if not self.apply else str(db_path)
            self.conn = sqlite3.connect(uri, uri=not self.apply, timeout=10)
            self.conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.conn:
            if exc_type and self.apply:
                self.conn.rollback()
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

    def columns(self):
        if self.kind == "postgres":
            rows = self.fetchall(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                ("vocabulary_pool",),
            )
            return {row["column_name"] for row in rows}
        return {row["name"] for row in self.fetchall("PRAGMA table_info(vocabulary_pool)")}

    def table_exists(self):
        if self.kind == "postgres":
            row = self.fetchone("SELECT to_regclass(%s) AS table_name", ("vocabulary_pool",))
            return bool(row and row.get("table_name"))
        row = self.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("vocabulary_pool",),
        )
        return bool(row)


def ensure_table(db):
    if not db.apply:
        return
    if db.kind == "postgres":
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS vocabulary_pool (
                id BIGSERIAL PRIMARY KEY,
                surface TEXT NOT NULL,
                base_form TEXT NOT NULL,
                normalized_key TEXT,
                jlpt_level TEXT DEFAULT '',
                created_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ
            )
            """
        )
        column_types = {
            **{column: "TEXT DEFAULT ''" for column in TEXT_COLUMNS},
            **{column: "INTEGER" for column in INTEGER_COLUMNS},
            **{column: "REAL" for column in REAL_COLUMNS},
            **{column: "BOOLEAN DEFAULT TRUE" for column in BOOLEAN_COLUMNS},
        }
        for column, col_type in column_types.items():
            db.execute(f"ALTER TABLE vocabulary_pool ADD COLUMN IF NOT EXISTS {column} {col_type}")
        db.execute("CREATE INDEX IF NOT EXISTS idx_vocab_pool_normalized ON vocabulary_pool(normalized_key)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_vocab_pool_level_category ON vocabulary_pool(jlpt_level, category)")
    else:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS vocabulary_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                surface TEXT NOT NULL,
                base_form TEXT NOT NULL,
                normalized_key TEXT,
                jlpt_level TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT,
                UNIQUE(base_form, jlpt_level)
            )
            """
        )
        columns = db.columns()
        for column in TEXT_COLUMNS:
            if column not in columns:
                db.execute(f"ALTER TABLE vocabulary_pool ADD COLUMN {column} TEXT DEFAULT ''")
        for column in INTEGER_COLUMNS | BOOLEAN_COLUMNS:
            if column not in columns:
                db.execute(f"ALTER TABLE vocabulary_pool ADD COLUMN {column} INTEGER")
        for column in REAL_COLUMNS:
            if column not in columns:
                db.execute(f"ALTER TABLE vocabulary_pool ADD COLUMN {column} REAL")
        db.execute("CREATE INDEX IF NOT EXISTS idx_vocab_pool_normalized ON vocabulary_pool(normalized_key)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_vocab_pool_level_category ON vocabulary_pool(jlpt_level, category)")


def existing_keys(db):
    if not db.table_exists():
        return set()
    rows = db.fetchall("SELECT normalized_key, jlpt_level FROM vocabulary_pool")
    return {(normalize_vocab_key(row.get("normalized_key")), clean_text(row.get("jlpt_level")).upper()) for row in rows}


def insert_items(db, items):
    columns = [column for column in PREFERRED_COLUMNS if column in db.columns()]
    keys = existing_keys(db)
    stats = Counter()
    for item in items:
        key = (item["normalized_key"], item["jlpt_level"])
        if key in keys:
            stats["skipped_duplicate"] += 1
            continue
        stats["inserted"] += 1
        if not db.apply:
            continue
        placeholders = ", ".join([db.param] * len(columns))
        db.execute(
            f"INSERT INTO vocabulary_pool ({', '.join(columns)}) VALUES ({placeholders})",
            [item.get(column) for column in columns],
        )
        keys.add(key)
    db.commit()
    return stats


def group(items, field):
    return dict(Counter(clean_text(item.get(field)) or "__empty__" for item in items))


def main():
    configure_stdout()
    parser = argparse.ArgumentParser(description="Import external Japanese vocabulary candidates into vocabulary_pool.")
    parser.add_argument("--apply", action="store_true", help="Write to the database. Default is dry-run.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; this is the default.")
    parser.add_argument("--file", action="append", default=[], help="Specific JSON/CSV file to import. May be repeated.")
    args = parser.parse_args()
    apply = bool(args.apply)
    rows, files = load_external_rows(args.file)
    items, normalize_stats = dedupe_items(rows)
    with Database(apply=apply) as db:
        ensure_table(db)
        stats = insert_items(db, items)
    output = {
        "mode": "apply" if apply else "dry_run",
        "database": "postgres" if os.environ.get("DATABASE_URL", "").strip() else "sqlite",
        "source_files": files,
        "input_count": len(rows),
        "normalized_count": len(items),
        "inserted_count": int(stats.get("inserted", 0)),
        "skipped_duplicate": int(stats.get("skipped_duplicate", 0)),
        "skipped_duplicate_input": int(normalize_stats.get("skipped_duplicate_input", 0)),
        "skipped_low_quality": int(normalize_stats.get("skipped_low_quality", 0)),
        "by_level": group(items, "jlpt_level"),
        "by_category": group(items, "category"),
        "by_source": group(items, "source"),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"[vocabulary-pool] imported total={output['inserted_count']}")
    print("[vocabulary-pool] by_level " + " ".join(f"{level}={output['by_level'].get(level, 0)}" for level in ("N5", "N4", "N3", "N2", "N1", "SNS")))
    print(f"[vocabulary-pool] skipped_duplicate={output['skipped_duplicate']}")
    print(f"[vocabulary-pool] skipped_low_quality={output['skipped_low_quality']}")


if __name__ == "__main__":
    main()
