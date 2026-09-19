import argparse
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEED_FILE = ROOT / "data" / "grammar_seed_n5_n1.json"
SQLITE_SETTINGS_FILE = ROOT / "state.sqlite3"


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value):
    return str(value or "").strip()


def normalize_level(value):
    level = clean_text(value).upper()
    return level if level in {"N5", "N4", "N3", "N2", "N1"} else ""


def normalize_seed_item(raw):
    if not isinstance(raw, dict):
        return None
    grammar_key = clean_text(raw.get("grammar_key"))
    pattern = clean_text(raw.get("pattern")) or grammar_key
    display_name = clean_text(raw.get("display_name")) or pattern
    level = normalize_level(raw.get("jlpt_level"))
    if not grammar_key or not level:
        return None
    return {
        "grammar_key": grammar_key,
        "pattern": pattern,
        "display_name": display_name,
        "jlpt_level": level,
        "meaning_seed": clean_text(raw.get("meaning_seed")),
        "connection_seed": clean_text(raw.get("connection_seed")),
        "category": clean_text(raw.get("category")) or "basic",
        "source": clean_text(raw.get("source")) or "manual_seed",
        "status": clean_text(raw.get("status")) or "active",
    }


def load_seed(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = []
    seen = set()
    invalid = 0
    duplicate_input = 0
    for raw in data:
        item = normalize_seed_item(raw)
        if not item:
            invalid += 1
            continue
        key = item["grammar_key"]
        if key in seen:
            duplicate_input += 1
            continue
        seen.add(key)
        items.append(item)
    return items, {"invalid": invalid, "duplicate_input": duplicate_input}


def database_kind():
    if os.getenv("DATABASE_URL"):
        return "postgres"
    return "sqlite"


def ensure_sqlite_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grammar_pool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grammar_key TEXT UNIQUE NOT NULL,
            pattern TEXT DEFAULT '',
            display_name TEXT DEFAULT '',
            jlpt_level TEXT DEFAULT '',
            meaning_seed TEXT DEFAULT '',
            connection_seed TEXT DEFAULT '',
            category TEXT DEFAULT 'basic',
            source TEXT DEFAULT 'manual_seed',
            status TEXT DEFAULT 'active',
            used_count INTEGER DEFAULT 0,
            last_used_at TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_grammar_pool_key ON grammar_pool(grammar_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_grammar_pool_level_status ON grammar_pool(jlpt_level, status)")


def ensure_postgres_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS grammar_pool (
                id BIGSERIAL PRIMARY KEY,
                grammar_key TEXT UNIQUE NOT NULL,
                pattern TEXT DEFAULT '',
                display_name TEXT DEFAULT '',
                jlpt_level TEXT DEFAULT '',
                meaning_seed TEXT DEFAULT '',
                connection_seed TEXT DEFAULT '',
                category TEXT DEFAULT 'basic',
                source TEXT DEFAULT 'manual_seed',
                status TEXT DEFAULT 'active',
                used_count INTEGER DEFAULT 0,
                last_used_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ
            )
            """
        )
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_grammar_pool_key ON grammar_pool(grammar_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_grammar_pool_level_status ON grammar_pool(jlpt_level, status)")
    conn.commit()


def existing_keys_sqlite(conn):
    try:
        rows = conn.execute("SELECT grammar_key FROM grammar_pool").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {row[0] for row in rows if row and row[0]}


def existing_keys_postgres(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT grammar_key FROM grammar_pool")
            rows = cur.fetchall()
    except Exception:
        return set()
    return {row[0] for row in rows if row and row[0]}


def apply_sqlite(items):
    inserted = 0
    skipped = 0
    now = utc_now_iso()
    with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
        ensure_sqlite_table(conn)
        existing = existing_keys_sqlite(conn)
        for item in items:
            if item["grammar_key"] in existing:
                skipped += 1
                continue
            conn.execute(
                """
                INSERT INTO grammar_pool (
                    grammar_key, pattern, display_name, jlpt_level, meaning_seed, connection_seed,
                    category, source, status, used_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    item["grammar_key"],
                    item["pattern"],
                    item["display_name"],
                    item["jlpt_level"],
                    item["meaning_seed"],
                    item["connection_seed"],
                    item["category"],
                    item["source"],
                    item["status"],
                    now,
                    now,
                ),
            )
            inserted += 1
            existing.add(item["grammar_key"])
        conn.commit()
    return inserted, skipped


def apply_postgres(items):
    import psycopg

    inserted = 0
    skipped = 0
    now = utc_now_iso()
    with psycopg.connect(os.getenv("DATABASE_URL")) as conn:
        ensure_postgres_table(conn)
        existing = existing_keys_postgres(conn)
        with conn.cursor() as cur:
            for item in items:
                if item["grammar_key"] in existing:
                    skipped += 1
                    continue
                cur.execute(
                    """
                    INSERT INTO grammar_pool (
                        grammar_key, pattern, display_name, jlpt_level, meaning_seed, connection_seed,
                        category, source, status, used_count, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s)
                    ON CONFLICT (grammar_key) DO NOTHING
                    """,
                    (
                        item["grammar_key"],
                        item["pattern"],
                        item["display_name"],
                        item["jlpt_level"],
                        item["meaning_seed"],
                        item["connection_seed"],
                        item["category"],
                        item["source"],
                        item["status"],
                        now,
                        now,
                    ),
                )
                if cur.rowcount:
                    inserted += 1
                    existing.add(item["grammar_key"])
                else:
                    skipped += 1
        conn.commit()
    return inserted, skipped


def dry_run(items):
    db = database_kind()
    if db == "postgres":
        try:
            import psycopg

            with psycopg.connect(os.getenv("DATABASE_URL")) as conn:
                existing = existing_keys_postgres(conn)
        except Exception:
            existing = set()
    else:
        with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
            existing = existing_keys_sqlite(conn)
    insertable = [item for item in items if item["grammar_key"] not in existing]
    return len(insertable), len(items) - len(insertable)


def main():
    parser = argparse.ArgumentParser(description="Import grammar candidates into grammar_pool.")
    parser.add_argument("--dry-run", action="store_true", help="Preview import without writing.")
    parser.add_argument("--apply", action="store_true", help="Write new grammar candidates.")
    parser.add_argument("--file", default=str(SEED_FILE), help="Seed JSON file path.")
    args = parser.parse_args()

    items, seed_stats = load_seed(args.file)
    mode = "apply" if args.apply else "dry_run"
    if args.apply:
        if database_kind() == "postgres":
            inserted, skipped = apply_postgres(items)
        else:
            inserted, skipped = apply_sqlite(items)
    else:
        inserted, skipped = dry_run(items)

    by_level = dict(Counter(item["jlpt_level"] for item in items))
    result = {
        "mode": mode,
        "database": database_kind(),
        "seed_file": str(Path(args.file).resolve()),
        "input_count": len(items),
        "inserted_count": inserted,
        "skipped_duplicate": skipped,
        "seed_invalid_count": seed_stats["invalid"],
        "seed_duplicate_input_count": seed_stats["duplicate_input"],
        "by_level": {level: by_level.get(level, 0) for level in ["N5", "N4", "N3", "N2", "N1"]},
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"[grammar-pool] imported total={inserted if args.apply else 0}")
    print(
        "[grammar-pool] by_level "
        + " ".join(f"{level}={result['by_level'].get(level, 0)}" for level in ["N5", "N4", "N3", "N2", "N1"])
    )
    print(f"[grammar-pool] skipped_duplicate={skipped}")


if __name__ == "__main__":
    main()
