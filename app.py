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
from werkzeug.exceptions import HTTPException
from services.grammar_debugger import debug_grammar


app = Flask(__name__, template_folder=".")


def api_error_payload(status_code, error, message):
    return {
        "ok": False,
        "error": error,
        "message": message,
        "path": request.path,
        "status": status_code,
    }


@app.errorhandler(404)
def handle_not_found(error):
    if request.path.startswith("/api/"):
        return jsonify(api_error_payload(404, "api_not_found", "API 路由不存在")), 404
    return error


@app.errorhandler(405)
def handle_method_not_allowed(error):
    if request.path.startswith("/api/"):
        return jsonify(api_error_payload(405, "method_not_allowed", "API 方法不允許")), 405
    return error


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        if request.path.startswith("/api/"):
            status_code = int(error.code or 500)
            message = "API 請求失敗"
            error_code = "http_error"
            if status_code == 404:
                message = "API 路由不存在"
                error_code = "api_not_found"
            elif status_code == 405:
                message = "API 方法不允許"
                error_code = "method_not_allowed"
            return jsonify(api_error_payload(status_code, error_code, message)), status_code
        return error
    if request.path.startswith("/api/"):
        print(f"[api-error] unhandled path={request.path}; reason={error}")
        print(traceback.format_exc())
        return jsonify(api_error_payload(500, "internal_server_error", "伺服器處理失敗，請查看 Render Logs")), 500
    raise error

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
VOCABULARY_SEED_BASIC_FILE = os.path.join(BASE_DIR, "data", "vocabulary_seed_n5_n3.json")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
GEMINI_TIMEOUT_SECONDS = read_int_env("GEMINI_TIMEOUT_SECONDS", 20, 5, 55)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview").strip()
GEMINI_MODEL_CANDIDATES = os.environ.get(
    "GEMINI_MODEL_CANDIDATES",
    "gemini-2.5-flash-lite,gemini-2.5-flash,gemini-2.0-flash-lite,gemini-2.0-flash",
).strip()
GEMINI_BILLING_BLOCK_SECONDS = read_int_env("GEMINI_BILLING_BLOCK_SECONDS", 600, 60, 86400)
TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:5000").rstrip("/")
CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()
DASHBOARD_CACHE_TTL_SECONDS = int(os.environ.get("DASHBOARD_CACHE_TTL_SECONDS", "90"))
ARCHIVE_DATES_CACHE_TTL_SECONDS = int(os.environ.get("ARCHIVE_DATES_CACHE_TTL_SECONDS", "60"))
_DASHBOARD_CACHE = {"expires_at": None, "payload": None}
_ARCHIVE_DATES_CACHE = {"expires_at": None, "payload": None}
_BASIC_SEED_VOCAB_CACHE = None
_SCHEMA_LOCK = threading.Lock()
_SETTINGS_SCHEMA_READY = False
_MATERIALS_SCHEMA_READY = False
_GEMINI_BILLING_LOCK = threading.Lock()
_GEMINI_BILLING_STATE = {
    "prepayment_depleted": False,
    "gemini_billing_block_until": 0.0,
    "last_model_check_ok_at": 0.0,
    "last_billing_status": "unknown",
    "last_recommended_model": "",
}

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
    "vocab_jlpt_level",
    "vocab_category",
    "vocab_normalized_key",
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

VOCAB_RULE_GROUPS = {
    "jlpt_level": "JLPT 等級",
    "category": "單字分類",
    "source": "資料來源",
    "quality": "品質",
    "part_of_speech": "詞性",
    "status": "Slang 狀態",
}
VOCAB_RULE_SOURCE_TYPES = set(VOCAB_RULE_GROUPS.keys())
EMPTY_RULE_VALUE = "__empty__"
EMPTY_RULE_LABELS = {
    "jlpt_level": "未分類 JLPT",
    "category": "未分類 category",
    "source": "未分類 source",
    "quality": "未設定 quality",
    "part_of_speech": "未分類詞性",
    "status": "未分類 status",
}
VOCAB_RULE_PERIODS = {"daily", "weekly", "monthly"}

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


def get_today_taipei_date():
    return today_string()


def material_date_display(value):
    parsed = parse_material_date(value)
    if parsed:
        return f"{parsed.year}/{parsed.month}/{parsed.day}"
    text = str(value or "").strip()
    return text or today_string()


def material_date_iso(value):
    parsed = parse_material_date(value)
    return parsed.isoformat() if parsed else ""


def material_date_variants(value):
    parsed = parse_material_date(value)
    text = str(value or "").strip()
    variants = []
    if parsed:
        variants.extend([f"{parsed.year}/{parsed.month}/{parsed.day}", parsed.isoformat()])
    elif text:
        variants.append(text)
    else:
        variants.append(today_string())
    output = []
    for item in variants:
        if item and item not in output:
            output.append(item)
    return output


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
    text = str(value or "").strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[: len(datetime.now().strftime(fmt))], fmt).date()
        except ValueError:
            pass
    for separator in ("/", "-"):
        parts = text.split(separator)
        if len(parts) >= 3 and all(part.isdigit() for part in parts[:3]):
            try:
                return datetime(int(parts[0]), int(parts[1]), int(parts[2])).date()
            except ValueError:
                return None
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


def invalidate_archive_dates_cache(reason=""):
    _ARCHIVE_DATES_CACHE["expires_at"] = None
    _ARCHIVE_DATES_CACHE["payload"] = None
    if reason:
        print(f"[archive-dates-cache] invalidated: {reason}")


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
    global _SETTINGS_SCHEMA_READY
    prepare_sqlite_path()
    if _SETTINGS_SCHEMA_READY and os.path.exists(SQLITE_SETTINGS_FILE):
        return
    with _SCHEMA_LOCK:
        if _SETTINGS_SCHEMA_READY and os.path.exists(SQLITE_SETTINGS_FILE):
            return
        _ensure_settings_store_uncached()
        _SETTINGS_SCHEMA_READY = True


def _ensure_settings_store_uncached():
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
        migrate_vocab_rules_sqlite(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_quiz_records_created_at ON quiz_records(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mistake_logs_last_reviewed_at ON mistake_logs(last_reviewed_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mistake_logs_next_review_date ON mistake_logs(next_review_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sns_practice_logs_created_at ON sns_practice_logs(created_at)")
        ensure_optional_sqlite_activity_indexes(conn)
        conn.commit()
    seed_verbs_if_empty()


def create_sqlite_index_if_possible(conn, table_name, index_name, columns):
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    if not exists:
        return
    existing_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if not set(columns).issubset(existing_columns):
        return
    conn.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name}({', '.join(columns)})")


def ensure_optional_sqlite_activity_indexes(conn):
    optional_indexes = [
        ("learning_logs", "idx_learning_logs_created_at", ["created_at"]),
        ("daily_records", "idx_daily_records_created_at", ["created_at"]),
        ("daily_records", "idx_daily_records_date", ["date"]),
        ("quiz_results", "idx_quiz_results_created_at", ["created_at"]),
        ("test_results", "idx_test_results_created_at", ["created_at"]),
        ("wrong_answers", "idx_wrong_answers_next_review_at", ["next_review_at"]),
        ("wrong_answer_reviews", "idx_wrong_answer_reviews_created_at", ["created_at"]),
        ("grammar_analysis_logs", "idx_grammar_analysis_logs_created_at", ["created_at"]),
        ("daily_activity_logs", "idx_daily_activity_logs_created_at", ["created_at"]),
        ("daily_material_views", "idx_daily_material_views_created_at", ["created_at"]),
    ]
    for table_name, index_name, columns in optional_indexes:
        create_sqlite_index_if_possible(conn, table_name, index_name, columns)


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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_slang_candidates_status_category ON slang_candidates(status, category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_slang_candidates_last_used_at ON slang_candidates(last_used_at)")


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
            verb_group INTEGER,
            conjugation_type TEXT DEFAULT '',
            quality TEXT DEFAULT 'normal',
            normalized_key TEXT,
            category TEXT DEFAULT 'general',
            cooldown_days INTEGER DEFAULT 14,
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
        "verb_group": "ALTER TABLE vocabulary_pool ADD COLUMN verb_group INTEGER",
        "conjugation_type": "ALTER TABLE vocabulary_pool ADD COLUMN conjugation_type TEXT DEFAULT ''",
        "quality": "ALTER TABLE vocabulary_pool ADD COLUMN quality TEXT DEFAULT 'normal'",
        "normalized_key": "ALTER TABLE vocabulary_pool ADD COLUMN normalized_key TEXT",
        "category": "ALTER TABLE vocabulary_pool ADD COLUMN category TEXT DEFAULT 'general'",
        "cooldown_days": "ALTER TABLE vocabulary_pool ADD COLUMN cooldown_days INTEGER DEFAULT 14",
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
            normalized_key = COALESCE(NULLIF(normalized_key, ''), NULLIF(base_form, ''), surface),
            category = COALESCE(NULLIF(category, ''), 'general'),
            cooldown_days = COALESCE(cooldown_days, 14),
            source = COALESCE(NULLIF(source, ''), 'manual'),
            quality = CASE
                WHEN quality IN ('core', 'normal', 'supplemental', 'experimental', 'rejected') AND quality != 'normal' THEN quality
                WHEN source IN ('seed_basic', 'jlpt_seed', 'manual', 'starter_pack') OR category IN ('general', 'jlpt_core', 'daily', 'common') THEN 'core'
                WHEN source IN ('seed_advanced', 'seed_advanced_synthetic', 'auto_generated') OR category IN ('business', 'advanced') THEN 'supplemental'
                ELSE COALESCE(NULLIF(quality, ''), 'normal')
            END,
            priority = COALESCE(priority, 1),
            is_active = COALESCE(is_active, 1),
            used_in_material_count = COALESCE(used_in_material_count, 0),
            created_at = COALESCE(NULLIF(created_at, ''), ?),
            updated_at = COALESCE(NULLIF(updated_at, ''), ?)
        """,
        (now, now),
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_base_level ON vocabulary_pool(base_form, jlpt_level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_normalized_key ON vocabulary_pool(normalized_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_category ON vocabulary_pool(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_quality ON vocabulary_pool(quality)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_level ON vocabulary_pool(jlpt_level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_active ON vocabulary_pool(is_active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_level_active ON vocabulary_pool(jlpt_level, is_active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_part_of_speech ON vocabulary_pool(part_of_speech)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_verb_group ON vocabulary_pool(verb_group)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_last_used_at ON vocabulary_pool(last_used_at)")


def migrate_vocab_rules_sqlite(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vocab_appearance_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_key TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            group_key TEXT NOT NULL,
            group_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            match_value TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            period TEXT DEFAULT 'daily',
            quota_count INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 50,
            max_per_material INTEGER,
            min_per_material INTEGER DEFAULT 0,
            strict_mode INTEGER DEFAULT 0,
            is_system_default INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vocab_selection_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material_date TEXT NOT NULL,
            vocabulary_id INTEGER,
            surface TEXT,
            base_form TEXT,
            normalized_key TEXT,
            rule_key TEXT,
            group_key TEXT,
            source_type TEXT,
            match_value TEXT,
            category TEXT,
            jlpt_level TEXT,
            source TEXT,
            quality TEXT,
            part_of_speech TEXT,
            selected_for TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_rules_rule_key ON vocab_appearance_rules(rule_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_rules_group_key ON vocab_appearance_rules(group_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_material_date ON vocab_selection_logs(material_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_rule_date ON vocab_selection_logs(rule_key, material_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_key_date ON vocab_selection_logs(normalized_key, material_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_group_date ON vocab_selection_logs(group_key, match_value, material_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_source_date ON vocab_selection_logs(source_type, match_value, material_date)")


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


def grammar_debug_enabled():
    return debug_endpoints_enabled() or gemini_smoke_test_enabled()


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


def boolish(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def clean_rule_match_value(value):
    text = str(value or "").strip()
    return text if text else EMPTY_RULE_VALUE


def rule_display_name(group_key, match_value):
    value = clean_rule_match_value(match_value)
    if value == EMPTY_RULE_VALUE:
        return EMPTY_RULE_LABELS.get(group_key, "未分類")
    return value


def make_vocab_rule_key(source_type, match_value):
    return f"{source_type}:{clean_rule_match_value(match_value)}"


def normalize_rule_period(value):
    period = str(value or "daily").strip().lower()
    return period if period in VOCAB_RULE_PERIODS else "daily"


def clamp_int(value, default=0, min_value=0, max_value=None):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default
    number = max(min_value, number)
    if max_value is not None:
        number = min(max_value, number)
    return number


def default_vocab_rule(source_type, match_value, available_count=0, is_system_default=False):
    source_type = source_type if source_type in VOCAB_RULE_SOURCE_TYPES else "custom"
    value = clean_rule_match_value(match_value)
    display = rule_display_name(source_type, value)
    group_name = VOCAB_RULE_GROUPS.get(source_type, "自訂類型")
    rule = {
        "rule_key": make_vocab_rule_key(source_type, value),
        "display_name": display,
        "group_key": source_type,
        "group_name": group_name,
        "source_type": source_type,
        "match_value": value,
        "enabled": True,
        "period": "daily",
        "quota_count": 0,
        "priority": 50,
        "max_per_material": None,
        "min_per_material": 0,
        "strict_mode": False,
        "is_system_default": bool(is_system_default),
        "available_count": int(available_count or 0),
    }
    lowered = value.lower()
    if value == EMPTY_RULE_VALUE:
        rule.update({"enabled": False, "period": "monthly", "quota_count": 0, "priority": 5, "max_per_material": 0, "strict_mode": True})
    elif source_type == "jlpt_level":
        presets = {
            "N5": ("daily", 2, 80, 2, False),
            "N4": ("daily", 2, 80, 2, False),
            "N3": ("daily", 6, 90, 6, False),
            "N2": ("weekly", 5, 55, 1, False),
            "N1": ("monthly", 8, 40, 1, False),
        }
        if value in presets:
            period, quota, priority, max_per_material, strict = presets[value]
            rule.update({"period": period, "quota_count": quota, "priority": priority, "max_per_material": max_per_material, "strict_mode": strict})
    elif source_type == "category":
        if lowered == "business":
            rule.update({"period": "weekly", "quota_count": 3, "priority": 35, "max_per_material": 1, "strict_mode": True})
        elif lowered == "advanced":
            rule.update({"period": "weekly", "quota_count": 2, "priority": 30, "max_per_material": 1, "strict_mode": True})
        elif lowered in {"internet_slang", "otaku_culture", "slang", "sns"}:
            rule.update({"period": "weekly", "quota_count": 2, "priority": 30, "max_per_material": 1, "strict_mode": True})
        elif lowered in {"generated_compound", "auto_generated", "synthetic", "unknown", "typo_or_noise", "sensitive", "named_entity"}:
            rule.update({"enabled": False, "period": "monthly", "quota_count": 0, "priority": 5, "max_per_material": 0, "strict_mode": True})
        elif lowered in {"general", "jlpt_core", "daily", "common"}:
            rule.update({"period": "daily", "quota_count": 20, "priority": 75, "max_per_material": None})
        else:
            rule.update({"period": "weekly", "quota_count": 1, "priority": 20, "max_per_material": 1, "strict_mode": True})
    elif source_type == "quality":
        if lowered == "core":
            rule.update({"period": "daily", "quota_count": 30, "priority": 85, "max_per_material": None})
        elif lowered == "normal":
            rule.update({"period": "daily", "quota_count": 20, "priority": 65, "max_per_material": None})
        elif lowered == "supplemental":
            rule.update({"period": "weekly", "quota_count": 4, "priority": 35, "max_per_material": 1})
        elif lowered == "experimental":
            rule.update({"enabled": False, "period": "monthly", "quota_count": 0, "priority": 5, "max_per_material": 0, "strict_mode": True})
        elif lowered == "rejected":
            rule.update({"enabled": False, "period": "monthly", "quota_count": 0, "priority": 0, "max_per_material": 0, "strict_mode": True})
    elif source_type == "source":
        if lowered in {"seed_basic", "jlpt_seed", "manual", "imported"}:
            rule.update({"period": "daily", "quota_count": 30, "priority": 70, "max_per_material": None})
        elif lowered in {"seed_advanced", "seed_sns", "grammar_analysis", "sns_capture"}:
            rule.update({"period": "weekly", "quota_count": 4, "priority": 35, "max_per_material": 1})
        elif lowered in {"auto_generated", "seed_advanced_synthetic"}:
            rule.update({"enabled": False, "period": "monthly", "quota_count": 0, "priority": 5, "max_per_material": 0, "strict_mode": True})
    return rule


def default_vocab_rule_seed():
    seeds = [
        ("jlpt_level", "N5"),
        ("jlpt_level", "N4"),
        ("jlpt_level", "N3"),
        ("jlpt_level", "N2"),
        ("jlpt_level", "N1"),
        ("category", "business"),
        ("category", "advanced"),
        ("category", "internet_slang"),
        ("category", "otaku_culture"),
        ("category", "generated_compound"),
        ("quality", "experimental"),
        ("quality", "rejected"),
    ]
    return [default_vocab_rule(source_type, value, is_system_default=True) for source_type, value in seeds]


def sanitize_vocab_rule_payload(rule):
    source_type = str(rule.get("source_type") or rule.get("group_key") or "").strip()
    if source_type not in VOCAB_RULE_SOURCE_TYPES:
        source_type = "category"
    match_value = clean_rule_match_value(rule.get("match_value"))
    base = default_vocab_rule(source_type, match_value)
    base.update(
        {
            "rule_key": rule.get("rule_key") or make_vocab_rule_key(source_type, match_value),
            "display_name": str(rule.get("display_name") or base["display_name"]).strip(),
            "group_key": source_type,
            "group_name": VOCAB_RULE_GROUPS.get(source_type, base["group_name"]),
            "source_type": source_type,
            "match_value": match_value,
            "enabled": boolish(rule.get("enabled", base["enabled"])),
            "period": normalize_rule_period(rule.get("period", base["period"])),
            "quota_count": clamp_int(rule.get("quota_count", base["quota_count"]), base["quota_count"], 0),
            "priority": clamp_int(rule.get("priority", base["priority"]), base["priority"], 0, 100),
            "max_per_material": None if rule.get("max_per_material", base["max_per_material"]) in (None, "") else clamp_int(rule.get("max_per_material"), 0, 0),
            "min_per_material": clamp_int(rule.get("min_per_material", base["min_per_material"]), base["min_per_material"], 0),
            "strict_mode": boolish(rule.get("strict_mode", base["strict_mode"])),
            "is_system_default": boolish(rule.get("is_system_default", base.get("is_system_default", False))),
        }
    )
    return base


def insert_or_update_vocab_rules(rules):
    if not rules:
        return {"saved": 0}
    ensure_vocab_rules_store()
    normalized = [sanitize_vocab_rule_payload(rule) for rule in rules]
    now = utc_now_iso()
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for rule in normalized:
                    cur.execute(
                        """
                        INSERT INTO vocab_appearance_rules (
                            rule_key, display_name, group_key, group_name, source_type, match_value,
                            enabled, period, quota_count, priority, max_per_material, min_per_material,
                            strict_mode, is_system_default, created_at, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (rule_key) DO UPDATE SET
                            display_name = EXCLUDED.display_name,
                            group_key = EXCLUDED.group_key,
                            group_name = EXCLUDED.group_name,
                            source_type = EXCLUDED.source_type,
                            match_value = EXCLUDED.match_value,
                            enabled = EXCLUDED.enabled,
                            period = EXCLUDED.period,
                            quota_count = EXCLUDED.quota_count,
                            priority = EXCLUDED.priority,
                            max_per_material = EXCLUDED.max_per_material,
                            min_per_material = EXCLUDED.min_per_material,
                            strict_mode = EXCLUDED.strict_mode,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (
                            rule["rule_key"],
                            rule["display_name"],
                            rule["group_key"],
                            rule["group_name"],
                            rule["source_type"],
                            rule["match_value"],
                            rule["enabled"],
                            rule["period"],
                            rule["quota_count"],
                            rule["priority"],
                            rule["max_per_material"],
                            rule["min_per_material"],
                            rule["strict_mode"],
                            rule["is_system_default"],
                            now,
                            now,
                        ),
                    )
            conn.commit()
    else:
        with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
            conn.executemany(
                """
                INSERT INTO vocab_appearance_rules (
                    rule_key, display_name, group_key, group_name, source_type, match_value,
                    enabled, period, quota_count, priority, max_per_material, min_per_material,
                    strict_mode, is_system_default, created_at, updated_at
                )
                VALUES (:rule_key, :display_name, :group_key, :group_name, :source_type, :match_value,
                    :enabled, :period, :quota_count, :priority, :max_per_material, :min_per_material,
                    :strict_mode, :is_system_default, :created_at, :updated_at)
                ON CONFLICT(rule_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    group_key = excluded.group_key,
                    group_name = excluded.group_name,
                    source_type = excluded.source_type,
                    match_value = excluded.match_value,
                    enabled = excluded.enabled,
                    period = excluded.period,
                    quota_count = excluded.quota_count,
                    priority = excluded.priority,
                    max_per_material = excluded.max_per_material,
                    min_per_material = excluded.min_per_material,
                    strict_mode = excluded.strict_mode,
                    updated_at = excluded.updated_at
                """,
                [
                    {
                        **rule,
                        "enabled": 1 if rule["enabled"] else 0,
                        "strict_mode": 1 if rule["strict_mode"] else 0,
                        "is_system_default": 1 if rule["is_system_default"] else 0,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for rule in normalized
                ],
            )
            conn.commit()
    return {"saved": len(normalized)}


def vocab_rules_count():
    ensure_vocab_rules_store()
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM vocab_appearance_rules")
                return int(cur.fetchone()[0] or 0)
    return int(sqlite_one("SELECT COUNT(*) AS count FROM vocab_appearance_rules")["count"])


def ensure_default_vocab_rules():
    if vocab_rules_count() == 0:
        insert_or_update_vocab_rules(default_vocab_rule_seed())


def load_vocab_rule_rows():
    ensure_default_vocab_rules()
    columns = """
        rule_key, display_name, group_key, group_name, source_type, match_value,
        enabled, period, quota_count, priority, max_per_material, min_per_material,
        strict_mode, is_system_default
    """
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {columns} FROM vocab_appearance_rules")
                rows = cur.fetchall()
        keys = [item.strip() for item in columns.replace("\n", "").split(",")]
        raw_rows = [dict(zip(keys, row)) for row in rows]
    else:
        raw_rows = sqlite_dicts(f"SELECT {columns} FROM vocab_appearance_rules")
    for row in raw_rows:
        row["enabled"] = boolish(row.get("enabled"))
        row["strict_mode"] = boolish(row.get("strict_mode"))
        row["is_system_default"] = boolish(row.get("is_system_default"))
        row["period"] = normalize_rule_period(row.get("period"))
        row["quota_count"] = clamp_int(row.get("quota_count"), 0, 0)
        row["priority"] = clamp_int(row.get("priority"), 50, 0, 100)
        row["min_per_material"] = clamp_int(row.get("min_per_material"), 0, 0)
        if row.get("max_per_material") in (None, ""):
            row["max_per_material"] = None
        else:
            row["max_per_material"] = clamp_int(row.get("max_per_material"), 0, 0)
    return raw_rows


def query_distinct_counts(table_name, field_name):
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT COALESCE(NULLIF({field_name}, ''), %s) AS value, COUNT(*)
                    FROM {table_name}
                    GROUP BY value
                    """,
                    (EMPTY_RULE_VALUE,),
                )
                return [(row[0], int(row[1] or 0)) for row in cur.fetchall()]
    ensure_settings_store()
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        rows = conn.execute(
            f"""
            SELECT COALESCE(NULLIF({field_name}, ''), ?) AS value, COUNT(*)
            FROM {table_name}
            GROUP BY value
            """,
            (EMPTY_RULE_VALUE,),
        ).fetchall()
    return [(row[0], int(row[1] or 0)) for row in rows]


def scan_vocab_rule_types():
    discovered = {}

    def add(source_type, value, count):
        value = clean_rule_match_value(value)
        key = make_vocab_rule_key(source_type, value)
        if key not in discovered:
            discovered[key] = default_vocab_rule(source_type, value, available_count=0)
        discovered[key]["available_count"] = discovered[key].get("available_count", 0) + int(count or 0)

    ensure_vocabulary_pool_store()
    for source_type in ("jlpt_level", "category", "source", "quality", "part_of_speech"):
        try:
            for value, count in query_distinct_counts("vocabulary_pool", source_type):
                add(source_type, value, count)
        except Exception as exc:
            print(f"[vocab-rules] scan skipped table=vocabulary_pool field={source_type}; reason={exc}")
    ensure_slang_candidates_store()
    for source_type in ("category", "source", "status"):
        try:
            for value, count in query_distinct_counts("slang_candidates", source_type):
                add(source_type, value, count)
        except Exception as exc:
            print(f"[vocab-rules] scan skipped table=slang_candidates field={source_type}; reason={exc}")
    return discovered


def period_bounds(period):
    today = taipei_now().date()
    period = normalize_rule_period(period)
    if period == "weekly":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif period == "monthly":
        start = today.replace(day=1)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end = start.replace(month=start.month + 1, day=1) - timedelta(days=1)
    else:
        start = today
        end = today
    return start.isoformat(), end.isoformat()


def vocab_rule_used_count(rule_key, period):
    start, end = period_bounds(period)
    ensure_vocab_rules_store()
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM vocab_selection_logs
                    WHERE rule_key = %s AND material_date BETWEEN %s AND %s
                    """,
                    (rule_key, start, end),
                )
                return int(cur.fetchone()[0] or 0)
    row = sqlite_one(
        """
        SELECT COUNT(*) AS count FROM vocab_selection_logs
        WHERE rule_key = ? AND material_date BETWEEN ? AND ?
        """,
        (rule_key, start, end),
    )
    return int(row["count"] if row else 0)


def build_vocab_rules_payload(create_missing=False):
    discovered = scan_vocab_rule_types()
    stored = {row["rule_key"]: row for row in load_vocab_rule_rows()}
    if create_missing:
        missing = [rule for key, rule in discovered.items() if key not in stored]
        if missing:
            insert_or_update_vocab_rules(missing)
            stored = {row["rule_key"]: row for row in load_vocab_rule_rows()}
    all_keys = set(discovered) | set(stored)
    grouped = {key: {"group_key": key, "group_name": VOCAB_RULE_GROUPS.get(key, key), "rules": []} for key in VOCAB_RULE_GROUPS}
    for key in sorted(all_keys):
        base = discovered.get(key) or default_vocab_rule((stored.get(key) or {}).get("source_type", "category"), (stored.get(key) or {}).get("match_value", ""))
        rule = {**base, **stored.get(key, {})}
        rule["available_count"] = int((discovered.get(key) or {}).get("available_count", rule.get("available_count", 0)) or 0)
        rule["is_new_detected_type"] = key not in stored
        used = vocab_rule_used_count(rule["rule_key"], rule.get("period", "daily"))
        quota = int(rule.get("quota_count", 0) or 0)
        rule["used_count"] = used
        rule["remaining_count"] = max(0, quota - used) if quota > 0 else None
        group_key = rule.get("group_key") or rule.get("source_type") or "category"
        grouped.setdefault(group_key, {"group_key": group_key, "group_name": VOCAB_RULE_GROUPS.get(group_key, group_key), "rules": []})
        grouped[group_key]["rules"].append(rule)
    groups = []
    for group_key in ("jlpt_level", "category", "source", "quality", "part_of_speech", "status"):
        group = grouped.get(group_key)
        if not group:
            continue
        group["rules"].sort(key=lambda rule: (not rule.get("is_new_detected_type"), -int(rule.get("available_count", 0) or 0), str(rule.get("display_name", ""))))
        groups.append(group)
    return {"groups": groups}


def load_vocab_rule_context():
    try:
        payload = build_vocab_rules_payload(create_missing=False)
        rules = {}
        for group in payload.get("groups", []):
            for rule in group.get("rules", []):
                rules[rule["rule_key"]] = rule
        return {"rules": rules}
    except Exception as exc:
        print(f"[vocab-rules] generation context unavailable; reason={exc}")
        return {"rules": {}}


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
            cur.execute("CREATE INDEX IF NOT EXISTS idx_slang_candidates_status_category ON slang_candidates(status, category)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_slang_candidates_last_used_at ON slang_candidates(last_used_at)")
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
                    verb_group INTEGER,
                    conjugation_type TEXT DEFAULT '',
                    quality TEXT DEFAULT 'normal',
                    normalized_key TEXT,
                    category TEXT DEFAULT 'general',
                    cooldown_days INTEGER DEFAULT 14,
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
                "verb_group": "INTEGER",
                "conjugation_type": "TEXT DEFAULT ''",
                "quality": "TEXT DEFAULT 'normal'",
                "normalized_key": "TEXT",
                "category": "TEXT DEFAULT 'general'",
                "cooldown_days": "INTEGER DEFAULT 14",
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
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_normalized_key ON vocabulary_pool(normalized_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_category ON vocabulary_pool(category)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_quality ON vocabulary_pool(quality)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_level ON vocabulary_pool(jlpt_level)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_active ON vocabulary_pool(is_active)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_level_active ON vocabulary_pool(jlpt_level, is_active)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_part_of_speech ON vocabulary_pool(part_of_speech)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_verb_group ON vocabulary_pool(verb_group)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocabulary_pool_last_used_at ON vocabulary_pool(last_used_at)")
            now = utc_now_iso()
            cur.execute(
                """
                UPDATE vocabulary_pool
                SET surface = COALESCE(NULLIF(surface, ''), base_form),
                    base_form = COALESCE(NULLIF(base_form, ''), surface),
                    normalized_key = COALESCE(NULLIF(normalized_key, ''), NULLIF(base_form, ''), surface),
                    category = COALESCE(NULLIF(category, ''), 'general'),
                    cooldown_days = COALESCE(cooldown_days, 14),
                    source = COALESCE(NULLIF(source, ''), 'manual'),
                    quality = CASE
                        WHEN quality IN ('core', 'normal', 'supplemental', 'experimental', 'rejected') AND quality != 'normal' THEN quality
                        WHEN source IN ('seed_basic', 'jlpt_seed', 'manual', 'starter_pack') OR category IN ('general', 'jlpt_core', 'daily', 'common') THEN 'core'
                        WHEN source IN ('seed_advanced', 'seed_advanced_synthetic', 'auto_generated') OR category IN ('business', 'advanced') THEN 'supplemental'
                        ELSE COALESCE(NULLIF(quality, ''), 'normal')
                    END,
                    priority = COALESCE(priority, 1),
                    is_active = COALESCE(is_active, TRUE),
                    used_in_material_count = COALESCE(used_in_material_count, 0),
                    created_at = COALESCE(created_at, %s),
                    updated_at = COALESCE(updated_at, %s)
                """,
                (now, now),
            )
        conn.commit()


def migrate_vocab_rules_postgres():
    if not DATABASE_URL:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS vocab_appearance_rules (
                    id BIGSERIAL PRIMARY KEY,
                    rule_key TEXT UNIQUE NOT NULL,
                    display_name TEXT NOT NULL,
                    group_key TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    match_value TEXT NOT NULL,
                    enabled BOOLEAN DEFAULT TRUE,
                    period TEXT DEFAULT 'daily',
                    quota_count INTEGER DEFAULT 0,
                    priority INTEGER DEFAULT 50,
                    max_per_material INTEGER,
                    min_per_material INTEGER DEFAULT 0,
                    strict_mode BOOLEAN DEFAULT FALSE,
                    is_system_default BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS vocab_selection_logs (
                    id BIGSERIAL PRIMARY KEY,
                    material_date DATE NOT NULL,
                    vocabulary_id BIGINT,
                    surface TEXT,
                    base_form TEXT,
                    normalized_key TEXT,
                    rule_key TEXT,
                    group_key TEXT,
                    source_type TEXT,
                    match_value TEXT,
                    category TEXT,
                    jlpt_level TEXT,
                    source TEXT,
                    quality TEXT,
                    part_of_speech TEXT,
                    selected_for TEXT,
                    created_at TIMESTAMPTZ
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_rules_rule_key ON vocab_appearance_rules(rule_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_rules_group_key ON vocab_appearance_rules(group_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_material_date ON vocab_selection_logs(material_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_rule_date ON vocab_selection_logs(rule_key, material_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_key_date ON vocab_selection_logs(normalized_key, material_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_group_date ON vocab_selection_logs(group_key, match_value, material_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_source_date ON vocab_selection_logs(source_type, match_value, material_date)")
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


def ensure_vocab_rules_store():
    if DATABASE_URL:
        migrate_vocab_rules_postgres()
    else:
        ensure_settings_store()


def ensure_database():
    global _MATERIALS_SCHEMA_READY
    if _MATERIALS_SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _MATERIALS_SCHEMA_READY:
            return
        _ensure_database_uncached()
        _MATERIALS_SCHEMA_READY = True


def _ensure_database_uncached():
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
                cur.execute("CREATE INDEX IF NOT EXISTS idx_materials_date ON materials(date)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_materials_created_at ON materials(created_at)")
            conn.commit()
        migrate_slang_candidates_postgres()
        migrate_vocabulary_pool_postgres()
        migrate_vocab_rules_postgres()
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


def read_material_rows_by_date(target_date):
    ensure_database()
    variants = material_date_variants(target_date)
    if DATABASE_URL:
        placeholders = ", ".join(["%s"] * len(variants))
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {', '.join(COLUMNS)} FROM materials WHERE date IN ({placeholders}) ORDER BY id", tuple(variants))
                rows = cur.fetchall()
        return pd.DataFrame(rows, columns=COLUMNS).astype(str) if rows else pd.DataFrame(columns=COLUMNS)
    df = read_database()
    return df[df["date"].isin(variants)]


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


def gemini_billing_block_message():
    return "Gemini API 暫時被帳務保護機制暫停，請稍後或執行 model-check 確認額度恢復。"


def timestamp_to_utc_iso(value):
    try:
        ts = float(value or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def gemini_billing_snapshot():
    now_ts = time.time()
    with _GEMINI_BILLING_LOCK:
        block_until = float(_GEMINI_BILLING_STATE.get("gemini_billing_block_until") or 0)
        if block_until and block_until <= now_ts:
            _GEMINI_BILLING_STATE["prepayment_depleted"] = False
            _GEMINI_BILLING_STATE["gemini_billing_block_until"] = 0.0
            _GEMINI_BILLING_STATE["last_billing_status"] = "expired"
            print("[gemini-billing] billing block expired; retrying gemini", flush=True)
        block_until = float(_GEMINI_BILLING_STATE.get("gemini_billing_block_until") or 0)
        return {
            "prepayment_depleted": bool(_GEMINI_BILLING_STATE.get("prepayment_depleted")),
            "gemini_billing_block_until": block_until,
            "gemini_billing_block_until_iso": timestamp_to_utc_iso(block_until),
            "billing_block_active": bool(block_until and block_until > now_ts),
            "last_model_check_ok_at": float(_GEMINI_BILLING_STATE.get("last_model_check_ok_at") or 0),
            "last_model_check_ok_at_iso": timestamp_to_utc_iso(
                _GEMINI_BILLING_STATE.get("last_model_check_ok_at")
            ),
            "last_billing_status": str(_GEMINI_BILLING_STATE.get("last_billing_status") or "unknown"),
            "last_recommended_model": str(_GEMINI_BILLING_STATE.get("last_recommended_model") or ""),
        }


def clear_gemini_billing_block(recommended_model="", reason=""):
    with _GEMINI_BILLING_LOCK:
        _GEMINI_BILLING_STATE["prepayment_depleted"] = False
        _GEMINI_BILLING_STATE["gemini_billing_block_until"] = 0.0
        _GEMINI_BILLING_STATE["last_model_check_ok_at"] = time.time()
        _GEMINI_BILLING_STATE["last_billing_status"] = "ok"
        if recommended_model:
            _GEMINI_BILLING_STATE["last_recommended_model"] = recommended_model
    print("[gemini-billing] model-check success; clearing billing block", flush=True)
    if reason:
        print(f"[gemini-billing] clear reason={reason}", flush=True)


def set_gemini_billing_block(reason="prepayment_depleted"):
    block_until = time.time() + GEMINI_BILLING_BLOCK_SECONDS
    with _GEMINI_BILLING_LOCK:
        _GEMINI_BILLING_STATE["prepayment_depleted"] = True
        _GEMINI_BILLING_STATE["gemini_billing_block_until"] = block_until
        _GEMINI_BILLING_STATE["last_billing_status"] = "prepayment_depleted"
    print(
        f"[gemini-billing] prepayment depleted; blocking gemini until={timestamp_to_utc_iso(block_until)}; reason={reason}",
        flush=True,
    )


def is_prepayment_depleted_error(error):
    lower = str(error or "").lower()
    return (
        ("prepayment" in lower and ("deplet" in lower or "credit" in lower))
        or "credits are depleted" in lower
        or "credit balance" in lower
        or "prepayment credits" in lower
    )


def gemini_error_type(error):
    text = str(error or "")
    lower = text.lower()
    if "尚未設定" in text or "missing api key" in lower or "api key missing" in lower:
        return "missing_api_key"
    if "timeout" in lower or "timed out" in lower or "逾時" in text:
        return "timeout"
    if is_prepayment_depleted_error(error):
        return "prepayment_depleted"
    if "quota" in lower or "resource_exhausted" in lower or "429" in lower:
        return "quota_exceeded"
    if "not_found" in lower or "not found" in lower or "404" in lower:
        return "not_found"
    if "permission" in lower or "unauthorized" in lower or "403" in lower:
        return "permission_denied"
    if "json" in lower:
        return "json_parse_error"
    if "unavailable" in lower or "503" in lower or "high demand" in lower:
        return "model_error"
    if "格式" in text or "空內容" in text:
        return "model_error"
    if "連接" in text or "connection" in lower:
        return "model_error"
    return "unknown_error"


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
    return gemini_error_type(error)


def choose_gemini_failure_reason(failures):
    priority = [
        "prepayment_depleted",
        "missing_api_key",
        "quota_exceeded",
        "timeout",
        "permission_denied",
        "not_found",
        "json_parse_error",
        "model_error",
        "unknown_error",
    ]
    error_types = {item.get("error_type") for item in failures or [] if isinstance(item, dict)}
    for reason in priority:
        if reason in error_types:
            return reason
    return next((item.get("error_type") for item in failures or [] if isinstance(item, dict) and item.get("error_type")), "unknown_error")


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
                "category": row.get("vocab_category", "") or "materials",
                "normalized_key": normalize_vocab_key(row.get("vocab_normalized_key", "") or word),
                "example_sentence": row.get("vocab_example_sentence", ""),
                "example_translation_zh": row.get("vocab_example_translation_zh", ""),
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
            "jlpt_level": "",
            "category": item.get("category", "sns"),
            "normalized_key": normalize_vocab_key(item.get("normalized_term") or item.get("term", "")),
            "source": "slang_candidates",
            "_slang_id": item.get("id"),
        }
        for item in selected
        if item.get("term")
    ]
    return items


def dedupe_vocab_items(items, existing_keys=None):
    selected = []
    seen = set(existing_keys or [])
    duplicate_count = 0
    for item in items or []:
        key = item_normalized_key(item)
        if key and key in seen:
            duplicate_count += 1
            continue
        if key:
            seen.add(key)
            item["normalized_key"] = key
        selected.append(item)
    return selected, duplicate_count, seen


def first_text(row, names):
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_vocab_key(value):
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"\s+", "", text)
    buzz_base = "\u30d0\u30ba\u308b"
    buzz_variants = (
        buzz_base,
        "\u30d0\u30ba\u3063\u305f",
        "\u30d0\u30ba\u308a\u305d\u3046",
        "\u30d0\u30ba\u3063\u3066\u308b",
        "\u30d0\u30ba\u308a",
        "\u30d0\u30ba\u308c",
    )
    if text == buzz_base or any(text.startswith(variant) for variant in buzz_variants):
        return buzz_base
    return text


def item_normalized_key(item):
    if not isinstance(item, dict):
        return ""
    for key in ("normalized_key", "normalized_term", "base_form", "surface", "term", "word"):
        value = item.get(key)
        if value:
            return normalize_vocab_key(value)
    return ""


def jlpt_level_rank(level):
    try:
        return int(str(level or "").upper().replace("N", ""))
    except ValueError:
        return 9


def preferred_level_distance(target, level):
    if not target or not level:
        return 2
    target_rank = jlpt_level_rank(target)
    level_rank = jlpt_level_rank(level)
    if target_rank == level_rank:
        return 0
    if abs(target_rank - level_rank) == 1:
        return 1
    return 3 if level_rank < target_rank else 2


def normalize_vocabulary_item(raw):
    raw = dict(raw or {})
    surface = first_text(raw, ["surface", "term", "word", "vocab_word", "base_form"])
    base_form = first_text(raw, ["base_form", "dictionary_form", "surface", "term", "word", "vocab_word"]) or surface
    normalized_key = normalize_vocab_key(first_text(raw, ["normalized_key", "normalized_term", "base_form", "surface", "term", "word"]) or base_form or surface)
    if not surface or not base_form:
        return None
    try:
        priority = int(raw.get("priority", 1) or 1)
    except (TypeError, ValueError):
        priority = 1
    try:
        verb_group = int(raw.get("verb_group") or 0) or None
    except (TypeError, ValueError):
        verb_group = None
    quality = first_text(raw, ["quality"]) or ""
    if not quality:
        source_hint = first_text(raw, ["source"]).lower()
        category_hint = first_text(raw, ["category"]).lower()
        if source_hint in {"seed_basic", "jlpt_seed", "manual"} or category_hint in {"general", "jlpt_core", "daily", "common"}:
            quality = "core"
        elif source_hint in {"seed_advanced", "seed_advanced_synthetic", "auto_generated"} or category_hint in {"business", "advanced"}:
            quality = "supplemental"
        else:
            quality = "normal"
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
            "verb_group": verb_group,
            "conjugation_type": first_text(raw, ["conjugation_type", "inflection_type"]),
            "quality": quality,
            "normalized_key": normalized_key,
            "category": first_text(raw, ["category"]) or "general",
            "cooldown_days": int(raw.get("cooldown_days", 14) or 14),
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
    result = {"success": 0, "failed": 0, "skipped": 0, "inserted_count": 0, "updated_count": 0, "total_count": len(normalized_items)}
    if not normalized_items:
        return result
    ensure_vocabulary_pool_store()
    if DATABASE_URL:
        with get_db_connection() as conn:
            for item in normalized_items:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT id FROM vocabulary_pool WHERE normalized_key = %s LIMIT 1",
                            (item["normalized_key"],),
                        )
                        existing = cur.fetchone()
                        if existing:
                            cur.execute(
                                """
                                UPDATE vocabulary_pool
                                SET surface = COALESCE(NULLIF(surface, ''), %s),
                                    base_form = COALESCE(NULLIF(base_form, ''), %s),
                                    reading_hiragana = COALESCE(NULLIF(reading_hiragana, ''), %s),
                                    meaning_zh = COALESCE(NULLIF(meaning_zh, ''), %s),
                                    part_of_speech = COALESCE(NULLIF(part_of_speech, ''), %s),
                                    jlpt_level = COALESCE(NULLIF(jlpt_level, ''), %s),
                                    verb_group = COALESCE(verb_group, %s),
                                    conjugation_type = COALESCE(NULLIF(conjugation_type, ''), %s),
                                    quality = COALESCE(NULLIF(quality, ''), %s),
                                    category = COALESCE(NULLIF(category, ''), %s),
                                    cooldown_days = COALESCE(cooldown_days, %s),
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
                                    item["base_form"],
                                    item["reading_hiragana"],
                                    item["meaning_zh"],
                                    item["part_of_speech"],
                                    item["jlpt_level"],
                                    item["verb_group"],
                                    item["conjugation_type"],
                                    item["quality"],
                                    item["category"],
                                    item["cooldown_days"],
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
                                    surface, base_form, normalized_key, reading_hiragana, meaning_zh, part_of_speech,
                                    jlpt_level, verb_group, conjugation_type, quality, category, cooldown_days, example_sentence, example_translation_zh, source, priority,
                                    is_active, used_in_material_count, last_used_at, created_at, updated_at
                                )
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    item["surface"],
                                    item["base_form"],
                                    item["normalized_key"],
                                    item["reading_hiragana"],
                                    item["meaning_zh"],
                                    item["part_of_speech"],
                                    item["jlpt_level"],
                                    item["verb_group"],
                                    item["conjugation_type"],
                                    item["quality"],
                                    item["category"],
                                    item["cooldown_days"],
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
                    result["updated_count" if existing else "inserted_count"] += 1
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
                    "SELECT id FROM vocabulary_pool WHERE normalized_key = ? LIMIT 1",
                    (item["normalized_key"],),
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE vocabulary_pool
                        SET surface = COALESCE(NULLIF(surface, ''), ?),
                            base_form = COALESCE(NULLIF(base_form, ''), ?),
                            reading_hiragana = COALESCE(NULLIF(reading_hiragana, ''), ?),
                            meaning_zh = COALESCE(NULLIF(meaning_zh, ''), ?),
                            part_of_speech = COALESCE(NULLIF(part_of_speech, ''), ?),
                            jlpt_level = COALESCE(NULLIF(jlpt_level, ''), ?),
                            verb_group = COALESCE(verb_group, ?),
                            conjugation_type = COALESCE(NULLIF(conjugation_type, ''), ?),
                            quality = COALESCE(NULLIF(quality, ''), ?),
                            category = COALESCE(NULLIF(category, ''), ?),
                            cooldown_days = COALESCE(cooldown_days, ?),
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
                            item["base_form"],
                            item["reading_hiragana"],
                            item["meaning_zh"],
                            item["part_of_speech"],
                            item["jlpt_level"],
                            item["verb_group"],
                            item["conjugation_type"],
                            item["quality"],
                            item["category"],
                            item["cooldown_days"],
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
                            surface, base_form, normalized_key, reading_hiragana, meaning_zh, part_of_speech,
                            jlpt_level, verb_group, conjugation_type, quality, category, cooldown_days, example_sentence, example_translation_zh, source, priority,
                            is_active, used_in_material_count, last_used_at, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item["surface"],
                            item["base_form"],
                            item["normalized_key"],
                            item["reading_hiragana"],
                            item["meaning_zh"],
                            item["part_of_speech"],
                            item["jlpt_level"],
                            item["verb_group"],
                            item["conjugation_type"],
                            item["quality"],
                            item["category"],
                            item["cooldown_days"],
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
                result["updated_count" if existing else "inserted_count"] += 1
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
                          AND COALESCE(category, 'general') NOT IN ('named_entity', 'sensitive', 'typo_or_noise', 'unknown')
                        ORDER BY COALESCE(used_in_material_count, 0) ASC,
                                 last_used_at ASC NULLS FIRST,
                                 priority DESC,
                                 id DESC
                        LIMIT 12000
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
                  AND COALESCE(category, 'general') NOT IN ('named_entity', 'sensitive', 'typo_or_noise', 'unknown')
                ORDER BY COALESCE(used_in_material_count, 0) ASC,
                         COALESCE(last_used_at, '') ASC,
                         priority DESC,
                         id DESC
                LIMIT 12000
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


GENERAL_VOCAB_CATEGORIES = {"general", "jlpt_core", "daily", "common", "seed", ""}
BUSINESS_VOCAB_CATEGORIES = {"business"}
ADVANCED_VOCAB_CATEGORIES = {"advanced"}
SNS_VOCAB_CATEGORIES = {"sns", "internet_slang", "otaku_culture"}
CORE_VOCAB_SOURCES = {"seed_basic", "jlpt_seed", "manual", "starter_pack"}
LOW_QUALITY_COMPOUND_SUFFIXES = (
    "導入策",
    "更新案",
    "整理力",
    "確認率",
    "検証性",
    "化力",
    "案",
    "力",
    "率",
    "性",
    "策",
)
LOW_QUALITY_MEANING_HINTS = ("方案", "能力", "策略", "驗證性", "更新方案")


def vocab_quality(row):
    quality = first_text(row, ["quality"]).lower()
    if quality:
        return quality
    source = first_text(row, ["source"]).lower()
    category = first_text(row, ["category"]).lower()
    if source in CORE_VOCAB_SOURCES or category in GENERAL_VOCAB_CATEGORIES:
        return "core"
    if source in {"seed_advanced", "seed_advanced_synthetic", "auto_generated"} or category in BUSINESS_VOCAB_CATEGORIES | ADVANCED_VOCAB_CATEGORIES:
        return "supplemental"
    return "normal"


def vocab_category_group(item):
    category = str(item.get("category") or "").strip().lower()
    source = str(item.get("source") or "").strip().lower()
    if category in SNS_VOCAB_CATEGORIES:
        return "sns"
    if category in BUSINESS_VOCAB_CATEGORIES:
        return "business"
    if category in ADVANCED_VOCAB_CATEGORIES:
        return "advanced"
    if source in CORE_VOCAB_SOURCES or category in GENERAL_VOCAB_CATEGORIES:
        return "general"
    return "general"


def is_low_quality_compound_word(row):
    surface = first_text(row, ["surface", "base_form", "term", "word"])
    meaning = first_text(row, ["meaning_zh", "meaning_zh_tw", "meaning", "vocab_meaning"])
    category = first_text(row, ["category"]).lower()
    source = first_text(row, ["source"]).lower()
    quality = vocab_quality(row)
    if quality == "rejected":
        return True, "quality_rejected"
    if quality == "experimental":
        return True, "quality_experimental"
    if source in {"auto_generated", "seed_advanced_synthetic"}:
        return True, "synthetic_source"
    if category not in BUSINESS_VOCAB_CATEGORIES | ADVANCED_VOCAB_CATEGORIES and source != "seed_advanced":
        return False, ""
    if any(surface.endswith(suffix) for suffix in LOW_QUALITY_COMPOUND_SUFFIXES):
        return True, "mechanical_suffix"
    if len(re.findall(r"[\u4e00-\u9fff]", surface)) >= 6:
        return True, "compound_too_long"
    if any(hint in meaning for hint in LOW_QUALITY_MEANING_HINTS):
        return True, "mechanical_meaning"
    return False, ""


def vocab_category_quotas(limit, mode="general"):
    if mode == "business":
        return {
            "general_min": max(1, int(limit * 0.55)),
            "business_max": max(1, int(limit * 0.30)),
            "advanced_max": max(1, int(limit * 0.15)),
            "sns_max": max(0, int(limit * 0.10)),
        }
    if mode == "sns":
        return {
            "general_min": max(1, int(limit * 0.65)),
            "business_max": max(0, int(limit * 0.10)),
            "advanced_max": max(0, int(limit * 0.10)),
            "sns_max": max(1, int(limit * 0.20)),
        }
    return {
        "general_min": max(1, int((limit * 7 + 9) // 10)),
        "business_max": max(0, min(2, int(limit * 0.20))),
        "advanced_max": max(0, min(1, int(limit * 0.15))),
        "sns_max": 1 if limit >= 8 else 0,
    }


def item_rule_values(item):
    return {
        "jlpt_level": clean_rule_match_value(first_text(item, ["jlpt_level", "target_level", "level"])),
        "category": clean_rule_match_value(first_text(item, ["category"])),
        "source": clean_rule_match_value(first_text(item, ["_raw_source", "source"])),
        "quality": clean_rule_match_value(first_text(item, ["quality"])),
        "part_of_speech": clean_rule_match_value(first_text(item, ["part_of_speech", "pos"])),
    }


def matching_vocab_rules(item, rule_context):
    rules = (rule_context or {}).get("rules", {})
    matches = []
    for source_type, value in item_rule_values(item).items():
        rule = rules.get(make_vocab_rule_key(source_type, value))
        if rule:
            matches.append(rule)
    return matches


def can_select_vocab_by_rules(item, rule_context, selected_rule_counts):
    matches = matching_vocab_rules(item, rule_context)
    if item_rule_values(item).get("quality") == "rejected":
        return False, "quality_rejected", matches
    for rule in matches:
        rule_key = rule["rule_key"]
        strict = boolish(rule.get("strict_mode"))
        enabled = boolish(rule.get("enabled"))
        quota = int(rule.get("quota_count", 0) or 0)
        used = int(rule.get("used_count", 0) or 0)
        selected_count = int(selected_rule_counts.get(rule_key, 0) or 0)
        max_per_material = rule.get("max_per_material")
        if not enabled and strict:
            return False, f"disabled_strict:{rule_key}", matches
        if enabled and quota > 0 and used + selected_count >= quota:
            return False, f"period_quota_reached:{rule_key}", matches
        if enabled and strict and quota == 0:
            return False, f"zero_quota_strict:{rule_key}", matches
        if max_per_material not in (None, "") and selected_count >= int(max_per_material or 0):
            return False, f"material_quota_reached:{rule_key}", matches
    return True, "", matches


def vocab_rule_priority(item, rule_context):
    matches = matching_vocab_rules(item, rule_context)
    enabled_priorities = [int(rule.get("priority", 50) or 50) for rule in matches if boolish(rule.get("enabled"))]
    return max(enabled_priorities) if enabled_priorities else 50


def record_vocab_selection_logs(items, selected_for="word"):
    rows = []
    material_date = today_iso_date()
    now = utc_now_iso()
    for item in items:
        rule_keys = item.get("_matched_rule_keys") or []
        if not rule_keys:
            continue
        values = item_rule_values(item)
        for rule_key in rule_keys:
            source_type, match_value = rule_key.split(":", 1) if ":" in rule_key else ("custom", rule_key)
            rows.append(
                {
                    "material_date": material_date,
                    "vocabulary_id": item.get("_pool_id"),
                    "surface": item.get("word", ""),
                    "base_form": item.get("base_form", item.get("word", "")),
                    "normalized_key": item_normalized_key(item),
                    "rule_key": rule_key,
                    "group_key": source_type,
                    "source_type": source_type,
                    "match_value": match_value,
                    "category": values.get("category", EMPTY_RULE_VALUE),
                    "jlpt_level": values.get("jlpt_level", EMPTY_RULE_VALUE),
                    "source": values.get("source", EMPTY_RULE_VALUE),
                    "quality": values.get("quality", EMPTY_RULE_VALUE),
                    "part_of_speech": values.get("part_of_speech", EMPTY_RULE_VALUE),
                    "selected_for": selected_for,
                    "created_at": now,
                }
            )
    if not rows:
        return
    ensure_vocab_rules_store()
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO vocab_selection_logs (
                        material_date, vocabulary_id, surface, base_form, normalized_key, rule_key,
                        group_key, source_type, match_value, category, jlpt_level, source,
                        quality, part_of_speech, selected_for, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            row["material_date"],
                            row["vocabulary_id"],
                            row["surface"],
                            row["base_form"],
                            row["normalized_key"],
                            row["rule_key"],
                            row["group_key"],
                            row["source_type"],
                            row["match_value"],
                            row["category"],
                            row["jlpt_level"],
                            row["source"],
                            row["quality"],
                            row["part_of_speech"],
                            row["selected_for"],
                            row["created_at"],
                        )
                        for row in rows
                    ],
                )
            conn.commit()
        return
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        conn.executemany(
            """
            INSERT INTO vocab_selection_logs (
                material_date, vocabulary_id, surface, base_form, normalized_key, rule_key,
                group_key, source_type, match_value, category, jlpt_level, source,
                quality, part_of_speech, selected_for, created_at
            )
            VALUES (:material_date, :vocabulary_id, :surface, :base_form, :normalized_key, :rule_key,
                :group_key, :source_type, :match_value, :category, :jlpt_level, :source,
                :quality, :part_of_speech, :selected_for, :created_at)
            """,
            rows,
        )
        conn.commit()


def material_vocab_from_vocabulary_pool(settings, limit, exclude_keys=None, return_stats=False):
    stats = {
        "rejected_low_quality_count": 0,
        "rejected_by_rule_count": 0,
        "category_counts": {},
        "candidate_counts": {},
        "selected_rule_counts": {},
        "rule_remaining_after_generation": {},
    }
    if limit <= 0:
        return ([], stats) if return_stats else []
    rows = fetch_vocabulary_pool_rows()
    if not rows:
        return ([], stats) if return_stats else []

    target = settings.get("target_level", "")
    mode = settings.get("vocab_mode", "general")
    quotas = vocab_category_quotas(limit, mode)
    buckets = {"general": [], "business": [], "advanced": [], "sns": []}
    rule_context = load_vocab_rule_context()
    rule_enabled = bool(rule_context.get("rules"))
    rule_selected_counts = {}
    rule_rejection_log_count = 0
    candidates = []
    seen = {normalize_vocab_key(key) for key in (exclude_keys or set()) if key}
    today = taipei_now().date()
    rejected_log_count = 0
    for row in rows:
        is_active = first_text(row, ["is_active", "active", "enabled"])
        if is_active and is_active.lower() in {"0", "false", "no", "off"}:
            continue
        low_quality, reason = is_low_quality_compound_word(row)
        if low_quality:
            stats["rejected_low_quality_count"] += 1
            if rejected_log_count < 25:
                rejected_log_count += 1
                print(
                    "[vocab-selector] rejected low quality compound "
                    f"surface={first_text(row, ['surface', 'base_form', 'term', 'word'])} reason={reason}"
                )
            continue
        quality = vocab_quality(row)
        if quality in {"experimental", "rejected"}:
            continue
        word = first_text(row, ["term", "surface", "word", "vocab_word", "dictionary_form"])
        normalized_key = normalize_vocab_key(first_text(row, ["normalized_key", "normalized_term", "base_form", "surface", "term", "word"]) or word)
        if not word or not normalized_key or normalized_key in seen:
            continue
        seen.add(normalized_key)
        row_level = first_text(row, ["jlpt_level", "target_level", "level"])
        priority = first_text(row, ["priority", "weight"])
        try:
            priority_value = int(float(priority)) if priority else 0
        except ValueError:
            priority_value = 0
        try:
            cooldown_days = max(0, int(row.get("cooldown_days", 14) or 14))
        except (TypeError, ValueError):
            cooldown_days = 14
        last_used = parse_loose_date(first_text(row, ["last_used_at", "last_seen_at"]))
        in_cooldown = bool(last_used and (today - last_used).days < cooldown_days)
        cooldown_penalty = 1 if in_cooldown else 0
        next_review = parse_loose_date(first_text(row, ["next_review_at", "next_review_date", "review_at"]))
        due_rank = 0 if not next_review or next_review <= taipei_now().date() else 1
        level_distance = preferred_level_distance(target, row_level)
        category = first_text(row, ["category"]) or "general"
        source = first_text(row, ["source"]) or "vocabulary_pool"
        item = {
            "word": word,
            "base_form": first_text(row, ["base_form", "surface", "term", "word"]) or word,
            "reading": first_text(row, ["reading_hiragana", "reading", "kana", "vocab_reading"]),
            "meaning": first_text(row, ["meaning_zh", "meaning_zh_tw", "meaning", "vocab_meaning"]),
            "part_of_speech": first_text(row, ["part_of_speech", "pos"]),
            "jlpt_level": row_level,
            "category": category,
            "quality": quality,
            "normalized_key": normalized_key,
            "example_sentence": first_text(row, ["example_sentence", "example_japanese"]),
            "example_translation_zh": first_text(row, ["example_translation_zh", "example_chinese", "example_translation"]),
            "source": "vocabulary_pool",
            "_raw_source": source,
            "_pool_id": row.get("id"),
            "_pool_has_last_seen": "last_seen_at" in row,
            "_pool_has_last_used": "last_used_at" in row,
            "_pool_has_used_count": "used_in_material_count" in row,
            "_sort": (
                0 if quality == "core" else 1 if quality == "normal" else 2,
                0 if source in CORE_VOCAB_SOURCES else 1,
                0 if first_text(row, ["meaning_zh", "meaning_zh_tw", "meaning", "vocab_meaning"]) else 1,
                0 if first_text(row, ["reading_hiragana", "reading", "kana", "vocab_reading"]) else 1,
                cooldown_penalty,
                level_distance,
                due_rank,
                -priority_value,
                int(row.get("used_in_material_count", 0) or 0),
                random.random(),
            ),
        }
        if rule_enabled:
            can_select, reason, matches = can_select_vocab_by_rules(item, rule_context, {})
            if not can_select and reason.startswith(("disabled_strict", "period_quota_reached", "zero_quota_strict")):
                stats["rejected_by_rule_count"] += 1
                if rule_rejection_log_count < 25:
                    rule_rejection_log_count += 1
                    print(f"[vocab-rules] rejected surface={word} reason={reason}")
                continue
            item["_matched_rule_keys"] = [rule["rule_key"] for rule in matches]
            item["_rule_priority"] = vocab_rule_priority(item, rule_context)
            item["_sort"] = (-item["_rule_priority"],) + item["_sort"]
            candidates.append(item)
        group = vocab_category_group({"category": category, "source": source})
        stats["candidate_counts"][group] = stats["candidate_counts"].get(group, 0) + 1
        buckets.setdefault(group, buckets["general"]).append(item)

    if stats["rejected_low_quality_count"] > rejected_log_count:
        print(
            "[vocab-selector] rejected low quality compound "
            f"additional_count={stats['rejected_low_quality_count'] - rejected_log_count}"
        )

    selected = []
    selected_keys = set()

    if rule_enabled:
        candidates.sort(key=lambda item: item["_sort"])
        for item in candidates:
            can_select, reason, matches = can_select_vocab_by_rules(item, rule_context, rule_selected_counts)
            if not can_select:
                stats["rejected_by_rule_count"] += 1
                if rule_rejection_log_count < 25:
                    rule_rejection_log_count += 1
                    print(f"[vocab-rules] rejected surface={item.get('word')} reason={reason}")
                continue
            key = item_normalized_key(item)
            if key in selected_keys:
                continue
            selected_keys.add(key)
            selected.append(item)
            for rule in matches:
                rule_selected_counts[rule["rule_key"]] = rule_selected_counts.get(rule["rule_key"], 0) + 1
            if len(selected) >= limit:
                break
    else:
        for group_items in buckets.values():
            group_items.sort(key=lambda item: item["_sort"])

    def take(group, count):
        if count <= 0:
            return
        for item in buckets.get(group, []):
            if len([x for x in selected if vocab_category_group(x) == group]) >= count:
                return
            key = item_normalized_key(item)
            if key in selected_keys:
                continue
            selected_keys.add(key)
            selected.append(item)
            if len(selected) >= limit:
                return

    if not rule_enabled:
        take("general", quotas["general_min"])
        take("business", quotas["business_max"])
        take("advanced", quotas["advanced_max"])
        take("sns", quotas["sns_max"])
        if len(selected) < limit:
            take("general", limit)
        if len(selected) < limit and mode != "general":
            take("business", quotas["business_max"])
            take("advanced", quotas["advanced_max"])
            take("sns", quotas["sns_max"])

    selected = selected[:limit]
    record_vocab_selection_logs(selected, selected_for="word")
    mark_vocabulary_pool_used(selected)
    for item in selected:
        group = vocab_category_group(item)
        stats["category_counts"][group] = stats["category_counts"].get(group, 0) + 1
        for rule_key in item.get("_matched_rule_keys") or []:
            stats["selected_rule_counts"][rule_key] = stats["selected_rule_counts"].get(rule_key, 0) + 1
        for key in list(item):
            if key.startswith("_"):
                item.pop(key, None)
    for rule_key, count in stats["selected_rule_counts"].items():
        rule = rule_context.get("rules", {}).get(rule_key, {})
        quota = int(rule.get("quota_count", 0) or 0)
        used = int(rule.get("used_count", 0) or 0)
        stats["rule_remaining_after_generation"][rule_key] = max(0, quota - used - count) if quota > 0 else None
    return (selected, stats) if return_stats else selected


MATERIAL_VERB_POS_MARKERS = (
    "動詞",
    "五段動詞",
    "一段動詞",
    "サ變動詞",
    "サ変動詞",
    "カ變動詞",
    "カ変動詞",
    "ichidan",
    "godan",
    "suru_verb",
    "kuru_verb",
    "verb",
)
MATERIAL_ICHIDAN_HINTS = ("一段", "上一段", "下一段", "ichidan")
MATERIAL_GODAN_HINTS = ("五段", "godan")
MATERIAL_SURU_HINTS = ("サ變", "サ変", "suru")
MATERIAL_KURU_HINTS = ("カ變", "カ変", "kuru")
MATERIAL_ICHIDAN_PRECEDING_KANA = "いきしちにひみりぎじぢびぴえけせてねへめれげぜでべぺ"
MATERIAL_GODAN_RU_EXCEPTIONS = {
    "帰る",
    "走る",
    "入る",
    "切る",
    "知る",
    "要る",
    "減る",
    "焦る",
    "限る",
    "蹴る",
    "滑る",
    "散る",
    "照る",
    "握る",
    "練る",
    "喋る",
    "参る",
    "混じる",
    "交じる",
    "茂る",
    "遮る",
    "湿る",
    "蘇る",
}
MATERIAL_ICHIDAN_RU_VERBS = {
    "見る",
    "食べる",
    "決める",
    "冷える",
    "起きる",
    "借りる",
    "降りる",
    "浴びる",
    "信じる",
    "着る",
    "過ぎる",
}
MATERIAL_GODAN_FORMS = {
    "う": ("い", "って", "った", "わない", "えば", "わせる", "われる"),
    "く": ("き", "いて", "いた", "かない", "けば", "かせる", "かれる"),
    "ぐ": ("ぎ", "いで", "いだ", "がない", "げば", "がせる", "がれる"),
    "す": ("し", "して", "した", "さない", "せば", "させる", "される"),
    "つ": ("ち", "って", "った", "たない", "てば", "たせる", "たれる"),
    "ぬ": ("に", "んで", "んだ", "なない", "ねば", "なせる", "なれる"),
    "ぶ": ("び", "んで", "んだ", "ばない", "べば", "ばせる", "ばれる"),
    "む": ("み", "んで", "んだ", "まない", "めば", "ませる", "まれる"),
    "る": ("り", "って", "った", "らない", "れば", "らせる", "られる"),
}
SAFE_SURU_NOUNS = {
    "確認",
    "準備",
    "改善",
    "提案",
    "共有",
    "説明",
    "相談",
    "連絡",
    "参加",
    "登録",
    "利用",
    "予約",
    "勉強",
    "練習",
    "運動",
    "検索",
    "保存",
    "変更",
    "更新",
    "開始",
    "終了",
    "報告",
    "連携",
    "対応",
    "管理",
    "整理",
    "分析",
    "調整",
}
SAFE_SURU_VERBS = {f"{noun}する" for noun in SAFE_SURU_NOUNS}


def material_verb_group_label(group):
    return {1: "五段", 2: "一段", 3: "不規則"}.get(int(group or 0), "未判定")


def row_is_explicit_verb(row):
    base = first_text(row, ["base_form", "dictionary_form", "surface", "term", "word"])
    part_of_speech = first_text(row, ["part_of_speech", "pos"])
    text = " ".join(
        filter(
            None,
            [
                part_of_speech,
                first_text(row, ["conjugation_type", "inflection_type"]),
                first_text(row, ["category"]),
            ],
        )
    ).lower()
    if first_text(row, ["verb_group"]):
        return True
    if any(marker.lower() in text for marker in MATERIAL_VERB_POS_MARKERS):
        return True
    if base in SAFE_SURU_VERBS:
        return True
    if "名詞" in part_of_speech:
        return False
    return base[-1:] in MATERIAL_GODAN_FORMS or base.endswith("る")


def fake_suru_rejection_reason(row):
    if row_is_explicit_verb(row):
        return ""
    base = first_text(row, ["base_form", "surface", "term", "word"])
    if not base or base in SAFE_SURU_NOUNS or base in SAFE_SURU_VERBS:
        return ""
    part_of_speech = first_text(row, ["part_of_speech", "pos"])
    category = first_text(row, ["category"]).lower()
    if "名詞" not in part_of_speech and category not in {"business", "advanced", "general"}:
        return ""
    if any(base.endswith(noun) for noun in SAFE_SURU_NOUNS):
        return "not_safe_suru_noun"
    if category in {"business", "advanced"}:
        return "business_or_advanced_noun"
    if len(re.findall(r"[\u4e00-\u9fff]", base)) > 4:
        return "compound_noun_too_long"
    return ""


def row_can_be_suru_verb(row):
    if row_is_explicit_verb(row):
        return False
    category = first_text(row, ["category"]).lower()
    if category in {"sns", "internet_slang", "otaku_culture", "named_entity", "sensitive", "typo_or_noise", "unknown"}:
        return False
    part_of_speech = first_text(row, ["part_of_speech", "pos"])
    if part_of_speech and "名詞" not in part_of_speech:
        return False
    base = first_text(row, ["base_form", "surface", "term", "word"])
    if not base or base.endswith("する"):
        return False
    return base in SAFE_SURU_NOUNS


def is_valid_verb_candidate(row):
    if row_is_explicit_verb(row) or row_can_be_suru_verb(row):
        return True, ""
    return False, fake_suru_rejection_reason(row)


def infer_material_verb_group(row, base_form):
    raw_group = first_text(row, ["verb_group"])
    if raw_group:
        try:
            group = int(raw_group)
            if group in {1, 2, 3}:
                return group
        except ValueError:
            pass

    hints = " ".join(
        filter(
            None,
            [
                first_text(row, ["conjugation_type", "inflection_type"]),
                first_text(row, ["part_of_speech", "pos"]),
                first_text(row, ["category"]),
            ],
        )
    ).lower()
    if base_form in {"する", "来る", "くる"} or base_form.endswith("する"):
        return 3
    if any(hint.lower() in hints for hint in MATERIAL_SURU_HINTS + MATERIAL_KURU_HINTS):
        return 3
    if any(hint.lower() in hints for hint in MATERIAL_ICHIDAN_HINTS):
        return 2
    if any(hint.lower() in hints for hint in MATERIAL_GODAN_HINTS):
        return 1
    if base_form.endswith("る"):
        if base_form in MATERIAL_ICHIDAN_RU_VERBS:
            return 2
        if base_form in MATERIAL_GODAN_RU_EXCEPTIONS:
            return 1
        reading = first_text(row, ["reading_hiragana", "reading", "kana"])
        reading_or_base = reading if reading.endswith("る") else base_form
        previous = reading_or_base[-2] if len(reading_or_base) >= 2 else ""
        return 2 if previous in MATERIAL_ICHIDAN_PRECEDING_KANA else 1
    if base_form[-1:] in MATERIAL_GODAN_FORMS:
        return 1
    return None


def conjugate_material_verb(base_form, group):
    if not base_form:
        return None
    if group == 3:
        if base_form in {"来る", "くる"}:
            if base_form == "くる":
                return {
                    "renyou": "き",
                    "te": "きて",
                    "ta": "きた",
                    "nai": "こない",
                    "ba": "くれば",
                    "causative": "こさせる",
                    "passive": "こられる",
                }
            prefix = "来"
            return {
                "renyou": prefix,
                "te": f"{prefix}て",
                "ta": f"{prefix}た",
                "nai": f"{prefix}ない",
                "ba": f"{prefix}れば",
                "causative": f"{prefix}させる",
                "passive": f"{prefix}られる",
            }
        stem = base_form[:-2] if base_form.endswith("する") else ""
        return {
            "renyou": f"{stem}し",
            "te": f"{stem}して",
            "ta": f"{stem}した",
            "nai": f"{stem}しない",
            "ba": f"{stem}すれば",
            "causative": f"{stem}させる",
            "passive": f"{stem}される",
        }
    if group == 2:
        stem = base_form[:-1]
        return {
            "renyou": stem,
            "te": f"{stem}て",
            "ta": f"{stem}た",
            "nai": f"{stem}ない",
            "ba": f"{stem}れば",
            "causative": f"{stem}させる",
            "passive": f"{stem}られる",
        }
    if group == 1:
        if base_form == "行く":
            return {
                "renyou": "行き",
                "te": "行って",
                "ta": "行った",
                "nai": "行かない",
                "ba": "行けば",
                "causative": "行かせる",
                "passive": "行かれる",
            }
        ending = base_form[-1:]
        forms = MATERIAL_GODAN_FORMS.get(ending)
        if not forms:
            return None
        stem = base_form[:-1]
        renyou, te, ta, nai, ba, causative, passive = forms
        return {
            "renyou": f"{stem}{renyou}",
            "te": f"{stem}{te}",
            "ta": f"{stem}{ta}",
            "nai": f"{stem}{nai}",
            "ba": f"{stem}{ba}",
            "causative": f"{stem}{causative}",
            "passive": f"{stem}{passive}",
        }
    return None


def build_material_verb_from_vocab_row(row):
    is_valid, _reason = is_valid_verb_candidate(row)
    if not is_valid:
        return None
    explicit = row_is_explicit_verb(row)
    suru_candidate = row_can_be_suru_verb(row)

    surface = first_text(row, ["surface", "term", "word", "base_form"])
    base_form = first_text(row, ["base_form", "dictionary_form", "surface", "term", "word"]) or surface
    reading = first_text(row, ["reading_hiragana", "reading", "kana"])
    source = "vocabulary_pool"
    if suru_candidate and not explicit:
        base_form = f"{base_form}する"
        reading = f"{reading}する" if reading else ""
        source = "vocabulary_pool_suru"

    group = infer_material_verb_group(row, base_form)
    forms = conjugate_material_verb(base_form, group)
    if not forms:
        return None

    meaning = first_text(row, ["meaning_zh", "meaning_zh_tw", "meaning", "vocab_meaning"])
    level = first_text(row, ["jlpt_level", "target_level", "level"])
    group_text = material_verb_group_label(group)
    normalized_key = normalize_vocab_key(first_text(row, ["normalized_key", "normalized_term", "base_form", "surface", "term", "word"]) or base_form)
    base_parts = [f"{base_form}（{reading}）" if reading else base_form]
    if meaning:
        base_parts.append(meaning)
    meta_parts = [group_text]
    if level:
        meta_parts.append(level)
    base_display = " - ".join(base_parts)
    if meta_parts:
        base_display = f"{base_display}｜{'｜'.join(meta_parts)}"
    return {
        "base": base_display,
        "dictionary_form": base_form,
        "reading": reading,
        "meaning": meaning,
        "verb_group": group,
        "verb_group_label": group_text,
        "jlpt_level": level,
        "part_of_speech": first_text(row, ["part_of_speech", "pos"]) or group_text,
        "normalized_key": normalized_key,
        "masuStem": forms["renyou"],
        "te": forms["te"],
        "ta": forms["ta"],
        "nai": forms["nai"],
        "ba": forms["ba"],
        "causative": forms["causative"],
        "passive": forms["passive"],
        "causativePassive": "",
        "source": source,
        "_pool_id": row.get("id"),
        "_pool_has_last_seen": "last_seen_at" in row,
        "_pool_has_last_used": "last_used_at" in row,
        "_pool_has_used_count": "used_in_material_count" in row,
    }


def material_verbs_from_vocabulary_pool(settings, limit, exclude_keys=None):
    stats = {
        "duplicate_filtered_count": 0,
        "selected_keys": [],
        "source_summary": {"vocabulary_pool": 0, "vocabulary_pool_suru": 0},
        "rejected_fake_suru_count": 0,
        "verb_candidate_count": 0,
    }
    if limit <= 0:
        return [], stats
    rows = fetch_vocabulary_pool_rows()
    if not rows:
        return [], stats

    target = settings.get("target_level", "")
    seen = {normalize_vocab_key(key) for key in (exclude_keys or set()) if key}
    today = taipei_now().date()
    buckets = {
        "target_fresh": [],
        "target_relaxed7": [],
        "adjacent_fresh": [],
        "adjacent_relaxed7": [],
        "distant_fresh": [],
        "cooldown_any": [],
    }

    rejected_log_count = 0
    for row in rows:
        is_valid, rejection_reason = is_valid_verb_candidate(row)
        if not is_valid:
            if rejection_reason:
                stats["rejected_fake_suru_count"] += 1
                if rejected_log_count < 25:
                    rejected_log_count += 1
                    print(
                        "[verb-selector] rejected fake suru verb "
                        f"surface={first_text(row, ['surface', 'base_form', 'term', 'word'])} "
                        f"reason={rejection_reason}"
                    )
            continue
        item = build_material_verb_from_vocab_row(row)
        if not item:
            continue
        stats["verb_candidate_count"] += 1
        key = item_normalized_key(item)
        if not key or key in seen:
            stats["duplicate_filtered_count"] += 1
            continue
        seen.add(key)
        row_level = first_text(row, ["jlpt_level", "target_level", "level"])
        level_distance = preferred_level_distance(target, row_level)
        try:
            cooldown_days = max(14, int(row.get("cooldown_days", 14) or 14))
        except (TypeError, ValueError):
            cooldown_days = 14
        last_used = parse_loose_date(first_text(row, ["last_used_at", "last_seen_at"]))
        days_since = 99999 if not last_used else (today - last_used).days
        priority = first_text(row, ["priority", "weight"])
        try:
            priority_value = int(float(priority)) if priority else 0
        except ValueError:
            priority_value = 0
        item["_sort"] = (
            0 if item.get("source") == "vocabulary_pool" else 1,
            level_distance,
            int(row.get("used_in_material_count", 0) or 0),
            -priority_value,
            random.random(),
        )
        if level_distance == 0 and days_since >= cooldown_days:
            buckets["target_fresh"].append(item)
        elif level_distance == 0 and days_since >= 7:
            buckets["target_relaxed7"].append(item)
        elif level_distance == 1 and days_since >= cooldown_days:
            buckets["adjacent_fresh"].append(item)
        elif level_distance == 1 and days_since >= 7:
            buckets["adjacent_relaxed7"].append(item)
        elif level_distance > 1 and days_since >= cooldown_days:
            buckets["distant_fresh"].append(item)
        else:
            buckets["cooldown_any"].append(item)

    ordered = []
    for bucket_name in ("target_fresh", "target_relaxed7", "adjacent_fresh", "adjacent_relaxed7", "distant_fresh", "cooldown_any"):
        ordered.extend(sorted(buckets[bucket_name], key=lambda item: item["_sort"]))
        if len(ordered) >= limit:
            break
    selected = ordered[:limit]
    mark_vocabulary_pool_used(selected)
    if stats["rejected_fake_suru_count"] > rejected_log_count:
        print(
            "[verb-selector] rejected fake suru verb "
            f"additional_count={stats['rejected_fake_suru_count'] - rejected_log_count}"
        )
    for item in selected:
        key = item_normalized_key(item)
        if key:
            stats["selected_keys"].append(key)
        stats["source_summary"][item.get("source", "vocabulary_pool")] = stats["source_summary"].get(item.get("source", "vocabulary_pool"), 0) + 1
        for private_key in list(item):
            if private_key.startswith("_"):
                item.pop(private_key, None)
    return selected, stats

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
    seed = load_basic_seed_vocab_items(settings) + list(sample_material(settings).get("vocab", [])) + LOCAL_SEED_VOCAB
    items = []
    seen = set()
    for item in seed:
        word = first_text(item, ["word", "surface", "base_form"]).strip()
        if not word or word in seen:
            continue
        seen.add(word)
        items.append(
            {
                "word": word,
                "reading": first_text(item, ["reading", "reading_hiragana"]),
                "meaning": first_text(item, ["meaning", "meaning_zh"]),
                "part_of_speech": first_text(item, ["part_of_speech", "pos"]),
                "jlpt_level": first_text(item, ["jlpt_level"]) or settings.get("target_level", ""),
                "category": first_text(item, ["category"]) or "general",
                "quality": first_text(item, ["quality"]) or "core",
                "normalized_key": normalize_vocab_key(item.get("normalized_key") or item.get("base_form") or word),
                "example_sentence": item.get("example_sentence", ""),
                "example_translation_zh": item.get("example_translation_zh", ""),
                "source": first_text(item, ["source"]) or "seed",
            }
        )
        if len(items) >= limit:
            return items
    base_items = list(items)
    while len(items) < limit and base_items:
        items.append(dict(base_items[len(items) % len(base_items)]))
    return items[:limit]


def merge_vocab_selector_stats(target, source):
    if not source:
        return target
    target["rejected_low_quality_count"] = target.get("rejected_low_quality_count", 0) + source.get("rejected_low_quality_count", 0)
    target["rejected_by_rule_count"] = target.get("rejected_by_rule_count", 0) + source.get("rejected_by_rule_count", 0)
    for key in ("category_counts", "candidate_counts", "selected_rule_counts"):
        target.setdefault(key, {})
        for name, count in (source.get(key) or {}).items():
            target[key][name] = target[key].get(name, 0) + count
    target.setdefault("rule_remaining_after_generation", {})
    target["rule_remaining_after_generation"].update(source.get("rule_remaining_after_generation") or {})
    return target


def load_basic_seed_vocab_items(settings=None):
    global _BASIC_SEED_VOCAB_CACHE
    if _BASIC_SEED_VOCAB_CACHE is None:
        try:
            with open(VOCABULARY_SEED_BASIC_FILE, "r", encoding="utf-8") as file:
                loaded = json.load(file)
            _BASIC_SEED_VOCAB_CACHE = loaded if isinstance(loaded, list) else []
        except Exception as exc:
            print(f"[vocab-selector] basic seed vocabulary unavailable; error={exc}")
            _BASIC_SEED_VOCAB_CACHE = []
    target = (settings or {}).get("target_level", "")
    rows = list(_BASIC_SEED_VOCAB_CACHE)
    if not target:
        return rows
    def seed_sort_key(row):
        try:
            priority = int(float(row.get("priority", 1) or 1))
        except (TypeError, ValueError):
            priority = 1
        return (preferred_level_distance(target, str(row.get("jlpt_level", ""))), -priority, random.random())

    rows.sort(key=seed_sort_key)
    return rows


def material_verbs_from_db(limit, exclude_keys=None):
    if limit <= 0:
        return []
    ensure_settings_store()
    rows = sqlite_dicts("SELECT * FROM verbs ORDER BY RANDOM() LIMIT ?", (max(limit * 4, limit),))
    seen = {normalize_vocab_key(key) for key in (exclude_keys or set()) if key}
    items = []
    for row in rows:
        key = normalize_vocab_key(row.get("dictionary_form", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        group = row.get("verb_group", "")
        group_text = material_verb_group_label(group)
        base_display = f"{row['dictionary_form']}（{row['reading']}） - {row['meaning']}｜{group_text}"
        items.append(
            {
                "base": base_display,
                "dictionary_form": row["dictionary_form"],
                "reading": row["reading"],
                "meaning": row["meaning"],
                "verb_group": group,
                "verb_group_label": group_text,
                "normalized_key": key,
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
        )
        if len(items) >= limit:
            break
    return items


NATURAL_SEED_VERB_ROWS = [
    ("見る", "みる", "看", "動詞", "N5"),
    ("食べる", "たべる", "吃", "動詞", "N5"),
    ("話す", "はなす", "說話", "動詞", "N5"),
    ("行く", "いく", "去", "動詞", "N5"),
    ("書く", "かく", "寫", "動詞", "N5"),
    ("読む", "よむ", "讀", "動詞", "N5"),
    ("聞く", "きく", "聽、詢問", "動詞", "N5"),
    ("使う", "つかう", "使用", "動詞", "N5"),
    ("作る", "つくる", "製作", "動詞", "N5"),
    ("買う", "かう", "買", "動詞", "N5"),
    ("会う", "あう", "見面", "動詞", "N5"),
    ("思う", "おもう", "想、認為", "動詞", "N5"),
    ("考える", "かんがえる", "思考、考慮", "動詞", "N4"),
    ("決める", "きめる", "決定", "動詞", "N4"),
    ("始める", "はじめる", "開始", "動詞", "N5"),
    ("続ける", "つづける", "繼續", "動詞", "N4"),
    ("入る", "はいる", "進入", "動詞", "N5"),
    ("出る", "でる", "出去、出現", "動詞", "N5"),
    ("働く", "はたらく", "工作", "動詞", "N5"),
    ("選ぶ", "えらぶ", "選擇", "動詞", "N4"),
    ("確認する", "かくにんする", "確認", "サ変動詞", "N3"),
    ("提案する", "ていあんする", "提案", "サ変動詞", "N3"),
    ("改善する", "かいぜんする", "改善", "サ変動詞", "N3"),
    ("共有する", "きょうゆうする", "共享", "サ変動詞", "N3"),
    ("準備する", "じゅんびする", "準備", "サ変動詞", "N5"),
    ("説明する", "せつめいする", "說明", "サ変動詞", "N4"),
    ("相談する", "そうだんする", "商量、諮詢", "サ変動詞", "N4"),
]


def natural_seed_verb_items(settings):
    items = []
    for surface, reading, meaning, part_of_speech, level in NATURAL_SEED_VERB_ROWS:
        item = build_material_verb_from_vocab_row(
            {
                "surface": surface,
                "base_form": surface,
                "reading_hiragana": reading,
                "meaning_zh": meaning,
                "part_of_speech": part_of_speech,
                "jlpt_level": level,
                "category": "seed",
                "source": "seed_natural",
            }
        )
        if not item:
            continue
        item["source"] = "seed"
        item["normalized_key"] = normalize_vocab_key(surface)
        items.append(item)
    return items


def material_seed_verbs(settings, limit, exclude_keys=None):
    if limit <= 0:
        return []
    seed = natural_seed_verb_items(settings) + list(sample_material(settings).get("verbs", [])) + LOCAL_SEED_VERBS
    items = []
    seen = {normalize_vocab_key(key) for key in (exclude_keys or set()) if key}
    for item in seed:
        base = str(item.get("base", "")).strip()
        key = normalize_vocab_key(item.get("normalized_key") or item.get("dictionary_form") or item.get("base") or base)
        if not base or not key or key in seen:
            continue
        seen.add(key)
        copied = dict(item)
        copied.setdefault("normalized_key", key)
        copied["source"] = "seed"
        items.append(copied)
        if len(items) >= limit:
            return items
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
    vocab_selector_stats = {
        "rejected_low_quality_count": 0,
        "rejected_by_rule_count": 0,
        "category_counts": {},
        "candidate_counts": {},
        "selected_rule_counts": {},
        "rule_remaining_after_generation": {},
    }
    seed_used = False
    duplicate_filtered_count = 0

    vocab_mode = settings.get("vocab_mode", "general")
    max_slang_quota = 0 if vocab_count < 5 else max(1, min(int(vocab_count * 0.2), vocab_count))
    slang_quota = 0
    if vocab_mode == "sns" and vocab_count >= 5:
        slang_quota = min(max(1, int(vocab_count * 0.1)), max_slang_quota)

    base_quota = max(0, vocab_count - slang_quota)
    if force_seed:
        vocab = []
    else:
        vocab, pool_stats = material_vocab_from_vocabulary_pool(settings, base_quota, return_stats=True)
        merge_vocab_selector_stats(vocab_selector_stats, pool_stats)
    if not force_seed and len(vocab) < base_quota:
        vocab.extend(material_vocab_from_existing(settings, base_quota - len(vocab)))
    vocab, duplicates, selected_keys = dedupe_vocab_items(vocab)
    duplicate_filtered_count += duplicates
    source_counts["vocabulary"] = len([item for item in vocab if item.get("source") in {"vocabulary_pool", "materials"}])

    slang_vocab = [] if force_seed else material_vocab_from_approved_slang(slang_quota)
    slang_vocab, duplicates, selected_keys = dedupe_vocab_items(slang_vocab, selected_keys)
    duplicate_filtered_count += duplicates
    mark_slang_used_in_material([{"id": item.get("_slang_id")} for item in slang_vocab if item.get("_slang_id")])
    source_counts["slang"] = len(slang_vocab)
    vocab.extend(slang_vocab)

    if len(vocab) < vocab_count:
        if force_seed:
            extra_pool = []
        else:
            extra_pool, pool_stats = material_vocab_from_vocabulary_pool(
                settings,
                vocab_count - len(vocab),
                exclude_keys=selected_keys,
                return_stats=True,
            )
            merge_vocab_selector_stats(vocab_selector_stats, pool_stats)
        extra_pool, duplicates, selected_keys = dedupe_vocab_items(extra_pool, selected_keys)
        duplicate_filtered_count += duplicates
        vocab.extend(extra_pool)
        source_counts["vocabulary"] += len(extra_pool)

    if len(vocab) < vocab_count and not force_seed:
        existing_items = material_vocab_from_existing(settings, vocab_count - len(vocab))
        existing_items, duplicates, selected_keys = dedupe_vocab_items(existing_items, selected_keys)
        duplicate_filtered_count += duplicates
        vocab.extend(existing_items)
        source_counts["vocabulary"] += len(existing_items)

    if len(vocab) < vocab_count:
        seed_items = material_seed_vocab(settings, vocab_count - len(vocab))
        seed_items, duplicates, selected_keys = dedupe_vocab_items(seed_items, selected_keys)
        duplicate_filtered_count += duplicates
        vocab.extend(seed_items)
        source_counts["seed"] += len(seed_items)
        seed_used = bool(seed_items)
    vocab = vocab[:vocab_count]
    selected_normalized_keys = [item_normalized_key(item) for item in vocab if item_normalized_key(item)]
    category_counts = Counter(vocab_category_group(item) for item in vocab)
    vocab_source_summary = Counter((item.get("source") or "unknown") for item in vocab)
    quality_counts = Counter((item.get("quality") or "未設定") for item in vocab)
    jlpt_counts = Counter((item.get("jlpt_level") or "未分類 JLPT") for item in vocab)
    part_of_speech_counts = Counter((item.get("part_of_speech") or "未分類詞性") for item in vocab)

    verb_duplicate_filtered_count = 0
    verb_source_summary = {"vocabulary_pool": 0, "vocabulary_pool_suru": 0, "verbs": 0, "seed_fallback": 0}
    rejected_fake_suru_count = 0
    verb_candidate_count = 0
    verbs = []
    selected_verb_keys = []
    if not force_seed:
        verbs, verb_pool_stats = material_verbs_from_vocabulary_pool(settings, verb_count, exclude_keys=selected_keys)
        verb_duplicate_filtered_count += verb_pool_stats.get("duplicate_filtered_count", 0)
        rejected_fake_suru_count += verb_pool_stats.get("rejected_fake_suru_count", 0)
        verb_candidate_count += verb_pool_stats.get("verb_candidate_count", 0)
        selected_verb_keys.extend(verb_pool_stats.get("selected_keys", []))
        for source, count in verb_pool_stats.get("source_summary", {}).items():
            verb_source_summary[source] = verb_source_summary.get(source, 0) + count
    if len(verbs) < verb_count:
        seed_verbs = material_seed_verbs(settings, verb_count - len(verbs), exclude_keys=set(selected_verb_keys) | set(selected_keys))
        verbs.extend(seed_verbs)
        seed_keys = [item_normalized_key(item) for item in seed_verbs if item_normalized_key(item)]
        selected_verb_keys.extend(seed_keys)
        source_counts["seed"] += len(seed_verbs)
        verb_source_summary["seed_fallback"] += len(seed_verbs)
        seed_used = bool(seed_verbs)
        if seed_verbs:
            print(f"[verb-selector] seed fallback used count={len(seed_verbs)} source=natural_seed")
    if len(verbs) < verb_count and not force_seed:
        db_verbs = material_verbs_from_db(verb_count - len(verbs), exclude_keys=set(selected_verb_keys) | set(selected_keys))
        verbs.extend(db_verbs)
        db_keys = [item_normalized_key(item) for item in db_verbs if item_normalized_key(item)]
        selected_verb_keys.extend(db_keys)
        verb_source_summary["verbs"] += len(db_verbs)
        if db_verbs:
            print(f"[verb-selector] seed fallback used count={len(db_verbs)} source=verbs_table")
    verbs = verbs[:verb_count]
    selected_verb_keys = [item_normalized_key(item) for item in verbs if item_normalized_key(item)]

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
        "vocab_source_summary": dict(vocab_source_summary),
        "category_counts": dict(category_counts),
        "source_counts": dict(vocab_source_summary),
        "quality_counts": dict(quality_counts),
        "jlpt_counts": dict(jlpt_counts),
        "part_of_speech_counts": dict(part_of_speech_counts),
        "vocab_rule_summary": vocab_selector_stats.get("selected_rule_counts", {}),
        "selected_rule_counts": vocab_selector_stats.get("selected_rule_counts", {}),
        "rule_remaining_after_generation": vocab_selector_stats.get("rule_remaining_after_generation", {}),
        "rejected_low_quality_count": vocab_selector_stats.get("rejected_low_quality_count", 0),
        "rejected_by_rule_count": vocab_selector_stats.get("rejected_by_rule_count", 0),
        "general_count": category_counts.get("general", 0),
        "business_count": category_counts.get("business", 0),
        "advanced_count": category_counts.get("advanced", 0),
        "sns_count": category_counts.get("sns", 0),
        "selected_normalized_keys": selected_normalized_keys,
        "duplicate_filtered_count": duplicate_filtered_count,
        "selected_verb_keys": selected_verb_keys,
        "verb_duplicate_filtered_count": verb_duplicate_filtered_count,
        "verb_source_summary": verb_source_summary,
        "rejected_fake_suru_count": rejected_fake_suru_count,
        "verb_candidate_count": verb_candidate_count,
        "seed_fallback_count": verb_source_summary.get("verbs", 0) + verb_source_summary.get("seed_fallback", 0),
        "fallback_reason": "insufficient_vocab" if len(vocab) < vocab_count else ("insufficient_verbs" if (verb_source_summary.get("verbs", 0) or verb_source_summary.get("seed_fallback", 0)) else ""),
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
    print(
        "[vocab-selector] final category_counts="
        f"{dict(category_counts)} rejected_low_quality={vocab_selector_stats.get('rejected_low_quality_count', 0)} "
        f"candidate_counts={vocab_selector_stats.get('candidate_counts', {})}"
    )
    print(
        "[material-generator] local verb sources "
        f"vocabulary_pool={verb_source_summary.get('vocabulary_pool', 0)} "
        f"vocabulary_pool_suru={verb_source_summary.get('vocabulary_pool_suru', 0)} "
        f"verbs={verb_source_summary.get('verbs', 0)} seed_fallback={verb_source_summary.get('seed_fallback', 0)} "
        f"duplicates={verb_duplicate_filtered_count}"
    )
    return {"vocab": vocab, "verbs": verbs, "grammar": grammar, "metadata": metadata}


def save_material_for_date(material_date, material, settings):
    ensure_database()
    date = material_date_display(material_date)
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
                "vocab_jlpt_level": vocab.get("jlpt_level", ""),
                "vocab_category": vocab.get("category", ""),
                "vocab_normalized_key": item_normalized_key(vocab),
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
        date_variants = material_date_variants(date)
        delete_placeholders = ", ".join(["%s"] * len(date_variants))
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                print(f"[history-protection] safe upsert material date={date}")
                cur.execute(f"DELETE FROM materials WHERE date IN ({delete_placeholders})", tuple(date_variants))
                cur.executemany(f"INSERT INTO materials ({columns_sql}) VALUES ({placeholders})", rows)
            conn.commit()
        invalidate_archive_dates_cache("daily material saved")
        return date

    df = read_database()
    print(f"[history-protection] safe upsert material date={date}")
    df = df[~df["date"].isin(material_date_variants(date))]
    output = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
    output[COLUMNS].to_csv(DATABASE_FILE, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    invalidate_archive_dates_cache("daily material saved")
    return date


def save_material_for_today(material, settings):
    return save_material_for_date(get_today_taipei_date(), material, settings)


def material_by_date(target_date):
    rows = read_material_rows_by_date(target_date)
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
                    "jlpt_level": row.get("vocab_jlpt_level", ""),
                    "category": row.get("vocab_category", ""),
                    "normalized_key": row.get("vocab_normalized_key", ""),
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
        "date": material_date_display(first.get("date", target_date)),
        "date_iso": material_date_iso(first.get("date", target_date)),
        "targetLevel": first.get("target_level", ""),
        "vocabulary": vocabulary,
        "verbs": verbs,
        "grammar": {"title": first["grammar_title"], "exp": first["grammar_exp"], "examples": examples},
        "metadata": metadata,
    }


def get_material_by_date(material_date):
    return material_by_date(material_date)


def build_telegram_notification(material, date, app_url=None):
    if not material:
        raise RuntimeError("教材尚未寫入資料庫，無法推送 Telegram。")
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


def generate_daily_material(use_sample=False, posted_settings=None, app_url=None, mode="local", material_date=None, notify_telegram=True):
    settings = save_settings_file(posted_settings) if posted_settings else load_settings()
    mode = "local" if use_sample else normalize_generation_mode(mode)
    print(f"[material-generator] mode={mode} start")

    if mode == "local":
        print("[feature-boundary] daily_material mode=local skip gemini")
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

    date = save_material_for_date(material_date or get_today_taipei_date(), raw_material, settings)
    print(f"[material-generator] local material generated; ai_used={str(raw_material.get('metadata', {}).get('ai_used', False)).lower()}")
    print(f"[material-generator] material saved date={date}")
    material = get_material_by_date(date)
    if not material:
        raise RuntimeError(f"教材寫入後重新讀取失敗：{date}")

    telegram_status = "未發送"
    try:
        send_telegram_message(build_telegram_notification(material, date, app_url))
        telegram_status = "Telegram 通知已發送"
    except Exception as e:
        telegram_status = f"Telegram 通知發送失敗：{e}"

    invalidate_dashboard_cache("daily material generated")
    return {
        "ok": True,
        "message": material_success_message(date, settings, raw_material, telegram_status),
        "date": date,
        "material_date": date,
        "telegram": telegram_status,
        "generation_mode": raw_material.get("metadata", {}).get("generation_mode", mode),
        "ai_used": bool(raw_material.get("metadata", {}).get("ai_used", False)),
        "fallback_used": bool(raw_material.get("metadata", {}).get("fallback_used", False)),
        "source_summary": raw_material.get("metadata", {}).get("source_summary", {}),
    }


def run_daily_schedule(app_url=None, mode="local"):
    date = get_today_taipei_date()
    print(f"[daily-schedule] start date={date}")
    try:
        material = get_material_by_date(date)
        print(f"[daily-schedule] material exists={str(bool(material)).lower()}")
        if not material:
            print(f"[daily-schedule] generating local material date={date}")
            result = generate_daily_material(app_url=app_url, mode=mode, material_date=date)
            print(f"[daily-schedule] save material success date={date}")
            print(f"[daily-schedule] reload material from db success date={date}")
            print(f"[daily-schedule] telegram push success date={date}")
            return result
        print(f"[daily-schedule] reload material from db success date={date}")
        send_telegram_message(build_telegram_notification(material, date, app_url))
        print(f"[daily-schedule] telegram push success date={date}")
        invalidate_dashboard_cache("daily schedule material ready")
        return {
            "ok": True,
            "message": f"{date} 的學習材料已確認落地，Telegram 已推送。",
            "date": date,
            "material_date": date,
            "generation_mode": material.get("metadata", {}).get("generation_mode", "local"),
            "ai_used": bool(material.get("metadata", {}).get("ai_used", False)),
            "telegram": "Telegram 通知已發送",
        }
    except Exception as exc:
        print(f"[daily-schedule] failed reason={exc}")
        print(traceback.format_exc())
        raise


def material_generation_error_payload(error):
    detail = str(error or "")
    print(f"[material-generator] ERROR generate failed: {detail}")
    print(traceback.format_exc())
    lower = detail.lower()
    if "timestamp" in lower or "timestamptz" in lower or "timestamp with time zone" in lower:
        return {
            "error": "local_generation_failed",
            "message": "\u672c\u5730\u6559\u6750\u751f\u6210\u5931\u6557\uff0c\u8cc7\u6599\u5eab\u6642\u9593\u6b04\u4f4d\u683c\u5f0f\u7570\u5e38\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66\u3002",
        }
    return {
        "error": "local_generation_failed",
        "message": "\u672c\u5730\u6559\u6750\u751f\u6210\u5931\u6557\uff0c\u8acb\u67e5\u770b\u7cfb\u7d71\u7d00\u9304\u3002",
    }

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
    started = time.perf_counter()
    candidates = gemini_model_candidates()
    diagnostic = {
        "gemini_api_key_present": bool(GEMINI_API_KEY),
        "selected_model": candidates[0] if candidates else "",
        "model_candidates": candidates,
        "cooldown_active": False,
        "local_mode_active": False,
        "gemini_called": False,
        "elapsed_ms": 0,
        "exception_type": "",
        "exception_message": "",
        "failures": [],
    }
    billing_snapshot = gemini_billing_snapshot()
    diagnostic.update(
        {
            "billing_block_active": billing_snapshot["billing_block_active"],
            "billing_status": billing_snapshot["last_billing_status"],
            "gemini_billing_block_until": billing_snapshot["gemini_billing_block_until_iso"],
            "prepayment_depleted": billing_snapshot["prepayment_depleted"],
        }
    )
    print("[grammar-analyzer] request received")
    print(f"[grammar-analyzer] input length={len(text or '')}")
    print(f"[grammar-analyzer] gemini api key present={str(bool(GEMINI_API_KEY)).lower()}")
    print(f"[grammar-analyzer] model candidates={','.join(candidates)}")
    print(f"[grammar-analyzer] selected model={diagnostic['selected_model']}")
    print("[grammar-analyzer] cooldown active=false")
    print("[grammar-analyzer] local mode active=false")
    print(f"[grammar-analyzer] billing block active={str(billing_snapshot['billing_block_active']).lower()}")
    if GEMINI_API_KEY:
        print("[feature-boundary] grammar_analyzer gemini enabled")
    else:
        print("[grammar-analyzer] fallback reason=missing_api_key")
    parsed, mecab_error = analyze_with_mecab(text)
    fallback_reading = parsed["reading_hiragana"] if parsed else answer_reading_hiragana(text)
    advanced_mecab = {
        "reading_hiragana": fallback_reading,
        "tokens": parsed["tokens"] if parsed else [],
        "particles": parsed["particles"] if parsed else [],
        "verb_forms": parsed["verb_forms"] if parsed else [],
        "error": mecab_error or "",
    }

    if billing_snapshot["billing_block_active"]:
        reason = "prepayment_depleted"
        diagnostic["cooldown_active"] = True
        diagnostic["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        diagnostic["exception_type"] = "billing_block_active"
        diagnostic["exception_message"] = gemini_billing_block_message()
        print(f"[grammar-analyzer] fallback reason={reason}")
        payload = grammar_fallback_response(text, fallback_reading, advanced_mecab)
        payload["error_message"] = gemini_billing_block_message()
        payload["naturalness_check"]["reason"] = gemini_billing_block_message()
        payload["sentence_summary"] = "Gemini API 帳務保護機制仍在暫停中，目前僅回傳本地規則偵測結果。"
        if grammar_debug_enabled():
            payload["fallback_reason"] = reason
            payload["debug"] = diagnostic
        persist_analysis_slang_terms(payload, text, "grammar_analyzer_billing_block")
        return payload, 200

    prompt = build_grammar_coach_prompt(text, fallback_reading)
    failures = []
    if not GEMINI_API_KEY:
        failures.append({"model": diagnostic["selected_model"], "error_type": "missing_api_key", "message": "尚未設定 Gemini API Key。"})

    for model_name in ([] if not GEMINI_API_KEY else candidates):
        raw_response = ""
        try:
            print(f"[grammar-analyzer] using model={model_name}")
            print(f"[grammar-analyzer] calling gemini model={model_name}")
            diagnostic["selected_model"] = model_name
            diagnostic["gemini_called"] = True
            call_started = time.perf_counter()
            raw_response = call_gemini(prompt, model_name=model_name)
            call_elapsed_ms = round((time.perf_counter() - call_started) * 1000)
            print(f"[grammar-analyzer] gemini response received elapsed_ms={call_elapsed_ms}")
            try:
                ai_payload = parse_gemini_json_safely(raw_response)
                print("[grammar-analyzer] json parse success=true")
            except Exception as parse_error:
                print("[grammar-analyzer] json parse success=false")
                print(f"[grammar-analyzer] json parse error；model={model_name}；message={parse_error}；raw={raw_response[:500]}")
                raise
            payload = normalize_grammar_analysis(ai_payload, text, fallback_reading, advanced_mecab)
            print(f"[grammar-analyzer] Gemini 解析成功；model={model_name}")
            diagnostic["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            if grammar_debug_enabled():
                payload["debug"] = diagnostic
            persist_analysis_slang_terms(payload, text, "grammar_analyzer")
            return payload, 200
        except Exception as e:
            error_text = str(e)
            error_type = gemini_error_type(e)
            diagnostic["exception_type"] = type(e).__name__
            diagnostic["exception_message"] = error_text[:500]
            failures.append({"model": model_name, "error_type": error_type, "message": error_text[:300]})
            if raw_response:
                print(f"[grammar-analyzer] Gemini 原始回傳；model={model_name}；raw={raw_response[:500]}")
            if error_type == "timeout":
                print(f"[grammar-analyzer] Gemini timeout; model={model_name}; timeout={GEMINI_TIMEOUT_SECONDS}s")
            print(f"[grammar-analyzer] Gemini 解析失敗；model={model_name}；error_type={error_type}；message={error_text}")
            print(traceback.format_exc())
            continue

    if failures:
        reason = choose_gemini_failure_reason(failures)
        if reason == "prepayment_depleted":
            set_gemini_billing_block("grammar analyzer prepayment_depleted")
            billing_snapshot = gemini_billing_snapshot()
            diagnostic["billing_block_active"] = billing_snapshot["billing_block_active"]
            diagnostic["billing_status"] = billing_snapshot["last_billing_status"]
            diagnostic["gemini_billing_block_until"] = billing_snapshot["gemini_billing_block_until_iso"]
            diagnostic["prepayment_depleted"] = billing_snapshot["prepayment_depleted"]
        diagnostic["failures"] = failures
        diagnostic["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        print(f"[grammar-analyzer] fallback reason={reason}")
        print(f"[grammar-analyzer] 所有 Gemini 模型皆失敗；failures={json.dumps(failures, ensure_ascii=False)}")
    else:
        reason = "unknown_error"
        diagnostic["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        print("[grammar-analyzer] fallback reason=unknown_error")
    payload = grammar_fallback_response(text, fallback_reading, advanced_mecab)
    if reason == "prepayment_depleted":
        payload["error_message"] = gemini_billing_block_message()
        payload["naturalness_check"]["reason"] = gemini_billing_block_message()
        payload["sentence_summary"] = "Gemini API 帳務保護機制已啟動，目前僅回傳本地規則偵測結果。"
    if grammar_debug_enabled():
        payload["fallback_reason"] = reason
        payload["debug"] = diagnostic
    persist_analysis_slang_terms(payload, text, "grammar_analyzer_fallback")
    return payload, 200


def handle_grammar_analyzer_api():
    started = time.perf_counter()
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify(grammar_not_japanese_response()), 400
    if not is_probably_japanese_text(text):
        return jsonify(grammar_not_japanese_response())
    payload, status = analyze_grammar_with_gemini(text)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    print(f"[perf] grammar_analyzer ms={elapsed_ms}")
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


@app.get("/api/vocab-rules")
def api_vocab_rules():
    return jsonify({"ok": True, **build_vocab_rules_payload(create_missing=False)})


@app.post("/api/vocab-rules")
def api_save_vocab_rules():
    data = request.get_json(silent=True) or {}
    rules = data.get("rules")
    if not isinstance(rules, list):
        return jsonify({"ok": False, "error": "invalid_rules_payload", "message": "\u8acb\u63d0\u4f9b\u8981\u5132\u5b58\u7684\u55ae\u5b57\u51fa\u73fe\u898f\u5247\u3002"}), 400
    try:
        result = insert_or_update_vocab_rules(rules)
        return jsonify({"ok": True, "success": True, "message": "\u55ae\u5b57\u51fa\u73fe\u898f\u5247\u5df2\u5132\u5b58\u3002", **result})
    except Exception as exc:
        print(f"[vocab-rules] save failed; reason={exc}")
        print(traceback.format_exc())
        return jsonify({"ok": False, "error": "vocab_rules_save_failed", "message": "\u55ae\u5b57\u51fa\u73fe\u898f\u5247\u4fdd\u5b58\u5931\u6557\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66\u3002"}), 500


@app.post("/api/vocab-rules/sync")
def api_sync_vocab_rules():
    try:
        before = {row["rule_key"] for row in load_vocab_rule_rows()}
        payload = build_vocab_rules_payload(create_missing=True)
        after = {row["rule_key"] for row in load_vocab_rule_rows()}
        return jsonify({"ok": True, "success": True, "message": "\u5df2\u540c\u6b65\u76ee\u524d\u8a5e\u5eab\u985e\u578b\u3002", "created_count": len(after - before), **payload})
    except Exception as exc:
        print(f"[vocab-rules] sync failed; reason={exc}")
        print(traceback.format_exc())
        return jsonify({"ok": False, "error": "vocab_rules_sync_failed", "message": "\u540c\u6b65\u8a5e\u5eab\u985e\u578b\u5931\u6557\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66\u3002"}), 500


@app.post("/api/vocab-rules/reset-defaults")
def api_reset_vocab_rules():
    try:
        ensure_vocab_rules_store()
        if DATABASE_URL:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM vocab_appearance_rules")
                conn.commit()
        else:
            with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
                conn.execute("DELETE FROM vocab_appearance_rules")
                conn.commit()
        insert_or_update_vocab_rules(default_vocab_rule_seed())
        return jsonify({"ok": True, "success": True, "message": "\u5df2\u9084\u539f\u9810\u8a2d\u898f\u5247\u3002", **build_vocab_rules_payload(create_missing=False)})
    except Exception as exc:
        print(f"[vocab-rules] reset failed; reason={exc}")
        print(traceback.format_exc())
        return jsonify({"ok": False, "error": "vocab_rules_reset_failed", "message": "\u9084\u539f\u9810\u8a2d\u898f\u5247\u5931\u6557\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66\u3002"}), 500


@app.get("/api/settings")
def api_get_settings():
    return jsonify(load_settings())


@app.post("/api/settings")
def api_save_settings():
    return jsonify(save_settings_file(request.get_json(silent=True) or {}))


@app.get("/api/archive-dates")
def api_archive_dates():
    started = time.perf_counter()
    now_ts = taipei_now().timestamp()
    if _ARCHIVE_DATES_CACHE["payload"] is not None and _ARCHIVE_DATES_CACHE["expires_at"] and _ARCHIVE_DATES_CACHE["expires_at"] > now_ts:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        print(f"[perf] api_archive_dates ms={elapsed_ms} cached=true")
        return jsonify(_ARCHIVE_DATES_CACHE["payload"])

    limit = max(1, min(int(request.args.get("limit", "90") or 90), 365))
    ensure_database()
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT date
                    FROM materials
                    WHERE COALESCE(date, '') <> ''
                    GROUP BY date
                    ORDER BY MAX(id) DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                dates = [row[0] for row in cur.fetchall() if row and row[0]]
    else:
        try:
            df = pd.read_csv(
                DATABASE_FILE,
                dtype=str,
                keep_default_na=False,
                encoding="utf-8-sig",
                usecols=["date"],
            )
            dates = [d for d in df["date"].drop_duplicates().tolist() if d]
        except (FileNotFoundError, ValueError):
            dates = []
    normalized_dates = []
    seen_dates = set()
    for value in dates:
        normalized = material_date_display(value)
        parsed = parse_material_date(normalized)
        sort_key = parsed or datetime.min.date()
        if normalized not in seen_dates:
            seen_dates.add(normalized)
            normalized_dates.append((normalized, sort_key))
    dates = [item[0] for item in sorted(normalized_dates, key=lambda item: item[1], reverse=True)[:limit]]

    _ARCHIVE_DATES_CACHE["payload"] = dates
    _ARCHIVE_DATES_CACHE["expires_at"] = now_ts + ARCHIVE_DATES_CACHE_TTL_SECONDS
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    print(f"[perf] api_archive_dates ms={elapsed_ms} cached=false")
    return jsonify(dates)


@app.get("/api/materials")
def api_materials():
    started = time.perf_counter()
    payload = material_by_date(request.args.get("date", today_string()))
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    print(f"[perf] load_daily_material ms={elapsed_ms}")
    return jsonify(payload)


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
        return jsonify({"ok": False, "error": "local_generation_failed", **material_generation_error_payload(e)}), 500


@app.get("/api/cron/daily-push")
def api_cron_daily_push():
    if CRON_SECRET and request.args.get("secret") != CRON_SECRET:
        return jsonify({"ok": False, "error": "unauthorized", "message": "unauthorized"}), 401
    try:
        return jsonify(run_daily_schedule(app_url=APP_URL, mode=request.args.get("mode", "local"))), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "daily_schedule_failed", **material_generation_error_payload(e)}), 500


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
    if not grammar_debug_enabled():
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
    if recommended:
        billing_status = "ok"
        gemini_available = True
        clear_gemini_billing_block(recommended_model=recommended, reason="model-check ok")
    elif not GEMINI_API_KEY:
        billing_status = "missing_api_key"
        gemini_available = False
    elif any(item.get("error_type") == "prepayment_depleted" for item in models):
        billing_status = "prepayment_depleted"
        gemini_available = False
        set_gemini_billing_block("model-check prepayment_depleted")
    else:
        billing_status = "error"
        gemini_available = False
    billing_snapshot = gemini_billing_snapshot()
    return jsonify(
        {
            "api_key_present": bool(GEMINI_API_KEY),
            "gemini_available": gemini_available,
            "billing_status": billing_status,
            "billing_block_active": billing_snapshot["billing_block_active"],
            "prepayment_depleted": billing_snapshot["prepayment_depleted"],
            "gemini_billing_block_until": billing_snapshot["gemini_billing_block_until_iso"],
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
    started = time.perf_counter()
    now_ts = taipei_now().timestamp()
    if _DASHBOARD_CACHE["payload"] is not None and _DASHBOARD_CACHE["expires_at"] and _DASHBOARD_CACHE["expires_at"] > now_ts:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        print(f"[perf] dashboard_summary ms={elapsed_ms} cached=true")
        return jsonify(_DASHBOARD_CACHE["payload"])
    payload = safe_dashboard_payload()
    _DASHBOARD_CACHE["payload"] = payload
    _DASHBOARD_CACHE["expires_at"] = now_ts + DASHBOARD_CACHE_TTL_SECONDS
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    print(f"[perf] dashboard_summary ms={elapsed_ms} cached=false")
    return jsonify(payload)


@app.get("/api/dashboard/summary")
def api_dashboard_summary():
    return api_dashboard()


def dashboard_safe_dates():
    now = taipei_now()
    return [
        {
            "date": (now - timedelta(days=i)).date().isoformat(),
            "label": f"{(now - timedelta(days=i)).month}/{(now - timedelta(days=i)).day}",
            "studied": False,
            "active": False,
        }
        for i in reversed(range(7))
    ]


def activity_iso_date(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if "T" in text or text.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", text):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Taipei"))
            return parsed.astimezone(ZoneInfo("Asia/Taipei")).date().isoformat()
        except ValueError:
            pass
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Taipei"))
    return parsed.astimezone(ZoneInfo("Asia/Taipei")).date().isoformat()


def sqlite_table_columns(table_name):
    ensure_settings_store()
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def add_activity_source(active_map, date_value, source):
    iso_date = activity_iso_date(date_value)
    if iso_date in active_map:
        active_map[iso_date].add(source)


def add_sqlite_activity_sources(active_map, table_name, columns, source):
    try:
        existing_columns = sqlite_table_columns(table_name)
        usable_columns = [column for column in columns if column in existing_columns]
        if not usable_columns:
            return
        select_sql = ", ".join(usable_columns)
        filters = []
        params = []
        for column in usable_columns:
            for iso_date in active_map:
                try:
                    parsed = datetime.strptime(iso_date, "%Y-%m-%d").date()
                    slash_date = f"{parsed.year}/{parsed.month}/{parsed.day}"
                except ValueError:
                    slash_date = iso_date
                filters.append(f"{column} LIKE ?")
                params.append(f"{iso_date}%")
                filters.append(f"{column} LIKE ?")
                params.append(f"{slash_date}%")
        where_sql = f" WHERE {' OR '.join(filters)}" if filters else ""
        rows = sqlite_dicts(f"SELECT {select_sql} FROM {table_name}{where_sql}", tuple(params))
        for row in rows:
            for column in usable_columns:
                add_activity_source(active_map, row.get(column), source)
    except Exception as e:
        print(f"[dashboard-summary] activity source skipped; source={source}; reason={e}")


def add_material_activity_sources(active_map):
    candidate_dates = []
    for iso_date in active_map:
        try:
            parsed = datetime.strptime(iso_date, "%Y-%m-%d").date()
            candidate_dates.extend([f"{parsed.year}/{parsed.month}/{parsed.day}", parsed.isoformat()])
        except ValueError:
            candidate_dates.extend(material_date_variants(iso_date))
    candidate_dates = list(dict.fromkeys(candidate_dates))
    try:
        if DATABASE_URL:
            ensure_database()
            placeholders = ", ".join(["%s"] * len(candidate_dates))
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT DISTINCT date FROM materials WHERE date IN ({placeholders})",
                        tuple(candidate_dates),
                    )
                    rows = cur.fetchall()
            for (date_value,) in rows:
                add_activity_source(active_map, date_value, "daily_materials")
            return
        df = read_database()
        if df.empty:
            return
        date_values = set(candidate_dates) | set(active_map.keys())
        rows = df[df["date"].isin(date_values)] if "date" in df.columns else df
        for _, row in rows.iterrows():
            add_activity_source(active_map, row.get("date"), "daily_materials")
            add_activity_source(active_map, row.get("created_at"), "daily_materials")
            add_activity_source(active_map, row.get("updated_at"), "daily_materials")
    except Exception as e:
        print(f"[dashboard-summary] material activity source failed; reason={e}")


def get_active_days_last_7():
    started = time.perf_counter()
    base_days = dashboard_safe_dates()
    active_map = {day["date"]: set() for day in base_days}

    add_material_activity_sources(active_map)

    activity_sources = [
        ("quiz_records", ["created_at"], "quiz_records"),
        ("mistake_logs", ["last_reviewed_at", "reviewed_at", "created_at", "updated_at"], "mistake_logs"),
        ("sns_practice_logs", ["created_at"], "sns_practice_logs"),
        ("learning_logs", ["created_at", "date", "activity_date"], "learning_logs"),
        ("daily_records", ["created_at", "date", "activity_date"], "daily_records"),
        ("quiz_results", ["created_at", "date", "activity_date"], "quiz_results"),
        ("test_results", ["created_at", "date", "activity_date"], "test_results"),
        ("wrong_answers", ["created_at", "last_reviewed_at", "date"], "wrong_answers"),
        ("wrong_answer_reviews", ["created_at", "reviewed_at", "date"], "wrong_answer_reviews"),
        ("grammar_analysis_logs", ["created_at", "date"], "grammar_analysis_logs"),
        ("daily_activity_logs", ["created_at", "date", "activity_date"], "daily_activity_logs"),
        ("daily_material_views", ["created_at", "date", "viewed_at"], "daily_material_views"),
    ]
    for table_name, columns, source in activity_sources:
        add_sqlite_activity_sources(active_map, table_name, columns, source)

    days = []
    for day in base_days:
        sources = sorted(active_map[day["date"]])
        days.append(
            {
                "date": day["date"],
                "label": day["label"],
                "active": bool(sources),
                "studied": bool(sources),
                "sources": sources,
            }
        )
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    print(f"[perf] active_days_query ms={elapsed_ms}")
    return {"active_days_last_7": sum(1 for day in days if day["active"]), "days": days}


def dashboard_default_payload(reason=""):
    settings = load_settings()
    today = today_string()
    quiz_total = int(settings.get("mcq_count", 0) or 0) + int(settings.get("fill_count", 0) or 0)
    days = dashboard_safe_dates()
    payload = {
        "today": today,
        "today_iso": today_iso_date(),
        "has_today_material": False,
        "target_level": settings["target_level"],
        "vocab_count": 0,
        "verb_count": 0,
        "quiz_total": quiz_total,
        "quiz_completed": 0,
        "quiz_accuracy_text": "尚無紀錄",
        "today_new_mistakes": 0,
        "due_review_count": 0,
        "last_7_days": days,
        "streak_days": 0,
        "review_items": [],
        "dashboard_warning": reason,
        "today_material": {
            "status": "not_generated",
            "date": today,
            "target_level": settings["target_level"],
            "word_count": 0,
            "verb_count": 0,
        },
        "quiz": {
            "completed": 0,
            "total": quiz_total,
            "accuracy_text": "尚無紀錄",
        },
        "learning_streak": {
            "active_days_last_7": 0,
            "days": days,
        },
        "review": {
            "due_count": 0,
            "message": "目前沒有待複習錯題。",
        },
    }
    return payload


def safe_dashboard_payload():
    try:
        payload = build_dashboard_payload()
    except Exception as e:
        print(f"[dashboard-summary] failed; reason={e}")
        print(traceback.format_exc())
        payload = dashboard_default_payload("統計資料暫時無法取得。")
    return payload


def build_dashboard_payload():
    payload = dashboard_default_payload()
    settings = load_settings()
    today = today_string()
    today_iso = today_iso_date()

    try:
        today_material = material_by_date(today)
        payload["has_today_material"] = bool(today_material)
        payload["vocab_count"] = len(today_material["vocabulary"]) if today_material else 0
        payload["verb_count"] = len(today_material["verbs"]) if today_material else 0
        payload["today_material"] = {
            "status": "generated" if today_material else "not_generated",
            "date": today,
            "target_level": today_material.get("targetLevel") if today_material else settings["target_level"],
            "word_count": payload["vocab_count"],
            "verb_count": payload["verb_count"],
        }
    except Exception as e:
        print(f"[dashboard-summary] material query failed; reason={e}")
        today_material = None

    try:
        learning_streak = get_active_days_last_7()
        days = learning_streak["days"]
        payload["last_7_days"] = days
        payload["streak_days"] = learning_streak["active_days_last_7"]
        payload["learning_streak"] = learning_streak
    except Exception as e:
        print(f"[dashboard-summary] streak query failed; reason={e}")

    try:
        today_quiz = sqlite_one(
            """
            SELECT COALESCE(SUM(total_questions), 0) AS total_questions,
                   COALESCE(SUM(correct_count), 0) AS correct_count
            FROM quiz_records
            WHERE substr(created_at, 1, 10) = ?
            """,
            (today_iso,),
        )
        completed = int(today_quiz.get("total_questions", 0) if today_quiz else 0)
        correct = int(today_quiz.get("correct_count", 0) if today_quiz else 0)
        accuracy_text = "尚無紀錄" if completed <= 0 else f"{round((correct / completed) * 100)}%"
        payload["quiz_completed"] = completed
        payload["quiz_accuracy_text"] = accuracy_text
        payload["quiz"] = {"completed": completed, "total": payload["quiz_total"], "accuracy_text": accuracy_text}
    except Exception as e:
        print(f"[dashboard-summary] quiz query failed; reason={e}")

    try:
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
            (today_iso, today_iso),
        )
        due_count = int(due_review["count"] if due_review else 0)
        payload["today_new_mistakes"] = len(today_mistakes)
        payload["due_review_count"] = due_count
        payload["review_items"] = query_mistakes({}, limit=5)
        payload["review"] = {
            "due_count": due_count,
            "message": "請前往「錯題複習」頁面完成今日複習。" if due_count > 0 else "目前沒有待複習錯題。",
        }
    except Exception as e:
        print(f"[dashboard-summary] review query failed; reason={e}")

    print(f"[dashboard-summary] material_status={payload['today_material']['status']}")
    print(f"[dashboard-summary] quiz completed={payload['quiz']['completed']} total={payload['quiz']['total']}")
    print(f"[dashboard-summary] active_days_last_7={payload['learning_streak']['active_days_last_7']}")
    print(f"[dashboard-summary] review_due_count={payload['review']['due_count']}")
    return payload


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


def initialize_runtime_schema():
    try:
        ensure_database()
        ensure_settings_store()
    except Exception as e:
        print(f"[startup] schema initialization failed; will retry on demand; reason={e}")
        print(traceback.format_exc())


initialize_runtime_schema()


if __name__ == "__main__":
    ensure_database()
    ensure_settings_store()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
