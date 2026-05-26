import csv
import html
import json
import os
import random
import re
import shutil
import sqlite3
import threading
import time
import traceback
import unicodedata
from collections import Counter
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from flask import Flask, jsonify, render_template, request
from services.grammar_debugger import debug_grammar


app = Flask(__name__, template_folder=".")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def read_int_env(name, default, min_value=None, max_value=None):
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


DATABASE_FILE = os.path.join(BASE_DIR, "database.csv")
DEFAULT_SQLITE_SETTINGS_FILE = os.path.join(BASE_DIR, "state.sqlite3")
SQLITE_SETTINGS_FILE = os.environ.get("SQLITE_DB_PATH", "").strip() or DEFAULT_SQLITE_SETTINGS_FILE
SNS_EXAMPLES_FILE = os.path.join(BASE_DIR, "data", "social_examples.json")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
GEMINI_TIMEOUT_SECONDS = read_int_env("GEMINI_TIMEOUT_SECONDS", 20, 5, 55)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview").strip()
GEMINI_MODEL_CANDIDATES = os.environ.get(
    "GEMINI_MODEL_CANDIDATES",
    "gemini-2.5-flash-lite,gemini-2.5-flash,gemini-2.0-flash-lite,gemini-2.0-flash",
).strip()
TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:5000").rstrip("/")
CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()
DASHBOARD_CACHE_TTL_SECONDS = int(os.environ.get("DASHBOARD_CACHE_TTL_SECONDS", "90"))
_DASHBOARD_CACHE = {"expires_at": None, "payload": None}

LEVELS = ["N5", "N4", "N3", "N2", "N1"]
VERB_FORM_LABELS = {
    "renyou_form": "連用形（ます形去ます）",
    "te_form": "て形",
    "ta_form": "た形",
    "nai_form": "ない形",
    "ba_form": "ば形",
    "shieki_form": "使役形（させる）",
    "ukemi_form": "受身形",
}
QUESTION_TYPES = list(VERB_FORM_LABELS.keys())
ERROR_CATEGORIES = [
    "動詞變化錯",
    "助詞錯",
    "直翻不自然",
    "口語語感不自然",
    "中文直翻造成不自然",
    "SNS語感錯",
    "讀音錯",
    "文法判斷錯",
    "其他",
]
REVIEW_INTERVAL_STEPS = [3, 7, 14, 30]
SLANG_CATEGORIES = {
    "slang",
    "internet_slang",
    "otaku_culture",
    "named_entity",
    "sensitive",
    "typo_or_noise",
    "unknown",
}
SLANG_MATERIAL_CATEGORIES = {"slang", "internet_slang", "otaku_culture"}
KNOWN_SLANG_RULES = [
    {
        "pattern": r"めちゃくちゃ",
        "term": "めちゃくちゃ",
        "normalized_term": "めちゃくちゃ",
        "reading_hiragana": "めちゃくちゃ",
        "base_form": "",
        "part_of_speech": "副詞",
        "category": "slang",
        "meaning_zh": "非常；超級；程度很強",
        "nuance": "常見口語強調詞，可表示程度很高，也可表示混亂或亂七八糟，需依語境判斷。",
        "confidence": 0.93,
    },
    {
        "pattern": r"めっちゃ",
        "term": "めっちゃ",
        "normalized_term": "めっちゃ",
        "reading_hiragana": "めっちゃ",
        "base_form": "",
        "part_of_speech": "副詞",
        "category": "slang",
        "meaning_zh": "非常、超級",
        "nuance": "めちゃくちゃ 的口語變體，常用於日常對話與 SNS。",
        "confidence": 0.93,
    },
    {
        "pattern": r"エモい",
        "term": "エモい",
        "normalized_term": "エモい",
        "reading_hiragana": "えもい",
        "base_form": "エモい",
        "part_of_speech": "形容詞",
        "category": "internet_slang",
        "meaning_zh": "很有氛圍、令人感動、很有情緒感染力",
        "nuance": "用於形容照片、音樂、場景等引發懷舊、感動或難以言說的情緒。",
        "confidence": 0.95,
    },
    {
        "pattern": r"バズ(?:る|った|りそう|ってる|って|りたい|らない|り)",
        "term": "バズる",
        "normalized_term": "バズる",
        "reading_hiragana": "ばずる",
        "base_form": "バズる",
        "part_of_speech": "動詞",
        "category": "internet_slang",
        "meaning_zh": "在網路上爆紅、被大量轉發或討論",
        "nuance": "常用於社群貼文、影片或話題快速被大量轉發與討論的情境。",
        "confidence": 0.95,
    },
    {
        "pattern": r"てぇてぇ",
        "term": "てぇてぇ",
        "normalized_term": "てぇてぇ",
        "reading_hiragana": "てぇてぇ",
        "base_form": "",
        "part_of_speech": "形容詞",
        "category": "otaku_culture",
        "meaning_zh": "尊い、太美好、太值得推了",
        "nuance": "推し活與宅文化常用語，用來表達被角色、偶像或關係性強烈打動。",
        "confidence": 0.94,
    },
    {
        "pattern": r"限界オタク",
        "term": "限界オタク",
        "normalized_term": "限界オタク",
        "reading_hiragana": "げんかいおたく",
        "base_form": "",
        "part_of_speech": "名詞",
        "category": "otaku_culture",
        "meaning_zh": "情緒激動到極限的粉絲、失控粉絲狀態",
        "nuance": "帶自嘲語氣，表示因推、角色或作品太好而情緒激動到接近失控。",
        "confidence": 0.95,
    },
    {
        "pattern": r"さくたん",
        "term": "さくたん",
        "normalized_term": "さくたん",
        "reading_hiragana": "さくたん",
        "base_form": "",
        "part_of_speech": "名詞",
        "category": "named_entity",
        "meaning_zh": "暱稱或特殊名詞",
        "nuance": "可能是人物暱稱或圈內稱呼，需人工確認，不可自動進入每日教材。",
        "confidence": 0.88,
    },
    {
        "pattern": r"ねんねちゃん",
        "term": "ねんねちゃん",
        "normalized_term": "ねんねちゃん",
        "reading_hiragana": "ねんねちゃん",
        "base_form": "",
        "part_of_speech": "名詞",
        "category": "named_entity",
        "meaning_zh": "暱稱或特殊名詞",
        "nuance": "可能是人物暱稱、角色稱呼或圈內用語，需人工確認，不可自動進入每日教材。",
        "confidence": 0.86,
    },
]
ANSWER_READING_FALLBACKS = {
    "冷える": "ひえる",
    "冷えた": "ひえた",
    "冷えだ": "ひえだ",
    "乗り換える": "のりかえる",
    "乗り換えさせる": "のりかえさせる",
    "励ます": "はげます",
    "励まれる": "はげまれる",
    "励まされる": "はげまされる",
    "断れば": "ことわれば",
    "断る": "ことわる",
    "断り": "ことわり",
    "断って": "ことわって",
    "断った": "ことわった",
    "断らない": "ことわらない",
    "断られる": "ことわられる",
    "断らせる": "ことわらせる",
    "吹かれる": "ふかれる",
    "吹けば": "ふけば",
    "吹く": "ふく",
    "降れば": "ふれば",
    "降る": "ふる",
    "降って": "ふって",
    "降った": "ふった",
    "降らない": "ふらない",
    "降りる": "おりる",
    "降りれば": "おりれば",
}

SEED_VERBS = [
    {
        "dictionary_form": "行く",
        "reading": "いく",
        "verb_group": 1,
        "meaning": "去",
        "te_form": "行って",
        "ta_form": "行った",
        "nai_form": "行かない",
        "renyou_form": "行き",
        "shieki_form": "行かせる",
        "ukemi_form": "行かれる",
        "ba_form": "行けば",
    },
    {
        "dictionary_form": "書く",
        "reading": "かく",
        "verb_group": 1,
        "meaning": "寫",
        "te_form": "書いて",
        "ta_form": "書いた",
        "nai_form": "書かない",
        "renyou_form": "書き",
        "shieki_form": "書かせる",
        "ukemi_form": "書かれる",
        "ba_form": "書けば",
    },
    {
        "dictionary_form": "話す",
        "reading": "はなす",
        "verb_group": 1,
        "meaning": "說話",
        "te_form": "話して",
        "ta_form": "話した",
        "nai_form": "話さない",
        "renyou_form": "話し",
        "shieki_form": "話させる",
        "ukemi_form": "話される",
        "ba_form": "話せば",
    },
    {
        "dictionary_form": "食べる",
        "reading": "たべる",
        "verb_group": 2,
        "meaning": "吃",
        "te_form": "食べて",
        "ta_form": "食べた",
        "nai_form": "食べない",
        "renyou_form": "食べ",
        "shieki_form": "食べさせる",
        "ukemi_form": "食べられる",
        "ba_form": "食べれば",
    },
    {
        "dictionary_form": "見る",
        "reading": "みる",
        "verb_group": 2,
        "meaning": "看",
        "te_form": "見て",
        "ta_form": "見た",
        "nai_form": "見ない",
        "renyou_form": "見",
        "shieki_form": "見させる",
        "ukemi_form": "見られる",
        "ba_form": "見れば",
    },
    {
        "dictionary_form": "する",
        "reading": "する",
        "verb_group": 3,
        "meaning": "做",
        "te_form": "して",
        "ta_form": "した",
        "nai_form": "しない",
        "renyou_form": "し",
        "shieki_form": "させる",
        "ukemi_form": "される",
        "ba_form": "すれば",
    },
    {
        "dictionary_form": "来る",
        "reading": "くる",
        "verb_group": 3,
        "meaning": "來",
        "te_form": "来て",
        "ta_form": "来た",
        "nai_form": "来ない",
        "renyou_form": "来",
        "shieki_form": "来させる",
        "ukemi_form": "来られる",
        "ba_form": "来れば",
    },
]

COLUMNS = [
    "date",
    "target_level",
    "vocab_word",
    "vocab_reading",
    "vocab_meaning",
    "vocab_part_of_speech",
    "vocab_source",
    "vocab_example_sentence",
    "vocab_example_translation_zh",
    "verb_base",
    "verb_masu_stem",
    "verb_te",
    "verb_ta",
    "verb_nai",
    "verb_ba",
    "verb_causative",
    "verb_passive",
    "verb_causative_passive",
    "grammar_title",
    "grammar_exp",
    "grammar_examples",
    "material_json",
    "generation_mode",
    "ai_used",
    "source_summary",
    "created_at",
    "updated_at",
]

DEFAULT_SETTINGS = {
    "target_level": "N3",
    "vocab_count": "8",
    "verb_count": "4",
    "mcq_count": "5",
    "fill_count": "5",
}

SETTING_ALIASES = {
    "targetLevel": "target_level",
    "vocabCount": "vocab_count",
    "verbCount": "verb_count",
    "quizMcqCount": "mcq_count",
    "quizFillCount": "fill_count",
}


def today_string():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    return f"{now.year}/{now.month}/{now.day}"


def today_iso_date():
    return datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()


def iso_date_after(days):
    return (datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=days)).isoformat()


def taipei_now():
    return datetime.now(ZoneInfo("Asia/Taipei"))


def taipei_iso_now():
    return taipei_now().isoformat(timespec="seconds")


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_timestamp(value):
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def is_temporal_field(field_name):
    name = str(field_name or "").lower()
    return name.endswith("_at") or "timestamp" in name or "time" in name or "date" in name


def clean_db_payload(payload):
    cleaned = {}
    for key, value in dict(payload or {}).items():
        if is_temporal_field(key):
            cleaned[key] = clean_timestamp(value)
        else:
            cleaned[key] = value
    return cleaned


def rolling_start(days):
    return (taipei_now().date() - timedelta(days=days - 1)).isoformat()


def parse_material_date(value):
    try:
        return datetime.strptime(str(value), "%Y/%m/%d").date()
    except ValueError:
        return None


def prepare_sqlite_path():
    target = os.path.abspath(SQLITE_SETTINGS_FILE)
    target_dir = os.path.dirname(target)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)
    if target != os.path.abspath(DEFAULT_SQLITE_SETTINGS_FILE) and not os.path.exists(target) and os.path.exists(DEFAULT_SQLITE_SETTINGS_FILE):
        shutil.copy2(DEFAULT_SQLITE_SETTINGS_FILE, target)
        print(f"[sqlite] copied legacy database to {target}")
    try:
        with open(target, "a+b"):
            pass
    except OSError as e:
        print(f"[sqlite] database path is not writable: {target} ({e})")
        raise


def invalidate_dashboard_cache(reason=""):
    _DASHBOARD_CACHE["expires_at"] = None
    _DASHBOARD_CACHE["payload"] = None
    if reason:
        print(f"[dashboard-cache] invalidated: {reason}")


def normalize_settings(raw):
    normalized = {}
    for key, value in (raw or {}).items():
        normalized[SETTING_ALIASES.get(key, key)] = value

    settings = DEFAULT_SETTINGS.copy()
    settings.update({k: str(v) for k, v in normalized.items() if k in settings and str(v) != ""})

    if settings["target_level"] not in LEVELS:
        settings["target_level"] = DEFAULT_SETTINGS["target_level"]

    for key, default, min_value, max_value in [
        ("vocab_count", 8, 1, 30),
        ("verb_count", 4, 0, 20),
        ("mcq_count", 5, 0, 30),
        ("fill_count", 5, 0, 30),
    ]:
        try:
            value = int(settings[key])
        except ValueError:
            value = default
        settings[key] = str(max(min_value, min(value, max_value)))

    return settings


def ensure_settings_store():
    prepare_sqlite_path()
    with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS verbs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dictionary_form TEXT NOT NULL,
                reading TEXT NOT NULL,
                verb_group INTEGER NOT NULL,
                meaning TEXT NOT NULL,
                te_form TEXT NOT NULL,
                ta_form TEXT NOT NULL,
                nai_form TEXT NOT NULL,
                renyou_form TEXT NOT NULL,
                shieki_form TEXT NOT NULL,
                ukemi_form TEXT NOT NULL,
                ba_form TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mistake_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                verb_id INTEGER NOT NULL,
                question_type TEXT NOT NULL,
                user_wrong_answer TEXT NOT NULL,
                mistake_count INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'learning',
                last_reviewed_at DATETIME NOT NULL,
                FOREIGN KEY (verb_id) REFERENCES verbs(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sns_favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sns_id TEXT NOT NULL,
                japanese TEXT NOT NULL,
                user_note TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sns_practice_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                example_id TEXT NOT NULL,
                user_translation TEXT DEFAULT '',
                self_evaluation TEXT NOT NULL,
                tone_category TEXT DEFAULT '',
                error_category TEXT DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                total_questions INTEGER NOT NULL DEFAULT 0,
                correct_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        migrate_mistake_logs(conn)
        migrate_sns_practice_logs(conn)
        migrate_quiz_records(conn)
        migrate_slang_candidates_sqlite(conn)
        migrate_vocabulary_pool_sqlite(conn)
        conn.commit()
    seed_verbs_if_empty()


def migrate_slang_candidates_sqlite(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS slang_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            term TEXT NOT NULL UNIQUE,
            normalized_term TEXT,
            reading_hiragana TEXT,
            base_form TEXT,
            part_of_speech TEXT,
            category TEXT,
            meaning_zh TEXT,
            nuance TEXT,
            example_sentence TEXT,
            source TEXT,
            source_context TEXT,
            frequency_count INTEGER DEFAULT 1,
            confidence REAL,
            status TEXT DEFAULT 'pending',
            review_note TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            reviewed_at TEXT,
            used_in_material_count INTEGER DEFAULT 0,
            last_used_at TEXT
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(slang_candidates)").fetchall()}
    migrations = {
        "normalized_term": "ALTER TABLE slang_candidates ADD COLUMN normalized_term TEXT",
        "reading_hiragana": "ALTER TABLE slang_candidates ADD COLUMN reading_hiragana TEXT",
        "base_form": "ALTER TABLE slang_candidates ADD COLUMN base_form TEXT",
        "part_of_speech": "ALTER TABLE slang_candidates ADD COLUMN part_of_speech TEXT",
        "category": "ALTER TABLE slang_candidates ADD COLUMN category TEXT",
        "meaning_zh": "ALTER TABLE slang_candidates ADD COLUMN meaning_zh TEXT",
        "nuance": "ALTER TABLE slang_candidates ADD COLUMN nuance TEXT",
        "example_sentence": "ALTER TABLE slang_candidates ADD COLUMN example_sentence TEXT",
        "source": "ALTER TABLE slang_candidates ADD COLUMN source TEXT",
        "source_context": "ALTER TABLE slang_candidates ADD COLUMN source_context TEXT",
        "frequency_count": "ALTER TABLE slang_candidates ADD COLUMN frequency_count INTEGER DEFAULT 1",
        "confidence": "ALTER TABLE slang_candidates ADD COLUMN confidence REAL",
        "status": "ALTER TABLE slang_candidates ADD COLUMN status TEXT DEFAULT 'pending'",
        "review_note": "ALTER TABLE slang_candidates ADD COLUMN review_note TEXT",
        "first_seen_at": "ALTER TABLE slang_candidates ADD COLUMN first_seen_at TEXT",
        "last_seen_at": "ALTER TABLE slang_candidates ADD COLUMN last_seen_at TEXT",
        "reviewed_at": "ALTER TABLE slang_candidates ADD COLUMN reviewed_at TEXT",
        "used_in_material_count": "ALTER TABLE slang_candidates ADD COLUMN used_in_material_count INTEGER DEFAULT 0",
        "last_used_at": "ALTER TABLE slang_candidates ADD COLUMN last_used_at TEXT",
    }
    for column, statement in migrations.items():
        if column not in columns:
            conn.execute(statement)
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE slang_candidates
        SET status = COALESCE(NULLIF(status, ''), 'pending'),
            category = COALESCE(NULLIF(category, ''), 'unknown'),
            frequency_count = COALESCE(frequency_count, 1),
            first_seen_at = COALESCE(NULLIF(first_seen_at, ''), ?),
            last_seen_at = COALESCE(NULLIF(last_seen_at, ''), ?),
            used_in_material_count = COALESCE(used_in_material_count, 0)
        """,
        (now, now),
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_slang_candidates_status ON slang_candidates(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_slang_candidates_category ON slang_candidates(category)")


def migrate_vocabulary_pool_sqlite(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vocabulary_pool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            surface TEXT NOT NULL,
            base_form TEXT NOT NULL,
            reading_hiragana TEXT DEFAULT '',
            meaning_zh TEXT DEFAULT '',
            part_of_speech TEXT DEFAULT '',
            jlpt_level TEXT DEFAULT '',
            example_sentence TEXT DEFAULT '',
            example_translation_zh TEXT DEFAULT '',
            source TEXT DEFAULT 'manual',
            priority INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1,
            used_in_material_count INTEGER DEFAULT 0,
            last_used_at TEXT,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(base_form, jlpt_level)
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(vocabulary_pool)").fetchall()}
    migrations = {
        "surface": "ALTER TABLE vocabulary_pool ADD COLUMN surface TEXT",
        "base_form": "ALTER TABLE vocabulary_pool ADD COLUMN base_form TEXT",
        "reading_hiragana": "ALTER TABLE vocabulary_pool ADD COLUMN reading_hiragana TEXT DEFAULT ''",
        "meaning_zh": "ALTER TABLE vocabulary_pool ADD COLUMN meaning_zh TEXT DEFAULT ''",
        "part_of_speech": "ALTER TABLE vocabulary_pool ADD COLUMN part_of_speech TEXT DEFAULT ''",
        "jlpt_level": "ALTER TABLE vocabulary_pool ADD COLUMN jlpt_level TEXT DEFAULT ''",
        "example_sentence": "ALTER TABLE vocabulary_pool ADD COLUMN example_sentence TEXT DEFAULT ''",
        "example_translation_zh": "ALTER TABLE vocabulary_pool ADD COLUMN example_translation_zh TEXT DEFAULT ''",
        "source": "ALTER TABLE vocabulary_pool ADD COLUMN source TEXT DEFAULT 'manual'",
        "priority": "ALTER TABLE vocabulary_pool ADD COLUMN priority INTEGER DEFAULT 1",
        "is_active": "ALTER TABLE vocabulary_pool ADD COLUMN is_active INTEGER DEFAULT 1",
        "used_in_material_count": "ALTER TABLE vocabulary_pool ADD COLUMN used_in_material_count INTEGER DEFAULT 0",
        "last_used_at": "ALTER TABLE vocabulary_pool ADD COLUMN last_used_at TEXT",
        "created_at": "ALTER TABLE vocabulary_pool ADD COLUMN created_at TEXT",
        "updated_at": "ALTER TABLE vocabulary_pool ADD COLUMN updated_at TEXT",
    }
    for column, statement in migrations.items():
        if column not in columns:
            conn.execute(statement)
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE vocabulary_pool
        SET surface = COALESCE(NULLIF(surface, ''), base_form),
            base_form = COALESCE(NULLIF(base_form, ''), surface),
            source = COALESCE(NULLIF(source, ''), 'manual'),
            priority = COALESCE(priority, 1),
            is_active = COALESCE(is_active, 1),
            used_in_material_count = COALESCE(used_in_material_count, 0),
            created_at = COALESCE(NULLIF(created_at, ''), ?),
            updated_at = COALESCE(NULLIF(updated_at, ''), ?)
        """,
        (now, now),
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_base_level ON vocabulary_pool(base_form, jlpt_level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_level ON vocabulary_pool(jlpt_level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_active ON vocabulary_pool(is_active)")


def migrate_mistake_logs(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(mistake_logs)").fetchall()}
    migrations = {
        "next_review_date": "ALTER TABLE mistake_logs ADD COLUMN next_review_date TEXT",
        "review_interval": "ALTER TABLE mistake_logs ADD COLUMN review_interval INTEGER NOT NULL DEFAULT 1",
        "review_count": "ALTER TABLE mistake_logs ADD COLUMN review_count INTEGER NOT NULL DEFAULT 0",
        "last_reviewed_at": "ALTER TABLE mistake_logs ADD COLUMN last_reviewed_at TEXT",
        "mastered": "ALTER TABLE mistake_logs ADD COLUMN mastered INTEGER NOT NULL DEFAULT 0",
        "error_category": "ALTER TABLE mistake_logs ADD COLUMN error_category TEXT",
        "debug_report_json": "ALTER TABLE mistake_logs ADD COLUMN debug_report_json TEXT",
    }
    for column, statement in migrations.items():
        if column not in columns:
            conn.execute(statement)

    now = datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds")
    today = today_iso_date()
    conn.execute(
        """
        UPDATE mistake_logs
        SET last_reviewed_at = COALESCE(NULLIF(last_reviewed_at, ''), ?),
            next_review_date = COALESCE(NULLIF(next_review_date, ''), ?),
            review_interval = COALESCE(review_interval, 1),
            review_count = COALESCE(review_count, 0),
            mastered = CASE WHEN status = 'mastered' THEN 1 ELSE COALESCE(mastered, 0) END,
            error_category = COALESCE(NULLIF(error_category, ''), '動詞變化錯')
        """,
        (now, today),
    )


def migrate_sns_practice_logs(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sns_practice_logs)").fetchall()}
    migrations = {
        "created_at": "ALTER TABLE sns_practice_logs ADD COLUMN created_at TEXT",
        "example_id": "ALTER TABLE sns_practice_logs ADD COLUMN example_id TEXT",
        "user_translation": "ALTER TABLE sns_practice_logs ADD COLUMN user_translation TEXT DEFAULT ''",
        "self_evaluation": "ALTER TABLE sns_practice_logs ADD COLUMN self_evaluation TEXT DEFAULT 'skip'",
        "tone_category": "ALTER TABLE sns_practice_logs ADD COLUMN tone_category TEXT DEFAULT ''",
        "error_category": "ALTER TABLE sns_practice_logs ADD COLUMN error_category TEXT DEFAULT ''",
    }
    for column, statement in migrations.items():
        if column not in columns:
            conn.execute(statement)
    now = datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds")
    conn.execute("UPDATE sns_practice_logs SET created_at = COALESCE(NULLIF(created_at, ''), ?)", (now,))


def migrate_quiz_records(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(quiz_records)").fetchall()}
    migrations = {
        "created_at": "ALTER TABLE quiz_records ADD COLUMN created_at TEXT",
        "total_questions": "ALTER TABLE quiz_records ADD COLUMN total_questions INTEGER NOT NULL DEFAULT 0",
        "correct_count": "ALTER TABLE quiz_records ADD COLUMN correct_count INTEGER NOT NULL DEFAULT 0",
    }
    for column, statement in migrations.items():
        if column not in columns:
            conn.execute(statement)
    now = datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds")
    conn.execute("UPDATE quiz_records SET created_at = COALESCE(NULLIF(created_at, ''), ?)", (now,))


def seed_verbs_if_empty():
    with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
        count = conn.execute("SELECT COUNT(*) FROM verbs").fetchone()[0]
        if count:
            return
        conn.executemany(
            """
            INSERT INTO verbs (
                dictionary_form, reading, verb_group, meaning,
                te_form, ta_form, nai_form, renyou_form,
                shieki_form, ukemi_form, ba_form
            )
            VALUES (
                :dictionary_form, :reading, :verb_group, :meaning,
                :te_form, :ta_form, :nai_form, :renyou_form,
                :shieki_form, :ukemi_form, :ba_form
            )
            """,
            SEED_VERBS,
        )
        conn.commit()
    invalidate_dashboard_cache("mistake mastered")


def sqlite_dicts(query, params=()):
    ensure_settings_store()
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def sqlite_one(query, params=()):
    rows = sqlite_dicts(query, params)
    return rows[0] if rows else None


def load_sns_examples():
    try:
        with open(SNS_EXAMPLES_FILE, "r", encoding="utf-8") as file:
            examples = json.load(file)
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in examples if item.get("id") and item.get("japanese")]


def find_sns_example(example_id):
    for item in load_sns_examples():
        if item.get("id") == example_id:
            return item
    return None


def normalize_slang_category(value):
    category = str(value or "").strip()
    return category if category in SLANG_CATEGORIES else "unknown"


def normalize_slang_status(value):
    status = str(value or "pending").strip()
    return status if status in {"pending", "approved", "rejected"} else "pending"


def slang_candidate_write_mode():
    mode = os.environ.get("SLANG_CANDIDATE_WRITE_MODE", "sync").strip().lower()
    return mode if mode in {"sync", "async"} else "sync"


def debug_endpoints_enabled():
    return os.environ.get("ENABLE_DEBUG_ENDPOINTS", "false").strip().lower() == "true"


def log_slang(message):
    print(f"[slang-candidates] {message}", flush=True)


def log_slang_exception(message):
    print(f"[slang-candidates] {message}\n{traceback.format_exc()}", flush=True)


def clean_slang_text(value):
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def coerce_confidence(value):
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(confidence, 1.0))


def normalize_slang_candidate(raw):
    if not isinstance(raw, dict):
        return None
    term = clean_slang_text(raw.get("term"))
    if not term:
        return None
    category = normalize_slang_category(raw.get("category"))
    reading = enforce_hiragana_reading(raw.get("reading_hiragana"), term)
    return {
        "term": term,
        "normalized_term": clean_slang_text(raw.get("normalized_term")) or term,
        "reading_hiragana": reading,
        "base_form": clean_slang_text(raw.get("base_form")),
        "part_of_speech": clean_slang_text(raw.get("part_of_speech")),
        "category": category,
        "meaning_zh": clean_slang_text(raw.get("meaning_zh")),
        "nuance": clean_slang_text(raw.get("nuance")),
        "confidence": coerce_confidence(raw.get("confidence")),
        "should_add_to_candidates": bool(raw.get("should_add_to_candidates")),
    }


def detect_known_slang_terms(text):
    found = []
    seen = set()
    for rule in KNOWN_SLANG_RULES:
        if not re.search(rule["pattern"], text or ""):
            continue
        key = rule["term"]
        if key in seen:
            continue
        seen.add(key)
        item = {k: v for k, v in rule.items() if k != "pattern"}
        item["should_add_to_candidates"] = True
        found.append(item)
    return found


def merge_slang_terms(ai_terms, supplemental_terms):
    merged = {}
    conservative = {"named_entity", "sensitive"}
    for item in list(ai_terms or []) + list(supplemental_terms or []):
        normalized = normalize_slang_candidate(item)
        if not normalized:
            continue
        key = normalized.get("normalized_term") or normalized["term"]
        existing = merged.get(key)
        if not existing:
            merged[key] = normalized
            continue
        if normalized["category"] in conservative and existing["category"] not in conservative:
            existing["category"] = normalized["category"]
        if normalized["confidence"] > existing["confidence"]:
            existing["confidence"] = normalized["confidence"]
            existing["term"] = normalized["term"] or existing["term"]
            existing["reading_hiragana"] = normalized["reading_hiragana"] or existing["reading_hiragana"]
            existing["part_of_speech"] = normalized["part_of_speech"] or existing["part_of_speech"]
            existing["base_form"] = normalized["base_form"] or existing["base_form"]
        for field in ("meaning_zh", "nuance"):
            if len(normalized.get(field, "")) > len(existing.get(field, "")):
                existing[field] = normalized[field]
        for field in ("normalized_term", "reading_hiragana", "base_form", "part_of_speech"):
            if not existing.get(field) and normalized.get(field):
                existing[field] = normalized[field]
        existing["should_add_to_candidates"] = existing["should_add_to_candidates"] or normalized["should_add_to_candidates"]
    return list(merged.values())


def slang_candidates_for_write(slang_terms):
    candidates = []
    skipped = 0
    for item in slang_terms or []:
        normalized = normalize_slang_candidate(item)
        if normalized and normalized["should_add_to_candidates"]:
            candidates.append(normalized)
        else:
            skipped += 1
    return candidates, skipped


def upsert_slang_candidates(slang_terms, source_context="", source="grammar_analyzer"):
    candidates, skipped = slang_candidates_for_write(slang_terms)
    db_type = "postgres" if DATABASE_URL else "sqlite"
    result = {
        "db_type": db_type,
        "success": 0,
        "failed": 0,
        "skipped": skipped,
        "details": [],
    }
    log_slang(f"upsert_slang_candidates 開始執行；db_type={db_type}；candidates={len(candidates)}；skipped={skipped}")
    if not candidates:
        log_slang("沒有可寫入的候選詞，跳過 upsert。")
        return result

    ensure_slang_candidates_store()
    now = utc_now_iso()
    if DATABASE_URL:
        with get_db_connection() as conn:
            for item in candidates:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1 FROM slang_candidates WHERE term = %s", (item["term"],))
                        exists = cur.fetchone() is not None
                        cur.execute(
                            """
                            INSERT INTO slang_candidates (
                                term, normalized_term, reading_hiragana, base_form, part_of_speech,
                                category, meaning_zh, nuance, example_sentence, source, source_context,
                                frequency_count, confidence, status, first_seen_at, last_seen_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (term) DO UPDATE SET
                                frequency_count = COALESCE(slang_candidates.frequency_count, 0) + 1,
                                last_seen_at = EXCLUDED.last_seen_at,
                                confidence = GREATEST(COALESCE(slang_candidates.confidence, 0), COALESCE(EXCLUDED.confidence, 0)),
                                normalized_term = COALESCE(NULLIF(slang_candidates.normalized_term, ''), EXCLUDED.normalized_term),
                                reading_hiragana = COALESCE(NULLIF(slang_candidates.reading_hiragana, ''), EXCLUDED.reading_hiragana),
                                base_form = COALESCE(NULLIF(slang_candidates.base_form, ''), EXCLUDED.base_form),
                                part_of_speech = COALESCE(NULLIF(slang_candidates.part_of_speech, ''), EXCLUDED.part_of_speech),
                                category = COALESCE(NULLIF(slang_candidates.category, ''), EXCLUDED.category),
                                meaning_zh = COALESCE(NULLIF(slang_candidates.meaning_zh, ''), EXCLUDED.meaning_zh),
                                nuance = COALESCE(NULLIF(slang_candidates.nuance, ''), EXCLUDED.nuance),
                                example_sentence = COALESCE(NULLIF(slang_candidates.example_sentence, ''), EXCLUDED.example_sentence),
                                source = COALESCE(NULLIF(slang_candidates.source, ''), EXCLUDED.source),
                                source_context = COALESCE(NULLIF(slang_candidates.source_context, ''), EXCLUDED.source_context)
                            """,
                            (
                                item["term"],
                                item["normalized_term"],
                                item["reading_hiragana"],
                                item["base_form"],
                                item["part_of_speech"],
                                item["category"],
                                item["meaning_zh"],
                                item["nuance"],
                                source_context,
                                source,
                                source_context,
                                1,
                                item["confidence"],
                                "pending",
                                now,
                                now,
                            ),
                        )
                    conn.commit()
                    action = "updated" if exists else "inserted"
                    result["success"] += 1
                    result["details"].append({"term": item["term"], "result": action})
                    log_slang(f"term={item['term']} result={action}")
                except Exception:
                    conn.rollback()
                    result["failed"] += 1
                    result["details"].append({"term": item.get("term"), "result": "failed"})
                    log_slang_exception(f"term={item.get('term')} 寫入失敗")
        log_slang(f"upsert 完成；success={result['success']}；failed={result['failed']}；skipped={result['skipped']}")
        return result

    with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
        for item in candidates:
            try:
                exists = conn.execute("SELECT 1 FROM slang_candidates WHERE term = ?", (item["term"],)).fetchone() is not None
                conn.execute(
                    """
                    INSERT INTO slang_candidates (
                        term, normalized_term, reading_hiragana, base_form, part_of_speech,
                        category, meaning_zh, nuance, example_sentence, source, source_context,
                        frequency_count, confidence, status, first_seen_at, last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(term) DO UPDATE SET
                        frequency_count = COALESCE(frequency_count, 0) + 1,
                        last_seen_at = excluded.last_seen_at,
                        confidence = MAX(COALESCE(confidence, 0), COALESCE(excluded.confidence, 0)),
                        normalized_term = COALESCE(NULLIF(normalized_term, ''), excluded.normalized_term),
                        reading_hiragana = COALESCE(NULLIF(reading_hiragana, ''), excluded.reading_hiragana),
                        base_form = COALESCE(NULLIF(base_form, ''), excluded.base_form),
                        part_of_speech = COALESCE(NULLIF(part_of_speech, ''), excluded.part_of_speech),
                        category = COALESCE(NULLIF(category, ''), excluded.category),
                        meaning_zh = COALESCE(NULLIF(meaning_zh, ''), excluded.meaning_zh),
                        nuance = COALESCE(NULLIF(nuance, ''), excluded.nuance),
                        example_sentence = COALESCE(NULLIF(example_sentence, ''), excluded.example_sentence),
                        source = COALESCE(NULLIF(source, ''), excluded.source),
                        source_context = COALESCE(NULLIF(source_context, ''), excluded.source_context)
                    """,
                    (
                        item["term"],
                        item["normalized_term"],
                        item["reading_hiragana"],
                        item["base_form"],
                        item["part_of_speech"],
                        item["category"],
                        item["meaning_zh"],
                        item["nuance"],
                        source_context,
                        source,
                        source_context,
                        1,
                        item["confidence"],
                        "pending",
                        now,
                        now,
                    ),
                )
                conn.commit()
                action = "updated" if exists else "inserted"
                result["success"] += 1
                result["details"].append({"term": item["term"], "result": action})
                log_slang(f"term={item['term']} result={action}")
            except Exception:
                conn.rollback()
                result["failed"] += 1
                result["details"].append({"term": item.get("term"), "result": "failed"})
                log_slang_exception(f"term={item.get('term')} 寫入失敗")
    log_slang(f"upsert 完成；success={result['success']}；failed={result['failed']}；skipped={result['skipped']}")
    return result


def enqueue_slang_candidates(slang_terms, source_context="", source="grammar_analyzer"):
    total = len(slang_terms or [])
    candidates, skipped = slang_candidates_for_write(slang_terms)
    mode = slang_candidate_write_mode()
    log_slang(
        f"enqueue_slang_candidates 被呼叫；slang_terms_total={total}；"
        f"should_add_to_candidates={len(candidates)}；skipped={skipped}；write_mode={mode}"
    )
    if not candidates:
        return {"mode": mode, "queued": False, "success": 0, "failed": 0, "skipped": skipped}

    if mode == "sync":
        try:
            log_slang("寫入模式是 sync，將在 API 回傳前執行 upsert。")
            result = upsert_slang_candidates(candidates, source_context=source_context, source=source)
            result["skipped"] = result.get("skipped", 0) + skipped
            result["mode"] = "sync"
            result["queued"] = False
            return result
        except Exception:
            log_slang_exception("sync 寫入發生未預期錯誤")
            return {"mode": "sync", "queued": False, "success": 0, "failed": len(candidates), "skipped": skipped}

    thread_terms = json.loads(json.dumps(candidates, ensure_ascii=False))
    thread_context = str(source_context or "")
    thread_source = str(source or "grammar_analyzer")

    def worker():
        try:
            with app.app_context():
                log_slang("寫入模式是 async，背景 Thread 開始 upsert。")
                upsert_slang_candidates(thread_terms, source_context=thread_context, source=thread_source)
        except Exception:
            log_slang_exception("async 背景 Thread 寫入失敗")

    threading.Thread(target=worker, name="slang-candidates-upsert", daemon=True).start()
    return {"mode": "async", "queued": True, "success": 0, "failed": 0, "skipped": skipped}


def query_slang_candidates(status="pending", limit=5):
    status = normalize_slang_status(status)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 100))
    ensure_slang_candidates_store()
    columns = """
        id, term, normalized_term, reading_hiragana, category, meaning_zh, nuance,
        frequency_count, confidence, example_sentence, status, first_seen_at, last_seen_at
    """
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {columns}
                    FROM slang_candidates
                    WHERE status = %s
                    ORDER BY frequency_count DESC, confidence DESC, last_seen_at DESC
                    LIMIT %s
                    """,
                    (status, limit),
                )
                rows = cur.fetchall()
        keys = [item.strip() for item in columns.replace("\n", "").split(",")]
        return [dict(zip(keys, row)) for row in rows]

    return sqlite_dicts(
        f"""
        SELECT {columns}
        FROM slang_candidates
        WHERE status = ?
        ORDER BY frequency_count DESC, confidence DESC, last_seen_at DESC
        LIMIT ?
        """,
        (status, limit),
    )


def update_slang_candidate_status(candidate_id, action):
    action = normalize_slang_status(action)
    if action not in {"approved", "rejected"}:
        raise ValueError("審核動作不正確。")
    try:
        candidate_id = int(candidate_id)
    except (TypeError, ValueError):
        raise ValueError("候選詞 ID 不正確。")
    now = utc_now_iso()
    ensure_slang_candidates_store()
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE slang_candidates SET status = %s, reviewed_at = %s WHERE id = %s",
                    (action, now, candidate_id),
                )
                updated = cur.rowcount
            conn.commit()
        return updated

    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        cur = conn.execute(
            "UPDATE slang_candidates SET status = ?, reviewed_at = ? WHERE id = ?",
            (action, now, candidate_id),
        )
        conn.commit()
        return cur.rowcount


def slang_debug_recent_snapshot(limit=20):
    ensure_slang_candidates_store()
    try:
        limit = int(limit or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))
    empty_counts = {"total_count": 0, "pending_count": 0, "approved_count": 0, "rejected_count": 0}
    columns = """
        id, term, category, status, frequency_count, confidence,
        first_seen_at, last_seen_at
    """
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM slang_candidates")
                total = cur.fetchone()[0]
                cur.execute("SELECT status, COUNT(*) FROM slang_candidates GROUP BY status")
                status_counts = {row[0] or "pending": row[1] for row in cur.fetchall()}
                cur.execute(
                    f"""
                    SELECT {columns}
                    FROM slang_candidates
                    ORDER BY last_seen_at DESC NULLS LAST, id DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
        keys = [item.strip() for item in columns.replace("\n", "").split(",")]
        recent_items = [dict(zip(keys, row)) for row in rows]
    else:
        total = sqlite_one("SELECT COUNT(*) AS count FROM slang_candidates")["count"]
        status_rows = sqlite_dicts("SELECT status, COUNT(*) AS count FROM slang_candidates GROUP BY status")
        status_counts = {row["status"] or "pending": row["count"] for row in status_rows}
        recent_items = sqlite_dicts(
            f"""
            SELECT {columns}
            FROM slang_candidates
            ORDER BY last_seen_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
    payload = dict(empty_counts)
    payload.update(
        {
            "total_count": int(total or 0),
            "pending_count": int(status_counts.get("pending", 0) or 0),
            "approved_count": int(status_counts.get("approved", 0) or 0),
            "rejected_count": int(status_counts.get("rejected", 0) or 0),
            "recent_items": recent_items,
        }
    )
    return payload


def approved_slang_for_material(limit):
    if limit <= 0:
        return []
    ensure_slang_candidates_store()
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, term, reading_hiragana, meaning_zh, category
                    FROM slang_candidates
                    WHERE status = 'approved'
                      AND category IN ('slang', 'internet_slang', 'otaku_culture')
                    ORDER BY COALESCE(last_used_at, '') ASC, frequency_count DESC, confidence DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
        keys = ["id", "term", "reading_hiragana", "meaning_zh", "category"]
        return [dict(zip(keys, row)) for row in rows]

    return sqlite_dicts(
        """
        SELECT id, term, reading_hiragana, meaning_zh, category
        FROM slang_candidates
        WHERE status = 'approved'
          AND category IN ('slang', 'internet_slang', 'otaku_culture')
        ORDER BY COALESCE(last_used_at, '') ASC, frequency_count DESC, confidence DESC
        LIMIT ?
        """,
        (limit,),
    )


def mark_slang_used_in_material(items):
    ids = [int(item["id"]) for item in items if item.get("id")]
    if not ids:
        return
    now = utc_now_iso()
    if DATABASE_URL:
        placeholders = ", ".join(["%s"] * len(ids))
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE slang_candidates
                    SET used_in_material_count = COALESCE(used_in_material_count, 0) + 1,
                        last_used_at = %s,
                        last_seen_at = %s
                    WHERE id IN ({placeholders})
                    """,
                    (now, now, *ids),
                )
            conn.commit()
        return

    ensure_slang_candidates_store()
    placeholders = ", ".join(["?"] * len(ids))
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        conn.execute(
            f"""
            UPDATE slang_candidates
            SET used_in_material_count = COALESCE(used_in_material_count, 0) + 1,
                last_used_at = ?,
                last_seen_at = ?
            WHERE id IN ({placeholders})
            """,
            (now, now, *ids),
        )
        conn.commit()


def load_settings():
    ensure_settings_store()
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return normalize_settings(dict(rows))


def save_settings_file(settings):
    current = normalize_settings(load_settings() | normalize_settings(settings))
    ensure_settings_store()
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        conn.executemany(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            list(current.items()),
        )
        conn.commit()
    invalidate_dashboard_cache("mistake retry")
    return current


def get_db_connection():
    import psycopg

    return psycopg.connect(DATABASE_URL)


def migrate_slang_candidates_postgres():
    if not DATABASE_URL:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS slang_candidates (
                    id BIGSERIAL PRIMARY KEY,
                    term TEXT NOT NULL UNIQUE,
                    normalized_term TEXT,
                    reading_hiragana TEXT,
                    base_form TEXT,
                    part_of_speech TEXT,
                    category TEXT,
                    meaning_zh TEXT,
                    nuance TEXT,
                    example_sentence TEXT,
                    source TEXT,
                    source_context TEXT,
                    frequency_count INTEGER DEFAULT 1,
                    confidence REAL,
                    status TEXT DEFAULT 'pending',
                    review_note TEXT,
                    first_seen_at TEXT,
                    last_seen_at TEXT,
                    reviewed_at TEXT,
                    used_in_material_count INTEGER DEFAULT 0,
                    last_used_at TEXT
                )
                """
            )
            postgres_columns = {
                "normalized_term": "TEXT",
                "reading_hiragana": "TEXT",
                "base_form": "TEXT",
                "part_of_speech": "TEXT",
                "category": "TEXT",
                "meaning_zh": "TEXT",
                "nuance": "TEXT",
                "example_sentence": "TEXT",
                "source": "TEXT",
                "source_context": "TEXT",
                "frequency_count": "INTEGER DEFAULT 1",
                "confidence": "REAL",
                "status": "TEXT DEFAULT 'pending'",
                "review_note": "TEXT",
                "first_seen_at": "TEXT",
                "last_seen_at": "TEXT",
                "reviewed_at": "TEXT",
                "used_in_material_count": "INTEGER DEFAULT 0",
                "last_used_at": "TEXT",
            }
            for column, col_type in postgres_columns.items():
                cur.execute(f"ALTER TABLE slang_candidates ADD COLUMN IF NOT EXISTS {column} {col_type}")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_slang_candidates_term_unique ON slang_candidates(term)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_slang_candidates_status ON slang_candidates(status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_slang_candidates_category ON slang_candidates(category)")
            now = utc_now_iso()
            cur.execute(
                """
                UPDATE slang_candidates
                SET status = COALESCE(NULLIF(status, ''), 'pending'),
                    category = COALESCE(NULLIF(category, ''), 'unknown'),
                    frequency_count = COALESCE(frequency_count, 1),
                    first_seen_at = COALESCE(NULLIF(first_seen_at, ''), %s),
                    last_seen_at = COALESCE(NULLIF(last_seen_at, ''), %s),
                    used_in_material_count = COALESCE(used_in_material_count, 0)
                """,
                (now, now),
            )
        conn.commit()


def migrate_vocabulary_pool_postgres():
    if not DATABASE_URL:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS vocabulary_pool (
                    id BIGSERIAL PRIMARY KEY,
                    surface TEXT NOT NULL,
                    base_form TEXT NOT NULL,
                    reading_hiragana TEXT DEFAULT '',
                    meaning_zh TEXT DEFAULT '',
                    part_of_speech TEXT DEFAULT '',
                    jlpt_level TEXT DEFAULT '',
                    example_sentence TEXT DEFAULT '',
                    example_translation_zh TEXT DEFAULT '',
                    source TEXT DEFAULT 'manual',
                    priority INTEGER DEFAULT 1,
                    is_active BOOLEAN DEFAULT TRUE,
                    used_in_material_count INTEGER DEFAULT 0,
                    last_used_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ
                )
                """
            )
            columns = {
                "surface": "TEXT",
                "base_form": "TEXT",
                "reading_hiragana": "TEXT DEFAULT ''",
                "meaning_zh": "TEXT DEFAULT ''",
                "part_of_speech": "TEXT DEFAULT ''",
                "jlpt_level": "TEXT DEFAULT ''",
                "example_sentence": "TEXT DEFAULT ''",
                "example_translation_zh": "TEXT DEFAULT ''",
                "source": "TEXT DEFAULT 'manual'",
                "priority": "INTEGER DEFAULT 1",
                "is_active": "BOOLEAN DEFAULT TRUE",
                "used_in_material_count": "INTEGER DEFAULT 0",
                "last_used_at": "TIMESTAMPTZ",
                "created_at": "TIMESTAMPTZ",
                "updated_at": "TIMESTAMPTZ",
            }
            for column, col_type in columns.items():
                cur.execute(f"ALTER TABLE vocabulary_pool ADD COLUMN IF NOT EXISTS {column} {col_type}")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_base_level ON vocabulary_pool(base_form, jlpt_level)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_level ON vocabulary_pool(jlpt_level)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_active ON vocabulary_pool(is_active)")
            now = utc_now_iso()
            cur.execute(
                """
                UPDATE vocabulary_pool
                SET surface = COALESCE(NULLIF(surface, ''), base_form),
                    base_form = COALESCE(NULLIF(base_form, ''), surface),
                    source = COALESCE(NULLIF(source, ''), 'manual'),
                    priority = COALESCE(priority, 1),
                    is_active = COALESCE(is_active, TRUE),
                    used_in_material_count = COALESCE(used_in_material_count, 0),
                    created_at = COALESCE(created_at, %s),
                    updated_at = COALESCE(updated_at, %s)
                """,
                (now, now),
            )
        conn.commit()


def ensure_slang_candidates_store():
    if DATABASE_URL:
        migrate_slang_candidates_postgres()
    else:
        ensure_settings_store()


def ensure_vocabulary_pool_store():
    if DATABASE_URL:
        migrate_vocabulary_pool_postgres()
    else:
        ensure_settings_store()


def ensure_database():
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS materials (
                        id BIGSERIAL PRIMARY KEY,
                        date TEXT NOT NULL,
                        target_level TEXT DEFAULT '',
                        vocab_word TEXT DEFAULT '',
                        vocab_reading TEXT DEFAULT '',
                        vocab_meaning TEXT DEFAULT '',
                        verb_base TEXT DEFAULT '',
                        verb_masu_stem TEXT DEFAULT '',
                        verb_te TEXT DEFAULT '',
                        verb_ta TEXT DEFAULT '',
                        verb_nai TEXT DEFAULT '',
                        verb_ba TEXT DEFAULT '',
                        verb_causative TEXT DEFAULT '',
                        verb_passive TEXT DEFAULT '',
                        verb_causative_passive TEXT DEFAULT '',
                        grammar_title TEXT DEFAULT '',
                        grammar_exp TEXT DEFAULT '',
                        grammar_examples TEXT DEFAULT '',
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                for col in COLUMNS:
                    if col not in ("date",):
                        cur.execute(f"ALTER TABLE materials ADD COLUMN IF NOT EXISTS {col} TEXT DEFAULT ''")
            conn.commit()
        migrate_slang_candidates_postgres()
        migrate_vocabulary_pool_postgres()
        return

    if not os.path.exists(DATABASE_FILE):
        pd.DataFrame(columns=COLUMNS).to_csv(DATABASE_FILE, index=False, encoding="utf-8-sig")


def read_database():
    ensure_database()
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {', '.join(COLUMNS)} FROM materials ORDER BY id")
                rows = cur.fetchall()
        return pd.DataFrame(rows, columns=COLUMNS).astype(str) if rows else pd.DataFrame(columns=COLUMNS)

    df = pd.read_csv(DATABASE_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[COLUMNS]


def parse_json_from_ai(text):
    cleaned = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("AI 沒有回傳可讀取的 JSON 內容。")
    return json.loads(cleaned[start : end + 1])


def list_gemini_models():
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data.get("models", [])


def gemini_model_candidates():
    candidates = []
    for value in [GEMINI_MODEL, *GEMINI_MODEL_CANDIDATES.split(",")]:
        model = str(value or "").strip()
        if model and model not in candidates:
            candidates.append(model)
    return candidates or ["gemini-3-flash-preview"]


def choose_gemini_model():
    return gemini_model_candidates()[0]


def gemini_smoke_test_enabled():
    return os.environ.get("GEMINI_ENABLE_MODEL_SMOKE_TEST", "false").strip().lower() == "true"


def compact_gemini_error_detail(raw_detail):
    raw_detail = str(raw_detail or "").strip()
    try:
        parsed = json.loads(raw_detail)
    except json.JSONDecodeError:
        return {"raw": re.sub(r"\s+", " ", raw_detail)[:800]}
    error = parsed.get("error") if isinstance(parsed, dict) else {}
    if not isinstance(error, dict):
        return {"raw": re.sub(r"\s+", " ", raw_detail)[:800]}
    return {
        "code": error.get("code"),
        "status": error.get("status"),
        "message": str(error.get("message", ""))[:800],
    }


def classify_gemini_error(error):
    text = str(error or "")
    lower = text.lower()
    if "timeout" in lower or "timed out" in lower or "逾時" in text:
        return "timeout"
    if "unavailable" in lower or "503" in lower or "high demand" in lower:
        return "service_unavailable"
    if "not_found" in lower or "not found" in lower or "404" in lower:
        return "not_found"
    if "quota" in lower or "resource_exhausted" in lower or "429" in lower:
        return "quota_exceeded"
    if "permission" in lower or "unauthorized" in lower or "api key" in lower or "403" in lower:
        return "permission_or_api_key"
    if "json" in lower:
        return "json_parse_error"
    if "格式" in text or "空內容" in text:
        return "invalid_response"
    if "連接" in text or "connection" in lower:
        return "connection_error"
    return "error"


def call_gemini(prompt, model_name=None, timeout_seconds=None):
    if not GEMINI_API_KEY:
        raise RuntimeError("尚未設定 Gemini API Key。")

    model_name = model_name or choose_gemini_model()
    timeout_seconds = timeout_seconds or GEMINI_TIMEOUT_SECONDS
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_name}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        compact_detail = json.dumps(compact_gemini_error_detail(detail), ensure_ascii=False)
        raise RuntimeError(
            f"AI 服務請求失敗；model={model_name}；http_status={e.code}；detail={compact_detail}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"無法連接 AI 服務；model={model_name}；reason={e.reason}") from e
    except TimeoutError as e:
        raise RuntimeError(f"AI 服務逾時；model={model_name}；timeout={timeout_seconds}s") from e

    if "error" in data:
        compact_detail = json.dumps(compact_gemini_error_detail(json.dumps(data, ensure_ascii=False)), ensure_ascii=False)
        raise RuntimeError(f"AI 服務回傳錯誤；model={model_name}；detail={compact_detail}")

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        if not str(text or "").strip():
            raise RuntimeError("AI 回傳空內容。")
        return text
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError("AI 回傳格式不正確。") from e


def smoke_test_gemini_model(model_name):
    prompt = '請只回傳純 JSON: {"ok": true}'
    started = time.perf_counter()
    try:
        raw_text = call_gemini(prompt, model_name=model_name, timeout_seconds=GEMINI_TIMEOUT_SECONDS)
        parsed = parse_gemini_json_safely(raw_text)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if parsed.get("ok") is True:
            return {
                "model": model_name,
                "status": "ok",
                "elapsed_ms": elapsed_ms,
                "error_type": "",
                "error_message": "",
            }
        return {
            "model": model_name,
            "status": "error",
            "elapsed_ms": elapsed_ms,
            "error_type": "invalid_response",
            "error_message": "模型有回應，但不是 {\"ok\": true}。",
        }
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        error_type = classify_gemini_error(e)
        return {
            "model": model_name,
            "status": "timeout" if error_type == "timeout" else "error",
            "elapsed_ms": elapsed_ms,
            "error_type": error_type,
            "error_message": str(e)[:500],
        }


def build_prompt(settings):
    level = settings["target_level"]
    topics = ["日常生活", "學校學習", "工作會話", "旅行交通", "購物點餐", "天氣季節", "人際關係", "新聞社會", "抽象議題"]
    topic = random.choice(topics)
    seed = random.randint(100000, 999999)

    return f"""
你是一位專業日語老師。請為繁體中文母語學習者生成一份 JLPT {level} 難度的日語教材。

主題：{topic}
隨機編號：{seed}

請嚴格只輸出 JSON，不要輸出 Markdown，不要解釋。所有中文說明必須使用繁體中文。
難度必須符合 {level}，不要混入過高或過低級別的內容。

JSON 格式：
{{
  "vocab": [
    {{"word": "日語單字", "reading": "假名讀音", "meaning": "繁體中文意思"}}
  ],
  "verbs": [
    {{
      "base": "辭書形（假名） - 繁體中文意思",
      "masuStem": "連用形，也就是ます形去掉ます後的形態（假名）",
      "te": "て形（假名）",
      "ta": "た形（假名）",
      "nai": "ない形（假名）",
      "ba": "ば形（假名）",
      "causative": "使役形（假名）",
      "passive": "被動形（假名）",
      "causativePassive": "使役被動形（假名）"
    }}
  ],
  "grammar": {{
    "title": "文法標題",
    "exp": "繁體中文說明",
    "examples": [
      {{"jp": "日文例句", "cn": "繁體中文翻譯"}}
    ]
  }}
}}

請嚴格生成剛好 {settings["vocab_count"]} 個單字、剛好 {settings["verb_count"]} 個動詞、1 個文法點，並至少給 2 個例句。
""".strip()


def sample_material(settings=None):
    settings = normalize_settings(settings or load_settings())
    vocab_count = int(settings["vocab_count"])
    verb_count = int(settings["verb_count"])
    vocab = [
        {"word": "予定", "reading": "よてい", "meaning": "計畫；預定"},
        {"word": "準備", "reading": "じゅんび", "meaning": "準備"},
        {"word": "確認", "reading": "かくにん", "meaning": "確認"},
        {"word": "資料", "reading": "しりょう", "meaning": "資料"},
        {"word": "進捗", "reading": "しんちょく", "meaning": "進度"},
        {"word": "提案", "reading": "ていあん", "meaning": "提案"},
    ]
    verbs = [
        {
            "base": "決める（きめる） - 決定",
            "masuStem": "決め（きめ）",
            "te": "決めて（きめて）",
            "ta": "決めた（きめた）",
            "nai": "決めない（きめない）",
            "ba": "決めれば（きめれば）",
            "causative": "決めさせる（きめさせる）",
            "passive": "決められる（きめられる）",
            "causativePassive": "決めさせられる（きめさせられる）",
        },
        {
            "base": "確認する（かくにんする） - 確認",
            "masuStem": "確認し（かくにんし）",
            "te": "確認して（かくにんして）",
            "ta": "確認した（かくにんした）",
            "nai": "確認しない（かくにんしない）",
            "ba": "確認すれば（かくにんすれば）",
            "causative": "確認させる（かくにんさせる）",
            "passive": "確認される（かくにんされる）",
            "causativePassive": "確認させられる（かくにんさせられる）",
        },
    ]
    return {
        "vocab": vocab[:vocab_count],
        "verbs": verbs[:verb_count],
        "grammar": {
            "title": "〜ようにする",
            "exp": "表示努力養成某個習慣，或盡量做到某件事。",
            "examples": [
                {"jp": "毎日日本語を聞くようにしています。", "cn": "我盡量每天聽日語。"},
                {"jp": "忘れないようにメモしてください。", "cn": "請做筆記，以免忘記。"},
            ],
        },
    }


def merge_approved_slang_into_material(material, settings):
    material = material if isinstance(material, dict) else {}
    total_quota_base = int(settings.get("vocab_count", 0)) + int(settings.get("verb_count", 0))
    slang_quota = int(total_quota_base * 0.1)
    if slang_quota <= 0:
        return material

    approved = approved_slang_for_material(slang_quota)
    if not approved:
        return material

    vocab_list = list(material.get("vocab") or [])
    existing_terms = {str(item.get("word", "")) for item in vocab_list if isinstance(item, dict)}
    selected = [item for item in approved if item.get("term") not in existing_terms]
    if not selected:
        return material

    target_vocab_count = int(settings.get("vocab_count", len(vocab_list)) or len(vocab_list))
    slang_vocab = [
        {
            "word": item.get("term", ""),
            "reading": item.get("reading_hiragana", ""),
            "meaning": item.get("meaning_zh", "") or "已審核的新詞",
        }
        for item in selected[:slang_quota]
        if item.get("term")
    ]
    if not slang_vocab:
        return material

    available_slots = max(0, target_vocab_count - len(vocab_list))
    if available_slots:
        vocab_list.extend(slang_vocab[:available_slots])
        remaining = slang_vocab[available_slots:]
    else:
        remaining = slang_vocab
    if remaining and vocab_list:
        replace_count = min(len(remaining), len(vocab_list), slang_quota)
        vocab_list[-replace_count:] = remaining[:replace_count]

    material["vocab"] = vocab_list[:target_vocab_count] if target_vocab_count else vocab_list
    mark_slang_used_in_material(selected[: len(slang_vocab)])
    return material


def material_vocab_from_existing(settings, limit):
    if limit <= 0:
        return []
    target = settings.get("target_level", "")
    recent_cutoff = taipei_now().date() - timedelta(days=7)
    df = read_database()
    if df.empty:
        return []
    items = []
    seen = set()
    for _, row in df.sample(frac=1).iterrows():
        word = str(row.get("vocab_word", "")).strip()
        if not word or word in seen:
            continue
        row_date = parse_material_date(row.get("date", ""))
        if row_date and row_date >= recent_cutoff:
            continue
        row_level = str(row.get("target_level", "")).strip()
        if target and row_level and row_level != target:
            continue
        seen.add(word)
        items.append(
            {
                "word": word,
                "reading": str(row.get("vocab_reading", "")).strip(),
                "meaning": str(row.get("vocab_meaning", "")).strip(),
                "part_of_speech": "",
                "jlpt_level": row_level,
                "source": "materials",
            }
        )
        if len(items) >= limit:
            break
    return items


def material_vocab_from_approved_slang(limit):
    if limit <= 0:
        return []
    selected = approved_slang_for_material(limit)
    items = [
        {
            "word": item.get("term", ""),
            "reading": item.get("reading_hiragana", ""),
            "meaning": item.get("meaning_zh", "") or "已審核的新詞",
            "part_of_speech": "SNS語彙",
            "jlpt_level": item.get("category", ""),
            "source": "slang_candidates",
            "_slang_id": item.get("id"),
        }
        for item in selected
        if item.get("term")
    ]
    mark_slang_used_in_material(selected[: len(items)])
    return items


def first_text(row, names):
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_vocabulary_item(raw):
    raw = dict(raw or {})
    surface = first_text(raw, ["surface", "term", "word", "vocab_word", "base_form"])
    base_form = first_text(raw, ["base_form", "dictionary_form", "surface", "term", "word", "vocab_word"]) or surface
    if not surface or not base_form:
        return None
    try:
        priority = int(raw.get("priority", 1) or 1)
    except (TypeError, ValueError):
        priority = 1
    is_active = raw.get("is_active", True)
    if isinstance(is_active, str):
        is_active = is_active.strip().lower() not in {"0", "false", "no", "off"}
    now = utc_now_iso()
    return clean_db_payload(
        {
            "surface": surface,
            "base_form": base_form,
            "reading_hiragana": first_text(raw, ["reading_hiragana", "reading", "kana"]),
            "meaning_zh": first_text(raw, ["meaning_zh", "meaning_zh_tw", "meaning", "vocab_meaning"]),
            "part_of_speech": first_text(raw, ["part_of_speech", "pos"]),
            "jlpt_level": first_text(raw, ["jlpt_level", "target_level", "level"]),
            "example_sentence": first_text(raw, ["example_sentence", "example_japanese"]),
            "example_translation_zh": first_text(raw, ["example_translation_zh", "example_chinese", "example_translation"]),
            "source": first_text(raw, ["source"]) or "seed_basic",
            "priority": priority,
            "is_active": bool(is_active),
            "used_in_material_count": int(raw.get("used_in_material_count", 0) or 0),
            "last_used_at": clean_timestamp(raw.get("last_used_at")),
            "created_at": clean_timestamp(raw.get("created_at")) or now,
            "updated_at": now,
        }
    )


def upsert_vocabulary_pool(items):
    normalized_items = [item for item in (normalize_vocabulary_item(raw) for raw in items or []) if item]
    result = {"success": 0, "failed": 0, "skipped": 0, "total": len(normalized_items)}
    if not normalized_items:
        return result
    ensure_vocabulary_pool_store()
    if DATABASE_URL:
        with get_db_connection() as conn:
            for item in normalized_items:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT id FROM vocabulary_pool WHERE base_form = %s AND jlpt_level = %s LIMIT 1",
                            (item["base_form"], item["jlpt_level"]),
                        )
                        existing = cur.fetchone()
                        if existing:
                            cur.execute(
                                """
                                UPDATE vocabulary_pool
                                SET surface = COALESCE(NULLIF(surface, ''), %s),
                                    reading_hiragana = COALESCE(NULLIF(reading_hiragana, ''), %s),
                                    meaning_zh = COALESCE(NULLIF(meaning_zh, ''), %s),
                                    part_of_speech = COALESCE(NULLIF(part_of_speech, ''), %s),
                                    example_sentence = COALESCE(NULLIF(example_sentence, ''), %s),
                                    example_translation_zh = COALESCE(NULLIF(example_translation_zh, ''), %s),
                                    source = COALESCE(NULLIF(source, ''), %s),
                                    priority = GREATEST(COALESCE(priority, 1), %s),
                                    is_active = COALESCE(is_active, %s),
                                    used_in_material_count = COALESCE(used_in_material_count, 0),
                                    updated_at = %s
                                WHERE id = %s
                                """,
                                (
                                    item["surface"],
                                    item["reading_hiragana"],
                                    item["meaning_zh"],
                                    item["part_of_speech"],
                                    item["example_sentence"],
                                    item["example_translation_zh"],
                                    item["source"],
                                    item["priority"],
                                    item["is_active"],
                                    item["updated_at"],
                                    existing[0],
                                ),
                            )
                        else:
                            cur.execute(
                                """
                                INSERT INTO vocabulary_pool (
                                    surface, base_form, reading_hiragana, meaning_zh, part_of_speech,
                                    jlpt_level, example_sentence, example_translation_zh, source, priority,
                                    is_active, used_in_material_count, last_used_at, created_at, updated_at
                                )
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    item["surface"],
                                    item["base_form"],
                                    item["reading_hiragana"],
                                    item["meaning_zh"],
                                    item["part_of_speech"],
                                    item["jlpt_level"],
                                    item["example_sentence"],
                                    item["example_translation_zh"],
                                    item["source"],
                                    item["priority"],
                                    item["is_active"],
                                    item["used_in_material_count"],
                                    item["last_used_at"],
                                    item["created_at"],
                                    item["updated_at"],
                                ),
                            )
                    conn.commit()
                    result["success"] += 1
                except Exception:
                    conn.rollback()
                    result["failed"] += 1
                    print(f"[vocabulary-pool] upsert failed surface={item.get('surface')}")
                    print(traceback.format_exc())
        return result

    with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
        for item in normalized_items:
            try:
                existing = conn.execute(
                    "SELECT id FROM vocabulary_pool WHERE base_form = ? AND jlpt_level = ? LIMIT 1",
                    (item["base_form"], item["jlpt_level"]),
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE vocabulary_pool
                        SET surface = COALESCE(NULLIF(surface, ''), ?),
                            reading_hiragana = COALESCE(NULLIF(reading_hiragana, ''), ?),
                            meaning_zh = COALESCE(NULLIF(meaning_zh, ''), ?),
                            part_of_speech = COALESCE(NULLIF(part_of_speech, ''), ?),
                            example_sentence = COALESCE(NULLIF(example_sentence, ''), ?),
                            example_translation_zh = COALESCE(NULLIF(example_translation_zh, ''), ?),
                            source = COALESCE(NULLIF(source, ''), ?),
                            priority = MAX(COALESCE(priority, 1), ?),
                            is_active = COALESCE(is_active, ?),
                            used_in_material_count = COALESCE(used_in_material_count, 0),
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            item["surface"],
                            item["reading_hiragana"],
                            item["meaning_zh"],
                            item["part_of_speech"],
                            item["example_sentence"],
                            item["example_translation_zh"],
                            item["source"],
                            item["priority"],
                            1 if item["is_active"] else 0,
                            item["updated_at"],
                            existing[0],
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO vocabulary_pool (
                            surface, base_form, reading_hiragana, meaning_zh, part_of_speech,
                            jlpt_level, example_sentence, example_translation_zh, source, priority,
                            is_active, used_in_material_count, last_used_at, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item["surface"],
                            item["base_form"],
                            item["reading_hiragana"],
                            item["meaning_zh"],
                            item["part_of_speech"],
                            item["jlpt_level"],
                            item["example_sentence"],
                            item["example_translation_zh"],
                            item["source"],
                            item["priority"],
                            1 if item["is_active"] else 0,
                            item["used_in_material_count"],
                            item["last_used_at"],
                            item["created_at"],
                            item["updated_at"],
                        ),
                    )
                result["success"] += 1
            except Exception:
                result["failed"] += 1
                print(f"[vocabulary-pool] upsert failed surface={item.get('surface')}")
                print(traceback.format_exc())
        conn.commit()
    return result


def parse_loose_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    for parser in (
        lambda raw: datetime.fromisoformat(raw.replace("Z", "+00:00")).date(),
        lambda raw: datetime.strptime(raw, "%Y/%m/%d").date(),
        lambda raw: datetime.strptime(raw, "%Y-%m-%d").date(),
    ):
        try:
            return parser(text)
        except ValueError:
            continue
    return None


def fetch_vocabulary_pool_rows():
    ensure_vocabulary_pool_store()
    if DATABASE_URL:
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT *
                        FROM vocabulary_pool
                        WHERE COALESCE(is_active, TRUE) = TRUE
                          AND COALESCE(NULLIF(meaning_zh, ''), '') <> ''
                          AND COALESCE(NULLIF(reading_hiragana, ''), '') <> ''
                        ORDER BY COALESCE(used_in_material_count, 0) ASC,
                                 last_used_at ASC NULLS FIRST,
                                 priority DESC,
                                 id DESC
                        LIMIT 500
                        """
                    )
                    columns = [desc[0] for desc in cur.description]
                    return [dict(zip(columns, row)) for row in cur.fetchall()]
        except Exception as e:
            print(f"[material-generator] vocabulary_pool unavailable; db=postgres; error={e}")
            return []
    try:
        ensure_settings_store()
        with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM vocabulary_pool
                WHERE COALESCE(is_active, 1) = 1
                  AND COALESCE(NULLIF(meaning_zh, ''), '') <> ''
                  AND COALESCE(NULLIF(reading_hiragana, ''), '') <> ''
                ORDER BY COALESCE(used_in_material_count, 0) ASC,
                         COALESCE(last_used_at, '') ASC,
                         priority DESC,
                         id DESC
                LIMIT 500
                """
            ).fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error as e:
        print(f"[material-generator] vocabulary_pool unavailable; db=sqlite; error={e}")
        return []


def mark_vocabulary_pool_used(items):
    ids = [item.get("_pool_id") for item in items if item.get("_pool_id")]
    if not ids:
        return
    has_last_seen = any(item.get("_pool_has_last_seen") for item in items)
    has_last_used = any(item.get("_pool_has_last_used") for item in items)
    has_used_count = any(item.get("_pool_has_used_count") for item in items)
    updates = []
    params = []
    if has_last_seen:
        updates.append("last_seen_at = %s" if DATABASE_URL else "last_seen_at = ?")
        params.append(utc_now_iso())
    if has_last_used:
        updates.append("last_used_at = %s" if DATABASE_URL else "last_used_at = ?")
        params.append(utc_now_iso())
    if has_used_count:
        updates.append("used_in_material_count = COALESCE(used_in_material_count, 0) + 1")
    if not updates:
        return
    placeholder = "%s" if DATABASE_URL else "?"
    id_placeholders = ", ".join([placeholder] * len(ids))
    sql = f"UPDATE vocabulary_pool SET {', '.join(updates)} WHERE id IN ({id_placeholders})"
    try:
        if DATABASE_URL:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params + ids)
                conn.commit()
            return
        with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
            conn.execute(sql, params + ids)
            conn.commit()
    except Exception as e:
        print(f"[material-generator] vocabulary_pool mark used failed; error={e}")


def material_vocab_from_vocabulary_pool(settings, limit):
    if limit <= 0:
        return []
    rows = fetch_vocabulary_pool_rows()
    if not rows:
        return []

    target = settings.get("target_level", "")
    recent_cutoff = taipei_now().date() - timedelta(days=7)
    target_items = []
    backup_items = []
    seen = set()
    for row in rows:
        is_active = first_text(row, ["is_active", "active", "enabled"])
        if is_active and is_active.lower() in {"0", "false", "no", "off"}:
            continue
        word = first_text(row, ["term", "surface", "word", "vocab_word", "dictionary_form"])
        if not word or word in seen:
            continue
        last_seen = parse_loose_date(first_text(row, ["last_seen_at", "last_used_at"]))
        if last_seen and last_seen >= recent_cutoff:
            continue
        seen.add(word)
        row_level = first_text(row, ["jlpt_level", "target_level", "level"])
        priority = first_text(row, ["priority", "weight"])
        try:
            priority_value = int(float(priority)) if priority else 0
        except ValueError:
            priority_value = 0
        next_review = parse_loose_date(first_text(row, ["next_review_at", "next_review_date", "review_at"]))
        due_rank = 0 if not next_review or next_review <= taipei_now().date() else 1
        item = {
            "word": word,
            "reading": first_text(row, ["reading_hiragana", "reading", "kana", "vocab_reading"]),
            "meaning": first_text(row, ["meaning_zh", "meaning_zh_tw", "meaning", "vocab_meaning"]),
            "part_of_speech": first_text(row, ["part_of_speech", "pos"]),
            "jlpt_level": row_level,
            "example_sentence": first_text(row, ["example_sentence", "example_japanese"]),
            "example_translation_zh": first_text(row, ["example_translation_zh", "example_chinese", "example_translation"]),
            "source": "vocabulary_pool",
            "_pool_id": row.get("id"),
            "_pool_has_last_seen": "last_seen_at" in row,
            "_pool_has_last_used": "last_used_at" in row,
            "_pool_has_used_count": "used_in_material_count" in row,
            "_sort": (
                0 if first_text(row, ["meaning_zh", "meaning_zh_tw", "meaning", "vocab_meaning"]) else 1,
                0 if first_text(row, ["reading_hiragana", "reading", "kana", "vocab_reading"]) else 1,
                due_rank,
                -priority_value,
                int(row.get("used_in_material_count", 0) or 0),
                random.random(),
            ),
        }
        if target and row_level and row_level != target:
            backup_items.append(item)
        else:
            target_items.append(item)

    ordered = sorted(target_items, key=lambda item: item["_sort"]) + sorted(backup_items, key=lambda item: item["_sort"])
    selected = ordered[:limit]
    mark_vocabulary_pool_used(selected)
    for item in selected:
        for key in list(item):
            if key.startswith("_"):
                item.pop(key, None)
    return selected


LOCAL_SEED_VOCAB = [
    {"word": "予定", "reading": "よてい", "meaning": "預定；計畫"},
    {"word": "準備", "reading": "じゅんび", "meaning": "準備"},
    {"word": "確認", "reading": "かくにん", "meaning": "確認"},
    {"word": "資料", "reading": "しりょう", "meaning": "資料"},
    {"word": "進捗", "reading": "しんちょく", "meaning": "進度"},
    {"word": "提案", "reading": "ていあん", "meaning": "提案"},
    {"word": "改善", "reading": "かいぜん", "meaning": "改善"},
    {"word": "共有", "reading": "きょうゆう", "meaning": "共享；告知"},
    {"word": "締切", "reading": "しめきり", "meaning": "截止期限"},
    {"word": "相談", "reading": "そうだん", "meaning": "商量；諮詢"},
    {"word": "対応", "reading": "たいおう", "meaning": "處理；應對"},
    {"word": "変更", "reading": "へんこう", "meaning": "變更"},
    {"word": "必要", "reading": "ひつよう", "meaning": "必要"},
    {"word": "可能", "reading": "かのう", "meaning": "可能"},
    {"word": "原因", "reading": "げんいん", "meaning": "原因"},
    {"word": "結果", "reading": "けっか", "meaning": "結果"},
]


LOCAL_SEED_VERBS = [
    {
        "base": "確認する（かくにんする） - 確認",
        "masuStem": "確認し",
        "te": "確認して",
        "ta": "確認した",
        "nai": "確認しない",
        "ba": "確認すれば",
        "causative": "確認させる",
        "passive": "確認される",
        "causativePassive": "確認させられる",
    },
    {
        "base": "進める（すすめる） - 推進；進行",
        "masuStem": "進め",
        "te": "進めて",
        "ta": "進めた",
        "nai": "進めない",
        "ba": "進めれば",
        "causative": "進めさせる",
        "passive": "進められる",
        "causativePassive": "進めさせられる",
    },
    {
        "base": "直す（なおす） - 修正",
        "masuStem": "直し",
        "te": "直して",
        "ta": "直した",
        "nai": "直さない",
        "ba": "直せば",
        "causative": "直させる",
        "passive": "直される",
        "causativePassive": "直させられる",
    },
    {
        "base": "選ぶ（えらぶ） - 選擇",
        "masuStem": "選び",
        "te": "選んで",
        "ta": "選んだ",
        "nai": "選ばない",
        "ba": "選べば",
        "causative": "選ばせる",
        "passive": "選ばれる",
        "causativePassive": "選ばせられる",
    },
    {
        "base": "伝える（つたえる） - 傳達",
        "masuStem": "伝え",
        "te": "伝えて",
        "ta": "伝えた",
        "nai": "伝えない",
        "ba": "伝えれば",
        "causative": "伝えさせる",
        "passive": "伝えられる",
        "causativePassive": "伝えさせられる",
    },
]


def material_seed_vocab(settings, limit):
    if limit <= 0:
        return []
    seed = list(sample_material(settings).get("vocab", [])) + LOCAL_SEED_VOCAB
    items = []
    seen = set()
    for item in seed:
        word = str(item.get("word", "")).strip()
        if not word or word in seen:
            continue
        seen.add(word)
        items.append(
            {
                "word": word,
                "reading": item.get("reading", ""),
                "meaning": item.get("meaning", ""),
                "part_of_speech": "",
                "jlpt_level": settings.get("target_level", ""),
                "example_sentence": item.get("example_sentence", ""),
                "example_translation_zh": item.get("example_translation_zh", ""),
                "source": "seed",
            }
        )
        if len(items) >= limit:
            return items
    base_items = list(items)
    while len(items) < limit and base_items:
        items.append(dict(base_items[len(items) % len(base_items)]))
    return items[:limit]


def material_verbs_from_db(limit):
    if limit <= 0:
        return []
    ensure_settings_store()
    rows = sqlite_dicts("SELECT * FROM verbs ORDER BY RANDOM() LIMIT ?", (limit,))
    return [
        {
            "base": f"{row['dictionary_form']}（{row['reading']}） - {row['meaning']}",
            "masuStem": answer_display_value(row["renyou_form"]),
            "te": answer_display_value(row["te_form"]),
            "ta": answer_display_value(row["ta_form"]),
            "nai": answer_display_value(row["nai_form"]),
            "ba": answer_display_value(row["ba_form"]),
            "causative": answer_display_value(row["shieki_form"]),
            "passive": answer_display_value(row["ukemi_form"]),
            "causativePassive": "",
            "source": "verbs",
        }
        for row in rows
    ]


def material_seed_verbs(settings, limit):
    if limit <= 0:
        return []
    seed = list(sample_material(settings).get("verbs", [])) + LOCAL_SEED_VERBS
    items = []
    seen = set()
    for item in seed:
        base = str(item.get("base", "")).strip()
        if not base or base in seen:
            continue
        seen.add(base)
        copied = dict(item)
        copied["source"] = "seed"
        items.append(copied)
        if len(items) >= limit:
            return items
    base_items = list(items)
    while len(items) < limit and base_items:
        items.append(dict(base_items[len(items) % len(base_items)]))
    return items[:limit]


def due_wrong_answer_summary(limit=5):
    try:
        rows = query_mistakes({"scope": "due"}, limit=limit)
    except Exception:
        return []
    return [
        {
            "question_type": row.get("question_type", ""),
            "wrong_answer": row.get("user_wrong_answer", ""),
            "correct_answer": row.get("correct_answer", ""),
            "mistake_count": row.get("mistake_count", 0),
        }
        for row in rows
    ]


def build_local_quiz(vocab, verbs, settings):
    mcq_count = int(settings.get("mcq_count", 0) or 0)
    fill_count = int(settings.get("fill_count", 0) or 0)
    questions = []
    meanings = [item.get("meaning", "") for item in vocab if item.get("meaning")]
    for item in vocab[:mcq_count]:
        answer = item.get("meaning", "")
        if not answer:
            continue
        options = [answer]
        for meaning in shuffled(meanings):
            if meaning and meaning not in options:
                options.append(meaning)
            if len(options) >= 4:
                break
        questions.append(
            {
                "type": "MCQ",
                "q": f"下列哪個意思最接近「{item.get('word', '')}」？",
                "options": shuffled(options),
                "ans": answer,
            }
        )
    for verb in verbs[:fill_count]:
        questions.append(
            {
                "type": "FILL",
                "q": f"請寫出「{verb.get('base', '')}」的て形。",
                "ans": clean_answer_value(verb.get("te", "")),
                "displayAns": verb.get("te", ""),
            }
        )
    return questions


def build_local_material(settings, force_seed=False):
    settings = normalize_settings(settings)
    vocab_count = int(settings["vocab_count"])
    verb_count = int(settings["verb_count"])
    source_counts = {"vocabulary": 0, "slang": 0, "wrong": 0, "seed": 0}
    seed_used = False

    slang_quota = 0 if vocab_count < 5 else max(1, min(int(vocab_count * 0.2), vocab_count))
    slang_vocab = [] if force_seed else material_vocab_from_approved_slang(slang_quota)
    source_counts["slang"] = len(slang_vocab)

    base_quota = max(0, vocab_count - len(slang_vocab))
    vocab = [] if force_seed else material_vocab_from_vocabulary_pool(settings, base_quota)
    if not force_seed and len(vocab) < base_quota:
        vocab.extend(material_vocab_from_existing(settings, base_quota - len(vocab)))
    source_counts["vocabulary"] = len(vocab)
    vocab.extend(slang_vocab)

    if len(vocab) < vocab_count:
        seed_items = material_seed_vocab(settings, vocab_count - len(vocab))
        vocab.extend(seed_items)
        source_counts["seed"] += len(seed_items)
        seed_used = bool(seed_items)
    vocab = vocab[:vocab_count]

    verbs = [] if force_seed else material_verbs_from_db(verb_count)
    if len(verbs) < verb_count:
        seed_verbs = material_seed_verbs(settings, verb_count - len(verbs))
        verbs.extend(seed_verbs)
        source_counts["seed"] += len(seed_verbs)
        seed_used = bool(seed_verbs)
    verbs = verbs[:verb_count]

    wrong_items = due_wrong_answer_summary()
    source_counts["wrong"] = len(wrong_items)
    quiz = build_local_quiz(vocab, verbs, settings)
    grammar = {
        "title": "本地題庫複習",
        "exp": "本日教材由本地詞庫、已審核新詞與錯題紀錄組成，適合用來穩定複習，不消耗 Gemini API 額度。",
        "examples": [
            {"jp": "今日は新しい言葉を復習します。", "cn": "今天複習新的詞彙。"},
            {"jp": "間違えたところをもう一度確認します。", "cn": "再確認一次曾經答錯的地方。"},
        ],
    }
    metadata = {
        "generation_mode": "local",
        "ai_used": False,
        "fallback_used": False,
        "source_summary": source_counts,
        "wrong_reviews": wrong_items,
        "quiz": quiz,
        "seed_used": seed_used,
        "generated_at": utc_now_iso(),
    }
    print(
        "[material-generator] local sources "
        f"vocabulary={source_counts['vocabulary']} slang={source_counts['slang']} "
        f"wrong={source_counts['wrong']} seed={source_counts['seed']}"
    )
    return {"vocab": vocab, "verbs": verbs, "grammar": grammar, "metadata": metadata}


def save_material_for_today(material, settings):
    ensure_database()
    date = today_string()
    vocab_list = material.get("vocab") or []
    verb_list = material.get("verbs") or []
    grammar = material.get("grammar") or {}
    metadata = material.get("metadata") or {}
    now = utc_now_iso()
    max_rows = max(len(vocab_list), len(verb_list), 1)

    new_rows = []
    for i in range(max_rows):
        vocab = vocab_list[i] if i < len(vocab_list) else {}
        verb = verb_list[i] if i < len(verb_list) else {}
        new_rows.append(
            {
                "date": date,
                "target_level": settings["target_level"],
                "vocab_word": vocab.get("word", ""),
                "vocab_reading": vocab.get("reading", ""),
                "vocab_meaning": vocab.get("meaning", ""),
                "vocab_part_of_speech": vocab.get("part_of_speech", ""),
                "vocab_source": vocab.get("source", ""),
                "vocab_example_sentence": vocab.get("example_sentence", ""),
                "vocab_example_translation_zh": vocab.get("example_translation_zh", ""),
                "verb_base": verb.get("base", ""),
                "verb_masu_stem": verb.get("masuStem", ""),
                "verb_te": verb.get("te", ""),
                "verb_ta": verb.get("ta", ""),
                "verb_nai": verb.get("nai", ""),
                "verb_ba": verb.get("ba", ""),
                "verb_causative": verb.get("causative", ""),
                "verb_passive": verb.get("passive", ""),
                "verb_causative_passive": verb.get("causativePassive", ""),
                "grammar_title": grammar.get("title", "") if i == 0 else "",
                "grammar_exp": grammar.get("exp", "") if i == 0 else "",
                "grammar_examples": json.dumps(grammar.get("examples", []), ensure_ascii=False) if i == 0 else "",
                "material_json": json.dumps(material, ensure_ascii=False) if i == 0 else "",
                "generation_mode": metadata.get("generation_mode", "") if i == 0 else "",
                "ai_used": str(bool(metadata.get("ai_used", False))).lower() if i == 0 else "",
                "source_summary": json.dumps(metadata.get("source_summary", {}), ensure_ascii=False) if i == 0 else "",
                "created_at": now,
                "updated_at": now,
            }
        )

    if DATABASE_URL:
        placeholders = ", ".join(["%s"] * len(COLUMNS))
        columns_sql = ", ".join(COLUMNS)
        rows = [tuple(clean_db_payload(row)[col] for col in COLUMNS) for row in new_rows]
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM materials WHERE date = %s", (date,))
                cur.executemany(f"INSERT INTO materials ({columns_sql}) VALUES ({placeholders})", rows)
            conn.commit()
        return date

    df = read_database()
    df = df[df["date"] != date]
    output = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
    output[COLUMNS].to_csv(DATABASE_FILE, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    return date


def material_by_date(target_date):
    df = read_database()
    rows = df[df["date"] == target_date]
    if rows.empty:
        return None

    vocabulary = []
    verbs = []
    for _, row in rows.iterrows():
        if row["vocab_word"]:
            vocabulary.append(
                {
                    "word": row["vocab_word"],
                    "reading": row["vocab_reading"],
                    "meaning": row["vocab_meaning"] or "尚未建立中文意思",
                    "part_of_speech": row.get("vocab_part_of_speech", ""),
                    "source": row.get("vocab_source", ""),
                    "example_sentence": row.get("vocab_example_sentence", ""),
                    "example_translation_zh": row.get("vocab_example_translation_zh", ""),
                }
            )
        if row["verb_base"]:
            verbs.append(
                {
                    "base": row["verb_base"],
                    "masuStem": row.get("verb_masu_stem", ""),
                    "te": row["verb_te"],
                    "ta": row["verb_ta"],
                    "nai": row["verb_nai"],
                    "ba": row["verb_ba"],
                    "causative": row["verb_causative"],
                    "passive": row["verb_passive"],
                    "causativePassive": row["verb_causative_passive"],
                }
            )

    first = rows.iloc[0]
    try:
        examples = json.loads(first["grammar_examples"]) if first["grammar_examples"] else []
    except json.JSONDecodeError:
        examples = []
    try:
        metadata = json.loads(first.get("material_json", "") or "{}")
        metadata = metadata.get("metadata", metadata) if isinstance(metadata, dict) else {}
    except json.JSONDecodeError:
        metadata = {}
    if not metadata:
        metadata = {
            "generation_mode": first.get("generation_mode", ""),
            "ai_used": first.get("ai_used", ""),
            "source_summary": first.get("source_summary", ""),
        }

    return {
        "date": target_date,
        "targetLevel": first.get("target_level", ""),
        "vocabulary": vocabulary,
        "verbs": verbs,
        "grammar": {"title": first["grammar_title"], "exp": first["grammar_exp"], "examples": examples},
        "metadata": metadata,
    }


def build_telegram_notification(material, date, app_url=None):
    link = html.escape(app_url or APP_URL)
    words = "、".join(html.escape(v.get("word", "")) for v in material.get("vocabulary", []) if v.get("word"))
    grammar_title = html.escape(material.get("grammar", {}).get("title", "今日文法"))
    level = html.escape(material.get("targetLevel", ""))
    return (
        f"<b>日語學習自動化系統</b>\n"
        f"日期：{html.escape(date)}\n"
        f"等級：{level}\n\n"
        f"<b>今日單字：</b>{words or '暫無'}\n"
        f"<b>今日文法：</b>{grammar_title}\n\n"
        f'<a href="{link}">點擊開啟學習頁面</a>'
    )


def send_telegram_message(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("Telegram Token 或 Chat ID 尚未設定。")

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"Telegram 回傳錯誤：{data}")
    return data


def normalize_generation_mode(value):
    mode = str(value or "local").strip().lower()
    return mode if mode in {"local", "ai_enhance", "ai_full"} else "local"


def material_success_message(date, settings, material, telegram_status):
    metadata = material.get("metadata") or {}
    missing_meaning_count = sum(1 for item in material.get("vocab", []) if not str(item.get("meaning", "")).strip())
    if metadata.get("fallback_used"):
        base = "AI 配額暫時用完，已改用本地教材生成。本次教材已成功建立，未中斷。"
    elif metadata.get("seed_used"):
        base = "✅ 今日教材已成功從本地詞庫建立。本次未消耗 Gemini API 額度。部分內容由內建範例補足，建議後續增加詞庫資料。"
    elif not metadata.get("ai_used"):
        base = "✅ 今日教材已成功從本地詞庫建立。本次未消耗 Gemini API 額度。"
    else:
        base = f"{date} 的 {settings['target_level']} 學習材料已經生成並保存。"
    if missing_meaning_count:
        base += " 部分詞彙尚未建立中文意思，建議後續補齊 vocabulary_pool。"
    return f"{base} {telegram_status}"


def generate_daily_material(use_sample=False, posted_settings=None, app_url=None, mode="local"):
    settings = save_settings_file(posted_settings) if posted_settings else load_settings()
    mode = "local" if use_sample else normalize_generation_mode(mode)
    print(f"[material-generator] mode={mode} start")

    if mode == "local":
        raw_material = build_local_material(settings, force_seed=use_sample)
    elif mode == "ai_enhance":
        raw_material = build_local_material(settings)
        raw_material["metadata"]["generation_mode"] = "ai_enhance"
        raw_material["metadata"]["ai_used"] = False
        raw_material["metadata"]["fallback_used"] = False
    else:
        try:
            raw_material = parse_json_from_ai(call_gemini(build_prompt(settings)))
            raw_material = merge_approved_slang_into_material(raw_material, settings)
            raw_material["metadata"] = {
                "generation_mode": "ai_full",
                "ai_used": True,
                "fallback_used": False,
                "source_summary": {"ai": 1},
                "seed_used": False,
                "generated_at": utc_now_iso(),
            }
        except Exception as e:
            print(f"[material-generator] ai_full failed; fallback local; error={classify_gemini_error(e)}")
            raw_material = build_local_material(settings)
            raw_material["metadata"]["generation_mode"] = "local"
            raw_material["metadata"]["ai_used"] = False
            raw_material["metadata"]["fallback_used"] = True

    if mode == "local" and raw_material.get("metadata", {}).get("ai_used"):
        print("[material-generator] ERROR local mode attempted to call Gemini")

    date = save_material_for_today(raw_material, settings)
    print(f"[material-generator] local material generated; ai_used={str(raw_material.get('metadata', {}).get('ai_used', False)).lower()}")
    print(f"[material-generator] material saved date={date}")
    material = material_by_date(date)

    telegram_status = "未發送"
    try:
        send_telegram_message(build_telegram_notification(material, date, app_url))
        telegram_status = "Telegram 通知已發送"
    except Exception as e:
        telegram_status = f"Telegram 通知發送失敗：{e}"

    invalidate_dashboard_cache("daily material generated")
    return {
        "message": material_success_message(date, settings, raw_material, telegram_status),
        "date": date,
        "telegram": telegram_status,
        "generation_mode": raw_material.get("metadata", {}).get("generation_mode", mode),
        "ai_used": bool(raw_material.get("metadata", {}).get("ai_used", False)),
        "fallback_used": bool(raw_material.get("metadata", {}).get("fallback_used", False)),
        "source_summary": raw_material.get("metadata", {}).get("source_summary", {}),
    }


def material_generation_error_payload(error):
    detail = str(error or "")
    print(f"[material-generator] ERROR generate failed: {detail}")
    print(traceback.format_exc())
    lower = detail.lower()
    if "timestamp" in lower or "timestamptz" in lower or "timestamp with time zone" in lower:
        return {"error": "本地教材生成失敗，資料庫時間欄位格式異常，請稍後再試。"}
    return {"error": "本地教材生成失敗，請稍後再試。"}


def shuffled(items):
    items = list(items)
    random.shuffle(items)
    return items


def group_label(group):
    return {1: "五段動詞", 2: "上下段動詞", 3: "不規則動詞"}.get(int(group), "未知類型")


def form_rule_explanation(verb, question_type):
    group = int(verb["verb_group"])
    label = group_label(group)
    if question_type == "renyou_form":
        if group == 1:
            rule = "五段動詞的連用形通常把語尾う段改成い段。例：書く→書き。"
        elif group == 2:
            rule = "上下段動詞的連用形通常去掉る。例：食べる→食べ。"
        else:
            rule = "不規則動詞需個別記憶。する→し，来る→来。"
        return f"{label}。連用形是ます形去掉ます，不是使役形。{rule}"
    if question_type == "shieki_form":
        if group == 1:
            rule = "五段動詞使役形通常把語尾う段改成あ段後加せる。例：行く→行かせる。"
        elif group == 2:
            rule = "上下段動詞使役形通常去掉る後加させる。例：食べる→食べさせる。"
        else:
            rule = "不規則動詞需個別記憶。する→させる，来る→来させる。"
        return f"{label}。使役形表示讓某人做某事，常見形態是させる。{rule}"
    return f"{label}。請比較題目指定形態與正確答案，注意假名與送假名。"


def clean_answer_value(value):
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    text = re.sub(r"[\s\u3000\u200b\u200c\u200d\ufeff]+", "", text)
    return text


def parenthetical_reading(value):
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    match = re.search(r"[（(]([ぁ-ゖーァ-ヶー\s\u3000]+)[）)]", normalized)
    return clean_answer_value(match.group(1)) if match else ""


def contains_kanji(text):
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def is_kana_reading(value):
    text = clean_answer_value(value)
    return bool(text) and re.fullmatch(r"[ぁ-ゖァ-ヶー]+", text) is not None


def extract_mecab_reading(surface, features):
    for index in (9, 11, 6, 7):
        if len(features) > index and is_kana_reading(features[index]):
            return kana_to_hiragana(features[index])
    for value in features:
        if is_kana_reading(value):
            return kana_to_hiragana(value)
    return kana_to_hiragana(surface)


def answer_reading_hiragana(value):
    explicit_reading = parenthetical_reading(value)
    if explicit_reading:
        return kana_to_hiragana(explicit_reading)
    text = clean_answer_value(value)
    if not text:
        return ""
    if text in ANSWER_READING_FALLBACKS:
        return ANSWER_READING_FALLBACKS[text]
    if not contains_kanji(text):
        return kana_to_hiragana(text)
    try:
        import MeCab
        import unidic_lite

        mecabrc = "nul" if os.name == "nt" else "/dev/null"
        tagger = MeCab.Tagger(f"-r {mecabrc} -d {unidic_lite.DICDIR}")
        readings = []
        for line in tagger.parse(text).splitlines():
            if not line or line == "EOS":
                continue
            surface, _, feature_text = line.partition("\t")
            features = feature_text.split(",") if feature_text else []
            readings.append(extract_mecab_reading(surface, features))
        return clean_answer_value("".join(readings))
    except Exception:
        return kana_to_hiragana(text)


def smart_answer_equal(user_input, correct_answer):
    user_clean = clean_answer_value(user_input)
    correct_clean = clean_answer_value(correct_answer)
    if not user_clean or not correct_clean:
        return False
    if user_clean == correct_clean:
        return True
    user_reading = answer_reading_hiragana(user_clean)
    correct_reading = answer_reading_hiragana(correct_clean)
    return bool(user_reading and correct_reading and user_reading == correct_reading)


def answer_display_value(correct_answer, preferred_answer=None):
    clean = clean_answer_value(correct_answer)
    preferred = clean_answer_value(preferred_answer)
    if preferred and contains_kanji(preferred) and smart_answer_equal(preferred, clean):
        preferred_reading = answer_reading_hiragana(preferred)
        if preferred_reading and preferred_reading != preferred:
            return f"{preferred}（{preferred_reading}）"
    reading = answer_reading_hiragana(clean)
    if contains_kanji(clean) and reading and reading != clean:
        return f"{clean}（{reading}）"
    return clean


def make_debug_report_payload(question_type="", prompt="", user_answer="", correct_answer="", target_text="", target_reading="", target_form="", error_category="", extra=None):
    return debug_grammar(
        {
            "question_type": question_type,
            "prompt": prompt,
            "user_answer": user_answer,
            "correct_answer": correct_answer,
            "target_text": target_text,
            "target_reading": target_reading,
            "target_form": target_form or question_type,
            "error_category": error_category,
            "extra": extra or {},
        }
    )


def debug_report_to_json(report):
    return json.dumps(report or {}, ensure_ascii=False)


def normalize_error_category(value):
    return value if value in ERROR_CATEGORIES else "動詞變化錯"


def next_interval_after_success(current_interval):
    try:
        current = int(current_interval or 1)
    except (TypeError, ValueError):
        current = 1
    for step in REVIEW_INTERVAL_STEPS:
        if current < step:
            return step
    return REVIEW_INTERVAL_STEPS[-1]


def add_mistake(verb_id, question_type, wrong_answer, error_category="動詞變化錯"):
    now = datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds")
    category = normalize_error_category(error_category)
    target = sqlite_one("SELECT * FROM verbs WHERE id = ?", (verb_id,)) if int(verb_id or 0) > 0 else None
    correct_answer = target.get(question_type, "") if target and question_type in target else ""
    prompt = ""
    if target:
        prompt = f"請寫出「{target['dictionary_form']}（{target['reading']}）」的{VERB_FORM_LABELS.get(question_type, question_type)}。"
    report = make_debug_report_payload(
        question_type=question_type,
        prompt=prompt,
        user_answer=wrong_answer,
        correct_answer=correct_answer,
        target_text=target.get("dictionary_form", "") if target else "",
        target_reading=target.get("reading", "") if target else "",
        target_form=question_type,
        error_category=category,
    )
    report_json = debug_report_to_json(report)
    existing = sqlite_one(
        """
        SELECT id, mistake_count, user_wrong_answer
        FROM mistake_logs
        WHERE verb_id = ? AND question_type = ? AND mastered = 0
        """,
        (verb_id, question_type),
    )
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        if existing:
            answers = [a for a in existing["user_wrong_answer"].split(" / ") if a]
            answers.append(wrong_answer)
            answers = answers[-5:]
            conn.execute(
                """
                UPDATE mistake_logs
                SET user_wrong_answer = ?,
                    mistake_count = ?,
                    last_reviewed_at = ?,
                    next_review_date = ?,
                    review_interval = 1,
                    mastered = 0,
                    status = 'learning',
                    error_category = ?,
                    debug_report_json = ?
                WHERE id = ?
                """,
                (" / ".join(answers), int(existing["mistake_count"]) + 1, now, iso_date_after(1), category, report_json, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO mistake_logs
                (
                    verb_id, question_type, user_wrong_answer, mistake_count,
                    status, last_reviewed_at, next_review_date, review_interval,
                    review_count, mastered, error_category, debug_report_json
                )
                VALUES (?, ?, ?, 1, 'learning', ?, ?, 1, 0, 0, ?, ?)
                """,
                (verb_id, question_type, wrong_answer, now, iso_date_after(1), category, report_json),
            )
        conn.commit()
    invalidate_dashboard_cache("mistake added")


def add_or_update_sns_mistake(example, user_translation, error_category, interval_days):
    now = utc_now_iso()
    category = normalize_error_category(error_category)
    question_type = f"sns_translation:{example['id']}"
    wrong_answer = f"{example['japanese']}｜使用者翻譯：{user_translation}"
    report = make_debug_report_payload(
        question_type=question_type,
        prompt=example["japanese"],
        user_answer=user_translation,
        correct_answer=example.get("zh_tw_translation", ""),
        target_text=example["japanese"],
        target_reading=example.get("reading_hiragana", ""),
        target_form="sns_translation",
        error_category=category,
        extra={
            "literal_translation_trap": example.get("literal_translation_trap", ""),
            "natural_rewrite": example.get("natural_rewrite", ""),
            "tone_category": example.get("tone_category", ""),
        },
    )
    report_json = debug_report_to_json(report)
    existing = sqlite_one(
        """
        SELECT id, mistake_count, user_wrong_answer
        FROM mistake_logs
        WHERE verb_id = 0 AND question_type = ? AND mastered = 0
        """,
        (question_type,),
    )
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        if existing:
            answers = [a for a in existing["user_wrong_answer"].split(" / ") if a]
            answers.append(wrong_answer)
            conn.execute(
                """
                UPDATE mistake_logs
                SET user_wrong_answer = ?,
                    mistake_count = mistake_count + 1,
                    last_reviewed_at = ?,
                    next_review_date = ?,
                    review_interval = ?,
                    mastered = 0,
                    status = 'learning',
                    error_category = ?,
                    debug_report_json = ?
                WHERE id = ?
                """,
                (" / ".join(answers[-5:]), now, iso_date_after(interval_days), interval_days, category, report_json, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO mistake_logs
                (
                    verb_id, question_type, user_wrong_answer, mistake_count,
                    status, last_reviewed_at, next_review_date, review_interval,
                    review_count, mastered, error_category, debug_report_json
                )
                VALUES (0, ?, ?, 1, 'learning', ?, ?, ?, 0, 0, ?, ?)
                """,
                (question_type, wrong_answer, now, iso_date_after(interval_days), interval_days, category, report_json),
            )
        conn.commit()
    invalidate_dashboard_cache("sns mistake updated")


def log_sns_practice(example, user_translation, self_evaluation, error_category=""):
    ensure_settings_store()
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        conn.execute(
            """
            INSERT INTO sns_practice_logs
            (created_at, example_id, user_translation, self_evaluation, tone_category, error_category)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                taipei_iso_now(),
                example["id"],
                user_translation,
                self_evaluation,
                example.get("tone_category", ""),
                error_category,
            ),
        )
        conn.commit()
    invalidate_dashboard_cache("sns practice logged")


def kana_to_hiragana(text):
    result = []
    for ch in text or "":
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            result.append(chr(code - 0x60))
        else:
            result.append(ch)
    return "".join(result)


def token_meaning_hint(surface, pos):
    hints = {
        "が": "主語或狀態對象標記",
        "を": "受詞標記",
        "に": "時間、方向、對象標記",
        "で": "地點、手段、原因標記",
        "は": "主題標記",
        "も": "也、同樣",
        "のに": "明明～卻～",
        "です": "禮貌判斷助動詞",
        "ます": "禮貌助動詞",
    }
    if surface in hints:
        return hints[surface]
    if "動詞" in pos:
        return "動作或狀態的核心"
    if "形容詞" in pos:
        return "性質或狀態描述"
    if "助詞" in pos:
        return "助詞，表示語法關係"
    return ""


def pick_reading(surface, features):
    return extract_mecab_reading(surface, features)


def analyze_with_mecab(text):
    try:
        import MeCab
        import unidic_lite

        mecabrc = "nul" if os.name == "nt" else "/dev/null"
        tagger = MeCab.Tagger(f"-r {mecabrc} -d {unidic_lite.DICDIR}")
    except Exception as e:
        return None, f"MeCab 初始化失敗，請檢查依賴：{e}"

    tokens = []
    particles = []
    verb_forms = []
    readings = []
    parsed = tagger.parse(text)
    for line in parsed.splitlines():
        if not line or line == "EOS":
            continue
        surface, _, feature_text = line.partition("\t")
        features = feature_text.split(",") if feature_text else []
        pos = features[0] if len(features) > 0 else ""
        pos_detail = features[1] if len(features) > 1 else ""
        conjugation_type = features[4] if len(features) > 4 else ""
        conjugation_form = features[5] if len(features) > 5 else ""
        base_form = features[7] if len(features) > 7 and features[7] != "*" else surface
        reading = pick_reading(surface, features)
        readings.append(reading)
        token = {
            "surface": surface,
            "reading_hiragana": reading,
            "base_form": base_form,
            "pos": pos,
            "pos_detail": pos_detail,
            "conjugation_type": conjugation_type,
            "conjugation_form": conjugation_form,
            "meaning_hint_zh_tw": token_meaning_hint(surface, pos),
        }
        tokens.append(token)
        if "助詞" in pos:
            particles.append(token)
        if "動詞" in pos or "助動詞" in pos:
            verb_forms.append(token)
    return {
        "reading_hiragana": "".join(readings),
        "tokens": tokens,
        "particles": particles,
        "verb_forms": verb_forms,
    }, None


def detect_grammar_patterns(text):
    rules = [
        ("Vたばかり", r"(た|だ)ばかり", "剛剛做完某動作。"),
        ("Vてばかりいる", r"(て|で)ばかり(いる|います|いて|いた|いない)", "老是一直做某事，常帶批評語氣。"),
        ("のに", r"のに", "明明～卻～，表示預期落差。"),
        ("ても", r"(ても|でも)", "即使～也。"),
        ("たら", r"(たら|だら)", "如果～／一旦～。"),
        ("ば形", r"(えば|けば|げば|せば|てば|ねば|べば|めば|れば)(?!かり)", "如果～的話。需確認是否為真正ば形。"),
        ("ように", r"ように", "希望～／像～一樣／為了～。需依語境判斷。"),
        ("ている／てる", r"(ている|でいる|てる|でる)", "正在進行或結果狀態持續。"),
        ("んだ／んですね", r"(んだ|んです|んですね|のだ|のです)", "說明、領悟、補充語氣。"),
        ("って", r"って(?:いう|言う|思う|こと|何|なに|、|。|？|！|!|$)", "引用、主題提示、口語說法。需依語境判斷。"),
        ("すぎる", r"すぎる", "太～了。"),
        ("そう", r"そう", "看起來～／聽說～。需人工確認是哪一種。"),
        ("たい", r"たい", "想～。"),
        ("られる", r"られる", "可能形或被動形，需依語境判斷。"),
        ("させる", r"させる|せる", "使役形，表示讓某人做某事。需確認是否為使役。"),
    ]
    patterns = []
    notes = []
    for name, pattern, description in rules:
        if re.search(pattern, text):
            item = {"pattern": name, "description_zh_tw": description}
            patterns.append(item)
            if "需" in description:
                notes.append(f"{name}：需人工確認。")
    if not patterns:
        notes.append("未偵測到指定的 15 種常見句型。")
    return patterns, notes


def common_misunderstandings_for(text, patterns):
    result = []
    names = {p["pattern"] for p in patterns}
    if "られる" in names:
        result.append("られる 可能表示可能形或受身形，不能只看字面判斷。")
    if "そう" in names:
        result.append("そう 可能是樣態或傳聞，需看前接詞性與上下文。")
    if "のに" in names:
        result.append("のに 常帶有遺憾或意外感，不只是單純連接詞。")
    if "ている／てる" in names:
        result.append("ている 不一定是正在進行，也可能表示結果狀態。")
    return result


def grammar_response_template(input_type="japanese"):
    return {
        "input_type": input_type,
        "natural_translation": "",
        "sentence_summary": "",
        "naturalness_check": {
            "is_natural": False,
            "level": "",
            "reason": "",
            "suggested_sentence": "",
        },
        "hiragana_reading": "",
        "tone": {"label": "", "explanation": ""},
        "sentence_structure": [],
        "grammar_points": [],
        "natural_alternatives": [],
        "learning_focus": {"summary": "", "tips": []},
        "slang_terms": [],
        "error_message": "",
    }


def grammar_not_japanese_response():
    payload = grammar_response_template("not_japanese")
    payload["naturalness_check"] = {
        "is_natural": False,
        "level": "無法解析",
        "reason": "目前此功能僅支援日文句子解析。",
        "suggested_sentence": "",
    }
    payload["error_message"] = "目前此功能僅支援日文句子解析，請輸入日文句子。"
    return payload


def grammar_ai_error_response(message="解析失敗，請稍後再試，或確認 Gemini API 金鑰是否正確。"):
    payload = grammar_response_template("japanese")
    payload["naturalness_check"] = {
        "is_natural": False,
        "level": "解析失敗",
        "reason": message,
        "suggested_sentence": "",
    }
    payload["error_message"] = message
    return payload


def grammar_fallback_response(text, hiragana_reading="", advanced_mecab=None):
    message = "Gemini 解析暫時失敗，已使用本地規則回傳部分結果。"
    payload = grammar_response_template("japanese")
    payload.update(
        {
            "natural_translation": "",
            "sentence_summary": "Gemini 解析暫時失敗，目前僅回傳本地規則偵測結果。",
            "naturalness_check": {
                "is_natural": True,
                "level": "未完整判定",
                "reason": "AI 解析逾時，無法完整判斷自然度。",
                "suggested_sentence": "",
            },
            "hiragana_reading": hiragana_reading or "",
            "tone": {
                "label": "未完整判定",
                "explanation": "AI 解析逾時，暫時無法提供完整語氣說明。",
            },
            "sentence_structure": [],
            "grammar_points": [],
            "natural_alternatives": [],
            "learning_focus": {
                "summary": "本次僅完成本地流行語補抓，建議稍後重新解析。",
                "tips": [],
            },
            "slang_terms": merge_slang_terms([], detect_known_slang_terms(text)),
            "error_message": message,
            "original": text,
            "advanced_mecab": advanced_mecab or {},
        }
    )
    return payload


def is_probably_japanese_text(text):
    return bool(re.search(r"[ぁ-ゖァ-ヺー]", text or ""))


def has_latin_letters(text):
    return bool(re.search(r"[A-Za-z]", text or ""))


def enforce_hiragana_reading(reading, japanese_source=""):
    reading = clean_answer_value(reading)
    if reading and not has_latin_letters(reading):
        return kana_to_hiragana(reading)
    return answer_reading_hiragana(japanese_source) if japanese_source else ""


def clean_gemini_json_text(text):
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^\s*```(?:json|JSON)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Gemini 沒有回傳合法 JSON 物件。")
    return cleaned[start : end + 1]


def parse_gemini_json_safely(raw_text):
    return json.loads(clean_gemini_json_text(raw_text))


def normalize_string(value):
    return str(value or "").strip()


def normalize_grammar_analysis(raw, original_text, fallback_reading, advanced_mecab=None):
    source = raw if isinstance(raw, dict) else {}
    payload = grammar_response_template(normalize_string(source.get("input_type")) or "japanese")
    payload["natural_translation"] = normalize_string(source.get("natural_translation"))
    payload["sentence_summary"] = normalize_string(source.get("sentence_summary"))
    payload["hiragana_reading"] = enforce_hiragana_reading(
        source.get("hiragana_reading") or fallback_reading,
        original_text,
    )

    naturalness = source.get("naturalness_check") if isinstance(source.get("naturalness_check"), dict) else {}
    payload["naturalness_check"] = {
        "is_natural": bool(naturalness.get("is_natural")),
        "level": normalize_string(naturalness.get("level")) or ("自然" if naturalness.get("is_natural") else "需確認"),
        "reason": normalize_string(naturalness.get("reason")),
        "suggested_sentence": normalize_string(naturalness.get("suggested_sentence")),
    }

    tone = source.get("tone") if isinstance(source.get("tone"), dict) else {}
    payload["tone"] = {
        "label": normalize_string(tone.get("label")),
        "explanation": normalize_string(tone.get("explanation")),
    }

    structure = source.get("sentence_structure") if isinstance(source.get("sentence_structure"), list) else []
    payload["sentence_structure"] = [
        {
            "segment": normalize_string(item.get("segment")),
            "function": normalize_string(item.get("function")),
            "meaning": normalize_string(item.get("meaning")),
        }
        for item in structure
        if isinstance(item, dict)
    ]

    grammar_points = source.get("grammar_points") if isinstance(source.get("grammar_points"), list) else []
    payload["grammar_points"] = []
    for item in grammar_points:
        if not isinstance(item, dict):
            continue
        example = item.get("example") if isinstance(item.get("example"), dict) else {}
        example_japanese = normalize_string(example.get("japanese"))
        payload["grammar_points"].append(
            {
                "name": normalize_string(item.get("name")),
                "formula": normalize_string(item.get("formula")),
                "explanation": normalize_string(item.get("explanation")),
                "meaning_in_sentence": normalize_string(item.get("meaning_in_sentence")),
                "example": {
                    "japanese": example_japanese,
                    "hiragana": enforce_hiragana_reading(example.get("hiragana"), example_japanese),
                    "chinese": normalize_string(example.get("chinese")),
                },
            }
        )

    alternatives = source.get("natural_alternatives") if isinstance(source.get("natural_alternatives"), list) else []
    payload["natural_alternatives"] = []
    for item in alternatives:
        if not isinstance(item, dict):
            continue
        alt_japanese = normalize_string(item.get("japanese"))
        payload["natural_alternatives"].append(
            {
                "japanese": alt_japanese,
                "hiragana": enforce_hiragana_reading(item.get("hiragana"), alt_japanese),
                "chinese": normalize_string(item.get("chinese")),
                "note": normalize_string(item.get("note")),
            }
        )

    focus = source.get("learning_focus") if isinstance(source.get("learning_focus"), dict) else {}
    tips = focus.get("tips") if isinstance(focus.get("tips"), list) else []
    payload["learning_focus"] = {
        "summary": normalize_string(focus.get("summary")),
        "tips": [normalize_string(tip) for tip in tips if normalize_string(tip)],
    }
    slang_terms = source.get("slang_terms") if isinstance(source.get("slang_terms"), list) else []
    payload["slang_terms"] = merge_slang_terms(slang_terms, detect_known_slang_terms(original_text))
    payload["error_message"] = normalize_string(source.get("error_message"))
    payload["original"] = original_text
    payload["advanced_mecab"] = advanced_mecab or {}
    return payload


def build_grammar_coach_prompt(text, hiragana_reading):
    return f"""
你是一位專門教台灣學習者理解日文語感的「日文句子理解教練」。
請分析使用者輸入的日文句子，並只回傳一個合法 JSON 物件。

嚴格禁止：
1. 禁止 Markdown。
2. 禁止 ```json 或 ```。
3. 禁止前言、後記、補充說明文字。
4. 禁止簡體中文，所有中文必須使用繁體中文。
5. 禁止羅馬拼音，所有日文讀音只能使用平假名。

分析規則：
1. natural_translation 必須自然通順，禁止逐字直翻。
2. hiragana_reading 必須只使用平假名，不得出現羅馬拼音。
3. sentence_structure 必須依照語意區塊拆解，不可只是單字詞性拆解。
4. grammar_points 只列出真正值得學習的文法與句型，不要列出無意義詞性資訊。
5. 若原句不自然，必須在 naturalness_check 中指出問題，並提供 suggested_sentence。
6. 若原句自然，naturalness_check.level 請填「自然」。
7. tone.label 必須簡短，tone.explanation 才放詳細說明。
8. example 必須包含 japanese、hiragana、chinese 三個欄位。
9. natural_alternatives 最多提供 2 句。
10. 不可為了填滿欄位而硬塞不重要的文法點。
11. 若句子很短，請解析真正有學習價值的語氣與用法。
12. 若輸入不是日文，必須回傳指定的 not_japanese JSON。
13. slang_terms 僅捕捉真正具有學習價值、需要審核的新詞、特殊名詞、SNS 用語、推し活用語、網路流行語或現代口語。
14. 不要把普通助詞、助動詞、一般單字或無意義碎片放入 slang_terms。
15. slang_terms.category 必須只能從 slang、internet_slang、otaku_culture、named_entity、sensitive、typo_or_noise、unknown 選擇，不可創造新分類。
16. 人名、暱稱、團名、作品名一律歸類為 named_entity。
17. 成人或敏感語境詞歸類為 sensitive。
18. 疑似錯字、一次性梗或雜訊歸類為 typo_or_noise 或 unknown。
19. should_add_to_candidates 代表是否加入候選池，不代表可直接進入每日教材。
20. named_entity、sensitive、typo_or_noise、unknown 即使 should_add_to_candidates 為 true，也只能進入候選池，不可直接進入正式每日教材。
21. 特別注意捕捉：めちゃくちゃ、めっちゃ、エモい、バズる、バズりそう、てぇてぇ、限界オタク。
22. 若出現 さくたん、ねんねちゃん 或類似暱稱，請放入 slang_terms，category 固定使用 named_entity。
23. sentence_structure 最多 5 個。
24. grammar_points 最多 3 個，只列最有學習價值的句型。
25. natural_alternatives 最多 2 個。
26. learning_focus.tips 最多 2 個。
27. slang_terms 最多 5 個。
28. 每個 explanation、reason、note、nuance 盡量控制在 80 到 120 字內，避免冗長。

Gemini 必須回傳的 JSON 結構：
{{
  "input_type": "japanese",
  "natural_translation": "通順、自然、非直翻的繁體中文翻譯",
  "sentence_summary": "用一句繁體中文說明整句核心意思",
  "naturalness_check": {{
    "is_natural": true,
    "level": "自然",
    "reason": "說明原句是否自然，若不自然需指出問題",
    "suggested_sentence": "若原句不自然，提供一個更自然的日文修正版；若原句自然則留空"
  }},
  "hiragana_reading": "整句平假名讀音，不可出現羅馬拼音",
  "tone": {{
    "label": "語氣類型，例如：疑問、委婉確認、吐槽、稱讚、感嘆、撒嬌、請求、推測、關心",
    "explanation": "說明這句在日常對話中的語感與使用情境"
  }},
  "sentence_structure": [
    {{
      "segment": "日文語意片段",
      "function": "該片段在句中的功能",
      "meaning": "該片段的自然中文理解"
    }}
  ],
  "grammar_points": [
    {{
      "name": "文法或句型名稱",
      "formula": "結構公式",
      "explanation": "詳細繁體中文說明，需解釋為什麼這裡這樣用，以及實際語感",
      "meaning_in_sentence": "在本句中的自然中文意思",
      "example": {{
        "japanese": "相同句型的日文例句",
        "hiragana": "例句的平假名讀音，不可使用羅馬拼音",
        "chinese": "自然繁體中文翻譯"
      }}
    }}
  ],
  "natural_alternatives": [
    {{
      "japanese": "更自然或不同語氣的日文替換說法",
      "hiragana": "替換句的平假名讀音，不可使用羅馬拼音",
      "chinese": "繁體中文意思",
      "note": "說明這個替換說法的語氣差異"
    }}
  ],
  "learning_focus": {{
    "summary": "用繁體中文總結這句最值得學習的地方",
    "tips": [
      "學習提醒 1",
      "學習提醒 2"
    ]
  }},
  "slang_terms": [
    {{
      "term": "捕捉到的流行詞彙",
      "normalized_term": "正規化後的詞條，例如 バズった 可歸為 バズる",
      "reading_hiragana": "純平假名讀音，絕對禁用羅馬拼音",
      "base_form": "原形，可空",
      "part_of_speech": "詞性，可空",
      "category": "slang / internet_slang / otaku_culture / named_entity / sensitive / typo_or_noise / unknown",
      "meaning_zh": "繁體中文意思",
      "nuance": "詳細語感、使用情境與使用陷阱說明",
      "confidence": 0.95,
      "should_add_to_candidates": true
    }}
  ],
  "error_message": ""
}}

若輸入不是日文，請回傳：
{{
  "input_type": "not_japanese",
  "natural_translation": "",
  "sentence_summary": "",
  "naturalness_check": {{
    "is_natural": false,
    "level": "無法解析",
    "reason": "目前此功能僅支援日文句子解析。",
    "suggested_sentence": ""
  }},
  "hiragana_reading": "",
  "tone": {{
    "label": "",
    "explanation": ""
  }},
  "sentence_structure": [],
  "grammar_points": [],
  "natural_alternatives": [],
  "learning_focus": {{
    "summary": "",
    "tips": []
  }},
  "slang_terms": [],
  "error_message": "目前此功能僅支援日文句子解析，請輸入日文句子。"
}}

使用者輸入：
{text}

系統參考平假名讀音：
{hiragana_reading}
""".strip()


def persist_analysis_slang_terms(payload, source_context, source):
    slang_terms = payload.get("slang_terms", []) if isinstance(payload, dict) else []
    candidates, _ = slang_candidates_for_write(slang_terms)
    log_slang(
        f"/api/analyze_grammar 完成；source={source}；"
        f"analysis_json_slang_terms={len(slang_terms)}；should_add_to_candidates={len(candidates)}"
    )
    return enqueue_slang_candidates(slang_terms, source_context=source_context, source=source)


def analyze_grammar_with_gemini(text):
    parsed, mecab_error = analyze_with_mecab(text)
    fallback_reading = parsed["reading_hiragana"] if parsed else answer_reading_hiragana(text)
    advanced_mecab = {
        "reading_hiragana": fallback_reading,
        "tokens": parsed["tokens"] if parsed else [],
        "particles": parsed["particles"] if parsed else [],
        "verb_forms": parsed["verb_forms"] if parsed else [],
        "error": mecab_error or "",
    }

    prompt = build_grammar_coach_prompt(text, fallback_reading)
    failures = []
    for model_name in gemini_model_candidates():
        raw_response = ""
        try:
            print(f"[grammar-analyzer] 嘗試 Gemini 模型；model={model_name}")
            raw_response = call_gemini(prompt, model_name=model_name)
            ai_payload = parse_gemini_json_safely(raw_response)
            payload = normalize_grammar_analysis(ai_payload, text, fallback_reading, advanced_mecab)
            print(f"[grammar-analyzer] Gemini 解析成功；model={model_name}")
            persist_analysis_slang_terms(payload, text, "grammar_analyzer")
            return payload, 200
        except Exception as e:
            error_text = str(e)
            error_type = classify_gemini_error(e)
            failures.append({"model": model_name, "error_type": error_type, "message": error_text[:300]})
            if raw_response:
                print(f"[grammar-analyzer] Gemini 原始回傳；model={model_name}；raw={raw_response[:1200]}")
            if error_type == "timeout":
                print(f"[grammar-analyzer] Gemini timeout; model={model_name}; timeout={GEMINI_TIMEOUT_SECONDS}s")
            print(f"[grammar-analyzer] Gemini 解析失敗；model={model_name}；error_type={error_type}；message={error_text}")
            print(traceback.format_exc())
            continue

    if failures:
        print(f"[grammar-analyzer] 所有 Gemini 模型皆失敗；failures={json.dumps(failures, ensure_ascii=False)}")
    payload = grammar_fallback_response(text, fallback_reading, advanced_mecab)
    persist_analysis_slang_terms(payload, text, "grammar_analyzer_fallback")
    return payload, 200


def handle_grammar_analyzer_api():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify(grammar_not_japanese_response()), 400
    if not is_probably_japanese_text(text):
        return jsonify(grammar_not_japanese_response())
    payload, status = analyze_grammar_with_gemini(text)
    return jsonify(payload), status


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "instance": "render-starter"})


@app.get("/readyz")
def readyz():
    checks = {}
    ok = True
    try:
        prepare_sqlite_path()
        with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=2) as conn:
            conn.execute("SELECT 1").fetchone()
        checks["sqlite"] = "ok"
        checks["sqlite_path"] = SQLITE_SETTINGS_FILE
    except Exception as e:
        ok = False
        checks["sqlite"] = f"error: {e}"
        checks["sqlite_path"] = SQLITE_SETTINGS_FILE

    if DATABASE_URL:
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            checks["postgresql"] = "ok"
        except Exception as e:
            ok = False
            checks["postgresql"] = f"error: {e}"
    else:
        checks["postgresql"] = "not_configured"

    status = 200 if ok else 503
    return jsonify({"status": "ready" if ok else "not_ready", "instance": "render-starter", "checks": checks}), status


@app.get("/verb-practice")
def verb_practice_page():
    return render_template("verb_practice.html", form_labels=VERB_FORM_LABELS)


@app.get("/mistake-review")
def mistake_review_page():
    return render_template("mistake_review.html", form_labels=VERB_FORM_LABELS)


@app.get("/grammar-analyzer")
def grammar_analyzer_page():
    return render_template("grammar_analyzer.html")


@app.get("/sns-practice")
def sns_practice_page():
    return render_template("sns_practice.html")


@app.get("/learning-report")
def learning_report_page():
    return render_template("learning_report.html")


@app.get("/api/settings")
def api_get_settings():
    return jsonify(load_settings())


@app.post("/api/settings")
def api_save_settings():
    return jsonify(save_settings_file(request.get_json(silent=True) or {}))


@app.get("/api/archive-dates")
def api_archive_dates():
    df = read_database()
    dates = [d for d in df["date"].drop_duplicates().tolist() if d]
    dates.sort(reverse=True)
    return jsonify(dates)


@app.get("/api/materials")
def api_materials():
    return jsonify(material_by_date(request.args.get("date", today_string())))


@app.post("/api/generate")
def api_generate():
    try:
        data = request.get_json(silent=True) or {}
        mode = request.args.get("mode") or data.get("mode") or "local"
        return jsonify(
            generate_daily_material(
                use_sample=request.args.get("sample") == "1",
                posted_settings=data,
                app_url=request.host_url.rstrip("/"),
                mode=mode,
            )
        )
    except Exception as e:
        return jsonify(material_generation_error_payload(e)), 500


@app.get("/api/cron/daily-push")
def api_cron_daily_push():
    if CRON_SECRET and request.args.get("secret") != CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    try:
        return jsonify(generate_daily_material(app_url=APP_URL, mode=request.args.get("mode", "local"))), 200
    except Exception as e:
        return jsonify(material_generation_error_payload(e)), 500


@app.post("/api/test-telegram")
def api_test_telegram():
    try:
        send_telegram_message(
            f"<b>Telegram 測試成功</b>\nFlask 日語學習系統可以發送訊息。\n時間：{datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return jsonify({"message": "Telegram 測試訊息已經發送成功。"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/verb-practice/question")
def api_verb_question():
    ensure_settings_store()
    question_type = request.args.get("type", "random")
    if question_type == "random" or question_type not in QUESTION_TYPES:
        question_type = random.choice(QUESTION_TYPES)
    verbs = sqlite_dicts("SELECT * FROM verbs ORDER BY RANDOM() LIMIT 1")
    if not verbs:
        return jsonify({"error": "動詞題庫尚未建立。"}), 404
    verb = verbs[0]
    return jsonify(
        {
            "verb_id": verb["id"],
            "dictionary_form": verb["dictionary_form"],
            "reading": verb["reading"],
            "meaning": verb["meaning"],
            "verb_group": verb["verb_group"],
            "verb_group_label": group_label(verb["verb_group"]),
            "question_type": question_type,
            "question_label": VERB_FORM_LABELS[question_type],
            "prompt": f"請寫出「{verb['dictionary_form']}（{verb['reading']}）・{verb['meaning']}」的{VERB_FORM_LABELS[question_type]}。",
        }
    )


@app.post("/api/verb-practice/check")
def api_verb_check():
    data = request.get_json(silent=True) or {}
    verb_id = data.get("verb_id")
    question_type = data.get("question_type")
    answer = clean_answer_value(data.get("answer", ""))
    if not verb_id or question_type not in QUESTION_TYPES or not answer:
        return jsonify({"error": "題目或答案不完整。"}), 400
    verb = sqlite_one("SELECT * FROM verbs WHERE id = ?", (verb_id,))
    if not verb:
        return jsonify({"error": "找不到動詞題目。"}), 404
    correct = verb[question_type]
    is_correct = smart_answer_equal(answer, correct)
    debug_report = None
    if not is_correct:
        add_mistake(int(verb_id), question_type, answer)
        debug_report = make_debug_report_payload(
            question_type=question_type,
            prompt=f"請寫出「{verb['dictionary_form']}（{verb['reading']}）」的{VERB_FORM_LABELS.get(question_type, question_type)}。",
            user_answer=answer,
            correct_answer=correct,
            target_text=verb["dictionary_form"],
            target_reading=verb["reading"],
            target_form=question_type,
            error_category="動詞變化錯",
        )
    return jsonify(
        {
            "correct": is_correct,
            "correct_answer": answer_display_value(correct, answer if is_correct else None),
            "verb_group": group_label(verb["verb_group"]),
            "rule": form_rule_explanation(verb, question_type),
            "mistake_added": not is_correct,
            "debug_report": debug_report,
        }
    )


@app.get("/api/mistakes")
def api_mistakes():
    return jsonify(query_mistakes(request.args))


def query_mistakes(args=None, limit=None):
    args = args or {}
    question_type = args.get("question_type", "all")
    error_category = args.get("error_category", "all")
    scope = args.get("scope", "due")
    params = []
    where = "m.mastered = 0"
    if question_type in QUESTION_TYPES:
        where += " AND m.question_type = ?"
        params.append(question_type)
    if error_category in ERROR_CATEGORIES:
        where += " AND m.error_category = ?"
        params.append(error_category)
    if scope != "all":
        where += " AND COALESCE(m.next_review_date, date(m.last_reviewed_at), ?) <= ?"
        params.extend([today_iso_date(), today_iso_date()])
    sql = f"""
    SELECT
        m.id, m.verb_id, m.question_type, m.user_wrong_answer,
        m.mistake_count, m.status, m.last_reviewed_at,
        m.next_review_date, m.review_interval, m.review_count,
        m.mastered, m.error_category, m.debug_report_json,
        v.dictionary_form, v.reading, v.meaning, v.verb_group,
        v.te_form, v.ta_form, v.nai_form, v.renyou_form,
        v.shieki_form, v.ukemi_form, v.ba_form
    FROM mistake_logs m
    JOIN verbs v ON v.id = m.verb_id
    WHERE {where}
    ORDER BY COALESCE(m.next_review_date, date(m.last_reviewed_at)) ASC,
             m.mistake_count DESC,
             m.last_reviewed_at DESC
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = sqlite_dicts(
        sql,
        tuple(params),
    )
    for row in rows:
        row["question_label"] = VERB_FORM_LABELS.get(row["question_type"], row["question_type"])
        row["correct_answer"] = answer_display_value(row[row["question_type"]]) if row["question_type"] in QUESTION_TYPES else ""
        row["verb_group_label"] = group_label(row["verb_group"])
    return rows


@app.get("/api/mistakes/stats")
def api_mistake_stats():
    today = today_iso_date()
    due_count = sqlite_one(
        """
        SELECT COUNT(*) AS count
        FROM mistake_logs
        WHERE mastered = 0 AND COALESCE(next_review_date, date(last_reviewed_at), ?) <= ?
        """,
        (today, today),
    )
    mastered_count = sqlite_one("SELECT COUNT(*) AS count FROM mistake_logs WHERE mastered = 1 OR status = 'mastered'")
    category_rows = sqlite_dicts(
        """
        SELECT COALESCE(NULLIF(error_category, ''), '動詞變化錯') AS category,
               SUM(mistake_count) AS total
        FROM mistake_logs
        WHERE mastered = 0
        GROUP BY COALESCE(NULLIF(error_category, ''), '動詞變化錯')
        ORDER BY total DESC
        LIMIT 1
        """
    )
    return jsonify(
        {
            "due_count": int(due_count["count"] if due_count else 0),
            "mastered_count": int(mastered_count["count"] if mastered_count else 0),
            "top_error_category": category_rows[0]["category"] if category_rows else "尚無資料",
            "error_categories": ERROR_CATEGORIES,
        }
    )


@app.post("/api/mistakes/<int:mistake_id>/mastered")
def api_mark_mistake_mastered(mistake_id):
    ensure_settings_store()
    now = datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds")
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        cur = conn.execute(
            """
            UPDATE mistake_logs
            SET status = 'mastered',
                mastered = 1,
                last_reviewed_at = ?,
                next_review_date = NULL
            WHERE id = ?
            """,
            (now, mistake_id),
        )
        conn.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "找不到錯題紀錄。"}), 404
    return jsonify({"success": True})


@app.post("/api/mistakes/<int:mistake_id>/retry")
def api_retry_mistake(mistake_id):
    data = request.get_json(silent=True) or {}
    answer = clean_answer_value(data.get("answer", ""))
    error_category = normalize_error_category(data.get("error_category", "動詞變化錯"))
    if not answer:
        return jsonify({"error": "請先輸入答案。"}), 400
    row = sqlite_one(
        """
        SELECT m.*, v.dictionary_form, v.reading, v.meaning, v.verb_group,
               v.te_form, v.ta_form, v.nai_form, v.renyou_form,
               v.shieki_form, v.ukemi_form, v.ba_form
        FROM mistake_logs m
        JOIN verbs v ON v.id = m.verb_id
        WHERE m.id = ?
        """,
        (mistake_id,),
    )
    if not row:
        return jsonify({"error": "找不到錯題紀錄。"}), 404
    correct = row[row["question_type"]]
    is_correct = smart_answer_equal(answer, correct)
    report = make_debug_report_payload(
        question_type=row["question_type"],
        prompt=f"請寫出「{row['dictionary_form']}（{row['reading']}）」的{VERB_FORM_LABELS.get(row['question_type'], row['question_type'])}。",
        user_answer=answer,
        correct_answer=correct,
        target_text=row["dictionary_form"],
        target_reading=row["reading"],
        target_form=row["question_type"],
        error_category=error_category,
    )
    report_json = debug_report_to_json(report)
    now = datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds")
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        if is_correct:
            next_interval = next_interval_after_success(row.get("review_interval"))
            conn.execute(
                """
                UPDATE mistake_logs
                SET last_reviewed_at = ?,
                    review_interval = ?,
                    review_count = COALESCE(review_count, 0) + 1,
                    next_review_date = ?,
                    error_category = COALESCE(NULLIF(error_category, ''), ?)
                WHERE id = ?
                """,
                (now, next_interval, iso_date_after(next_interval), error_category, mistake_id),
            )
        else:
            conn.execute(
                """
                UPDATE mistake_logs
                SET user_wrong_answer = ?,
                    mistake_count = mistake_count + 1,
                    last_reviewed_at = ?,
                    next_review_date = ?,
                    review_interval = 1,
                    mastered = 0,
                    status = 'learning',
                    error_category = ?,
                    debug_report_json = ?
                WHERE id = ?
                """,
                (f"{row['user_wrong_answer']} / {answer}", now, iso_date_after(1), error_category, report_json, mistake_id),
            )
        conn.commit()
    return jsonify(
        {
            "correct": is_correct,
            "correct_answer": answer_display_value(correct, answer if is_correct else None),
            "rule": form_rule_explanation(row, row["question_type"]),
            "next_review_date": iso_date_after(next_interval) if is_correct else iso_date_after(1),
            "review_interval": next_interval if is_correct else 1,
            "debug_report": None if is_correct else report,
        }
    )


@app.get("/api/mistakes/<int:mistake_id>/debug")
def api_mistake_debug(mistake_id):
    row = sqlite_one(
        """
        SELECT m.*, v.dictionary_form, v.reading, v.meaning, v.verb_group,
               v.te_form, v.ta_form, v.nai_form, v.renyou_form,
               v.shieki_form, v.ukemi_form, v.ba_form
        FROM mistake_logs m
        LEFT JOIN verbs v ON v.id = m.verb_id
        WHERE m.id = ?
        """,
        (mistake_id,),
    )
    if not row:
        return jsonify({"error": "找不到錯題紀錄。"}), 404
    if row.get("debug_report_json"):
        try:
            return jsonify(json.loads(row["debug_report_json"]))
        except json.JSONDecodeError:
            pass
    correct = row[row["question_type"]] if row.get("question_type") in QUESTION_TYPES else ""
    report = make_debug_report_payload(
        question_type=row.get("question_type", ""),
        prompt=f"請寫出「{row.get('dictionary_form') or row.get('question_type')}」的{VERB_FORM_LABELS.get(row.get('question_type'), row.get('question_type'))}。",
        user_answer=row.get("user_wrong_answer", ""),
        correct_answer=correct,
        target_text=row.get("dictionary_form", ""),
        target_reading=row.get("reading", ""),
        target_form=row.get("question_type", ""),
        error_category=row.get("error_category", ""),
    )
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        conn.execute("UPDATE mistake_logs SET debug_report_json = ? WHERE id = ?", (debug_report_to_json(report), mistake_id))
        conn.commit()
    return jsonify(report)


@app.post("/api/mistakes/generate-similar")
def api_generate_similar_mistakes():
    data = request.get_json(silent=True) or {}
    mistake_id = data.get("mistake_id")
    if not mistake_id:
        return jsonify({"error": "缺少錯題代號。"}), 400
    mistake = sqlite_one("SELECT * FROM mistake_logs WHERE id = ?", (mistake_id,))
    if not mistake:
        return jsonify({"error": "找不到錯題紀錄。"}), 404

    question_type = mistake.get("question_type", "")
    category = mistake.get("error_category", "")
    if question_type in QUESTION_TYPES and int(mistake.get("verb_id") or 0) > 0:
        source_verb = sqlite_one("SELECT * FROM verbs WHERE id = ?", (mistake["verb_id"],))
        if not source_verb:
            return jsonify({"message": "此題暫無法自動生成類似題", "items": []})
        rows = sqlite_dicts(
            """
            SELECT *
            FROM verbs
            WHERE verb_group = ? AND id != ?
            ORDER BY RANDOM()
            LIMIT 3
            """,
            (source_verb["verb_group"], source_verb["id"]),
        )
        items = [
            {
                "type": "verb",
                "verb_id": row["id"],
                "question_type": question_type,
                "prompt": f"請寫出「{row['dictionary_form']}（{row['reading']}）・{row['meaning']}」的{VERB_FORM_LABELS[question_type]}。",
                "answer": clean_answer_value(row[question_type]),
                "display_answer": answer_display_value(row[question_type]),
            }
            for row in rows
        ]
        return jsonify({"message": "已生成類似題。", "items": items})

    if question_type.startswith("sns_translation:") or category in {"SNS語感錯", "口語語感不自然", "中文直翻造成不自然", "直翻不自然"}:
        example_id = question_type.replace("sns_translation:", "") if question_type.startswith("sns_translation:") else ""
        source = find_sns_example(example_id) if example_id else None
        examples = load_sns_examples()
        if source:
            examples = [item for item in examples if item.get("tone_category") == source.get("tone_category") and item.get("id") != source.get("id")]
        else:
            examples = [item for item in examples if item.get("tone_category")]
        items = [
            {
                "type": "sns",
                "id": item["id"],
                "prompt": item["japanese"],
                "reference_translation": item["zh_tw_translation"],
                "tone_category": item["tone_category"],
                "literal_translation_trap": item["literal_translation_trap"],
            }
            for item in random.sample(examples, min(3, len(examples)))
        ]
        return jsonify({"message": "已生成 SNS 類似題。", "items": items})

    return jsonify({"message": "此題暫無法自動生成類似題", "items": []})


@app.post("/api/debug/grammar")
def api_debug_grammar():
    data = request.get_json(silent=True) or {}
    report = make_debug_report_payload(
        question_type=str(data.get("question_type", "")),
        prompt=str(data.get("prompt", "")),
        user_answer=str(data.get("user_answer", "")),
        correct_answer=str(data.get("correct_answer", "")),
        target_text=str(data.get("target_text", "")),
        target_reading=str(data.get("target_reading", "")),
        target_form=str(data.get("target_form", "")),
        error_category=str(data.get("error_category", "")),
        extra=data.get("extra") if isinstance(data.get("extra"), dict) else {},
    )
    return jsonify(report)


@app.post("/api/analyze_japanese")
def api_analyze_japanese():
    return handle_grammar_analyzer_api()


@app.post("/api/analyze_grammar")
def api_analyze_grammar():
    return handle_grammar_analyzer_api()


@app.get("/api/gemini/debug/model-check")
def api_gemini_debug_model_check():
    if not gemini_smoke_test_enabled():
        return jsonify({"error": "Gemini 模型測試端點未啟用。"}), 404
    models = []
    for model_name in gemini_model_candidates():
        result = smoke_test_gemini_model(model_name)
        models.append(result)
        print(
            "[grammar-analyzer] Gemini model smoke test；"
            f"model={result['model']}；status={result['status']}；"
            f"elapsed_ms={result['elapsed_ms']}；error_type={result['error_type']}"
        )
    recommended = next((item["model"] for item in models if item["status"] == "ok"), "")
    return jsonify(
        {
            "models": models,
            "recommended_model": recommended,
            "timeout_seconds": GEMINI_TIMEOUT_SECONDS,
            "candidate_count": len(models),
        }
    )


@app.get("/api/sns/random")
def api_sns_random():
    examples = load_sns_examples()
    if not examples:
        return jsonify({"error": "SNS 題庫尚未建立。"}), 404
    return jsonify(random.choice(examples))


@app.post("/api/sns/add_mistake")
def api_sns_add_mistake():
    data = request.get_json(silent=True) or {}
    example_id = str(data.get("id", "")).strip()
    user_translation = str(data.get("user_translation", "")).strip()
    if not example_id or not user_translation:
        return jsonify({"error": "請先輸入自己的繁體中文翻譯。"}), 400
    example = find_sns_example(example_id)
    if not example:
        return jsonify({"error": "找不到 SNS 例句。"}), 404
    add_or_update_sns_mistake(example, user_translation, "直翻不自然", 1)
    log_sns_practice(example, user_translation, "literal_translation", "直翻不自然")
    return jsonify({"success": True, "message": "已加入錯題紀錄。"})


@app.post("/api/sns/favorite")
def api_sns_favorite():
    data = request.get_json(silent=True) or {}
    example_id = str(data.get("id", "")).strip()
    note = str(data.get("note", "")).strip()
    example = find_sns_example(example_id)
    if not example:
        return jsonify({"error": "找不到 SNS 例句。"}), 404
    now = datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds")
    ensure_settings_store()
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        conn.execute(
            """
            INSERT INTO sns_favorites (sns_id, japanese, user_note, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (example_id, example["japanese"], note, now),
        )
        conn.commit()
    return jsonify({"success": True, "message": "已收藏此 SNS 例句。"})


@app.post("/api/sns/self-evaluate")
def api_sns_self_evaluate():
    data = request.get_json(silent=True) or {}
    example_id = str(data.get("example_id", "")).strip()
    user_translation = str(data.get("user_translation", "")).strip()
    self_evaluation = str(data.get("self_evaluation", "")).strip()
    if self_evaluation not in {"mastered", "nuance_off", "literal_translation", "skip"}:
        return jsonify({"error": "自我評估狀態不正確。"}), 400
    if not example_id:
        return jsonify({"error": "缺少 SNS 例句代號。"}), 400
    example = find_sns_example(example_id)
    if not example:
        return jsonify({"error": "找不到 SNS 例句。"}), 404

    error_category = ""
    report = None
    message = "已記錄本次自我評估。"
    if self_evaluation == "mastered":
        message = "已記錄為完全掌握，不加入錯題。"
    elif self_evaluation == "nuance_off":
        error_category = "口語語感不自然"
        add_or_update_sns_mistake(example, user_translation, error_category, 3)
        report = make_debug_report_payload(
            question_type=f"sns_translation:{example['id']}",
            prompt=example["japanese"],
            user_answer=user_translation,
            correct_answer=example.get("zh_tw_translation", ""),
            target_text=example["japanese"],
            target_reading=example.get("reading_hiragana", ""),
            target_form="sns_translation",
            error_category=error_category,
            extra={"literal_translation_trap": example.get("literal_translation_trap", ""), "natural_rewrite": example.get("natural_rewrite", "")},
        )
        message = "已加入錯題，3 天後安排複習。"
    elif self_evaluation == "literal_translation":
        error_category = "中文直翻造成不自然"
        add_or_update_sns_mistake(example, user_translation, error_category, 1)
        report = make_debug_report_payload(
            question_type=f"sns_translation:{example['id']}",
            prompt=example["japanese"],
            user_answer=user_translation,
            correct_answer=example.get("zh_tw_translation", ""),
            target_text=example["japanese"],
            target_reading=example.get("reading_hiragana", ""),
            target_form="sns_translation",
            error_category=error_category,
            extra={"literal_translation_trap": example.get("literal_translation_trap", ""), "natural_rewrite": example.get("natural_rewrite", "")},
        )
        message = "已加入錯題，明天安排複習。"

    log_sns_practice(example, user_translation, self_evaluation, error_category)
    return jsonify({"success": True, "message": message, "debug_report": report})


@app.get("/api/dashboard")
def api_dashboard():
    now_ts = taipei_now().timestamp()
    if _DASHBOARD_CACHE["payload"] is not None and _DASHBOARD_CACHE["expires_at"] and _DASHBOARD_CACHE["expires_at"] > now_ts:
        return jsonify(_DASHBOARD_CACHE["payload"])
    payload = build_dashboard_payload()
    _DASHBOARD_CACHE["payload"] = payload
    _DASHBOARD_CACHE["expires_at"] = now_ts + DASHBOARD_CACHE_TTL_SECONDS
    return jsonify(payload)


def build_dashboard_payload():
    settings = load_settings()
    today = today_string()
    today_material = material_by_date(today)
    df = read_database()
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    last_7_dates = [f"{(now - timedelta(days=i)).year}/{(now - timedelta(days=i)).month}/{(now - timedelta(days=i)).day}" for i in range(7)]
    material_dates = set(d for d in df["date"].drop_duplicates().tolist() if d)
    active_days = [date for date in last_7_dates if date in material_dates]
    today_iso = now.date().isoformat()
    today_mistakes = sqlite_dicts(
        """
        SELECT id FROM mistake_logs
        WHERE mastered = 0 AND substr(last_reviewed_at, 1, 10) = ?
        """,
        (today_iso,),
    )
    due_review = sqlite_one(
        """
        SELECT COUNT(*) AS count
        FROM mistake_logs
        WHERE mastered = 0 AND COALESCE(next_review_date, date(last_reviewed_at), ?) <= ?
        """,
        (today_iso_date(), today_iso_date()),
    )
    review_items = query_mistakes({}, limit=5)
    return {
        "today": today,
        "has_today_material": bool(today_material),
        "target_level": settings["target_level"],
        "vocab_count": len(today_material["vocabulary"]) if today_material else 0,
        "verb_count": len(today_material["verbs"]) if today_material else 0,
        "quiz_total": int(settings["mcq_count"]) + int(settings["fill_count"]),
        "today_new_mistakes": len(today_mistakes),
        "due_review_count": int(due_review["count"] if due_review else 0),
        "last_7_days": [{"date": date, "studied": date in material_dates} for date in reversed(last_7_dates)],
        "streak_days": len(active_days),
        "review_items": review_items,
    }


def table_status(table_name):
    ensure_settings_store()
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table_name,)).fetchone()
        if not exists:
            return "missing"
        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
    return "ok" if count else "missing"


def rows_since(table, days):
    start = rolling_start(days)
    return sqlite_dicts(f"SELECT * FROM {table} WHERE date(created_at) >= date(?)", (start,))


def mistake_rows_since(days):
    start = rolling_start(days)
    return sqlite_dicts(
        """
        SELECT *
        FROM mistake_logs
        WHERE date(last_reviewed_at) >= date(?)
        """,
        (start,),
    )


def material_days_since(days):
    start_date = taipei_now().date() - timedelta(days=days - 1)
    df = read_database()
    dates = set()
    if df.empty or "date" not in df.columns:
        return dates
    for value in df["date"].drop_duplicates().tolist():
        parsed = parse_material_date(value)
        if parsed and parsed >= start_date:
            dates.add(value)
    return dates


def summarize_period(days):
    quiz_rows = rows_since("quiz_records", days)
    mistakes = mistake_rows_since(days)
    sns_rows = rows_since("sns_practice_logs", days)
    total_questions = sum(int(row.get("total_questions") or 0) for row in quiz_rows)
    correct_count = sum(int(row.get("correct_count") or 0) for row in quiz_rows)
    accuracy = round(correct_count / total_questions * 100) if total_questions else None
    mistake_categories = Counter(row.get("error_category") or "未分類" for row in mistakes)
    verb_forms = Counter(
        VERB_FORM_LABELS.get(row.get("question_type"), row.get("question_type"))
        for row in mistakes
        if row.get("question_type") in QUESTION_TYPES
    )
    weak_sns = Counter(
        row.get("tone_category") or "未分類"
        for row in sns_rows
        if row.get("self_evaluation") in {"nuance_off", "literal_translation"}
    )
    naturalness_issues = Counter(
        row.get("error_category") or "未分類"
        for row in sns_rows
        if row.get("error_category")
    )
    mastered_count = sum(1 for row in mistakes if int(row.get("mastered") or 0) == 1 or row.get("status") == "mastered")
    return {
        "learning_days": len(material_days_since(days)),
        "completed_questions": total_questions,
        "accuracy": accuracy,
        "new_mistakes": len(mistakes),
        "mastered_mistakes": mastered_count,
        "top_error_categories": [{"category": key, "count": value} for key, value in mistake_categories.most_common(3)],
        "most_missed_verb_forms": [{"form": key, "count": value} for key, value in verb_forms.most_common(3)],
        "weakest_sns_tone": weak_sns.most_common(1)[0][0] if weak_sns else "資料不足",
        "translation_issue": naturalness_issues.most_common(1)[0][0] if naturalness_issues else "資料不足",
    }


def build_coach_suggestion(seven_days, thirty_days, health):
    suggestions = []
    if "missing" in health.values():
        suggestions.append("目前資料不足，請持續完成測驗、錯題複習與 SNS 語感練習，以解鎖更完整的教練分析。")
    if seven_days["most_missed_verb_forms"]:
        forms = "、".join(item["form"] for item in seven_days["most_missed_verb_forms"][:2])
        suggestions.append(f"建議下週優先複習{forms}，並搭配錯題本的類似題再訓練。")
    if seven_days["weakest_sns_tone"] != "資料不足":
        suggestions.append(f"SNS 語氣中「{seven_days['weakest_sns_tone']}」類型較容易誤解，建議加強直翻陷阱辨識。")
    if seven_days["translation_issue"] != "資料不足":
        suggestions.append(f"翻譯自然度最常見問題是「{seven_days['translation_issue']}」，練習時可先判斷語氣，再翻成自然繁中。")
    if not suggestions:
        suggestions.append("目前狀態穩定，建議維持每日教材與錯題複習節奏。")
    return suggestions


@app.get("/api/learning-report")
def api_learning_report():
    today = today_string()
    today_iso = today_iso_date()
    today_material = material_by_date(today)
    today_quiz = sqlite_one(
        """
        SELECT COALESCE(SUM(total_questions), 0) AS total,
               COALESCE(SUM(correct_count), 0) AS correct
        FROM quiz_records
        WHERE date(created_at) = date(?)
        """,
        (today_iso,),
    )
    today_mistakes = sqlite_one(
        "SELECT COUNT(*) AS count FROM mistake_logs WHERE date(last_reviewed_at) = date(?)",
        (today_iso,),
    )
    due_review = sqlite_one(
        """
        SELECT COUNT(*) AS count
        FROM mistake_logs
        WHERE mastered = 0 AND COALESCE(next_review_date, date(last_reviewed_at), ?) <= ?
        """,
        (today_iso, today_iso),
    )
    completed = int(today_quiz["total"] if today_quiz else 0)
    correct = int(today_quiz["correct"] if today_quiz else 0)
    seven = summarize_period(7)
    thirty = summarize_period(30)
    health = {
        "materials": "ok" if len(material_days_since(30)) else "missing",
        "quiz_records": table_status("quiz_records"),
        "mistake_logs": table_status("mistake_logs"),
        "sns_records": table_status("sns_practice_logs"),
    }
    if thirty["accuracy"] is None:
        trend = "資料不足，尚無法判斷進步趨勢。"
    elif thirty["accuracy"] >= 80:
        trend = "近 30 天答對率穩定偏高，可增加題量或提高 JLPT 等級。"
    elif thirty["accuracy"] >= 60:
        trend = "近 30 天表現中等，建議維持每日複習並針對高頻錯題加強。"
    else:
        trend = "近 30 天基礎仍不穩，建議先降低題量並集中處理最常錯類型。"
    thirty["progress_trend_summary"] = trend
    return jsonify(
        {
            "today": {
                "has_material": bool(today_material),
                "completed_questions": completed,
                "accuracy": round(correct / completed * 100) if completed else None,
                "new_mistakes": int(today_mistakes["count"] if today_mistakes else 0),
                "due_reviews": int(due_review["count"] if due_review else 0),
            },
            "rolling_7_days": seven,
            "rolling_30_days": thirty,
            "coach_suggestion": build_coach_suggestion(seven, thirty, health),
            "data_health": health,
        }
    )


@app.get("/api/slang/candidates")
def api_slang_candidates():
    status = request.args.get("status", "pending")
    limit = request.args.get("limit", 5)
    try:
        rows = query_slang_candidates(status=status, limit=limit)
    except Exception as e:
        log_slang_exception(f"讀取失敗：{e}")
        return jsonify({"error": "讀取新詞候選池失敗，請稍後再試。"}), 500
    return jsonify({"items": rows, "candidates": rows, "count": len(rows), "status": normalize_slang_status(status)})


@app.post("/api/slang/triage")
def api_slang_triage():
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "")).strip()
    if action not in {"approved", "rejected"}:
        return jsonify({"error": "審核動作不正確，請選擇核准或拒絕。"}), 400
    try:
        updated = update_slang_candidate_status(data.get("id"), action)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log_slang_exception(f"審核失敗：{e}")
        return jsonify({"error": "新詞審核失敗，請稍後再試。"}), 500
    if not updated:
        return jsonify({"error": "找不到指定的新詞候選。"}), 404
    return jsonify({"success": True, "status": action})


@app.get("/api/slang/debug/recent")
def api_slang_debug_recent():
    if not debug_endpoints_enabled():
        return jsonify({"error": "Debug endpoint 未啟用。"}), 404
    try:
        return jsonify(slang_debug_recent_snapshot())
    except Exception as e:
        log_slang_exception(f"debug recent 失敗：{e}")
        return jsonify({"error": "讀取 debug 狀態失敗。"}), 500


@app.post("/api/slang/debug/insert-test")
def api_slang_debug_insert_test():
    if not debug_endpoints_enabled():
        return jsonify({"error": "Debug endpoint 未啟用。"}), 404
    test_candidate = {
        "term": "めちゃくちゃ",
        "normalized_term": "めちゃくちゃ",
        "reading_hiragana": "めちゃくちゃ",
        "category": "slang",
        "meaning_zh": "非常、超級",
        "nuance": "常見口語強調用法",
        "confidence": 0.99,
        "should_add_to_candidates": True,
    }
    try:
        result = upsert_slang_candidates([test_candidate], source_context="debug_insert", source="debug_insert")
        snapshot = slang_debug_recent_snapshot(limit=10)
        return jsonify({"success": result.get("failed", 0) == 0, "result": result, "debug": snapshot})
    except Exception as e:
        log_slang_exception(f"debug insert-test 失敗：{e}")
        return jsonify({"success": False, "error": "Debug 寫入測試失敗。"}), 500


@app.get("/api/quiz")
def api_quiz():
    df = read_database()
    if len(df) < 2:
        return jsonify({"error": "資料太少，請先生成或累積幾天學習材料後再測驗。"})

    settings = load_settings()
    mcq_count = int(settings["mcq_count"])
    fill_count = int(settings["fill_count"])
    questions = []
    vocab_rows = df[(df["vocab_word"] != "") & (df["vocab_reading"] != "")]
    verb_rows = df[df["verb_base"] != ""]

    for _ in range(mcq_count):
        if vocab_rows.empty:
            break
        row = vocab_rows.sample(1).iloc[0]
        options = [row["vocab_reading"]]
        for reading in shuffled(vocab_rows["vocab_reading"].drop_duplicates().tolist()):
            if reading and reading not in options:
                options.append(reading)
            if len(options) >= 4:
                break
        for filler in ["たべもの", "でんしゃ", "あした", "べんきょう"]:
            if len(options) >= 4:
                break
            if filler not in options:
                options.append(filler)
        questions.append(
            {
                "type": "MCQ",
                "q": f"「{row['vocab_word']}」的正確讀音是哪一個？",
                "options": shuffled(options),
                "ans": row["vocab_reading"],
            }
        )

    forms = [
        ("連用形", "verb_masu_stem"),
        ("て形", "verb_te"),
        ("た形", "verb_ta"),
        ("ない形", "verb_nai"),
        ("ば形", "verb_ba"),
        ("使役形", "verb_causative"),
        ("被動形", "verb_passive"),
        ("使役被動形", "verb_causative_passive"),
    ]
    for _ in range(fill_count):
        if verb_rows.empty:
            break
        row = verb_rows.sample(1).iloc[0]
        form_name, column = random.choice(forms)
        base = row["verb_base"].split("-")[0].strip()
        questions.append({"type": "FILL", "q": f"請寫出「{base}」的 {form_name}。", "ans": clean_answer_value(row[column]), "displayAns": answer_display_value(row[column])})

    return jsonify(questions if questions else {"error": "目前沒有足夠資料可以產生測驗。"})


@app.post("/api/quiz/submit")
def api_quiz_submit():
    data = request.get_json(silent=True) or {}
    questions = data.get("questions") or []
    answers = data.get("answers") or []
    if not isinstance(questions, list) or not isinstance(answers, list):
        return jsonify({"error": "測驗資料格式不正確。"}), 400

    results = []
    score = 0
    for index, question in enumerate(questions):
        user_answer = answers[index] if index < len(answers) else ""
        correct_answer = question.get("ans", "")
        is_correct = smart_answer_equal(user_answer, correct_answer)
        if is_correct:
            score += 1
        results.append(
            {
                "correct": is_correct,
                "correct_answer": question.get("displayAns") or answer_display_value(correct_answer, user_answer if is_correct else None),
            }
        )
    ensure_settings_store()
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        conn.execute(
            """
            INSERT INTO quiz_records (created_at, total_questions, correct_count)
            VALUES (?, ?, ?)
            """,
            (taipei_iso_now(), len(questions), score),
        )
        conn.commit()
    invalidate_dashboard_cache("quiz submitted")
    return jsonify({"score": score, "total": len(questions), "results": results})


if __name__ == "__main__":
    ensure_database()
    ensure_settings_store()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
