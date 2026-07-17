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
import uuid
from collections import Counter
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd
from flask import Flask, has_request_context, jsonify, render_template, request
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
POSTGRES_SETTINGS_TABLE = "app_settings"
SNS_EXAMPLES_FILE = os.path.join(BASE_DIR, "data", "social_examples.json")
VOCABULARY_SEED_BASIC_FILE = os.path.join(BASE_DIR, "data", "vocabulary_seed_n5_n3.json")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
GEMINI_TIMEOUT_SECONDS = read_int_env("GEMINI_TIMEOUT_SECONDS", 40, 5, 60)
GEMINI_GENERATE_TIMEOUT_SECONDS = read_int_env("GEMINI_GENERATE_TIMEOUT_SECONDS", 50, 5, 55)
GEMINI_STAGE_TIMEOUT_SECONDS = read_int_env("GEMINI_STAGE_TIMEOUT_SECONDS", 18, 5, 25)
GEMINI_BANK_STAGE_TIMEOUT_SECONDS = read_int_env("GEMINI_BANK_STAGE_TIMEOUT_SECONDS", 15, 5, 20)
GEMINI_BANK_REFILL_BATCH_SIZE = read_int_env("GEMINI_BANK_REFILL_BATCH_SIZE", 3, 1, 3)
GEMINI_BANK_MIN_WORD_PER_LEVEL = read_int_env("GEMINI_BANK_MIN_WORD_PER_LEVEL", 20, 1, 100)
GEMINI_BANK_MIN_VERB_PER_LEVEL = read_int_env("GEMINI_BANK_MIN_VERB_PER_LEVEL", 15, 1, 100)
GEMINI_BANK_MIN_GRAMMAR_PER_LEVEL = read_int_env("GEMINI_BANK_MIN_GRAMMAR_PER_LEVEL", 10, 1, 100)
GEMINI_BANK_TARGET_WORD_PER_LEVEL = read_int_env("GEMINI_BANK_TARGET_WORD_PER_LEVEL", 50, 1, 200)
GEMINI_BANK_TARGET_VERB_PER_LEVEL = read_int_env("GEMINI_BANK_TARGET_VERB_PER_LEVEL", 30, 1, 200)
GEMINI_BANK_TARGET_GRAMMAR_PER_LEVEL = read_int_env("GEMINI_BANK_TARGET_GRAMMAR_PER_LEVEL", 15, 1, 100)
GEMINI_BANK_TARGET_SNS = read_int_env("GEMINI_BANK_TARGET_SNS", 20, 1, 100)
GEMINI_BANK_MAX_REFILL_ATTEMPTS_PER_POOL = read_int_env("GEMINI_BANK_MAX_REFILL_ATTEMPTS_PER_POOL", 20, 1, 100)
GEMINI_BANK_ALLOW_RECENT_REUSE = os.environ.get("GEMINI_BANK_ALLOW_RECENT_REUSE", "false").strip().lower() in {"1", "true", "yes", "on"}

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview").strip()
GEMINI_MODEL_CANDIDATES = os.environ.get(
    "GEMINI_MODEL_CANDIDATES",
    "gemini-2.5-flash-lite,gemini-2.5-flash,gemini-2.0-flash-lite,gemini-2.0-flash",
).strip()
GEMINI_BILLING_BLOCK_SECONDS = read_int_env("GEMINI_BILLING_BLOCK_SECONDS", 600, 60, 86400)
TG_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_TOKEN", "")).strip()
TG_CHAT_ID = (os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID", "")).strip()
TELEGRAM_REMINDER_SECRET = os.environ.get("TELEGRAM_REMINDER_SECRET", "").strip()
APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:5000").rstrip("/")
CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()
DASHBOARD_CACHE_TTL_SECONDS = int(os.environ.get("DASHBOARD_CACHE_TTL_SECONDS", "90"))
ARCHIVE_DATES_CACHE_TTL_SECONDS = int(os.environ.get("ARCHIVE_DATES_CACHE_TTL_SECONDS", "60"))
MANUAL_GENERATE_TIMEOUT_SECONDS = read_int_env("MANUAL_GENERATE_TIMEOUT_SECONDS", 20, 1, 55)
LOCAL_GENERATION_SAFE_MODE_DEFAULT = os.environ.get("LOCAL_GENERATION_SAFE_MODE", "true").strip().lower()
LOCAL_SELECTION_COOLDOWN_DAYS = read_int_env("LOCAL_SELECTION_COOLDOWN_DAYS", 7, 0, 30)
LOCAL_SELECTION_ALLOW_COOLDOWN_RELAX = os.environ.get("LOCAL_SELECTION_ALLOW_COOLDOWN_RELAX", "false").strip().lower() == "true"
LOCAL_GRAMMAR_COOLDOWN_DAYS = read_int_env("LOCAL_GRAMMAR_COOLDOWN_DAYS", 7, 0, 30)
GRAMMAR_FALLBACK_ADJACENT_LEVELS = os.environ.get("GRAMMAR_FALLBACK_ADJACENT_LEVELS", "true").strip().lower() != "false"
RUN_MIGRATIONS_ON_REQUEST = os.environ.get("RUN_MIGRATIONS_ON_REQUEST", "false").strip().lower() == "true"
RUN_MIGRATIONS_ON_STARTUP = os.environ.get("RUN_MIGRATIONS_ON_STARTUP", "false").strip().lower() == "true"
DB_CONNECT_TIMEOUT_SECONDS = read_int_env("DB_CONNECT_TIMEOUT_SECONDS", 5, 1, 20)
_DASHBOARD_CACHE = {"expires_at": None, "payload": None}
_ARCHIVE_DATES_CACHE = {"expires_at": None, "payload": None}
_BASIC_SEED_VOCAB_CACHE = None
_VOCAB_POOL_DB_UNAVAILABLE_UNTIL = 0.0
_SCHEMA_LOCK = threading.Lock()
_SETTINGS_SCHEMA_READY = False
_MATERIALS_SCHEMA_READY = False
_GEMINI_JOBS_SCHEMA_READY = False
_GEMINI_BANK_SCHEMA_READY = False
_GEMINI_BILLING_LOCK = threading.Lock()
_GEMINI_BILLING_STATE = {
    "prepayment_depleted": False,
    "gemini_billing_block_until": 0.0,
    "last_model_check_ok_at": 0.0,
    "last_billing_status": "unknown",
    "last_recommended_model": "",
}

LEVELS = ["N5", "N4", "N3", "N2", "N1"]
TARGET_LEVEL_CHOICES = LEVELS + ["SNS"]
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
PRACTICE_QUESTION_TYPE_ALIASES = {
    "masu_stem": "renyou_form",
    "renyou": "renyou_form",
    "te": "te_form",
    "ta": "ta_form",
    "nai": "nai_form",
    "ba": "ba_form",
    "causative": "shieki_form",
    "causative_form": "shieki_form",
    "passive": "ukemi_form",
    "passive_form": "ukemi_form",
}


class ManualGenerateTimeout(Exception):
    def __init__(self, stage, elapsed_ms, timeout_seconds):
        super().__init__("manual generation exceeded time budget")
        self.stage = stage
        self.elapsed_ms = elapsed_ms
        self.timeout_seconds = timeout_seconds


def manual_generate_timeout_payload(error):
    return {
        "ok": False,
        "error": "manual_generate_timeout",
        "reason": "manual generation exceeded time budget",
        "message": "本地生成逾時，已放棄。請稍後再試或降低生成數量。",
        "stage": getattr(error, "stage", "unknown"),
        "elapsed_ms": getattr(error, "elapsed_ms", 0),
        "timeout_seconds": getattr(error, "timeout_seconds", MANUAL_GENERATE_TIMEOUT_SECONDS),
    }


def run_manual_generation_with_timeout(operation, timeout_seconds=None, started_at=None):
    timeout_seconds = timeout_seconds or MANUAL_GENERATE_TIMEOUT_SECONDS
    started_at = started_at or time.monotonic()
    deadline = started_at + timeout_seconds
    stage_state = {"stage": "start"}
    result = {}

    def deadline_check(stage):
        stage_state["stage"] = stage
        elapsed_ms = round((time.monotonic() - started_at) * 1000)
        if time.monotonic() >= deadline:
            raise ManualGenerateTimeout(stage, elapsed_ms, timeout_seconds)

    def worker():
        try:
            result["value"] = operation(deadline_check)
        except Exception as exc:
            result["error"] = exc

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ManualGenerateTimeout(stage_state["stage"], round((time.monotonic() - started_at) * 1000), timeout_seconds)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(remaining)
    if thread.is_alive():
        elapsed_ms = round((time.monotonic() - started_at) * 1000)
        raise ManualGenerateTimeout(stage_state["stage"], elapsed_ms, timeout_seconds)
    if "error" in result:
        raise result["error"]
    return result.get("value")
PRACTICE_CANONICAL_FORM_ALIASES = {
    "renyou_form": ("renyou_form", "masu_stem", "renyou"),
    "te_form": ("te_form", "te"),
    "ta_form": ("ta_form", "ta"),
    "nai_form": ("nai_form", "nai"),
    "ba_form": ("ba_form", "ba"),
    "tara_form": ("tara_form", "tara"),
    "volitional_form": ("volitional_form", "volitional"),
    "potential_form": ("potential_form", "potential"),
    "shieki_form": ("shieki_form", "causative_form", "causative"),
    "ukemi_form": ("ukemi_form", "passive_form", "passive"),
    "causative_passive_form": ("causative_passive_form", "causative_passive", "causativePassive"),
}
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
    "material_key",
    "material_date",
    "version_no",
    "generation_source",
    "generation_mode",
    "is_latest",
    "ai_used",
    "source_summary",
    "created_at",
    "updated_at",
]

DEFAULT_SETTINGS = {
    "target_level": "N3",
    "target_levels": "[\"N3\"]",
    "vocab_count": "15",
    "verb_count": "10",
    "mcq_count": "5",
    "fill_count": "5",
    "grammar_level": "N5",
    "grammar_count": "3",
}

VOCAB_RULE_GROUPS = {
    "jlpt_level": "JLPT 等級",
    "category": "單字分類",
}
VOCAB_RULE_SOURCE_TYPES = set(VOCAB_RULE_GROUPS.keys())
VOCAB_RULE_VISIBLE_TYPES = {"jlpt_level", "category"}
EMPTY_RULE_VALUE = "__empty__"
EMPTY_RULE_LABELS = {
    "jlpt_level": "未分類 JLPT",
    "category": "未分類 category",
}
VOCAB_RULE_PERIODS = {"daily", "weekly", "monthly"}
JLPT_ADJACENCY = {
    "N5": ["N5", "N4", "N3"],
    "N4": ["N4", "N5", "N3"],
    "N3": ["N3", "N4", "N2", "N5"],
    "N2": ["N2", "N3", "N1"],
    "N1": ["N1", "N2", "N3"],
}
SNS_RULE_CATEGORIES = {"slang", "internet_slang", "otaku_culture", "approved_slang"}
SIX_MAIN_VOCAB_RULE_ORDER = ["jlpt:N5", "jlpt:N4", "jlpt:N3", "jlpt:N2", "jlpt:N1", "category:SNS"]
SIX_MAIN_VOCAB_RULE_DEFAULTS = {
    "jlpt:N5": {
        "rule_key": "jlpt:N5",
        "display_name": "N5",
        "group_key": "main_vocab_rules",
        "group_name": "單字出現設定",
        "source_type": "jlpt_level",
        "match_value": "N5",
        "enabled": True,
        "period": "daily",
        "quota_count": 20,
        "priority": 90,
        "max_per_material": 20,
        "min_per_material": 0,
        "strict_mode": False,
        "is_system_default": True,
    },
    "jlpt:N4": {
        "rule_key": "jlpt:N4",
        "display_name": "N4",
        "group_key": "main_vocab_rules",
        "group_name": "單字出現設定",
        "source_type": "jlpt_level",
        "match_value": "N4",
        "enabled": True,
        "period": "daily",
        "quota_count": 5,
        "priority": 80,
        "max_per_material": 5,
        "min_per_material": 0,
        "strict_mode": False,
        "is_system_default": True,
    },
    "jlpt:N3": {
        "rule_key": "jlpt:N3",
        "display_name": "N3",
        "group_key": "main_vocab_rules",
        "group_name": "單字出現設定",
        "source_type": "jlpt_level",
        "match_value": "N3",
        "enabled": True,
        "period": "weekly",
        "quota_count": 3,
        "priority": 70,
        "max_per_material": 3,
        "min_per_material": 0,
        "strict_mode": False,
        "is_system_default": True,
    },
    "jlpt:N2": {
        "rule_key": "jlpt:N2",
        "display_name": "N2",
        "group_key": "main_vocab_rules",
        "group_name": "單字出現設定",
        "source_type": "jlpt_level",
        "match_value": "N2",
        "enabled": True,
        "period": "monthly",
        "quota_count": 1,
        "priority": 50,
        "max_per_material": 1,
        "min_per_material": 0,
        "strict_mode": False,
        "is_system_default": True,
    },
    "jlpt:N1": {
        "rule_key": "jlpt:N1",
        "display_name": "N1",
        "group_key": "main_vocab_rules",
        "group_name": "單字出現設定",
        "source_type": "jlpt_level",
        "match_value": "N1",
        "enabled": True,
        "period": "monthly",
        "quota_count": 1,
        "priority": 40,
        "max_per_material": 1,
        "min_per_material": 0,
        "strict_mode": False,
        "is_system_default": True,
    },
    "category:SNS": {
        "rule_key": "category:SNS",
        "display_name": "SNS 詞類",
        "group_key": "main_vocab_rules",
        "group_name": "單字出現設定",
        "source_type": "category",
        "match_value": "SNS",
        "enabled": True,
        "period": "weekly",
        "quota_count": 1,
        "priority": 20,
        "max_per_material": 1,
        "min_per_material": 0,
        "strict_mode": False,
        "is_system_default": True,
    },
}
SIX_MAIN_VOCAB_RULE_KEYS = set(SIX_MAIN_VOCAB_RULE_DEFAULTS)
LEGACY_MAIN_VOCAB_RULE_KEYS = {
    "jlpt:N5": "jlpt_level:N5",
    "jlpt:N4": "jlpt_level:N4",
    "jlpt:N3": "jlpt_level:N3",
    "jlpt:N2": "jlpt_level:N2",
    "jlpt:N1": "jlpt_level:N1",
}
GRAMMAR_LEVEL_FALLBACKS = {
    "N5": ["N5"],
    "N4": ["N4", "N5"],
    "N3": ["N3", "N4", "N5"],
    "N2": ["N2", "N3", "N4"],
    "N1": ["N1", "N2", "N3"],
}
DEFAULT_GRAMMAR_COUNT_BY_LEVEL = {"N5": 3, "N4": 2, "N3": 2, "N2": 1, "N1": 1}
LOCAL_FIXED_GRAMMAR_QUOTA = {"N1": 1, "N2": 1, "N3": 1, "N4": 2, "N5": 2}
LOCAL_FIXED_GRAMMAR_TOTAL = sum(LOCAL_FIXED_GRAMMAR_QUOTA.values())
GRAMMAR_ADJACENT_FALLBACKS = {
    "N5": ["N5", "N4"],
    "N4": ["N4", "N5"],
    "N3": ["N3", "N4", "N5"],
    "N2": ["N2", "N3", "N4"],
    "N1": ["N1", "N2", "N3"],
}


SETTING_ALIASES = {
    "targetLevel": "target_level",
    "targetLevels": "target_levels",
    "vocabCount": "vocab_count",
    "wordCount": "vocab_count",
    "word_count": "vocab_count",
    "verbCount": "verb_count",
    "quizMcqCount": "mcq_count",
    "choiceCount": "mcq_count",
    "choice_count": "mcq_count",
    "quizFillCount": "fill_count",
    "fillCount": "fill_count",
    "grammarLevel": "grammar_level",
    "grammarCount": "grammar_count",
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


def canonical_material_date(value=None):
    return material_date_iso(value) or today_iso_date()


def build_material_key(material_date, version_no):
    return f"{canonical_material_date(material_date)}-{int(version_no or 1)}"


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


def local_selection_cooldown_sequence():
    base_days = max(0, int(LOCAL_SELECTION_COOLDOWN_DAYS or 0))
    if base_days <= 0:
        return (0,)
    if not LOCAL_SELECTION_ALLOW_COOLDOWN_RELAX:
        return (base_days,)
    sequence = [base_days]
    for days in (14, 7, 3, 0):
        if days < base_days and days not in sequence:
            sequence.append(days)
    if 0 not in sequence:
        sequence.append(0)
    return tuple(sequence)


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


def normalize_target_levels(value, fallback_level=None):
    raw_items = []
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    elif isinstance(value, str):
        text = value.strip()
        if text:
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                    raw_items = parsed if isinstance(parsed, list) else []
                except (TypeError, ValueError):
                    raw_items = []
            if not raw_items:
                raw_items = re.split(r"[,，\s]+", text)
    elif value:
        raw_items = [value]

    selected = []
    for item in raw_items:
        token = str(item or "").strip().upper()
        if token in TARGET_LEVEL_CHOICES and token not in selected:
            selected.append(token)

    fallback = str(fallback_level or "").strip().upper()
    if not selected and fallback in LEVELS:
        selected.append(fallback)
    if not selected:
        selected.append("N5")
    return selected


def primary_target_level(target_levels, fallback_level=None):
    for level in normalize_target_levels(target_levels, fallback_level):
        if level in LEVELS:
            return level
    fallback = str(fallback_level or "").strip().upper()
    return fallback if fallback in LEVELS else "N5"


def target_levels_json(target_levels):
    return json.dumps(normalize_target_levels(target_levels), ensure_ascii=False)


def settings_target_levels(settings):
    return normalize_target_levels((settings or {}).get("target_levels"), (settings or {}).get("target_level"))


def target_level_rule_keys(settings):
    keys = []
    for level in settings_target_levels(settings):
        if level in LEVELS:
            keys.append(f"jlpt:{level}")
        elif level == "SNS":
            keys.append("category:SNS")
    if not keys:
        keys.append(f"jlpt:{primary_target_level([], (settings or {}).get('target_level'))}")
    return set(keys)


def normalize_settings(raw):
    normalized = {}
    for key, value in (raw or {}).items():
        normalized[SETTING_ALIASES.get(key, key)] = value

    target_levels_provided = "target_levels" in normalized and str(normalized.get("target_levels") or "") != ""
    settings = DEFAULT_SETTINGS.copy()
    for key, value in normalized.items():
        if key not in settings or str(value) == "":
            continue
        if key == "target_levels":
            settings[key] = target_levels_json(value)
        else:
            settings[key] = str(value)

    if settings["target_level"] not in LEVELS:
        settings["target_level"] = DEFAULT_SETTINGS["target_level"]
    selected_target_levels = normalize_target_levels(
        settings.get("target_levels") if target_levels_provided else None,
        settings.get("target_level"),
    )
    settings["target_levels"] = target_levels_json(selected_target_levels)
    settings["target_level"] = primary_target_level(selected_target_levels, settings.get("target_level"))
    if settings["grammar_level"] not in LEVELS:
        settings["grammar_level"] = DEFAULT_SETTINGS["grammar_level"]

    for key, default, min_value, max_value in [
        ("vocab_count", int(DEFAULT_SETTINGS["vocab_count"]), 1, 20),
        ("verb_count", int(DEFAULT_SETTINGS["verb_count"]), 0, 20),
        ("mcq_count", 5, 0, 30),
        ("fill_count", 5, 0, 30),
        ("grammar_count", 3, 0, 10),
    ]:
        try:
            value = int(settings[key])
        except ValueError:
            value = default
        settings[key] = str(max(min_value, min(value, max_value)))

    return settings


def request_settings_overrides(raw):
    overrides = {}
    for key, value in (raw or {}).items():
        normalized_key = SETTING_ALIASES.get(key, key)
        if value is None:
            continue
        if isinstance(value, str) and value.strip().lower() in {"", "undefined", "null", "nan"}:
            continue
        if normalized_key in DEFAULT_SETTINGS:
            overrides[normalized_key] = value
    return overrides


def trace_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def settings_trace_snapshot(mapping, settings_source=None):
    normalized = {}
    for key, value in (mapping or {}).items():
        normalized[SETTING_ALIASES.get(key, key)] = value
    target_levels = normalize_target_levels(normalized.get("target_levels"), normalized.get("target_level"))
    snapshot = {
        "target_level": primary_target_level(target_levels, normalized.get("target_level")),
        "target_levels": target_levels,
        "word_count": trace_int(normalized.get("vocab_count")),
        "verb_count": trace_int(normalized.get("verb_count")),
        "choice_count": trace_int(normalized.get("mcq_count")),
        "fill_count": trace_int(normalized.get("fill_count")),
        "grammar_level": normalized.get("grammar_level", ""),
        "grammar_count": trace_int(normalized.get("grammar_count")),
    }
    if settings_source:
        snapshot["settings_source"] = settings_source
    return snapshot


def public_settings(settings):
    data = dict(settings or {})
    levels = settings_target_levels(data)
    data["target_levels"] = levels
    data["target_level"] = primary_target_level(levels, data.get("target_level"))
    data["word_count"] = trace_int(data.get("vocab_count"))
    data["verb_count"] = trace_int(data.get("verb_count"))
    data["choice_count"] = trace_int(data.get("mcq_count"))
    data["fill_count"] = trace_int(data.get("fill_count"))
    return data


def build_settings_trace(frontend_payload, db_settings, resolved_settings, settings_source, selector_actual=None):
    resolved_snapshot = settings_trace_snapshot(resolved_settings, settings_source=settings_source)
    return {
        "frontend_payload": settings_trace_snapshot(frontend_payload),
        "db_settings": settings_trace_snapshot(db_settings),
        "resolved_settings": resolved_snapshot,
        "selector_requested": {
            "word_count": resolved_snapshot.get("word_count"),
            "verb_count": resolved_snapshot.get("verb_count"),
        },
        "selector_actual": selector_actual or {"word_count": None, "verb_count": None},
    }


def material_matches_generation_settings(material, settings):
    metadata = (material or {}).get("metadata") or {}
    resolved = metadata.get("resolved_settings") or {}
    count_validation = metadata.get("count_validation") or {}
    if not resolved:
        return False
    resolved_levels = normalize_target_levels(resolved.get("target_levels"), resolved.get("target_level"))
    expected_levels = settings_target_levels(settings)
    checks = {
        "target_levels": resolved_levels == expected_levels,
        "target_level": (resolved.get("target_level") or "") == (settings.get("target_level") or ""),
        "word_count": trace_int(resolved.get("word_count") or count_validation.get("word_count_requested")) == trace_int(settings.get("vocab_count")),
        "verb_count": trace_int(resolved.get("verb_count") or count_validation.get("verb_count_requested")) == trace_int(settings.get("verb_count")),
        "grammar_level": (resolved.get("grammar_level") or "") == (settings.get("grammar_level") or ""),
        "grammar_count": trace_int(resolved.get("grammar_count")) == trace_int(settings.get("grammar_count")),
    }
    if not all(checks.values()):
        print(f"[scheduled-material-generator] existing_material_settings_mismatch checks={checks}")
        return False
    return True


def resolve_generation_settings_with_trace(posted_settings=None, persist=False):
    overrides = request_settings_overrides(posted_settings or {})
    db_settings = load_settings()
    if overrides:
        merged = db_settings.copy()
        merged.update(overrides)
        settings = normalize_settings(merged)
        if persist:
            settings = save_settings_file(settings)
        print(
            "[settings-resolver] "
            f"source=request_payload target_levels={','.join(settings_target_levels(settings))} "
            f"target_level={settings.get('target_level')} word_count={settings.get('vocab_count')} "
            f"verb_count={settings.get('verb_count')} grammar_level={settings.get('grammar_level')} "
            f"grammar_count={settings.get('grammar_count')}"
        )
        return settings, "request_payload", db_settings
    print(
        "[settings-resolver] "
        f"source=db_settings target_levels={','.join(settings_target_levels(db_settings))} "
        f"target_level={db_settings.get('target_level')} word_count={db_settings.get('vocab_count')} "
        f"verb_count={db_settings.get('verb_count')} grammar_level={db_settings.get('grammar_level')} "
        f"grammar_count={db_settings.get('grammar_count')}"
    )
    return db_settings, "db_settings", db_settings


def resolve_generation_settings(posted_settings=None, persist=False):
    settings, settings_source, _db_settings = resolve_generation_settings_with_trace(posted_settings, persist=persist)
    return settings, settings_source


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
        migrate_grammar_points_sqlite(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_quiz_records_created_at ON quiz_records(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mistake_logs_last_reviewed_at ON mistake_logs(last_reviewed_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mistake_logs_next_review_date ON mistake_logs(next_review_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mistake_logs_status_due ON mistake_logs(status, next_review_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mistake_logs_review_due_at ON mistake_logs(review_due_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mistake_logs_first_wrong_at ON mistake_logs(first_wrong_at)")
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_pool_level_category ON vocabulary_pool(jlpt_level, category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_pool_normalized ON vocabulary_pool(normalized_key)")
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
            material_key TEXT,
            material_version_no INTEGER,
            selected_for TEXT,
            created_at TEXT
        )
        """
    )
    vocab_log_columns = {row[1] for row in conn.execute("PRAGMA table_info(vocab_selection_logs)").fetchall()}
    if "material_key" not in vocab_log_columns:
        conn.execute("ALTER TABLE vocab_selection_logs ADD COLUMN material_key TEXT")
    if "material_version_no" not in vocab_log_columns:
        conn.execute("ALTER TABLE vocab_selection_logs ADD COLUMN material_version_no INTEGER")
    if "selected_for" not in vocab_log_columns:
        conn.execute("ALTER TABLE vocab_selection_logs ADD COLUMN selected_for TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_rules_rule_key ON vocab_appearance_rules(rule_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_rules_group_key ON vocab_appearance_rules(group_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_material_date ON vocab_selection_logs(material_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_rule_date ON vocab_selection_logs(rule_key, material_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_key_date ON vocab_selection_logs(normalized_key, material_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_material_key ON vocab_selection_logs(material_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_logs_selected_date_key ON vocab_selection_logs(selected_for, material_date, normalized_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_logs_selected_key_date ON vocab_selection_logs(selected_for, normalized_key, material_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_logs_selected_created ON vocab_selection_logs(selected_for, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_group_date ON vocab_selection_logs(group_key, match_value, material_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_source_date ON vocab_selection_logs(source_type, match_value, material_date)")


def grammar_usage_items_json(usage_items):
    if isinstance(usage_items, str):
        return usage_items
    return json.dumps(usage_items or [], ensure_ascii=False)


def enrich_grammar_seed_row(row, now, usage_items=None):
    row["meaning_zh"] = row.get("meaning_zh") or row.get("usage_summary_zh", "")
    row["connection"] = row.get("connection") or row.get("structure_formula", "")
    row["note_zh"] = row.get("note_zh") or row.get("learning_tip_zh", "")
    row["learning_tip_zh"] = row.get("learning_tip_zh") or row.get("note_zh", "")
    row["common_mistake_zh"] = row.get("common_mistake_zh", "")
    row["fake_name_example"] = row.get("fake_name_example") or row.get("example_hiragana", "")
    row["usage_items"] = grammar_usage_items_json(row.get("usage_items", usage_items or []))
    row["is_active"] = row.get("is_active", True)
    row["used_count"] = row.get("used_count", 0)
    row["last_used_at"] = row.get("last_used_at")
    row["created_at"] = row.get("created_at") or now
    row["updated_at"] = row.get("updated_at") or now
    return row


def n5_grammar_seed_rows():
    now = utc_now_iso()
    rows = [
        {
            "jlpt_level": "N5",
            "grammar_key": "particle_wa_topic",
            "title": "は",
            "display_name": "は：主題提示 / 對比",
            "grammar_type": "particle",
            "usage_summary_zh": "表示句子的主題，也可用來做對比。",
            "usage_detail_zh": "「は」用來提示接下來要說明的主題，中文常可理解為「至於～」「～的話」。也常用在兩個事物的對比中。",
            "structure_formula": "名詞 + は",
            "example_japanese": "これは本です。\nラーメンは好きですが、すしはまあまあです。",
            "example_hiragana": "これはほんです。\nらーめんはすきですが、すしはまあまあです。",
            "example_zh": "這是書。\n拉麵我喜歡，但壽司還好。",
            "common_mistake_zh": "初學者常把「は」當成主詞標記；其實它更像是在提示「接下來要談的主題」。",
            "learning_tip_zh": "看到「は」時，先想成「至於～」，會比較容易抓到句子的焦點。",
            "priority": 100,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "particle_mo_also",
            "title": "も",
            "display_name": "も：也 / 都",
            "grammar_type": "particle",
            "usage_summary_zh": "表示「也」「都」，也可用在全面否定或表示很多次。",
            "usage_detail_zh": "「も」表示前面的項目也符合後面的狀態。若搭配疑問詞與否定，會變成「哪裡都不」「什麼都不」的意思。",
            "structure_formula": "名詞 + も",
            "example_japanese": "この荷物もお願いします。\n明日はどこも行きません。\n何回もダイエットをしたことがあります。",
            "example_hiragana": "このにもつもおねがいします。\nあしたはどこもいきません。\nなんかいもだいえっとをしたことがあります。",
            "example_zh": "這個行李也麻煩了。\n明天哪裡都不去。\n我減肥過很多次。",
            "common_mistake_zh": "「も」不只表示「也」，搭配疑問詞與否定時意思會變成全面否定。",
            "learning_tip_zh": "把「も」記成「同樣納入」會比只背「也」更準。",
            "priority": 98,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "particle_no_possession",
            "title": "の",
            "display_name": "の：所有 / 屬性 / 代名詞化",
            "grammar_type": "particle",
            "usage_summary_zh": "表示所有、屬性、所屬，也可以代替前面提過的名詞。",
            "usage_detail_zh": "「の」常用來連接兩個名詞，表示前面的名詞修飾後面的名詞。也可以用來代替已知名詞，例如「大きいの」表示「大的那個」。",
            "structure_formula": "名詞 + の + 名詞",
            "example_japanese": "日本語の本です。\nもう少し大きいのはありませんか。",
            "example_hiragana": "にほんごのほんです。\nもうすこしおおきいのはありませんか。",
            "example_zh": "這是日文書。\n有沒有再大一點的？",
            "common_mistake_zh": "不要把所有「の」都翻成「的」；有時它是代替前面已知的名詞。",
            "learning_tip_zh": "名詞接名詞時，先檢查中間是否需要「の」來連接修飾關係。",
            "priority": 96,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "particle_wo_object",
            "title": "を",
            "display_name": "を：動作受詞 / 移動經過點",
            "grammar_type": "particle",
            "usage_summary_zh": "表示動作作用的對象，也可表示移動經過的場所或出發點。",
            "usage_detail_zh": "「を」最常用來標示動作的受詞，例如喝果汁、讀書。也可用於表示經過某個空間，或離開某地。",
            "structure_formula": "名詞 + を + 動詞",
            "example_japanese": "ジュースを飲みます。\n公園を散歩します。\n毎朝8時にうちを出ます。",
            "example_hiragana": "じゅーすをのみます。\nこうえんをさんぽします。\nまいあさはちじにうちをでます。",
            "example_zh": "喝果汁。\n在公園散步。\n每天早上八點出門。",
            "common_mistake_zh": "「公園を散歩します」的「を」不是受詞，而是表示移動經過的範圍。",
            "learning_tip_zh": "看到移動動詞時，注意「を」可能表示經過或離開的位置。",
            "priority": 94,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "particle_e_direction",
            "title": "へ",
            "display_name": "へ：方向",
            "grammar_type": "particle",
            "usage_summary_zh": "表示移動的方向。",
            "usage_detail_zh": "「へ」表示朝某個方向移動，重點在方向感，不一定強調最終抵達點。",
            "structure_formula": "場所 + へ + 行く / 来る / 帰る",
            "example_japanese": "フランスへ料理を習いに行きます。",
            "example_hiragana": "ふらんすへりょうりをならいにいきます。",
            "example_zh": "去法國學料理。",
            "common_mistake_zh": "「へ」重點是方向；若要強調到達點，常會使用「に」。",
            "learning_tip_zh": "移動句中可以先分辨你要說的是方向感，還是到達目的地。",
            "priority": 92,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "particle_de_place_method",
            "title": "で",
            "display_name": "で：方式 / 工具 / 動作場所",
            "grammar_type": "particle",
            "usage_summary_zh": "表示動作發生的場所，也可表示手段、工具、交通方式。",
            "usage_detail_zh": "「で」用在動作發生的場所，例如在車站買報紙。也可表示使用某種工具或方式，例如用日文寫報告、搭計程車回家。",
            "structure_formula": "場所 + で + 動作\n工具 / 手段 + で + 動作",
            "example_japanese": "タクシーで家へ帰ります。\n日本語でレポートを書きます。\n駅で新聞を買います。",
            "example_hiragana": "たくしーでいえへかえります。\nにほんごでれぽーとをかきます。\nえきでしんぶんをかいます。",
            "example_zh": "搭計程車回家。\n用日文寫報告。\n在車站買報紙。",
            "common_mistake_zh": "存在場所通常用「に」，動作發生場所通常用「で」。",
            "learning_tip_zh": "問自己「這裡是在做動作嗎？」如果是，常常用「で」。",
            "priority": 90,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "particle_ni_place_time_target",
            "title": "に",
            "display_name": "に：存在場所 / 時間點 / 對象",
            "grammar_type": "particle",
            "usage_summary_zh": "表示存在場所、時間點、目的地或動作對象。",
            "usage_detail_zh": "「に」常用於表示某物存在的位置、動作發生的時間點，也能表示動作指向的對象。",
            "structure_formula": "場所 + に + あります / います\n時間 + に\n對象 + に + 動作",
            "example_japanese": "部屋に猫がいます。\n7月に京都でお祭りがあります。\n先生に質問します。",
            "example_hiragana": "へやにねこがいます。\nしちがつにきょうとでおまつりがあります。\nせんせいにしつもんします。",
            "example_zh": "房間裡有貓。\n七月在京都有祭典。\n向老師提問。",
            "common_mistake_zh": "「に」用途很多，但核心常是指向某個點：位置點、時間點、對象點。",
            "learning_tip_zh": "先把「に」理解成「指向某個點」，再依語境細分意思。",
            "priority": 88,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "particle_to_with_quote",
            "title": "と",
            "display_name": "と：一起 / 完全列舉 / 引用",
            "grammar_type": "particle",
            "usage_summary_zh": "表示一起做事的對象、完整列舉，也可用於引用內容。",
            "usage_detail_zh": "「と」可以表示「和～一起」，也能表示完整列舉。另外放在句子後面時，常用來接「思います」「言います」，表示想法或說話內容。",
            "structure_formula": "名詞 + と\n句子 + と + 思う / 言う",
            "example_japanese": "私は家族と日本へ来ました。\n本屋は花屋とスーパーの間にあります。\n明日雨が降ると思います。",
            "example_hiragana": "わたしはかぞくとにほんへきました。\nほんやははなやとすーぱーのあいだにあります。\nあしたあめがふるとおもいます。",
            "example_zh": "我和家人來日本。\n書店在花店和超市之間。\n我覺得明天會下雨。",
            "common_mistake_zh": "列舉全部項目時用「と」；只舉部分例子時更常用「や」。",
            "learning_tip_zh": "「と」常有「精確連接」的感覺：一起、完整列舉、引用。",
            "priority": 86,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "particle_ya_examples",
            "title": "や",
            "display_name": "や：部分列舉",
            "grammar_type": "particle",
            "usage_summary_zh": "表示列舉部分例子，常和「など」一起使用。",
            "usage_detail_zh": "「や」表示只舉出幾個代表例，不是全部列出。比「と」更有「等等」的感覺。",
            "structure_formula": "名詞 + や + 名詞 + など",
            "example_japanese": "箱の中に古い手紙や写真などがあります。",
            "example_hiragana": "はこのなかにふるいてがみやしゃしんなどがあります。",
            "example_zh": "箱子裡有舊信和照片等等。",
            "common_mistake_zh": "不要把「や」當成完整列舉；它表示還有其他未列出的同類事物。",
            "learning_tip_zh": "如果中文想說「A、B 之類的」，日文常用「AやBなど」。",
            "priority": 84,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "particle_kara_start_reason",
            "title": "から",
            "display_name": "から：起點 / 原因",
            "grammar_type": "particle",
            "usage_summary_zh": "表示時間或地點的起點，也可表示原因。",
            "usage_detail_zh": "「から」可表示「從～開始」，也可放在句子後面表示原因，相當於「因為～」。",
            "structure_formula": "時間 / 地點 + から\n句子 + から",
            "example_japanese": "日本語の授業は1時半からです。\n寒いですから、窓を閉めます。",
            "example_hiragana": "にほんごのじゅぎょうはいちじはんからです。\nさむいですから、まどをしめます。",
            "example_zh": "日文課從一點半開始。\n因為很冷，所以關窗。",
            "common_mistake_zh": "表示原因時，「から」通常放在完整句子後面。",
            "learning_tip_zh": "「から」的核心是起點，可以是時間起點，也可以是理由的起點。",
            "priority": 82,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "particle_made_endpoint",
            "title": "まで",
            "display_name": "まで：終點",
            "grammar_type": "particle",
            "usage_summary_zh": "表示時間或地點的終點。",
            "usage_detail_zh": "「まで」表示「到～為止」，可用於時間或地點。",
            "structure_formula": "時間 / 地點 + まで",
            "example_japanese": "日本語の授業は3時までです。\n駅まで歩きます。",
            "example_hiragana": "にほんごのじゅぎょうはさんじまでです。\nえきまであるきます。",
            "example_zh": "日文課到三點。\n走到車站。",
            "common_mistake_zh": "「まで」只標示終點，不表示開始時間。",
            "learning_tip_zh": "和「から」一起背，會更容易建立範圍感。",
            "priority": 80,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "pattern_kara_made_range",
            "title": "から〜まで",
            "display_name": "から〜まで：從～到～",
            "grammar_type": "particle",
            "usage_summary_zh": "表示從某個起點到某個終點。",
            "usage_detail_zh": "「から〜まで」可用於時間或地點，表示範圍的開始與結束。",
            "structure_formula": "起點 + から + 終點 + まで",
            "example_japanese": "日本語の授業は1時半から3時までです。",
            "example_hiragana": "にほんごのじゅぎょうはいちじはんからさんじまでです。",
            "example_zh": "日文課從一點半到三點。",
            "common_mistake_zh": "時間範圍與地點範圍都能用，但要注意起點和終點的順序。",
            "learning_tip_zh": "先記成「から = 從」「まで = 到」，再合成範圍。",
            "priority": 78,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "particle_yori_comparison",
            "title": "より",
            "display_name": "より：比較基準",
            "grammar_type": "comparison",
            "usage_summary_zh": "表示比較的基準。",
            "usage_detail_zh": "「より」表示「比～」，用來說明 A 相對於 B 的差異。",
            "structure_formula": "A は B より + 形容詞です",
            "example_japanese": "東京は台北より大きいです。",
            "example_hiragana": "とうきょうはたいぺいよりおおきいです。",
            "example_zh": "東京比台北大。",
            "common_mistake_zh": "「より」後面是被比較的基準，不是主角。",
            "learning_tip_zh": "句子的重點通常在「は」前面的 A，B 是比較基準。",
            "priority": 76,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "pattern_desu_masu_polite",
            "title": "です / ます",
            "display_name": "です / ます：丁寧體基本句",
            "grammar_type": "sentence_pattern",
            "usage_summary_zh": "表示禮貌語氣，是日語初學最基本的句型。",
            "usage_detail_zh": "「です」常接在名詞或形容詞後面。「ます」接在動詞ます形後面，使句子聽起來禮貌。",
            "structure_formula": "名詞 / 形容詞 + です\n動詞ます形 + ます",
            "example_japanese": "これは本です。\n毎日学校へ行きます。",
            "example_hiragana": "これはほんです。\nまいにちがっこうへいきます。",
            "example_zh": "這是書。\n每天去學校。",
            "common_mistake_zh": "「です」不能直接接在一般動詞原形後面。",
            "learning_tip_zh": "先把「です」和「ます」當成禮貌句的基本骨架。",
            "priority": 74,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "pattern_masen_negative",
            "title": "ません / ませんでした",
            "display_name": "ません / ませんでした：丁寧體否定",
            "grammar_type": "sentence_pattern",
            "usage_summary_zh": "表示禮貌體的現在否定與過去否定。",
            "usage_detail_zh": "「ません」表示現在或未來不做某事。「ませんでした」表示過去沒有做某事。",
            "structure_formula": "動詞ます形 + ません\n動詞ます形 + ませんでした",
            "example_japanese": "今日は行きません。\n昨日は食べませんでした。",
            "example_hiragana": "きょうはいきません。\nきのうはたべませんでした。",
            "example_zh": "今天不去。\n昨天沒有吃。",
            "common_mistake_zh": "過去否定要用「ませんでした」，不是「ませんです」。",
            "learning_tip_zh": "把「ます → ません → ませんでした」當成禮貌體否定的基本變化。",
            "priority": 72,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "pattern:tai",
            "title": "たい",
            "display_name": "たい：想要做某事",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "想要做某事。",
            "connection": "動詞ます形去ます + たい",
            "usage_summary_zh": "表示說話者自己想做某件事。",
            "usage_detail_zh": "「たい」用來表達自己的願望，前面接動詞ます形去掉ます後的語幹。若要問對方想不想做，也可以用疑問句。",
            "structure_formula": "動詞ます形去ます + たい",
            "example_japanese": "日本へ行きたいです。",
            "example_hiragana": "にほんへいきたいです。",
            "example_zh": "我想去日本。",
            "common_mistake_zh": "描述第三人稱想做某事時，通常不用直接說「彼は行きたいです」，可改用「行きたがっています」。",
            "learning_tip_zh": "把「食べます」變成「食べたい」、「行きます」變成「行きたい」來練習。",
            "note_zh": "否定形是「たくないです」，例如「行きたくないです」。",
            "priority": 70,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "pattern:mashou",
            "title": "ましょう",
            "display_name": "ましょう：邀請 / 提議",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "一起做吧；我來做吧。",
            "connection": "動詞ます形去ます + ましょう",
            "usage_summary_zh": "用來邀請對方一起做某事，或主動提出做某事。",
            "usage_detail_zh": "「ましょう」帶有禮貌的提議語氣，常用在邀請、安排活動或課堂指示中。",
            "structure_formula": "動詞ます形去ます + ましょう",
            "example_japanese": "一緒に勉強しましょう。",
            "example_hiragana": "いっしょにべんきょうしましょう。",
            "example_zh": "一起讀書吧。",
            "common_mistake_zh": "不要把「ましょう」和單純未來式混在一起，它更像邀請或提議。",
            "learning_tip_zh": "聽到「〜ましょうか」時，通常是「要不要我來～？」的語氣。",
            "note_zh": "更口語的邀請可以用「〜しよう」。",
            "priority": 68,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "pattern:te_kudasai",
            "title": "てください",
            "display_name": "てください：請求",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "請對方做某事。",
            "connection": "動詞て形 + ください",
            "usage_summary_zh": "用來禮貌地請對方做某個動作。",
            "usage_detail_zh": "「てください」是很基本的請求句型。語氣比命令形柔和，但仍是要求對方行動。",
            "structure_formula": "動詞て形 + ください",
            "example_japanese": "ここに名前を書いてください。",
            "example_hiragana": "ここになまえをかいてください。",
            "example_zh": "請在這裡寫名字。",
            "common_mistake_zh": "不要把辞書形直接接ください，應先變成て形。",
            "learning_tip_zh": "先熟悉て形，再把常用動詞接上ください練習。",
            "note_zh": "更委婉可用「〜ていただけますか」。",
            "priority": 66,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "pattern:temo_ii",
            "title": "てもいいです",
            "display_name": "てもいいです：可以做某事",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "可以做某事；允許做某事。",
            "connection": "動詞て形 + もいいです",
            "usage_summary_zh": "表示允許或詢問是否可以做某事。",
            "usage_detail_zh": "「てもいいです」常用在確認規則、請求許可，或告訴對方某件事可以做。",
            "structure_formula": "動詞て形 + もいいです",
            "example_japanese": "写真を撮ってもいいですか。",
            "example_hiragana": "しゃしんをとってもいいですか。",
            "example_zh": "可以拍照嗎？",
            "common_mistake_zh": "詢問許可時常加「か」，變成「〜てもいいですか」。",
            "learning_tip_zh": "可以和「てはいけません」一起對照學習。",
            "note_zh": "回答允許時可說「はい、いいです」。",
            "priority": 64,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "pattern:tewa_ikemasen",
            "title": "てはいけません",
            "display_name": "てはいけません：不可以做某事",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "不可以做某事；禁止。",
            "connection": "動詞て形 + はいけません",
            "usage_summary_zh": "表示規則上禁止做某個動作。",
            "usage_detail_zh": "「てはいけません」常用於規定、告示、課堂或正式提醒，語氣比普通否定更像禁止。",
            "structure_formula": "動詞て形 + はいけません",
            "example_japanese": "ここでたばこを吸ってはいけません。",
            "example_hiragana": "ここでたばこをすってはいけません。",
            "example_zh": "不可以在這裡抽菸。",
            "common_mistake_zh": "「ません」只是沒有做或不做，「てはいけません」是禁止。",
            "learning_tip_zh": "看到規則說明時，常會遇到這個句型。",
            "note_zh": "口語中也常聽到「〜ちゃだめです」。",
            "priority": 62,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "pattern:koto_ga_aru",
            "title": "ことがある",
            "display_name": "ことがある：曾經 / 有時",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "曾經有某種經驗；有時會做某事。",
            "connection": "動詞た形 + ことがある / 動詞辞書形 + ことがある",
            "usage_summary_zh": "接た形時表示曾經有過的經驗；接辞書形時可表示有時會發生。",
            "usage_detail_zh": "初學常先學「た形 + ことがあります」，表示人生經驗，例如去過、吃過、看過。",
            "structure_formula": "動詞た形 + ことがある",
            "example_japanese": "日本へ行ったことがあります。",
            "example_hiragana": "にほんへいったことがあります。",
            "example_zh": "我曾經去過日本。",
            "common_mistake_zh": "表示經驗時要用た形，不是辞書形。",
            "learning_tip_zh": "把「食べたことがあります」「見たことがあります」當作固定練習。",
            "note_zh": "否定經驗是「〜たことがありません」。",
            "priority": 60,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "pattern:koto_ga_dekimasu",
            "title": "ことができます",
            "display_name": "ことができます：能夠做某事",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "能夠做某件事。",
            "connection": "動詞辞書形 + ことができます",
            "usage_summary_zh": "表示有能力或條件可以做某件事。",
            "usage_detail_zh": "「ことができます」是很基本的能力表現，比可能形更容易套用，適合初學者先掌握。",
            "structure_formula": "動詞辞書形 + ことができます",
            "example_japanese": "日本語を話すことができます。",
            "example_hiragana": "にほんごをはなすことができます。",
            "example_zh": "我會說日文。",
            "common_mistake_zh": "前面要接辞書形，例如「話すこと」，不是「話しますこと」。",
            "learning_tip_zh": "可和可能形「話せます」一起比較。",
            "note_zh": "否定是「ことができません」。",
            "priority": 58,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "pattern:to_omoimasu",
            "title": "と思います",
            "display_name": "と思います：我認為 / 我想",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "我認為；我想。",
            "connection": "普通形 + と思います",
            "usage_summary_zh": "用來表達自己的想法、推測或意見。",
            "usage_detail_zh": "「と思います」前面通常接普通形句子。常用來讓語氣比直接斷言更柔和。",
            "structure_formula": "普通形 + と思います",
            "example_japanese": "明日は雨が降ると思います。",
            "example_hiragana": "あしたはあめがふるとおもいます。",
            "example_zh": "我覺得明天會下雨。",
            "common_mistake_zh": "前面接句子時要用普通形，不要直接接ます形。",
            "learning_tip_zh": "把「〜です」改成普通形後再接「と思います」。",
            "note_zh": "引用別人的話時可用「と言いました」。",
            "priority": 56,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "pattern:deshou",
            "title": "でしょう",
            "display_name": "でしょう：推量 / 確認",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "大概吧；是吧。",
            "connection": "普通形 + でしょう",
            "usage_summary_zh": "表示推測，也可用來向對方確認。",
            "usage_detail_zh": "「でしょう」比直接斷定更委婉。句尾語調上揚時，常帶有確認對方是否同意的感覺。",
            "structure_formula": "普通形 + でしょう",
            "example_japanese": "明日は晴れるでしょう。",
            "example_hiragana": "あしたははれるでしょう。",
            "example_zh": "明天大概會放晴吧。",
            "common_mistake_zh": "不要和丁寧體的「です」重複接成不自然的形式。",
            "learning_tip_zh": "天氣預報、推測句中很常見。",
            "note_zh": "更口語的推測可用「だろう」。",
            "priority": 54,
        },
        {
            "jlpt_level": "N5",
            "grammar_key": "sentence_end:ne_yo",
            "title": "ね / よ",
            "display_name": "ね / よ：語氣助詞",
            "grammar_type": "expression",
            "meaning_zh": "ね 表示確認或共感；よ 表示提醒或告知。",
            "connection": "句子 + ね / よ",
            "usage_summary_zh": "用在句尾調整語氣，讓句子更自然。",
            "usage_detail_zh": "「ね」常用來尋求認同或共感；「よ」常用來告訴對方新資訊，或提醒對方注意。",
            "structure_formula": "句子 + ね / よ",
            "example_japanese": "今日は寒いですね。これは大切ですよ。",
            "example_hiragana": "きょうはさむいですね。これはたいせつですよ。",
            "example_zh": "今天很冷呢。這個很重要喔。",
            "common_mistake_zh": "「よ」用太多會顯得強勢；「ね」用太多也可能讓語氣過黏。",
            "learning_tip_zh": "先從固定句「そうですね」「いいですよ」開始感受語氣。",
            "note_zh": "句尾助詞沒有固定中文翻譯，要從情境理解。",
            "priority": 52,
        },
    ]
    rows.extend(
        [
            {
                "jlpt_level": "N5",
                "grammar_key": "pattern:amari_nai",
                "title": "あまり〜ない",
                "display_name": "あまり〜ない：不太……",
                "grammar_type": "sentence_pattern",
                "usage_summary_zh": "表示程度不高，常和否定形一起使用。",
                "usage_detail_zh": "「あまり」搭配否定時，表示「不太～」「沒有那麼～」。語氣比完全否定柔和。",
                "structure_formula": "あまり + 動詞否定 / い形容詞否定 / な形容詞ではない",
                "example_japanese": "この映画はあまり面白くないです。",
                "example_hiragana": "このえいがはあまりおもしろくないです。",
                "example_zh": "這部電影不太有趣。",
                "learning_tip_zh": "看到「あまり」時，先確認後面是否是否定形。",
                "priority": 48,
            },
            {
                "jlpt_level": "N5",
                "grammar_key": "pattern:ichiban",
                "title": "いちばん",
                "display_name": "いちばん：最……",
                "grammar_type": "sentence_pattern",
                "usage_summary_zh": "表示在一群事物中程度最高。",
                "usage_detail_zh": "「いちばん」用來表達「最～」，常搭配形容詞或喜歡、擅長等表現。",
                "structure_formula": "名詞 + の中で + 名詞 + が + いちばん + 形容詞",
                "example_japanese": "季節の中で春がいちばん好きです。",
                "example_hiragana": "きせつのなかではるがいちばんすきです。",
                "example_zh": "四季之中我最喜歡春天。",
                "learning_tip_zh": "比較三個以上項目時常用「いちばん」。",
                "priority": 48,
            },
            {
                "jlpt_level": "N5",
                "grammar_key": "pattern:mou",
                "title": "もう",
                "display_name": "もう：已經",
                "grammar_type": "adverb_pattern",
                "usage_summary_zh": "表示某事已經完成或狀態已經改變。",
                "usage_detail_zh": "「もう」常和過去式或完成狀態一起出現，表示「已經～」。",
                "structure_formula": "もう + 動詞ました / です",
                "example_japanese": "宿題はもう終わりました。",
                "example_hiragana": "しゅくだいはもうおわりました。",
                "example_zh": "作業已經寫完了。",
                "learning_tip_zh": "回答「もう〜ましたか」時，可用「はい、もう〜ました」。",
                "priority": 47,
            },
            {
                "jlpt_level": "N5",
                "grammar_key": "pattern:mada",
                "title": "まだ",
                "display_name": "まだ：還、尚未",
                "grammar_type": "adverb_pattern",
                "usage_summary_zh": "表示狀態仍持續，或某事尚未完成。",
                "usage_detail_zh": "「まだ」可表示「還～」，搭配否定時表示「還沒～」。",
                "structure_formula": "まだ + 動詞 / まだ + 動詞ていません",
                "example_japanese": "まだ昼ご飯を食べていません。",
                "example_hiragana": "まだひるごはんをたべていません。",
                "example_zh": "我還沒吃午餐。",
                "learning_tip_zh": "「まだです」可簡短回答「還沒」。",
                "priority": 47,
            },
            {
                "jlpt_level": "N5",
                "grammar_key": "pattern:dake",
                "title": "だけ",
                "display_name": "だけ：只有、僅",
                "grammar_type": "sentence_pattern",
                "usage_summary_zh": "表示限定範圍，相當於「只有～」。",
                "usage_detail_zh": "「だけ」放在名詞、數量或普通形後，用來限制範圍。",
                "structure_formula": "名詞 / 數量詞 / 普通形 + だけ",
                "example_japanese": "今日は水だけ飲みました。",
                "example_hiragana": "きょうはみずだけのみました。",
                "example_zh": "今天只喝了水。",
                "learning_tip_zh": "「だけ」是中性限定，不一定帶負面語氣。",
                "priority": 46,
            },
            {
                "jlpt_level": "N5",
                "grammar_key": "pattern:shika_nai",
                "title": "しか〜ない",
                "display_name": "しか〜ない：只有、僅有",
                "grammar_type": "sentence_pattern",
                "usage_summary_zh": "表示數量或選項很少，常帶有「只有這樣」的感覺。",
                "usage_detail_zh": "「しか」必須和否定形一起使用，形式上是否定，意思上表示限定。",
                "structure_formula": "名詞 / 數量詞 + しか + 否定形",
                "example_japanese": "財布に千円しかありません。",
                "example_hiragana": "さいふにせんえんしかありません。",
                "example_zh": "錢包裡只有一千日圓。",
                "common_mistake_zh": "不要說「しかあります」，しか 後面要接否定。",
                "priority": 46,
            },
            {
                "jlpt_level": "N5",
                "grammar_key": "pattern:naide_kudasai",
                "title": "ないでください",
                "display_name": "ないでください：請不要……",
                "grammar_type": "sentence_pattern",
                "usage_summary_zh": "用來禮貌地請對方不要做某事。",
                "usage_detail_zh": "動詞ない形加「でください」表示禁止或請求對方避免某動作。",
                "structure_formula": "動詞ない形 + でください",
                "example_japanese": "ここで写真を撮らないでください。",
                "example_hiragana": "ここでしゃしんをとらないでください。",
                "example_zh": "請不要在這裡拍照。",
                "learning_tip_zh": "和「てください」相反，一個是請做，一個是請不要做。",
                "priority": 45,
            },
            {
                "jlpt_level": "N5",
                "grammar_key": "pattern:hou_ga_ii",
                "title": "ほうがいい",
                "display_name": "ほうがいい：最好……",
                "grammar_type": "sentence_pattern",
                "usage_summary_zh": "用來給建議，表示某做法比較好。",
                "usage_detail_zh": "常用於提醒或建議對方採取某行動。動詞た形表示「最好做」，ない形表示「最好不要做」。",
                "structure_formula": "動詞た形 / 動詞ない形 + ほうがいい",
                "example_japanese": "早く寝たほうがいいです。",
                "example_hiragana": "はやくねたほうがいいです。",
                "example_zh": "最好早點睡。",
                "learning_tip_zh": "語氣比命令柔和，但仍有建議對方的感覺。",
                "priority": 45,
            },
            {
                "jlpt_level": "N5",
                "grammar_key": "pattern:mae_ni",
                "title": "前に",
                "display_name": "前に：在……之前",
                "grammar_type": "sentence_pattern",
                "usage_summary_zh": "表示某動作或時間點之前。",
                "usage_detail_zh": "接在動詞辞書形或名詞 + の 後，表示「在～之前」。",
                "structure_formula": "動詞辞書形 + 前に / 名詞 + の + 前に",
                "example_japanese": "寝る前に歯を磨きます。",
                "example_hiragana": "ねるまえにはをみがきます。",
                "example_zh": "睡覺前刷牙。",
                "learning_tip_zh": "注意動詞用辞書形，不用過去式。",
                "priority": 44,
            },
            {
                "jlpt_level": "N5",
                "grammar_key": "pattern:ato_de",
                "title": "後で",
                "display_name": "後で：在……之後",
                "grammar_type": "sentence_pattern",
                "usage_summary_zh": "表示某事發生之後再做另一件事。",
                "usage_detail_zh": "接在動詞た形或名詞 + の 後，表示「在～之後」。",
                "structure_formula": "動詞た形 + 後で / 名詞 + の + 後で",
                "example_japanese": "ご飯を食べた後で、勉強します。",
                "example_hiragana": "ごはんをたべたあとで、べんきょうします。",
                "example_zh": "吃完飯之後學習。",
                "learning_tip_zh": "和「前に」不同，動詞要用た形。",
                "priority": 44,
            },
        ]
    )
    usage_items_map = {
        "particle_wa_topic": [
            {
                "usage_title": "表主題",
                "meaning_zh": "提示接下來要說明的主題。",
                "connection": "名詞 + は",
                "example_japanese": "これは本です。",
                "example_hiragana": "これはほんです。",
                "example_zh": "這是書。",
                "note_zh": "は 的重點是把話題提出來，後面才是要說明的內容。",
            },
            {
                "usage_title": "表對比",
                "meaning_zh": "用來對比兩個事物或狀態。",
                "connection": "名詞 + は",
                "example_japanese": "ラーメンは好きですが、すしはまあまあです。",
                "example_hiragana": "らーめんはすきですが、すしはまあまあです。",
                "example_zh": "拉麵我喜歡，但壽司還好。",
                "note_zh": "對比時，は 的語氣比單純描述更明顯。",
            },
        ],
        "particle_mo_also": [
            {
                "usage_title": "表也、都",
                "meaning_zh": "表示前面的項目也符合後面的內容。",
                "connection": "名詞 + も",
                "example_japanese": "この荷物もお願いします。",
                "example_hiragana": "このにもつもおねがいします。",
                "example_zh": "這個行李也麻煩了。",
                "note_zh": "も 會把該項目一起納入同一個狀態。",
            },
            {
                "usage_title": "疑問詞 + も + 否定",
                "meaning_zh": "表示全面否定，例如哪裡都不、什麼都不。",
                "connection": "疑問詞 + も + 否定",
                "example_japanese": "明日はどこも行きません。",
                "example_hiragana": "あしたはどこもいきません。",
                "example_zh": "明天哪裡都不去。",
                "note_zh": "搭配否定時，不要翻成單純的「也」。",
            },
            {
                "usage_title": "數量詞 + も",
                "meaning_zh": "表示次數或數量很多。",
                "connection": "數量詞 + も",
                "example_japanese": "何回もダイエットをしたことがあります。",
                "example_hiragana": "なんかいもだいえっとをしたことがあります。",
                "example_zh": "我減肥過很多次。",
                "note_zh": "這裡的も帶有「多到值得一提」的感覺。",
            },
        ],
        "particle_no_possession": [
            {
                "usage_title": "表所有、屬性、所屬",
                "meaning_zh": "連接兩個名詞，前面的名詞修飾後面的名詞。",
                "connection": "名詞 + の + 名詞",
                "example_japanese": "日本語の本です。",
                "example_hiragana": "にほんごのほんです。",
                "example_zh": "這是日文書。",
                "note_zh": "の 不一定都要硬翻成「的」，要看中文是否自然。",
            },
            {
                "usage_title": "代替前面提到的名詞",
                "meaning_zh": "把已知名詞省略，用の代替。",
                "connection": "修飾語 + の",
                "example_japanese": "もう少し大きいのはありませんか。",
                "example_hiragana": "もうすこしおおきいのはありませんか。",
                "example_zh": "有沒有再大一點的？",
                "note_zh": "大きいの 的 の 代表「東西」或「那個」。",
            },
        ],
        "particle_wo_object": [
            {
                "usage_title": "表動作受詞",
                "meaning_zh": "標示動作直接作用的對象。",
                "connection": "名詞 + を + 動詞",
                "example_japanese": "ジュースを飲みます。",
                "example_hiragana": "じゅーすをのみます。",
                "example_zh": "喝果汁。",
                "note_zh": "中文常省略受詞標記，但日文需要 を。",
            },
            {
                "usage_title": "表移動經過的場所",
                "meaning_zh": "表示在某個空間中移動或經過。",
                "connection": "場所 + を + 移動動詞",
                "example_japanese": "公園を散歩します。",
                "example_hiragana": "こうえんをさんぽします。",
                "example_zh": "在公園散步。",
                "note_zh": "此時を不是受詞，而是移動範圍。",
            },
            {
                "usage_title": "表起點 / 出發點",
                "meaning_zh": "表示離開某處。",
                "connection": "場所 + を + 出る / 離れる",
                "example_japanese": "毎朝8時にうちを出ます。",
                "example_hiragana": "まいあさはちじにうちをでます。",
                "example_zh": "每天早上八點出門。",
                "note_zh": "離開的地方可用 を 標示。",
            },
        ],
        "particle_de_place_method": [
            {
                "usage_title": "表手段、工具、方法",
                "meaning_zh": "表示使用某種工具或方法做事。",
                "connection": "工具 / 手段 + で + 動作",
                "example_japanese": "日本語でレポートを書きます。",
                "example_hiragana": "にほんごでれぽーとをかきます。",
                "example_zh": "用日文寫報告。",
                "note_zh": "で 可以理解為「用～」。",
            },
            {
                "usage_title": "表交通方式",
                "meaning_zh": "表示搭乘或使用的交通工具。",
                "connection": "交通工具 + で + 移動動詞",
                "example_japanese": "タクシーで家へ帰ります。",
                "example_hiragana": "たくしーでいえへかえります。",
                "example_zh": "搭計程車回家。",
                "note_zh": "步行時常說歩いて，不用で。",
            },
            {
                "usage_title": "表動作發生場所",
                "meaning_zh": "表示動作在哪裡進行。",
                "connection": "場所 + で + 動作",
                "example_japanese": "駅で新聞を買います。",
                "example_hiragana": "えきでしんぶんをかいます。",
                "example_zh": "在車站買報紙。",
                "note_zh": "存在場所多用に，動作場所多用で。",
            },
        ],
        "particle_ni_place_time_target": [
            {
                "usage_title": "表存在場所",
                "meaning_zh": "表示某人或某物存在的位置。",
                "connection": "場所 + に + あります / います",
                "example_japanese": "部屋に猫がいます。",
                "example_hiragana": "へやにねこがいます。",
                "example_zh": "房間裡有貓。",
                "note_zh": "描述存在時，位置通常用に。",
            },
            {
                "usage_title": "表時間點",
                "meaning_zh": "表示事情發生的具體時間點。",
                "connection": "時間 + に",
                "example_japanese": "7月に京都でお祭りがあります。",
                "example_hiragana": "しちがつにきょうとでおまつりがあります。",
                "example_zh": "七月在京都有祭典。",
                "note_zh": "明日、今日、毎日這類時間詞通常不加に。",
            },
            {
                "usage_title": "表對象",
                "meaning_zh": "表示動作指向的人或對象。",
                "connection": "對象 + に + 動作",
                "example_japanese": "先生に質問します。",
                "example_hiragana": "せんせいにしつもんします。",
                "example_zh": "向老師提問。",
                "note_zh": "に 可理解為動作投向的方向或對象。",
            },
        ],
        "particle_to_with_quote": [
            {
                "usage_title": "表一起做事的對象",
                "meaning_zh": "表示和誰一起做某件事。",
                "connection": "人 + と + 動作",
                "example_japanese": "私は家族と日本へ来ました。",
                "example_hiragana": "わたしはかぞくとにほんへきました。",
                "example_zh": "我和家人來日本。",
                "note_zh": "和某人一起行動時常用 と。",
            },
            {
                "usage_title": "表完全列舉",
                "meaning_zh": "列出全部項目。",
                "connection": "名詞 + と + 名詞",
                "example_japanese": "本屋は花屋とスーパーの間にあります。",
                "example_hiragana": "ほんやははなやとすーぱーのあいだにあります。",
                "example_zh": "書店在花店和超市之間。",
                "note_zh": "と 比 や 更像完整列舉。",
            },
            {
                "usage_title": "表引用內容",
                "meaning_zh": "接在想法或說話內容後面。",
                "connection": "句子 + と + 思う / 言う",
                "example_japanese": "明日雨が降ると思います。",
                "example_hiragana": "あしたあめがふるとおもいます。",
                "example_zh": "我覺得明天會下雨。",
                "note_zh": "と 前面是被引用的內容。",
            },
        ],
        "particle_kara_start_reason": [
            {
                "usage_title": "表時間 / 地點起點",
                "meaning_zh": "表示從某個時間或地點開始。",
                "connection": "時間 / 地點 + から",
                "example_japanese": "日本語の授業は1時半からです。",
                "example_hiragana": "にほんごのじゅぎょうはいちじはんからです。",
                "example_zh": "日文課從一點半開始。",
                "note_zh": "から 是範圍的起點。",
            },
            {
                "usage_title": "表原因",
                "meaning_zh": "表示原因，常翻成因為。",
                "connection": "句子 + から",
                "example_japanese": "寒いですから、窓を閉めます。",
                "example_hiragana": "さむいですから、まどをしめます。",
                "example_zh": "因為很冷，所以關窗。",
                "note_zh": "から 的原因語氣較直接。",
            },
        ],
    }
    for row in rows:
        enrich_grammar_seed_row(row, now, usage_items_map.get(row["grammar_key"], []))
    return rows


def n3_grammar_seed_rows():
    now = utc_now_iso()
    rows = [
        {
            "jlpt_level": "N3",
            "grammar_key": "n3_seide_seika",
            "title": "せいで・せいか",
            "display_name": "せいで・せいか：因為……",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "因為……，多用於負面原因。",
            "connection": "名詞 + の + せいで\n普通形 + せいで\n普通形 + せいか",
            "usage_summary_zh": "表示某件事是造成不好結果的原因。",
            "usage_detail_zh": "「せいで」通常帶有責怪、遺憾或負面結果的語氣；「せいか」較委婉，表示原因可能是……。",
            "structure_formula": "名詞 + の + せいで\n普通形 + せいで\n普通形 + せいか",
            "example_japanese": "風邪のせいで、学校を休みました。",
            "example_hiragana": "かぜのせいで、がっこうをやすみました。",
            "example_zh": "因為感冒，所以請假沒去上學。",
            "fake_name_example": "かぜのせいで、がっこうをやすみました。",
            "note_zh": "若原因不是負面，通常不使用「せいで」，可改用「おかげで」或「ので」。",
            "common_mistake_zh": "不要把所有「因為」都翻成せいで，正面結果時不適合。",
            "priority": 90,
        },
        {
            "jlpt_level": "N3",
            "grammar_key": "n3_tai_mono_da",
            "title": "たいものだ・てほしいものだ",
            "display_name": "たいものだ・てほしいものだ：真想 / 真希望",
            "grammar_type": "expression",
            "meaning_zh": "真想…… / 真希望……",
            "connection": "動詞たい形 + ものだ\n動詞て形 + ほしい + ものだ",
            "usage_summary_zh": "表示說話者強烈的願望或期待。",
            "usage_detail_zh": "語氣比單純的「たい」更感慨，常用於表達真心願望、感嘆或希望對方理解。",
            "structure_formula": "動詞たい形 + ものだ\n動詞て形 + ほしい + ものだ",
            "example_japanese": "一度日本へ行ってみたいものだ。",
            "example_hiragana": "いちどにほんへいってみたいものだ。",
            "example_zh": "真想去一次日本啊。",
            "fake_name_example": "いちどにほんへいってみたいものだ。",
            "note_zh": "多帶有感慨，不是單純陳述計畫。",
            "common_mistake_zh": "不要把它和普通的「行きたいです」完全等同，ものだ 會增加感嘆語氣。",
            "priority": 88,
        },
        {
            "jlpt_level": "N3",
            "grammar_key": "n3_dake_dewa",
            "title": "だけでは",
            "display_name": "だけでは：光是……不夠",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "光是……的話不夠。",
            "connection": "名詞 + だけでは\n動詞普通形 + だけでは",
            "usage_summary_zh": "表示單靠某件事無法達到期待結果。",
            "usage_detail_zh": "常用於提醒條件不足，後面多接否定或不充分的結果。",
            "structure_formula": "名詞 + だけでは\n動詞普通形 + だけでは",
            "example_japanese": "勉強するだけでは上手になりません。",
            "example_hiragana": "べんきょうするだけではじょうずになりません。",
            "example_zh": "光是念書不會進步。",
            "fake_name_example": "べんきょうするだけではじょうずになりません。",
            "note_zh": "重點是「只有這樣還不夠」，通常需要補充其他條件。",
            "common_mistake_zh": "不要把だけでは只翻成「只有」，要把不足感也翻出來。",
            "priority": 86,
        },
        {
            "jlpt_level": "N3",
            "grammar_key": "n3_dake_de_naku",
            "title": "だけでなく",
            "display_name": "だけでなく：不但……而且……",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "不但……而且……",
            "connection": "名詞 + だけでなく\n普通形 + だけでなく",
            "usage_summary_zh": "表示不只前項，後項也成立。",
            "usage_detail_zh": "常與「も」搭配，形成「不但 A，連 B 也……」的語感。",
            "structure_formula": "名詞 + だけでなく\n普通形 + だけでなく",
            "example_japanese": "彼は日本語だけでなく、中国語も話せます。",
            "example_hiragana": "かれはにほんごだけでなく、ちゅうごくごもはなせます。",
            "example_zh": "他不但會日文，也會中文。",
            "fake_name_example": "かれはにほんごだけでなく、ちゅうごくごもはなせます。",
            "note_zh": "後面常出現も，表示追加的項目也成立。",
            "common_mistake_zh": "不要漏掉後項的も，否則追加語氣會變弱。",
            "priority": 84,
        },
        {
            "jlpt_level": "N3",
            "grammar_key": "n3_koto_ni_naru",
            "title": "ことになる",
            "display_name": "ことになる：決定 / 結果變成",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "決定…… / 結果變成……",
            "connection": "動詞辞書形 + ことになる\n動詞ない形 + ことになる",
            "usage_summary_zh": "表示某事被決定，或事情自然發展成某種結果。",
            "usage_detail_zh": "比「ことにする」更偏向外在決定或自然結果，不強調自己的主觀決定。",
            "structure_formula": "動詞辞書形 + ことになる\n動詞ない形 + ことになる",
            "example_japanese": "来月転勤することになりました。",
            "example_hiragana": "らいげつてんきんすることになりました。",
            "example_zh": "決定下個月要調職。",
            "fake_name_example": "らいげつてんきんすることになりました。",
            "note_zh": "常用於公司、學校、規則等外部因素決定的事情。",
            "common_mistake_zh": "自己的決定通常用「ことにする」，不是「ことになる」。",
            "priority": 82,
        },
        {
            "jlpt_level": "N3",
            "grammar_key": "n3_koto_ni_suru",
            "title": "ことにする",
            "display_name": "ことにする：決定要……",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "決定要……",
            "connection": "動詞辞書形 + ことにする\n動詞ない形 + ことにする",
            "usage_summary_zh": "表示說話者自己決定做或不做某事。",
            "usage_detail_zh": "重點在主觀決定，常用於告訴別人自己已做出選擇。",
            "structure_formula": "動詞辞書形 + ことにする\n動詞ない形 + ことにする",
            "example_japanese": "来年日本へ行くことにしました。",
            "example_hiragana": "らいねんにほんへいくことにしました。",
            "example_zh": "我決定明年去日本。",
            "fake_name_example": "らいねんにほんへいくことにしました。",
            "note_zh": "若是外部決定，請用「ことになる」。",
            "common_mistake_zh": "不要把「ことにする」和「ことになる」混用。",
            "priority": 80,
        },
        {
            "jlpt_level": "N3",
            "grammar_key": "n3_sae_ba",
            "title": "さえ〜ば",
            "display_name": "さえ〜ば：只要……就……",
            "grammar_type": "sentence_pattern",
            "meaning_zh": "只要……就……",
            "connection": "動詞ば形\nい形容詞ければ\nな形容詞なら\n名詞 + さえ + ば",
            "usage_summary_zh": "表示只需要滿足某個最低條件，就能達成結果。",
            "usage_detail_zh": "帶有「其他都不重要，這個條件最關鍵」的語感。",
            "structure_formula": "動詞ば形\nい形容詞ければ\nな形容詞なら\n名詞 + さえ + ば",
            "example_japanese": "お金があれば、幸せになれる。",
            "example_hiragana": "おかねがあれば、しあわせになれる。",
            "example_zh": "只要有錢，就能幸福。",
            "fake_name_example": "おかねがあれば、しあわせになれる。",
            "note_zh": "さえ凸顯最低條件，ば表示條件成立。",
            "common_mistake_zh": "不要只看ば形，要注意さえ強調「只要」。",
            "priority": 78,
        },
        {
            "jlpt_level": "N3",
            "grammar_key": "n3_janai_ka_no",
            "title": "じゃないか・じゃないの",
            "display_name": "じゃないか・じゃないの：不是嗎？",
            "grammar_type": "expression",
            "meaning_zh": "不是嗎？用於確認、指責或驚訝。",
            "connection": "普通形 + じゃないか / じゃないの",
            "usage_summary_zh": "用來確認對方是否同意，也可帶有驚訝或責備。",
            "usage_detail_zh": "語氣會依上下文改變，可以是溫和確認，也可以是較強烈的反問。",
            "structure_formula": "普通形 + じゃないか / じゃないの",
            "example_japanese": "彼は優しいじゃないか。",
            "example_hiragana": "かれはやさしいじゃないか。",
            "example_zh": "他不是很溫柔嗎？",
            "fake_name_example": "かれはやさしいじゃないか。",
            "note_zh": "口語中常用，語氣需看說話情境。",
            "common_mistake_zh": "不要只當成否定句，它常常是反問或確認。",
            "priority": 76,
        },
        {
            "jlpt_level": "N3",
            "grammar_key": "n3_zutsu",
            "title": "ずつ",
            "display_name": "ずつ：每…… / 一點一點地",
            "grammar_type": "expression",
            "meaning_zh": "每…… / 一點一點地……",
            "connection": "數量詞 + ずつ",
            "usage_summary_zh": "表示平均分配，或每次固定一點地進行。",
            "usage_detail_zh": "可用於說明每人、每天、每次的固定數量，也可表漸進累積。",
            "structure_formula": "數量詞 + ずつ",
            "example_japanese": "毎日少しずつ勉強しています。",
            "example_hiragana": "まいにちすこしずつべんきょうしています。",
            "example_zh": "每天一點一點地學習。",
            "fake_name_example": "まいにちすこしずつべんきょうしています。",
            "note_zh": "ずつ前面通常接數量或程度。",
            "common_mistake_zh": "不要把ずつ誤用成單純的「少し」，它有分配或累積感。",
            "priority": 74,
        },
        {
            "jlpt_level": "N3",
            "grammar_key": "n3_kara_to_itte",
            "title": "からといって",
            "display_name": "からといって：雖說……但不代表……",
            "grammar_type": "conjunction",
            "meaning_zh": "雖說……但不代表……",
            "connection": "普通形 + からといって",
            "usage_summary_zh": "表示不能只因為前項，就直接推出後項。",
            "usage_detail_zh": "常用來反駁過度推論，後面常接「とは限らない」「わけではない」。",
            "structure_formula": "普通形 + からといって",
            "example_japanese": "安いからといって、品質が悪いとは限りません。",
            "example_hiragana": "やすいからといって、ひんしつがわるいとはかぎりません。",
            "example_zh": "雖然便宜，但不代表品質一定差。",
            "fake_name_example": "やすいからといって、ひんしつがわるいとはかぎりません。",
            "note_zh": "常和否定判斷搭配，避免武斷推論。",
            "common_mistake_zh": "不要把からといって翻成單純的「因為」。",
            "priority": 72,
        },
    ]
    for row in rows:
        enrich_grammar_seed_row(row, now)
    return rows


def default_grammar_seed_rows():
    return n5_grammar_seed_rows() + n3_grammar_seed_rows()


GRAMMAR_SEED_COLUMNS = (
    "jlpt_level",
    "grammar_key",
    "title",
    "display_name",
    "grammar_type",
    "meaning_zh",
    "connection",
    "usage_summary_zh",
    "usage_detail_zh",
    "structure_formula",
    "example_japanese",
    "example_hiragana",
    "example_zh",
    "common_mistake_zh",
    "learning_tip_zh",
    "note_zh",
    "fake_name_example",
    "usage_items",
    "is_active",
    "priority",
    "used_count",
    "last_used_at",
    "created_at",
    "updated_at",
)


GRAMMAR_SEED_FILL_IF_EMPTY_COLUMNS = (
    "meaning_zh",
    "connection",
    "usage_summary_zh",
    "usage_detail_zh",
    "structure_formula",
    "example_japanese",
    "example_hiragana",
    "example_zh",
    "common_mistake_zh",
    "learning_tip_zh",
    "note_zh",
    "fake_name_example",
    "usage_items",
)


def seed_grammar_points_sqlite(conn):
    rows = default_grammar_seed_rows()
    columns = ", ".join(GRAMMAR_SEED_COLUMNS)
    values = ", ".join([f":{column}" for column in GRAMMAR_SEED_COLUMNS])
    conn.executemany(
        f"""
        INSERT OR IGNORE INTO grammar_points ({columns})
        VALUES ({values})
        """,
        rows,
    )
    fill_sql = ",\n            ".join(
        [
            f"{column} = CASE WHEN COALESCE({column}, '') = '' THEN :{column} ELSE {column} END"
            for column in GRAMMAR_SEED_FILL_IF_EMPTY_COLUMNS
        ]
    )
    conn.executemany(
        f"""
        UPDATE grammar_points
        SET {fill_sql},
            updated_at = CASE WHEN updated_at IS NULL OR updated_at = '' THEN :updated_at ELSE updated_at END
        WHERE grammar_key = :grammar_key
        """,
        rows,
    )


def seed_grammar_points_postgres(cur):
    rows = default_grammar_seed_rows()
    columns = ", ".join(GRAMMAR_SEED_COLUMNS)
    values = ", ".join([f"%({column})s" for column in GRAMMAR_SEED_COLUMNS])
    fill_sql = ",\n                        ".join(
        [
            f"{column} = CASE WHEN COALESCE(grammar_points.{column}, '') = '' THEN EXCLUDED.{column} ELSE grammar_points.{column} END"
            for column in GRAMMAR_SEED_FILL_IF_EMPTY_COLUMNS
        ]
    )
    cur.executemany(
        f"""
        INSERT INTO grammar_points ({columns})
        VALUES ({values})
        ON CONFLICT (grammar_key) DO UPDATE SET
                        {fill_sql},
                        updated_at = CASE
                            WHEN grammar_points.updated_at IS NULL THEN EXCLUDED.updated_at
                            ELSE grammar_points.updated_at
                        END
        """,
        rows,
    )


def migrate_grammar_points_sqlite(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grammar_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            jlpt_level TEXT NOT NULL,
            grammar_key TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            display_name TEXT NOT NULL,
            grammar_type TEXT,
            meaning_zh TEXT DEFAULT '',
            connection TEXT DEFAULT '',
            usage_summary_zh TEXT NOT NULL,
            usage_detail_zh TEXT,
            structure_formula TEXT,
            example_japanese TEXT NOT NULL,
            example_hiragana TEXT,
            example_zh TEXT NOT NULL,
            common_mistake_zh TEXT,
            learning_tip_zh TEXT,
            note_zh TEXT,
            fake_name_example TEXT,
            usage_items TEXT,
            is_active INTEGER DEFAULT 1,
            priority INTEGER DEFAULT 50,
            used_count INTEGER DEFAULT 0,
            last_used_at TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(grammar_points)").fetchall()}
    migrations = {
        "jlpt_level": "ALTER TABLE grammar_points ADD COLUMN jlpt_level TEXT DEFAULT 'N5'",
        "grammar_key": "ALTER TABLE grammar_points ADD COLUMN grammar_key TEXT",
        "title": "ALTER TABLE grammar_points ADD COLUMN title TEXT DEFAULT ''",
        "display_name": "ALTER TABLE grammar_points ADD COLUMN display_name TEXT DEFAULT ''",
        "grammar_type": "ALTER TABLE grammar_points ADD COLUMN grammar_type TEXT",
        "meaning_zh": "ALTER TABLE grammar_points ADD COLUMN meaning_zh TEXT DEFAULT ''",
        "connection": "ALTER TABLE grammar_points ADD COLUMN connection TEXT DEFAULT ''",
        "usage_summary_zh": "ALTER TABLE grammar_points ADD COLUMN usage_summary_zh TEXT DEFAULT ''",
        "usage_detail_zh": "ALTER TABLE grammar_points ADD COLUMN usage_detail_zh TEXT",
        "structure_formula": "ALTER TABLE grammar_points ADD COLUMN structure_formula TEXT",
        "example_japanese": "ALTER TABLE grammar_points ADD COLUMN example_japanese TEXT DEFAULT ''",
        "example_hiragana": "ALTER TABLE grammar_points ADD COLUMN example_hiragana TEXT",
        "example_zh": "ALTER TABLE grammar_points ADD COLUMN example_zh TEXT DEFAULT ''",
        "common_mistake_zh": "ALTER TABLE grammar_points ADD COLUMN common_mistake_zh TEXT",
        "learning_tip_zh": "ALTER TABLE grammar_points ADD COLUMN learning_tip_zh TEXT",
        "note_zh": "ALTER TABLE grammar_points ADD COLUMN note_zh TEXT",
        "fake_name_example": "ALTER TABLE grammar_points ADD COLUMN fake_name_example TEXT",
        "usage_items": "ALTER TABLE grammar_points ADD COLUMN usage_items TEXT",
        "is_active": "ALTER TABLE grammar_points ADD COLUMN is_active INTEGER DEFAULT 1",
        "priority": "ALTER TABLE grammar_points ADD COLUMN priority INTEGER DEFAULT 50",
        "used_count": "ALTER TABLE grammar_points ADD COLUMN used_count INTEGER DEFAULT 0",
        "last_used_at": "ALTER TABLE grammar_points ADD COLUMN last_used_at TEXT",
        "created_at": "ALTER TABLE grammar_points ADD COLUMN created_at TEXT",
        "updated_at": "ALTER TABLE grammar_points ADD COLUMN updated_at TEXT",
    }
    for column, statement in migrations.items():
        if column not in columns:
            conn.execute(statement)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grammar_selection_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material_date TEXT NOT NULL,
            grammar_point_id INTEGER,
            grammar_key TEXT,
            jlpt_level TEXT,
            grammar_type TEXT,
            material_key TEXT,
            material_version_no INTEGER,
            created_at TEXT
        )
        """
    )
    grammar_log_columns = {row[1] for row in conn.execute("PRAGMA table_info(grammar_selection_logs)").fetchall()}
    if "material_key" not in grammar_log_columns:
        conn.execute("ALTER TABLE grammar_selection_logs ADD COLUMN material_key TEXT")
    if "material_version_no" not in grammar_log_columns:
        conn.execute("ALTER TABLE grammar_selection_logs ADD COLUMN material_version_no INTEGER")
    if "version_no" not in grammar_log_columns:
        conn.execute("ALTER TABLE grammar_selection_logs ADD COLUMN version_no INTEGER")
    if "selected_for" not in grammar_log_columns:
        conn.execute("ALTER TABLE grammar_selection_logs ADD COLUMN selected_for TEXT DEFAULT 'grammar'")
    if "title" not in grammar_log_columns:
        conn.execute("ALTER TABLE grammar_selection_logs ADD COLUMN title TEXT")
    if "pattern" not in grammar_log_columns:
        conn.execute("ALTER TABLE grammar_selection_logs ADD COLUMN pattern TEXT")
    if "category" not in grammar_log_columns:
        conn.execute("ALTER TABLE grammar_selection_logs ADD COLUMN category TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_grammar_points_key ON grammar_points(grammar_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_grammar_points_level ON grammar_points(jlpt_level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_grammar_points_active ON grammar_points(is_active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_grammar_selection_logs_date ON grammar_selection_logs(material_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_grammar_selection_logs_key_date ON grammar_selection_logs(grammar_key, material_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_grammar_logs_date_key ON grammar_selection_logs(material_date, grammar_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_grammar_selection_logs_level_date ON grammar_selection_logs(jlpt_level, material_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_grammar_selection_logs_material_key ON grammar_selection_logs(material_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_grammar_logs_selected_date_key ON grammar_selection_logs(selected_for, material_date, grammar_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_grammar_logs_level_key_date ON grammar_selection_logs(jlpt_level, grammar_key, material_date)")
    seed_grammar_points_sqlite(conn)


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
        "question_text": "ALTER TABLE mistake_logs ADD COLUMN question_text TEXT",
        "base_surface": "ALTER TABLE mistake_logs ADD COLUMN base_surface TEXT",
        "base_reading": "ALTER TABLE mistake_logs ADD COLUMN base_reading TEXT",
        "conjugation_type": "ALTER TABLE mistake_logs ADD COLUMN conjugation_type TEXT",
        "primary_answer": "ALTER TABLE mistake_logs ADD COLUMN primary_answer TEXT",
        "accepted_answers_json": "ALTER TABLE mistake_logs ADD COLUMN accepted_answers_json TEXT",
        "explanation": "ALTER TABLE mistake_logs ADD COLUMN explanation TEXT",
        "created_at": "ALTER TABLE mistake_logs ADD COLUMN created_at TEXT",
        "updated_at": "ALTER TABLE mistake_logs ADD COLUMN updated_at TEXT",
        "first_wrong_at": "ALTER TABLE mistake_logs ADD COLUMN first_wrong_at TEXT",
        "last_wrong_at": "ALTER TABLE mistake_logs ADD COLUMN last_wrong_at TEXT",
        "review_due_at": "ALTER TABLE mistake_logs ADD COLUMN review_due_at TEXT",
        "mastered_at": "ALTER TABLE mistake_logs ADD COLUMN mastered_at TEXT",
        "correct_count": "ALTER TABLE mistake_logs ADD COLUMN correct_count INTEGER NOT NULL DEFAULT 0",
        "material_key": "ALTER TABLE mistake_logs ADD COLUMN material_key TEXT",
        "material_date": "ALTER TABLE mistake_logs ADD COLUMN material_date TEXT",
        "question_id": "ALTER TABLE mistake_logs ADD COLUMN question_id TEXT",
        "error_type": "ALTER TABLE mistake_logs ADD COLUMN error_type TEXT",
        "prompt": "ALTER TABLE mistake_logs ADD COLUMN prompt TEXT",
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
    conn.execute(
        """
        UPDATE mistake_logs
        SET review_due_at = COALESCE(NULLIF(review_due_at, ''), NULLIF(next_review_date, ''), NULLIF(last_reviewed_at, ''), ?),
            created_at = COALESCE(NULLIF(created_at, ''), NULLIF(first_wrong_at, ''), NULLIF(last_reviewed_at, ''), ?),
            updated_at = COALESCE(NULLIF(updated_at, ''), NULLIF(last_reviewed_at, ''), ?),
            first_wrong_at = COALESCE(NULLIF(first_wrong_at, ''), NULLIF(created_at, ''), NULLIF(last_reviewed_at, ''), ?),
            last_wrong_at = COALESCE(NULLIF(last_wrong_at, ''), NULLIF(last_reviewed_at, ''), ?),
            correct_count = COALESCE(correct_count, 0),
            mastered_at = CASE
                WHEN (status = 'mastered' OR COALESCE(mastered, 0) = 1) AND COALESCE(NULLIF(mastered_at, ''), '') = '' THEN ?
                ELSE mastered_at
            END,
            status = CASE
                WHEN status = 'mastered' OR COALESCE(mastered, 0) = 1 THEN 'mastered'
                WHEN COALESCE(NULLIF(status, ''), '') = '' THEN 'review_due'
                ELSE status
            END,
            material_date = COALESCE(NULLIF(material_date, ''), substr(COALESCE(NULLIF(created_at, ''), NULLIF(last_reviewed_at, ''), ?), 1, 10)),
            question_id = COALESCE(NULLIF(question_id, ''), CASE WHEN COALESCE(verb_id, 0) > 0 THEN 'verb:' || verb_id || ':' || question_type ELSE question_type END),
            error_type = COALESCE(NULLIF(error_type, ''), CASE WHEN COALESCE(verb_id, 0) > 0 THEN 'verb_conjugation_wrong' ELSE 'unknown' END),
            prompt = COALESCE(NULLIF(prompt, ''), question_text)
        """,
        (now, now, now, now, now, now, now),
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
        if not vocab_pool_db_query_allowed():
            return []
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


def six_main_default_vocab_rule(rule_key):
    base = SIX_MAIN_VOCAB_RULE_DEFAULTS.get(rule_key)
    return dict(base) if base else None


def sanitize_six_main_vocab_rule(rule):
    rule_key = str(rule.get("rule_key") or "").strip()
    base = six_main_default_vocab_rule(rule_key)
    if not base:
        return None
    max_default = int(base.get("max_per_material") or 0)
    max_per_material = clamp_int(rule.get("max_per_material", max_default), max_default, 0)
    base.update(
        {
            "enabled": boolish(rule.get("enabled", base["enabled"])),
            "period": normalize_rule_period(rule.get("period", base["period"])),
            "quota_count": max_per_material,
            "priority": clamp_int(rule.get("priority", base["priority"]), base["priority"], 0, 100),
            "max_per_material": max_per_material,
            "min_per_material": 0,
            "strict_mode": False,
            "is_system_default": True,
        }
    )
    return base


def default_vocab_rule(source_type, match_value, available_count=0, is_system_default=False):
    source_type = source_type if source_type in VOCAB_RULE_SOURCE_TYPES else "category"
    value = clean_rule_match_value(match_value)
    display = rule_display_name(source_type, value)
    group_name = VOCAB_RULE_GROUPS.get(source_type, "單字分類")
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
        rule.update({"enabled": False, "period": "monthly", "quota_count": 0, "priority": 0, "max_per_material": 0, "strict_mode": True})
    elif source_type == "jlpt_level":
        presets = {
            "N5": ("daily", 5, 90, 5, False),
            "N4": ("daily", 5, 85, 5, False),
            "N3": ("daily", 8, 90, 8, False),
            "N2": ("weekly", 5, 60, 1, False),
            "N1": ("monthly", 6, 50, 1, False),
        }
        if value in presets:
            period, quota, priority, max_per_material, strict = presets[value]
            rule.update({"period": period, "quota_count": quota, "priority": priority, "max_per_material": max_per_material, "strict_mode": strict})
    elif source_type == "category":
        if lowered in {"general", "common", "daily"}:
            rule.update({"period": "daily", "quota_count": 5, "priority": 80, "max_per_material": 5})
        elif lowered in {"business", "advanced"}:
            rule.update({"period": "weekly", "quota_count": 2, "priority": 30, "max_per_material": 1, "strict_mode": True})
        elif lowered in {"internet_slang", "otaku_culture", "slang", "approved_slang"}:
            rule.update({"period": "weekly", "quota_count": 1, "priority": 25, "max_per_material": 1, "strict_mode": True})
        elif lowered in {"generated_compound", "unknown", "auto_generated", "synthetic", "typo_or_noise", "sensitive", "named_entity"}:
            rule.update({"enabled": False, "period": "monthly", "quota_count": 0, "priority": 0, "max_per_material": 0, "strict_mode": True})
        else:
            rule.update({"period": "weekly", "quota_count": 1, "priority": 20, "max_per_material": 1, "strict_mode": True})
    return rule


def default_vocab_rule_seed():
    return [six_main_default_vocab_rule(rule_key) for rule_key in SIX_MAIN_VOCAB_RULE_ORDER]

def sanitize_vocab_rule_payload(rule):
    six_rule = sanitize_six_main_vocab_rule(rule)
    if six_rule:
        return six_rule
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
        if source_type not in VOCAB_RULE_VISIBLE_TYPES:
            return
        value = clean_rule_match_value(value)
        if local_generation_safe_mode_enabled():
            if source_type == "jlpt_level" and value not in LOCAL_SAFE_MODE_JLPT_LEVELS:
                return
            if source_type == "category" and value not in LOCAL_SAFE_MODE_CATEGORIES:
                return
        key = make_vocab_rule_key(source_type, value)
        if key not in discovered:
            discovered[key] = default_vocab_rule(source_type, value, available_count=0)
        discovered[key]["available_count"] = discovered[key].get("available_count", 0) + int(count or 0)

    ensure_vocabulary_pool_store()
    for source_type in ("jlpt_level", "category"):
        try:
            for value, count in query_distinct_counts("vocabulary_pool", source_type):
                add(source_type, value, count)
        except Exception as exc:
            print(f"[vocab-rules] scan skipped table=vocabulary_pool field={source_type}; reason={exc}")
    if not local_generation_safe_mode_enabled():
        ensure_slang_candidates_store()
        try:
            for value, count in query_distinct_counts("slang_candidates", "category"):
                add("category", value, count)
        except Exception as exc:
            print(f"[vocab-rules] scan skipped table=slang_candidates field=category; reason={exc}")
    return discovered

def period_bounds(period, material_date=None):
    if material_date:
        try:
            today = datetime.strptime(canonical_material_date(material_date), "%Y-%m-%d").date()
        except Exception:
            today = taipei_now().date()
    else:
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


def vocab_rule_used_count(rule_key, period, material_date=None):
    start, end = period_bounds(period, material_date)
    try:
        if DATABASE_URL and not vocab_pool_db_query_allowed():
            return 0
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
    except Exception as exc:
        print(f"[local-generate] vocab rule period check failed rule_key={rule_key}; reason={exc}")
        mark_vocab_pool_db_unavailable(exc)
        return 0


def is_rule_period_available(rule_key, period, material_date=None):
    period = normalize_rule_period(period)
    if period == "daily":
        return True
    return vocab_rule_used_count(rule_key, period, material_date) <= 0


def load_six_main_vocab_rules():
    try:
        rows = {row["rule_key"]: row for row in load_vocab_rule_rows()}
    except Exception as exc:
        print(f"[local-generate] vocab rules unavailable; using six defaults; reason={exc}")
        mark_vocab_pool_db_unavailable(exc)
        rows = {}
    merged = []
    for rule_key in SIX_MAIN_VOCAB_RULE_ORDER:
        rule = six_main_default_vocab_rule(rule_key)
        stored = rows.get(rule_key) or rows.get(LEGACY_MAIN_VOCAB_RULE_KEYS.get(rule_key, ""))
        if stored:
            rule.update(
                {
                    "enabled": boolish(stored.get("enabled", rule["enabled"])),
                    "period": normalize_rule_period(stored.get("period", rule["period"])),
                    "max_per_material": clamp_int(stored.get("max_per_material", rule["max_per_material"]), rule["max_per_material"], 0),
                    "priority": clamp_int(stored.get("priority", rule["priority"]), rule["priority"], 0, 100),
                }
            )
            rule["quota_count"] = int(rule.get("max_per_material") or 0)
        merged.append(rule)
    return merged


def build_vocab_rules_payload(create_missing=False, material_date=None):
    rules = load_six_main_vocab_rules()
    if create_missing:
        existing = {row["rule_key"] for row in load_vocab_rule_rows()}
        missing = [rule for rule in rules if rule["rule_key"] not in existing]
        if missing:
            insert_or_update_vocab_rules(missing)
            rules = load_six_main_vocab_rules()
    public_rules = [
        {
            "rule_key": rule["rule_key"],
            "display_name": rule["display_name"],
            "source_type": rule["source_type"],
            "match_value": rule["match_value"],
            "enabled": boolish(rule.get("enabled")),
            "period": normalize_rule_period(rule.get("period")),
            "max_per_material": int(rule.get("max_per_material") or 0),
            "priority": int(rule.get("priority") or 0),
        }
        for rule in rules
    ]
    return {
        "groups": [
            {
                "group_key": "main_vocab_rules",
                "group_name": "單字出現設定",
                "rules": public_rules,
            }
        ]
    }


def load_vocab_rule_context(material_date=None):
    try:
        payload = build_vocab_rules_payload(create_missing=False, material_date=material_date)
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
    if DATABASE_URL:
        postgres_settings = load_postgres_settings()
        if postgres_settings:
            return normalize_settings(postgres_settings)
        return normalize_settings({})

    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return normalize_settings(dict(rows))


def load_postgres_settings():
    if not DATABASE_URL:
        return {}
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT key, value FROM {POSTGRES_SETTINGS_TABLE}")
                rows = cur.fetchall()
        if rows:
            print(f"[settings-store] loaded postgres_settings count={len(rows)}")
        return dict(rows)
    except Exception as exc:
        print(f"[settings-store] postgres_settings_unavailable fallback=sqlite reason={exc}")
        return {}


def ensure_postgres_settings_schema(cur):
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {POSTGRES_SETTINGS_TABLE} (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def save_postgres_settings(settings):
    if not DATABASE_URL:
        return False
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                ensure_postgres_settings_schema(cur)
                cur.executemany(
                    f"""
                    INSERT INTO {POSTGRES_SETTINGS_TABLE} (key, value)
                    VALUES (%s, %s)
                    ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    [(key, str(value)) for key, value in settings.items()],
                )
            conn.commit()
        print(f"[settings-store] saved postgres_settings count={len(settings)}")
        return True
    except Exception as exc:
        print(f"[settings-store] postgres_settings_save_failed fallback=sqlite reason={exc}")
        return False


def save_settings_file(settings):
    sanitized = request_settings_overrides(settings or {})
    current = normalize_settings(load_settings() | sanitized)
    print(
        "[settings-save] received "
        f"target_levels={','.join(settings_target_levels(sanitized)) if sanitized else ''} "
        f"word_count={sanitized.get('vocab_count', '')} "
        f"verb_count={sanitized.get('verb_count', '')} "
        f"choice_count={sanitized.get('mcq_count', '')} "
        f"fill_count={sanitized.get('fill_count', '')}"
    )
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
    postgres_saved = save_postgres_settings(current)
    if DATABASE_URL and not postgres_saved:
        raise RuntimeError("postgres_settings_save_failed")
    invalidate_dashboard_cache("settings saved")
    print(
        "[settings-save] persisted "
        f"target_levels={','.join(settings_target_levels(current))} "
        f"word_count={current.get('vocab_count')} "
        f"verb_count={current.get('verb_count')} "
        f"choice_count={current.get('mcq_count')} "
        f"fill_count={current.get('fill_count')}"
    )
    return current


REMINDER_DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
REMINDER_DEFAULT_MESSAGE = "せにせに，今天也來學一點日文吧。\n不用一次很多，持續才是最強的。"


def reminder_time_options():
    return [f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in (0, 30)]


REMINDER_TIME_OPTIONS = set(reminder_time_options())


def raw_app_settings():
    ensure_settings_store()
    if DATABASE_URL:
        return load_postgres_settings()
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return dict(rows)


def save_app_setting_values(values):
    ensure_settings_store()
    payload = {key: str(value) for key, value in (values or {}).items()}
    if not payload:
        return
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        conn.executemany(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            list(payload.items()),
        )
        conn.commit()
    if DATABASE_URL and not save_postgres_settings(payload):
        raise RuntimeError("postgres_settings_save_failed")


def normalize_reminder_days(value):
    if isinstance(value, str):
        raw = [item.strip().upper() for item in re.split(r"[,\s]+", value) if item.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw = [str(item or "").strip().upper() for item in value]
    else:
        raw = []
    days = [day for day in REMINDER_DAYS if day in raw]
    return days or REMINDER_DAYS[:]


def normalize_reminder_times(value):
    if isinstance(value, str):
        raw = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw = [str(item or "").strip() for item in value]
    else:
        raw = []
    seen = []
    for item in raw:
        if item not in REMINDER_TIME_OPTIONS:
            raise ValueError(f"invalid_reminder_time:{item}")
        if item not in seen:
            seen.append(item)
    return seen or ["09:00", "21:00"]


def normalize_reminder_settings(raw):
    raw = raw or {}
    enabled_value = raw.get("enabled", raw.get("telegram_reminder_enabled", "false"))
    message = str(raw.get("message", raw.get("telegram_reminder_message", REMINDER_DEFAULT_MESSAGE)) or "").strip()
    if len(message) > 500:
        raise ValueError("reminder_message_too_long")
    return {
        "enabled": boolish(enabled_value),
        "times": normalize_reminder_times(raw.get("times", raw.get("telegram_reminder_times", "09:00,21:00"))),
        "timezone": str(raw.get("timezone", raw.get("telegram_reminder_timezone", "Asia/Taipei")) or "Asia/Taipei").strip() or "Asia/Taipei",
        "days": normalize_reminder_days(raw.get("days", raw.get("telegram_reminder_days", ",".join(REMINDER_DAYS)))),
        "message": message or REMINDER_DEFAULT_MESSAGE,
        "last_sent_key": str(raw.get("last_sent_key", raw.get("telegram_reminder_last_sent_key", "")) or ""),
        "last_sent_at": str(raw.get("last_sent_at", raw.get("telegram_reminder_last_sent_at", "")) or ""),
    }


def load_telegram_reminder_settings():
    raw = raw_app_settings()
    return normalize_reminder_settings(raw)


def save_telegram_reminder_settings(settings):
    normalized = normalize_reminder_settings(settings)
    save_app_setting_values(
        {
            "telegram_reminder_enabled": "true" if normalized["enabled"] else "false",
            "telegram_reminder_times": ",".join(normalized["times"]),
            "telegram_reminder_timezone": normalized["timezone"],
            "telegram_reminder_days": ",".join(normalized["days"]),
            "telegram_reminder_message": normalized["message"],
        }
    )
    return load_telegram_reminder_settings()


def reminder_day_code(dt):
    return REMINDER_DAYS[dt.weekday()]


def reminder_slot_time(dt):
    minute = 30 if dt.minute >= 30 else 0
    return f"{dt.hour:02d}:{minute:02d}"


def send_telegram_reminder_message(message):
    return send_telegram_message(html.escape(message).replace("\n", "\n"), timeout_seconds=5)


def migrations_allowed_now():
    if has_request_context() and request.path.startswith("/api/") and not RUN_MIGRATIONS_ON_REQUEST:
        return False
    return True


def get_db_connection():
    import psycopg

    return psycopg.connect(DATABASE_URL, connect_timeout=DB_CONNECT_TIMEOUT_SECONDS)


GEMINI_JOB_COLUMNS = [
    "job_id",
    "material_date",
    "status",
    "current_stage",
    "settings_json",
    "vocab_json",
    "verbs_json",
    "grammar_json",
    "planned_steps_json",
    "completed_steps_json",
    "error_message",
    "created_at",
    "updated_at",
]


def ensure_gemini_generation_jobs_store():
    global _GEMINI_JOBS_SCHEMA_READY
    if _GEMINI_JOBS_SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _GEMINI_JOBS_SCHEMA_READY:
            return
        if DATABASE_URL:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS gemini_generation_jobs (
                            job_id TEXT PRIMARY KEY,
                            material_date DATE NOT NULL,
                            status TEXT NOT NULL DEFAULT 'running',
                            current_stage TEXT NOT NULL DEFAULT 'created',
                            settings_json TEXT NOT NULL DEFAULT '{}',
                            vocab_json TEXT NOT NULL DEFAULT '[]',
                            verbs_json TEXT NOT NULL DEFAULT '[]',
                            grammar_json TEXT NOT NULL DEFAULT '{}',
                            planned_steps_json TEXT NOT NULL DEFAULT '[]',
                            completed_steps_json TEXT NOT NULL DEFAULT '[]',
                            error_message TEXT DEFAULT '',
                            created_at TIMESTAMPTZ NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL
                        )
                        """
                    )
                    cur.execute("ALTER TABLE gemini_generation_jobs ADD COLUMN IF NOT EXISTS planned_steps_json TEXT NOT NULL DEFAULT '[]'")
                    cur.execute("ALTER TABLE gemini_generation_jobs ADD COLUMN IF NOT EXISTS completed_steps_json TEXT NOT NULL DEFAULT '[]'")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_gemini_generation_jobs_status ON gemini_generation_jobs(status)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_gemini_generation_jobs_date ON gemini_generation_jobs(material_date)")
                conn.commit()
        else:
            prepare_sqlite_path()
            with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS gemini_generation_jobs (
                        job_id TEXT PRIMARY KEY,
                        material_date TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'running',
                        current_stage TEXT NOT NULL DEFAULT 'created',
                        settings_json TEXT NOT NULL DEFAULT '{}',
                        vocab_json TEXT NOT NULL DEFAULT '[]',
                        verbs_json TEXT NOT NULL DEFAULT '[]',
                        grammar_json TEXT NOT NULL DEFAULT '{}',
                        planned_steps_json TEXT NOT NULL DEFAULT '[]',
                        completed_steps_json TEXT NOT NULL DEFAULT '[]',
                        error_message TEXT DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                columns = {row[1] for row in conn.execute("PRAGMA table_info(gemini_generation_jobs)").fetchall()}
                if "planned_steps_json" not in columns:
                    conn.execute("ALTER TABLE gemini_generation_jobs ADD COLUMN planned_steps_json TEXT NOT NULL DEFAULT '[]'")
                if "completed_steps_json" not in columns:
                    conn.execute("ALTER TABLE gemini_generation_jobs ADD COLUMN completed_steps_json TEXT NOT NULL DEFAULT '[]'")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_gemini_generation_jobs_status ON gemini_generation_jobs(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_gemini_generation_jobs_date ON gemini_generation_jobs(material_date)")
                conn.commit()
        _GEMINI_JOBS_SCHEMA_READY = True


def gemini_job_json(value, fallback):
    try:
        parsed = json.loads(value or "")
        return parsed if parsed not in (None, "") else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def create_gemini_generation_job(settings, material_date=None, planned_steps=None):
    ensure_gemini_generation_jobs_store()
    job_id = uuid.uuid4().hex
    date_iso = canonical_material_date(material_date or get_today_taipei_date())
    now = utc_now_iso()
    payload = {
        "job_id": job_id,
        "material_date": date_iso,
        "status": "running",
        "current_stage": "created",
        "settings_json": json.dumps(settings, ensure_ascii=False),
        "vocab_json": "[]",
        "verbs_json": "[]",
        "grammar_json": "{}",
        "planned_steps_json": json.dumps(planned_steps or [], ensure_ascii=False),
        "completed_steps_json": "[]",
        "error_message": "",
        "created_at": now,
        "updated_at": now,
    }
    if DATABASE_URL:
        placeholders = ", ".join(["%s"] * len(GEMINI_JOB_COLUMNS))
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO gemini_generation_jobs ({', '.join(GEMINI_JOB_COLUMNS)}) VALUES ({placeholders})",
                    tuple(payload[column] for column in GEMINI_JOB_COLUMNS),
                )
            conn.commit()
    else:
        with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
            conn.execute(
                f"INSERT INTO gemini_generation_jobs ({', '.join(GEMINI_JOB_COLUMNS)}) VALUES ({', '.join(':' + column for column in GEMINI_JOB_COLUMNS)})",
                payload,
            )
            conn.commit()
    print(f"[gemini-job] created job_id={job_id}")
    return payload


def load_gemini_generation_job(job_id):
    ensure_gemini_generation_jobs_store()
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {', '.join(GEMINI_JOB_COLUMNS)} FROM gemini_generation_jobs WHERE job_id = %s",
                    (job_id,),
                )
                row = cur.fetchone()
        return dict(zip(GEMINI_JOB_COLUMNS, row)) if row else None
    rows = sqlite_dicts(
        f"SELECT {', '.join(GEMINI_JOB_COLUMNS)} FROM gemini_generation_jobs WHERE job_id = ?",
        (job_id,),
    )
    return rows[0] if rows else None


def update_gemini_generation_job(job_id, **fields):
    if not fields:
        return
    ensure_gemini_generation_jobs_store()
    fields["updated_at"] = utc_now_iso()
    allowed = {column for column in GEMINI_JOB_COLUMNS if column != "job_id"}
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    if DATABASE_URL:
        assignments = ", ".join(f"{key} = %s" for key in updates)
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE gemini_generation_jobs SET {assignments} WHERE job_id = %s",
                    (*updates.values(), job_id),
                )
            conn.commit()
    else:
        assignments = ", ".join(f"{key} = :{key}" for key in updates)
        payload = {**updates, "job_id": job_id}
        with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
            conn.execute(f"UPDATE gemini_generation_jobs SET {assignments} WHERE job_id = :job_id", payload)
            conn.commit()


def gemini_refill_step_key(item_type, level):
    return f"{str(item_type or '').strip().lower()}:{normalize_gemini_bank_level(level, str(item_type or '').strip().lower())}"


def gemini_step_key_from_payload(step):
    if not isinstance(step, dict):
        return ""
    if str(step.get("stage") or "").strip().lower() != "refill":
        return ""
    item_type = str(step.get("item_type") or "").strip().lower()
    level = str(step.get("jlpt_level") or step.get("level") or "").strip().upper()
    if item_type not in {"word", "verb", "grammar"}:
        return ""
    return gemini_refill_step_key(item_type, level)


def gemini_planned_refill_step_keys(job):
    steps = gemini_job_json(job.get("planned_steps_json"), [])
    keys = [gemini_step_key_from_payload(step) for step in steps if isinstance(step, dict)]
    return [key for key in keys if key]


def gemini_completed_step_keys(job):
    completed = gemini_job_json(job.get("completed_steps_json"), [])
    return [str(item) for item in completed if item]


def gemini_bank_not_ready_pools(job):
    if GEMINI_BANK_ALLOW_RECENT_REUSE:
        return []
    steps = gemini_job_json(job.get("planned_steps_json"), [])
    not_ready = []
    for step in steps:
        if not isinstance(step, dict) or step.get("stage") != "refill":
            continue
        item_type = str(step.get("item_type") or "").strip().lower()
        level = normalize_gemini_bank_level(step.get("jlpt_level") or step.get("level"), item_type)
        if item_type not in {"word", "verb"}:
            continue
        target_stock = int(step.get("target_stock") or gemini_bank_target_stock(item_type, level))
        fresh_stock = gemini_bank_count_fresh_stock(item_type, level)
        active_total = gemini_bank_count_active_total(item_type, level)
        if fresh_stock < target_stock:
            not_ready.append(
                {
                    "item_type": item_type,
                    "jlpt_level": level,
                    "fresh_stock": fresh_stock,
                    "target_fresh_stock": target_stock,
                    "active_total": active_total,
                }
            )
    return not_ready


def mark_gemini_generation_step_completed(job_id, item_type, level):
    job = load_gemini_generation_job(job_id)
    if not job:
        return []
    key = gemini_refill_step_key(item_type, level)
    completed = gemini_completed_step_keys(job)
    if key not in completed:
        completed.append(key)
        update_gemini_generation_job(job_id, completed_steps_json=json.dumps(completed, ensure_ascii=False))
    return completed


GEMINI_BANK_COLUMNS = [
    "item_type",
    "normalized_key",
    "display_text",
    "reading",
    "jlpt_level",
    "category",
    "source",
    "status",
    "payload_json",
    "used_count",
    "first_used_at",
    "last_used_at",
    "created_at",
    "updated_at",
]


GEMINI_BANK_MIN_UNUSED = {
    "word": GEMINI_BANK_MIN_WORD_PER_LEVEL,
    "verb": GEMINI_BANK_MIN_VERB_PER_LEVEL,
    "grammar": GEMINI_BANK_MIN_GRAMMAR_PER_LEVEL,
    "SNS": 20,
}


def ensure_gemini_item_bank_store():
    global _GEMINI_BANK_SCHEMA_READY
    if _GEMINI_BANK_SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _GEMINI_BANK_SCHEMA_READY:
            return
        if DATABASE_URL:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS gemini_item_bank (
                            id BIGSERIAL PRIMARY KEY,
                            item_type TEXT NOT NULL,
                            normalized_key TEXT NOT NULL,
                            display_text TEXT NOT NULL DEFAULT '',
                            reading TEXT NOT NULL DEFAULT '',
                            jlpt_level TEXT NOT NULL DEFAULT '',
                            category TEXT NOT NULL DEFAULT 'general',
                            source TEXT NOT NULL DEFAULT 'gemini',
                            status TEXT NOT NULL DEFAULT 'unused',
                            payload_json TEXT NOT NULL DEFAULT '{}',
                            used_count INTEGER NOT NULL DEFAULT 0,
                            first_used_at TIMESTAMPTZ,
                            last_used_at TIMESTAMPTZ,
                            created_at TIMESTAMPTZ NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL,
                            UNIQUE(item_type, normalized_key)
                        )
                        """
                    )
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_gemini_item_bank_type_status_level ON gemini_item_bank(item_type, status, jlpt_level)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_gemini_item_bank_usage ON gemini_item_bank(item_type, used_count, created_at)")
                    cur.execute("UPDATE gemini_item_bank SET status = 'unused', updated_at = %s WHERE status = 'used'", (utc_now_iso(),))
                conn.commit()
        else:
            prepare_sqlite_path()
            with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS gemini_item_bank (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        item_type TEXT NOT NULL,
                        normalized_key TEXT NOT NULL,
                        display_text TEXT NOT NULL DEFAULT '',
                        reading TEXT NOT NULL DEFAULT '',
                        jlpt_level TEXT NOT NULL DEFAULT '',
                        category TEXT NOT NULL DEFAULT 'general',
                        source TEXT NOT NULL DEFAULT 'gemini',
                        status TEXT NOT NULL DEFAULT 'unused',
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        used_count INTEGER NOT NULL DEFAULT 0,
                        first_used_at TEXT,
                        last_used_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(item_type, normalized_key)
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_gemini_item_bank_type_status_level ON gemini_item_bank(item_type, status, jlpt_level)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_gemini_item_bank_usage ON gemini_item_bank(item_type, used_count, created_at)")
                conn.execute("UPDATE gemini_item_bank SET status = 'unused', updated_at = ? WHERE status = 'used'", (utc_now_iso(),))
                conn.commit()
        _GEMINI_BANK_SCHEMA_READY = True


def gemini_bank_item_from_payload(item_type, payload, fallback_level):
    if item_type == "word":
        normalized = normalize_gemini_vocab_item(payload, fallback_level)
        display = normalized.get("word", "")
        reading = normalized.get("reading", "")
        key = normalized.get("normalized_key") or normalize_vocab_key(display)
        level = normalized.get("jlpt_level") or fallback_level
        category = normalized.get("category") or "general"
    elif item_type == "verb":
        normalized = normalize_gemini_verb_item(payload, fallback_level)
        display = normalized.get("surface") or normalized.get("dictionary_form") or ""
        reading = normalized.get("reading_hiragana", "")
        key = normalized.get("normalized_key") or normalize_vocab_key(display)
        level = normalized.get("jlpt_level") or fallback_level
        category = "general"
    elif item_type == "grammar":
        normalized = normalize_gemini_grammar_point(payload, fallback_level)
        display = normalized.get("display_name") or normalized.get("title") or normalized.get("grammar_key") or ""
        reading = normalized.get("example_hiragana", "")
        key = normalized.get("grammar_key") or normalize_vocab_key(display)
        level = normalized.get("jlpt_level") or fallback_level
        category = normalized.get("category") or normalized.get("grammar_type") or "grammar"
    else:
        raise ValueError(f"unsupported_bank_item_type:{item_type}")
    if not display or not key or not level:
        raise ValueError("invalid_bank_item")
    normalized["source"] = "gemini"
    return {
        "item_type": item_type,
        "normalized_key": key,
        "display_text": display,
        "reading": reading,
        "jlpt_level": normalize_gemini_level(level, fallback_level),
        "category": category,
        "source": "gemini",
        "status": "unused",
        "payload_json": json.dumps(normalized, ensure_ascii=False),
        "used_count": 0,
    }


def normalize_gemini_bank_level(value, item_type="word", fallback="N5"):
    level = simple_text(value).upper()
    if item_type == "word" and level == "SNS":
        return "SNS"
    return level if level in LEVELS else fallback


def gemini_bank_count_unused(item_type, level):
    ensure_gemini_item_bank_store()
    level = normalize_gemini_bank_level(level, item_type)
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM gemini_item_bank
                    WHERE item_type = %s AND status = 'unused' AND jlpt_level = %s
                    """,
                    (item_type, level),
                )
                return int(cur.fetchone()[0] or 0)
    row = sqlite_one(
        """
        SELECT COUNT(*) AS count
        FROM gemini_item_bank
        WHERE item_type = ? AND status = 'unused' AND jlpt_level = ?
        """,
        (item_type, level),
    )
    return int(row["count"] or 0)


GEMINI_BANK_ACTIVE_STATUSES = ("unused", "available", "active")


def gemini_bank_count_active_total(item_type, level):
    ensure_gemini_item_bank_store()
    level = normalize_gemini_bank_level(level, item_type)
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM gemini_item_bank
                    WHERE item_type = %s AND jlpt_level = %s AND status = ANY(%s)
                    """,
                    (item_type, level, list(GEMINI_BANK_ACTIVE_STATUSES)),
                )
                return int(cur.fetchone()[0] or 0)
    row = sqlite_one(
        f"""
        SELECT COUNT(*) AS count
        FROM gemini_item_bank
        WHERE item_type = ? AND jlpt_level = ? AND status IN ({",".join(["?"] * len(GEMINI_BANK_ACTIVE_STATUSES))})
        """,
        (item_type, level, *GEMINI_BANK_ACTIVE_STATUSES),
    )
    return int(row["count"] or 0)


def gemini_bank_count_fresh_stock(item_type, level):
    ensure_gemini_item_bank_store()
    level = normalize_gemini_bank_level(level, item_type)
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM gemini_item_bank
                    WHERE item_type = %s
                      AND jlpt_level = %s
                      AND status = ANY(%s)
                      AND (COALESCE(used_count, 0) = 0 OR last_used_at IS NULL)
                    """,
                    (item_type, level, list(GEMINI_BANK_ACTIVE_STATUSES)),
                )
                return int(cur.fetchone()[0] or 0)
    row = sqlite_one(
        f"""
        SELECT COUNT(*) AS count
        FROM gemini_item_bank
        WHERE item_type = ?
          AND jlpt_level = ?
          AND status IN ({",".join(["?"] * len(GEMINI_BANK_ACTIVE_STATUSES))})
          AND (COALESCE(used_count, 0) = 0 OR last_used_at IS NULL)
        """,
        (item_type, level, *GEMINI_BANK_ACTIVE_STATUSES),
    )
    return int(row["count"] or 0)


def gemini_bank_existing_keys(item_type, level, limit=120):
    ensure_gemini_item_bank_store()
    level = normalize_gemini_bank_level(level, item_type)
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT normalized_key
                    FROM gemini_item_bank
                    WHERE item_type = %s AND jlpt_level = %s AND normalized_key IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (item_type, level, int(limit)),
                )
                return [str(row[0]) for row in cur.fetchall() if row and row[0]]
    with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
        rows = conn.execute(
            """
            SELECT normalized_key
            FROM gemini_item_bank
            WHERE item_type = ? AND jlpt_level = ? AND normalized_key IS NOT NULL
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (item_type, level, int(limit)),
        ).fetchall()
    return [str(row[0]) for row in rows if row and row[0]]


GEMINI_BANK_REFILL_STRATEGIES = {
    "word": [
        ("standard_jlpt", "Focus on standard JLPT vocabulary that appears in everyday learning materials."),
        ("daily_life", "Focus on daily-life vocabulary for home, shopping, health, relationships, and routines."),
        ("school_work_transport_food_emotion_action", "Focus on school, work, transport, food, emotions, and common actions."),
        ("concrete_abstract_mix", "Mix concrete nouns, useful adjectives, adverbs, and abstract but learnable terms."),
        ("semantic_distance", "Choose items that are semantically distant from common seed words and previous outputs."),
    ],
    "verb": [
        ("core_verbs", "Focus on core standalone Japanese verbs. Do not generate noun + suru compounds."),
        ("movement_communication_daily_actions", "Focus on movement, communication, perception, eating, buying, and daily actions."),
        ("emotion_cognition_interaction", "Focus on emotion, cognition, social interaction, and classroom-friendly verbs."),
        ("transitive_intransitive_mix", "Mix transitive and intransitive verbs. Avoid repeating the same verb family."),
        ("no_suru_compounds", "Generate only real dictionary-form verbs, not business nouns or abstract nouns plus suru."),
    ],
    "grammar": [
        ("core_patterns", "Focus on core JLPT grammar patterns with clear learner-facing explanations."),
        ("contrast_condition_sequence", "Focus on contrast, condition, sequence, reason, and purpose patterns."),
        ("spoken_written_balance", "Mix spoken and written grammar where appropriate for the JLPT level."),
        ("contextual_examples", "Choose patterns that can be explained through short natural examples."),
        ("nuance_variation", "Prefer grammar points with distinct nuance from common beginner particles."),
    ],
}


def gemini_bank_refill_strategy(item_type, attempt_count=0, duplicate_streak=0):
    strategies = GEMINI_BANK_REFILL_STRATEGIES.get(item_type) or [("default", "Generate diverse, useful learning items.")]
    attempt_count = max(0, int(attempt_count or 0))
    duplicate_streak = max(0, int(duplicate_streak or 0))
    strategy_span = max(1, int(GEMINI_BANK_MAX_REFILL_ATTEMPTS_PER_POOL or 1))
    attempt_turn = attempt_count // strategy_span
    duplicate_turn = 2 if duplicate_streak >= 6 else 1 if duplicate_streak >= 3 else 0
    strategy_index = max(attempt_turn, duplicate_turn) % len(strategies)
    strategy_name, instruction = strategies[strategy_index]
    strategy_rotated = (
        (attempt_count > 0 and attempt_count % strategy_span == 0)
        or duplicate_streak in {3, 6}
    )
    return {
        "strategy": strategy_name,
        "strategy_index": strategy_index,
        "strategy_attempt": attempt_count % strategy_span,
        "strategy_rotated": strategy_rotated,
        "instruction": instruction,
    }


def build_gemini_bank_prompt(item_type, level, count, exclude_keys=None, strategy=None):
    if item_type == "word":
        category = "approved_slang" if level == "SNS" else "general"
        schema = {"items": [{"word": "", "reading": "", "meaning": "", "part_of_speech": "", "jlpt_level": level, "category": category, "normalized_key": "", "example_sentence": "", "example_translation_zh": ""}]}
        detail = "Generate approved SNS / casual Japanese expressions with safe learning notes." if level == "SNS" else "Generate useful standard Japanese vocabulary for daily learning. Avoid mechanical business compounds."
    elif item_type == "verb":
        schema = {"items": [{"surface": "", "dictionary_form": "", "reading_hiragana": "", "meaning_zh": "", "verb_group": 1, "verb_type": "", "jlpt_level": level, "normalized_key": "", "forms": {"dictionary": "", "masu_stem": "", "te": "", "ta": "", "nai": "", "ba": "", "volitional": "", "potential": "", "causative": "", "passive": "", "causativePassive": ""}}]}
        detail = "Generate real Japanese verbs only. Avoid fake noun + suru compounds. Include complete conjugation forms."
    elif item_type == "grammar":
        schema = {"items": [{"grammar_key": "", "title": "", "display_name": "", "jlpt_level": level, "grammar_type": "", "meaning_zh": "", "connection": "", "structure_formula": "", "usage_summary_zh": "", "usage_detail_zh": "", "example_japanese": "", "example_hiragana": "", "example_zh": "", "note_zh": "", "learning_tip_zh": "", "common_mistake_zh": "", "usage_items": []}]}
        detail = "Generate concise JLPT grammar points for daily learning."
    else:
        raise ValueError(f"unsupported_bank_item_type:{item_type}")
    exclude = [str(key) for key in (exclude_keys or []) if key]
    exclude_instruction = ""
    if exclude:
        exclude_instruction = (
            " Do not generate any item whose normalized_key, surface, word, title, or dictionary_form matches these existing keys: "
            f"{json.dumps(exclude[:120], ensure_ascii=False)}."
        )
    strategy_instruction = ""
    if isinstance(strategy, dict) and strategy.get("instruction"):
        strategy_instruction = (
            f" Current refill strategy: {strategy.get('strategy')}. "
            f"{strategy.get('instruction')} "
        )
    return (
        "Return JSON only. Do not use Markdown. "
        f"Generate exactly {count} {item_type} candidates for JLPT {level}. {detail} "
        f"{strategy_instruction}"
        "Readings must be hiragana. Meanings must be Traditional Chinese. No duplicates. No empty strings. "
        "Prioritize diverse, non-overlapping items that are different from previous outputs."
        f"{exclude_instruction} "
        f"Schema: {json.dumps(schema, ensure_ascii=False)}"
    )


def parse_gemini_bank_items(item_type, level, raw_text):
    parsed = parse_gemini_stage_payload(raw_text)
    source = source_from_gemini_payload(parsed)
    raw_items = source.get("items") or source.get("vocab") or source.get("vocabulary") or source.get("verbs") or source.get("grammar_points") or []
    if not isinstance(raw_items, list):
        raise ValueError("invalid_bank_items")
    items = []
    seen = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        try:
            bank_item = gemini_bank_item_from_payload(item_type, raw, level)
        except ValueError:
            continue
        key = bank_item["normalized_key"]
        if key in seen:
            continue
        seen.add(key)
        items.append(bank_item)
    if not items:
        raise ValueError("empty_bank_items")
    return items


def upsert_gemini_bank_items(items):
    if not items:
        return {"inserted": 0, "skipped": 0}
    ensure_gemini_item_bank_store()
    now = utc_now_iso()
    inserted = 0
    skipped = 0
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for item in items:
                    cur.execute(
                        """
                        INSERT INTO gemini_item_bank (
                            item_type, normalized_key, display_text, reading, jlpt_level, category, source,
                            status, payload_json, used_count, created_at, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'unused', %s, 0, %s, %s)
                        ON CONFLICT (item_type, normalized_key) DO NOTHING
                        """,
                        (
                            item["item_type"],
                            item["normalized_key"],
                            item["display_text"],
                            item["reading"],
                            item["jlpt_level"],
                            item["category"],
                            item["source"],
                            item["payload_json"],
                            now,
                            now,
                        ),
                    )
                    if cur.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
            conn.commit()
    else:
        with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
            for item in items:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO gemini_item_bank (
                        item_type, normalized_key, display_text, reading, jlpt_level, category, source,
                        status, payload_json, used_count, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'unused', ?, 0, ?, ?)
                    """,
                    (
                        item["item_type"],
                        item["normalized_key"],
                        item["display_text"],
                        item["reading"],
                        item["jlpt_level"],
                        item["category"],
                        item["source"],
                        item["payload_json"],
                        now,
                        now,
                    ),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    skipped += 1
            conn.commit()
    return {"inserted": inserted, "skipped": skipped}


def ensure_gemini_bank_level_capacity(item_type, level, min_unused_per_level, requested_count=None):
    return ensure_gemini_bank_level_capacity_for_generation(
        item_type,
        level,
        min_unused_per_level=min_unused_per_level,
        requested_count=requested_count,
    )


def ensure_gemini_bank_level_capacity_for_generation(
    item_type,
    level,
    min_unused_per_level=None,
    requested_count=None,
    required_for_generation=None,
    target_stock=None,
    attempt_count=0,
    duplicate_streak=0,
):
    ensure_gemini_item_bank_store()
    level = normalize_gemini_bank_level(level, item_type)
    if target_stock is not None:
        target = max(0, int(target_stock or 0))
        before = gemini_bank_count_fresh_stock(item_type, level)
        active_total = gemini_bank_count_active_total(item_type, level)
        missing = max(0, target - before)
        print(f"[gemini-bank] stock item_type={item_type} level={level} active_total={active_total} fresh_stock={before} target_fresh={target}")
        if missing <= 0:
            print(f"[gemini-bank] pool_ready item_type={item_type} level={level} fresh_stock={before} target_fresh={target}")
            return {
                "active_total": active_total,
                "fresh_stock": before,
                "target_fresh_stock": target,
                "missing_count": 0,
                "requested": 0,
                "inserted": 0,
                "skipped": 0,
                "fresh_stock_after": before,
                "pool_ready": True,
                "continue_same_step": False,
                "skipped_refill": True,
                "reason": "fresh_stock_ready",
            }
    else:
        before = gemini_bank_count_unused(item_type, level)
        missing = None
        target = None

    if target_stock is not None:
        needed = missing
    elif required_for_generation is not None:
        required = max(0, int(required_for_generation or 0))
        needed = max(0, required - before)
        print(f"[gemini-bank] generation_need item_type={item_type} level={level} unused={before} required={required}")
        if needed <= 0:
            print(f"[gemini-bank] skip refill item_type={item_type} level={level} reason=enough_for_generation")
            return {
                "before_unused": before,
                "required_for_generation": required,
                "requested": 0,
                "inserted": 0,
                "skipped": 0,
                "after_unused": before,
                "skipped_refill": True,
                "reason": "enough_for_generation",
            }
    else:
        minimum = int(min_unused_per_level)
        needed = max(0, minimum - before)
        print(f"[gemini-bank] check item_type={item_type} level={level} unused={before} min={minimum}")
        if needed <= 0:
            return {"before_unused": before, "requested": 0, "inserted": 0, "skipped": 0, "after_unused": before, "skipped_refill": True}
    if needed <= 0:
        return {"before_unused": before, "requested": 0, "inserted": 0, "skipped": 0, "after_unused": before, "skipped_refill": True}
    if not GEMINI_API_KEY:
        raise RuntimeError("gemini_generation_not_available")
    strategy = gemini_bank_refill_strategy(item_type, attempt_count=attempt_count, duplicate_streak=duplicate_streak)
    if strategy.get("strategy_rotated"):
        print(
            f"[gemini-bank] strategy rotated item_type={item_type} level={level} "
            f"strategy={strategy.get('strategy')} attempt_count={int(attempt_count or 0)} "
            f"duplicate_streak={int(duplicate_streak or 0)}"
        )
    step_limit = GEMINI_BANK_REFILL_BATCH_SIZE
    if requested_count is not None:
        step_limit = min(step_limit, max(1, int(requested_count or 1)))
    request_count = min(step_limit, 3, needed)
    print(f"[gemini-bank] refill needed item_type={item_type} level={level} missing_fresh={needed} batch={request_count}")
    print(f"[gemini-bank] refill start item_type={item_type} level={level} count={request_count} timeout={GEMINI_BANK_STAGE_TIMEOUT_SECONDS}")
    try:
        exclude_limit = 240 if int(duplicate_streak or 0) >= 6 else 180 if int(duplicate_streak or 0) >= 3 else 120
        exclude_keys = gemini_bank_existing_keys(item_type, level, limit=exclude_limit)
        prompt = build_gemini_bank_prompt(item_type, level, request_count, exclude_keys=exclude_keys, strategy=strategy)
        raw_text = call_gemini(prompt, timeout_seconds=GEMINI_BANK_STAGE_TIMEOUT_SECONDS)
        items = parse_gemini_bank_items(item_type, level, raw_text)
        result = upsert_gemini_bank_items(items)
        inserted = int(result.get("inserted") or 0)
        skipped = int(result.get("skipped") or 0)
        if target_stock is not None:
            after_stock = gemini_bank_count_fresh_stock(item_type, level)
            after_active_total = gemini_bank_count_active_total(item_type, level)
            remaining = max(0, target - after_stock)
            pool_ready = remaining <= 0
            print(f"[gemini-bank] refill saved item_type={item_type} level={level} inserted={inserted} skipped={skipped} fresh_stock_after={after_stock}")
            if not pool_ready:
                print(f"[gemini-bank] continue_same_step item_type={item_type} level={level} fresh_stock={after_stock} target_fresh={target}")
            else:
                print(f"[gemini-bank] pool_ready item_type={item_type} level={level} fresh_stock={after_stock} target_fresh={target}")
            warning = "gemini_bank_duplicate_batch" if inserted == 0 and skipped > 0 else ""
            return {
                "active_total": after_active_total,
                "fresh_stock": before,
                "target_fresh_stock": target,
                "missing_count": remaining,
                "requested": request_count,
                "inserted": inserted,
                "skipped": skipped,
                "fresh_stock_after": after_stock,
                "pool_ready": pool_ready,
                "continue_same_step": not pool_ready,
                "skipped_refill": False,
                "attempt_count": int(attempt_count or 0) + 1,
                "duplicate_streak": int(duplicate_streak or 0) + 1 if inserted == 0 and skipped > 0 else 0,
                "warning": warning,
                "strategy": strategy.get("strategy"),
                "strategy_index": strategy.get("strategy_index"),
                "strategy_attempt": strategy.get("strategy_attempt"),
                "strategy_rotated": bool(strategy.get("strategy_rotated")),
            }
        after = gemini_bank_count_unused(item_type, level)
        print(f"[gemini-bank] refill saved item_type={item_type} level={level} inserted={inserted} skipped={skipped}")
        return {"before_unused": before, "requested": request_count, "inserted": inserted, "skipped": skipped, "after_unused": after, "skipped_refill": False}
    except Exception as exc:
        print(f"[gemini-bank] refill failed item_type={item_type} level={level} error={exc}")
        raise


def ensure_gemini_bank_capacity(item_type, target_levels, min_unused_per_level):
    refill = {}
    levels = [level for level in target_levels if level in LEVELS] or ["N5"]
    for level in levels:
        refill[level] = ensure_gemini_bank_level_capacity(item_type, level, min_unused_per_level)
    return refill


def build_level_quota(target_levels, total_count, item_type="word"):
    levels = [level for level in target_levels if level in LEVELS]
    sns_selected = item_type == "word" and "SNS" in target_levels
    total = max(0, int(total_count or 0))
    if total <= 0:
        return {}
    if not levels and sns_selected:
        return {"SNS": total}
    if not levels:
        levels = ["N5"]
    sns_quota = 1 if sns_selected and total > len(levels) else 0
    remaining = total - sns_quota
    quota = {level: 0 for level in levels}
    if remaining <= 0:
        return {"SNS": sns_quota} if sns_quota else quota
    base = remaining // len(levels)
    extra = remaining % len(levels)
    for index, level in enumerate(levels):
        quota[level] = base + (1 if index < extra else 0)
    if sns_quota:
        quota["SNS"] = sns_quota
    return {key: value for key, value in quota.items() if value > 0}


def gemini_generation_level_quota(settings, item_type):
    settings = normalize_settings(settings or {})
    target_levels = settings_target_levels(settings)
    jlpt_levels = [level for level in target_levels if level in LEVELS] or [settings.get("target_level", "N5")]
    if item_type == "word":
        return build_level_quota(target_levels, int(settings.get("vocab_count") or 0), item_type="word")
    if item_type == "verb":
        return build_level_quota(jlpt_levels, int(settings.get("verb_count") or 0), item_type="verb")
    if item_type == "grammar":
        return dict(LOCAL_FIXED_GRAMMAR_QUOTA)
    return {}


def gemini_bank_target_stock(item_type, level):
    normalized_level = normalize_gemini_bank_level(level, item_type)
    if item_type == "word" and normalized_level == "SNS":
        return GEMINI_BANK_TARGET_SNS
    if item_type == "word":
        return GEMINI_BANK_TARGET_WORD_PER_LEVEL
    if item_type == "verb":
        return GEMINI_BANK_TARGET_VERB_PER_LEVEL
    if item_type == "grammar":
        return GEMINI_BANK_TARGET_GRAMMAR_PER_LEVEL
    return 0


def gemini_required_for_generation(settings, item_type, level):
    quota = gemini_generation_level_quota(settings, item_type)
    normalized_level = normalize_gemini_bank_level(level, item_type)
    return int(quota.get(normalized_level) or 0)


def gemini_bank_stage_plan(settings):
    target_levels = settings_target_levels(settings)
    jlpt_levels = [level for level in target_levels if level in LEVELS] or [settings.get("target_level", "N5")]
    stages = []
    stages.extend(f"word:{level}" for level in jlpt_levels)
    if "SNS" in target_levels:
        stages.append("word:SNS")
    stages.extend(f"verb:{level}" for level in jlpt_levels)
    stages.extend(f"grammar:{level}" for level in reversed(LEVELS))
    stages.append("finalize")
    return stages


def gemini_bank_step_plan(settings):
    target_levels = settings_target_levels(settings)
    jlpt_levels = [level for level in target_levels if level in LEVELS] or [settings.get("target_level", "N5")]
    steps = []
    def add_step(item_type, level):
        normalized_level = normalize_gemini_bank_level(level, item_type)
        target_stock = gemini_bank_target_stock(item_type, normalized_level)
        fresh_stock = gemini_bank_count_fresh_stock(item_type, normalized_level)
        active_total = gemini_bank_count_active_total(item_type, normalized_level)
        missing_count = max(0, target_stock - fresh_stock)
        steps.append(
            {
                "stage": "refill",
                "item_type": item_type,
                "jlpt_level": normalized_level,
                "count": GEMINI_BANK_REFILL_BATCH_SIZE,
                "target_stock": target_stock,
                "target_fresh_stock": target_stock,
                "fresh_stock": fresh_stock,
                "active_total": active_total,
                "current_stock": fresh_stock,
                "missing_count": missing_count,
                "attempt_count": 0,
                "batch_size": min(3, GEMINI_BANK_REFILL_BATCH_SIZE),
            }
        )
    for level in jlpt_levels:
        add_step("word", level)
    if "SNS" in target_levels:
        add_step("word", "SNS")
    for level in jlpt_levels:
        add_step("verb", level)
    for level in reversed(LEVELS):
        add_step("grammar", level)
    steps.append({"stage": "finalize"})
    print(f"[gemini-plan] target_levels={','.join(target_levels)}")
    print(f"[gemini-plan] steps={steps}")
    return steps


def parse_gemini_bank_stage(stage):
    text = str(stage or "").strip()
    legacy = {"vocab": "word", "verbs": "verb"}
    if text in legacy:
        return legacy[text], None
    if text == "grammar":
        return "grammar", None
    if ":" not in text:
        return None, None
    item_type, level = text.split(":", 1)
    item_type = legacy.get(item_type, item_type)
    item_type = item_type.strip()
    level = level.strip().upper()
    valid_levels = [*LEVELS, "SNS"] if item_type == "word" else LEVELS
    if item_type not in {"word", "verb", "grammar"} or level not in valid_levels:
        return None, None
    return item_type, level


def select_gemini_bank_items(item_type, quota):
    ensure_gemini_item_bank_store()
    selected = []
    now = utc_now_iso()
    fresh_clause_pg = "" if GEMINI_BANK_ALLOW_RECENT_REUSE else "AND (COALESCE(used_count, 0) = 0 OR last_used_at IS NULL)"
    fresh_clause_sqlite = fresh_clause_pg
    for level, count in quota.items():
        if count <= 0:
            continue
        query_level = level
        if DATABASE_URL:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT id, item_type, normalized_key, display_text, reading, jlpt_level, category, source, payload_json
                        FROM gemini_item_bank
                        WHERE item_type = %s
                          AND status = ANY(%s)
                          AND jlpt_level = %s
                          {fresh_clause_pg}
                        ORDER BY (last_used_at IS NOT NULL) ASC, last_used_at ASC, used_count ASC, created_at ASC, id ASC
                        LIMIT %s
                        """,
                        (item_type, list(GEMINI_BANK_ACTIVE_STATUSES), query_level, int(count)),
                    )
                    rows = cur.fetchall()
                    ids = [row[0] for row in rows]
                    if ids:
                        cur.execute(
                            "UPDATE gemini_item_bank SET status = 'reserved', updated_at = %s WHERE id = ANY(%s)",
                            (now, ids),
                        )
                conn.commit()
            keys = ["id", "item_type", "normalized_key", "display_text", "reading", "jlpt_level", "category", "source", "payload_json"]
            selected.extend(dict(zip(keys, row)) for row in rows)
        else:
            with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"""
                    SELECT id, item_type, normalized_key, display_text, reading, jlpt_level, category, source, payload_json
                    FROM gemini_item_bank
                    WHERE item_type = ?
                      AND status IN ({",".join(["?"] * len(GEMINI_BANK_ACTIVE_STATUSES))})
                      AND jlpt_level = ?
                      {fresh_clause_sqlite}
                    ORDER BY (last_used_at IS NOT NULL) ASC, last_used_at ASC, used_count ASC, created_at ASC, id ASC
                    LIMIT ?
                    """,
                    (item_type, *GEMINI_BANK_ACTIVE_STATUSES, query_level, int(count)),
                ).fetchall()
                ids = [row["id"] for row in rows]
                if ids:
                    placeholders = ",".join(["?"] * len(ids))
                    conn.execute(
                        f"UPDATE gemini_item_bank SET status = 'reserved', updated_at = ? WHERE id IN ({placeholders})",
                        (now, *ids),
                    )
                conn.commit()
            selected.extend(dict(row) for row in rows)
        print(f"[gemini-bank] select item_type={item_type} level={level} selected={len(rows)}")
    return selected


def select_gemini_bank_top_up_items(item_type, deficit, excluded_keys=None):
    ensure_gemini_item_bank_store()
    limit = max(0, int(deficit or 0))
    if limit <= 0:
        return []
    excluded = {str(key) for key in (excluded_keys or set()) if key}
    now = utc_now_iso()
    fresh_clause_pg = "" if GEMINI_BANK_ALLOW_RECENT_REUSE else "AND (COALESCE(used_count, 0) = 0 OR last_used_at IS NULL)"
    fresh_clause_sqlite = fresh_clause_pg
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                excluded_clause = "AND NOT (normalized_key = ANY(%s))" if excluded else ""
                params = [item_type, list(GEMINI_BANK_ACTIVE_STATUSES), LEVELS]
                if excluded:
                    params.append(list(excluded))
                params.append(limit)
                cur.execute(
                    f"""
                    SELECT id, item_type, normalized_key, display_text, reading, jlpt_level, category, source, payload_json
                    FROM gemini_item_bank
                    WHERE item_type = %s
                      AND status = ANY(%s)
                      AND jlpt_level = ANY(%s)
                      {fresh_clause_pg}
                      {excluded_clause}
                    ORDER BY (last_used_at IS NOT NULL) ASC, last_used_at ASC, used_count ASC, created_at ASC, id ASC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
                ids = [row[0] for row in rows]
                if ids:
                    cur.execute(
                        "UPDATE gemini_item_bank SET status = 'reserved', updated_at = %s WHERE id = ANY(%s)",
                        (now, ids),
                    )
            conn.commit()
        keys = ["id", "item_type", "normalized_key", "display_text", "reading", "jlpt_level", "category", "source", "payload_json"]
        selected = [dict(zip(keys, row)) for row in rows]
    else:
        with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            level_placeholders = ",".join(["?"] * len(LEVELS))
            status_placeholders = ",".join(["?"] * len(GEMINI_BANK_ACTIVE_STATUSES))
            params = [item_type, *GEMINI_BANK_ACTIVE_STATUSES, *LEVELS]
            excluded_clause = ""
            if excluded:
                excluded_placeholders = ",".join(["?"] * len(excluded))
                excluded_clause = f"AND normalized_key NOT IN ({excluded_placeholders})"
                params.extend(list(excluded))
            params.append(limit)
            rows = conn.execute(
                f"""
                SELECT id, item_type, normalized_key, display_text, reading, jlpt_level, category, source, payload_json
                FROM gemini_item_bank
                WHERE item_type = ?
                  AND status IN ({status_placeholders})
                  AND jlpt_level IN ({level_placeholders})
                  {fresh_clause_sqlite}
                  {excluded_clause}
                ORDER BY (last_used_at IS NOT NULL) ASC, last_used_at ASC, used_count ASC, created_at ASC, id ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                placeholders = ",".join(["?"] * len(ids))
                conn.execute(
                    f"UPDATE gemini_item_bank SET status = 'reserved', updated_at = ? WHERE id IN ({placeholders})",
                    (now, *ids),
                )
            conn.commit()
        selected = [dict(row) for row in rows]
    print(f"[gemini-finalize] top_up item_type={item_type} deficit={limit} added={len(selected)}")
    return selected


def mark_gemini_bank_items_used(items):
    ids = [int(item["id"]) for item in items if item.get("id")]
    if not ids:
        return
    now = utc_now_iso()
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE gemini_item_bank
                    SET status = 'unused',
                        used_count = used_count + 1,
                        first_used_at = COALESCE(first_used_at, %s),
                        last_used_at = %s,
                        updated_at = %s
                    WHERE id = ANY(%s)
                    """,
                    (now, now, now, ids),
                )
            conn.commit()
    else:
        with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
            placeholders = ",".join(["?"] * len(ids))
            conn.execute(
                f"""
                UPDATE gemini_item_bank
                SET status = 'unused',
                    used_count = used_count + 1,
                    first_used_at = COALESCE(first_used_at, ?),
                    last_used_at = ?,
                    updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (now, now, now, *ids),
            )
            conn.commit()


def release_gemini_bank_items(items):
    ids = [int(item["id"]) for item in items if item.get("id")]
    if not ids:
        return
    now = utc_now_iso()
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE gemini_item_bank SET status = 'unused', updated_at = %s WHERE id = ANY(%s) AND status = 'reserved'",
                    (now, ids),
                )
            conn.commit()
    else:
        with sqlite3.connect(SQLITE_SETTINGS_FILE, timeout=10) as conn:
            placeholders = ",".join(["?"] * len(ids))
            conn.execute(
                f"UPDATE gemini_item_bank SET status = 'unused', updated_at = ? WHERE id IN ({placeholders}) AND status = 'reserved'",
                (now, *ids),
            )
            conn.commit()


def bank_rows_to_payload(rows):
    payloads = []
    for row in rows:
        parsed = gemini_job_json(row.get("payload_json"), {})
        if isinstance(parsed, dict):
            payloads.append(parsed)
    return payloads


def build_gemini_bank_material(settings, selected_words, selected_verbs, selected_grammar, level_quota, bank_refill):
    vocab = bank_rows_to_payload(selected_words)
    verbs = bank_rows_to_payload(selected_verbs)
    grammar_points = bank_rows_to_payload(selected_grammar)
    grammar = {
        "title": (grammar_points[0].get("display_name") or grammar_points[0].get("title") or "Grammar") if grammar_points else "Grammar",
        "exp": grammar_points[0].get("meaning_zh", "") if grammar_points else "",
        "examples": [
            {
                "jp": grammar_points[0].get("example_japanese", ""),
                "cn": grammar_points[0].get("example_zh", ""),
            }
        ] if grammar_points else [],
    }
    selected_counts_by_level = {
        "word": dict(Counter(item.get("jlpt_level", "") for item in vocab if item.get("jlpt_level"))),
        "verb": dict(Counter(item.get("jlpt_level", "") for item in verbs if item.get("jlpt_level"))),
        "grammar": dict(Counter(item.get("jlpt_level", "") for item in grammar_points if item.get("jlpt_level"))),
    }
    material = {
        "date": material_date_display(get_today_taipei_date()),
        "level": settings.get("target_level", "N5"),
        "target_levels": settings_target_levels(settings),
        "grammar_level": "fixed_quota",
        "vocab": vocab,
        "verbs": verbs,
        "grammar": grammar,
        "grammar_points": grammar_points,
        "metadata": {
            "generation_mode": "gemini_bank",
            "generation_source": "gemini_api",
            "ai_used": True,
            "fallback_used": False,
            "bank_enabled": True,
            "level_quota": level_quota,
            "selected_counts_by_level": selected_counts_by_level,
            "bank_refill": bank_refill,
            "source_summary": {"gemini_bank": 1},
            "generated_at": utc_now_iso(),
            "grammar_mode": "fixed_quota",
            "grammar_quota": dict(LOCAL_FIXED_GRAMMAR_QUOTA),
            "grammar_total_target": LOCAL_FIXED_GRAMMAR_TOTAL,
            "grammar_total_actual": len(grammar_points),
        },
    }
    requested_words = int(settings.get("vocab_count") or 0)
    requested_verbs = int(settings.get("verb_count") or 0)
    if len(vocab) < requested_words:
        raise ValueError(f"insufficient_vocab_count:{len(vocab)}/{requested_words}")
    if len(verbs) < requested_verbs:
        raise ValueError(f"insufficient_verb_count:{len(verbs)}/{requested_verbs}")
    return material


def generate_daily_material_from_gemini_bank(posted_settings=None, app_url=None):
    if not GEMINI_API_KEY:
        return {
            "ok": False,
            "error": "gemini_generation_not_available",
            "reason": "Gemini daily material generator is not configured",
        }
    settings, settings_source, db_settings = resolve_generation_settings_with_trace(posted_settings or {}, persist=bool(posted_settings))
    target_levels = settings_target_levels(settings)
    jlpt_levels = [level for level in target_levels if level in LEVELS] or [settings.get("target_level", "N5")]
    word_count = int(settings.get("vocab_count") or 0)
    verb_count = int(settings.get("verb_count") or 0)
    word_quota = build_level_quota(target_levels, word_count)
    verb_quota = build_level_quota(jlpt_levels, verb_count)
    grammar_quota = dict(LOCAL_FIXED_GRAMMAR_QUOTA)
    print(
        "[gemini-bank] start "
        f"target_levels={','.join(target_levels)} word_count={word_count} verb_count={verb_count}"
    )
    try:
        bank_refill = {
            "word": ensure_gemini_bank_capacity("word", jlpt_levels, GEMINI_BANK_MIN_UNUSED["word"]),
            "verb": ensure_gemini_bank_capacity("verb", jlpt_levels, GEMINI_BANK_MIN_UNUSED["verb"]),
            "grammar": ensure_gemini_bank_capacity("grammar", LEVELS, GEMINI_BANK_MIN_UNUSED["grammar"]),
        }
        selected_words = select_gemini_bank_items("word", word_quota)
        selected_verbs = select_gemini_bank_items("verb", verb_quota)
        selected_grammar = select_gemini_bank_items("grammar", grammar_quota)
        reserved = [*selected_words, *selected_verbs, *selected_grammar]
        try:
            material = build_gemini_bank_material(settings, selected_words, selected_verbs, selected_grammar, {"word": word_quota, "verb": verb_quota, "grammar": grammar_quota}, bank_refill)
            requested_words = word_count
            requested_verbs = verb_count
            actual_words = len(material.get("vocab") or [])
            actual_verbs = len(material.get("verbs") or [])
            count_validation = {
                "target_level_requested": settings.get("target_level", ""),
                "target_level_actual": material.get("level") or settings.get("target_level", ""),
                "word_count_requested": requested_words,
                "word_count_actual": actual_words,
                "verb_count_requested": requested_verbs,
                "verb_count_actual": actual_verbs,
                "word_count_matched": actual_words >= requested_words,
                "verb_count_matched": actual_verbs >= requested_verbs,
                "warnings": [],
            }
            settings_trace = build_settings_trace(
                posted_settings or {},
                db_settings,
                settings,
                settings_source,
                selector_actual={"word_count": actual_words, "verb_count": actual_verbs},
            )
            material["metadata"].update(
                {
                    "settings_source": settings_source,
                    "generation_trigger": "manual",
                    "count_validation": count_validation,
                    "settings_trace": settings_trace,
                    "resolved_settings": {
                        "generation_trigger": "manual",
                        "settings_source": settings_source,
                        "target_level": settings.get("target_level", ""),
                        "target_levels": target_levels,
                        "word_count": requested_words,
                        "verb_count": requested_verbs,
                        "choice_count": int(settings.get("mcq_count") or 0),
                        "fill_count": int(settings.get("fill_count") or 0),
                        "grammar_level": "fixed_quota",
                        "grammar_count": LOCAL_FIXED_GRAMMAR_TOTAL,
                        "grammar_mode": "fixed_quota",
                        "grammar_quota": dict(LOCAL_FIXED_GRAMMAR_QUOTA),
                        "grammar_total_target": LOCAL_FIXED_GRAMMAR_TOTAL,
                    },
                }
            )
            save_info = save_material_for_date(
                get_today_taipei_date(),
                material,
                settings,
                generation_source="gemini_api",
                generation_mode="gemini_bank",
            )
            mark_gemini_bank_items_used(reserved)
            invalidate_dashboard_cache("gemini bank material generated")
            print(f"[gemini-bank] saved material_key={save_info.get('material_key')} words={actual_words} verbs={actual_verbs}")
            return {
                "ok": True,
                "message": "今日教材已從 Gemini 候選池建立。",
                "material_date": save_info.get("material_date"),
                "material_key": save_info.get("material_key"),
                "version_no": save_info.get("version_no"),
                "generation_source": "gemini_api",
                "generation_mode": "gemini_bank",
                "ai_used": True,
                "bank_enabled": True,
                "settings_source": settings_source,
                "resolved_settings": material["metadata"].get("resolved_settings", {}),
                "count_validation": count_validation,
                "settings_trace": settings_trace,
                "metadata": {
                    "level_quota": material["metadata"].get("level_quota", {}),
                    "selected_counts_by_level": material["metadata"].get("selected_counts_by_level", {}),
                    "bank_refill": bank_refill,
                },
                "selection_log_warnings": save_info.get("selection_log_warnings", []),
            }
        except Exception:
            release_gemini_bank_items(reserved)
            raise
    except Exception as exc:
        print(f"[gemini-bank] failed reason={exc}")
        print(traceback.format_exc())
        return {
            "ok": False,
            "error": "gemini_bank_generation_failed",
            "reason": classify_gemini_daily_material_error(exc),
            "message": "Gemini 候選池教材生成失敗。",
        }


def vocab_pool_db_query_allowed():
    return not (DATABASE_URL and time.time() < _VOCAB_POOL_DB_UNAVAILABLE_UNTIL)


def mark_vocab_pool_db_unavailable(reason):
    global _VOCAB_POOL_DB_UNAVAILABLE_UNTIL
    if not DATABASE_URL:
        return
    _VOCAB_POOL_DB_UNAVAILABLE_UNTIL = time.time() + 30
    print(f"[local-generate] db_pool_fetch_failed cooldown=30s reason={reason}")


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
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_pool_level_category ON vocabulary_pool(jlpt_level, category)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_pool_normalized ON vocabulary_pool(normalized_key)")
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
                    material_key TEXT,
                    material_version_no INTEGER,
                    selected_for TEXT,
                    created_at TIMESTAMPTZ
                )
                """
            )
            cur.execute("ALTER TABLE vocab_selection_logs ADD COLUMN IF NOT EXISTS material_key TEXT")
            cur.execute("ALTER TABLE vocab_selection_logs ADD COLUMN IF NOT EXISTS material_version_no INTEGER")
            cur.execute("ALTER TABLE vocab_selection_logs ADD COLUMN IF NOT EXISTS selected_for TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_rules_rule_key ON vocab_appearance_rules(rule_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_rules_group_key ON vocab_appearance_rules(group_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_material_date ON vocab_selection_logs(material_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_rule_date ON vocab_selection_logs(rule_key, material_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_key_date ON vocab_selection_logs(normalized_key, material_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_material_key ON vocab_selection_logs(material_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_logs_selected_date_key ON vocab_selection_logs(selected_for, material_date, normalized_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_logs_selected_key_date ON vocab_selection_logs(selected_for, normalized_key, material_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_logs_selected_created ON vocab_selection_logs(selected_for, created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_group_date ON vocab_selection_logs(group_key, match_value, material_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vocab_selection_logs_source_date ON vocab_selection_logs(source_type, match_value, material_date)")
        conn.commit()


def migrate_grammar_points_postgres():
    if not DATABASE_URL:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS grammar_points (
                    id BIGSERIAL PRIMARY KEY,
                    jlpt_level TEXT NOT NULL,
                    grammar_key TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    grammar_type TEXT,
                    meaning_zh TEXT DEFAULT '',
                    connection TEXT DEFAULT '',
                    usage_summary_zh TEXT NOT NULL,
                    usage_detail_zh TEXT,
                    structure_formula TEXT,
                    example_japanese TEXT NOT NULL,
                    example_hiragana TEXT,
                    example_zh TEXT NOT NULL,
                    common_mistake_zh TEXT,
                    learning_tip_zh TEXT,
                    note_zh TEXT,
                    fake_name_example TEXT,
                    usage_items TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    priority INTEGER DEFAULT 50,
                    used_count INTEGER DEFAULT 0,
                    last_used_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ
                )
                """
            )
            columns = {
                "jlpt_level": "TEXT DEFAULT 'N5'",
                "grammar_key": "TEXT",
                "title": "TEXT DEFAULT ''",
                "display_name": "TEXT DEFAULT ''",
                "grammar_type": "TEXT",
                "meaning_zh": "TEXT DEFAULT ''",
                "connection": "TEXT DEFAULT ''",
                "usage_summary_zh": "TEXT DEFAULT ''",
                "usage_detail_zh": "TEXT",
                "structure_formula": "TEXT",
                "example_japanese": "TEXT DEFAULT ''",
                "example_hiragana": "TEXT",
                "example_zh": "TEXT DEFAULT ''",
                "common_mistake_zh": "TEXT",
                "learning_tip_zh": "TEXT",
                "note_zh": "TEXT",
                "fake_name_example": "TEXT",
                "usage_items": "TEXT",
                "is_active": "BOOLEAN DEFAULT TRUE",
                "priority": "INTEGER DEFAULT 50",
                "used_count": "INTEGER DEFAULT 0",
                "last_used_at": "TIMESTAMPTZ",
                "created_at": "TIMESTAMPTZ",
                "updated_at": "TIMESTAMPTZ",
            }
            for column, col_type in columns.items():
                cur.execute(f"ALTER TABLE grammar_points ADD COLUMN IF NOT EXISTS {column} {col_type}")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS grammar_selection_logs (
                    id BIGSERIAL PRIMARY KEY,
                    material_date DATE NOT NULL,
                    grammar_point_id BIGINT,
                    grammar_key TEXT,
                    jlpt_level TEXT,
                    grammar_type TEXT,
                    material_key TEXT,
                    material_version_no INTEGER,
                    created_at TIMESTAMPTZ
                )
                """
            )
            cur.execute("ALTER TABLE grammar_selection_logs ADD COLUMN IF NOT EXISTS material_key TEXT")
            cur.execute("ALTER TABLE grammar_selection_logs ADD COLUMN IF NOT EXISTS material_version_no INTEGER")
            cur.execute("ALTER TABLE grammar_selection_logs ADD COLUMN IF NOT EXISTS version_no INTEGER")
            cur.execute("ALTER TABLE grammar_selection_logs ADD COLUMN IF NOT EXISTS selected_for TEXT DEFAULT 'grammar'")
            cur.execute("ALTER TABLE grammar_selection_logs ADD COLUMN IF NOT EXISTS title TEXT")
            cur.execute("ALTER TABLE grammar_selection_logs ADD COLUMN IF NOT EXISTS pattern TEXT")
            cur.execute("ALTER TABLE grammar_selection_logs ADD COLUMN IF NOT EXISTS category TEXT")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_grammar_points_key ON grammar_points(grammar_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_grammar_points_level ON grammar_points(jlpt_level)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_grammar_points_active ON grammar_points(is_active)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_grammar_selection_logs_date ON grammar_selection_logs(material_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_grammar_selection_logs_key_date ON grammar_selection_logs(grammar_key, material_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_grammar_logs_date_key ON grammar_selection_logs(material_date, grammar_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_grammar_selection_logs_level_date ON grammar_selection_logs(jlpt_level, material_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_grammar_selection_logs_material_key ON grammar_selection_logs(material_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_grammar_logs_selected_date_key ON grammar_selection_logs(selected_for, material_date, grammar_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_grammar_logs_level_key_date ON grammar_selection_logs(jlpt_level, grammar_key, material_date)")
            seed_grammar_points_postgres(cur)
        conn.commit()


def ensure_slang_candidates_store():
    if not migrations_allowed_now():
        return
    if DATABASE_URL:
        migrate_slang_candidates_postgres()
    else:
        ensure_settings_store()


def ensure_vocabulary_pool_store():
    if not migrations_allowed_now():
        return
    if DATABASE_URL:
        migrate_vocabulary_pool_postgres()
    else:
        ensure_settings_store()


def ensure_vocab_rules_store():
    if not migrations_allowed_now():
        return
    if DATABASE_URL:
        migrate_vocab_rules_postgres()
    else:
        ensure_settings_store()


def ensure_grammar_points_store():
    if not migrations_allowed_now():
        return
    if DATABASE_URL:
        migrate_grammar_points_postgres()
    else:
        ensure_settings_store()


def material_version_columns():
    return {
        "material_key": "TEXT DEFAULT ''",
        "material_date": "DATE",
        "version_no": "INTEGER DEFAULT 1",
        "generation_source": "TEXT DEFAULT ''",
        "is_latest": "BOOLEAN DEFAULT TRUE",
        "updated_at": "TIMESTAMPTZ",
    }


def migrate_material_versions_postgres(cur):
    for column, column_type in material_version_columns().items():
        cur.execute(f"ALTER TABLE materials ADD COLUMN IF NOT EXISTS {column} {column_type}")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_materials_material_date ON materials(material_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_materials_material_key ON materials(material_key)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_materials_date_latest ON materials(material_date, is_latest)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_materials_date_version ON materials(material_date, version_no)")
    cur.execute(
        """
        SELECT id, date, material_date, version_no, material_key, material_json, created_at
        FROM materials
        ORDER BY COALESCE(created_at, NOW()), id
        """
    )
    rows = cur.fetchall()
    version_state = {}
    current_version = {}
    for row in rows:
        row_id, date_value, material_date_value, version_value, key_value, material_json_value, _created_at = row
        date_iso = canonical_material_date(material_date_value or date_value)
        has_existing_version = str(version_value or "").strip().isdigit()
        has_existing_key = bool(str(key_value or "").strip())
        if has_existing_version and has_existing_key and material_date_value:
            version_no = int(version_value)
            version_state[date_iso] = max(version_state.get(date_iso, 0), version_no)
            current_version[date_iso] = max(current_version.get(date_iso, 0), version_no)
            continue
        if str(material_json_value or "").strip() or date_iso not in current_version:
            version_no = version_state.get(date_iso, 0) + 1
            version_state[date_iso] = version_no
            current_version[date_iso] = version_no
        else:
            version_no = current_version[date_iso]
        material_key = build_material_key(date_iso, version_no)
        cur.execute(
            """
            UPDATE materials
            SET material_date = %s,
                version_no = %s,
                material_key = %s,
                generation_source = COALESCE(NULLIF(generation_source, ''), 'migration'),
                generation_mode = COALESCE(NULLIF(generation_mode, ''), 'local'),
                updated_at = COALESCE(NULLIF(updated_at::text, ''), %s)
            WHERE id = %s
            """,
            (date_iso, version_no, material_key, utc_now_iso(), row_id),
        )
    cur.execute("UPDATE materials SET is_latest = FALSE WHERE material_date IS NOT NULL")
    cur.execute(
        """
        WITH latest AS (
            SELECT material_date, MAX(version_no) AS max_version
            FROM materials
            WHERE material_date IS NOT NULL
            GROUP BY material_date
        )
        UPDATE materials AS m
        SET is_latest = TRUE
        FROM latest
        WHERE m.material_date = latest.material_date
          AND m.version_no = latest.max_version
        """
    )


def ensure_material_version_columns_df(df):
    df = df.copy()
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ""
    version_state = {}
    current_version = {}
    for index, row in df.iterrows():
        date_iso = canonical_material_date(row.get("material_date") or row.get("date"))
        existing_version = str(row.get("version_no", "") or "").strip()
        existing_key = str(row.get("material_key", "") or "").strip()
        if existing_version.isdigit() and existing_key and str(row.get("material_date", "") or "").strip():
            version_no = int(existing_version)
            version_state[date_iso] = max(version_state.get(date_iso, 0), version_no)
            current_version[date_iso] = max(current_version.get(date_iso, 0), version_no)
            continue
        if str(row.get("material_json", "") or "").strip() or date_iso not in current_version:
            version_no = version_state.get(date_iso, 0) + 1
            version_state[date_iso] = version_no
            current_version[date_iso] = version_no
        else:
            version_no = current_version[date_iso]
        df.at[index, "material_date"] = date_iso
        df.at[index, "version_no"] = str(version_no)
        df.at[index, "material_key"] = build_material_key(date_iso, version_no)
        if not str(row.get("generation_source", "") or "").strip():
            df.at[index, "generation_source"] = "migration"
        if not str(row.get("generation_mode", "") or "").strip():
            df.at[index, "generation_mode"] = "local"
    if not df.empty:
        df["is_latest"] = "false"
        latest = {}
        for _, row in df.iterrows():
            date_iso = canonical_material_date(row.get("material_date") or row.get("date"))
            try:
                version_no = int(row.get("version_no") or 1)
            except (TypeError, ValueError):
                version_no = 1
            latest[date_iso] = max(latest.get(date_iso, 0), version_no)
        for index, row in df.iterrows():
            date_iso = canonical_material_date(row.get("material_date") or row.get("date"))
            try:
                version_no = int(row.get("version_no") or 1)
            except (TypeError, ValueError):
                version_no = 1
            if latest.get(date_iso) == version_no:
                df.at[index, "is_latest"] = "true"
    return df[COLUMNS]


def ensure_database():
    global _MATERIALS_SCHEMA_READY
    if _MATERIALS_SCHEMA_READY:
        return
    if not migrations_allowed_now():
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
                typed_material_columns = material_version_columns()
                for col, column_type in typed_material_columns.items():
                    cur.execute(f"ALTER TABLE materials ADD COLUMN IF NOT EXISTS {col} {column_type}")
                for col in COLUMNS:
                    if col not in ("date", *typed_material_columns.keys()):
                        cur.execute(f"ALTER TABLE materials ADD COLUMN IF NOT EXISTS {col} TEXT DEFAULT ''")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_materials_date ON materials(date)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_materials_created_at ON materials(created_at)")
                migrate_material_versions_postgres(cur)
            conn.commit()
        migrate_slang_candidates_postgres()
        migrate_vocabulary_pool_postgres()
        migrate_vocab_rules_postgres()
        migrate_grammar_points_postgres()
        return

    if not os.path.exists(DATABASE_FILE):
        pd.DataFrame(columns=COLUMNS).to_csv(DATABASE_FILE, index=False, encoding="utf-8-sig")
    else:
        df = pd.read_csv(DATABASE_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        migrated = ensure_material_version_columns_df(df)
        migrated.to_csv(DATABASE_FILE, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)


def read_database():
    ensure_database()
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {', '.join(COLUMNS)} FROM materials ORDER BY id")
                rows = cur.fetchall()
        return pd.DataFrame(rows, columns=COLUMNS).astype(str) if rows else pd.DataFrame(columns=COLUMNS)

    if not os.path.exists(DATABASE_FILE):
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(DATABASE_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    return ensure_material_version_columns_df(df)


def material_version_sort_value(row):
    try:
        version_no = int(row.get("version_no") or 0)
    except (TypeError, ValueError):
        version_no = 0
    return version_no


def read_material_rows_by_key(material_key):
    ensure_database()
    key = str(material_key or "").strip()
    if not key:
        return pd.DataFrame(columns=COLUMNS)
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {', '.join(COLUMNS)} FROM materials WHERE material_key = %s ORDER BY id",
                    (key,),
                )
                rows = cur.fetchall()
        return pd.DataFrame(rows, columns=COLUMNS).astype(str) if rows else pd.DataFrame(columns=COLUMNS)
    df = read_database()
    return df[df["material_key"] == key]


def latest_material_key_for_date(target_date, generation_source=None):
    ensure_database()
    date_iso = canonical_material_date(target_date)
    variants = material_date_variants(date_iso)
    source = str(generation_source or "").strip()
    if DATABASE_URL:
        placeholders = ", ".join(["%s"] * len(variants))
        source_sql = " AND generation_source = %s" if source else ""
        params = [date_iso, *variants]
        if source:
            params.append(source)
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT material_key
                    FROM materials
                    WHERE (material_date = %s OR date IN ({placeholders}))
                      AND COALESCE(material_key, '') <> ''
                      {source_sql}
                    ORDER BY is_latest DESC, version_no DESC, created_at DESC, id DESC
                    LIMIT 1
                    """,
                    tuple(params),
                )
                row = cur.fetchone()
        return row[0] if row else ""
    df = read_database()
    rows = df[(df["material_date"].isin([date_iso])) | (df["date"].isin(variants))]
    if source:
        rows = rows[rows["generation_source"].astype(str) == source]
    if rows.empty:
        return ""
    rows = rows.copy()
    rows["_version_sort"] = rows.apply(material_version_sort_value, axis=1)
    rows["_latest_sort"] = rows["is_latest"].astype(str).str.lower().isin(["true", "1", "t", "yes"]).astype(int)
    rows = rows.sort_values(["_latest_sort", "_version_sort", "created_at"], ascending=[False, False, False])
    return str(rows.iloc[0].get("material_key", "") or "")


def read_material_rows_by_date(target_date):
    ensure_database()
    material_key = latest_material_key_for_date(target_date)
    if material_key:
        return read_material_rows_by_key(material_key)
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
        if isinstance(getattr(e, "reason", None), TimeoutError) or "timed out" in str(getattr(e, "reason", "")).lower():
            raise RuntimeError(f"AI 服務逾時；model={model_name}；timeout={timeout_seconds}s") from e
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


def build_gemini_daily_material_prompt(settings):
    target_levels = settings_target_levels(settings)
    word_count = int(settings.get("vocab_count") or 0)
    verb_count = int(settings.get("verb_count") or 0)
    grammar_quota = dict(LOCAL_FIXED_GRAMMAR_QUOTA)
    grammar_total = int(sum(grammar_quota.values()))
    schema = {
        "level": settings.get("target_level", "N5"),
        "target_levels": target_levels,
        "vocab": [
            {
                "word": "見る",
                "reading": "みる",
                "meaning": "看",
                "part_of_speech": "動詞",
                "jlpt_level": "N5",
                "category": "general",
                "example_sentence": "映画を見ます。",
                "example_translation_zh": "我看電影。",
            }
        ],
        "verbs": [
            {
                "surface": "見る",
                "dictionary_form": "見る",
                "reading_hiragana": "みる",
                "meaning_zh": "看",
                "verb_group": 2,
                "verb_type": "一段動詞",
                "jlpt_level": "N5",
                "forms": {
                    "dictionary": "見る",
                    "masu_stem": "見",
                    "te": "見て",
                    "ta": "見た",
                    "nai": "見ない",
                    "ba": "見れば",
                    "volitional": "見よう",
                    "potential": "見られる",
                    "causative": "見させる",
                    "passive": "見られる",
                    "causativePassive": "見させられる",
                },
            }
        ],
        "grammar": {
            "title": "今日の文法",
            "exp": "本日教材中的重點文法整理。",
            "examples": [{"jp": "今日は雨です。", "cn": "今天下雨。"}],
        },
        "grammar_points": [
            {
                "grammar_key": "particle:は",
                "title": "は",
                "display_name": "は",
                "jlpt_level": "N5",
                "grammar_type": "particle",
                "meaning_zh": "主題提示 / 對比",
                "connection": "名詞 + は",
                "structure_formula": "名詞 + は",
                "usage_summary_zh": "提示句子的主題。",
                "usage_detail_zh": "用來指出接下來要談論的主題。",
                "example_japanese": "私は学生です。",
                "example_hiragana": "わたしはがくせいです。",
                "example_zh": "我是學生。",
                "note_zh": "不要把 は 只理解成主詞標記。",
                "learning_tip_zh": "把 は 想成「至於...」。",
                "common_mistake_zh": "初學者常把 は 和 が 混用。",
                "usage_items": [],
            }
        ],
    }
    return f"""
你是日語學習教材生成器。請根據使用者設定，生成一份「每日教材」。

設定：
- target_levels: {json.dumps(target_levels, ensure_ascii=False)}
- primary target_level: {settings.get("target_level", "N5")}
- word_count: {word_count}
- verb_count: {verb_count}
- grammar_quota: {json.dumps(grammar_quota, ensure_ascii=False)}
- grammar_total_target: {grammar_total}

嚴格要求：
1. 只回傳 JSON，不要 Markdown，不要說明文字，不要 code fence。
2. vocab 必須剛好 {word_count} 筆，word 不可重複。
3. verbs 必須剛好 {verb_count} 筆，surface/dictionary_form 不可重複。
4. grammar_points 目標最多 {grammar_total} 筆，文法點不可重複，盡量依 grammar_quota 分配。
5. reading / reading_hiragana / example_hiragana 必須是平假名。
6. meaning / meaning_zh / example_translation_zh 必須是繁體中文。
7. JLPT level 必須落在 target_levels 內；若含 SNS，SNS 只可少量作為詞彙，不可主導教材。
8. 例句要自然、短、適合學習。
9. verbs 只使用真正日語動詞，不要用大量「名詞 + する」或商務機械詞補數量。
10. 每個 verb 的 forms 必須完整包含 dictionary, masu_stem, te, ta, nai, ba, volitional, potential, causative, passive, causativePassive。
11. 不可回空字串；不知道就換一個更基礎的詞或文法。

請完全符合這個 JSON schema 的欄位名稱：
{json.dumps(schema, ensure_ascii=False, indent=2)}
""".strip()


def build_gemini_stage_prompt(stage, settings):
    target_levels = settings_target_levels(settings)
    fallback_level = settings.get("target_level", "N5")
    word_count = int(settings.get("vocab_count") or 0)
    verb_count = int(settings.get("verb_count") or 0)
    grammar_quota = dict(LOCAL_FIXED_GRAMMAR_QUOTA)
    grammar_total = int(sum(grammar_quota.values()))
    common_rules = (
        "Return JSON only. Do not use Markdown fences. "
        "Use Traditional Chinese meanings. Japanese readings must be hiragana. "
        f"Allowed JLPT levels: {', '.join(target_levels)}. Primary level: {fallback_level}. "
        "Do not return duplicate items or empty strings."
    )
    if stage == "vocab":
        schema = {
            "vocab": [
                {
                    "word": "",
                    "reading": "",
                    "meaning": "",
                    "part_of_speech": "",
                    "jlpt_level": fallback_level,
                    "category": "general",
                    "source": "gemini",
                    "normalized_key": "",
                    "example_sentence": "",
                    "example_translation_zh": "",
                }
            ]
        }
        return (
            f"{common_rules} Generate exactly {word_count} useful Japanese vocabulary items "
            "for a daily learning material. Avoid niche business compounds and internet slang unless SNS is requested. "
            f"Schema: {json.dumps(schema, ensure_ascii=False)}"
        )
    if stage == "verbs":
        schema = {
            "verbs": [
                {
                    "surface": "",
                    "dictionary_form": "",
                    "reading_hiragana": "",
                    "meaning_zh": "",
                    "verb_group": 1,
                    "verb_type": "",
                    "jlpt_level": fallback_level,
                    "part_of_speech": "動詞",
                    "normalized_key": "",
                    "forms": {
                        "dictionary": "",
                        "masu_stem": "",
                        "te": "",
                        "ta": "",
                        "nai": "",
                        "ba": "",
                        "volitional": "",
                        "potential": "",
                        "causative": "",
                        "passive": "",
                        "causativePassive": "",
                    },
                }
            ]
        }
        return (
            f"{common_rules} Generate exactly {verb_count} real Japanese verbs for a conjugation table. "
            "Avoid fake noun + suru compounds. Include complete conjugation forms for every verb. "
            f"Schema: {json.dumps(schema, ensure_ascii=False)}"
        )
    if stage == "grammar":
        schema = {
            "grammar": {
                "title": "",
                "exp": "",
                "examples": [{"jp": "", "cn": ""}],
            },
            "grammar_points": [
                {
                    "grammar_key": "",
                    "title": "",
                    "display_name": "",
                    "jlpt_level": fallback_level,
                    "grammar_type": "",
                    "meaning_zh": "",
                    "connection": "",
                    "structure_formula": "",
                    "usage_summary_zh": "",
                    "usage_detail_zh": "",
                    "example_japanese": "",
                    "example_hiragana": "",
                    "example_zh": "",
                    "note_zh": "",
                    "learning_tip_zh": "",
                    "common_mistake_zh": "",
                    "usage_items": [],
                }
            ],
        }
        return (
            f"{common_rules} Generate daily grammar points with quota {json.dumps(grammar_quota, ensure_ascii=False)} "
            f"and at most {grammar_total} total grammar_points. Keep explanations concise. "
            f"Schema: {json.dumps(schema, ensure_ascii=False)}"
        )
    raise ValueError(f"unsupported_gemini_stage:{stage}")


def classify_gemini_daily_material_error(error):
    text = str(error or "")
    if text.startswith("json_parse_error"):
        return "json_parse_error"
    if text.startswith(("insufficient_", "invalid_", "incomplete_", "empty_")):
        return "schema_validation_failed"
    return classify_gemini_error(error)


def simple_text(value):
    return str(value or "").strip()


def normalize_gemini_level(value, fallback="N5"):
    level = simple_text(value).upper()
    return level if level in LEVELS else fallback


def normalize_gemini_forms(raw_forms, source):
    forms = raw_forms if isinstance(raw_forms, dict) else {}
    normalized = {}
    for canonical, aliases in VERB_FORM_SCHEMA.items():
        normalized[canonical] = clean_verb_form(pick_form_value(forms, aliases) or pick_form_value(source, aliases))
    for camel, canonical in VERB_ALIAS_FIELDS.items():
        if camel == "base":
            continue
        normalized[camel] = normalized.get(canonical, NO_VERB_FORM)
    return normalized


def normalize_gemini_vocab_item(item, fallback_level):
    if not isinstance(item, dict):
        item = {}
    word = simple_text(item.get("word") or item.get("surface") or item.get("term"))
    reading = enforce_hiragana_reading(item.get("reading") or item.get("reading_hiragana"), word)
    meaning = simple_text(item.get("meaning") or item.get("meaning_zh"))
    return {
        "word": word,
        "reading": reading,
        "meaning": meaning,
        "part_of_speech": simple_text(item.get("part_of_speech") or item.get("pos")),
        "jlpt_level": normalize_gemini_level(item.get("jlpt_level") or item.get("level"), fallback_level),
        "category": simple_text(item.get("category")) or "general",
        "source": "gemini",
        "normalized_key": normalize_vocab_key(item.get("normalized_key") or word),
        "example_sentence": simple_text(item.get("example_sentence") or item.get("example_japanese")),
        "example_translation_zh": simple_text(item.get("example_translation_zh") or item.get("example_zh")),
    }


def normalize_gemini_verb_item(item, fallback_level):
    if not isinstance(item, dict):
        item = {}
    forms_source = item.get("forms") if isinstance(item.get("forms"), dict) else {}
    surface = simple_text(
        item.get("surface")
        or item.get("dictionary_form")
        or item.get("base_form")
        or forms_source.get("dictionary")
        or item.get("base")
    )
    reading = enforce_hiragana_reading(item.get("reading_hiragana") or item.get("reading"), surface)
    meaning = simple_text(item.get("meaning_zh") or item.get("meaning"))
    try:
        group = int(item.get("verb_group") or 0)
    except (TypeError, ValueError):
        group = 0
    if group not in {1, 2, 3}:
        group = infer_material_verb_group(item, surface) or 0
    forms = normalize_gemini_forms(forms_source, item)
    if surface and forms.get("dictionary") in {"", NO_VERB_FORM}:
        forms["dictionary"] = surface
    normalized = {
        "surface": surface,
        "dictionary_form": surface,
        "reading_hiragana": reading,
        "reading": reading,
        "meaning_zh": meaning,
        "meaning": meaning,
        "verb_group": group,
        "verb_type": simple_text(item.get("verb_type")) or material_verb_type_label(group, surface),
        "verb_group_label": simple_text(item.get("verb_type")) or material_verb_type_label(group, surface),
        "jlpt_level": normalize_gemini_level(item.get("jlpt_level") or item.get("level"), fallback_level),
        "part_of_speech": "動詞",
        "normalized_key": normalize_vocab_key(item.get("normalized_key") or surface),
        "forms": forms,
        "base": make_verb_base_display(surface, reading, meaning, simple_text(item.get("verb_type")) or material_verb_type_label(group, surface), normalize_gemini_level(item.get("jlpt_level") or item.get("level"), fallback_level)),
    }
    for alias, form_key in VERB_ALIAS_FIELDS.items():
        if alias == "base":
            continue
        normalized[alias] = forms.get(form_key, NO_VERB_FORM)
    return normalized


def normalize_gemini_grammar_point(item, fallback_level):
    if not isinstance(item, dict):
        item = {}
    title = simple_text(item.get("title") or item.get("display_name") or item.get("grammar_key"))
    grammar_key = simple_text(item.get("grammar_key")) or normalize_vocab_key(title)
    return {
        "grammar_key": grammar_key,
        "title": title,
        "display_name": simple_text(item.get("display_name")) or title,
        "jlpt_level": normalize_gemini_level(item.get("jlpt_level") or item.get("level"), fallback_level),
        "grammar_type": simple_text(item.get("grammar_type") or item.get("category")),
        "meaning_zh": simple_text(item.get("meaning_zh") or item.get("meaning")),
        "connection": simple_text(item.get("connection") or item.get("structure_formula") or item.get("structure")),
        "structure_formula": simple_text(item.get("structure_formula") or item.get("connection") or item.get("structure")),
        "usage_summary_zh": simple_text(item.get("usage_summary_zh")),
        "usage_detail_zh": simple_text(item.get("usage_detail_zh")),
        "example_japanese": simple_text(item.get("example_japanese") or item.get("example_jp")),
        "example_hiragana": enforce_hiragana_reading(item.get("example_hiragana"), item.get("example_japanese") or item.get("example_jp")),
        "example_zh": simple_text(item.get("example_zh") or item.get("example_translation_zh")),
        "note_zh": simple_text(item.get("note_zh")),
        "learning_tip_zh": simple_text(item.get("learning_tip_zh")),
        "common_mistake_zh": simple_text(item.get("common_mistake_zh")),
        "usage_items": item.get("usage_items") if isinstance(item.get("usage_items"), list) else [],
        "category": simple_text(item.get("category") or item.get("grammar_type")),
        "source": "gemini",
    }


def unique_by_key(items, key_name):
    unique = []
    seen = set()
    for item in items:
        key = simple_text(item.get(key_name))
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def source_from_gemini_payload(raw_payload):
    source = raw_payload if isinstance(raw_payload, dict) else {}
    if isinstance(source.get("result"), dict):
        source = source["result"]
    return source


def parse_gemini_stage_payload(raw_text):
    try:
        return parse_gemini_json_safely(raw_text)
    except Exception as exc:
        raise ValueError(f"json_parse_error:{exc}") from exc


def normalize_gemini_stage_result(stage, raw_payload, settings):
    source = source_from_gemini_payload(raw_payload)
    fallback_level = settings.get("target_level", "N5")
    if stage == "vocab":
        vocab = [
            normalize_gemini_vocab_item(item, fallback_level)
            for item in (source.get("vocab") or source.get("vocabulary") or [])
            if isinstance(item, dict)
        ]
        vocab = unique_by_key(vocab, "normalized_key")
        requested = int(settings.get("vocab_count") or 0)
        if len(vocab) < requested:
            raise ValueError(f"insufficient_vocab_count:{len(vocab)}/{requested}")
        for index, item in enumerate(vocab[:requested], start=1):
            if not item.get("word") or not item.get("reading") or not item.get("meaning"):
                raise ValueError(f"invalid_vocab_item:{index}")
        return vocab[:requested]
    if stage == "verbs":
        verbs = [
            normalize_gemini_verb_item(item, fallback_level)
            for item in (source.get("verbs") or [])
            if isinstance(item, dict)
        ]
        verbs = unique_by_key(verbs, "normalized_key")
        requested = int(settings.get("verb_count") or 0)
        required_form_keys = [
            "dictionary",
            "masu_stem",
            "te_form",
            "ta_form",
            "nai_form",
            "ba_form",
            "volitional_form",
            "potential_form",
            "causative_form",
            "passive_form",
            "causative_passive_form",
        ]
        if len(verbs) < requested:
            raise ValueError(f"insufficient_verb_count:{len(verbs)}/{requested}")
        for index, item in enumerate(verbs[:requested], start=1):
            if not item.get("surface") or not item.get("reading_hiragana") or not item.get("meaning_zh"):
                raise ValueError(f"invalid_verb_item:{index}")
            forms = item.get("forms") if isinstance(item.get("forms"), dict) else {}
            if any(not forms.get(key) or forms.get(key) == NO_VERB_FORM for key in required_form_keys):
                raise ValueError(f"incomplete_verb_forms:{index}")
        return verbs[:requested]
    if stage == "grammar":
        grammar_points = [
            normalize_gemini_grammar_point(item, fallback_level)
            for item in (source.get("grammar_points") or [])
            if isinstance(item, dict)
        ]
        grammar_points = unique_by_key(grammar_points, "grammar_key")
        if not grammar_points:
            raise ValueError("empty_grammar_points")
        raw_grammar = source.get("grammar") if isinstance(source.get("grammar"), dict) else {}
        grammar_examples = raw_grammar.get("examples") if isinstance(raw_grammar.get("examples"), list) else []
        grammar = {
            "title": simple_text(raw_grammar.get("title")) or (grammar_points[0].get("display_name") or grammar_points[0].get("title") or "Grammar"),
            "exp": simple_text(raw_grammar.get("exp") or raw_grammar.get("explanation") or raw_grammar.get("meaning_zh")),
            "examples": [
                {
                    "jp": simple_text(item.get("jp") or item.get("example_japanese")),
                    "cn": simple_text(item.get("cn") or item.get("example_zh")),
                }
                for item in grammar_examples
                if isinstance(item, dict)
            ],
        }
        if not grammar["examples"] and grammar_points:
            grammar["examples"] = [
                {
                    "jp": simple_text(grammar_points[0].get("example_japanese")),
                    "cn": simple_text(grammar_points[0].get("example_zh")),
                }
            ]
        return {"grammar": grammar, "grammar_points": grammar_points[:LOCAL_FIXED_GRAMMAR_TOTAL]}
    raise ValueError(f"unsupported_gemini_stage:{stage}")


def adapt_gemini_daily_material(raw_payload, settings):
    source = source_from_gemini_payload(raw_payload)
    fallback_level = settings.get("target_level", "N5")
    target_levels = settings_target_levels(settings)
    vocab = [
        normalize_gemini_vocab_item(item, fallback_level)
        for item in (source.get("vocab") or source.get("vocabulary") or [])
        if isinstance(item, dict)
    ]
    verbs = [
        normalize_gemini_verb_item(item, fallback_level)
        for item in (source.get("verbs") or [])
        if isinstance(item, dict)
    ]
    grammar_points = [
        normalize_gemini_grammar_point(item, fallback_level)
        for item in (source.get("grammar_points") or [])
        if isinstance(item, dict)
    ]
    vocab = unique_by_key(vocab, "normalized_key")
    verbs = unique_by_key(verbs, "normalized_key")
    grammar_points = unique_by_key(grammar_points, "grammar_key")
    raw_grammar = source.get("grammar") if isinstance(source.get("grammar"), dict) else {}
    grammar_examples = raw_grammar.get("examples") if isinstance(raw_grammar.get("examples"), list) else []
    grammar = {
        "title": simple_text(raw_grammar.get("title")) or "今日の文法",
        "exp": simple_text(raw_grammar.get("exp") or raw_grammar.get("explanation")) or "本日教材中的重點文法整理。",
        "examples": [
            {
                "jp": simple_text(item.get("jp") or item.get("example_japanese")),
                "cn": simple_text(item.get("cn") or item.get("example_zh")),
            }
            for item in grammar_examples
            if isinstance(item, dict)
        ],
    }
    material = {
        "date": material_date_display(get_today_taipei_date()),
        "level": fallback_level,
        "target_levels": target_levels,
        "grammar_level": "fixed_quota",
        "vocab": vocab,
        "verbs": verbs,
        "grammar": grammar,
        "grammar_points": grammar_points,
        "metadata": {
            "generation_mode": "gemini",
            "generation_source": "gemini_api",
            "ai_used": True,
            "fallback_used": False,
            "source_summary": {"gemini": 1},
            "generated_at": utc_now_iso(),
            "grammar_mode": "fixed_quota",
            "grammar_quota": dict(LOCAL_FIXED_GRAMMAR_QUOTA),
            "grammar_total_target": LOCAL_FIXED_GRAMMAR_TOTAL,
            "grammar_total_actual": len(grammar_points),
        },
    }
    return material


def validate_gemini_daily_material(material, settings):
    requested_words = int(settings.get("vocab_count") or 0)
    requested_verbs = int(settings.get("verb_count") or 0)
    vocab = material.get("vocab") if isinstance(material.get("vocab"), list) else []
    verbs = material.get("verbs") if isinstance(material.get("verbs"), list) else []
    grammar_points = material.get("grammar_points") if isinstance(material.get("grammar_points"), list) else []
    if len(vocab) < requested_words:
        raise ValueError(f"insufficient_vocab_count:{len(vocab)}/{requested_words}")
    if len(verbs) < requested_verbs:
        raise ValueError(f"insufficient_verb_count:{len(verbs)}/{requested_verbs}")
    for index, item in enumerate(vocab[:requested_words], start=1):
        if not item.get("word") or not item.get("reading") or not item.get("meaning"):
            raise ValueError(f"invalid_vocab_item:{index}")
    required_form_keys = ["dictionary", "masu_stem", "te_form", "ta_form", "nai_form", "ba_form", "volitional_form", "potential_form", "causative_form", "passive_form", "causative_passive_form"]
    for index, item in enumerate(verbs[:requested_verbs], start=1):
        if not item.get("surface") or not item.get("reading_hiragana") or not item.get("meaning_zh"):
            raise ValueError(f"invalid_verb_item:{index}")
        forms = item.get("forms") if isinstance(item.get("forms"), dict) else {}
        if any(not forms.get(key) or forms.get(key) == NO_VERB_FORM for key in required_form_keys):
            raise ValueError(f"incomplete_verb_forms:{index}")
    if not isinstance(material.get("grammar"), dict):
        raise ValueError("invalid_grammar")
    if not grammar_points:
        raise ValueError("empty_grammar_points")
    material["vocab"] = vocab[:requested_words]
    material["verbs"] = verbs[:requested_verbs]
    return material


def build_gemini_daily_material(settings, deadline_check=None):
    if deadline_check:
        deadline_check("gemini_prompt_before")
    prompt = build_gemini_daily_material_prompt(settings)
    started = time.perf_counter()
    raw_text = call_gemini(prompt, timeout_seconds=GEMINI_GENERATE_TIMEOUT_SECONDS)
    if deadline_check:
        deadline_check("gemini_response_after")
    try:
        parsed = parse_gemini_json_safely(raw_text)
    except Exception as exc:
        raise ValueError(f"json_parse_error:{exc}") from exc
    material = adapt_gemini_daily_material(parsed, settings)
    validate_gemini_daily_material(material, settings)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    material.setdefault("metadata", {})["gemini_elapsed_ms"] = elapsed_ms
    material["metadata"]["gemini_model"] = choose_gemini_model()
    print(
        "[gemini-material] success "
        f"words={len(material.get('vocab') or [])} "
        f"verbs={len(material.get('verbs') or [])} "
        f"grammar_points={len(material.get('grammar_points') or [])} "
        f"elapsed_ms={elapsed_ms}"
    )
    return material


def gemini_job_settings(job):
    settings = gemini_job_json(job.get("settings_json"), {})
    return normalize_settings(settings if isinstance(settings, dict) else {})


def run_gemini_generation_stage(job_id, stage, item_type=None, level=None, requested_count=None):
    job = load_gemini_generation_job(job_id)
    if not job:
        return {"ok": False, "error": "gemini_job_not_found", "reason": "job_id not found"}, 404
    if job.get("status") == "failed":
        return {
            "ok": False,
            "error": "gemini_stage_failed",
            "stage": job.get("current_stage") or stage,
            "reason": job.get("error_message") or "job already failed",
        }, 200
    if not item_type or not level:
        item_type, level = parse_gemini_bank_stage(stage)
    if not item_type:
        return {"ok": False, "error": "unsupported_gemini_stage", "stage": stage, "reason": "unsupported stage"}, 400
    level = normalize_gemini_bank_level(level, item_type)
    planned_keys = gemini_planned_refill_step_keys(job)
    step_key = gemini_refill_step_key(item_type, level)
    if planned_keys and step_key not in planned_keys:
        return {
            "ok": False,
            "error": "gemini_unplanned_step_rejected",
            "stage": stage,
            "reason": "Use planned refill steps returned by /api/generate?mode=gemini",
            "received_step": step_key,
        }, 200
    settings = gemini_job_settings(job)
    started = time.perf_counter()
    print(f"[gemini-stage] start job_id={job_id} stage={stage} timeout_seconds={GEMINI_BANK_STAGE_TIMEOUT_SECONDS}")
    update_gemini_generation_job(job_id, status="running", current_stage=stage, error_message="")
    field_name = {"word": "vocab_json", "verb": "verbs_json", "grammar": "grammar_json"}[item_type]
    cache = gemini_job_json(job.get(field_name), {})
    if not isinstance(cache, dict):
        cache = {}
    refill_cache = cache.get("refill") if isinstance(cache.get("refill"), dict) else {}
    previous_refill = refill_cache.get(level) if isinstance(refill_cache.get(level), dict) else {}
    attempt_count = int(previous_refill.get("attempt_count") or 0)
    duplicate_streak = int(previous_refill.get("duplicate_streak") or 0)
    target_stock = int(gemini_bank_target_stock(item_type, level))
    try:
        refill = ensure_gemini_bank_level_capacity_for_generation(
            item_type,
            level,
            requested_count=requested_count,
            target_stock=target_stock,
            attempt_count=attempt_count,
            duplicate_streak=duplicate_streak,
        )
        refill_cache[level] = refill
        cache["refill"] = refill_cache
        update_gemini_generation_job(
            job_id,
            current_stage=f"{stage}_done",
            **{field_name: json.dumps(cache, ensure_ascii=False)},
        )
        next_stage = ""
        count = int(refill.get("inserted") or 0)
        pool_ready = bool(refill.get("pool_ready"))
        continue_same_step = bool(refill.get("continue_same_step"))
        completed_steps = gemini_completed_step_keys(load_gemini_generation_job(job_id) or job)
        if pool_ready:
            completed_steps = mark_gemini_generation_step_completed(job_id, item_type, level)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if pool_ready:
            print(f"[gemini-step-runner] completed stage=refill item_type={item_type} level={level}")
        else:
            print(f"[gemini-step-runner] pending stage=refill item_type={item_type} level={level} fresh={refill.get('fresh_stock_after')} target={refill.get('target_fresh_stock')}")
        print(f"[gemini-stage] saved job_id={job_id} stage={stage} count={count} elapsed_ms={elapsed_ms}")
        return {
            "ok": True,
            "job_id": job_id,
            "stage": stage,
            "status": "running",
            "next_stage": next_stage,
            "count": count,
            "refill": refill,
            "pool_ready": pool_ready,
            "continue_same_step": continue_same_step,
            "item_type": item_type,
            "jlpt_level": level,
            "current_stock": refill.get("fresh_stock_after", refill.get("fresh_stock")),
            "target_stock": refill.get("target_fresh_stock"),
            "fresh_stock": refill.get("fresh_stock_after", refill.get("fresh_stock")),
            "target_fresh_stock": refill.get("target_fresh_stock"),
            "active_total": refill.get("active_total"),
            "missing_count": refill.get("missing_count"),
            "warning": refill.get("warning"),
            "strategy": refill.get("strategy"),
            "strategy_rotated": bool(refill.get("strategy_rotated")),
            "duplicate_streak": refill.get("duplicate_streak"),
            "attempt_count": refill.get("attempt_count"),
            "completed_steps": completed_steps,
            "elapsed_ms": elapsed_ms,
        }, 200
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        reason = classify_gemini_daily_material_error(exc)
        message = f"{reason}:{str(exc)[:240]}"
        if "gemini_generation_not_available" in str(exc):
            return {
                "ok": False,
                "error": "gemini_generation_not_available",
                "job_id": job_id,
                "stage": stage,
                "item_type": item_type,
                "jlpt_level": level,
                "reason": "Gemini daily material generator is not configured",
                "elapsed_ms": elapsed_ms,
            }, 200
        warning_code = "gemini_bank_refill_timeout" if reason == "timeout" else "gemini_bank_refill_failed"
        warnings = cache.get("warnings") if isinstance(cache.get("warnings"), list) else []
        warning_entry = {
            "warning": warning_code,
            "item_type": item_type,
            "jlpt_level": level,
            "reason": reason,
            "message": str(exc)[:240],
            "continued": True,
        }
        warnings.append(warning_entry)
        cache["warnings"] = warnings
        refill_cache = cache.get("refill") if isinstance(cache.get("refill"), dict) else {}
        current_stock = gemini_bank_count_fresh_stock(item_type, level)
        active_total = gemini_bank_count_active_total(item_type, level)
        refill_cache[level] = {
            "warning": warning_code,
            "reason": reason,
            "message": str(exc)[:240],
            "continued": True,
            "requested": int(requested_count or 0),
            "fresh_stock": current_stock,
            "target_fresh_stock": target_stock,
            "active_total": active_total,
            "missing_count": max(0, target_stock - current_stock),
            "pool_ready": False,
            "attempt_count": attempt_count + 1,
            "duplicate_streak": duplicate_streak,
            "strategy": previous_refill.get("strategy"),
            "strategy_rotated": False,
        }
        cache["refill"] = refill_cache
        update_gemini_generation_job(
            job_id,
            status="running",
            current_stage=f"{stage}_warning",
            error_message=message,
            **{field_name: json.dumps(cache, ensure_ascii=False)},
        )
        print(f"[gemini-bank] refill warning item_type={item_type} level={level} error={reason} continued=true")
        print(f"[gemini-step-runner] continue after refill warning item_type={item_type} level={level}")
        completed_steps = gemini_completed_step_keys(load_gemini_generation_job(job_id) or job)
        return {
            "ok": True,
            "job_id": job_id,
            "stage": stage,
            "item_type": item_type,
            "jlpt_level": level,
            "warning": warning_code,
            "continued": True,
            "pool_ready": False,
            "continue_same_step": True,
            "current_stock": current_stock,
            "target_stock": target_stock,
            "fresh_stock": current_stock,
            "target_fresh_stock": target_stock,
            "active_total": active_total,
            "missing_count": max(0, target_stock - current_stock),
            "attempt_count": attempt_count + 1,
            "duplicate_streak": duplicate_streak,
            "strategy": previous_refill.get("strategy"),
            "strategy_rotated": False,
            "completed_steps": completed_steps,
            "elapsed_ms": elapsed_ms,
        }, 200


def finalize_gemini_generation_job(job_id, app_url=None):
    job = load_gemini_generation_job(job_id)
    if not job:
        return {"ok": False, "error": "gemini_job_not_found", "reason": "job_id not found"}, 404
    if job.get("status") == "failed":
        return {
            "ok": False,
            "error": "gemini_stage_failed",
            "stage": job.get("current_stage") or "finalize",
            "reason": job.get("error_message") or "job already failed",
        }, 200
    started = time.perf_counter()
    print(f"[gemini-stage] finalize job_id={job_id}")
    reserved = []
    try:
        planned_steps = gemini_planned_refill_step_keys(job)
        completed_steps = gemini_completed_step_keys(job)
        missing_steps = [step for step in planned_steps if step not in completed_steps]
        print(f"[gemini-finalize] completed_steps={completed_steps}")
        print(f"[gemini-finalize] missing_steps={missing_steps}")
        if missing_steps:
            return {
                "ok": False,
                "error": "gemini_bank_steps_incomplete",
                "stage": "finalize",
                "reason": "planned refill steps are not complete",
                "missing_steps": missing_steps,
            }, 200
        not_ready = gemini_bank_not_ready_pools(job)
        if not_ready:
            print(f"[gemini-finalize] not_ready={not_ready}")
            return {
                "ok": False,
                "error": "gemini_bank_not_ready",
                "stage": "finalize",
                "reason": "required word/verb pools have not reached target stock",
                "not_ready": not_ready,
            }, 200
        settings = gemini_job_settings(job)
        vocab_stage = gemini_job_json(job.get("vocab_json"), {})
        verbs_stage = gemini_job_json(job.get("verbs_json"), {})
        grammar_stage = gemini_job_json(job.get("grammar_json"), {})
        target_levels = settings_target_levels(settings)
        jlpt_levels = [level for level in target_levels if level in LEVELS] or [settings.get("target_level", "N5")]
        word_quota = build_level_quota(target_levels, int(settings.get("vocab_count") or 0), item_type="word")
        verb_quota = build_level_quota(jlpt_levels, int(settings.get("verb_count") or 0), item_type="verb")
        grammar_quota = dict(LOCAL_FIXED_GRAMMAR_QUOTA)
        print(f"[gemini-plan] target_levels={','.join(target_levels)}")
        print(f"[gemini-plan] word_quota={word_quota}")
        print(f"[gemini-plan] verb_quota={verb_quota}")
        print(f"[gemini-plan] grammar_quota={grammar_quota}")
        selected_words = select_gemini_bank_items("word", word_quota)
        selected_verbs = select_gemini_bank_items("verb", verb_quota)
        selected_grammar = select_gemini_bank_items("grammar", grammar_quota)
        reserved = [*(selected_words or []), *(selected_verbs or []), *(selected_grammar or [])]
        initial_counts = {
            "word": dict(Counter(row.get("jlpt_level", "") for row in selected_words if row.get("jlpt_level"))),
            "verb": dict(Counter(row.get("jlpt_level", "") for row in selected_verbs if row.get("jlpt_level"))),
            "grammar": dict(Counter(row.get("jlpt_level", "") for row in selected_grammar if row.get("jlpt_level"))),
        }
        print(f"[gemini-finalize] initial selected_counts_by_level={initial_counts}")
        requested_words = int(settings.get("vocab_count") or 0)
        requested_verbs = int(settings.get("verb_count") or 0)
        top_up = {
            "word": {"needed": max(0, requested_words - len(selected_words)), "added": 0},
            "verb": {"needed": max(0, requested_verbs - len(selected_verbs)), "added": 0},
            "grammar_warning": [],
        }
        if top_up["word"]["needed"] > 0:
            selected_keys = {row.get("normalized_key") for row in selected_words}
            extra_words = select_gemini_bank_top_up_items("word", top_up["word"]["needed"], selected_keys)
            selected_words.extend(extra_words)
            reserved.extend(extra_words)
            top_up["word"]["added"] = len(extra_words)
        if top_up["verb"]["needed"] > 0:
            selected_keys = {row.get("normalized_key") for row in selected_verbs}
            extra_verbs = select_gemini_bank_top_up_items("verb", top_up["verb"]["needed"], selected_keys)
            selected_verbs.extend(extra_verbs)
            reserved.extend(extra_verbs)
            top_up["verb"]["added"] = len(extra_verbs)
        if not selected_grammar:
            top_up["grammar_warning"].append("gemini_bank_grammar_empty")
            print("[gemini-finalize] grammar_warning=gemini_bank_grammar_empty")
        if len(selected_words) < requested_words:
            print(f"[gemini-finalize] insufficient item_type=word selected={len(selected_words)} requested={requested_words}")
            raise ValueError("gemini_bank_insufficient:word")
        if len(selected_verbs) < requested_verbs:
            print(f"[gemini-finalize] insufficient item_type=verb selected={len(selected_verbs)} requested={requested_verbs}")
            raise ValueError("gemini_bank_insufficient:verb")
        bank_refill = {
            "word": vocab_stage.get("refill", {}) if isinstance(vocab_stage, dict) else {},
            "verb": verbs_stage.get("refill", {}) if isinstance(verbs_stage, dict) else {},
            "grammar": grammar_stage.get("refill", {}) if isinstance(grammar_stage, dict) else {},
        }
        bank_warnings = []
        for stage_cache in (vocab_stage, verbs_stage, grammar_stage):
            if isinstance(stage_cache, dict) and isinstance(stage_cache.get("warnings"), list):
                bank_warnings.extend(stage_cache.get("warnings") or [])
        level_quota = {
            "word": word_quota,
            "verb": verb_quota,
            "grammar": grammar_quota,
        }
        material = build_gemini_bank_material(settings, selected_words, selected_verbs, selected_grammar, level_quota, bank_refill)
        actual_words = len(material.get("vocab") or [])
        actual_verbs = len(material.get("verbs") or [])
        count_validation = {
            "target_level_requested": settings.get("target_level", ""),
            "target_level_actual": material.get("level") or settings.get("target_level", ""),
            "word_count_requested": requested_words,
            "word_count_actual": actual_words,
            "verb_count_requested": requested_verbs,
            "verb_count_actual": actual_verbs,
            "word_count_matched": actual_words >= requested_words,
            "verb_count_matched": actual_verbs >= requested_verbs,
            "warnings": [],
        }
        material.setdefault("metadata", {}).update(
            {
                "generation_mode": "gemini_bank",
                "generation_source": "gemini_api",
                "ai_used": True,
                "fallback_used": False,
                "gemini_job_id": job_id,
                "resolved_settings": {
                    "generation_trigger": "manual",
                    "settings_source": "request_payload",
                    "target_level": settings.get("target_level", ""),
                    "target_levels": settings_target_levels(settings),
                    "word_count": requested_words,
                    "verb_count": requested_verbs,
                    "choice_count": int(settings.get("mcq_count") or 0),
                    "fill_count": int(settings.get("fill_count") or 0),
                    "grammar_level": "fixed_quota",
                    "grammar_count": LOCAL_FIXED_GRAMMAR_TOTAL,
                    "grammar_mode": "fixed_quota",
                    "grammar_quota": dict(LOCAL_FIXED_GRAMMAR_QUOTA),
                    "grammar_total_target": LOCAL_FIXED_GRAMMAR_TOTAL,
                },
                "count_validation": count_validation,
                "grammar_mode": "fixed_quota",
                "grammar_quota": dict(LOCAL_FIXED_GRAMMAR_QUOTA),
                "grammar_total_target": LOCAL_FIXED_GRAMMAR_TOTAL,
                "grammar_total_actual": len(material.get("grammar_points") or []),
                "top_up": top_up,
                "gemini_bank_warnings": bank_warnings,
            }
        )
        material["metadata"].setdefault("warnings", []).extend(top_up.get("grammar_warning") or [])
        if bank_warnings:
            material["metadata"].setdefault("warnings", []).extend(
                sorted({str(item.get("warning") or "gemini_bank_refill_warning") for item in bank_warnings if isinstance(item, dict)})
            )
        final_counts = material["metadata"].get("selected_counts_by_level", {})
        print(f"[gemini-finalize] final selected word={actual_words} verb={actual_verbs} grammar={len(material.get('grammar_points') or [])}")
        save_info = save_material_for_date(
            job.get("material_date") or get_today_taipei_date(),
            material,
            settings,
            generation_source="gemini_api",
            generation_mode="gemini_bank",
        )
        material["metadata"]["selection_log_warnings"] = save_info.get("selection_log_warnings", [])
        mark_gemini_bank_items_used(reserved)
        print(f"[gemini-bank] mark_used count={len(reserved)}")
        update_gemini_generation_job(job_id, status="completed", current_stage="finalized", error_message="")
        invalidate_dashboard_cache("gemini staged material generated")
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        print(f"[gemini-bank] finalize material_key={save_info.get('material_key')}")
        print(f"[gemini-stage] finalized job_id={job_id} material_key={save_info.get('material_key')} elapsed_ms={elapsed_ms}")
        return {
            "ok": True,
            "job_id": job_id,
            "status": "completed",
            "material_key": save_info.get("material_key"),
            "material_date": save_info.get("material_date"),
            "version_no": save_info.get("version_no"),
            "generation_mode": "gemini_bank",
            "generation_source": "gemini_api",
            "resolved_settings": material["metadata"].get("resolved_settings", {}),
            "count_validation": count_validation,
            "top_up": top_up,
            "warnings": material["metadata"].get("warnings", []),
            "gemini_bank_warnings": bank_warnings,
            "selection_log_warnings": save_info.get("selection_log_warnings", []),
            "elapsed_ms": elapsed_ms,
        }, 200
    except Exception as exc:
        release_gemini_bank_items(reserved)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        reason = classify_gemini_daily_material_error(exc)
        error_code = "gemini_bank_insufficient" if "gemini_bank_insufficient" in str(exc) or str(exc).startswith("insufficient_") else "gemini_stage_failed"
        message = f"{reason}:{str(exc)[:240]}"
        update_gemini_generation_job(job_id, status="failed", current_stage="finalize", error_message=message)
        print(f"[gemini-stage] failed job_id={job_id} stage=finalize error={message} elapsed_ms={elapsed_ms}")
        return {
            "ok": False,
            "error": error_code,
            "stage": "finalize",
            "reason": "bank cannot satisfy requested word/verb count after one top-up" if error_code == "gemini_bank_insufficient" else reason,
            "missing": [str(exc).split(":", 1)[1]] if error_code == "gemini_bank_insufficient" and ":" in str(exc) else [],
            "elapsed_ms": elapsed_ms,
        }, 200


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


def get_recent_used_normalized_keys(days=None, material_date=None, include_all_versions=True):
    try:
        if days is None:
            days = LOCAL_SELECTION_COOLDOWN_DAYS
        day_count = max(0, int(days or 0))
    except (TypeError, ValueError):
        day_count = LOCAL_SELECTION_COOLDOWN_DAYS
    if day_count <= 0:
        return set()
    try:
        end_date = datetime.strptime(canonical_material_date(material_date or get_today_taipei_date()), "%Y-%m-%d").date()
    except Exception:
        end_date = taipei_now().date()
    start_date = end_date - timedelta(days=day_count)
    start, end = start_date.isoformat(), end_date.isoformat()
    try:
        if DATABASE_URL and not vocab_pool_db_query_allowed():
            return set()
        ensure_vocab_rules_store()
        if DATABASE_URL:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT DISTINCT normalized_key
                        FROM vocab_selection_logs
                        WHERE material_date BETWEEN %s AND %s
                          AND (selected_for = 'word' OR selected_for IS NULL OR selected_for = '')
                          AND COALESCE(NULLIF(normalized_key, ''), '') <> ''
                        """,
                        (start, end),
                    )
                    return {normalize_vocab_key(row[0]) for row in cur.fetchall() if row and row[0]}
        rowset = sqlite_dicts(
            """
            SELECT DISTINCT normalized_key
            FROM vocab_selection_logs
            WHERE material_date BETWEEN ? AND ?
              AND (selected_for = 'word' OR selected_for IS NULL OR selected_for = '')
              AND COALESCE(NULLIF(normalized_key, ''), '') <> ''
            """,
            (start, end),
        )
        return {normalize_vocab_key(row.get("normalized_key")) for row in rowset if row.get("normalized_key")}
    except Exception as exc:
        print(f"[local-generate] recent duplicate lookup failed; days={days}; reason={exc}")
        mark_vocab_pool_db_unavailable(exc)
        return set()


def get_recent_used_verb_keys(material_date=None, days=None, include_all_versions=True):
    try:
        if days is None:
            days = LOCAL_SELECTION_COOLDOWN_DAYS
        day_count = max(0, int(days or 0))
    except (TypeError, ValueError):
        day_count = LOCAL_SELECTION_COOLDOWN_DAYS
    if day_count <= 0:
        return set()
    try:
        end_date = datetime.strptime(canonical_material_date(material_date or get_today_taipei_date()), "%Y-%m-%d").date()
    except Exception:
        end_date = taipei_now().date()
    start_date = end_date - timedelta(days=day_count)
    start, end = start_date.isoformat(), end_date.isoformat()
    try:
        if DATABASE_URL and not vocab_pool_db_query_allowed():
            return set()
        ensure_vocab_rules_store()
        if DATABASE_URL:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT DISTINCT normalized_key
                        FROM vocab_selection_logs
                        WHERE material_date BETWEEN %s AND %s
                          AND selected_for = 'verb'
                          AND COALESCE(NULLIF(normalized_key, ''), '') <> ''
                        """,
                        (start, end),
                    )
                    return {normalize_vocab_key(row[0]) for row in cur.fetchall() if row and row[0]}
        rowset = sqlite_dicts(
            """
            SELECT DISTINCT normalized_key
            FROM vocab_selection_logs
            WHERE material_date BETWEEN ? AND ?
              AND selected_for = 'verb'
              AND COALESCE(NULLIF(normalized_key, ''), '') <> ''
            """,
            (start, end),
        )
        return {normalize_vocab_key(row.get("normalized_key")) for row in rowset if row.get("normalized_key")}
    except Exception as exc:
        print(f"[verb-selector] recent verb lookup failed; days={days}; reason={exc}")
        mark_vocab_pool_db_unavailable(exc)
        return set()


def get_selection_usage_stats(selected_for):
    selected_for = str(selected_for or "").strip() or "word"
    stats = {}
    try:
        if DATABASE_URL and not vocab_pool_db_query_allowed():
            return stats
        ensure_vocab_rules_store()
        if DATABASE_URL:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT normalized_key,
                               COUNT(*) AS used_count,
                               MAX(COALESCE(created_at::TEXT, material_date::TEXT)) AS last_used_at
                        FROM vocab_selection_logs
                        WHERE selected_for = %s
                          AND COALESCE(NULLIF(normalized_key, ''), '') <> ''
                        GROUP BY normalized_key
                        """,
                        (selected_for,),
                    )
                    rows = cur.fetchall()
        else:
            rows = sqlite_dicts(
                """
                SELECT normalized_key,
                       COUNT(*) AS used_count,
                       MAX(COALESCE(created_at, material_date)) AS last_used_at
                FROM vocab_selection_logs
                WHERE selected_for = ?
                  AND COALESCE(NULLIF(normalized_key, ''), '') <> ''
                GROUP BY normalized_key
                """,
                (selected_for,),
            )
        for row in rows:
            if isinstance(row, dict):
                raw_key = row.get("normalized_key")
                used_count = row.get("used_count")
                last_used_at = row.get("last_used_at")
            else:
                raw_key, used_count, last_used_at = row
            key = normalize_vocab_key(raw_key)
            if not key:
                continue
            try:
                count_value = int(used_count or 0)
            except (TypeError, ValueError):
                count_value = 0
            stats[key] = {"used_count": count_value, "last_used_at": str(last_used_at or "")}
    except Exception as exc:
        print(f"[local-generate] selection usage lookup failed selected_for={selected_for}; reason={exc}")
        mark_vocab_pool_db_unavailable(exc)
    return stats


def rotation_item_key(item):
    key = item_normalized_key(item)
    if key:
        return key
    if isinstance(item, dict):
        return normalize_vocab_key(first_text(item, ["dictionary_form", "base", "surface", "word", "term"]))
    return ""


def rotation_usage_sort_key(item, usage_stats):
    key = rotation_item_key(item)
    usage = usage_stats.get(key, {}) if key else {}
    try:
        used_count = int(usage.get("used_count") or 0)
    except (TypeError, ValueError):
        used_count = 0
    last_used_at = str(usage.get("last_used_at") or "")
    return (used_count, 0 if not last_used_at else 1, last_used_at, random.random())


def sort_candidates_for_rotation(items, usage_stats):
    return sorted([item for item in items if item], key=lambda item: rotation_usage_sort_key(item, usage_stats))


def count_never_used_candidates(items, usage_stats):
    return sum(1 for item in items if not usage_stats.get(rotation_item_key(item), {}).get("used_count"))


def is_never_used_candidate(item, usage_stats):
    return not bool(usage_stats.get(rotation_item_key(item), {}).get("used_count"))


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
    if DATABASE_URL:
        if not vocab_pool_db_query_allowed():
            return []
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
                                 RANDOM()
                        LIMIT 1200
                        """
                    )
                    columns = [desc[0] for desc in cur.description]
                    return [dict(zip(columns, row)) for row in cur.fetchall()]
        except Exception as e:
            print(f"[local-generate] db_pool_fetch_failed db=postgres; reason={e}")
            mark_vocab_pool_db_unavailable(e)
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
                         RANDOM()
                LIMIT 1200
                """
            ).fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error as e:
        print(f"[local-generate] db_pool_fetch_failed db=sqlite; reason={e}")
        return []


VERB_POOL_POS_VALUES = {
    "verb",
    "verb_godan",
    "verb_ichidan",
    "godan",
    "ichidan",
    "suru_verb",
    "kuru_verb",
    "動詞",
    "動詞-自立",
    "五段動詞",
    "一段動詞",
    "不規則動詞",
}
VERB_POOL_POS_LIKE = ("動詞", "五段", "一段")
VERB_POOL_BLOCKED_CATEGORIES = {
    "business",
    "advanced",
    "generated_compound",
    "unknown",
    "named_entity",
    "sensitive",
    "typo_or_noise",
}
VERB_POOL_BLOCKED_SOURCES = {
    "synthetic",
    "seed_advanced",
    "seed_advanced_synthetic",
    "auto_generated",
    "generated",
}


def fetch_vocabulary_pool_verb_rows(limit=300):
    limit = max(1, min(int(limit or 300), 500))
    pos_values = ["verb", "動詞"]
    blocked_categories = sorted(VERB_POOL_BLOCKED_CATEGORIES)
    blocked_sources = sorted(VERB_POOL_BLOCKED_SOURCES)
    params = {}

    def bind(name, value):
        params[name] = value
        return f"%({name})s" if DATABASE_URL else f":{name}"

    def bind_list(prefix, values):
        return ", ".join(bind(f"{prefix}_{index}", value) for index, value in enumerate(values))

    where = [
        "COALESCE(is_active, TRUE) = TRUE" if DATABASE_URL else "COALESCE(is_active, 1) = 1",
        "COALESCE(NULLIF(meaning_zh, ''), '') <> ''",
        "COALESCE(NULLIF(reading_hiragana, ''), '') <> ''",
        "LOWER(COALESCE(quality, 'normal')) NOT IN ('rejected', 'experimental', 'low_quality')",
        f"LOWER(COALESCE(NULLIF(category, ''), 'general')) NOT IN ({bind_list('blocked_category', blocked_categories)})",
        f"LOWER(COALESCE(NULLIF(source, ''), 'manual')) NOT IN ({bind_list('blocked_source', blocked_sources)})",
        f"COALESCE(NULLIF(part_of_speech, ''), '') IN ({bind_list('pos', pos_values)})",
    ]
    sql = f"""
        SELECT *
        FROM vocabulary_pool
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(used_in_material_count, 0) ASC,
                 {'last_used_at ASC NULLS FIRST' if DATABASE_URL else "COALESCE(last_used_at, '') ASC"},
                 RANDOM()
        LIMIT {bind('limit', limit)}
    """
    try:
        if DATABASE_URL:
            if not vocab_pool_db_query_allowed():
                return []
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    columns = [desc[0] for desc in cur.description]
                    return [dict(zip(columns, row)) for row in cur.fetchall()]
        ensure_settings_store()
        with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        db_type = "postgres" if DATABASE_URL else "sqlite"
        print(f"[local-generate] db_verb_pool_fetch_failed db={db_type}; reason={e}")
        if DATABASE_URL:
            mark_vocab_pool_db_unavailable(e)
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
SNS_VOCAB_CATEGORIES = {"sns", "slang", "internet_slang", "otaku_culture", "approved_slang"}
CORE_VOCAB_SOURCES = {"seed_basic", "jlpt_seed", "manual", "starter_pack"}
SAFE_JLPT_MAIN_CATEGORIES = {"general", "common", "daily", "jlpt_core", "seed", ""}
UNSAFE_MAIN_POOL_CATEGORIES = {
    "business",
    "advanced",
    "internet_slang",
    "slang",
    "sns",
    "otaku_culture",
    "generated_compound",
    "unknown",
    "named_entity",
    "sensitive",
    "typo_or_noise",
}
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


# Extra hard filters for synthetic business compounds.  The older generated
# vocabulary seed contains many mechanically combined nouns; these should never
# become the main daily-learning pool even when their JLPT field is filled.
LOW_QUALITY_COMPOUND_SUFFIXES_JA = (
    "導入案",
    "導入策",
    "支援案",
    "推進力",
    "検討性",
    "評価化",
    "修正上",
    "標準化案",
    "解決上",
    "確認率",
    "整理力",
    "更新案",
    "検証性",
    "化案",
    "化力",
    "化率",
    "化性",
    "案",
    "力",
    "率",
    "性",
    "化",
    "上",
    "策",
    "目的",
    "標準化性",
    "可視化",
    "効率上",
    "強化策",
    "運用案",
    "構築力",
    "設計率",
    "提案性",
    "共有化",
    "報告上",
)
LOW_QUALITY_COMPOUND_PREFIXES_JA = (
    "開発",
    "条件",
    "品質",
    "販売",
    "顧客",
    "物流",
    "製品",
    "組織",
    "感情",
    "関係",
    "文化",
    "業務",
    "管理",
    "運用",
    "推進",
    "導入",
)
LOW_QUALITY_EXACT_WORDS_JA = {
    "開発支援案",
    "開発推進力",
    "開発導入案",
    "開発検討性",
    "開発評価化",
    "開発効率化",
    "条件修正上",
    "条件検証",
    "条件基準",
    "条件変更",
    "条件連携力",
    "開発標準化性",
    "開発可視化",
    "開発効率上",
    "開発強化策",
    "開発運用案",
    "開発構築力",
    "開発設計率",
    "開発提案性",
    "開発共有化",
    "開発報告上",
    "品質更新案",
    "販売導入策",
    "顧客確認率",
    "物流共有",
    "感情検証性",
}
LOW_QUALITY_MEANING_HINTS_ZH = ("方案", "能力", "策略", "驗證性", "更新方案")
SYNTHETIC_VOCAB_CATEGORIES = {"business", "advanced", "generated_compound", "unknown"}
SYNTHETIC_VOCAB_SOURCES = {"seed_advanced", "auto_generated", "synthetic", "seed_advanced_synthetic"}
CORE_SAFE_VOCAB_SOURCES = {"seed_basic", "jlpt_seed", "manual", "starter_pack"}
LOCAL_SAFE_MODE_JLPT_LEVELS = {"N5", "N4"}
LOCAL_SAFE_MODE_CATEGORIES = {"general", "common", "daily"}
LOCAL_SAFE_MODE_SOURCES = {"core", "basic", "manual", "seed_basic", "jlpt_core"}
LOCAL_SAFE_MODE_DISABLED_CATEGORIES = {
    "business",
    "advanced",
    "internet_slang",
    "slang",
    "otaku_culture",
    "generated_compound",
    "unknown",
    "named_entity",
    "sensitive",
    "typo_or_noise",
    "sns",
}
LOCAL_SAFE_MODE_DISABLED_SOURCES = {
    "auto_generated",
    "synthetic",
    "seed_advanced",
    "sns_capture",
    "grammar_analysis",
    "telegram",
    "generated",
    "import_generated",
    "seed_advanced_synthetic",
}


def local_generation_safe_mode_enabled():
    value = os.environ.get("LOCAL_GENERATION_SAFE_MODE", LOCAL_GENERATION_SAFE_MODE_DEFAULT)
    return str(value).strip().lower() not in {"0", "false", "off", "no"}


def safe_mode_level_order(target_level):
    return ["N4", "N5"] if target_level == "N4" else ["N5", "N4"]


def is_safe_mode_seed_vocab_item(item):
    level = first_text(item, ["jlpt_level"]).upper()
    category = first_text(item, ["category"]).lower() or "general"
    source = first_text(item, ["source"]).lower() or "seed_basic"
    quality = first_text(item, ["quality"]).lower() or "core"
    return (
        level in LOCAL_SAFE_MODE_JLPT_LEVELS
        and category in LOCAL_SAFE_MODE_CATEGORIES
        and source in LOCAL_SAFE_MODE_SOURCES
        and quality not in {"experimental", "rejected", "low_quality"}
        and bool(first_text(item, ["surface", "word", "base_form"]))
        and bool(first_text(item, ["reading_hiragana", "reading"]))
        and bool(first_text(item, ["meaning_zh", "meaning"]))
    )


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
    kanji_count = len(re.findall(r"[\u4e00-\u9fff]", surface or ""))
    is_core_safe = (
        source in CORE_SAFE_VOCAB_SOURCES
        or category in GENERAL_VOCAB_CATEGORIES
        or quality == "core"
    )
    if quality == "rejected":
        return True, "quality_rejected"
    if quality == "experimental":
        return True, "quality_experimental"
    if source in {"auto_generated", "seed_advanced_synthetic"}:
        return True, "synthetic_source"
    if surface in LOW_QUALITY_EXACT_WORDS_JA:
        return True, "mechanical_exact"
    if not is_core_safe and any(surface.endswith(suffix) for suffix in LOW_QUALITY_COMPOUND_SUFFIXES_JA):
        return True, "mechanical_suffix"
    if kanji_count >= 4 and any(surface.startswith(prefix) for prefix in LOW_QUALITY_COMPOUND_PREFIXES_JA) and any(surface.endswith(suffix) for suffix in LOW_QUALITY_COMPOUND_SUFFIXES_JA):
        return True, "mechanical_prefix"
    if (
        not is_core_safe
        and category in SYNTHETIC_VOCAB_CATEGORIES
        and source in SYNTHETIC_VOCAB_SOURCES
        and kanji_count >= 4
    ):
        return True, "synthetic_compound"
    if category not in BUSINESS_VOCAB_CATEGORIES | ADVANCED_VOCAB_CATEGORIES and source != "seed_advanced":
        return False, ""
    if any(surface.endswith(suffix) for suffix in LOW_QUALITY_COMPOUND_SUFFIXES):
        return True, "mechanical_suffix"
    if kanji_count >= 6:
        return True, "compound_too_long"
    if any(hint in meaning for hint in LOW_QUALITY_MEANING_HINTS) or any(hint in meaning for hint in LOW_QUALITY_MEANING_HINTS_ZH):
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
    }


def matching_vocab_rules(item, rule_context):
    rules = (rule_context or {}).get("rules", {})
    matches = []
    for source_type, value in item_rule_values(item).items():
        rule = rules.get(make_vocab_rule_key(source_type, value))
        if rule:
            matches.append(rule)
    return matches


def is_target_jlpt_pool(item, target_level):
    target = clean_rule_match_value(target_level)
    if not target or target == EMPTY_RULE_VALUE:
        return False
    item_level = clean_rule_match_value(first_text(item, ["jlpt_level", "target_level", "level"]))
    return item_level == target


def can_select_vocab_by_rules(item, rule_context, selected_rule_counts, target_level=None, ignore_empty_jlpt_rule=False):
    matches = matching_vocab_rules(item, rule_context)
    if item_rule_values(item).get("quality") == "rejected":
        return False, "quality_rejected", matches
    target_jlpt_pool = is_target_jlpt_pool(item, target_level)
    for rule in matches:
        rule_key = rule["rule_key"]
        strict = boolish(rule.get("strict_mode"))
        enabled = boolish(rule.get("enabled"))
        quota = int(rule.get("quota_count", 0) or 0)
        used = int(rule.get("used_count", 0) or 0)
        selected_count = int(selected_rule_counts.get(rule_key, 0) or 0)
        max_per_material = rule.get("max_per_material")
        source_type = rule.get("source_type") or rule.get("group_key")
        match_value = clean_rule_match_value(rule.get("match_value"))
        if source_type == "jlpt_level" and ignore_empty_jlpt_rule and match_value == EMPTY_RULE_VALUE:
            continue
        if source_type == "jlpt_level" and target_jlpt_pool:
            # The requested target JLPT level is the primary pool. Its own
            # period quota should not empty the main learning set; category
            # quotas, quality filters, cooldown, and duplicates still apply.
            continue
        if not enabled and strict:
            return False, f"disabled_strict:{rule_key}", matches
        hard_quota = source_type in {"jlpt_level", "category"} or strict
        if enabled and hard_quota and quota > 0 and used + selected_count >= quota:
            return False, f"period_quota_reached:{rule_key}", matches
        if enabled and strict and quota == 0:
            return False, f"zero_quota_strict:{rule_key}", matches
        if hard_quota and max_per_material not in (None, "") and selected_count >= int(max_per_material or 0):
            return False, f"material_quota_reached:{rule_key}", matches
    return True, "", matches


def vocab_rule_priority(item, rule_context):
    matches = matching_vocab_rules(item, rule_context)
    enabled_priorities = [int(rule.get("priority", 50) or 50) for rule in matches if boolish(rule.get("enabled"))]
    return max(enabled_priorities) if enabled_priorities else 50


def record_vocab_selection_logs(items, selected_for="word", material_date=None, material_key=None, material_version_no=None):
    rows = []
    material_date = canonical_material_date(material_date or get_today_taipei_date())
    now = clean_timestamp(utc_now_iso()) or datetime.now(timezone.utc).isoformat(timespec="seconds")
    for item in items:
        rule_keys = list(item.get("_matched_rule_keys") or [])
        if item.get("rule_key"):
            rule_keys.append(str(item.get("rule_key")))
        if not rule_keys:
            values = item_rule_values(item)
            for source_type in ("jlpt_level", "category"):
                value = values.get(source_type)
                if value:
                    rule_keys.append(make_vocab_rule_key(source_type, value))
        if not rule_keys:
            continue
        values = item_rule_values(item)
        for rule_key in sorted(set(rule_keys)):
            source_type, match_value = rule_key.split(":", 1) if ":" in rule_key else ("custom", rule_key)
            rows.append(
                {
                    "material_date": material_date,
                    "vocabulary_id": item.get("_pool_id"),
                    "surface": first_text(item, ["word", "surface", "dictionary_form", "base_form", "term"]),
                    "base_form": first_text(item, ["base_form", "dictionary_form", "surface", "word", "term"]),
                    "normalized_key": item_normalized_key(item),
                    "rule_key": rule_key,
                    "group_key": source_type,
                    "source_type": source_type,
                    "match_value": match_value,
                    "category": values.get("category", EMPTY_RULE_VALUE),
                    "jlpt_level": values.get("jlpt_level", EMPTY_RULE_VALUE),
                    "source": clean_rule_match_value(first_text(item, ["_raw_source", "source"])),
                    "quality": clean_rule_match_value(first_text(item, ["quality"])),
                    "part_of_speech": clean_rule_match_value(first_text(item, ["part_of_speech", "pos"])),
                    "selected_for": selected_for,
                    "material_key": material_key or "",
                    "material_version_no": material_version_no,
                    "created_at": clean_timestamp(now) or datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
                        quality, part_of_speech, selected_for, material_key, material_version_no, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                            row["material_key"],
                            row["material_version_no"],
                            clean_timestamp(row["created_at"]) or datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
                quality, part_of_speech, selected_for, material_key, material_version_no, created_at
            )
            VALUES (:material_date, :vocabulary_id, :surface, :base_form, :normalized_key, :rule_key,
                :group_key, :source_type, :match_value, :category, :jlpt_level, :source,
                :quality, :part_of_speech, :selected_for, :material_key, :material_version_no, :created_at)
            """,
            rows,
        )
        conn.commit()


def _record_vocab_selection_logs_safely(
    items,
    selected_for="word",
    material_date=None,
    material_key=None,
    material_version_no=None,
    warning="vocab_selection_log_failed",
):
    try:
        record_vocab_selection_logs(
            items,
            selected_for=selected_for,
            material_date=material_date,
            material_key=material_key,
            material_version_no=material_version_no,
        )
        return ""
    except BaseException as exc:
        print(
            "[vocab-selector] selection log best-effort failed "
            f"selected_for={selected_for} material_key={material_key}; reason={exc}"
        )
        try:
            print(traceback.format_exc())
        except Exception:
            pass
        return warning


def best_effort_record_vocab_selection_logs(
    items,
    selected_for="word",
    material_date=None,
    material_key=None,
    material_version_no=None,
):
    if not items:
        return ""
    warning = f"{selected_for}_selection_log_failed" if selected_for else "vocab_selection_log_failed"
    if DATABASE_URL:
        result = {"warning": ""}

        def worker():
            result["warning"] = _record_vocab_selection_logs_safely(
                items,
                selected_for=selected_for,
                material_date=material_date,
                material_key=material_key,
                material_version_no=material_version_no,
                warning=warning,
            )

        thread = threading.Thread(
            target=worker,
            name=f"{selected_for or 'vocab'}-selection-log-{material_key or 'unknown'}",
            daemon=True,
        )
        thread.start()
        thread.join(0.05)
        if thread.is_alive():
            print(
                "[vocab-selector] selection log deferred "
                f"selected_for={selected_for} material_key={material_key}"
            )
            return f"{selected_for}_selection_log_deferred" if selected_for else "vocab_selection_log_deferred"
        return result["warning"]
    return _record_vocab_selection_logs_safely(
        items,
        selected_for=selected_for,
        material_date=material_date,
        material_key=material_key,
        material_version_no=material_version_no,
        warning=warning,
    )


def sql_placeholders(count):
    return ", ".join(["%s" if DATABASE_URL else "?"] * count)


def fetch_vocabulary_pool_candidates(
    jlpt_levels=None,
    categories=None,
    limit=300,
    safe_jlpt_pool=False,
    safe_mode=False,
    exclude_low_quality=True,
    exclude_normalized_keys=None,
):
    jlpt_levels = [level for level in (jlpt_levels or []) if level and level != EMPTY_RULE_VALUE]
    categories = [category for category in (categories or []) if category and category != EMPTY_RULE_VALUE]
    limit = max(1, min(int(limit or 300), 1000))
    where = [
        "COALESCE(is_active, TRUE) = TRUE" if DATABASE_URL else "COALESCE(is_active, 1) = 1",
        "COALESCE(NULLIF(meaning_zh, ''), '') <> ''",
        "COALESCE(NULLIF(reading_hiragana, ''), '') <> ''",
        "LOWER(COALESCE(quality, 'normal')) NOT IN ('rejected', 'experimental', 'low_quality')",
        "COALESCE(category, 'general') NOT IN ('named_entity', 'sensitive', 'typo_or_noise')",
    ]
    params = []
    surface_expr = "COALESCE(surface, base_form, '')"
    normalized_expr = "COALESCE(NULLIF(normalized_key, ''), NULLIF(base_form, ''), NULLIF(surface, ''))"
    if jlpt_levels:
        where.append(f"COALESCE(NULLIF(jlpt_level, ''), '{EMPTY_RULE_VALUE}') IN ({sql_placeholders(len(jlpt_levels))})")
        params.extend(jlpt_levels)
    if categories:
        where.append(f"COALESCE(NULLIF(category, ''), '{EMPTY_RULE_VALUE}') IN ({sql_placeholders(len(categories))})")
        params.extend(categories)
    if safe_jlpt_pool:
        safe_categories = sorted(SAFE_JLPT_MAIN_CATEGORIES - {""})
        where.append(
            "("
            f"LOWER(COALESCE(NULLIF(category, ''), 'general')) IN ({sql_placeholders(len(safe_categories))}) "
            "OR COALESCE(NULLIF(category, ''), '') = ''"
            ")"
        )
        params.extend(safe_categories)
        blocked_sources = ["seed_advanced", "auto_generated", "synthetic", "seed_advanced_synthetic"]
        where.append(
            "("
            "COALESCE(NULLIF(source, ''), '') = '' "
            f"OR LOWER(COALESCE(source, '')) NOT IN ({sql_placeholders(len(blocked_sources))})"
            ")"
        )
        params.extend(blocked_sources)
    if safe_mode:
        safe_levels = sorted(LOCAL_SAFE_MODE_JLPT_LEVELS)
        safe_categories = sorted(LOCAL_SAFE_MODE_CATEGORIES)
        safe_sources = sorted(LOCAL_SAFE_MODE_SOURCES)
        blocked_categories = sorted(LOCAL_SAFE_MODE_DISABLED_CATEGORIES)
        blocked_sources = sorted(LOCAL_SAFE_MODE_DISABLED_SOURCES)
        where.append(f"COALESCE(NULLIF(jlpt_level, ''), '{EMPTY_RULE_VALUE}') IN ({sql_placeholders(len(safe_levels))})")
        params.extend(safe_levels)
        where.append(f"LOWER(COALESCE(NULLIF(category, ''), 'general')) IN ({sql_placeholders(len(safe_categories))})")
        params.extend(safe_categories)
        where.append(f"LOWER(COALESCE(NULLIF(source, ''), 'seed_basic')) IN ({sql_placeholders(len(safe_sources))})")
        params.extend(safe_sources)
        where.append(f"LOWER(COALESCE(NULLIF(category, ''), 'general')) NOT IN ({sql_placeholders(len(blocked_categories))})")
        params.extend(blocked_categories)
        where.append(f"LOWER(COALESCE(NULLIF(source, ''), 'seed_basic')) NOT IN ({sql_placeholders(len(blocked_sources))})")
        params.extend(blocked_sources)
    if exclude_low_quality:
        if LOW_QUALITY_EXACT_WORDS_JA:
            exact_words = sorted(LOW_QUALITY_EXACT_WORDS_JA)
            where.append(f"{surface_expr} NOT IN ({sql_placeholders(len(exact_words))})")
            params.extend(exact_words)
        prefix_clauses = [f"{surface_expr} LIKE ?" for _ in LOW_QUALITY_COMPOUND_PREFIXES_JA]
        suffix_clauses = [f"{surface_expr} LIKE ?" for _ in LOW_QUALITY_COMPOUND_SUFFIXES_JA]
        if DATABASE_URL:
            prefix_clauses = [clause.replace("?", "%s") for clause in prefix_clauses]
            suffix_clauses = [clause.replace("?", "%s") for clause in suffix_clauses]
        where.append(f"NOT (({' OR '.join(prefix_clauses)}) AND ({' OR '.join(suffix_clauses)}))")
        params.extend([f"{prefix}%" for prefix in LOW_QUALITY_COMPOUND_PREFIXES_JA])
        params.extend([f"%{suffix}" for suffix in LOW_QUALITY_COMPOUND_SUFFIXES_JA])
    exclude_keys = sorted({normalize_vocab_key(key) for key in (exclude_normalized_keys or set()) if normalize_vocab_key(key)})
    if exclude_keys:
        exclude_keys = exclude_keys[:500]
        where.append(f"{normalized_expr} NOT IN ({sql_placeholders(len(exclude_keys))})")
        params.extend(exclude_keys)
    sql = f"""
        SELECT *
        FROM vocabulary_pool
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(used_in_material_count, 0) ASC,
                 {'last_used_at ASC NULLS FIRST' if DATABASE_URL else "COALESCE(last_used_at, '') ASC"},
                 RANDOM()
        LIMIT {'%s' if DATABASE_URL else '?'}
    """
    params.append(limit)
    try:
        if DATABASE_URL:
            if not vocab_pool_db_query_allowed():
                return []
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    columns = [desc[0] for desc in cur.description]
                    return [dict(zip(columns, row)) for row in cur.fetchall()]
        ensure_settings_store()
        with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        db_type = "postgres" if DATABASE_URL else "sqlite"
        print(f"[local-generate] db_pool_fetch_failed db={db_type}; reason={e}")
        mark_vocab_pool_db_unavailable(e)
        return []


def get_safe_jlpt_candidates(target_level, limit, cooldown_days=None, exclude_normalized_keys=None):
    return fetch_vocabulary_pool_candidates(
        jlpt_levels=[target_level],
        limit=limit,
        safe_jlpt_pool=True,
        safe_mode=local_generation_safe_mode_enabled(),
        exclude_low_quality=True,
        exclude_normalized_keys=exclude_normalized_keys or set(),
    )


def build_vocab_item_from_pool_row(row):
    word = first_text(row, ["term", "surface", "word", "vocab_word", "dictionary_form"])
    normalized_key = normalize_vocab_key(first_text(row, ["normalized_key", "normalized_term", "base_form", "surface", "term", "word"]) or word)
    if not word or not normalized_key:
        return None
    source = first_text(row, ["source"]) or "vocabulary_pool"
    quality = vocab_quality(row)
    return {
        "word": word,
        "base_form": first_text(row, ["base_form", "surface", "term", "word"]) or word,
        "reading": first_text(row, ["reading_hiragana", "reading", "kana", "vocab_reading"]),
        "meaning": first_text(row, ["meaning_zh", "meaning_zh_tw", "meaning", "vocab_meaning"]),
        "part_of_speech": first_text(row, ["part_of_speech", "pos"]),
        "jlpt_level": first_text(row, ["jlpt_level", "target_level", "level"]),
        "category": first_text(row, ["category"]) or "general",
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
        "_row": row,
    }


def category_rule_available(rule, selected_rule_counts):
    if not rule or not boolish(rule.get("enabled")):
        return False
    quota = int(rule.get("quota_count", 0) or 0)
    used = int(rule.get("used_count", 0) or 0)
    selected = int(selected_rule_counts.get(rule["rule_key"], 0) or 0)
    if quota > 0 and used + selected >= quota:
        return False
    if boolish(rule.get("strict_mode")) and quota == 0:
        return False
    max_per_material = rule.get("max_per_material")
    if max_per_material not in (None, "") and selected >= int(max_per_material or 0):
        return False
    return True


def approved_slang_vocab_items_for_rule(limit):
    try:
        items = material_vocab_from_approved_slang(limit)
    except Exception as exc:
        print(f"[local-generate] approved_slang_fetch_failed reason={exc}")
        items = []
    for item in items:
        item["rule_key"] = "category:SNS"
        item["_matched_rule_keys"] = ["category:SNS"]
    return items


def allocate_vocab_slots(settings, available_rules, word_count):
    try:
        total_slots = max(0, int(word_count or 0))
    except (TypeError, ValueError):
        total_slots = 0
    if total_slots <= 0:
        return {}
    ordered_rules = [
        rule for rule in sorted(
            available_rules,
            key=lambda item: (-int(item.get("priority", 0) or 0), SIX_MAIN_VOCAB_RULE_ORDER.index(item["rule_key"])),
        )
        if boolish(rule.get("enabled")) and int(rule.get("max_per_material", 0) or 0) > 0
    ]
    caps = {
        rule["rule_key"]: min(int(rule.get("max_per_material", 0) or 0), total_slots)
        for rule in ordered_rules
    }
    slots = {rule["rule_key"]: 0 for rule in ordered_rules}
    remaining = total_slots

    for rule in ordered_rules:
        rule_key = rule["rule_key"]
        if remaining <= 0:
            break
        if caps.get(rule_key, 0) <= 0:
            continue
        slots[rule_key] += 1
        remaining -= 1

    while remaining > 0:
        progressed = False
        for rule in ordered_rules:
            rule_key = rule["rule_key"]
            if remaining <= 0:
                break
            if slots.get(rule_key, 0) >= caps.get(rule_key, 0):
                continue
            slots[rule_key] += 1
            remaining -= 1
            progressed = True
        if not progressed:
            break
    return {key: count for key, count in slots.items() if count > 0}


def material_vocab_from_six_main_rules(
    settings,
    limit,
    exclude_keys=None,
    return_stats=False,
    material_date=None,
    recent_keys_by_days=None,
    word_usage_stats=None,
):
    started = time.perf_counter()
    material_date = canonical_material_date(material_date or get_today_taipei_date())
    stats = {
        "selection_strategy": "rotation_until_exhausted",
        "run_migrations_on_request": RUN_MIGRATIONS_ON_REQUEST,
        "db_pool_used": False,
        "seed_fallback_used": False,
        "selected_from_db_count": 0,
        "selected_from_seed_fallback_count": 0,
        "selected_by_jlpt_count": 0,
        "selected_target_jlpt_count": 0,
        "selected_adjacent_jlpt_count": 0,
        "selected_by_category_count": 0,
        "target_jlpt_quota_skipped": True,
        "rejected_low_quality_count": 0,
        "rejected_by_rule_count": 0,
        "rejected_by_quota_count": 0,
        "category_counts": {},
        "candidate_counts": {},
        "selected_rule_counts": {},
        "rule_remaining_after_generation": {},
        "rejected_recent_duplicate_count": 0,
        "rejected_by_category_quota": {},
        "slot_allocation": {},
        "prefiltered_low_quality_compound_count": 0,
        "prefiltered_unsupported_category_count": 0,
        "skipped_empty_jlpt_count": 0,
        "safe_jlpt_candidates": 0,
        "recent_used_word_count": 0,
        "pool_total_count": 0,
        "eligible_pool_count": 0,
        "never_used_candidates": 0,
        "selected_from_never_used_count": 0,
        "selected_from_oldest_used_count": 0,
        "repeated_within_14_days_count": 0,
        "cooldown_days_used": LOCAL_SELECTION_COOLDOWN_DAYS,
        "generation_elapsed_ms": 0,
        "rule_selection": {
            "available_rules": [],
            "blocked_by_period": [],
            "selected_counts": {},
        },
    }
    if limit <= 0:
        return ([], stats) if return_stats else []

    allowed_rule_keys = target_level_rule_keys(settings)
    rules = [
        rule for rule in load_six_main_vocab_rules()
        if boolish(rule.get("enabled")) and rule.get("rule_key") in allowed_rule_keys
    ]
    available_rules = []
    for rule in rules:
        rule_key = rule["rule_key"]
        if is_rule_period_available(rule_key, rule.get("period", "daily"), material_date):
            available_rules.append(rule)
            stats["rule_selection"]["available_rules"].append(rule_key)
        else:
            stats["rule_selection"]["blocked_by_period"].append(rule_key)
    available_rules.sort(key=lambda rule: (-int(rule.get("priority", 0) or 0), SIX_MAIN_VOCAB_RULE_ORDER.index(rule["rule_key"])))
    print(
        "[word-distribution] "
        f"target_levels={','.join(settings_target_levels(settings))} "
        f"enabled_rules={','.join(rule['rule_key'] for rule in available_rules)}"
    )
    planned_slots = allocate_vocab_slots(settings, available_rules, limit)
    stats["slot_allocation"] = dict(planned_slots)

    selected = []
    selected_keys = {normalize_vocab_key(key) for key in (exclude_keys or set()) if normalize_vocab_key(key)}
    cooldown_sequence = local_selection_cooldown_sequence()
    if recent_keys_by_days is None:
        recent_keys_by_days = {
            days: get_recent_used_normalized_keys(days, material_date=material_date)
            for days in cooldown_sequence
            if days > 0
        }
    if word_usage_stats is None:
        word_usage_stats = get_selection_usage_stats("word")
    stats["recent_used_word_count"] = len(recent_keys_by_days.get(LOCAL_SELECTION_COOLDOWN_DAYS, set()))
    sns_slang_ids = []

    def allocation_elapsed_ms():
        return round((time.perf_counter() - started) * 1000)

    def allocation_soft_timeout():
        return (time.perf_counter() - started) >= 2.0

    def note_allocation_soft_timeout():
        stats["soft_timeout"] = True
        stats.setdefault("warnings", [])
        if "vocab_allocation_soft_timeout_used_seed_fallback" not in stats["warnings"]:
            stats["warnings"].append("vocab_allocation_soft_timeout_used_seed_fallback")

    def select_item(item, rule_key):
        key = item_normalized_key(item)
        if not key or key in selected_keys:
            return False
        selected_keys.add(key)
        item["normalized_key"] = key
        item["rule_key"] = rule_key
        item["_matched_rule_keys"] = [rule_key]
        selected.append(item)
        if is_never_used_candidate(item, word_usage_stats):
            stats["selected_from_never_used_count"] += 1
        else:
            stats["selected_from_oldest_used_count"] += 1
        stats["selected_rule_counts"][rule_key] = stats["selected_rule_counts"].get(rule_key, 0) + 1
        stats["rule_selection"]["selected_counts"][rule_key] = stats["rule_selection"]["selected_counts"].get(rule_key, 0) + 1
        if rule_key.startswith("jlpt:"):
            stats["selected_by_jlpt_count"] += 1
            if item.get("jlpt_level") in [level for level in settings_target_levels(settings) if level in LEVELS]:
                stats["selected_target_jlpt_count"] += 1
            else:
                stats["selected_adjacent_jlpt_count"] += 1
        else:
            stats["selected_by_category_count"] += 1
            if item.get("_slang_id"):
                sns_slang_ids.append({"id": item.get("_slang_id")})
        return True

    def rows_for_rule(rule, needed, cooldown_days):
        if allocation_soft_timeout():
            note_allocation_soft_timeout()
            return []
        rule_key = rule["rule_key"]
        exclude_for_sql = set(selected_keys)
        if cooldown_days > 0:
            exclude_for_sql.update(recent_keys_by_days.get(cooldown_days, set()))
        query_limit = max(needed * 8, 60)
        if rule_key.startswith("jlpt:"):
            level = rule_key.split(":", 1)[1]
            rows = fetch_vocabulary_pool_candidates(
                jlpt_levels=[level],
                limit=query_limit,
                safe_jlpt_pool=True,
                safe_mode=False,
                exclude_low_quality=True,
                exclude_normalized_keys=exclude_for_sql,
            )
            stats["safe_jlpt_candidates"] += len(rows)
            if rows:
                stats["db_pool_used"] = True
            items = [item for item in (build_vocab_item_from_pool_row(row) for row in rows) if item]
            stats["pool_total_count"] += len(rows)
            stats["eligible_pool_count"] += len(items)
            stats["never_used_candidates"] += count_never_used_candidates(items, word_usage_stats)
            return sort_candidates_for_rotation(items, word_usage_stats)
        if rule_key == "category:SNS":
            vocab_rows = fetch_vocabulary_pool_candidates(
                categories=sorted(SNS_RULE_CATEGORIES),
                limit=query_limit,
                safe_jlpt_pool=False,
                safe_mode=False,
                exclude_low_quality=True,
                exclude_normalized_keys=exclude_for_sql,
            )
            if vocab_rows:
                stats["db_pool_used"] = True
            vocab_items = [item for item in (build_vocab_item_from_pool_row(row) for row in vocab_rows) if item]
            slang_items = approved_slang_vocab_items_for_rule(query_limit)
            items = vocab_items + slang_items
            stats["pool_total_count"] += len(vocab_rows) + len(slang_items)
            stats["eligible_pool_count"] += len(items)
            stats["never_used_candidates"] += count_never_used_candidates(items, word_usage_stats)
            return sort_candidates_for_rotation(items, word_usage_stats)
        return []

    def select_with_slot_limits(slot_limits):
        for cooldown_days in cooldown_sequence:
            if len(selected) >= limit:
                break
            selected_before_cooldown = len(selected)
            for rule in available_rules:
                if allocation_soft_timeout():
                    note_allocation_soft_timeout()
                    break
                if len(selected) >= limit:
                    break
                rule_key = rule["rule_key"]
                max_per_material = clamp_int(rule.get("max_per_material", 0), 0, 0)
                slot_limit = min(int(slot_limits.get(rule_key, 0) or 0), max_per_material)
                already_selected = stats["selected_rule_counts"].get(rule_key, 0)
                remaining_for_rule = max(0, slot_limit - already_selected)
                if remaining_for_rule <= 0:
                    continue
                needed = min(limit - len(selected), remaining_for_rule)
                for item in rows_for_rule(rule, needed, cooldown_days):
                    if len(selected) >= limit or stats["selected_rule_counts"].get(rule_key, 0) >= slot_limit:
                        break
                    if not item:
                        continue
                    key = item_normalized_key(item)
                    if not key or key in selected_keys:
                        stats["rejected_recent_duplicate_count"] += 1
                        continue
                    if cooldown_days > 0 and key in recent_keys_by_days.get(cooldown_days, set()):
                        stats["rejected_recent_duplicate_count"] += 1
                        continue
                    low_quality, _ = is_low_quality_compound_word(item)
                    if low_quality:
                        stats["rejected_low_quality_count"] += 1
                        continue
                    select_item(item, rule_key)
            if len(selected) > selected_before_cooldown:
                stats["cooldown_days_used"] = cooldown_days
            if allocation_soft_timeout():
                break

    select_with_slot_limits(planned_slots)
    if len(selected) < limit and not stats.get("soft_timeout"):
        refill_limits = {
            rule["rule_key"]: min(clamp_int(rule.get("max_per_material", 0), 0, 0), limit)
            for rule in available_rules
            if clamp_int(rule.get("max_per_material", 0), 0, 0) > 0
        }
        select_with_slot_limits(refill_limits)

    selected = selected[:limit]
    insufficient_unique = len(selected) < limit
    if insufficient_unique:
        stats.setdefault("warnings", []).append("insufficient_unique_words_after_7_day_cooldown")
    mark_vocabulary_pool_used([item for item in selected if item.get("source") == "vocabulary_pool"])
    mark_slang_used_in_material(sns_slang_ids)
    for item in selected:
        group = vocab_category_group(item)
        stats["category_counts"][group] = stats["category_counts"].get(group, 0) + 1
        item.pop("_row", None)
        item.pop("_slang_id", None)
        item.pop("_pool_id", None)
        item.pop("_pool_has_last_seen", None)
        item.pop("_pool_has_last_used", None)
        item.pop("_pool_has_used_count", None)
        item.pop("_raw_source", None)
        item.pop("_matched_rule_keys", None)
    stats["selected_from_db_count"] = len(selected)
    stats["generation_elapsed_ms"] = allocation_elapsed_ms()
    if stats["generation_elapsed_ms"] > 2000:
        stats.setdefault("warnings", [])
        if "vocab_allocation_elapsed_over_2000ms" not in stats["warnings"]:
            stats["warnings"].append("vocab_allocation_elapsed_over_2000ms")
    print(
        f"[vocab-allocation] requested={limit} "
        f"available_rules={stats['rule_selection']['available_rules']} "
        f"blocked_by_period={stats['rule_selection']['blocked_by_period']} "
        f"planned_slots={stats['slot_allocation']} "
        f"selected_counts={stats['rule_selection']['selected_counts']} "
        f"cooldown_days_used={stats['cooldown_days_used']} "
        f"fallback_count=0 elapsed_ms={stats['generation_elapsed_ms']}"
    )
    print(
        "[vocab-selector] "
        f"cooldown_days={LOCAL_SELECTION_COOLDOWN_DAYS} "
        f"recent_used_word_count={len(recent_keys_by_days.get(LOCAL_SELECTION_COOLDOWN_DAYS, set()))} "
        f"selected_word_count={len(selected)} "
        f"insufficient_unique={str(insufficient_unique).lower()}"
    )
    print(
        "[word-selector] strategy=rotation_until_exhausted "
        f"requested={limit} pool_total={stats['pool_total_count']} "
        f"eligible_pool={stats['eligible_pool_count']} "
        f"recent_7_days_used={stats['recent_used_word_count']} "
        f"never_used_candidates={stats['never_used_candidates']} "
        f"selected_from_never_used={stats['selected_from_never_used_count']} "
        f"selected_from_oldest_used={stats['selected_from_oldest_used_count']} "
        f"final_selected={len(selected)}"
    )
    return (selected, stats) if return_stats else selected


def material_vocab_from_vocabulary_pool(
    settings,
    limit,
    exclude_keys=None,
    return_stats=False,
    material_date=None,
    recent_keys_by_days=None,
    word_usage_stats=None,
):
    return material_vocab_from_six_main_rules(
        settings,
        limit,
        exclude_keys=exclude_keys,
        return_stats=return_stats,
        material_date=material_date,
        recent_keys_by_days=recent_keys_by_days,
        word_usage_stats=word_usage_stats,
    )
    started = time.perf_counter()
    stats = {
        "selection_strategy": "safe_jlpt_basic_only" if local_generation_safe_mode_enabled() else "safe_jlpt_prefilter_first",
        "local_generation_safe_mode": local_generation_safe_mode_enabled(),
        "allowed_jlpt_levels": sorted(LOCAL_SAFE_MODE_JLPT_LEVELS),
        "allowed_categories": sorted(LOCAL_SAFE_MODE_CATEGORIES),
        "disabled_sources": sorted(LOCAL_SAFE_MODE_DISABLED_SOURCES),
        "selected_from_db_count": 0,
        "selected_from_seed_fallback_count": 0,
        "selected_by_jlpt_count": 0,
        "selected_target_jlpt_count": 0,
        "selected_adjacent_jlpt_count": 0,
        "selected_by_category_count": 0,
        "target_jlpt_quota_skipped": False,
        "rejected_low_quality_count": 0,
        "rejected_by_rule_count": 0,
        "rejected_by_quota_count": 0,
        "category_counts": {},
        "candidate_counts": {},
        "selected_rule_counts": {},
        "rule_remaining_after_generation": {},
        "rejected_recent_duplicate_count": 0,
        "rejected_by_category_quota": {},
        "prefiltered_low_quality_compound_count": 0,
        "prefiltered_unsupported_category_count": 0,
        "skipped_empty_jlpt_count": 0,
        "safe_jlpt_candidates": 0,
        "cooldown_days_used": LOCAL_SELECTION_COOLDOWN_DAYS,
        "generation_elapsed_ms": 0,
    }
    if limit <= 0:
        return ([], stats) if return_stats else []

    safe_mode = local_generation_safe_mode_enabled()
    target = settings.get("target_level", "")
    rule_context = {"rules": {}} if safe_mode else load_vocab_rule_context(material_date=material_date)
    rules = rule_context.get("rules", {})
    selected = []
    selected_keys = {normalize_vocab_key(key) for key in (exclude_keys or set()) if key}
    selected_rule_counts = {}
    low_quality_examples = []
    quota_examples = []
    cooldown_sequence = local_selection_cooldown_sequence()
    recent_keys_by_days = {
        days: get_recent_used_normalized_keys(days, material_date=material_date)
        for days in cooldown_sequence
        if days > 0
    }
    try:
        today = datetime.strptime(canonical_material_date(material_date or get_today_taipei_date()), "%Y-%m-%d").date()
    except Exception:
        today = taipei_now().date()

    def select_from_rows(rows, source_stage, cooldown_days):
        for row in rows:
            if len(selected) >= limit:
                return
            stats["candidate_counts"][source_stage] = stats["candidate_counts"].get(source_stage, 0) + 1
            low_quality, reason = is_low_quality_compound_word(row)
            if low_quality:
                stats["rejected_low_quality_count"] += 1
                if len(low_quality_examples) < 5:
                    low_quality_examples.append(
                        {
                            "surface": first_text(row, ["surface", "base_form", "term", "word"]),
                            "reason": reason,
                        }
                    )
                continue
            item = build_vocab_item_from_pool_row(row)
            if not item:
                continue
            key = item_normalized_key(item)
            if not key or key in selected_keys:
                if key:
                    stats["rejected_recent_duplicate_count"] += 1
                continue
            if cooldown_days > 0 and key in recent_keys_by_days.get(cooldown_days, set()):
                stats["rejected_recent_duplicate_count"] += 1
                continue
            last_used = parse_loose_date(first_text(row, ["last_used_at", "last_seen_at"]))
            if cooldown_days > 0 and last_used and (today - last_used).days < cooldown_days:
                stats["rejected_recent_duplicate_count"] += 1
                continue
            if source_stage == "jlpt" and is_target_jlpt_pool(item, target):
                stats["target_jlpt_quota_skipped"] = True
            if safe_mode:
                can_select, reason, matches = True, "", []
            else:
                can_select, reason, matches = can_select_vocab_by_rules(
                    item,
                    rule_context,
                    selected_rule_counts,
                    target_level=target,
                    ignore_empty_jlpt_rule=(source_stage == "category"),
                )
            if not can_select:
                stats["rejected_by_rule_count"] += 1
                if "quota" in reason:
                    stats["rejected_by_quota_count"] += 1
                if "category:" in reason:
                    category_rule = reason.split(":", 1)[1]
                    stats["rejected_by_category_quota"][category_rule] = stats["rejected_by_category_quota"].get(category_rule, 0) + 1
                if len(quota_examples) < 5:
                    quota_examples.append({"surface": item.get("word"), "reason": reason})
                continue
            if cooldown_days != LOCAL_SELECTION_COOLDOWN_DAYS:
                stats["cooldown_days_used"] = min(stats.get("cooldown_days_used", LOCAL_SELECTION_COOLDOWN_DAYS), cooldown_days)
            selected_keys.add(key)
            item["_matched_rule_keys"] = [rule["rule_key"] for rule in matches]
            selected.append(item)
            for rule in matches:
                selected_rule_counts[rule["rule_key"]] = selected_rule_counts.get(rule["rule_key"], 0) + 1
            if source_stage == "jlpt":
                stats["selected_by_jlpt_count"] += 1
                if is_target_jlpt_pool(item, target):
                    stats["selected_target_jlpt_count"] += 1
                else:
                    stats["selected_adjacent_jlpt_count"] += 1
            else:
                stats["selected_by_category_count"] += 1

    level_order = safe_mode_level_order(target) if safe_mode else JLPT_ADJACENCY.get(target, [target] if target else [])
    per_level_limit = max(limit * 6, 80)
    for cooldown_days in cooldown_sequence:
        if len(selected) >= limit:
            break
        for level in level_order:
            if len(selected) >= limit:
                break
            exclude_for_sql = set(selected_keys)
            if cooldown_days > 0:
                exclude_for_sql.update(recent_keys_by_days.get(cooldown_days, set()))
            rows = get_safe_jlpt_candidates(level, per_level_limit, cooldown_days=cooldown_days, exclude_normalized_keys=exclude_for_sql)
            stats["safe_jlpt_candidates"] += len(rows)
            select_from_rows(rows, "jlpt", cooldown_days)

    if len(selected) < limit and not safe_mode:
        allowed_categories = []
        for rule in rules.values():
            if rule.get("source_type") != "category":
                continue
            value = clean_rule_match_value(rule.get("match_value"))
            if value == EMPTY_RULE_VALUE:
                continue
            if category_rule_available(rule, selected_rule_counts):
                allowed_categories.append(value)
        allowed_categories = sorted(set(allowed_categories), key=lambda value: -int((rules.get(make_vocab_rule_key("category", value)) or {}).get("priority", 0) or 0))
        if allowed_categories:
            for cooldown_days in cooldown_sequence:
                if len(selected) >= limit:
                    break
                exclude_for_sql = set(selected_keys)
                if cooldown_days > 0:
                    exclude_for_sql.update(recent_keys_by_days.get(cooldown_days, set()))
                rows = fetch_vocabulary_pool_candidates(
                    categories=allowed_categories,
                    limit=max((limit - len(selected)) * 12, 80),
                    safe_jlpt_pool=False,
                    exclude_low_quality=True,
                    exclude_normalized_keys=exclude_for_sql,
                )
                select_from_rows(rows, "category", cooldown_days)

    selected = selected[:limit]
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
        rule = rules.get(rule_key, {})
        quota = int(rule.get("quota_count", 0) or 0)
        used = int(rule.get("used_count", 0) or 0)
        stats["rule_remaining_after_generation"][rule_key] = max(0, quota - used - count) if quota > 0 else None
    stats["selected_from_db_count"] = len(selected)
    stats["generation_elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    if safe_mode:
        print(
            f"[vocab-selector] safe_mode=true target_level={target} word_count={limit} "
            f"db_safe_candidates={stats['safe_jlpt_candidates']} selected_from_db={stats['selected_from_db_count']} "
            f"selected_target_jlpt={stats['selected_target_jlpt_count']} selected_adjacent_jlpt={stats['selected_adjacent_jlpt_count']} "
            f"excluded_low_quality_pattern_count={stats['rejected_low_quality_count']} "
            f"excluded_empty_jlpt_count={stats['skipped_empty_jlpt_count']} elapsed_ms={stats['generation_elapsed_ms']}"
        )
    else:
        print(
            f"[vocab-selector] target_level={target} word_count={limit} "
            f"safe_jlpt_candidates={stats['safe_jlpt_candidates']} "
            f"selected_target_jlpt={stats['selected_target_jlpt_count']} "
            f"selected_adjacent_jlpt={stats['selected_adjacent_jlpt_count']} "
            f"selected_by_category={stats['selected_by_category_count']} "
            f"seed_fallback_count=0 rejected_recent_duplicate={stats['rejected_recent_duplicate_count']} "
            f"rejected_low_quality_count={stats['rejected_low_quality_count']} "
            f"rejected_by_category_quota={stats['rejected_by_category_quota']} "
            f"target_jlpt_quota_skipped={stats['target_jlpt_quota_skipped']} "
            f"cooldown_days_used={stats['cooldown_days_used']} elapsed_ms={stats['generation_elapsed_ms']}"
        )
    if low_quality_examples and not safe_mode:
        print(f"[vocab-selector] rejected_low_quality_examples={low_quality_examples}")
    if quota_examples and not safe_mode:
        print(f"[vocab-rules] rejected_quota_examples={quota_examples}")
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
MATERIAL_GODAN_A_ROW = {
    "う": "わ",
    "く": "か",
    "ぐ": "が",
    "す": "さ",
    "つ": "た",
    "ぬ": "な",
    "ぶ": "ば",
    "む": "ま",
    "る": "ら",
}
MATERIAL_GODAN_E_ROW = {
    "う": "え",
    "く": "け",
    "ぐ": "げ",
    "す": "せ",
    "つ": "て",
    "ぬ": "ね",
    "ぶ": "べ",
    "む": "め",
    "る": "れ",
}
MATERIAL_GODAN_O_ROW = {
    "う": "お",
    "く": "こ",
    "ぐ": "ご",
    "す": "そ",
    "つ": "と",
    "ぬ": "の",
    "ぶ": "ぼ",
    "む": "も",
    "る": "ろ",
}
NO_VERB_FORM = "無此型態"
VERB_FORM_SCHEMA = {
    "dictionary": ("dictionary", "dictionary_form", "base_form", "surface"),
    "masu_stem": ("masu_stem", "masuStem", "renyou", "renyou_form"),
    "te_form": ("te_form", "te", "teForm"),
    "ta_form": ("ta_form", "ta", "taForm"),
    "nai_form": ("nai_form", "nai", "naiForm"),
    "ba_form": ("ba_form", "ba", "baForm"),
    "tara_form": ("tara_form", "tara", "taraForm"),
    "volitional_form": ("volitional_form", "volitional", "volitionalForm"),
    "potential_form": ("potential_form", "potential", "potentialForm"),
    "causative_form": ("causative_form", "causative", "causativeForm"),
    "passive_form": ("passive_form", "passive", "passiveForm"),
    "causative_passive_form": ("causative_passive_form", "causativePassive", "causativePassiveForm"),
}
VERB_ALIAS_FIELDS = {
    "base": "dictionary",
    "masuStem": "masu_stem",
    "te": "te_form",
    "ta": "ta_form",
    "nai": "nai_form",
    "ba": "ba_form",
    "tara": "tara_form",
    "volitional": "volitional_form",
    "potential": "potential_form",
    "causative": "causative_form",
    "passive": "passive_form",
    "causativePassive": "causative_passive_form",
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
SURU_VERB_LIMIT_PER_MATERIAL = 1


def verb_surface_for_filter(item):
    if not isinstance(item, dict):
        return ""
    return first_text(item, ["surface", "dictionary_form", "base_form", "term", "word", "base"])


def is_suru_verb_surface(surface):
    text = str(surface or "").strip()
    return text == "する" or (text.endswith("する") and len(text) > len("する"))


def is_suru_compound_surface(surface):
    text = str(surface or "").strip()
    return text.endswith("する") and text != "する"


def is_core_suru_verb_item(item):
    if not isinstance(item, dict):
        return False
    source = first_text(item, ["source", "_raw_source"]).lower()
    quality = first_text(item, ["quality"]).lower()
    return boolish(item.get("is_core_verb")) or source in {"manual_core", "core_verb"} or (source == "manual" and quality == "core")


def should_exclude_suru_compound_verb(item):
    return is_suru_compound_surface(verb_surface_for_filter(item)) and not is_core_suru_verb_item(item)


def material_verb_is_suru(item):
    surface = verb_surface_for_filter(item)
    verb_type = first_text(item, ["verb_type", "verb_group_label", "part_of_speech"])
    return is_suru_verb_surface(surface) or "サ" in verb_type or "suru" in verb_type.lower()


def material_verb_group_label(group):
    return {1: "五段", 2: "一段", 3: "不規則"}.get(int(group or 0), "未判定")


def material_verb_type_label(group, base_form=""):
    try:
        group_int = int(group or 0)
    except (TypeError, ValueError):
        group_int = 0
    if group_int == 1:
        return "五段"
    if group_int == 2:
        return "一段"
    if group_int == 3:
        if str(base_form) in {"来る", "くる"}:
            return "カ變"
        if str(base_form).endswith("する") or str(base_form) == "する":
            return "サ變"
        return "不規則"
    return "未分類"


def clean_verb_form(value):
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null", "undefined", "-"}:
        return NO_VERB_FORM
    return text


def pick_form_value(source, keys):
    if not isinstance(source, dict):
        return ""
    for key in keys:
        if key in source and str(source.get(key) or "").strip():
            return source.get(key)
    return ""


def make_verb_base_display(surface, reading, meaning, verb_type, jlpt_level):
    surface = str(surface or "").strip()
    reading = str(reading or "").strip()
    meaning = str(meaning or "").strip() or "尚無中文說明"
    verb_type = str(verb_type or "").strip() or "未分類"
    jlpt_level = str(jlpt_level or "").strip() or "未標記"
    label = f"{surface}（{reading}）" if reading else surface
    return f"{label} - {meaning}｜{verb_type}｜{jlpt_level}｜動詞"


def normalize_material_verb_schema(item):
    if not isinstance(item, dict):
        item = {}
    forms_source = item.get("forms") if isinstance(item.get("forms"), dict) else item
    legacy_base = first_text(item, ["base"])
    surface = (
        first_text(item, ["surface", "dictionary_form", "base_form"])
        or pick_form_value(forms_source, VERB_FORM_SCHEMA["dictionary"])
        or first_text(item, ["dictionary"])
    )
    reading = first_text(item, ["reading_hiragana", "reading", "kana"])
    meaning = first_text(item, ["meaning_zh", "meaning", "vocab_meaning"])
    if not surface and legacy_base:
        surface = re.split(r"（|\s+-\s+|｜", legacy_base, maxsplit=1)[0].strip()
    if not reading and legacy_base:
        match = re.search(r"（([^）]+)）", legacy_base)
        reading = match.group(1).strip() if match else ""
    if not meaning and " - " in legacy_base:
        meaning = legacy_base.split(" - ", 1)[1].split("｜", 1)[0].strip()
    meaning = meaning or "尚無中文說明"
    raw_group = item.get("verb_group")
    try:
        group_for_generation = int(raw_group or 0) or infer_material_verb_group(item, surface)
    except (TypeError, ValueError):
        group_for_generation = infer_material_verb_group(item, surface)
    generated_forms = conjugate_material_verb(surface, group_for_generation) if surface and group_for_generation else {}
    if isinstance(generated_forms, dict) and generated_forms:
        merged_forms = dict(generated_forms)
        merged_forms.update({key: value for key, value in forms_source.items() if str(value or "").strip()})
        forms_source = merged_forms
    verb_type = first_text(item, ["verb_type", "verb_group_label"]) or material_verb_type_label(group_for_generation or raw_group, surface)
    jlpt_level = first_text(item, ["jlpt_level", "target_level", "level"]) or "未標記"
    part_of_speech = first_text(item, ["part_of_speech", "pos"]) or "動詞"
    forms = {}
    for key, aliases in VERB_FORM_SCHEMA.items():
        forms[key] = clean_verb_form(pick_form_value(forms_source, aliases))
    if forms["dictionary"] == NO_VERB_FORM and surface:
        forms["dictionary"] = surface
    if not surface and forms["dictionary"] != NO_VERB_FORM:
        surface = forms["dictionary"]

    normalized = dict(item)
    normalized.update(
        {
            "surface": surface,
            "dictionary_form": surface,
            "reading_hiragana": reading,
            "reading": reading,
            "meaning_zh": meaning,
            "meaning": meaning,
            "verb_group": group_for_generation or raw_group,
            "verb_type": verb_type,
            "verb_group_label": verb_type,
            "jlpt_level": jlpt_level,
            "part_of_speech": "動詞" if not part_of_speech or "動詞" not in part_of_speech else part_of_speech,
            "forms": forms,
            "base": make_verb_base_display(surface, reading, meaning, verb_type, jlpt_level),
        }
    )
    for alias, form_key in VERB_ALIAS_FIELDS.items():
        if alias == "base":
            continue
        normalized[alias] = forms.get(form_key, NO_VERB_FORM)
    normalized.setdefault("normalized_key", normalize_vocab_key(surface))
    return normalized


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
    if not is_core_suru_verb_item(row):
        return False
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
    if should_exclude_suru_compound_verb(row):
        return False, "suru_compound_not_core"
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
    def with_aliases(values):
        values = {key: clean_verb_form(value) for key, value in values.items()}
        return {
            "dictionary": values.get("dictionary", clean_verb_form(base_form)),
            "renyou": values.get("masu_stem", NO_VERB_FORM),
            "masu_stem": values.get("masu_stem", NO_VERB_FORM),
            "te": values.get("te_form", NO_VERB_FORM),
            "te_form": values.get("te_form", NO_VERB_FORM),
            "ta": values.get("ta_form", NO_VERB_FORM),
            "ta_form": values.get("ta_form", NO_VERB_FORM),
            "nai": values.get("nai_form", NO_VERB_FORM),
            "nai_form": values.get("nai_form", NO_VERB_FORM),
            "ba": values.get("ba_form", NO_VERB_FORM),
            "ba_form": values.get("ba_form", NO_VERB_FORM),
            "tara": values.get("tara_form", NO_VERB_FORM),
            "tara_form": values.get("tara_form", NO_VERB_FORM),
            "volitional": values.get("volitional_form", NO_VERB_FORM),
            "volitional_form": values.get("volitional_form", NO_VERB_FORM),
            "potential": values.get("potential_form", NO_VERB_FORM),
            "potential_form": values.get("potential_form", NO_VERB_FORM),
            "causative": values.get("causative_form", NO_VERB_FORM),
            "causative_form": values.get("causative_form", NO_VERB_FORM),
            "passive": values.get("passive_form", NO_VERB_FORM),
            "passive_form": values.get("passive_form", NO_VERB_FORM),
            "causative_passive": values.get("causative_passive_form", NO_VERB_FORM),
            "causative_passive_form": values.get("causative_passive_form", NO_VERB_FORM),
        }

    if group == 3:
        if base_form in {"来る", "くる"}:
            if base_form == "くる":
                return with_aliases(
                    {
                        "dictionary": "くる",
                        "masu_stem": "き",
                        "te_form": "きて",
                        "ta_form": "きた",
                        "nai_form": "こない",
                        "ba_form": "くれば",
                        "tara_form": "きたら",
                        "volitional_form": "こよう",
                        "potential_form": "こられる",
                        "causative_form": "こさせる",
                        "passive_form": "こられる",
                        "causative_passive_form": "こさせられる",
                    }
                )
            prefix = "来"
            return with_aliases(
                {
                    "dictionary": "来る",
                    "masu_stem": prefix,
                    "te_form": f"{prefix}て",
                    "ta_form": f"{prefix}た",
                    "nai_form": f"{prefix}ない",
                    "ba_form": f"{prefix}れば",
                    "tara_form": f"{prefix}たら",
                    "volitional_form": f"{prefix}よう",
                    "potential_form": f"{prefix}られる",
                    "causative_form": f"{prefix}させる",
                    "passive_form": f"{prefix}られる",
                    "causative_passive_form": f"{prefix}させられる",
                }
            )
        stem = base_form[:-2] if base_form.endswith("する") else ""
        potential = "できる" if not stem else f"{stem}できる"
        return with_aliases(
            {
                "dictionary": base_form,
                "masu_stem": f"{stem}し",
                "te_form": f"{stem}して",
                "ta_form": f"{stem}した",
                "nai_form": f"{stem}しない",
                "ba_form": f"{stem}すれば",
                "tara_form": f"{stem}したら",
                "volitional_form": f"{stem}しよう",
                "potential_form": potential,
                "causative_form": f"{stem}させる",
                "passive_form": f"{stem}される",
                "causative_passive_form": f"{stem}させられる",
            }
        )
    if group == 2:
        stem = base_form[:-1]
        return with_aliases(
            {
                "dictionary": base_form,
                "masu_stem": stem,
                "te_form": f"{stem}て",
                "ta_form": f"{stem}た",
                "nai_form": f"{stem}ない",
                "ba_form": f"{stem}れば",
                "tara_form": f"{stem}たら",
                "volitional_form": f"{stem}よう",
                "potential_form": f"{stem}られる",
                "causative_form": f"{stem}させる",
                "passive_form": f"{stem}られる",
                "causative_passive_form": f"{stem}させられる",
            }
        )
    if group == 1:
        if base_form == "行く":
            return with_aliases(
                {
                    "dictionary": "行く",
                    "masu_stem": "行き",
                    "te_form": "行って",
                    "ta_form": "行った",
                    "nai_form": "行かない",
                    "ba_form": "行けば",
                    "tara_form": "行ったら",
                    "volitional_form": "行こう",
                    "potential_form": "行ける",
                    "causative_form": "行かせる",
                    "passive_form": "行かれる",
                    "causative_passive_form": "行かせられる",
                }
            )
        ending = base_form[-1:]
        forms = MATERIAL_GODAN_FORMS.get(ending)
        if not forms:
            return None
        stem = base_form[:-1]
        renyou, te, ta, nai, ba, causative, passive = forms
        a_row = MATERIAL_GODAN_A_ROW.get(ending, "")
        e_row = MATERIAL_GODAN_E_ROW.get(ending, "")
        o_row = MATERIAL_GODAN_O_ROW.get(ending, "")
        return with_aliases(
            {
                "dictionary": base_form,
                "masu_stem": f"{stem}{renyou}",
                "te_form": f"{stem}{te}",
                "ta_form": f"{stem}{ta}",
                "nai_form": f"{stem}{nai}",
                "ba_form": f"{stem}{ba}",
                "tara_form": f"{stem}{ta}ら",
                "volitional_form": f"{stem}{o_row}う",
                "potential_form": f"{stem}{e_row}る",
                "causative_form": f"{stem}{causative}",
                "passive_form": f"{stem}{passive}",
                "causative_passive_form": f"{stem}{a_row}せられる",
            }
        )
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
    group_text = material_verb_type_label(group, base_form)
    normalized_key = normalize_vocab_key(first_text(row, ["normalized_key", "normalized_term", "base_form", "surface", "term", "word"]) or base_form)
    item = {
        "surface": base_form,
        "dictionary_form": base_form,
        "reading_hiragana": reading,
        "meaning_zh": meaning,
        "verb_group": group,
        "verb_type": group_text,
        "jlpt_level": level,
        "part_of_speech": "動詞",
        "normalized_key": normalized_key,
        "forms": {
            "dictionary": forms["dictionary"],
            "masu_stem": forms["masu_stem"],
            "te_form": forms["te_form"],
            "ta_form": forms["ta_form"],
            "nai_form": forms["nai_form"],
            "ba_form": forms["ba_form"],
            "tara_form": forms["tara_form"],
            "volitional_form": forms["volitional_form"],
            "potential_form": forms["potential_form"],
            "causative_form": forms["causative_form"],
            "passive_form": forms["passive_form"],
            "causative_passive_form": forms["causative_passive_form"],
        },
        "masuStem": forms["renyou"],
        "te": forms["te"],
        "ta": forms["ta"],
        "nai": forms["nai"],
        "ba": forms["ba"],
        "tara": forms["tara"],
        "volitional": forms["volitional"],
        "potential": forms["potential"],
        "causative": forms["causative"],
        "passive": forms["passive"],
        "causativePassive": forms["causative_passive"],
        "source": source,
        "_pool_id": row.get("id"),
        "_pool_has_last_seen": "last_seen_at" in row,
        "_pool_has_last_used": "last_used_at" in row,
        "_pool_has_used_count": "used_in_material_count" in row,
    }
    return normalize_material_verb_schema(item)


def material_verbs_from_vocabulary_pool(settings, limit, exclude_keys=None, material_date=None, recent_keys_by_days=None):
    stats = {
        "duplicate_filtered_count": 0,
        "selected_keys": [],
        "source_summary": {"vocabulary_pool": 0, "vocabulary_pool_suru": 0},
        "rejected_fake_suru_count": 0,
        "excluded_suru_compound_count": 0,
        "verb_candidate_count": 0,
        "recent_duplicate_rejected_count": 0,
        "cooldown_days_used": LOCAL_SELECTION_COOLDOWN_DAYS,
        "pool_total_count": 0,
        "eligible_pool_count": 0,
        "never_used_candidates": 0,
        "selected_from_never_used_count": 0,
        "selected_from_oldest_used_count": 0,
    }
    if limit <= 0:
        return [], stats
    rows = fetch_vocabulary_pool_verb_rows(limit=max(limit * 20, 120))
    if not rows:
        return [], stats

    target = settings.get("target_level", "")
    seen = {normalize_vocab_key(key) for key in (exclude_keys or set()) if key}
    cooldown_sequence = local_selection_cooldown_sequence()
    recent_keys_by_days = recent_keys_by_days or {
        days: get_recent_used_verb_keys(material_date=material_date, days=days)
        for days in cooldown_sequence
        if days > 0
    }
    verb_usage_stats = get_selection_usage_stats("verb")
    candidates = []

    rejected_log_count = 0
    stats["pool_total_count"] = len(rows)
    for row in rows:
        is_valid, rejection_reason = is_valid_verb_candidate(row)
        if not is_valid:
            if rejection_reason:
                stats["rejected_fake_suru_count"] += 1
                if rejection_reason == "suru_compound_not_core":
                    stats["excluded_suru_compound_count"] += 1
                if rejected_log_count < 5:
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
        stats["eligible_pool_count"] += 1
        if is_never_used_candidate(item, verb_usage_stats):
            stats["never_used_candidates"] += 1
        key = item_normalized_key(item)
        if not key or key in seen:
            stats["duplicate_filtered_count"] += 1
            continue
        row_level = first_text(row, ["jlpt_level", "target_level", "level"])
        level_distance = preferred_level_distance(target, row_level)
        priority = first_text(row, ["priority", "weight"])
        try:
            priority_value = int(float(priority)) if priority else 0
        except ValueError:
            priority_value = 0
        usage_sort = rotation_usage_sort_key(item, verb_usage_stats)
        item["_sort"] = (
            usage_sort[0],
            usage_sort[1],
            usage_sort[2],
            0 if item.get("source") == "vocabulary_pool" else 1,
            level_distance,
            -priority_value,
            usage_sort[3],
        )
        candidates.append(item)

    candidates = sorted(candidates, key=lambda item: item["_sort"])
    selected = []
    selected_keys = set(seen)
    for cooldown_days in cooldown_sequence:
        before_count = len(selected)
        recent_keys = recent_keys_by_days.get(cooldown_days, set()) if cooldown_days > 0 else set()
        for item in candidates:
            if len(selected) >= limit:
                break
            key = item_normalized_key(item)
            if not key or key in selected_keys:
                continue
            if cooldown_days > 0 and key in recent_keys:
                stats["recent_duplicate_rejected_count"] += 1
                continue
            copied = dict(item)
            copied["rule_key"] = f"verb:{copied.get('jlpt_level') or 'local'}"
            selected.append(copied)
            selected_keys.add(key)
            if is_never_used_candidate(copied, verb_usage_stats):
                stats["selected_from_never_used_count"] += 1
            else:
                stats["selected_from_oldest_used_count"] += 1
        if len(selected) > before_count:
            stats["cooldown_days_used"] = cooldown_days
        if len(selected) >= limit:
            break

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


def material_seed_vocab(
    settings,
    limit,
    exclude_keys=None,
    material_date=None,
    recent_keys_by_days=None,
    word_usage_stats=None,
):
    if limit <= 0:
        return []
    seed = seed_basic_safe_pool(settings)
    selected = []
    seen = {normalize_vocab_key(key) for key in (exclude_keys or set()) if key}
    cooldown_sequence = local_selection_cooldown_sequence()
    if recent_keys_by_days is None:
        recent_keys_by_days = {
            days: get_recent_used_normalized_keys(days, material_date=material_date)
            for days in cooldown_sequence
            if days > 0
        }
    if word_usage_stats is None:
        word_usage_stats = get_selection_usage_stats("word")

    def normalized_seed_item(item):
        word = first_text(item, ["word", "surface", "base_form"]).strip()
        if not word:
            return None
        return {
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
            "source": first_text(item, ["source"]) or "seed_basic",
            "rule_key": f"jlpt:{first_text(item, ['jlpt_level']) or settings.get('target_level', 'N5')}",
        }

    seed_items = [item for item in (normalized_seed_item(raw) for raw in seed) if item]
    seed_items = sort_candidates_for_rotation(seed_items, word_usage_stats)
    for cooldown_days in cooldown_sequence:
        for item in seed_items:
            if len(selected) >= limit:
                return selected
            key = item_normalized_key(item)
            if not key or key in seen:
                continue
            if cooldown_days > 0 and key in recent_keys_by_days.get(cooldown_days, set()):
                continue
            seen.add(key)
            selected.append(dict(item))
    return selected[:limit]


def enabled_jlpt_levels_for_vocab_supplement():
    levels = []
    try:
        rules = load_six_main_vocab_rules()
    except Exception as exc:
        print(f"[word-selector] load vocab rules for supplement failed; reason={exc}")
        rules = []
    ordered = sorted(
        [
            rule for rule in rules
            if str(rule.get("rule_key", "")).startswith("jlpt:")
            and boolish(rule.get("enabled"))
        ],
        key=lambda rule: (
            -int(rule.get("priority", 0) or 0),
            SIX_MAIN_VOCAB_RULE_ORDER.index(rule["rule_key"])
            if rule.get("rule_key") in SIX_MAIN_VOCAB_RULE_ORDER
            else 99,
        ),
    )
    for rule in ordered:
        level = str(rule.get("rule_key", "")).split(":", 1)[-1]
        if level in LEVELS and level not in levels:
            levels.append(level)
    return levels or ["N5", "N4"]


def material_safe_vocab_supplement(
    settings,
    limit,
    levels,
    exclude_keys=None,
    material_date=None,
    stage_name="safe_pool",
    recent_keys=None,
    word_usage_stats=None,
):
    stats = {
        "selection_strategy": "safe_vocab_supplement",
        "selected_from_db_count": 0,
        "selected_by_jlpt_count": 0,
        "selected_target_jlpt_count": 0,
        "selected_adjacent_jlpt_count": 0,
        "selected_by_category_count": 0,
        "rejected_low_quality_count": 0,
        "rejected_recent_duplicate_count": 0,
        "safe_jlpt_candidates": 0,
        "pool_total_count": 0,
        "eligible_pool_count": 0,
        "never_used_candidates": 0,
        "selected_from_never_used_count": 0,
        "selected_from_oldest_used_count": 0,
        "repeated_within_14_days_count": 0,
        "candidate_counts": {stage_name: 0},
        "selected_rule_counts": {},
        "rule_selection": {"available_rules": [], "blocked_by_period": [], "selected_counts": {}},
        "category_counts": {},
        "cooldown_days_used": LOCAL_SELECTION_COOLDOWN_DAYS,
    }
    if limit <= 0:
        return [], stats
    target = settings.get("target_level", "")
    selected = []
    selected_keys = {normalize_vocab_key(key) for key in (exclude_keys or set()) if key}
    if word_usage_stats is None:
        word_usage_stats = get_selection_usage_stats("word")
    if recent_keys is None:
        recent_keys = get_recent_used_normalized_keys(LOCAL_SELECTION_COOLDOWN_DAYS, material_date=material_date)
    levels = [level for level in levels if level in LEVELS]
    for level in levels:
        if len(selected) >= limit:
            break
        exclude_for_sql = set(selected_keys) | set(recent_keys)
        rows = fetch_vocabulary_pool_candidates(
            jlpt_levels=[level],
            limit=max((limit - len(selected)) * 12, 100),
            safe_jlpt_pool=True,
            safe_mode=False,
            exclude_low_quality=True,
            exclude_normalized_keys=exclude_for_sql,
        )
        stats["pool_total_count"] += len(rows)
        stats["safe_jlpt_candidates"] += len(rows)
        stats["candidate_counts"][stage_name] = stats["candidate_counts"].get(stage_name, 0) + len(rows)
        rows = sort_candidates_for_rotation(rows, word_usage_stats)
        rule_key = f"jlpt:{level}"
        if rows and rule_key not in stats["rule_selection"]["available_rules"]:
            stats["rule_selection"]["available_rules"].append(rule_key)
        for row in rows:
            if len(selected) >= limit:
                break
            low_quality, _ = is_low_quality_compound_word(row)
            if low_quality:
                stats["rejected_low_quality_count"] += 1
                continue
            item = build_vocab_item_from_pool_row(row)
            if not item:
                continue
            stats["eligible_pool_count"] += 1
            if is_never_used_candidate(item, word_usage_stats):
                stats["never_used_candidates"] += 1
            key = item_normalized_key(item)
            if not key or key in selected_keys or key in recent_keys:
                stats["rejected_recent_duplicate_count"] += 1
                continue
            item["normalized_key"] = key
            item["rule_key"] = rule_key
            item["_matched_rule_keys"] = [rule_key]
            selected.append(item)
            selected_keys.add(key)
            if is_never_used_candidate(item, word_usage_stats):
                stats["selected_from_never_used_count"] += 1
            else:
                stats["selected_from_oldest_used_count"] += 1
            stats["selected_from_db_count"] += 1
            stats["selected_by_jlpt_count"] += 1
            if level == target:
                stats["selected_target_jlpt_count"] += 1
            else:
                stats["selected_adjacent_jlpt_count"] += 1
            stats["selected_rule_counts"][rule_key] = stats["selected_rule_counts"].get(rule_key, 0) + 1
            stats["rule_selection"]["selected_counts"][rule_key] = stats["rule_selection"]["selected_counts"].get(rule_key, 0) + 1
            group = vocab_category_group(item)
            stats["category_counts"][group] = stats["category_counts"].get(group, 0) + 1
    mark_vocabulary_pool_used(selected)
    return selected[:limit], stats


def merge_vocab_selector_stats(target, source):
    if not source:
        return target
    target["rejected_low_quality_count"] = target.get("rejected_low_quality_count", 0) + source.get("rejected_low_quality_count", 0)
    target["rejected_by_rule_count"] = target.get("rejected_by_rule_count", 0) + source.get("rejected_by_rule_count", 0)
    target["rejected_by_quota_count"] = target.get("rejected_by_quota_count", 0) + source.get("rejected_by_quota_count", 0)
    target["rejected_recent_duplicate_count"] = target.get("rejected_recent_duplicate_count", 0) + source.get("rejected_recent_duplicate_count", 0)
    target["selected_by_jlpt_count"] = target.get("selected_by_jlpt_count", 0) + source.get("selected_by_jlpt_count", 0)
    target["selected_target_jlpt_count"] = target.get("selected_target_jlpt_count", 0) + source.get("selected_target_jlpt_count", 0)
    target["selected_adjacent_jlpt_count"] = target.get("selected_adjacent_jlpt_count", 0) + source.get("selected_adjacent_jlpt_count", 0)
    target["selected_by_category_count"] = target.get("selected_by_category_count", 0) + source.get("selected_by_category_count", 0)
    target["target_jlpt_quota_skipped"] = bool(target.get("target_jlpt_quota_skipped")) or bool(source.get("target_jlpt_quota_skipped"))
    target["prefiltered_low_quality_compound_count"] = target.get("prefiltered_low_quality_compound_count", 0) + source.get("prefiltered_low_quality_compound_count", 0)
    target["prefiltered_unsupported_category_count"] = target.get("prefiltered_unsupported_category_count", 0) + source.get("prefiltered_unsupported_category_count", 0)
    target["skipped_empty_jlpt_count"] = target.get("skipped_empty_jlpt_count", 0) + source.get("skipped_empty_jlpt_count", 0)
    target["safe_jlpt_candidates"] = target.get("safe_jlpt_candidates", 0) + source.get("safe_jlpt_candidates", 0)
    target["selected_from_db_count"] = target.get("selected_from_db_count", 0) + source.get("selected_from_db_count", 0)
    target["selected_from_seed_fallback_count"] = target.get("selected_from_seed_fallback_count", 0) + source.get("selected_from_seed_fallback_count", 0)
    target["pool_total_count"] = target.get("pool_total_count", 0) + source.get("pool_total_count", 0)
    target["eligible_pool_count"] = target.get("eligible_pool_count", 0) + source.get("eligible_pool_count", 0)
    target["never_used_candidates"] = target.get("never_used_candidates", 0) + source.get("never_used_candidates", 0)
    target["selected_from_never_used_count"] = target.get("selected_from_never_used_count", 0) + source.get("selected_from_never_used_count", 0)
    target["selected_from_oldest_used_count"] = target.get("selected_from_oldest_used_count", 0) + source.get("selected_from_oldest_used_count", 0)
    target["repeated_within_14_days_count"] = target.get("repeated_within_14_days_count", 0) + source.get("repeated_within_14_days_count", 0)
    target["generation_elapsed_ms"] = target.get("generation_elapsed_ms", 0) + source.get("generation_elapsed_ms", 0)
    if source.get("selection_strategy"):
        target["selection_strategy"] = source["selection_strategy"]
    target.setdefault("slot_allocation", {})
    for rule_key, count in (source.get("slot_allocation") or {}).items():
        target["slot_allocation"][rule_key] = target["slot_allocation"].get(rule_key, 0) + count
    for key in ("category_counts", "candidate_counts", "selected_rule_counts"):
        target.setdefault(key, {})
        for name, count in (source.get(key) or {}).items():
            target[key][name] = target[key].get(name, 0) + count
    target.setdefault("rule_remaining_after_generation", {})
    target["rule_remaining_after_generation"].update(source.get("rule_remaining_after_generation") or {})
    target.setdefault("rule_selection", {"available_rules": [], "blocked_by_period": [], "selected_counts": {}})
    source_rule_selection = source.get("rule_selection") or {}
    target["rule_selection"]["available_rules"] = sorted(
        set(target["rule_selection"].get("available_rules", [])) | set(source_rule_selection.get("available_rules", []))
    )
    target["rule_selection"]["blocked_by_period"] = sorted(
        set(target["rule_selection"].get("blocked_by_period", [])) | set(source_rule_selection.get("blocked_by_period", []))
    )
    target["rule_selection"].setdefault("selected_counts", {})
    for rule_key, count in (source_rule_selection.get("selected_counts") or {}).items():
        target["rule_selection"]["selected_counts"][rule_key] = target["rule_selection"]["selected_counts"].get(rule_key, 0) + count
    target.setdefault("rejected_by_category_quota", {})
    for name, count in (source.get("rejected_by_category_quota") or {}).items():
        target["rejected_by_category_quota"][name] = target["rejected_by_category_quota"].get(name, 0) + count
    target.setdefault("warnings", [])
    for warning in (source.get("warnings") or []):
        if warning and warning not in target["warnings"]:
            target["warnings"].append(warning)
    if source.get("soft_timeout"):
        target["soft_timeout"] = True
    target["cooldown_days_used"] = min(
        target.get("cooldown_days_used", LOCAL_SELECTION_COOLDOWN_DAYS),
        source.get("cooldown_days_used", LOCAL_SELECTION_COOLDOWN_DAYS),
    )
    return target


@lru_cache(maxsize=None)
def cached_seed_json_rows(path):
    started = time.perf_counter()
    try:
        with open(path, "r", encoding="utf-8") as file:
            loaded = json.load(file)
        rows = tuple(dict(row) for row in loaded) if isinstance(loaded, list) else tuple()
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        print(f"[seed-cache] miss path={path} count={len(rows)} elapsed_ms={elapsed_ms}")
        return rows
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        print(f"[seed-cache] miss path={path} failed elapsed_ms={elapsed_ms} reason={exc}")
        return tuple()


def load_cached_seed_json_rows(path):
    before = cached_seed_json_rows.cache_info().hits
    rows = cached_seed_json_rows(path)
    after = cached_seed_json_rows.cache_info().hits
    if after > before:
        print(f"[seed-cache] hit path={path} count={len(rows)}")
    return [dict(row) for row in rows]


def load_basic_seed_vocab_items(settings=None):
    target = (settings or {}).get("target_level", "")
    rows = load_cached_seed_json_rows(VOCABULARY_SEED_BASIC_FILE)
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


EXTENDED_SAFE_SEED_VOCAB_ROWS = [
    ("朝ご飯", "あさごはん", "早餐", "名詞", "N5", "daily"),
    ("昼ご飯", "ひるごはん", "午餐", "名詞", "N5", "daily"),
    ("晩ご飯", "ばんごはん", "晚餐", "名詞", "N5", "daily"),
    ("朝", "あさ", "早上", "名詞", "N5", "daily"),
    ("昼", "ひる", "中午、白天", "名詞", "N5", "daily"),
    ("夜", "よる", "晚上", "名詞", "N5", "daily"),
    ("午前", "ごぜん", "上午", "名詞", "N5", "daily"),
    ("午後", "ごご", "下午", "名詞", "N5", "daily"),
    ("毎朝", "まいあさ", "每天早上", "名詞", "N5", "daily"),
    ("毎晩", "まいばん", "每天晚上", "名詞", "N5", "daily"),
    ("今週", "こんしゅう", "這週", "名詞", "N5", "daily"),
    ("来週", "らいしゅう", "下週", "名詞", "N5", "daily"),
    ("先週", "せんしゅう", "上週", "名詞", "N5", "daily"),
    ("今年", "ことし", "今年", "名詞", "N5", "daily"),
    ("来年", "らいねん", "明年", "名詞", "N5", "daily"),
    ("去年", "きょねん", "去年", "名詞", "N5", "daily"),
    ("春", "はる", "春天", "名詞", "N5", "daily"),
    ("夏", "なつ", "夏天", "名詞", "N5", "daily"),
    ("秋", "あき", "秋天", "名詞", "N5", "daily"),
    ("冬", "ふゆ", "冬天", "名詞", "N5", "daily"),
    ("月曜日", "げつようび", "星期一", "名詞", "N5", "daily"),
    ("火曜日", "かようび", "星期二", "名詞", "N5", "daily"),
    ("水曜日", "すいようび", "星期三", "名詞", "N5", "daily"),
    ("木曜日", "もくようび", "星期四", "名詞", "N5", "daily"),
    ("金曜日", "きんようび", "星期五", "名詞", "N5", "daily"),
    ("土曜日", "どようび", "星期六", "名詞", "N5", "daily"),
    ("日曜日", "にちようび", "星期日", "名詞", "N5", "daily"),
    ("家", "いえ", "家", "名詞", "N5", "daily"),
    ("庭", "にわ", "庭院", "名詞", "N5", "daily"),
    ("台所", "だいどころ", "廚房", "名詞", "N5", "daily"),
    ("トイレ", "といれ", "廁所", "名詞", "N5", "daily"),
    ("風呂", "ふろ", "澡堂、浴室", "名詞", "N5", "daily"),
    ("玄関", "げんかん", "玄關", "名詞", "N5", "daily"),
    ("窓", "まど", "窗戶", "名詞", "N5", "daily"),
    ("ドア", "どあ", "門", "名詞", "N5", "daily"),
    ("時計", "とけい", "時鐘、手錶", "名詞", "N5", "daily"),
    ("傘", "かさ", "傘", "名詞", "N5", "daily"),
    ("鞄", "かばん", "包包", "名詞", "N5", "daily"),
    ("靴", "くつ", "鞋子", "名詞", "N5", "daily"),
    ("服", "ふく", "衣服", "名詞", "N5", "daily"),
    ("帽子", "ぼうし", "帽子", "名詞", "N5", "daily"),
    ("紙", "かみ", "紙", "名詞", "N5", "daily"),
    ("鉛筆", "えんぴつ", "鉛筆", "名詞", "N5", "daily"),
    ("ペン", "ぺん", "筆", "名詞", "N5", "daily"),
    ("新聞", "しんぶん", "報紙", "名詞", "N5", "daily"),
    ("雑誌", "ざっし", "雜誌", "名詞", "N5", "daily"),
    ("辞書", "じしょ", "字典", "名詞", "N5", "daily"),
    ("手紙", "てがみ", "信", "名詞", "N5", "daily"),
    ("切手", "きって", "郵票", "名詞", "N5", "daily"),
    ("荷物", "にもつ", "行李、包裹", "名詞", "N5", "daily"),
    ("写真", "しゃしん", "照片", "名詞", "N5", "daily"),
    ("映画", "えいが", "電影", "名詞", "N5", "daily"),
    ("音楽", "おんがく", "音樂", "名詞", "N5", "daily"),
    ("料理", "りょうり", "料理", "名詞", "N5", "daily"),
    ("旅行", "りょこう", "旅行", "名詞", "N5", "daily"),
    ("宿題", "しゅくだい", "作業", "名詞", "N5", "daily"),
    ("質問", "しつもん", "問題、提問", "名詞", "N5", "daily"),
    ("答え", "こたえ", "答案", "名詞", "N5", "daily"),
    ("病院", "びょういん", "醫院", "名詞", "N5", "daily"),
    ("銀行", "ぎんこう", "銀行", "名詞", "N5", "daily"),
    ("郵便局", "ゆうびんきょく", "郵局", "名詞", "N5", "daily"),
    ("図書館", "としょかん", "圖書館", "名詞", "N5", "daily"),
    ("食堂", "しょくどう", "食堂", "名詞", "N5", "daily"),
    ("公園", "こうえん", "公園", "名詞", "N5", "daily"),
    ("道", "みち", "道路", "名詞", "N5", "daily"),
    ("交差点", "こうさてん", "十字路口", "名詞", "N5", "daily"),
    ("右", "みぎ", "右邊", "名詞", "N5", "daily"),
    ("左", "ひだり", "左邊", "名詞", "N5", "daily"),
    ("前", "まえ", "前面", "名詞", "N5", "daily"),
    ("後ろ", "うしろ", "後面", "名詞", "N5", "daily"),
    ("近く", "ちかく", "附近", "名詞", "N5", "daily"),
    ("隣", "となり", "隔壁、旁邊", "名詞", "N5", "daily"),
    ("中", "なか", "裡面", "名詞", "N5", "daily"),
    ("外", "そと", "外面", "名詞", "N5", "daily"),
    ("上", "うえ", "上面", "名詞", "N5", "daily"),
    ("下", "した", "下面", "名詞", "N5", "daily"),
    ("犬", "いぬ", "狗", "名詞", "N5", "daily"),
    ("猫", "ねこ", "貓", "名詞", "N5", "daily"),
    ("魚", "さかな", "魚", "名詞", "N5", "daily"),
    ("肉", "にく", "肉", "名詞", "N5", "daily"),
    ("野菜", "やさい", "蔬菜", "名詞", "N5", "daily"),
    ("果物", "くだもの", "水果", "名詞", "N5", "daily"),
    ("卵", "たまご", "蛋", "名詞", "N5", "daily"),
    ("牛乳", "ぎゅうにゅう", "牛奶", "名詞", "N5", "daily"),
    ("お茶", "おちゃ", "茶", "名詞", "N5", "daily"),
    ("町", "まち", "城鎮、街道", "名詞", "N5", "daily"),
    ("村", "むら", "村莊", "名詞", "N5", "daily"),
    ("国", "くに", "國家", "名詞", "N5", "daily"),
    ("外国", "がいこく", "外國", "名詞", "N5", "daily"),
    ("人", "ひと", "人", "名詞", "N5", "daily"),
    ("子供", "こども", "小孩", "名詞", "N5", "daily"),
    ("男の人", "おとこのひと", "男人", "名詞", "N5", "daily"),
    ("女の人", "おんなのひと", "女人", "名詞", "N5", "daily"),
    ("白い", "しろい", "白色的", "い形容詞", "N5", "common"),
    ("黒い", "くろい", "黑色的", "い形容詞", "N5", "common"),
    ("赤い", "あかい", "紅色的", "い形容詞", "N5", "common"),
    ("青い", "あおい", "藍色的", "い形容詞", "N5", "common"),
    ("暑い", "あつい", "炎熱的", "い形容詞", "N5", "common"),
    ("寒い", "さむい", "寒冷的", "い形容詞", "N5", "common"),
    ("暖かい", "あたたかい", "溫暖的", "い形容詞", "N5", "common"),
    ("涼しい", "すずしい", "涼爽的", "い形容詞", "N5", "common"),
    ("重い", "おもい", "重的", "い形容詞", "N5", "common"),
    ("軽い", "かるい", "輕的", "い形容詞", "N5", "common"),
    ("近い", "ちかい", "近的", "い形容詞", "N5", "common"),
    ("遠い", "とおい", "遠的", "い形容詞", "N5", "common"),
    ("早い", "はやい", "早的、快的", "い形容詞", "N5", "common"),
    ("遅い", "おそい", "慢的、晚的", "い形容詞", "N5", "common"),
    ("長い", "ながい", "長的", "い形容詞", "N5", "common"),
    ("短い", "みじかい", "短的", "い形容詞", "N5", "common"),
    ("多い", "おおい", "多的", "い形容詞", "N5", "common"),
    ("少ない", "すくない", "少的", "い形容詞", "N5", "common"),
    ("静か", "しずか", "安靜", "な形容詞", "N5", "common"),
    ("元気", "げんき", "有精神、健康", "な形容詞", "N5", "common"),
    ("暇", "ひま", "有空", "な形容詞", "N5", "common"),
    ("有名", "ゆうめい", "有名", "な形容詞", "N5", "common"),
    ("親切", "しんせつ", "親切", "な形容詞", "N5", "common"),
    ("上手", "じょうず", "擅長", "な形容詞", "N5", "common"),
    ("下手", "へた", "不擅長", "な形容詞", "N5", "common"),
    ("嫌い", "きらい", "討厭", "な形容詞", "N5", "common"),
    ("色", "いろ", "顏色", "名詞", "N5", "daily"),
    ("声", "こえ", "聲音", "名詞", "N4", "common"),
    ("形", "かたち", "形狀", "名詞", "N4", "common"),
    ("光", "ひかり", "光", "名詞", "N4", "common"),
    ("音", "おと", "聲音", "名詞", "N4", "common"),
    ("空気", "くうき", "空氣、氣氛", "名詞", "N4", "common"),
    ("文化", "ぶんか", "文化", "名詞", "N4", "common"),
    ("習慣", "しゅうかん", "習慣", "名詞", "N4", "common"),
    ("社会", "しゃかい", "社會", "名詞", "N4", "common"),
    ("歴史", "れきし", "歷史", "名詞", "N4", "common"),
    ("自然", "しぜん", "自然", "名詞", "N4", "common"),
    ("理由", "りゆう", "理由", "名詞", "N4", "common"),
    ("場合", "ばあい", "場合", "名詞", "N4", "common"),
    ("予定", "よてい", "預定、計畫", "名詞", "N4", "common"),
    ("用事", "ようじ", "事情、要辦的事", "名詞", "N4", "daily"),
    ("約束", "やくそく", "約定", "名詞", "N4", "daily"),
    ("必要", "ひつよう", "必要", "な形容詞", "N4", "common"),
    ("十分", "じゅうぶん", "充分", "な形容詞", "N4", "common"),
    ("全部", "ぜんぶ", "全部", "副詞", "N4", "common"),
    ("特に", "とくに", "特別、尤其", "副詞", "N4", "common"),
    ("最近", "さいきん", "最近", "副詞", "N4", "daily"),
    ("将来", "しょうらい", "將來", "名詞", "N4", "common"),
    ("場合", "ばあい", "情況、場合", "名詞", "N4", "common"),
    ("気持ち", "きもち", "心情", "名詞", "N4", "common"),
    ("気分", "きぶん", "心情、身體狀況", "名詞", "N4", "common"),
    ("生活", "せいかつ", "生活", "名詞", "N4", "daily"),
    ("仕事", "しごと", "工作", "名詞", "N5", "daily"),
    ("勉強", "べんきょう", "學習", "名詞", "N5", "daily"),
    ("練習", "れんしゅう", "練習", "名詞", "N4", "daily"),
    ("説明", "せつめい", "說明", "名詞", "N4", "common"),
    ("確認", "かくにん", "確認", "名詞", "N4", "common"),
    ("準備", "じゅんび", "準備", "名詞", "N4", "daily"),
    ("紹介", "しょうかい", "介紹", "名詞", "N4", "common"),
    ("経験", "けいけん", "經驗", "名詞", "N4", "common"),
]


def extended_safe_seed_vocab_items():
    return [
        {
            "surface": surface,
            "base_form": surface,
            "word": surface,
            "reading_hiragana": reading,
            "reading": reading,
            "meaning_zh": meaning,
            "meaning": meaning,
            "part_of_speech": part_of_speech,
            "jlpt_level": jlpt_level,
            "category": category,
            "quality": "core",
            "source": "seed_basic",
            "normalized_key": normalize_vocab_key(surface),
        }
        for surface, reading, meaning, part_of_speech, jlpt_level, category in EXTENDED_SAFE_SEED_VOCAB_ROWS
    ]


def dedupe_seed_vocab_rows(rows):
    deduped = []
    seen = set()
    for row in rows:
        key = normalize_vocab_key(row.get("normalized_key") or row.get("base_form") or row.get("surface") or row.get("word"))
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def seed_basic_safe_pool(settings=None):
    rows = [row for row in load_basic_seed_vocab_items(settings) if is_safe_mode_seed_vocab_item(row)]
    rows = dedupe_seed_vocab_rows(rows + extended_safe_seed_vocab_items())
    if local_generation_safe_mode_enabled():
        return rows
    if len(rows) >= 200:
        return rows
    # The checked-in basic seed file normally has 100+ N5-N3 words.  If it is
    # missing in a local environment, fall back to the small built-in examples
    # so local generation still returns JSON instead of failing.
    return dedupe_seed_vocab_rows(rows + list(sample_material(settings or {}).get("vocab", [])) + LOCAL_SEED_VOCAB)


def material_verbs_from_db(limit, exclude_keys=None, material_date=None, recent_keys_by_days=None):
    if limit <= 0:
        return [], {
            "recent_duplicate_rejected_count": 0,
            "cooldown_days_used": LOCAL_SELECTION_COOLDOWN_DAYS,
            "candidates_from_db": 0,
            "pool_total_count": 0,
            "eligible_pool_count": 0,
            "never_used_candidates": 0,
            "selected_from_never_used_count": 0,
            "selected_from_oldest_used_count": 0,
        }
    ensure_settings_store()
    rows = sqlite_dicts("SELECT * FROM verbs ORDER BY RANDOM() LIMIT ?", (max(limit * 12, limit, 80),))
    seen = {normalize_vocab_key(key) for key in (exclude_keys or set()) if key}
    cooldown_sequence = local_selection_cooldown_sequence()
    recent_keys_by_days = recent_keys_by_days or {
        days: get_recent_used_verb_keys(material_date=material_date, days=days)
        for days in cooldown_sequence
        if days > 0
    }
    stats = {
        "recent_duplicate_rejected_count": 0,
        "cooldown_days_used": LOCAL_SELECTION_COOLDOWN_DAYS,
        "candidates_from_db": len(rows),
        "pool_total_count": len(rows),
        "eligible_pool_count": 0,
        "never_used_candidates": 0,
        "selected_from_never_used_count": 0,
        "selected_from_oldest_used_count": 0,
    }
    verb_usage_stats = get_selection_usage_stats("verb")
    candidates = []
    for row in rows:
        key = normalize_vocab_key(row.get("dictionary_form", ""))
        if not key or key in seen:
            continue
        group = row.get("verb_group", "")
        try:
            group_int = int(group)
        except (TypeError, ValueError):
            group_int = infer_material_verb_group(row, row["dictionary_form"])
        generated = conjugate_material_verb(row["dictionary_form"], group_int) or {}
        item = {
            "surface": row["dictionary_form"],
            "dictionary_form": row["dictionary_form"],
            "reading_hiragana": row["reading"],
            "meaning_zh": row["meaning"],
            "verb_group": group_int,
            "verb_type": material_verb_type_label(group_int, row["dictionary_form"]),
            "jlpt_level": row.get("jlpt_level", ""),
            "part_of_speech": "動詞",
            "normalized_key": key,
            "forms": {
                "dictionary": row["dictionary_form"],
                "masu_stem": answer_display_value(row.get("renyou_form") or generated.get("masu_stem")),
                "te_form": answer_display_value(row.get("te_form") or generated.get("te_form")),
                "ta_form": answer_display_value(row.get("ta_form") or generated.get("ta_form")),
                "nai_form": answer_display_value(row.get("nai_form") or generated.get("nai_form")),
                "ba_form": answer_display_value(row.get("ba_form") or generated.get("ba_form")),
                "tara_form": answer_display_value(generated.get("tara_form")),
                "volitional_form": answer_display_value(generated.get("volitional_form")),
                "potential_form": answer_display_value(generated.get("potential_form")),
                "causative_form": answer_display_value(row.get("shieki_form") or generated.get("causative_form")),
                "passive_form": answer_display_value(row.get("ukemi_form") or generated.get("passive_form")),
                "causative_passive_form": answer_display_value(generated.get("causative_passive_form")),
            },
            "source": "verbs",
            "rule_key": f"verb:{row.get('jlpt_level') or 'local'}",
        }
        normalized = normalize_material_verb_schema(item)
        candidates.append(normalized)
        stats["eligible_pool_count"] += 1
        if is_never_used_candidate(normalized, verb_usage_stats):
            stats["never_used_candidates"] += 1
    candidates = sort_candidates_for_rotation(candidates, verb_usage_stats)

    items = []
    selected_keys = set(seen)
    for cooldown_days in cooldown_sequence:
        before_count = len(items)
        recent_keys = recent_keys_by_days.get(cooldown_days, set()) if cooldown_days > 0 else set()
        for item in candidates:
            if len(items) >= limit:
                break
            key = item_normalized_key(item)
            if not key or key in selected_keys:
                continue
            if cooldown_days > 0 and key in recent_keys:
                stats["recent_duplicate_rejected_count"] += 1
                continue
            items.append(item)
            selected_keys.add(key)
            if is_never_used_candidate(item, verb_usage_stats):
                stats["selected_from_never_used_count"] += 1
            else:
                stats["selected_from_oldest_used_count"] += 1
        if len(items) > before_count:
            stats["cooldown_days_used"] = cooldown_days
        if len(items) >= limit:
            break
    return items, stats


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


EXTENDED_SAFE_SEED_VERB_ROWS = [
    ("見る", "みる", "看", "動詞", "N5"),
    ("食べる", "たべる", "吃", "動詞", "N5"),
    ("飲む", "のむ", "喝", "動詞", "N5"),
    ("行く", "いく", "去", "動詞", "N5"),
    ("来る", "くる", "來", "動詞", "N5"),
    ("帰る", "かえる", "回去、回家", "動詞", "N5"),
    ("読む", "よむ", "閱讀", "動詞", "N5"),
    ("書く", "かく", "寫", "動詞", "N5"),
    ("聞く", "きく", "聽、問", "動詞", "N5"),
    ("話す", "はなす", "說話", "動詞", "N5"),
    ("買う", "かう", "買", "動詞", "N5"),
    ("使う", "つかう", "使用", "動詞", "N5"),
    ("作る", "つくる", "製作", "動詞", "N5"),
    ("会う", "あう", "見面", "動詞", "N5"),
    ("思う", "おもう", "想、認為", "動詞", "N5"),
    ("考える", "かんがえる", "思考、考慮", "動詞", "N4"),
    ("分かる", "わかる", "知道、明白", "動詞", "N5"),
    ("入る", "はいる", "進入", "動詞", "N5"),
    ("出る", "でる", "出去、出現", "動詞", "N5"),
    ("起きる", "おきる", "起床、發生", "動詞", "N5"),
    ("寝る", "ねる", "睡覺", "動詞", "N5"),
    ("働く", "はたらく", "工作", "動詞", "N5"),
    ("休む", "やすむ", "休息、請假", "動詞", "N5"),
    ("選ぶ", "えらぶ", "選擇", "動詞", "N4"),
    ("始める", "はじめる", "開始", "動詞", "N5"),
    ("終わる", "おわる", "結束", "動詞", "N5"),
    ("続ける", "つづける", "繼續", "動詞", "N4"),
    ("開ける", "あける", "打開", "動詞", "N5"),
    ("閉める", "しめる", "關上", "動詞", "N5"),
    ("待つ", "まつ", "等待", "動詞", "N5"),
    ("持つ", "もつ", "持有、拿", "動詞", "N5"),
    ("取る", "とる", "拿取", "動詞", "N5"),
    ("置く", "おく", "放置", "動詞", "N5"),
    ("送る", "おくる", "寄送、送行", "動詞", "N4"),
    ("借りる", "かりる", "借入", "動詞", "N5"),
    ("貸す", "かす", "借出", "動詞", "N5"),
    ("教える", "おしえる", "教、告訴", "動詞", "N5"),
    ("習う", "ならう", "學習", "動詞", "N5"),
    ("忘れる", "わすれる", "忘記", "動詞", "N5"),
    ("覚える", "おぼえる", "記住", "動詞", "N5"),
    ("歩く", "あるく", "走路", "動詞", "N5"),
    ("走る", "はしる", "跑", "動詞", "N5"),
    ("住む", "すむ", "居住", "動詞", "N5"),
    ("笑う", "わらう", "笑", "動詞", "N5"),
    ("泣く", "なく", "哭", "動詞", "N4"),
    ("遊ぶ", "あそぶ", "玩", "動詞", "N5"),
    ("洗う", "あらう", "洗", "動詞", "N5"),
    ("着る", "きる", "穿", "動詞", "N5"),
    ("脱ぐ", "ぬぐ", "脫掉", "動詞", "N5"),
    ("死ぬ", "しぬ", "死亡", "動詞", "N5"),
    ("生きる", "いきる", "活著", "動詞", "N4"),
    ("立つ", "たつ", "站立", "動詞", "N5"),
    ("座る", "すわる", "坐下", "動詞", "N5"),
    ("並ぶ", "ならぶ", "排隊、並列", "動詞", "N4"),
    ("急ぐ", "いそぐ", "趕快", "動詞", "N4"),
    ("手伝う", "てつだう", "幫忙", "動詞", "N4"),
    ("呼ぶ", "よぶ", "呼叫、邀請", "動詞", "N5"),
    ("決める", "きめる", "決定", "動詞", "N4"),
    ("変える", "かえる", "改變", "動詞", "N4"),
    ("増える", "ふえる", "增加", "動詞", "N4"),
    ("減る", "へる", "減少", "動詞", "N4"),
    ("知る", "しる", "知道", "動詞", "N5"),
    ("信じる", "しんじる", "相信", "動詞", "N3"),
    ("助ける", "たすける", "幫助", "動詞", "N4"),
    ("探す", "さがす", "尋找", "動詞", "N4"),
    ("払う", "はらう", "支付", "動詞", "N5"),
    ("渡す", "わたす", "交給、渡過", "動詞", "N4"),
    ("集める", "あつめる", "收集", "動詞", "N4"),
    ("直す", "なおす", "修理、改正", "動詞", "N4"),
    ("動く", "うごく", "移動、運作", "動詞", "N4"),
    ("止まる", "とまる", "停止", "動詞", "N4"),
    ("消す", "けす", "關掉、消除", "動詞", "N5"),
    ("消える", "きえる", "消失", "動詞", "N4"),
    ("開く", "ひらく", "打開、舉辦", "動詞", "N4"),
    ("閉まる", "しまる", "關閉", "動詞", "N4"),
    ("乗る", "のる", "搭乘", "動詞", "N5"),
    ("降りる", "おりる", "下車、下來", "動詞", "N5"),
    ("曲がる", "まがる", "轉彎", "動詞", "N5"),
    ("泳ぐ", "およぐ", "游泳", "動詞", "N5"),
    ("歌う", "うたう", "唱歌", "動詞", "N5"),
    ("踊る", "おどる", "跳舞", "動詞", "N4"),
    ("覚ます", "さます", "弄醒、醒來", "動詞", "N3"),
    ("続く", "つづく", "持續", "動詞", "N4"),
    ("届ける", "とどける", "送達", "動詞", "N3"),
    ("変わる", "かわる", "改變", "動詞", "N4"),
    ("育てる", "そだてる", "培育", "動詞", "N3"),
    ("答える", "こたえる", "回答", "動詞", "N5"),
    ("頼む", "たのむ", "拜託、點餐", "動詞", "N4"),
    ("誘う", "さそう", "邀請", "動詞", "N3"),
    ("運ぶ", "はこぶ", "搬運", "動詞", "N4"),
    ("なくす", "なくす", "遺失", "動詞", "N4"),
    ("拾う", "ひろう", "撿起", "動詞", "N4"),
    ("捨てる", "すてる", "丟掉", "動詞", "N4"),
    ("並べる", "ならべる", "排列", "動詞", "N4"),
    ("比べる", "くらべる", "比較", "動詞", "N4"),
    ("調べる", "しらべる", "調查", "動詞", "N4"),
    ("相談する", "そうだんする", "商量", "サ變動詞", "N4"),
    ("確認する", "かくにんする", "確認", "サ變動詞", "N3"),
    ("説明する", "せつめいする", "說明", "サ變動詞", "N4"),
    ("練習する", "れんしゅうする", "練習", "サ變動詞", "N5"),
    ("勉強する", "べんきょうする", "學習", "サ變動詞", "N5"),
    ("準備する", "じゅんびする", "準備", "サ變動詞", "N4"),
]


def seed_file_verb_items(settings):
    items = []
    for row in load_basic_seed_vocab_items(settings):
        if "動詞" not in first_text(row, ["part_of_speech", "pos"]):
            continue
        item = build_material_verb_from_vocab_row(row)
        if not item:
            continue
        item["source"] = "seed_basic_verb_pool"
        item["normalized_key"] = normalize_vocab_key(first_text(row, ["normalized_key", "base_form", "surface"]) or item.get("surface"))
        items.append(item)
    return items


def extended_safe_seed_verb_items():
    items = []
    for surface, reading, meaning, part_of_speech, level in EXTENDED_SAFE_SEED_VERB_ROWS:
        item = build_material_verb_from_vocab_row(
            {
                "surface": surface,
                "base_form": surface,
                "reading_hiragana": reading,
                "meaning_zh": meaning,
                "part_of_speech": part_of_speech,
                "jlpt_level": level,
                "category": "general",
                "source": "seed_basic_verb_pool",
            }
        )
        if not item:
            continue
        item["source"] = "seed_basic_verb_pool"
        item["normalized_key"] = normalize_vocab_key(surface)
        items.append(item)
    return items


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


def pure_verb_safe_pool(settings):
    raw_seed = (
        seed_file_verb_items(settings)
        + extended_safe_seed_verb_items()
        + natural_seed_verb_items(settings)
        + list(sample_material(settings).get("verbs", []))
        + LOCAL_SEED_VERBS
    )
    items = []
    seen = set()
    excluded_suru = 0
    for raw in raw_seed:
        if should_exclude_suru_compound_verb(raw) or material_verb_is_suru(raw):
            excluded_suru += 1
            continue
        normalized = normalize_material_verb_schema(dict(raw))
        if should_exclude_suru_compound_verb(normalized) or material_verb_is_suru(normalized):
            excluded_suru += 1
            continue
        key = item_normalized_key(normalized)
        if not key or key in seen:
            continue
        seen.add(key)
        normalized["source"] = normalized.get("source") or "pure_verb_safe_pool"
        normalized["rule_key"] = f"verb:{normalized.get('jlpt_level') or 'local'}"
        items.append(normalized)
    random.shuffle(items)
    return items, excluded_suru


def material_seed_verbs(settings, limit, exclude_keys=None, material_date=None, recent_keys_by_days=None, return_stats=False):
    stats = {
        "candidates_from_seed": 0,
        "recent_duplicate_rejected_count": 0,
        "cooldown_days_used": LOCAL_SELECTION_COOLDOWN_DAYS,
        "pure_verb_count": 0,
        "suru_verb_count": 0,
        "suru_verb_limit": SURU_VERB_LIMIT_PER_MATERIAL,
        "excluded_suru_compound_count": 0,
        "pool_total_count": 0,
        "eligible_pool_count": 0,
        "never_used_candidates": 0,
        "selected_from_never_used_count": 0,
        "selected_from_oldest_used_count": 0,
    }
    if limit <= 0:
        return ([], stats) if return_stats else []
    seed, excluded_suru_count = pure_verb_safe_pool(settings)
    stats["excluded_suru_compound_count"] += excluded_suru_count
    stats["candidates_from_seed"] = len(seed)
    stats["pool_total_count"] = len(seed)
    seen = {normalize_vocab_key(key) for key in (exclude_keys or set()) if key}
    cooldown_sequence = local_selection_cooldown_sequence()
    recent_keys_by_days = recent_keys_by_days or {
        days: get_recent_used_verb_keys(material_date=material_date, days=days)
        for days in cooldown_sequence
        if days > 0
    }
    verb_usage_stats = get_selection_usage_stats("verb")
    seed = sort_candidates_for_rotation(seed, verb_usage_stats)
    stats["eligible_pool_count"] = len(seed)
    stats["never_used_candidates"] = count_never_used_candidates(seed, verb_usage_stats)
    items = []
    selected_keys = set(seen)
    for cooldown_days in cooldown_sequence:
        before_count = len(items)
        recent_keys = recent_keys_by_days.get(cooldown_days, set()) if cooldown_days > 0 else set()
        for item in seed:
            if len(items) >= limit:
                break
            base = str(item.get("base", "") or item.get("surface", "") or item.get("dictionary_form", "")).strip()
            key = normalize_vocab_key(item.get("normalized_key") or item.get("dictionary_form") or item.get("surface") or item.get("base") or base)
            if not base or not key or key in selected_keys:
                continue
            if cooldown_days > 0 and key in recent_keys:
                stats["recent_duplicate_rejected_count"] += 1
                continue
            copied = dict(item)
            if should_exclude_suru_compound_verb(copied):
                stats["excluded_suru_compound_count"] += 1
                continue
            copied.setdefault("normalized_key", key)
            copied["source"] = copied.get("source") or "seed_basic_verb_pool"
            copied["rule_key"] = f"verb:{copied.get('jlpt_level') or 'local'}"
            normalized = normalize_material_verb_schema(copied)
            if material_verb_is_suru(normalized):
                stats["excluded_suru_compound_count"] += 1
                continue
            normalized["rule_key"] = copied["rule_key"]
            items.append(normalized)
            if is_never_used_candidate(normalized, verb_usage_stats):
                stats["selected_from_never_used_count"] += 1
            else:
                stats["selected_from_oldest_used_count"] += 1
            stats["pure_verb_count"] += 1
            selected_keys.add(key)
        if len(items) > before_count:
            stats["cooldown_days_used"] = cooldown_days
        if len(items) >= limit:
            break
    return (items[:limit], stats) if return_stats else items[:limit]


def enforce_material_suru_limit(verb_items):
    filtered = []
    suru_kept = 0
    excluded = 0
    for item in verb_items:
        normalized = normalize_material_verb_schema(item)
        if material_verb_is_suru(normalized):
            if suru_kept >= SURU_VERB_LIMIT_PER_MATERIAL:
                excluded += 1
                continue
            suru_kept += 1
        filtered.append(normalized)
    return filtered, excluded


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


def default_grammar_count(grammar_level):
    return DEFAULT_GRAMMAR_COUNT_BY_LEVEL.get(grammar_level, 3)


def parse_grammar_usage_items(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def grammar_item_from_row(row):
    meaning_zh = row.get("meaning_zh", "") or row.get("usage_summary_zh", "") or row.get("meaning", "")
    connection = row.get("connection", "") or row.get("structure_formula", "") or row.get("structure", "") or row.get("pattern", "")
    note_zh = row.get("note_zh", "") or row.get("learning_tip_zh", "") or row.get("note", "")
    fake_name_example = row.get("fake_name_example", "") or row.get("example_hiragana", "")
    return {
        "id": row.get("id"),
        "jlpt_level": row.get("jlpt_level", ""),
        "grammar_key": row.get("grammar_key", ""),
        "title": row.get("title", ""),
        "display_name": row.get("display_name", "") or row.get("title", ""),
        "grammar_type": row.get("grammar_type", ""),
        "meaning_zh": meaning_zh,
        "connection": connection,
        "usage_summary_zh": row.get("usage_summary_zh", ""),
        "usage_detail_zh": row.get("usage_detail_zh", ""),
        "structure_formula": row.get("structure_formula", "") or row.get("structure", "") or row.get("pattern", ""),
        "example_japanese": row.get("example_japanese", "") or row.get("example_jp", ""),
        "example_hiragana": row.get("example_hiragana", ""),
        "example_zh": row.get("example_zh", "") or row.get("example_translation_zh", ""),
        "common_mistake_zh": row.get("common_mistake_zh", ""),
        "learning_tip_zh": row.get("learning_tip_zh", ""),
        "note_zh": note_zh,
        "fake_name_example": fake_name_example,
        "usage_items": parse_grammar_usage_items(row.get("usage_items")),
        "category": row.get("category", "") or row.get("grammar_type", ""),
        "source": row.get("source", "") or row.get("_candidate_source", ""),
        "_candidate_source": row.get("_candidate_source", ""),
    }


def fetch_grammar_candidates(grammar_level, limit, cutoff_date=None):
    ensure_grammar_points_store()
    limit = max(1, int(limit or 1))
    if DATABASE_URL:
        params = [grammar_level]
        recent_filter = ""
        if cutoff_date:
            recent_filter = """
                AND id NOT IN (
                    SELECT grammar_point_id
                    FROM grammar_selection_logs
                    WHERE material_date >= %s AND grammar_point_id IS NOT NULL
                )
            """
            params.append(cutoff_date)
        params.append(limit)
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT *
                    FROM grammar_points
                    WHERE jlpt_level = %s
                      AND COALESCE(is_active, TRUE) = TRUE
                      {recent_filter}
                    ORDER BY priority DESC,
                             COALESCE(used_count, 0) ASC,
                             last_used_at ASC NULLS FIRST,
                             RANDOM()
                    LIMIT %s
                    """,
                    params,
                )
                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
    ensure_settings_store()
    params = [grammar_level]
    recent_filter = ""
    if cutoff_date:
        recent_filter = """
            AND id NOT IN (
                SELECT grammar_point_id
                FROM grammar_selection_logs
                WHERE material_date >= ? AND grammar_point_id IS NOT NULL
            )
        """
        params.append(cutoff_date)
    params.append(limit)
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT *
            FROM grammar_points
            WHERE jlpt_level = ?
              AND COALESCE(is_active, 1) = 1
              {recent_filter}
            ORDER BY priority DESC,
                     COALESCE(used_count, 0) ASC,
                     COALESCE(last_used_at, '') ASC,
                     RANDOM()
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def grammar_fallback_levels(grammar_level):
    grammar_level = grammar_level if grammar_level in LEVELS else "N5"
    if GRAMMAR_FALLBACK_ADJACENT_LEVELS:
        return GRAMMAR_ADJACENT_FALLBACKS.get(grammar_level, [grammar_level])
    return [grammar_level]


def fetch_imported_grammar_candidates(grammar_level):
    rows = []
    table_names = ("grammar_bank", "imported_grammar_pool")
    if DATABASE_URL:
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    for table_name in table_names:
                        cur.execute("SELECT to_regclass(%s)", (table_name,))
                        exists = cur.fetchone()
                        if not exists or not exists[0]:
                            continue
                        cur.execute(
                            f"""
                            SELECT *
                            FROM {table_name}
                            WHERE jlpt_level = %s
                              AND COALESCE(is_active, TRUE) = TRUE
                            LIMIT 5000
                            """,
                            (grammar_level,),
                        )
                        columns = [desc[0] for desc in cur.description]
                        for row in cur.fetchall():
                            item = dict(zip(columns, row))
                            item["_candidate_source"] = table_name
                            rows.append(item)
        except Exception as exc:
            print(f"[grammar-selector] imported grammar lookup failed level={grammar_level}; reason={exc}")
        return rows
    try:
        ensure_settings_store()
        with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
            conn.row_factory = sqlite3.Row
            for table_name in table_names:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,),
                ).fetchone()
                if not exists:
                    continue
                for row in conn.execute(
                    f"""
                    SELECT *
                    FROM {table_name}
                    WHERE jlpt_level = ?
                      AND COALESCE(is_active, 1) = 1
                    LIMIT 5000
                    """,
                    (grammar_level,),
                ).fetchall():
                    item = dict(row)
                    item["_candidate_source"] = table_name
                    rows.append(item)
    except Exception as exc:
        print(f"[grammar-selector] imported grammar lookup failed level={grammar_level}; reason={exc}")
    return rows


def fetch_grammar_pool_candidates(grammar_level):
    raw_rows = []
    warnings = []
    try:
        rows = fetch_grammar_candidates(grammar_level, 5000, None)
        for row in rows:
            row["_candidate_source"] = "grammar_points"
        raw_rows.extend(rows)
    except Exception as exc:
        warnings.append("grammar_points_unavailable")
        print(f"[grammar-selector] grammar_points fetch failed level={grammar_level}; reason={exc}")
    raw_rows.extend(fetch_imported_grammar_candidates(grammar_level))
    for item in seed_grammar_candidates(grammar_level):
        item["_candidate_source"] = "seed_grammar_pool"
        raw_rows.append(item)

    unique = {}
    for row in raw_rows:
        item = row if isinstance(row, dict) and isinstance(row.get("usage_items"), list) else grammar_item_from_row(row)
        key = grammar_item_dedupe_key(item)
        if not key or key in unique:
            continue
        unique[key] = item
    return list(unique.values()), warnings


def grammar_item_dedupe_key(item):
    if not isinstance(item, dict):
        return ""
    for key in ("grammar_key", "title", "display_name"):
        value = str(item.get(key, "") or "").strip()
        if value:
            return value
    return ""


def get_grammar_usage_by_level(jlpt_level):
    usage = {}
    level = jlpt_level if jlpt_level in LEVELS else "N5"
    try:
        ensure_grammar_points_store()
        if DATABASE_URL:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT grammar_key, MAX(COALESCE(created_at::TEXT, material_date::TEXT)) AS last_used
                        FROM grammar_selection_logs
                        WHERE jlpt_level = %s
                          AND COALESCE(NULLIF(grammar_key, ''), '') <> ''
                        GROUP BY grammar_key
                        """,
                        (level,),
                    )
                    return {str(row[0]).strip(): str(row[1] or "") for row in cur.fetchall() if row and row[0]}
        for row in sqlite_dicts(
            """
            SELECT grammar_key, MAX(COALESCE(created_at, material_date)) AS last_used
            FROM grammar_selection_logs
            WHERE jlpt_level = ?
              AND COALESCE(NULLIF(grammar_key, ''), '') <> ''
            GROUP BY grammar_key
            """,
            (level,),
        ):
            key = str(row.get("grammar_key", "") or "").strip()
            if key:
                usage[key] = str(row.get("last_used", "") or "")
    except Exception as exc:
        print(f"[grammar-selector] grammar usage lookup failed level={level}; reason={exc}")
    return usage


def get_used_grammar_keys_by_level(jlpt_level):
    return set(get_grammar_usage_by_level(jlpt_level).keys())


def get_recent_used_grammar_keys(material_date=None, days=None, include_all_versions=True):
    try:
        if days is None:
            days = LOCAL_GRAMMAR_COOLDOWN_DAYS
        day_count = max(0, int(days or 0))
    except (TypeError, ValueError):
        day_count = LOCAL_GRAMMAR_COOLDOWN_DAYS
    if day_count <= 0:
        return set()
    try:
        end_date = datetime.strptime(canonical_material_date(material_date or get_today_taipei_date()), "%Y-%m-%d").date()
    except Exception:
        end_date = taipei_now().date()
    start_date = end_date - timedelta(days=day_count)
    start, end = start_date.isoformat(), end_date.isoformat()
    try:
        ensure_grammar_points_store()
        if DATABASE_URL:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT DISTINCT grammar_key
                        FROM grammar_selection_logs
                        WHERE material_date BETWEEN %s AND %s
                          AND COALESCE(NULLIF(grammar_key, ''), '') <> ''
                        """,
                        (start, end),
                    )
                    return {str(row[0]).strip() for row in cur.fetchall() if row and row[0]}
        rowset = sqlite_dicts(
            """
            SELECT DISTINCT grammar_key
            FROM grammar_selection_logs
            WHERE material_date BETWEEN ? AND ?
              AND COALESCE(NULLIF(grammar_key, ''), '') <> ''
            """,
            (start, end),
        )
        return {str(row.get("grammar_key", "")).strip() for row in rowset if row.get("grammar_key")}
    except Exception as exc:
        print(f"[grammar-selector] recent grammar lookup failed; days={days}; reason={exc}")
        return set()


def seed_grammar_candidates(grammar_level):
    rows = [row for row in default_grammar_seed_rows() if row.get("jlpt_level") == grammar_level]
    return [grammar_item_from_row(row) for row in rows]


def record_grammar_selection(grammar_items, material_date, material_key=None, material_version_no=None):
    if not grammar_items:
        return
    ensure_grammar_points_store()
    now = utc_now_iso()
    date_iso = material_date_iso(material_date) or today_iso_date()
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for item in grammar_items:
                    grammar_id = item.get("id")
                    if grammar_id:
                        cur.execute(
                            """
                            UPDATE grammar_points
                            SET used_count = COALESCE(used_count, 0) + 1,
                                last_used_at = %s,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (now, now, grammar_id),
                        )
                    cur.execute(
                        """
                        INSERT INTO grammar_selection_logs (
                            material_date, grammar_point_id, grammar_key, jlpt_level, grammar_type,
                            material_key, material_version_no, version_no, title, pattern, category, selected_for, created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            date_iso,
                            grammar_id,
                            item.get("grammar_key", ""),
                            item.get("jlpt_level", ""),
                            item.get("grammar_type", ""),
                            material_key or "",
                            material_version_no,
                            material_version_no,
                            item.get("title", ""),
                            item.get("connection", "") or item.get("structure_formula", ""),
                            item.get("category", "") or item.get("grammar_type", ""),
                            "grammar",
                            now,
                        ),
                    )
            conn.commit()
        return
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        for item in grammar_items:
            grammar_id = item.get("id")
            if grammar_id:
                conn.execute(
                    """
                    UPDATE grammar_points
                    SET used_count = COALESCE(used_count, 0) + 1,
                        last_used_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, grammar_id),
                )
            conn.execute(
                """
                INSERT INTO grammar_selection_logs (
                    material_date, grammar_point_id, grammar_key, jlpt_level, grammar_type,
                    material_key, material_version_no, version_no, title, pattern, category, selected_for, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    date_iso,
                    grammar_id,
                    item.get("grammar_key", ""),
                    item.get("jlpt_level", ""),
                    item.get("grammar_type", ""),
                    material_key or "",
                    material_version_no,
                    material_version_no,
                    item.get("title", ""),
                    item.get("connection", "") or item.get("structure_formula", ""),
                    item.get("category", "") or item.get("grammar_type", ""),
                    "grammar",
                    now,
                ),
            )
        conn.commit()


def best_effort_record_grammar_selection(grammar_items, material_date, material_key=None, material_version_no=None):
    if not grammar_items:
        return ""

    def run_safely():
        try:
            record_grammar_selection(
                grammar_items,
                material_date,
                material_key=material_key,
                material_version_no=material_version_no,
            )
            return ""
        except BaseException as exc:
            warning = "grammar_selection_log_failed"
            print(
                "[grammar-selector] selection log best-effort failed "
                f"material_key={material_key}; reason={exc}"
            )
            try:
                print(traceback.format_exc())
            except Exception:
                pass
            return warning

    if DATABASE_URL:
        result = {"warning": ""}

        def worker():
            result["warning"] = run_safely()

        thread = threading.Thread(
            target=worker,
            name=f"grammar-selection-log-{material_key or 'unknown'}",
            daemon=True,
        )
        thread.start()
        thread.join(0.05)
        if thread.is_alive():
            print(f"[grammar-selector] selection log deferred material_key={material_key}")
            return "grammar_selection_log_deferred"
        return result["warning"]

    return run_safely()

def select_grammar_points(grammar_level, grammar_count, material_date=None, allow_level_fallback=True, ignore_recent=False):
    selector_started = time.perf_counter()
    grammar_level = grammar_level if grammar_level in LEVELS else "N5"
    try:
        grammar_count = int(grammar_count)
    except (TypeError, ValueError):
        grammar_count = default_grammar_count(grammar_level)
    grammar_count = max(0, min(grammar_count, 10))
    if grammar_count <= 0:
        return [], {
            "grammar_pool_empty": False,
            "grammar_fallback_used": False,
            "grammar_warnings": [],
            "grammar_selection": {},
            "grammar_duplicate_filter": {
                "cooldown_days_requested": LOCAL_GRAMMAR_COOLDOWN_DAYS,
                "cooldown_days_used": 0,
                "recent_duplicate_rejected_count": 0,
                "recent_used_count": 0,
                "selected_count": 0,
                "insufficient_unique": False,
            },
        }

    selected = []
    selected_keys = set()
    warnings = []
    material_date_value = material_date or get_today_taipei_date()
    levels = grammar_fallback_levels(grammar_level) if allow_level_fallback else [grammar_level]
    recent_keys = set() if ignore_recent else get_recent_used_grammar_keys(material_date_value, days=LOCAL_GRAMMAR_COOLDOWN_DAYS)
    recent_duplicate_rejected_count = 0
    selected_from_unused_count = 0
    selected_from_oldest_used_count = 0
    fallback_level_used = []
    source_counts = {"grammar_points": 0, "grammar_bank": 0, "imported_grammar_pool": 0, "seed_grammar_pool": 0}
    level_pool_counts = {}
    level_used_counts = {}
    level_unused_counts = {}
    level_recent_counts = {}
    usage_by_level = {}
    pools_by_level = {}

    def sort_unused_candidates(rows):
        rows = list(rows)
        random.shuffle(rows)
        return sorted(rows, key=lambda item: -clamp_int(item.get("priority", 50), default=50))

    def sort_oldest_used_candidates(rows, usage_map):
        rows = list(rows)
        random.shuffle(rows)
        return sorted(
            rows,
            key=lambda item: (
                usage_map.get(grammar_item_dedupe_key(item), ""),
                -clamp_int(item.get("priority", 50), default=50),
            ),
        )

    def take_candidates(rows, source_label):
        nonlocal selected_from_unused_count, selected_from_oldest_used_count
        for row in rows:
            if len(selected) >= grammar_count:
                break
            key = grammar_item_dedupe_key(row)
            if not key or key in selected_keys:
                continue
            selected.append(row)
            selected_keys.add(key)
            candidate_source = row.get("_candidate_source") or row.get("source") or "grammar_points"
            source_counts[candidate_source] = source_counts.get(candidate_source, 0) + 1
            if source_label == "unused":
                selected_from_unused_count += 1
            else:
                selected_from_oldest_used_count += 1

    def load_level_pool(level):
        if level in pools_by_level:
            return pools_by_level[level]
        candidates, pool_warnings = fetch_grammar_pool_candidates(level)
        for warning in pool_warnings:
            if warning not in warnings:
                warnings.append(warning)
        unique = {}
        for item in candidates:
            key = grammar_item_dedupe_key(item)
            if key and key not in unique:
                unique[key] = item
        pool = list(unique.values())
        usage = get_grammar_usage_by_level(level)
        pool_keys = {grammar_item_dedupe_key(item) for item in pool if grammar_item_dedupe_key(item)}
        used_keys = pool_keys & set(usage)
        recent_in_level = pool_keys & recent_keys
        pools_by_level[level] = pool
        usage_by_level[level] = usage
        level_pool_counts[level] = len(pool_keys)
        level_used_counts[level] = len(used_keys)
        level_unused_counts[level] = max(0, len(pool_keys) - len(used_keys))
        level_recent_counts[level] = len(recent_in_level)
        return pool

    try:
        for level in levels:
            if len(selected) >= grammar_count:
                break
            pool = load_level_pool(level)
            usage = usage_by_level.get(level, {})
            eligible = []
            for item in pool:
                key = grammar_item_dedupe_key(item)
                if not key or key in selected_keys:
                    continue
                if key in recent_keys:
                    recent_duplicate_rejected_count += 1
                    continue
                eligible.append(item)
            if not eligible:
                continue
            unused_candidates = [item for item in eligible if grammar_item_dedupe_key(item) not in usage]
            take_candidates(sort_unused_candidates(unused_candidates), "unused")
            if len(selected) < grammar_count:
                oldest_used_candidates = [
                    item
                    for item in eligible
                    if grammar_item_dedupe_key(item) in usage and grammar_item_dedupe_key(item) not in selected_keys
                ]
                take_candidates(sort_oldest_used_candidates(oldest_used_candidates, usage), "oldest_used")
            if level != grammar_level and any((item.get("jlpt_level") == level) for item in selected):
                if level not in fallback_level_used:
                    fallback_level_used.append(level)

        if not selected:
            warnings.append("grammar_pool_empty")
        elif len(selected) < grammar_count:
            warnings.append("insufficient_grammar_candidates_after_rotation")
            warnings.append("insufficient_unique_grammar_after_7_day_cooldown")
        for level in levels:
            load_level_pool(level)
        elapsed_ms = round((time.perf_counter() - selector_started) * 1000)
        selected_key_list = [grammar_item_dedupe_key(item) for item in selected if grammar_item_dedupe_key(item)]
        primary_pool_total = level_pool_counts.get(grammar_level, 0)
        primary_pool_used = level_used_counts.get(grammar_level, 0)
        primary_pool_unused = level_unused_counts.get(grammar_level, 0)
        primary_recent_count = level_recent_counts.get(grammar_level, 0)
        fallback_used = bool(fallback_level_used) or bool(source_counts.get("seed_grammar_pool"))
        print(
            "[grammar-selector] "
            "strategy=rotation_until_exhausted"
        )
        print(f"[grammar-selector] level={grammar_level} requested={grammar_count}")
        print(
            "[grammar-selector] "
            f"pool_total={primary_pool_total} pool_used={primary_pool_used} "
            f"pool_unused={primary_pool_unused} recent_7_days_used={primary_recent_count}"
        )
        print(
            "[grammar-selector] "
            f"selected_from_unused={selected_from_unused_count} "
            f"selected_from_oldest_used={selected_from_oldest_used_count} "
            f"fallback_used={str(fallback_used).lower()} selected_keys={selected_key_list} "
            f"elapsed_ms={elapsed_ms}"
        )
        return selected, {
            "grammar_pool_empty": not bool(selected),
            "grammar_fallback_used": fallback_used,
            "grammar_warnings": warnings,
            "grammar_selection": {
                "strategy": "rotation_until_exhausted",
                "requested_count": grammar_count,
                "selected_count": len(selected),
                "grammar_level": grammar_level,
                "pool_total_count": primary_pool_total,
                "pool_used_count": primary_pool_used,
                "pool_unused_count": primary_pool_unused,
                "recent_7_days_used_count": primary_recent_count,
                "selected_from_unused_count": selected_from_unused_count,
                "selected_from_oldest_used_count": selected_from_oldest_used_count,
                "fallback_used": fallback_used,
                "fallback_level_used": fallback_level_used,
                "source_counts": source_counts,
                "recent_duplicate_rejected_count": recent_duplicate_rejected_count,
                "cooldown_days_requested": LOCAL_GRAMMAR_COOLDOWN_DAYS,
                "cooldown_days_used": LOCAL_GRAMMAR_COOLDOWN_DAYS,
                "selected_grammar_keys": selected_key_list,
                "level_pool_counts": level_pool_counts,
                "level_used_counts": level_used_counts,
                "level_unused_counts": level_unused_counts,
                "warnings": warnings,
            },
            "grammar_duplicate_filter": {
                "cooldown_days_requested": LOCAL_GRAMMAR_COOLDOWN_DAYS,
                "cooldown_days_used": LOCAL_GRAMMAR_COOLDOWN_DAYS,
                "recent_duplicate_rejected_count": recent_duplicate_rejected_count,
                "recent_used_count": len(recent_keys),
                "selected_count": len(selected),
                "insufficient_unique": len(selected) < grammar_count,
            },
        }
    except Exception as exc:
        print(f"[grammar-pool] local grammar selection failed; reason={exc}")
        print(traceback.format_exc())
        return [], {
            "grammar_pool_empty": True,
            "grammar_fallback_used": True,
            "grammar_warnings": ["grammar_pool_empty"],
            "grammar_selection": {
                "strategy": "rotation_until_exhausted",
                "requested_count": grammar_count,
                "selected_count": 0,
                "grammar_level": grammar_level,
                "source_counts": {"grammar_points": 0, "seed_grammar_pool": 0},
                "recent_duplicate_rejected_count": 0,
                "cooldown_days_requested": LOCAL_GRAMMAR_COOLDOWN_DAYS,
                "cooldown_days_used": 0,
                "selected_grammar_keys": [],
                "warnings": ["grammar_pool_empty"],
            },
            "grammar_duplicate_filter": {
                "cooldown_days_requested": LOCAL_GRAMMAR_COOLDOWN_DAYS,
                "cooldown_days_used": 0,
                "recent_duplicate_rejected_count": 0,
                "recent_used_count": 0,
                "selected_count": 0,
                "insufficient_unique": True,
            },
        }


def select_fixed_quota_grammar_points(material_date=None):
    selector_started = time.perf_counter()
    quota = dict(LOCAL_FIXED_GRAMMAR_QUOTA)
    selected = []
    selected_keys = set()
    selected_counts = {level: 0 for level in quota}
    warnings = []
    level_stats = {}
    source_counts = {}
    recent_duplicate_rejected_count = 0
    recent_used_count = 0
    selected_from_unused_count = 0
    selected_from_oldest_used_count = 0
    cooldown_relaxed_levels = []
    pool_total_by_level = {}

    for level, requested_count in quota.items():
        if requested_count <= 0:
            continue
        try:
            level_items, stats = select_grammar_points(
                level,
                requested_count,
                material_date=material_date,
                allow_level_fallback=False,
            )
        except Exception as exc:
            print(f"[grammar-selector] fixed quota level={level} failed; reason={exc}")
            level_items, stats = [], {
                "grammar_warnings": [f"grammar_level_{level}_failed"],
                "grammar_selection": {"selected_count": 0},
                "grammar_duplicate_filter": {},
            }

        level_stats[level] = stats
        grammar_selection = stats.get("grammar_selection") or {}
        duplicate_filter = stats.get("grammar_duplicate_filter") or {}
        pool_total_by_level[level] = int(grammar_selection.get("pool_total_count") or 0)
        recent_duplicate_rejected_count += int(grammar_selection.get("recent_duplicate_rejected_count") or 0)
        recent_used_count += int(duplicate_filter.get("recent_used_count") or 0)
        selected_from_unused_count += int(grammar_selection.get("selected_from_unused_count") or 0)
        selected_from_oldest_used_count += int(grammar_selection.get("selected_from_oldest_used_count") or 0)
        for source, count in (grammar_selection.get("source_counts") or {}).items():
            source_counts[source] = source_counts.get(source, 0) + int(count or 0)
        for warning in stats.get("grammar_warnings") or []:
            if warning and warning not in warnings:
                warnings.append(warning)

        for item in level_items:
            if selected_counts[level] >= requested_count:
                break
            key = grammar_item_dedupe_key(item)
            if not key or key in selected_keys:
                continue
            selected.append(item)
            selected_keys.add(key)
            selected_counts[level] += 1

        if pool_total_by_level[level] <= 0:
            warning = f"grammar_{level}_pool_empty"
            if warning not in warnings:
                warnings.append(warning)
            continue

        if selected_counts[level] < requested_count:
            warning = f"insufficient_grammar_{level}_quota"
            if warning not in warnings:
                warnings.append(warning)

    total_target = sum(quota.values())
    total_actual = len(selected)
    selected_key_list = [grammar_item_dedupe_key(item) for item in selected if grammar_item_dedupe_key(item)]
    elapsed_ms = round((time.perf_counter() - selector_started) * 1000)
    print(
        "[grammar-selector] strategy=fixed_quota "
        f"quota={quota} selected_counts={selected_counts} "
        f"total_actual={total_actual} warnings={warnings} elapsed_ms={elapsed_ms}"
    )
    return selected, {
        "grammar_pool_empty": not bool(selected),
        "grammar_fallback_used": bool(warnings),
        "grammar_warnings": warnings,
        "grammar_quota": quota,
        "grammar_selected_counts": selected_counts,
        "grammar_total_target": total_target,
        "grammar_total_actual": total_actual,
        "grammar_pool_total_by_level": pool_total_by_level,
        "grammar_fallback_warnings": warnings,
        "grammar_selection": {
            "strategy": "fixed_quota_rotation",
            "requested_count": total_target,
            "selected_count": total_actual,
            "grammar_quota": quota,
            "grammar_selected_counts": selected_counts,
            "grammar_total_target": total_target,
            "grammar_total_actual": total_actual,
            "level_pool_counts": pool_total_by_level,
            "grammar_fallback_warnings": warnings,
            "cooldown_relaxed_levels": cooldown_relaxed_levels,
            "source_counts": source_counts,
            "recent_duplicate_rejected_count": recent_duplicate_rejected_count,
            "cooldown_days_requested": LOCAL_GRAMMAR_COOLDOWN_DAYS,
            "cooldown_days_used": LOCAL_GRAMMAR_COOLDOWN_DAYS,
            "selected_grammar_keys": selected_key_list,
            "level_stats": level_stats,
            "warnings": warnings,
        },
        "grammar_duplicate_filter": {
            "cooldown_days_requested": LOCAL_GRAMMAR_COOLDOWN_DAYS,
            "cooldown_days_used": 0 if cooldown_relaxed_levels else LOCAL_GRAMMAR_COOLDOWN_DAYS,
            "recent_duplicate_rejected_count": recent_duplicate_rejected_count,
            "recent_used_count": recent_used_count,
            "selected_count": total_actual,
            "insufficient_unique": total_actual < total_target,
        },
    }


def build_local_material(settings, force_seed=False, material_date=None, deadline_check=None):
    material_started = time.perf_counter()
    settings = normalize_settings(settings)
    if deadline_check:
        deadline_check("build_material_start")
    safe_mode = False
    vocab_count = int(settings["vocab_count"])
    verb_count = int(settings["verb_count"])
    grammar_level = "fixed_quota"
    grammar_quota = dict(LOCAL_FIXED_GRAMMAR_QUOTA)
    grammar_count = LOCAL_FIXED_GRAMMAR_TOTAL
    source_counts = {"vocabulary": 0, "slang": 0, "wrong": 0, "seed": 0}
    vocab_selector_stats = {
        "selection_strategy": "rotation_until_exhausted",
        "local_generation_safe_mode": safe_mode,
        "allowed_jlpt_levels": sorted(LOCAL_SAFE_MODE_JLPT_LEVELS),
        "allowed_categories": sorted(LOCAL_SAFE_MODE_CATEGORIES),
        "disabled_sources": sorted(LOCAL_SAFE_MODE_DISABLED_SOURCES),
        "selected_from_db_count": 0,
        "selected_from_seed_fallback_count": 0,
        "selected_by_jlpt_count": 0,
        "selected_target_jlpt_count": 0,
        "selected_adjacent_jlpt_count": 0,
        "selected_by_category_count": 0,
        "target_jlpt_quota_skipped": False,
        "rejected_low_quality_count": 0,
        "rejected_by_rule_count": 0,
        "rejected_by_quota_count": 0,
        "category_counts": {},
        "candidate_counts": {},
        "selected_rule_counts": {},
        "rule_remaining_after_generation": {},
        "slot_allocation": {},
        "rejected_recent_duplicate_count": 0,
        "rejected_by_category_quota": {},
        "prefiltered_low_quality_compound_count": 0,
        "prefiltered_unsupported_category_count": 0,
        "skipped_empty_jlpt_count": 0,
        "safe_jlpt_candidates": 0,
        "cooldown_days_used": LOCAL_SELECTION_COOLDOWN_DAYS,
        "generation_elapsed_ms": 0,
    }
    seed_used = False
    generation_warnings = []
    duplicate_filtered_count = 0

    vocab_mode = settings.get("vocab_mode", "general")
    # SNS terms are now governed only by the six main appearance rules
    # (category:SNS).  The older vocab_mode quota path is intentionally
    # disabled so it cannot bypass weekly/monthly period controls.
    slang_quota = 0

    base_quota = max(0, vocab_count - slang_quota)
    if deadline_check:
        deadline_check("word_selector_before")
    word_cooldown_sequence = local_selection_cooldown_sequence()
    word_recent_keys_by_days = {
        days: get_recent_used_normalized_keys(days, material_date=material_date or get_today_taipei_date())
        for days in word_cooldown_sequence
        if days > 0
    }
    word_usage_stats = get_selection_usage_stats("word")
    primary_word_pool_stats = {}
    if force_seed:
        vocab = []
    else:
        vocab, pool_stats = material_vocab_from_vocabulary_pool(
            settings,
            base_quota,
            return_stats=True,
            material_date=material_date or get_today_taipei_date(),
            recent_keys_by_days=word_recent_keys_by_days,
            word_usage_stats=word_usage_stats,
        )
        primary_word_pool_stats = pool_stats or {}
        merge_vocab_selector_stats(vocab_selector_stats, pool_stats)
    if deadline_check:
        deadline_check("word_primary_pool_after")
    vocab, duplicates, selected_keys = dedupe_vocab_items(vocab)
    duplicate_filtered_count += duplicates
    source_counts["vocabulary"] = len([item for item in vocab if item.get("source") in {"vocabulary_pool", "materials"}])

    slang_vocab = [] if (force_seed or safe_mode) else material_vocab_from_approved_slang(slang_quota)
    slang_vocab, duplicates, selected_keys = dedupe_vocab_items(slang_vocab, selected_keys)
    duplicate_filtered_count += duplicates
    mark_slang_used_in_material([{"id": item.get("_slang_id")} for item in slang_vocab if item.get("_slang_id")])
    source_counts["slang"] = len(slang_vocab)
    vocab.extend(slang_vocab)

    word_stage_counts = {
        "rules": len(vocab),
        "target_level_safe_pool": 0,
        "adjacent_level_safe_pool": 0,
        "enabled_levels_safe_pool": 0,
        "reallocation_safe_pool": 0,
        "seed_basic_safe_pool": 0,
    }
    target_level = settings.get("target_level", "")
    selected_target_levels = settings_target_levels(settings)
    selected_jlpt_levels = [level for level in selected_target_levels if level in LEVELS]
    word_unfilled_slots = max(0, vocab_count - len(vocab))
    word_reallocated_slots = 0
    word_fallback_used = False
    word_fallback_reason = ""
    word_warnings = []

    def add_vocab_supplement(stage_key, levels):
        nonlocal vocab, selected_keys, duplicate_filtered_count
        remaining = vocab_count - len(vocab)
        if remaining <= 0:
            return []
        items, supplement_stats = material_safe_vocab_supplement(
            settings,
            remaining,
            levels,
            exclude_keys=selected_keys,
            material_date=material_date or get_today_taipei_date(),
            stage_name=stage_key,
            recent_keys=word_recent_keys_by_days.get(LOCAL_SELECTION_COOLDOWN_DAYS, set()),
            word_usage_stats=word_usage_stats,
        )
        items, duplicates, selected_keys = dedupe_vocab_items(items, selected_keys)
        duplicate_filtered_count += duplicates
        if items:
            vocab.extend(items)
            word_stage_counts[stage_key] += len(items)
            source_counts["vocabulary"] += len([item for item in items if item.get("source") in {"vocabulary_pool", "materials"}])
        merge_vocab_selector_stats(vocab_selector_stats, supplement_stats)
        return items

    def one_time_reallocation_levels():
        ordered = []
        for level in selected_jlpt_levels + [target_level] + ["N5", "N4", "N3"] + enabled_jlpt_levels_for_vocab_supplement():
            if level in LEVELS and level not in ordered:
                ordered.append(level)
        return ordered

    skip_db_reallocation = (
        bool(primary_word_pool_stats.get("soft_timeout"))
        or (
            not force_seed
            and base_quota > 0
            and int(primary_word_pool_stats.get("pool_total_count", 0) or 0) < base_quota
            and len(vocab) < vocab_count
        )
    )
    if skip_db_reallocation:
        warning = "insufficient_primary_word_pool_used_seed_fallback"
        if primary_word_pool_stats.get("soft_timeout"):
            warning = "vocab_allocation_soft_timeout_used_seed_fallback"
        if warning not in generation_warnings:
            generation_warnings.append(warning)
        word_warnings.append(warning)
        print(
            "[word-selector] skip db reallocation "
            f"pool_total={primary_word_pool_stats.get('pool_total_count', 0)} "
            f"selected={len(vocab)} requested={vocab_count} reason={warning}"
        )

    if not force_seed and len(vocab) < vocab_count and not skip_db_reallocation:
        before_reallocation = len(vocab)
        add_vocab_supplement("reallocation_safe_pool", one_time_reallocation_levels())
        word_reallocated_slots += max(0, len(vocab) - before_reallocation)

    vocab_seed_fallback_count = 0
    if len(vocab) < vocab_count:
        before_seed_reallocation = len(vocab)
        seed_started = time.perf_counter()
        seed_items = material_seed_vocab(
            settings,
            vocab_count - len(vocab),
            exclude_keys=selected_keys,
            material_date=material_date or get_today_taipei_date(),
            recent_keys_by_days=word_recent_keys_by_days,
            word_usage_stats=word_usage_stats,
        )
        seed_elapsed_ms = round((time.perf_counter() - seed_started) * 1000)
        seed_items, duplicates, selected_keys = dedupe_vocab_items(seed_items, selected_keys)
        duplicate_filtered_count += duplicates
        vocab.extend(seed_items)
        vocab_seed_fallback_count = len(seed_items)
        word_stage_counts["seed_basic_safe_pool"] += len(seed_items)
        word_reallocated_slots += max(0, len(vocab) - before_seed_reallocation)
        vocab_selector_stats["selected_from_seed_fallback_count"] = vocab_seed_fallback_count
        seed_word_usage_stats = get_selection_usage_stats("word")
        for item in seed_items:
            if is_never_used_candidate(item, seed_word_usage_stats):
                vocab_selector_stats["selected_from_never_used_count"] = (
                    vocab_selector_stats.get("selected_from_never_used_count", 0) + 1
                )
            else:
                vocab_selector_stats["selected_from_oldest_used_count"] = (
                    vocab_selector_stats.get("selected_from_oldest_used_count", 0) + 1
                )
        if seed_items:
            vocab_selector_stats["seed_fallback_used"] = True
            word_fallback_used = True
            word_fallback_reason = "clean_seed_basic_safe_pool"
            if not vocab_selector_stats.get("db_pool_used"):
                generation_warnings.append("vocabulary_pool_unavailable_used_seed_fallback")
        vocab_selector_stats.setdefault("rule_selection", {"available_rules": [], "blocked_by_period": [], "selected_counts": {}})
        vocab_selector_stats["rule_selection"].setdefault("selected_counts", {})
        for item in seed_items:
            rule_key = item.get("rule_key")
            if rule_key:
                vocab_selector_stats["rule_selection"]["selected_counts"][rule_key] = (
                    vocab_selector_stats["rule_selection"]["selected_counts"].get(rule_key, 0) + 1
                )
                vocab_selector_stats.setdefault("selected_rule_counts", {})
                vocab_selector_stats["selected_rule_counts"][rule_key] = vocab_selector_stats["selected_rule_counts"].get(rule_key, 0) + 1
        source_counts["seed"] += len(seed_items)
        seed_used = bool(seed_items)
        print(
            "[word-fallback] "
            f"used={str(bool(seed_items)).lower()} selected={len(seed_items)} "
            f"elapsed_ms={seed_elapsed_ms} remaining_before={vocab_count - before_seed_reallocation}"
        )
    if len(vocab) < vocab_count:
        word_warnings.append("insufficient_words_after_one_time_reallocation")
        for warning in word_warnings:
            if warning not in generation_warnings:
                generation_warnings.append(warning)
    vocab = vocab[:vocab_count]
    if deadline_check:
        deadline_check("word_selector_after")
    selected_normalized_keys = [item_normalized_key(item) for item in vocab if item_normalized_key(item)]
    category_counts = Counter(vocab_category_group(item) for item in vocab)
    vocab_source_summary = Counter((item.get("source") or "unknown") for item in vocab)
    quality_counts = Counter((item.get("quality") or "未設定") for item in vocab)
    jlpt_counts = Counter((item.get("jlpt_level") or "未分類 JLPT") for item in vocab)
    part_of_speech_counts = Counter((item.get("part_of_speech") or "未分類詞性") for item in vocab)

    verb_duplicate_filtered_count = 0
    verb_source_summary = {"vocabulary_pool": 0, "vocabulary_pool_suru": 0, "verbs": 0, "seed_fallback": 0}
    rejected_fake_suru_count = 0
    excluded_suru_compound_count = 0
    verb_candidate_count = 0
    verb_recent_duplicate_rejected_count = 0
    verb_cooldown_days_used = LOCAL_SELECTION_COOLDOWN_DAYS
    verb_candidates_from_db = 0
    verb_candidates_from_seed = 0
    verb_pool_total_count = 0
    verb_eligible_pool_count = 0
    verb_never_used_candidates = 0
    verb_selected_from_never_used_count = 0
    verb_selected_from_oldest_used_count = 0
    verb_stage_counts = {"db_pure_verbs": 0, "pure_verb_safe_pool": 0, "verbs_table": 0}
    verbs = []
    selected_verb_keys = []
    if deadline_check:
        deadline_check("verb_selector_before")
    verb_recent_keys_by_days = {
        days: get_recent_used_verb_keys(material_date=material_date or get_today_taipei_date(), days=days)
        for days in local_selection_cooldown_sequence()
        if days > 0
    }
    if not force_seed and not safe_mode:
        verbs, verb_pool_stats = material_verbs_from_vocabulary_pool(
            settings,
            verb_count,
            exclude_keys=selected_verb_keys,
            material_date=material_date or get_today_taipei_date(),
            recent_keys_by_days=verb_recent_keys_by_days,
        )
        verb_stage_counts["db_pure_verbs"] += len(verbs)
        verb_duplicate_filtered_count += verb_pool_stats.get("duplicate_filtered_count", 0)
        rejected_fake_suru_count += verb_pool_stats.get("rejected_fake_suru_count", 0)
        excluded_suru_compound_count += verb_pool_stats.get("excluded_suru_compound_count", 0)
        verb_candidate_count += verb_pool_stats.get("verb_candidate_count", 0)
        verb_recent_duplicate_rejected_count += verb_pool_stats.get("recent_duplicate_rejected_count", 0)
        verb_cooldown_days_used = min(verb_cooldown_days_used, verb_pool_stats.get("cooldown_days_used", LOCAL_SELECTION_COOLDOWN_DAYS))
        verb_candidates_from_db += verb_pool_stats.get("verb_candidate_count", 0)
        verb_pool_total_count += verb_pool_stats.get("pool_total_count", 0)
        verb_eligible_pool_count += verb_pool_stats.get("eligible_pool_count", 0)
        verb_never_used_candidates += verb_pool_stats.get("never_used_candidates", 0)
        verb_selected_from_never_used_count += verb_pool_stats.get("selected_from_never_used_count", 0)
        verb_selected_from_oldest_used_count += verb_pool_stats.get("selected_from_oldest_used_count", 0)
        selected_verb_keys.extend(verb_pool_stats.get("selected_keys", []))
        for source, count in verb_pool_stats.get("source_summary", {}).items():
            verb_source_summary[source] = verb_source_summary.get(source, 0) + count
    if len(verbs) < verb_count:
        seed_verbs, seed_verb_stats = material_seed_verbs(
            settings,
            verb_count - len(verbs),
            exclude_keys=set(selected_verb_keys),
            material_date=material_date or get_today_taipei_date(),
            recent_keys_by_days=verb_recent_keys_by_days,
            return_stats=True,
        )
        verbs.extend(seed_verbs)
        verb_stage_counts["pure_verb_safe_pool"] += len(seed_verbs)
        seed_keys = [item_normalized_key(item) for item in seed_verbs if item_normalized_key(item)]
        selected_verb_keys.extend(seed_keys)
        source_counts["seed"] += len(seed_verbs)
        verb_source_summary["seed_fallback"] += len(seed_verbs)
        verb_recent_duplicate_rejected_count += seed_verb_stats.get("recent_duplicate_rejected_count", 0)
        excluded_suru_compound_count += seed_verb_stats.get("excluded_suru_compound_count", 0)
        verb_cooldown_days_used = min(verb_cooldown_days_used, seed_verb_stats.get("cooldown_days_used", LOCAL_SELECTION_COOLDOWN_DAYS))
        verb_candidates_from_seed += seed_verb_stats.get("candidates_from_seed", 0)
        verb_pool_total_count += seed_verb_stats.get("pool_total_count", 0)
        verb_eligible_pool_count += seed_verb_stats.get("eligible_pool_count", 0)
        verb_never_used_candidates += seed_verb_stats.get("never_used_candidates", 0)
        verb_selected_from_never_used_count += seed_verb_stats.get("selected_from_never_used_count", 0)
        verb_selected_from_oldest_used_count += seed_verb_stats.get("selected_from_oldest_used_count", 0)
        seed_used = bool(seed_verbs)
        if seed_verbs:
            print(f"[verb-selector] seed fallback used count={len(seed_verbs)} source=seed_basic_verb_pool")
    if len(verbs) < verb_count and not force_seed:
        db_verbs, db_verb_stats = material_verbs_from_db(
            verb_count - len(verbs),
            exclude_keys=set(selected_verb_keys),
            material_date=material_date or get_today_taipei_date(),
            recent_keys_by_days=verb_recent_keys_by_days,
        )
        verbs.extend(db_verbs)
        verb_stage_counts["verbs_table"] += len(db_verbs)
        db_keys = [item_normalized_key(item) for item in db_verbs if item_normalized_key(item)]
        selected_verb_keys.extend(db_keys)
        verb_source_summary["verbs"] += len(db_verbs)
        verb_recent_duplicate_rejected_count += db_verb_stats.get("recent_duplicate_rejected_count", 0)
        verb_cooldown_days_used = min(verb_cooldown_days_used, db_verb_stats.get("cooldown_days_used", LOCAL_SELECTION_COOLDOWN_DAYS))
        verb_candidates_from_db += db_verb_stats.get("candidates_from_db", 0)
        verb_pool_total_count += db_verb_stats.get("pool_total_count", 0)
        verb_eligible_pool_count += db_verb_stats.get("eligible_pool_count", 0)
        verb_never_used_candidates += db_verb_stats.get("never_used_candidates", 0)
        verb_selected_from_never_used_count += db_verb_stats.get("selected_from_never_used_count", 0)
        verb_selected_from_oldest_used_count += db_verb_stats.get("selected_from_oldest_used_count", 0)
        if db_verbs:
            print(f"[verb-selector] seed fallback used count={len(db_verbs)} source=verbs_table")
    verbs = [normalize_material_verb_schema(item) for item in verbs]
    verbs, suru_limit_excluded = enforce_material_suru_limit(verbs)
    excluded_suru_compound_count += suru_limit_excluded
    if len(verbs) > verb_count:
        verbs = verbs[:verb_count]
    selected_verb_keys = [item_normalized_key(item) for item in verbs if item_normalized_key(item)]
    if len(verbs) < verb_count:
        topup_seed_verbs, topup_seed_stats = material_seed_verbs(
            settings,
            verb_count - len(verbs),
            exclude_keys=set(selected_verb_keys),
            material_date=material_date or get_today_taipei_date(),
            recent_keys_by_days=verb_recent_keys_by_days,
            return_stats=True,
        )
        if topup_seed_verbs:
            verbs.extend(topup_seed_verbs)
            verb_stage_counts["pure_verb_safe_pool"] += len(topup_seed_verbs)
            verb_source_summary["seed_fallback"] += len(topup_seed_verbs)
            source_counts["seed"] += len(topup_seed_verbs)
        verb_recent_duplicate_rejected_count += topup_seed_stats.get("recent_duplicate_rejected_count", 0)
        excluded_suru_compound_count += topup_seed_stats.get("excluded_suru_compound_count", 0)
        verb_cooldown_days_used = min(verb_cooldown_days_used, topup_seed_stats.get("cooldown_days_used", LOCAL_SELECTION_COOLDOWN_DAYS))
        verb_candidates_from_seed += topup_seed_stats.get("candidates_from_seed", 0)
        verb_pool_total_count += topup_seed_stats.get("pool_total_count", 0)
        verb_eligible_pool_count += topup_seed_stats.get("eligible_pool_count", 0)
        verb_never_used_candidates += topup_seed_stats.get("never_used_candidates", 0)
        verb_selected_from_never_used_count += topup_seed_stats.get("selected_from_never_used_count", 0)
        verb_selected_from_oldest_used_count += topup_seed_stats.get("selected_from_oldest_used_count", 0)
        verbs, suru_limit_excluded = enforce_material_suru_limit(verbs)
        excluded_suru_compound_count += suru_limit_excluded
        if len(verbs) > verb_count:
            verbs = verbs[:verb_count]
    selected_verb_keys = [item_normalized_key(item) for item in verbs if item_normalized_key(item)]
    if len(verbs) < verb_count and not force_seed:
        topup_db_verbs, topup_db_stats = material_verbs_from_db(
            verb_count - len(verbs),
            exclude_keys=set(selected_verb_keys),
            material_date=material_date or get_today_taipei_date(),
            recent_keys_by_days=verb_recent_keys_by_days,
        )
        if topup_db_verbs:
            verbs.extend(topup_db_verbs)
            verb_stage_counts["verbs_table"] += len(topup_db_verbs)
            verb_source_summary["verbs"] += len(topup_db_verbs)
        verb_recent_duplicate_rejected_count += topup_db_stats.get("recent_duplicate_rejected_count", 0)
        verb_cooldown_days_used = min(verb_cooldown_days_used, topup_db_stats.get("cooldown_days_used", LOCAL_SELECTION_COOLDOWN_DAYS))
        verb_candidates_from_db += topup_db_stats.get("candidates_from_db", 0)
        verb_pool_total_count += topup_db_stats.get("pool_total_count", 0)
        verb_eligible_pool_count += topup_db_stats.get("eligible_pool_count", 0)
        verb_never_used_candidates += topup_db_stats.get("never_used_candidates", 0)
        verb_selected_from_never_used_count += topup_db_stats.get("selected_from_never_used_count", 0)
        verb_selected_from_oldest_used_count += topup_db_stats.get("selected_from_oldest_used_count", 0)
        verbs, suru_limit_excluded = enforce_material_suru_limit(verbs)
        excluded_suru_compound_count += suru_limit_excluded
        if len(verbs) > verb_count:
            verbs = verbs[:verb_count]
    selected_verb_keys = [item_normalized_key(item) for item in verbs if item_normalized_key(item)]
    suru_verb_count = sum(1 for item in verbs if material_verb_is_suru(item))
    pure_verb_count = max(0, len(verbs) - suru_verb_count)
    if deadline_check:
        deadline_check("verb_selector_after")
    final_verb_stage_counts = {"db_pure_verbs": 0, "pure_verb_safe_pool": 0, "verbs_table": 0}
    for item in verbs:
        source = str(item.get("source") or "").strip()
        if source == "vocabulary_pool":
            final_verb_stage_counts["db_pure_verbs"] += 1
        elif source in {"verbs", "verbs_table"}:
            final_verb_stage_counts["verbs_table"] += 1
        else:
            final_verb_stage_counts["pure_verb_safe_pool"] += 1
    verb_stage_counts = final_verb_stage_counts

    wrong_items = due_wrong_answer_summary()
    source_counts["wrong"] = len(wrong_items)
    quiz = build_local_quiz(vocab, verbs, settings)
    if deadline_check:
        deadline_check("grammar_selector_before")
    grammar_points, grammar_stats = select_fixed_quota_grammar_points(material_date or get_today_taipei_date())
    if deadline_check:
        deadline_check("grammar_selector_after")
    grammar_examples = [
        {"jp": item.get("example_japanese", ""), "cn": item.get("example_zh", "")}
        for item in grammar_points[:2]
        if item.get("example_japanese") or item.get("example_zh")
    ]
    grammar = {
        "title": f"{grammar_level} 本地文法題庫",
        "exp": "今日文法由本地題庫抽取，不消耗 Gemini API 額度。請先掌握例句中的助詞、句型功能與常見錯誤。",
        "examples": grammar_examples,
    }
    if len(vocab) < vocab_count:
        generation_warnings.append("insufficient_unique_words_after_7_day_cooldown")
        generation_warnings.append("insufficient_unique_words_after_all_safe_fallbacks")
    if len(verbs) < verb_count:
        generation_warnings.append("insufficient_unique_verbs_after_7_day_cooldown")
    if len(grammar_points) < grammar_count:
        generation_warnings.append("insufficient_unique_grammar_after_7_day_cooldown")
    if safe_mode and len(vocab) < vocab_count:
        generation_warnings.append("insufficient_safe_vocab_pool")
    for warning in (vocab_selector_stats.get("warnings") or []):
        if warning == "insufficient_unique_words_after_7_day_cooldown" and len(vocab) >= vocab_count:
            continue
        if warning and warning not in generation_warnings:
            generation_warnings.append(warning)
    for warning in (grammar_stats.get("grammar_warnings") or []):
        if warning and warning not in generation_warnings:
            generation_warnings.append(warning)
    metadata = {
        "generation_mode": "local",
        "selection_strategy": "rotation_until_exhausted",
        "resolved_settings": {
            "target_level": settings.get("target_level", ""),
            "target_levels": selected_target_levels,
            "word_count": vocab_count,
            "verb_count": verb_count,
            "grammar_level": grammar_level,
            "grammar_count": grammar_count,
            "grammar_mode": "fixed_quota",
            "grammar_quota": grammar_quota,
            "grammar_total_target": grammar_count,
        },
        "target_levels": selected_target_levels,
        "cooldown_policy": {
            "days": LOCAL_SELECTION_COOLDOWN_DAYS,
            "allow_relax": LOCAL_SELECTION_ALLOW_COOLDOWN_RELAX,
        },
        "run_migrations_on_request": RUN_MIGRATIONS_ON_REQUEST,
        "db_pool_used": bool(vocab_selector_stats.get("db_pool_used", False)),
        "seed_fallback_used": bool(vocab_seed_fallback_count),
        "local_generation_safe_mode": safe_mode,
        "allowed_jlpt_levels": sorted(LOCAL_SAFE_MODE_JLPT_LEVELS) if safe_mode else [],
        "allowed_categories": sorted(LOCAL_SAFE_MODE_CATEGORIES) if safe_mode else [],
        "disabled_sources": sorted(LOCAL_SAFE_MODE_DISABLED_SOURCES) if safe_mode else [],
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
        "rule_selection": vocab_selector_stats.get("rule_selection", {}),
        "slot_allocation": vocab_selector_stats.get("slot_allocation", {}),
        "selected_counts": vocab_selector_stats.get("rule_selection", {}).get("selected_counts", {}),
        "blocked_by_period": vocab_selector_stats.get("rule_selection", {}).get("blocked_by_period", []),
        "rule_remaining_after_generation": vocab_selector_stats.get("rule_remaining_after_generation", {}),
        "selected_by_jlpt_count": vocab_selector_stats.get("selected_by_jlpt_count", 0),
        "selected_target_jlpt_count": vocab_selector_stats.get("selected_target_jlpt_count", 0),
        "selected_adjacent_jlpt_count": vocab_selector_stats.get("selected_adjacent_jlpt_count", 0),
        "selected_by_category_count": vocab_selector_stats.get("selected_by_category_count", 0),
        "selected_from_db_count": vocab_selector_stats.get("selected_from_db_count", 0),
        "selected_from_seed_fallback_count": vocab_seed_fallback_count,
        "target_jlpt_quota_skipped": vocab_selector_stats.get("target_jlpt_quota_skipped", False),
        "rejected_low_quality_count": vocab_selector_stats.get("rejected_low_quality_count", 0),
        "rejected_by_rule_count": vocab_selector_stats.get("rejected_by_rule_count", 0),
        "rejected_by_quota_count": vocab_selector_stats.get("rejected_by_quota_count", 0),
        "rejected_by_category_quota": vocab_selector_stats.get("rejected_by_category_quota", {}),
        "prefiltered_low_quality_compound_count": vocab_selector_stats.get("prefiltered_low_quality_compound_count", 0),
        "prefiltered_unsupported_category_count": vocab_selector_stats.get("prefiltered_unsupported_category_count", 0),
        "skipped_empty_jlpt_count": vocab_selector_stats.get("skipped_empty_jlpt_count", 0),
        "safe_jlpt_candidates": vocab_selector_stats.get("safe_jlpt_candidates", 0),
        "general_count": category_counts.get("general", 0),
        "business_count": category_counts.get("business", 0),
        "advanced_count": category_counts.get("advanced", 0),
        "sns_count": category_counts.get("sns", 0),
        "selected_normalized_keys": selected_normalized_keys,
        "duplicate_filtered_count": duplicate_filtered_count,
        "duplicate_filter": {
            "cooldown_days_requested": LOCAL_SELECTION_COOLDOWN_DAYS,
            "cooldown_days": vocab_selector_stats.get("cooldown_days_used", LOCAL_SELECTION_COOLDOWN_DAYS),
            "cooldown_days_used": vocab_selector_stats.get("cooldown_days_used", LOCAL_SELECTION_COOLDOWN_DAYS),
            "rejected_recent_duplicate_count": vocab_selector_stats.get("rejected_recent_duplicate_count", 0),
        },
        "word_duplicate_filter": {
            "recent_used_count": vocab_selector_stats.get("recent_used_word_count", 0),
            "selected_count": len(vocab),
            "insufficient_unique": len(vocab) < vocab_count,
        },
        "word_selection": {
            "strategy": "rotation_until_exhausted",
            "requested_count": vocab_count,
            "selected_count": len(vocab),
            "pool_total_count": vocab_selector_stats.get("pool_total_count", 0),
            "eligible_pool_count": vocab_selector_stats.get("eligible_pool_count", 0),
            "recent_7_days_used_count": vocab_selector_stats.get("recent_used_word_count", 0),
            "selected_from_never_used_count": vocab_selector_stats.get("selected_from_never_used_count", 0),
            "selected_from_oldest_used_count": vocab_selector_stats.get("selected_from_oldest_used_count", 0),
            "fallback_used": bool(vocab_seed_fallback_count),
            "repeated_within_14_days_count": vocab_selector_stats.get("repeated_within_14_days_count", 0),
            "selected_by_rule": vocab_selector_stats.get("rule_selection", {}).get("selected_counts", {}),
            "selected_from_safe_fallback": vocab_seed_fallback_count,
            "stage_counts": word_stage_counts,
            "selected_from_rules": word_stage_counts.get("rules", 0),
            "selected_from_target_safe_pool": word_stage_counts.get("target_level_safe_pool", 0),
            "selected_from_adjacent_safe_pool": word_stage_counts.get("adjacent_level_safe_pool", 0),
            "selected_from_enabled_safe_pool": word_stage_counts.get("enabled_levels_safe_pool", 0),
            "selected_from_seed_pool": word_stage_counts.get("seed_basic_safe_pool", 0),
            "cooldown_days": LOCAL_SELECTION_COOLDOWN_DAYS,
            "recent_duplicate_rejected_count": vocab_selector_stats.get("rejected_recent_duplicate_count", 0),
            "insufficient_unique": len(vocab) < vocab_count,
        },
        "word_distribution": {
            "requested_count": vocab_count,
            "selected_count": len(vocab),
            "rules_count": word_stage_counts.get("rules", 0),
            "refill_count": max(0, len(vocab) - word_stage_counts.get("rules", 0)),
            "refill_stage_counts": {
                "target_level_safe_pool": word_stage_counts.get("target_level_safe_pool", 0),
                "adjacent_level_safe_pool": word_stage_counts.get("adjacent_level_safe_pool", 0),
                "enabled_levels_safe_pool": word_stage_counts.get("enabled_levels_safe_pool", 0),
                "seed_basic_safe_pool": word_stage_counts.get("seed_basic_safe_pool", 0),
            },
            "count_matched": len(vocab) >= vocab_count,
        },
        "selected_verb_keys": selected_verb_keys,
        "verb_duplicate_filtered_count": verb_duplicate_filtered_count,
        "verb_source_summary": verb_source_summary,
        "verb_duplicate_filter": {
            "cooldown_days_requested": LOCAL_SELECTION_COOLDOWN_DAYS,
            "cooldown_days_used": verb_cooldown_days_used,
            "recent_duplicate_rejected_count": verb_recent_duplicate_rejected_count,
            "recent_used_count": len(verb_recent_keys_by_days.get(LOCAL_SELECTION_COOLDOWN_DAYS, set())),
            "selected_count": len(verbs),
            "insufficient_unique": len(verbs) < verb_count,
        },
        "verb_selection": {
            "strategy": "rotation_until_exhausted",
            "requested_count": verb_count,
            "selected_count": len(verbs),
            "pool_total_count": verb_pool_total_count,
            "eligible_pool_count": verb_eligible_pool_count,
            "recent_7_days_used_count": len(verb_recent_keys_by_days.get(LOCAL_SELECTION_COOLDOWN_DAYS, set())),
            "never_used_candidates": verb_never_used_candidates,
            "selected_from_never_used_count": verb_selected_from_never_used_count,
            "selected_from_oldest_used_count": verb_selected_from_oldest_used_count,
            "fallback_used": bool(verb_source_summary.get("seed_fallback", 0) or verb_source_summary.get("verbs", 0)),
            "stage_counts": verb_stage_counts,
            "pure_verb_count": pure_verb_count,
            "suru_verb_count": suru_verb_count,
            "suru_verb_limit": SURU_VERB_LIMIT_PER_MATERIAL,
            "excluded_suru_compound_count": excluded_suru_compound_count,
            "cooldown_days": LOCAL_SELECTION_COOLDOWN_DAYS,
            "source_counts": verb_source_summary,
            "recent_duplicate_rejected_count": verb_recent_duplicate_rejected_count,
            "cooldown_days_requested": LOCAL_SELECTION_COOLDOWN_DAYS,
            "cooldown_days_used": verb_cooldown_days_used,
            "selected_verbs": [first_text(item, ["surface", "dictionary_form", "base_form"]) for item in verbs],
            "candidates_from_db": verb_candidates_from_db,
            "candidates_from_seed": verb_candidates_from_seed,
        },
        "verb_distribution": {
            "requested_count": verb_count,
            "selected_count": len(verbs),
            "db_pure_verbs_count": verb_stage_counts.get("db_pure_verbs", 0),
            "refill_count": max(0, len(verbs) - verb_stage_counts.get("db_pure_verbs", 0)),
            "refill_stage_counts": {
                "pure_verb_safe_pool": verb_stage_counts.get("pure_verb_safe_pool", 0),
                "verbs_table": verb_stage_counts.get("verbs_table", 0),
            },
            "count_matched": len(verbs) >= verb_count,
        },
        "rejected_fake_suru_count": rejected_fake_suru_count,
        "excluded_suru_compound_count": excluded_suru_compound_count,
        "verb_candidate_count": verb_candidate_count,
        "seed_fallback_count": vocab_seed_fallback_count,
        "vocab_seed_fallback_count": vocab_seed_fallback_count,
        "verb_seed_fallback_count": verb_source_summary.get("verbs", 0) + verb_source_summary.get("seed_fallback", 0),
        "fallback_reason": "insufficient_vocab" if len(vocab) < vocab_count else ("insufficient_verbs" if (verb_source_summary.get("verbs", 0) or verb_source_summary.get("seed_fallback", 0)) else ""),
        "fallback": {
            "used": bool(vocab_seed_fallback_count),
            "count": vocab_seed_fallback_count,
        },
        "wrong_reviews": wrong_items,
        "quiz": quiz,
        "grammar_count": len(grammar_points),
        "grammar_level": grammar_level,
        "grammar_mode": "fixed_quota",
        "grammar_quota": grammar_stats.get("grammar_quota", grammar_quota),
        "grammar_selected_counts": grammar_stats.get("grammar_selected_counts", {}),
        "grammar_total_target": grammar_stats.get("grammar_total_target", grammar_count),
        "grammar_total_actual": grammar_stats.get("grammar_total_actual", len(grammar_points)),
        "grammar_fallback_warnings": grammar_stats.get("grammar_fallback_warnings", []),
        "grammar_keys": [item.get("grammar_key", "") for item in grammar_points if item.get("grammar_key")],
        "grammar_fallback_used": bool(grammar_stats.get("grammar_fallback_used")),
        "grammar_warnings": grammar_stats.get("grammar_warnings", []),
        "grammar_selection": grammar_stats.get("grammar_selection", {}),
        "grammar_duplicate_filter": grammar_stats.get("grammar_duplicate_filter", {}),
        "warnings": generation_warnings,
        "seed_used": seed_used,
        "generated_at": utc_now_iso(),
    }
    metadata.update(
        {
            "word_requested_count": vocab_count,
            "word_selected_count": len(vocab),
            "word_unfilled_slots": word_unfilled_slots,
            "word_reallocated_slots": word_reallocated_slots,
            "word_fallback_used": word_fallback_used,
            "word_fallback_reason": word_fallback_reason,
            "word_warnings": word_warnings,
            "verb_requested_count": verb_count,
            "verb_selected_count": len(verbs),
            "verb_clean_pool_count": verb_eligible_pool_count,
            "verb_seed_fallback_used": bool(verb_source_summary.get("seed_fallback", 0)),
            "verb_fallback_source": "seed_basic_verb_pool" if verb_source_summary.get("seed_fallback", 0) else "",
            "verb_fake_suru_rejected_count": rejected_fake_suru_count,
            "verb_warnings": ["insufficient_verbs_after_safe_fallback"] if len(verbs) < verb_count else [],
            "grammar_pool_total_by_level": grammar_stats.get("grammar_pool_total_by_level", {}),
            "pool_diagnostics": {
                "word": {
                    "requested": vocab_count,
                    "selected": len(vocab),
                    "unfilled_slots": word_unfilled_slots,
                    "reallocated_slots": word_reallocated_slots,
                    "fallback_used": word_fallback_used,
                    "fallback_reason": word_fallback_reason,
                    "warnings": list(word_warnings),
                },
                "verb": {
                    "requested": verb_count,
                    "selected": len(verbs),
                    "clean_pool_count": verb_eligible_pool_count,
                    "fake_suru_rejected_count": rejected_fake_suru_count,
                    "seed_fallback_used": bool(verb_source_summary.get("seed_fallback", 0)),
                    "fallback_source": "seed_basic_verb_pool" if verb_source_summary.get("seed_fallback", 0) else "",
                    "warnings": ["insufficient_verbs_after_safe_fallback"] if len(verbs) < verb_count else [],
                },
                "grammar": {
                    "quota": grammar_stats.get("grammar_quota", grammar_quota),
                    "pool_total_by_level": grammar_stats.get("grammar_pool_total_by_level", {}),
                    "selected_count_by_level": grammar_stats.get("grammar_selected_counts", {}),
                    "fallback_warnings": grammar_stats.get("grammar_fallback_warnings", []),
                },
            },
        }
    )
    metadata["generation_elapsed_ms"] = round((time.perf_counter() - material_started) * 1000)
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
        "[word-selector] "
        f"requested={vocab_count} "
        f"selected_from_rules={word_stage_counts.get('rules', 0)} "
        f"selected_from_target_safe_pool={word_stage_counts.get('target_level_safe_pool', 0)} "
        f"selected_from_adjacent_safe_pool={word_stage_counts.get('adjacent_level_safe_pool', 0)} "
        f"selected_from_enabled_safe_pool={word_stage_counts.get('enabled_levels_safe_pool', 0)} "
        f"selected_from_seed_pool={word_stage_counts.get('seed_basic_safe_pool', 0)} "
        f"final_selected={len(vocab)} "
        f"recent_duplicate_rejected={vocab_selector_stats.get('rejected_recent_duplicate_count', 0)} "
        f"insufficient_unique={str(len(vocab) < vocab_count).lower()}"
    )
    print(
        "[word-selector] strategy=rotation_until_exhausted "
        f"requested={vocab_count} "
        f"pool_total={vocab_selector_stats.get('pool_total_count', 0)} "
        f"eligible_pool={vocab_selector_stats.get('eligible_pool_count', 0)} "
        f"recent_7_days_used={vocab_selector_stats.get('recent_used_word_count', 0)} "
        f"selected_from_never_used={vocab_selector_stats.get('selected_from_never_used_count', 0)} "
        f"selected_from_oldest_used={vocab_selector_stats.get('selected_from_oldest_used_count', 0)} "
        f"final_selected={len(vocab)}"
    )
    print(
        "[material-generator] local verb sources "
        f"vocabulary_pool={verb_source_summary.get('vocabulary_pool', 0)} "
        f"vocabulary_pool_suru={verb_source_summary.get('vocabulary_pool_suru', 0)} "
        f"verbs={verb_source_summary.get('verbs', 0)} seed_fallback={verb_source_summary.get('seed_fallback', 0)} "
        f"duplicates={verb_duplicate_filtered_count}"
    )
    print(
        "[verb-selector] "
        f"requested={verb_count} "
        f"selected_from_db_pure_verbs={verb_stage_counts.get('db_pure_verbs', 0)} "
        f"selected_from_pure_verb_safe_pool={verb_stage_counts.get('pure_verb_safe_pool', 0)} "
        f"selected_from_verbs_table={verb_stage_counts.get('verbs_table', 0)} "
        f"candidates_from_db={verb_candidates_from_db} "
        f"candidates_from_seed={verb_candidates_from_seed} "
        f"recent_used_count={len(verb_recent_keys_by_days.get(LOCAL_SELECTION_COOLDOWN_DAYS, set()))} "
        f"rejected_recent_duplicate={verb_recent_duplicate_rejected_count} "
        f"cooldown_days_used={verb_cooldown_days_used} "
        f"selected_count={len(verbs)} "
        f"elapsed_ms={metadata['generation_elapsed_ms']}"
    )
    print(
        "[verb-selector] "
        f"cooldown_days={LOCAL_SELECTION_COOLDOWN_DAYS} "
        f"recent_used_verb_count={len(verb_recent_keys_by_days.get(LOCAL_SELECTION_COOLDOWN_DAYS, set()))} "
        f"selected_verb_count={len(verbs)} "
        f"insufficient_unique={str(len(verbs) < verb_count).lower()}"
    )
    print(
        "[verb-selector] "
        f"requested={verb_count} final_selected={len(verbs)} "
        f"pure_verb_count={pure_verb_count} suru_verb_count={suru_verb_count} "
        f"excluded_suru_compound_count={excluded_suru_compound_count} "
        f"cooldown_days={LOCAL_SELECTION_COOLDOWN_DAYS} "
        f"insufficient_unique={str(len(verbs) < verb_count).lower()}"
    )
    print(
        "[verb-selector] strategy=rotation_until_exhausted "
        f"requested={verb_count} "
        f"pool_total={verb_pool_total_count} "
        f"eligible_pool={verb_eligible_pool_count} "
        f"recent_7_days_used={len(verb_recent_keys_by_days.get(LOCAL_SELECTION_COOLDOWN_DAYS, set()))} "
        f"never_used_candidates={verb_never_used_candidates} "
        f"selected_from_never_used={verb_selected_from_never_used_count} "
        f"selected_from_oldest_used={verb_selected_from_oldest_used_count} "
        f"suru_verb_count={suru_verb_count} "
        f"final_selected={len(verbs)}"
    )
    return {
        "date": material_date_display(material_date or get_today_taipei_date()),
        "level": settings.get("target_level", ""),
        "target_levels": selected_target_levels,
        "grammar_level": grammar_level,
        "vocab": vocab,
        "verbs": verbs,
        "grammar": grammar,
        "grammar_points": grammar_points,
        "metadata": metadata,
    }


def get_next_material_version(material_date):
    ensure_database()
    date_iso = canonical_material_date(material_date)
    variants = material_date_variants(date_iso)
    if DATABASE_URL:
        placeholders = ", ".join(["%s"] * len(variants))
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT COALESCE(MAX(version_no), 0)
                    FROM materials
                    WHERE material_date = %s OR date IN ({placeholders})
                    """,
                    (date_iso, *variants),
                )
                current = cur.fetchone()[0] or 0
        return int(current) + 1
    df = read_database()
    rows = df[(df["material_date"].isin([date_iso])) | (df["date"].isin(variants))]
    if rows.empty:
        return 1
    versions = []
    for _, row in rows.iterrows():
        try:
            versions.append(int(row.get("version_no") or 0))
        except (TypeError, ValueError):
            continue
    return (max(versions) if versions else 0) + 1


def material_versions_for_date(material_date):
    date_iso = canonical_material_date(material_date)
    variants = material_date_variants(date_iso)
    versions = []
    if DATABASE_URL:
        placeholders = ", ".join(["%s"] * len(variants))
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT material_key, material_date, date, version_no, generation_source,
                           generation_mode, is_latest, created_at
                    FROM materials
                    WHERE (material_date = %s OR date IN ({placeholders}))
                      AND COALESCE(material_key, '') <> ''
                      AND COALESCE(material_json, '') <> ''
                    ORDER BY version_no ASC, created_at ASC, id ASC
                    """,
                    (date_iso, *variants),
                )
                rows = cur.fetchall()
        for row in rows:
            key, mat_date, date_value, version_no, source, mode, is_latest, created_at = row
            display_date = material_date_display(mat_date or date_value)
            versions.append(
                {
                    "material_key": key,
                    "material_date": canonical_material_date(mat_date or date_value),
                    "version_no": int(version_no or 1),
                    "display_label": f"{display_date.split('/', 1)[-1] if display_date.count('/') == 2 else display_date} -- {int(version_no or 1)}",
                    "generation_source": source or "",
                    "generation_mode": mode or "",
                    "is_latest": bool(is_latest),
                    "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
                }
            )
        return versions
    df = read_database()
    rows = df[
        ((df["material_date"].isin([date_iso])) | (df["date"].isin(variants)))
        & (df["material_key"].astype(str) != "")
        & (df["material_json"].astype(str) != "")
    ]
    for _, row in rows.iterrows():
        try:
            version_no = int(row.get("version_no") or 1)
        except (TypeError, ValueError):
            version_no = 1
        display_date = material_date_display(row.get("material_date") or row.get("date"))
        versions.append(
            {
                "material_key": row.get("material_key", ""),
                "material_date": canonical_material_date(row.get("material_date") or row.get("date")),
                "version_no": version_no,
                "display_label": f"{display_date.split('/', 1)[-1] if display_date.count('/') == 2 else display_date} -- {version_no}",
                "generation_source": row.get("generation_source", ""),
                "generation_mode": row.get("generation_mode", ""),
                "is_latest": str(row.get("is_latest", "")).lower() in {"true", "1", "t", "yes"},
                "created_at": row.get("created_at", ""),
            }
        )
    return sorted(versions, key=lambda item: item.get("version_no", 0))


def save_material_for_date(material_date, material, settings, generation_source="manual_local", generation_mode="local"):
    ensure_database()
    date_iso = canonical_material_date(material_date)
    date = material_date_display(date_iso)
    version_no = get_next_material_version(date_iso)
    material_key = build_material_key(date_iso, version_no)
    vocab_list = material.get("vocab") or []
    verb_list = material.get("verbs") or []
    grammar = material.get("grammar") or {}
    metadata = material.get("metadata") or {}
    now = utc_now_iso()
    metadata.update(
        {
            "material_key": material_key,
            "material_date": date_iso,
            "version_no": version_no,
            "generation_source": generation_source,
            "generation_mode": metadata.get("generation_mode") or generation_mode,
        }
    )
    material["metadata"] = metadata
    material["material_key"] = material_key
    material["material_date"] = date_iso
    material["version_no"] = version_no
    material["generation_source"] = generation_source
    material["generation_mode"] = metadata.get("generation_mode") or generation_mode
    material["date"] = date
    max_rows = 1
    selection_log_warnings = []

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
                "material_key": material_key,
                "material_date": date_iso,
                "version_no": version_no,
                "generation_source": generation_source,
                "generation_mode": metadata.get("generation_mode", "") if i == 0 else "",
                "is_latest": "true",
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
                print(f"[history-protection] append material version date={date_iso} material_key={material_key}")
                cur.execute("UPDATE materials SET is_latest = FALSE WHERE material_date = %s", (date_iso,))
                cur.executemany(f"INSERT INTO materials ({columns_sql}) VALUES ({placeholders})", rows)
            conn.commit()
        word_log_warning = best_effort_record_vocab_selection_logs(
            vocab_list,
            selected_for="word",
            material_date=date_iso,
            material_key=material_key,
            material_version_no=version_no,
        )
        if word_log_warning:
            selection_log_warnings.append(word_log_warning)
        verb_log_warning = best_effort_record_vocab_selection_logs(
            verb_list,
            selected_for="verb",
            material_date=date_iso,
            material_key=material_key,
            material_version_no=version_no,
        )
        if verb_log_warning:
            selection_log_warnings.append(verb_log_warning)
        grammar_log_warning = best_effort_record_grammar_selection(
            material.get("grammar_points") or [],
            date_iso,
            material_key=material_key,
            material_version_no=version_no,
        )
        if grammar_log_warning:
            selection_log_warnings.append(grammar_log_warning)
        invalidate_archive_dates_cache("daily material saved")
        return {
            "date": date,
            "material_date": date_iso,
            "material_key": material_key,
            "version_no": version_no,
            "generation_source": generation_source,
            "generation_mode": metadata.get("generation_mode", generation_mode),
            "selection_log_warnings": selection_log_warnings,
        }

    df = read_database()
    print(f"[history-protection] append material version date={date_iso} material_key={material_key}")
    df.loc[df["material_date"] == date_iso, "is_latest"] = "false"
    output = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
    output[COLUMNS].to_csv(DATABASE_FILE, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    word_log_warning = best_effort_record_vocab_selection_logs(
        vocab_list,
        selected_for="word",
        material_date=date_iso,
        material_key=material_key,
        material_version_no=version_no,
    )
    if word_log_warning:
        selection_log_warnings.append(word_log_warning)
    verb_log_warning = best_effort_record_vocab_selection_logs(
        verb_list,
        selected_for="verb",
        material_date=date_iso,
        material_key=material_key,
        material_version_no=version_no,
    )
    if verb_log_warning:
        selection_log_warnings.append(verb_log_warning)
    grammar_log_warning = best_effort_record_grammar_selection(
        material.get("grammar_points") or [],
        date_iso,
        material_key=material_key,
        material_version_no=version_no,
    )
    if grammar_log_warning:
        selection_log_warnings.append(grammar_log_warning)
    invalidate_archive_dates_cache("daily material saved")
    return {
        "date": date,
        "material_date": date_iso,
        "material_key": material_key,
        "version_no": version_no,
        "generation_source": generation_source,
        "generation_mode": metadata.get("generation_mode", generation_mode),
        "selection_log_warnings": selection_log_warnings,
    }


def save_material_for_today(material, settings):
    return save_material_for_date(get_today_taipei_date(), material, settings)


def material_from_rows(rows, target_date=None):
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
                normalize_material_verb_schema(
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
            )

    json_rows = rows[rows["material_json"].astype(str).str.strip() != ""] if "material_json" in rows.columns else rows
    first = json_rows.iloc[0] if not json_rows.empty else rows.iloc[0]
    try:
        examples = json.loads(first["grammar_examples"]) if first["grammar_examples"] else []
    except json.JSONDecodeError:
        examples = []
    material_payload = {}
    try:
        material_payload = json.loads(first.get("material_json", "") or "{}")
        metadata = material_payload.get("metadata", material_payload) if isinstance(material_payload, dict) else {}
    except json.JSONDecodeError:
        material_payload = {}
        metadata = {}
    if not metadata:
        metadata = {
            "generation_mode": first.get("generation_mode", ""),
            "ai_used": first.get("ai_used", ""),
            "source_summary": first.get("source_summary", ""),
        }
    if isinstance(material_payload, dict) and material_payload:
        payload_vocab = material_payload.get("vocab") or material_payload.get("vocabulary") or []
        if payload_vocab:
            vocabulary = [
                {
                    "word": item.get("word") or item.get("surface") or item.get("term") or "",
                    "reading": item.get("reading") or item.get("reading_hiragana") or "",
                    "meaning": item.get("meaning") or item.get("meaning_zh") or "尚未建立中文意思",
                    "part_of_speech": item.get("part_of_speech", ""),
                    "source": item.get("source", ""),
                    "jlpt_level": item.get("jlpt_level", ""),
                    "category": item.get("category", ""),
                    "normalized_key": item.get("normalized_key", ""),
                    "example_sentence": item.get("example_sentence", ""),
                    "example_translation_zh": item.get("example_translation_zh", ""),
                }
                for item in payload_vocab
            ]
        payload_verbs = material_payload.get("verbs") or []
        if payload_verbs:
            verbs = [normalize_material_verb_schema(item) for item in payload_verbs]
    resolved_settings = metadata.get("resolved_settings") if isinstance(metadata.get("resolved_settings"), dict) else {}
    count_validation = metadata.get("count_validation") if isinstance(metadata.get("count_validation"), dict) else {}
    target_level_value = resolved_settings.get("target_level") or first.get("target_level", "")

    date_iso = canonical_material_date(first.get("material_date") or first.get("date") or target_date)
    try:
        version_no = int(first.get("version_no") or metadata.get("version_no") or 1)
    except (TypeError, ValueError):
        version_no = 1
    material_key = first.get("material_key") or metadata.get("material_key") or build_material_key(date_iso, version_no)
    available_versions = material_versions_for_date(date_iso)
    return {
        "date": material_date_display(first.get("date", target_date)),
        "date_iso": date_iso,
        "material_date": date_iso,
        "material_key": material_key,
        "version_no": version_no,
        "is_latest": str(first.get("is_latest", "")).lower() in {"true", "1", "t", "yes"},
        "generation_source": first.get("generation_source", "") or metadata.get("generation_source", ""),
        "generation_mode": first.get("generation_mode", "") or metadata.get("generation_mode", ""),
        "available_versions": available_versions,
        "targetLevel": target_level_value,
        "vocabulary": vocabulary,
        "verbs": verbs,
        "grammar": {"title": first["grammar_title"], "exp": first["grammar_exp"], "examples": examples},
        "grammar_points": material_payload.get("grammar_points", []) if isinstance(material_payload, dict) else [],
        "grammarLevel": metadata.get("grammar_level", ""),
        "resolved_settings": resolved_settings,
        "count_validation": count_validation,
        "metadata": metadata,
    }


def material_by_date(target_date):
    return material_from_rows(read_material_rows_by_date(target_date), target_date)


def material_by_key(material_key):
    return material_from_rows(read_material_rows_by_key(material_key), None)


def get_material_by_date(material_date):
    return material_by_date(material_date)


def build_telegram_notification(material, date, app_url=None):
    if not material:
        raise RuntimeError("教材尚未寫入資料庫，無法推送 Telegram。")
    material_key = material.get("material_key", "")
    base_link = app_url or APP_URL
    if material_key:
        separator = "&" if "?" in base_link else "?"
        base_link = f"{base_link}{separator}material_key={urllib.parse.quote(str(material_key))}"
    link = html.escape(base_link)
    words = "、".join(html.escape(v.get("word", "")) for v in material.get("vocabulary", []) if v.get("word"))
    grammar_points = material.get("grammar_points") or []
    if grammar_points:
        grammar_title = "\n".join(
            f"・{html.escape(item.get('display_name') or item.get('title') or item.get('grammar_key') or '')}"
            for item in grammar_points[:5]
            if item.get("display_name") or item.get("title") or item.get("grammar_key")
        ) or html.escape(material.get("grammar", {}).get("title", "今日文法"))
    else:
        grammar_title = html.escape(material.get("grammar", {}).get("title", "今日文法"))
    version_no = material.get("version_no")
    source = material.get("generation_source", "")
    date_label = html.escape(f"{date} -- {version_no}" if version_no else str(date))
    return (
        f"<b>日語學習自動化系統</b>\n"
        f"日期：{date_label}\n"
        f"來源：{html.escape(source or 'local')}\n\n"
        f"<b>今日單字：</b>{words or '暫無'}\n"
        f"<b>今日文法：</b>\n{grammar_title}\n\n"
        f'<a href="{link}">點擊開啟學習頁面</a>'
    )


def send_telegram_message(text, timeout_seconds=5):
    if not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("Telegram Token 或 Chat ID 尚未設定。")

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"Telegram 回傳錯誤：{data}")
    return data


def _safe_send_telegram_message(text, context="telegram"):
    started = time.perf_counter()
    try:
        send_telegram_message(text)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        print(f"[telegram] async_send_success context={context} elapsed_ms={elapsed_ms}")
    except BaseException as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        print(f"[telegram] async_send_failed context={context} elapsed_ms={elapsed_ms} reason={exc}")


def send_telegram_message_async(text, context="telegram"):
    if not TG_TOKEN or not TG_CHAT_ID:
        print(f"[telegram] async_send_skipped context={context} reason=missing_token_or_chat_id")
        return False
    thread = threading.Thread(
        target=_safe_send_telegram_message,
        args=(text, context),
        name=f"telegram-send-{context}",
        daemon=True,
    )
    thread.start()
    print(f"[telegram] async_send_queued context={context}")
    return True


def normalize_generation_mode(value):
    mode = str(value or "local").strip().lower()
    return mode if mode in {"local", "gemini", "ai_enhance", "ai_full"} else "local"


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
    word_selection = metadata.get("word_selection") or {}
    requested_words = int(word_selection.get("requested_count") or 0)
    selected_words = int(word_selection.get("selected_count") or 0)
    verb_selection = metadata.get("verb_selection") or {}
    requested_verbs = int(verb_selection.get("requested_count") or 0)
    selected_verbs = int(verb_selection.get("selected_count") or 0)
    partial_parts = []
    if requested_words and selected_words < requested_words:
        partial_parts.append(f"單字 {selected_words} / {requested_words}")
    if requested_verbs and selected_verbs < requested_verbs:
        partial_parts.append(f"動詞 {selected_verbs} / {requested_verbs}")
    if partial_parts:
        base += f" 今日教材已建立，但部分內容因 7 天不重複與安全詞庫限制未補滿：{'、'.join(partial_parts)}。"
    return f"{base} {telegram_status}"


def generate_daily_material(
    use_sample=False,
    posted_settings=None,
    app_url=None,
    mode="local",
    material_date=None,
    notify_telegram=True,
    generation_source="manual_local",
    generation_trigger="manual",
    deadline_check=None,
):
    settings, settings_source, db_settings = resolve_generation_settings_with_trace(posted_settings, persist=bool(posted_settings))
    if deadline_check:
        deadline_check("resolve_settings_after")
    mode = "local" if use_sample else normalize_generation_mode(mode)
    print(f"[material-generator] mode={mode} start")
    print(
        "[material-generator] requested "
        f"generation_trigger={generation_trigger} "
        f"target_levels={','.join(settings_target_levels(settings))} "
        f"target_level={settings.get('target_level')} "
        f"word_count={settings.get('vocab_count')} "
        f"verb_count={settings.get('verb_count')} "
        f"choice_count={settings.get('mcq_count')} "
        f"fill_count={settings.get('fill_count')} "
        f"grammar_level={settings.get('grammar_level')} "
        f"grammar_count={settings.get('grammar_count')} "
        f"settings_source={settings_source}"
    )
    print(
        "[generate-start] resolved "
        f"word_count={settings.get('vocab_count')} "
        f"verb_count={settings.get('verb_count')} "
        f"settings_source={settings_source}"
    )

    if mode == "local":
        print("[feature-boundary] daily_material mode=local skip gemini")
        if deadline_check:
            deadline_check("build_material_before")
        raw_material = build_local_material(
            settings,
            force_seed=use_sample,
            material_date=material_date or get_today_taipei_date(),
            deadline_check=deadline_check,
        )
        if deadline_check:
            deadline_check("build_material_after")
    elif mode == "gemini":
        print("[feature-boundary] daily_material mode=gemini use gemini daily material generator")
        if deadline_check:
            deadline_check("gemini_material_before")
        raw_material = build_gemini_daily_material(settings, deadline_check=deadline_check)
        if deadline_check:
            deadline_check("gemini_material_after")
    elif mode == "ai_enhance":
        raw_material = build_local_material(settings, material_date=material_date or get_today_taipei_date(), deadline_check=deadline_check)
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
            raw_material = build_local_material(settings, material_date=material_date or get_today_taipei_date(), deadline_check=deadline_check)
            raw_material["metadata"]["generation_mode"] = "local"
            raw_material["metadata"]["ai_used"] = False
            raw_material["metadata"]["fallback_used"] = True

    if mode == "local" and raw_material.get("metadata", {}).get("ai_used"):
        print("[material-generator] ERROR local mode attempted to call Gemini")

    requested_words = int(settings.get("vocab_count") or 0)
    requested_verbs = int(settings.get("verb_count") or 0)
    actual_words = len(raw_material.get("vocab") or raw_material.get("words") or [])
    actual_verbs = len(raw_material.get("verbs") or [])
    count_warnings = []
    if actual_words < requested_words:
        count_warnings.append("scheduled_word_count_not_matched" if generation_trigger == "cron" else "word_count_not_matched")
    if actual_verbs < requested_verbs:
        count_warnings.append("scheduled_verb_count_not_matched" if generation_trigger == "cron" else "verb_count_not_matched")
    count_validation = {
        "target_level_requested": settings.get("target_level", ""),
        "target_level_actual": raw_material.get("level") or settings.get("target_level", ""),
        "word_count_requested": requested_words,
        "word_count_actual": actual_words,
        "verb_count_requested": requested_verbs,
        "verb_count_actual": actual_verbs,
        "word_count_matched": actual_words >= requested_words,
        "verb_count_matched": actual_verbs >= requested_verbs,
        "warnings": count_warnings,
    }
    settings_trace = build_settings_trace(
        posted_settings or {},
        db_settings,
        settings,
        settings_source,
        selector_actual={"word_count": actual_words, "verb_count": actual_verbs},
    )
    raw_material.setdefault("metadata", {})
    raw_material["metadata"]["generation_trigger"] = generation_trigger
    raw_material["metadata"]["settings_source"] = settings_source
    grammar_mode = raw_material["metadata"].get("grammar_mode") or raw_material["metadata"].get("resolved_settings", {}).get("grammar_mode", "")
    grammar_level_for_metadata = "fixed_quota" if grammar_mode == "fixed_quota" else settings.get("grammar_level", "")
    grammar_count_for_metadata = (
        int(raw_material["metadata"].get("grammar_total_target") or LOCAL_FIXED_GRAMMAR_TOTAL)
        if grammar_mode == "fixed_quota"
        else int(settings.get("grammar_count") or 0)
    )
    raw_material["metadata"].setdefault("resolved_settings", {}).update(
        {
            "generation_trigger": generation_trigger,
            "settings_source": settings_source,
            "target_level": settings.get("target_level", ""),
            "target_levels": settings_target_levels(settings),
            "word_count": requested_words,
            "verb_count": requested_verbs,
            "choice_count": int(settings.get("mcq_count") or 0),
            "fill_count": int(settings.get("fill_count") or 0),
            "grammar_level": grammar_level_for_metadata,
            "grammar_count": grammar_count_for_metadata,
            "grammar_mode": grammar_mode or "settings",
            "grammar_quota": raw_material["metadata"].get("grammar_quota", {}),
            "grammar_total_target": raw_material["metadata"].get("grammar_total_target", grammar_count_for_metadata),
        }
    )
    raw_material["metadata"]["count_validation"] = count_validation
    raw_material["metadata"]["settings_trace"] = settings_trace
    print(f"[count-validation] requested_words={requested_words} actual_words={actual_words} word_count_matched={str(actual_words >= requested_words).lower()}")
    print(f"[count-validation] requested_verbs={requested_verbs} actual_verbs={actual_verbs} verb_count_matched={str(actual_verbs >= requested_verbs).lower()}")

    if deadline_check:
        deadline_check("save_material_before")
    save_info = save_material_for_date(
        material_date or get_today_taipei_date(),
        raw_material,
        settings,
        generation_source=generation_source,
        generation_mode=raw_material.get("metadata", {}).get("generation_mode", mode),
    )
    if deadline_check:
        deadline_check("save_material_after")
    date = save_info["date"]
    print(f"[material-generator] local material generated; ai_used={str(raw_material.get('metadata', {}).get('ai_used', False)).lower()}")
    print(f"[material-generator] material saved date={date} material_key={save_info['material_key']}")
    print(
        "[material-save] "
        f"material_key={save_info['material_key']} "
        f"version_no={save_info['version_no']} "
        f"resolved word_count={requested_words} "
        f"verb_count={requested_verbs}"
    )
    material = material_by_key(save_info["material_key"])
    if not material:
        raise RuntimeError(f"教材寫入後重新讀取失敗：{save_info['material_key']}")

    telegram_status = "未發送"
    if deadline_check:
        deadline_check("telegram_before")
    telegram_dispatched_async = False
    telegram_delivery = "web_async" if notify_telegram else "external_cron"
    try:
        telegram_message = build_telegram_notification(material, date, app_url)
    except Exception as e:
        telegram_message = ""
        telegram_status = f"Telegram notification build failed: {e}"
    if notify_telegram and telegram_message:
        try:
            telegram_dispatched_async = send_telegram_message_async(
                telegram_message,
                context=f"{generation_trigger}:{save_info['material_key']}",
            )
            telegram_status = "Telegram 通知已排入背景發送"
        except Exception as e:
            telegram_status = f"Telegram 通知發送失敗：{e}"

    if not notify_telegram:
        telegram_status = "Telegram delivery delegated to cron_generate.py"

    invalidate_dashboard_cache("daily material generated")
    return {
        "ok": True,
        "message": f"{material_success_message(date, settings, raw_material, telegram_status)} 新版本：{save_info['material_key']}。",
        "date": date,
        "material_date": save_info["material_date"],
        "material_key": save_info["material_key"],
        "version_no": save_info["version_no"],
        "generation_source": save_info["generation_source"],
        "generation_trigger": generation_trigger,
        "telegram": telegram_status,
        "telegram_dispatched_async": telegram_dispatched_async,
        "telegram_delivery": telegram_delivery,
        "telegram_message": telegram_message,
        "notification_message": telegram_message,
        "generation_mode": raw_material.get("metadata", {}).get("generation_mode", mode),
        "ai_used": bool(raw_material.get("metadata", {}).get("ai_used", False)),
        "fallback_used": bool(raw_material.get("metadata", {}).get("fallback_used", False)),
        "source_summary": raw_material.get("metadata", {}).get("source_summary", {}),
        "settings_source": settings_source,
        "resolved_settings": raw_material["metadata"].get("resolved_settings", {}),
        "count_validation": count_validation,
        "settings_trace": settings_trace,
        "pool_diagnostics": raw_material["metadata"].get("pool_diagnostics", {}),
        "selection_log_warnings": save_info.get("selection_log_warnings", []),
    }


def run_daily_schedule(app_url=None, mode="local", notify_telegram=True):
    date = get_today_taipei_date()
    print(f"[daily-schedule] start date={date}")
    try:
        schedule_settings = load_settings()
        print(
            "[scheduled-material-generator] db_settings "
            f"target_level={schedule_settings.get('target_level')} "
            f"target_levels={','.join(settings_target_levels(schedule_settings))} "
            f"word_count={schedule_settings.get('vocab_count')} "
            f"verb_count={schedule_settings.get('verb_count')} "
            f"choice_count={schedule_settings.get('mcq_count')} "
            f"fill_count={schedule_settings.get('fill_count')} "
            f"grammar_level={schedule_settings.get('grammar_level')} "
            f"grammar_count={schedule_settings.get('grammar_count')}"
        )
        scheduled_key = latest_material_key_for_date(date, generation_source="scheduled")
        material = material_by_key(scheduled_key) if scheduled_key else None
        print(f"[daily-schedule] material exists={str(bool(material)).lower()} generation_source=scheduled")
        if material and not material_matches_generation_settings(material, schedule_settings):
            print(
                "[scheduled-material-generator] existing scheduled material does not match current settings; "
                f"will create new version old_material_key={material.get('material_key')}"
            )
            material = None
        if not material:
            print(f"[daily-schedule] generating local material date={date}")
            try:
                result = generate_daily_material(
                    app_url=app_url,
                    mode=mode,
                    material_date=date,
                    generation_source="scheduled",
                    generation_trigger="cron",
                    notify_telegram=notify_telegram,
                )
            except Exception as generation_exc:
                print(
                    "[scheduled-material-generator] primary generation failed; "
                    f"trying seed fallback reason={generation_exc}"
                )
                print(traceback.format_exc())
                result = generate_daily_material(
                    use_sample=True,
                    app_url=app_url,
                    mode="local",
                    material_date=date,
                    generation_source="scheduled",
                    generation_trigger="cron",
                    notify_telegram=notify_telegram,
                )
                result["fallback_used"] = True
                result["primary_error"] = str(generation_exc)
            print(f"[daily-schedule] save material success date={date} material_key={result.get('material_key')}")
            print(f"[daily-schedule] reload material from db success date={date} material_key={result.get('material_key')}")
            print(
                "[daily-schedule] telegram delivery "
                f"mode={result.get('telegram_delivery')} date={date} material_key={result.get('material_key')}"
            )
            return result
        print(f"[daily-schedule] reload material from db success date={date} material_key={material.get('material_key')}")
        telegram_dispatched_async = False
        telegram_delivery = "web_async" if notify_telegram else "external_cron"
        telegram_message = ""
        try:
            telegram_message = build_telegram_notification(material, date, app_url)
        except Exception as telegram_exc:
            print(
                "[daily-schedule] telegram message build failed "
                f"date={date} material_key={material.get('material_key')} reason={telegram_exc}"
            )
        if notify_telegram and telegram_message:
            try:
                telegram_dispatched_async = send_telegram_message_async(
                    telegram_message,
                    context=f"cron-existing:{material.get('material_key')}",
                )
            except Exception as telegram_exc:
                print(
                    "[daily-schedule] telegram queue failed "
                    f"date={date} material_key={material.get('material_key')} reason={telegram_exc}"
                )
        telegram_status = "Telegram 通知已排入背景發送" if telegram_dispatched_async else "Telegram 通知未排入背景發送"
        print(
            "[daily-schedule] telegram delivery "
            f"mode={telegram_delivery} date={date} material_key={material.get('material_key')}"
        )
        invalidate_dashboard_cache("daily schedule material ready")
        return {
            "ok": True,
            "message": f"{date} 的學習材料已確認落地，Telegram 已推送。",
            "date": date,
            "material_date": material.get("material_date") or canonical_material_date(date),
            "material_key": material.get("material_key"),
            "version_no": material.get("version_no"),
            "generation_source": material.get("generation_source", "scheduled"),
            "generation_mode": material.get("metadata", {}).get("generation_mode", "local"),
            "ai_used": bool(material.get("metadata", {}).get("ai_used", False)),
            "telegram_dispatched_async": telegram_dispatched_async,
            "telegram_delivery": telegram_delivery,
            "telegram_message": telegram_message,
            "notification_message": telegram_message,
            "telegram": telegram_status,
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


def smart_answer_equal(user_input, correct_answer, accepted_answers=None):
    user_clean = clean_answer_value(user_input)
    correct_clean = clean_answer_value(correct_answer)
    if not user_clean or not correct_clean:
        return False
    if user_clean == correct_clean:
        return True
    for accepted in accepted_answers or []:
        accepted_clean = clean_answer_value(accepted)
        if accepted_clean and user_clean == accepted_clean:
            return True
    user_reading = answer_reading_hiragana(user_clean)
    correct_reading = answer_reading_hiragana(correct_clean)
    if user_reading and correct_reading and user_reading == correct_reading:
        return True
    for accepted in accepted_answers or []:
        accepted_reading = answer_reading_hiragana(accepted)
        if user_reading and accepted_reading and user_reading == accepted_reading:
            return True
    return False


def practice_verb_group(value):
    try:
        group = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return group if group in {1, 2, 3} else 0


def normalize_practice_question_type(question_type):
    key = str(question_type or "").strip()
    return PRACTICE_QUESTION_TYPE_ALIASES.get(key, key)


def practice_form_aliases(question_type):
    key = normalize_practice_question_type(question_type)
    return PRACTICE_CANONICAL_FORM_ALIASES.get(key, (key,))


def infer_practice_verb_group(base_form, base_reading=""):
    base = clean_answer_value(base_form)
    reading = clean_answer_value(base_reading)
    if not base:
        return 0
    if base in {"する", "来る", "くる"} or base.endswith("する"):
        return 3
    if base.endswith("る"):
        reading_or_base = reading if reading.endswith("る") else base
        if base in MATERIAL_GODAN_RU_EXCEPTIONS:
            return 1
        if base in MATERIAL_ICHIDAN_RU_VERBS:
            return 2
        previous = reading_or_base[-2] if len(reading_or_base) >= 2 else ""
        return 2 if previous in MATERIAL_ICHIDAN_PRECEDING_KANA else 1
    return 1 if base[-1:] in MATERIAL_GODAN_FORMS else 0


PRACTICE_GODAN_FORMS = {
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


def conjugate_practice_verb_base(base_form, verb_group, base_reading=""):
    base = clean_answer_value(base_form)
    group = practice_verb_group(verb_group) or infer_practice_verb_group(base, base_reading)
    if not base:
        return {}
    generated = conjugate_material_verb(base, group) if group else None
    if not isinstance(generated, dict):
        return {}
    forms = {}
    for key, value in generated.items():
        clean = clean_answer_value(value)
        if clean and clean != NO_VERB_FORM:
            forms[key] = clean
    alias_pairs = {
        "masu_stem": "renyou_form",
        "causative_form": "shieki_form",
        "passive_form": "ukemi_form",
    }
    for source_key, target_key in alias_pairs.items():
        if source_key in forms:
            forms[target_key] = forms[source_key]
    return forms


def surface_variant_from_reading_answer(correct_answer, base_surface, base_reading):
    correct = clean_answer_value(correct_answer)
    surface = clean_answer_value(base_surface)
    reading = clean_answer_value(base_reading)
    if not correct or not surface or not reading or surface == reading:
        return ""
    stems = []
    if reading.endswith("する") and surface.endswith("する"):
        stems.append((reading[:-2], surface[:-2]))
    if reading.endswith("る") and surface.endswith("る"):
        stems.append((reading[:-1], surface[:-1]))
    if len(reading) >= 2 and len(surface) >= 2:
        stems.append((reading[:-1], surface[:-1]))
    stems.append((reading, surface))
    for reading_stem, surface_stem in stems:
        if reading_stem and correct.startswith(reading_stem):
            return clean_answer_value(f"{surface_stem}{correct[len(reading_stem):]}")
    return ""


def unique_answer_list(values):
    answers = []
    seen = set()
    for value in values or []:
        clean = clean_answer_value(value)
        if clean and clean != NO_VERB_FORM and clean not in seen:
            answers.append(clean)
            seen.add(clean)
    return answers


def build_accepted_verb_answers(base_surface, base_reading, conjugation_type, verb_group=None, stored_answer=None, source=None):
    answers = []
    source = source or {}
    key = normalize_practice_question_type(conjugation_type)
    aliases = practice_form_aliases(key)

    def add(value):
        clean = clean_answer_value(value)
        if clean and clean != NO_VERB_FORM:
            answers.append(clean)

    add(stored_answer)
    for alias in aliases:
        add(source.get(alias))

    base_surface = clean_answer_value(base_surface)
    base_reading = clean_answer_value(base_reading)
    group = practice_verb_group(verb_group) or infer_practice_verb_group(base_surface, base_reading)
    generated_surface = conjugate_practice_verb_base(base_surface, group, base_reading)
    generated_reading = conjugate_practice_verb_base(base_reading, group)
    for alias in aliases:
        add(generated_surface.get(alias))
        add(generated_reading.get(alias))

    for answer in list(answers):
        add(surface_variant_from_reading_answer(answer, base_surface, base_reading))
    return unique_answer_list(answers)


def accepted_verb_form_answers(verb, question_type, correct_answer):
    verb = verb or {}
    return build_accepted_verb_answers(
        verb.get("dictionary_form", ""),
        verb.get("reading", ""),
        question_type,
        verb_group=verb.get("verb_group"),
        stored_answer=correct_answer,
        source=verb,
    )


def primary_accepted_answer(accepted_answers, correct_answer=""):
    answers = unique_answer_list(accepted_answers)
    for answer in answers:
        if contains_kanji(answer):
            return answer
    return answers[0] if answers else clean_answer_value(correct_answer)


def accepted_answer_display(accepted_answers, correct_answer=""):
    answers = unique_answer_list(accepted_answers)
    primary = primary_accepted_answer(answers, correct_answer)
    kana = next((answer for answer in answers if is_kana_reading(answer)), "")
    if primary and contains_kanji(primary):
        if kana and kana != primary:
            return f"{primary}（{kana}）"
        reading = answer_reading_hiragana(primary)
        if reading and reading != primary:
            return f"{primary}（{reading}）"
    return answer_display_value(correct_answer or primary, primary, answers)


def verb_answer_context(verb, question_type, correct_answer):
    accepted_answers = accepted_verb_form_answers(verb, question_type, correct_answer)
    primary = primary_accepted_answer(accepted_answers, correct_answer)
    return {
        "accepted_answers": accepted_answers,
        "primary_answer": primary,
        "display_answer": accepted_answer_display(accepted_answers, correct_answer),
    }


def answer_display_value(correct_answer, preferred_answer=None, accepted_answers=None):
    clean = clean_answer_value(correct_answer)
    preferred = clean_answer_value(preferred_answer)
    if preferred and contains_kanji(preferred) and smart_answer_equal(preferred, clean, accepted_answers):
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


MISTAKE_MASTERY_CORRECT_THRESHOLD = 2


def mistake_due_where(alias="m"):
    return (
        f"COALESCE({alias}.mastered, 0) = 0 "
        f"AND COALESCE(NULLIF({alias}.status, ''), 'review_due') IN ('review_due', 'learning') "
        f"AND COALESCE(NULLIF({alias}.review_due_at, ''), NULLIF({alias}.next_review_date, ''), "
        f"NULLIF({alias}.last_reviewed_at, ''), ?) <= ?"
    )


def mistake_due_params():
    now = taipei_iso_now()
    return (now, now)


def mistake_created_date_expr(alias="m"):
    prefix = f"{alias}." if alias else ""
    return (
        f"substr(COALESCE(NULLIF({prefix}first_wrong_at, ''), NULLIF({prefix}created_at, ''), "
        f"NULLIF({prefix}last_reviewed_at, '')), 1, 10)"
    )


def add_mistake(verb_id, question_type, wrong_answer, error_category="動詞變化錯"):
    now = datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds")
    due_date = today_iso_date()
    category = normalize_error_category(error_category)
    target = sqlite_one("SELECT * FROM verbs WHERE id = ?", (verb_id,)) if int(verb_id or 0) > 0 else None
    correct_answer = target.get(question_type, "") if target and question_type in target else ""
    prompt = ""
    accepted_answers = []
    primary_answer = clean_answer_value(correct_answer)
    if target:
        prompt = f"請寫出「{target['dictionary_form']}（{target['reading']}）」的{VERB_FORM_LABELS.get(question_type, question_type)}。"
        context = verb_answer_context(target, question_type, correct_answer)
        accepted_answers = context["accepted_answers"]
        primary_answer = context["primary_answer"]
    accepted_answers_json = json.dumps(accepted_answers, ensure_ascii=False)
    report = make_debug_report_payload(
        question_type=question_type,
        prompt=prompt,
        user_answer=wrong_answer,
        correct_answer=primary_answer or correct_answer,
        target_text=target.get("dictionary_form", "") if target else "",
        target_reading=target.get("reading", "") if target else "",
        target_form=question_type,
        error_category=category,
        extra={
            "question_text": prompt,
            "base_surface": target.get("dictionary_form", "") if target else "",
            "base_reading": target.get("reading", "") if target else "",
            "conjugation_type": question_type,
            "primary_answer": primary_answer,
            "accepted_answers": accepted_answers,
        },
    )
    report_json = debug_report_to_json(report)
    explanation = " ".join(filter(None, [report.get("diagnosis", ""), report.get("rule", "")]))
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
                    review_due_at = ?,
                    review_interval = 1,
                    mastered = 0,
                    status = 'review_due',
                    last_wrong_at = ?,
                    updated_at = ?,
                    correct_count = COALESCE(correct_count, 0),
                    error_category = ?,
                    error_type = 'verb_conjugation_wrong',
                    debug_report_json = ?,
                    question_text = ?,
                    prompt = ?,
                    base_surface = ?,
                    base_reading = ?,
                    conjugation_type = ?,
                    primary_answer = ?,
                    accepted_answers_json = ?,
                    explanation = ?
                WHERE id = ?
                """,
                (
                    " / ".join(answers),
                    int(existing["mistake_count"]) + 1,
                    now,
                    due_date,
                    now,
                    now,
                    now,
                    category,
                    report_json,
                    prompt,
                    prompt,
                    target.get("dictionary_form", "") if target else "",
                    target.get("reading", "") if target else "",
                    question_type,
                    primary_answer,
                    accepted_answers_json,
                    explanation,
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO mistake_logs
                (
                    verb_id, question_type, user_wrong_answer, mistake_count,
                    status, last_reviewed_at, next_review_date, review_interval,
                    review_count, mastered, error_category, debug_report_json,
                    question_text, base_surface, base_reading, conjugation_type,
                    primary_answer, accepted_answers_json, explanation,
                    created_at, updated_at, first_wrong_at, last_wrong_at,
                    review_due_at, correct_count, question_id, error_type, prompt
                )
                VALUES (?, ?, ?, 1, 'review_due', ?, ?, 1, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, 0, ?, 'verb_conjugation_wrong', ?)
                """,
                (
                    verb_id,
                    question_type,
                    wrong_answer,
                    now,
                    due_date,
                    category,
                    report_json,
                    prompt,
                    target.get("dictionary_form", "") if target else "",
                    target.get("reading", "") if target else "",
                    question_type,
                    primary_answer,
                    accepted_answers_json,
                    explanation,
                    now,
                    now,
                    now,
                    now,
                    now,
                    f"verb:{verb_id}:{question_type}",
                    prompt,
                ),
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
        "original_text": "",
        "reading_hiragana_full": "",
        "natural_translation": "",
        "sentence_summary": "",
        "overall_core_meaning": "",
        "naturalness_check": {
            "is_natural": False,
            "level": "",
            "reason": "",
            "suggested_sentence": "",
        },
        "hiragana_reading": "",
        "tone": {"label": "", "explanation": ""},
        "sentence_breakdown": [],
        "sentence_structure": [],
        "grammar_points": [],
        "vocabulary_notes": [],
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


def grammar_failure_message(reason):
    messages = {
        "quota_exceeded": "\u89e3\u6790\u5931\u6557\uff1aGemini \u984d\u5ea6\u6216\u8acb\u6c42\u914d\u984d\u4e0d\u8db3\u3002",
        "prepayment_depleted": "\u89e3\u6790\u5931\u6557\uff1aGemini \u9810\u4ed8\u984d\u5ea6\u4e0d\u8db3\u6216\u5e33\u52d9\u4fdd\u8b77\u4e2d\u3002",
        "missing_api_key": "\u89e3\u6790\u5931\u6557\uff1a\u7cfb\u7d71\u672a\u8b80\u5230 Gemini API key\u3002",
        "timeout": "\u89e3\u6790\u5931\u6557\uff1aGemini \u56de\u61c9\u903e\u6642\uff0c\u8acb\u7e2e\u77ed\u8f38\u5165\u6216\u7a0d\u5f8c\u518d\u8a66\u3002",
        "json_parse_error": "\u89e3\u6790\u5931\u6557\uff1aAI \u56de\u50b3\u683c\u5f0f\u7570\u5e38\uff0c\u7cfb\u7d71\u7121\u6cd5\u89e3\u6790 JSON\u3002",
        "model_error": "\u89e3\u6790\u5931\u6557\uff1aGemini \u6a21\u578b\u56de\u61c9\u7570\u5e38\u3002",
        "permission_denied": "\u89e3\u6790\u5931\u6557\uff1aGemini API \u6b0a\u9650\u4e0d\u8db3\u3002",
        "not_found": "\u89e3\u6790\u5931\u6557\uff1a\u6307\u5b9a\u7684 Gemini \u6a21\u578b\u4e0d\u5b58\u5728\u6216\u4e0d\u53ef\u7528\u3002",
        "input_too_long": "\u8f38\u5165\u6587\u5b57\u904e\u9577\uff0c\u8acb\u7e2e\u77ed\u5f8c\u518d\u8a66\u3002",
    }
    return messages.get(reason, "\u89e3\u6790\u5931\u6557\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66\u3002")


def annotate_grammar_payload(payload, ok, source, reason="", model="", diagnostic=None):
    payload = payload if isinstance(payload, dict) else grammar_response_template("japanese")
    payload["ok"] = bool(ok)
    payload["source"] = source
    payload["model"] = model or ""
    payload["reason"] = reason or ""
    if ok:
        payload["result"] = {
            "original_text": payload.get("original_text") or payload.get("original", ""),
            "reading_hiragana_full": payload.get("reading_hiragana_full") or payload.get("hiragana_reading", ""),
            "natural_translation_zh": payload.get("natural_translation", ""),
            "overall_core_meaning": payload.get("overall_core_meaning") or payload.get("sentence_summary", ""),
            "sentence_breakdown": payload.get("sentence_breakdown", []),
            "sentence_structure": payload.get("sentence_structure", []),
            "grammar_points": payload.get("grammar_points", []),
            "vocabulary_notes": payload.get("vocabulary_notes", []) or payload.get("slang_terms", []),
            "nuance": (payload.get("tone") or {}).get("explanation", "") if isinstance(payload.get("tone"), dict) else "",
            "learning_tips": (payload.get("learning_focus") or {}).get("tips", []) if isinstance(payload.get("learning_focus"), dict) else [],
        }
    else:
        message = grammar_failure_message(reason)
        payload["error"] = "grammar_analysis_failed"
        payload["message"] = message
        payload["fallback_reason"] = reason or "unknown_error"
        payload["error_message"] = payload.get("error_message") or message
        payload["debug"] = diagnostic or {}
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
            "original_text": text,
            "reading_hiragana_full": hiragana_reading or "",
            "overall_core_meaning": "Gemini 解析暫時失敗，目前僅回傳本地規則偵測結果。",
            "sentence_breakdown": [],
            "advanced_mecab": advanced_mecab or {},
        }
    )
    return payload


def is_probably_japanese_text(text):
    return bool(re.search(r"[ぁ-ゖァ-ヺー]", text or ""))


def split_japanese_sentences(text):
    text = str(text or "").strip()
    if not text:
        return []
    parts = re.findall(r"[^。！？!?\n]+[。！？!?]?", text)
    sentences = [part.strip() for part in parts if part.strip()]
    return sentences or ([text] if text else [])


def count_japanese_sentences(text):
    return len(split_japanese_sentences(text))


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
    if isinstance(source.get("result"), dict):
        source = {**source, **source["result"]}
    payload = grammar_response_template(normalize_string(source.get("input_type")) or "japanese")
    payload["natural_translation"] = normalize_string(source.get("natural_translation") or source.get("natural_translation_zh"))
    payload["original_text"] = normalize_string(source.get("original_text")) or original_text
    payload["overall_core_meaning"] = normalize_string(source.get("overall_core_meaning")) or normalize_string(source.get("sentence_summary"))
    payload["sentence_summary"] = payload["overall_core_meaning"]
    payload["reading_hiragana_full"] = enforce_hiragana_reading(
        source.get("reading_hiragana_full") or source.get("hiragana_reading") or fallback_reading,
        original_text,
    )
    payload["hiragana_reading"] = payload["reading_hiragana_full"]

    sentence_breakdown = source.get("sentence_breakdown") if isinstance(source.get("sentence_breakdown"), list) else []
    payload["sentence_breakdown"] = []
    for index, item in enumerate(sentence_breakdown, start=1):
        if not isinstance(item, dict):
            continue
        original_sentence = normalize_string(item.get("original"))
        grammar_items = item.get("grammar_points") if isinstance(item.get("grammar_points"), list) else []
        vocab_items = item.get("vocabulary_notes") if isinstance(item.get("vocabulary_notes"), list) else []
        payload["sentence_breakdown"].append(
            {
                "sentence_index": int(item.get("sentence_index") or index),
                "original": original_sentence,
                "reading_hiragana": enforce_hiragana_reading(item.get("reading_hiragana"), original_sentence),
                "translation_zh": normalize_string(item.get("translation_zh") or item.get("translation")),
                "core_meaning": normalize_string(item.get("core_meaning") or item.get("meaning")),
                "grammar_points": grammar_items,
                "vocabulary_notes": vocab_items,
            }
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
    vocabulary_notes = source.get("vocabulary_notes") if isinstance(source.get("vocabulary_notes"), list) else []
    payload["vocabulary_notes"] = vocabulary_notes
    slang_terms = source.get("slang_terms") if isinstance(source.get("slang_terms"), list) else []
    payload["slang_terms"] = merge_slang_terms(slang_terms, detect_known_slang_terms(original_text))
    payload["error_message"] = normalize_string(source.get("error_message"))
    payload["original"] = original_text
    payload["advanced_mecab"] = advanced_mecab or {}
    return payload


def build_grammar_coach_prompt(text, hiragana_reading):
    base_prompt = f"""
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
    full_passage_requirements = """
【整段解析硬性要求】
你是一位日文句子理解教練。請完整分析使用者輸入的「整段日文」，不可只分析最後一句。
請保留前後文、人物關係、語氣、情境脈絡。若內容包含多個句子，必須同時提供：
1. 原文全文 original_text。
2. 全文平假名讀音 reading_hiragana_full。
3. 整段自然中文翻譯 natural_translation。
4. 整段核心意思 overall_core_meaning；sentence_summary 也請填入同一個整段核心意思。
5. 逐句解析 sentence_breakdown。每一句都必須包含 sentence_index、original、reading_hiragana、translation_zh、core_meaning、grammar_points、vocabulary_notes。
   為避免輸出過長，每句的文法與詞彙說明請精簡扼要，單一說明限制在 50 字內。
6. 全文層級的 grammar_points、vocabulary_notes、tone、learning_focus。
如果輸入有多句，sentence_breakdown 不可以只回最後一句。

請在 JSON 中加入並填滿以下欄位：
{
  "original_text": "完整原文，必須包含使用者輸入的所有句子",
  "reading_hiragana_full": "完整全文平假名讀音，不可只讀最後一句",
  "natural_translation": "整段自然中文翻譯",
  "sentence_summary": "整段核心意思，不可只摘要最後一句",
  "overall_core_meaning": "整段核心意思，包含前後文與人物關係",
  "sentence_breakdown": [
    {
      "sentence_index": 1,
      "original": "第 1 句原文",
      "reading_hiragana": "第 1 句平假名",
      "translation_zh": "第 1 句自然中文翻譯",
      "core_meaning": "第 1 句重點說明",
      "grammar_points": [],
      "vocabulary_notes": []
    }
  ],
  "vocabulary_notes": [],
  "error_message": ""
}
""".strip()
    return f"{full_passage_requirements}\n\n{base_prompt}"


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
    sentence_count = count_japanese_sentences(text)
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
    print(f"[grammar-analyzer] sentence_count={sentence_count}")
    print("[grammar-analyzer] using_full_text=true")
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
        payload = annotate_grammar_payload(payload, False, "fallback", reason, diagnostic["selected_model"], diagnostic)
        persist_analysis_slang_terms(payload, text, "grammar_analyzer_billing_block")
        return payload, 200

    prompt = build_grammar_coach_prompt(text, fallback_reading)
    print(f"[grammar-analyzer] prompt_chars={len(prompt)}")
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
            breakdown_count = len(payload.get("sentence_breakdown") or [])
            print(f"[grammar-analyzer] result_sentence_breakdown_count={breakdown_count}")
            if sentence_count > 1 and breakdown_count <= 1:
                print("[grammar-analyzer] warning=multi_sentence_input_but_single_sentence_output")
            print(f"[grammar-analyzer] Gemini 解析成功；model={model_name}")
            diagnostic["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            if grammar_debug_enabled():
                payload["debug"] = diagnostic
            payload = annotate_grammar_payload(payload, True, "gemini", "", model_name, diagnostic if grammar_debug_enabled() else None)
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
    payload = annotate_grammar_payload(payload, False, "fallback", reason, diagnostic.get("selected_model", ""), diagnostic)
    persist_analysis_slang_terms(payload, text, "grammar_analyzer_fallback")
    return payload, 200


def handle_grammar_analyzer_api():
    started = time.perf_counter()
    try:
        data = request.get_json(silent=True) or {}
        text = str(data.get("text", "")).strip()
        if not text:
            payload = grammar_not_japanese_response()
            payload.update({"ok": False, "error": "grammar_analysis_failed", "reason": "empty_input", "message": "\u8acb\u8f38\u5165\u8981\u89e3\u6790\u7684\u65e5\u6587\u3002"})
            return jsonify(payload), 400
        sentence_count = count_japanese_sentences(text)
        print(f"[grammar-analyzer] input_chars={len(text)}")
        print(f"[grammar-analyzer] sentence_count={sentence_count}")
        print("[grammar-analyzer] using_full_text=true")
        if len(text) > 1000:
            payload = annotate_grammar_payload(
                grammar_response_template("japanese"),
                False,
                "none",
                "input_too_long",
                "",
                {"input_chars": len(text), "limit_chars": 1000},
            )
            return jsonify(payload), 400
        if not is_probably_japanese_text(text):
            payload = grammar_not_japanese_response()
            payload.update({"ok": False, "error": "grammar_analysis_failed", "reason": "not_japanese", "message": "\u8acb\u8f38\u5165\u65e5\u6587\u53e5\u5b50\u6216\u6587\u7ae0\u3002"})
            return jsonify(payload), 400
        payload, status = analyze_grammar_with_gemini(text)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        print(f"[perf] grammar_analyzer ms={elapsed_ms}")
        return jsonify(payload), status
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        print(f"[grammar-analyzer] unhandled error elapsed_ms={elapsed_ms}; reason={exc}")
        print(traceback.format_exc())
        payload = annotate_grammar_payload(
            grammar_response_template("japanese"),
            False,
            "none",
            "unknown_error",
            "",
            {"elapsed_ms": elapsed_ms, "exception_type": type(exc).__name__, "exception_message": str(exc)[:500]},
        )
        return jsonify(payload), 500


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
    unsupported = [
        str(rule.get("rule_key") or "")
        for rule in rules
        if str(rule.get("rule_key") or "") not in SIX_MAIN_VOCAB_RULE_KEYS
    ]
    if unsupported:
        return jsonify(
            {
                "ok": False,
                "error": "unsupported_rule_key",
                "message": "\u76ee\u524d\u53ea\u652f\u63f4 N1\u3001N2\u3001N3\u3001N4\u3001N5\u3001SNS \u8a5e\u985e\u3002",
            }
        ), 400
    try:
        result = insert_or_update_vocab_rules(rules)
        return jsonify({"ok": True, "success": True, "message": "\u55ae\u5b57\u51fa\u73fe\u898f\u5247\u5df2\u5132\u5b58\u3002", **result, **build_vocab_rules_payload(create_missing=False)})
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
    settings = public_settings(load_settings())
    print(
        "[settings-load] returned "
        f"target_levels={','.join(settings_target_levels(settings))} "
        f"word_count={settings.get('word_count')} "
        f"verb_count={settings.get('verb_count')} "
        f"choice_count={settings.get('choice_count')} "
        f"fill_count={settings.get('fill_count')}"
    )
    return jsonify({"ok": True, "settings": settings, **settings})


@app.post("/api/settings")
def api_save_settings():
    try:
        saved = public_settings(save_settings_file(request.get_json(silent=True) or {}))
        return jsonify({"ok": True, "settings": saved, **saved})
    except Exception as exc:
        print(f"[settings-save] failed reason={exc}")
        print(traceback.format_exc())
        return jsonify({"ok": False, "error": "settings_save_failed", "reason": str(exc)}), 200


@app.get("/api/telegram/reminder-settings")
def api_telegram_reminder_settings():
    try:
        return jsonify({"ok": True, "settings": load_telegram_reminder_settings(), "time_options": reminder_time_options()})
    except Exception as exc:
        print(f"[telegram-reminder] settings_load_failed reason={exc}")
        return jsonify({"ok": False, "error": "reminder_settings_load_failed", "reason": str(exc)}), 200


@app.post("/api/telegram/reminder-settings")
def api_save_telegram_reminder_settings():
    try:
        saved = save_telegram_reminder_settings(request.get_json(silent=True) or {})
        return jsonify({"ok": True, "settings": saved, "time_options": reminder_time_options()})
    except Exception as exc:
        print(f"[telegram-reminder] settings_save_failed reason={exc}")
        return jsonify({"ok": False, "error": "reminder_settings_save_failed", "reason": str(exc)}), 200


@app.post("/api/telegram/test-reminder")
def api_test_telegram_reminder():
    try:
        payload = request.get_json(silent=True) or {}
        settings = normalize_reminder_settings({**raw_app_settings(), **payload}) if payload else load_telegram_reminder_settings()
        send_telegram_reminder_message(settings["message"])
        return jsonify({"ok": True, "sent": True, "reason": "test_reminder_sent"})
    except Exception as exc:
        print(f"[telegram-reminder] test_failed reason={exc}")
        return jsonify({"ok": False, "error": "telegram_reminder_test_failed", "reason": str(exc)}), 200


@app.route("/api/telegram/reminder-run", methods=["GET", "POST"])
def api_telegram_reminder_run():
    try:
        if TELEGRAM_REMINDER_SECRET and request.args.get("secret") != TELEGRAM_REMINDER_SECRET:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        settings = load_telegram_reminder_settings()
        if not settings["enabled"]:
            return jsonify({"ok": True, "sent": False, "reason": "disabled"}), 200
        tz = ZoneInfo(settings.get("timezone") or "Asia/Taipei")
        now = datetime.now(tz)
        slot = reminder_slot_time(now)
        if slot not in settings["times"]:
            return jsonify({"ok": True, "sent": False, "reason": "not_due_yet", "slot": slot}), 200
        if reminder_day_code(now) not in settings["days"]:
            return jsonify({"ok": True, "sent": False, "reason": "not_due_yet", "slot": slot}), 200
        sent_key = f"{now.date().isoformat()}|{slot}"
        if settings.get("last_sent_key") == sent_key:
            return jsonify({"ok": True, "sent": False, "reason": "already_sent", "sent_key": sent_key}), 200
        send_telegram_reminder_message(settings["message"])
        sent_at = datetime.now(timezone.utc).isoformat()
        save_app_setting_values(
            {
                "telegram_reminder_last_sent_key": sent_key,
                "telegram_reminder_last_sent_at": sent_at,
            }
        )
        print(f"[telegram-reminder] sent sent_key={sent_key}")
        return jsonify({"ok": True, "sent": True, "reason": "reminder_sent", "sent_key": sent_key}), 200
    except Exception as exc:
        print(f"[telegram-reminder] run_failed reason={exc}")
        return jsonify({"ok": False, "error": "telegram_reminder_run_failed", "reason": str(exc)}), 200


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
    version_rows = []
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT material_key, material_date, date, version_no, generation_source,
                           generation_mode, is_latest, created_at
                    FROM materials
                    WHERE COALESCE(material_json, '') <> ''
                      AND (material_date IS NOT NULL OR COALESCE(date, '') <> '')
                    ORDER BY COALESCE(material_date, CURRENT_DATE) DESC, version_no DESC, id DESC
                    LIMIT %s
                    """,
                    (limit * 10,),
                )
                version_rows = cur.fetchall()
    else:
        try:
            df = pd.read_csv(
                DATABASE_FILE,
                dtype=str,
                keep_default_na=False,
                encoding="utf-8-sig",
            )
            df = ensure_material_version_columns_df(df)
            rows = df[(df["material_json"].astype(str) != "") & ((df["material_date"].astype(str) != "") | (df["date"].astype(str) != ""))]
            version_rows = [
                (
                    row.get("material_key", ""),
                    row.get("material_date", ""),
                    row.get("date", ""),
                    row.get("version_no", 1),
                    row.get("generation_source", ""),
                    row.get("generation_mode", ""),
                    row.get("is_latest", ""),
                    row.get("created_at", ""),
                )
                for _, row in rows.iterrows()
            ]
        except (FileNotFoundError, ValueError):
            version_rows = []
    grouped = {}
    for row in version_rows:
        key, mat_date, date_value, version_no, source, mode, is_latest, created_at = row
        date_iso = canonical_material_date(mat_date or date_value)
        display = material_date_display(date_iso)
        try:
            version_no = int(version_no or 1)
        except (TypeError, ValueError):
            version_no = 1
        item = {
            "material_key": key or build_material_key(date_iso, version_no),
            "version_no": version_no,
            "display_label": f"{display.split('/', 1)[-1] if display.count('/') == 2 else display} -- {version_no}",
            "generation_source": source or "",
            "generation_mode": mode or "",
            "is_latest": str(is_latest).lower() in {"true", "1", "t", "yes"},
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
        }
        grouped.setdefault(
            date_iso,
            {
                "material_date": date_iso,
                "display_date": f"{parse_material_date(date_iso).month:02d}/{parse_material_date(date_iso).day:02d}" if parse_material_date(date_iso) else display,
                "latest_material_key": item["material_key"],
                "latest_version_no": version_no,
                "version_count": 0,
                "versions": [],
            },
        )
        grouped[date_iso]["versions"].append(item)
        if item["is_latest"] or version_no >= grouped[date_iso]["latest_version_no"]:
            grouped[date_iso]["latest_material_key"] = item["material_key"]
            grouped[date_iso]["latest_version_no"] = version_no
    dates_payload = []
    for date_iso, entry in grouped.items():
        entry["versions"] = sorted(entry["versions"], key=lambda item: item["version_no"])
        entry["version_count"] = len(entry["versions"])
        dates_payload.append(entry)
    dates_payload = sorted(dates_payload, key=lambda item: item["material_date"], reverse=True)[:limit]
    payload = {"ok": True, "dates": dates_payload}

    _ARCHIVE_DATES_CACHE["payload"] = payload
    _ARCHIVE_DATES_CACHE["expires_at"] = now_ts + ARCHIVE_DATES_CACHE_TTL_SECONDS
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    print(f"[perf] api_archive_dates ms={elapsed_ms} cached=false")
    return jsonify(payload)


@app.get("/api/materials")
def api_materials():
    started = time.perf_counter()
    material_key = request.args.get("material_key", "").strip()
    payload = material_by_key(material_key) if material_key else material_by_date(request.args.get("date", today_string()))
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
    resolved = payload.get("resolved_settings") or metadata.get("resolved_settings", {}) if isinstance(payload, dict) else {}
    validation = payload.get("count_validation") or metadata.get("count_validation", {}) if isinstance(payload, dict) else {}
    print(
        "[materials-load] "
        f"latest material_key={payload.get('material_key', '') if isinstance(payload, dict) else ''} "
        f"version_no={payload.get('version_no', '') if isinstance(payload, dict) else ''} "
        f"resolved word_count={resolved.get('word_count', '') if isinstance(resolved, dict) else ''} "
        f"verb_count={resolved.get('verb_count', '') if isinstance(resolved, dict) else ''} "
        f"actual word_count={validation.get('word_count_actual', '') if isinstance(validation, dict) else ''} "
        f"verb_count={validation.get('verb_count_actual', '') if isinstance(validation, dict) else ''}"
    )
    print(f"[perf] load_daily_material ms={elapsed_ms}")
    return jsonify(payload)


@app.post("/api/generate")
def api_generate():
    mode = "local"
    data = {}
    manual_started = time.monotonic()
    try:
        data = request.get_json(silent=True) or {}
        mode = request.args.get("mode") or data.get("mode") or "local"
        use_sample = request.args.get("sample") == "1"
        app_url = request.host_url.rstrip("/")
        normalized_mode = normalize_generation_mode(mode)
        if normalized_mode in {"local", "gemini"}:
            if normalized_mode == "gemini" and not GEMINI_API_KEY:
                return jsonify(
                    {
                        "ok": False,
                        "error": "gemini_generation_not_available",
                        "reason": "Gemini daily material generator is not configured",
                    }
                ), 200
            if normalized_mode == "gemini":
                print("[material-generator] mode=gemini start")
                print("[feature-boundary] daily_material mode=gemini use gemini item bank")
                settings, settings_source, _db_settings = resolve_generation_settings_with_trace(data, persist=bool(data))
                stages = gemini_bank_stage_plan(settings)
                steps = gemini_bank_step_plan(settings)
                job = create_gemini_generation_job(settings, material_date=get_today_taipei_date(), planned_steps=steps)
                return jsonify(
                    {
                        "ok": True,
                        "job_id": job["job_id"],
                        "status": "running",
                        "next_stage": stages[0] if stages else "finalize",
                        "stages": stages,
                        "steps": steps,
                        "generation_mode": "gemini_bank",
                        "settings_source": settings_source,
                        "resolved_settings": {
                            "target_level": settings.get("target_level", ""),
                            "target_levels": settings_target_levels(settings),
                            "word_count": int(settings.get("vocab_count") or 0),
                            "verb_count": int(settings.get("verb_count") or 0),
                            "choice_count": int(settings.get("mcq_count") or 0),
                            "fill_count": int(settings.get("fill_count") or 0),
                            "grammar_level": "fixed_quota",
                            "grammar_count": LOCAL_FIXED_GRAMMAR_TOTAL,
                        },
                    }
                )
            timeout_seconds = GEMINI_GENERATE_TIMEOUT_SECONDS if normalized_mode == "gemini" else MANUAL_GENERATE_TIMEOUT_SECONDS
            if normalized_mode == "gemini":
                print(f"[gemini-generate] start timeout_seconds={timeout_seconds}")
            else:
                print(f"[manual-generate] start timeout_seconds={timeout_seconds}")
            generation_source = "gemini_api" if normalized_mode == "gemini" else "manual_local"
            result = run_manual_generation_with_timeout(
                lambda deadline_check: generate_daily_material(
                    use_sample=use_sample,
                    posted_settings=data,
                    app_url=app_url,
                    mode=normalized_mode,
                    generation_source=generation_source,
                    deadline_check=deadline_check,
                ),
                timeout_seconds=timeout_seconds,
                started_at=manual_started,
            )
            elapsed_ms = round((time.monotonic() - manual_started) * 1000)
            if normalized_mode == "gemini":
                print(f"[gemini-generate] success elapsed_ms={elapsed_ms} material_key={result.get('material_key', '')}")
            else:
                print(f"[manual-generate] success elapsed_ms={elapsed_ms} material_key={result.get('material_key', '')}")
            return jsonify(result)
        return jsonify(
            generate_daily_material(
                use_sample=request.args.get("sample") == "1",
                posted_settings=data,
                app_url=app_url,
                mode=mode,
                generation_source="manual_local",
            )
        )
    except ManualGenerateTimeout as e:
        if normalize_generation_mode(mode) == "gemini":
            print(f"[gemini-generate] timeout elapsed_ms={e.elapsed_ms} stage={e.stage}")
            return jsonify(
                {
                    "ok": False,
                    "error": "gemini_generate_timeout",
                    "reason": "Gemini generation exceeded time budget",
                    "elapsed_ms": e.elapsed_ms,
                    "stage": e.stage,
                    "timeout_seconds": e.timeout_seconds,
                }
            ), 200
        print(f"[manual-generate] timeout elapsed_ms={e.elapsed_ms} stage={e.stage}")
        return jsonify(manual_generate_timeout_payload(e)), 200
    except Exception as e:
        if mode == "local":
            print(f"[local-generate] primary local generation failed; trying seed fallback; reason={e}")
            print(traceback.format_exc())
            try:
                def fallback_operation(deadline_check):
                    settings, settings_source, db_settings = resolve_generation_settings_with_trace(data, persist=bool(data))
                    deadline_check("resolve_settings_after")
                    fallback_material = build_local_material(
                        settings,
                        force_seed=True,
                        material_date=get_today_taipei_date(),
                        deadline_check=deadline_check,
                    )
                    deadline_check("build_material_after")
                    return settings, settings_source, db_settings, fallback_material

                settings, settings_source, db_settings, fallback_material = run_manual_generation_with_timeout(
                    fallback_operation,
                    timeout_seconds=MANUAL_GENERATE_TIMEOUT_SECONDS,
                    started_at=manual_started,
                )
                requested_words = int(settings.get("vocab_count") or 0)
                requested_verbs = int(settings.get("verb_count") or 0)
                actual_words = len(fallback_material.get("vocab") or fallback_material.get("words") or [])
                actual_verbs = len(fallback_material.get("verbs") or [])
                count_warnings = []
                if actual_words < requested_words:
                    count_warnings.append("word_count_not_matched")
                if actual_verbs < requested_verbs:
                    count_warnings.append("verb_count_not_matched")
                count_validation = {
                    "target_level_requested": settings.get("target_level", ""),
                    "target_level_actual": fallback_material.get("level") or settings.get("target_level", ""),
                    "word_count_requested": requested_words,
                    "word_count_actual": actual_words,
                    "verb_count_requested": requested_verbs,
                    "verb_count_actual": actual_verbs,
                    "word_count_matched": actual_words >= requested_words,
                    "verb_count_matched": actual_verbs >= requested_verbs,
                    "warnings": count_warnings,
                }
                settings_trace = build_settings_trace(
                    data or {},
                    db_settings,
                    settings,
                    settings_source,
                    selector_actual={"word_count": actual_words, "verb_count": actual_verbs},
                )
                fallback_material.setdefault("metadata", {})
                fallback_material["metadata"].update(
                    {
                        "generation_mode": "local_fallback",
                        "fallback_used": True,
                        "ai_used": False,
                        "settings_source": settings_source,
                        "count_validation": count_validation,
                        "settings_trace": settings_trace,
                        "pool_diagnostics": fallback_material.get("metadata", {}).get("pool_diagnostics", {}),
                    }
                )
                warnings = fallback_material["metadata"].setdefault("warnings", [])
                if "local_generation_primary_failed_used_seed_fallback" not in warnings:
                    warnings.append("local_generation_primary_failed_used_seed_fallback")
                elapsed_ms = round((time.monotonic() - manual_started) * 1000)
                if elapsed_ms >= MANUAL_GENERATE_TIMEOUT_SECONDS * 1000:
                    raise ManualGenerateTimeout("save_material_before", elapsed_ms, MANUAL_GENERATE_TIMEOUT_SECONDS)
                save_info = save_material_for_date(
                    get_today_taipei_date(),
                    fallback_material,
                    settings,
                    generation_source="manual_local",
                    generation_mode="local_fallback",
                )
                fallback_material["metadata"]["selection_log_warnings"] = save_info.get("selection_log_warnings", [])
                invalidate_dashboard_cache("local generation seed fallback")
                print(
                    "[local-generate] seed_fallback_used=true "
                    f"material_key={save_info.get('material_key')} reason={e}"
                )
                return jsonify(
                    {
                        "ok": True,
                        "message": "\u5df2\u4f7f\u7528\u5b89\u5168\u57fa\u790e\u8a5e\u5eab\u5efa\u7acb\u6559\u6750\u3002",
                        "fallback_used": True,
                        "material_date": save_info.get("material_date"),
                        "material_key": save_info.get("material_key"),
                        "version_no": save_info.get("version_no"),
                        "generation_source": save_info.get("generation_source"),
                        "generation_mode": "local_fallback",
                        "ai_used": False,
                        "settings_source": settings_source,
                        "count_validation": count_validation,
                        "settings_trace": settings_trace,
                    }
                )
            except ManualGenerateTimeout as timeout_exc:
                print(f"[manual-generate] timeout elapsed_ms={timeout_exc.elapsed_ms} stage={timeout_exc.stage}")
                return jsonify(manual_generate_timeout_payload(timeout_exc)), 200
            except Exception as fallback_exc:
                elapsed_ms = round((time.monotonic() - manual_started) * 1000)
                print(f"[manual-generate] failed error={fallback_exc} elapsed_ms={elapsed_ms}")
                print(f"[local-generate] seed fallback failed; reason={fallback_exc}")
                print(traceback.format_exc())
                return jsonify(
                    {
                        "ok": False,
                        "error": "local_generation_failed",
                        "message": "\u672c\u5730\u6559\u6750\u751f\u6210\u5931\u6557\uff0c\u7cfb\u7d71\u5df2\u4fdd\u7559\u65e2\u6709\u6559\u6750\u8207\u8a2d\u5b9a\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66\u3002",
                        "debug": {
                            "stage": "api_generate_seed_fallback",
                            "mode": mode,
                            "reason": str(fallback_exc),
                        },
                    }
                ), 200
        if normalize_generation_mode(mode) == "gemini":
            reason = classify_gemini_daily_material_error(e)
            if reason == "timeout":
                return jsonify(
                    {
                        "ok": False,
                        "error": "gemini_generate_timeout",
                        "reason": "Gemini generation exceeded time budget",
                        "elapsed_ms": round((time.monotonic() - manual_started) * 1000),
                    }
                ), 200
            return jsonify(
                {
                    "ok": False,
                    "error": "gemini_generation_failed",
                    "reason": reason,
                    "message": "Gemini 每日教材生成失敗，請稍後再試。",
                    "debug": {
                        "stage": "api_generate_gemini",
                        "reason": str(e)[:500],
                    },
                }
            ), 200

        payload = {
            "ok": False,
            "error": "local_generation_failed",
            **material_generation_error_payload(e),
            "debug": {
                "stage": "api_generate",
                "mode": mode,
                "reason": str(e),
            },
        }
        return jsonify(payload), 200 if mode == "local" else 500


@app.post("/api/generate/gemini-step")
def api_generate_gemini_step():
    try:
        data = request.get_json(silent=True) or {}
        job_id = str(data.get("job_id") or "").strip()
        stage = str(data.get("stage") or "").strip().lower()
        if not job_id:
            return jsonify({"ok": False, "error": "missing_job_id", "reason": "job_id is required"}), 400
        if stage in {"vocab", "verbs", "grammar"}:
            print(f"[gemini-step-runner] rejected legacy_stage={stage}")
            return jsonify(
                {
                    "ok": False,
                    "error": "gemini_legacy_stage_rejected",
                    "reason": "Use planned refill steps returned by /api/generate?mode=gemini",
                    "received_stage": stage,
                }
            ), 200
        if stage == "finalize":
            payload, status_code = finalize_gemini_generation_job(job_id, app_url=request.host_url.rstrip("/"))
            return jsonify(payload), status_code
        if stage == "refill":
            item_type = str(data.get("item_type") or "").strip().lower()
            jlpt_level = str(data.get("jlpt_level") or data.get("level") or "").strip().upper()
            count = min(GEMINI_BANK_REFILL_BATCH_SIZE, max(1, int(data.get("count") or GEMINI_BANK_REFILL_BATCH_SIZE)))
            valid_levels = [*LEVELS, "SNS"] if item_type == "word" else LEVELS
            if item_type not in {"word", "verb", "grammar"} or jlpt_level not in valid_levels:
                return jsonify(
                    {
                        "ok": False,
                        "error": "unsupported_gemini_stage",
                        "stage": stage,
                        "reason": "refill step requires item_type and jlpt_level",
                    }
                ), 400
            print(f"[gemini-step-runner] executing stage=refill item_type={item_type} level={jlpt_level} count={count}")
            payload, status_code = run_gemini_generation_stage(
                job_id,
                f"{item_type}:{jlpt_level}",
                item_type=item_type,
                level=jlpt_level,
                requested_count=count,
            )
            return jsonify(payload), status_code
        payload, status_code = run_gemini_generation_stage(job_id, stage)
        return jsonify(payload), status_code
    except Exception as exc:
        print(f"[gemini-stage] failed job_id=unknown stage=unknown error={exc}")
        print(traceback.format_exc())
        return jsonify(
            {
                "ok": False,
                "error": "gemini_stage_failed",
                "stage": "unknown",
                "reason": str(exc),
            }
        ), 200


@app.route("/api/cron/daily-push", methods=["GET", "POST"])
def api_cron_daily_push():
    cron_started = time.perf_counter()
    print("[cron-api] skipped reason=daily_cron_disabled_manual_only")
    return jsonify({"ok": True, "skipped": True, "reason": "daily_cron_disabled_manual_only"}), 200
    if CRON_SECRET and request.args.get("secret") != CRON_SECRET:
        return jsonify({"ok": False, "error": "unauthorized", "message": "unauthorized"}), 401
    notify_telegram = str(request.args.get("notify", "1")).strip().lower() not in {"0", "false", "no", "off"}
    try:
        payload = run_daily_schedule(
            app_url=APP_URL,
            mode=request.args.get("mode", "local"),
            notify_telegram=notify_telegram,
        )
        elapsed_ms = round((time.perf_counter() - cron_started) * 1000)
        payload["cron_total_elapsed_ms"] = elapsed_ms
        if not notify_telegram:
            payload["telegram_delivery"] = "external_cron"
            payload["telegram_dispatched_async"] = False
        print(f"[cron] daily_push success elapsed_ms={elapsed_ms}")
        return jsonify(payload), 200
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - cron_started) * 1000)
        payload = material_generation_error_payload(e)
        payload.update(
            {
                "ok": False,
                "error": "scheduled_generate_failed",
                "debug": {
                    "stage": "api_cron_daily_push",
                    "reason": str(e),
                    "exception_type": e.__class__.__name__,
                },
            }
        )
        payload["cron_total_elapsed_ms"] = elapsed_ms
        print(f"[cron] daily_push failed elapsed_ms={elapsed_ms} reason={e}")
        return jsonify(payload), 500


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
    question_type = normalize_practice_question_type(question_type)
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
    question_type = normalize_practice_question_type(data.get("question_type"))
    answer = clean_answer_value(data.get("answer", ""))
    if not verb_id or question_type not in QUESTION_TYPES or not answer:
        return jsonify({"error": "題目或答案不完整。"}), 400
    verb = sqlite_one("SELECT * FROM verbs WHERE id = ?", (verb_id,))
    if not verb:
        return jsonify({"error": "找不到動詞題目。"}), 404
    correct = verb[question_type]
    answer_context = verb_answer_context(verb, question_type, correct)
    accepted_answers = answer_context["accepted_answers"]
    is_correct = smart_answer_equal(answer, correct, accepted_answers)
    debug_report = None
    if not is_correct:
        add_mistake(int(verb_id), question_type, answer)
        debug_report = make_debug_report_payload(
            question_type=question_type,
            prompt=f"請寫出「{verb['dictionary_form']}（{verb['reading']}）」的{VERB_FORM_LABELS.get(question_type, question_type)}。",
            user_answer=answer,
            correct_answer=answer_context["primary_answer"] or correct,
            target_text=verb["dictionary_form"],
            target_reading=verb["reading"],
            target_form=question_type,
            error_category="動詞變化錯",
            extra={
                "base_surface": verb["dictionary_form"],
                "base_reading": verb["reading"],
                "conjugation_type": question_type,
                "primary_answer": answer_context["primary_answer"],
                "accepted_answers": accepted_answers,
            },
        )
    return jsonify(
        {
            "correct": is_correct,
            "correct_answer": answer_context["display_answer"],
            "primary_answer": answer_context["primary_answer"],
            "accepted_answers": accepted_answers,
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
    ensure_settings_store()
    args = args or {}
    question_type = args.get("question_type", "all")
    error_category = args.get("error_category", "all")
    scope = args.get("scope", "due")
    params = []
    where_parts = ["COALESCE(m.mastered, 0) = 0"]
    if question_type in QUESTION_TYPES:
        where_parts.append("m.question_type = ?")
        params.append(question_type)
    if error_category in ERROR_CATEGORIES:
        where_parts.append("m.error_category = ?")
        params.append(error_category)
    if scope != "all":
        where_parts.append(mistake_due_where("m"))
        params.extend(mistake_due_params())
    where = " AND ".join(where_parts)
    sql = f"""
    SELECT
        m.id, m.verb_id, m.question_type, m.user_wrong_answer,
        m.mistake_count, m.status, m.last_reviewed_at,
        m.next_review_date, m.review_interval, m.review_count,
        m.mastered, m.error_category, m.debug_report_json,
        m.question_text, m.base_surface, m.base_reading, m.conjugation_type,
        m.primary_answer, m.accepted_answers_json, m.explanation,
        m.created_at, m.updated_at, m.first_wrong_at, m.last_wrong_at,
        m.review_due_at, m.mastered_at, m.correct_count,
        m.material_key, m.material_date, m.question_id, m.error_type, m.prompt,
        v.dictionary_form, v.reading, v.meaning, v.verb_group,
        v.te_form, v.ta_form, v.nai_form, v.renyou_form,
        v.shieki_form, v.ukemi_form, v.ba_form
    FROM mistake_logs m
    LEFT JOIN verbs v ON v.id = m.verb_id
    WHERE {where}
    ORDER BY COALESCE(m.review_due_at, m.next_review_date, date(m.last_reviewed_at)) ASC,
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
        if row["question_type"] in QUESTION_TYPES:
            stored_accepted = []
            if row.get("accepted_answers_json"):
                try:
                    stored_accepted = json.loads(row["accepted_answers_json"]) or []
                except (TypeError, json.JSONDecodeError):
                    stored_accepted = []
            correct_source = row.get(row["question_type"]) or row.get("primary_answer") or ""
            accepted = stored_accepted or accepted_verb_form_answers(row, row["question_type"], correct_source)
            row["accepted_answers"] = accepted
            row["correct_answer"] = accepted_answer_display(accepted, correct_source)
        else:
            stored_accepted = []
            if row.get("accepted_answers_json"):
                try:
                    stored_accepted = json.loads(row["accepted_answers_json"]) or []
                except (TypeError, json.JSONDecodeError):
                    stored_accepted = []
            row["accepted_answers"] = stored_accepted
            row["correct_answer"] = row.get("primary_answer") or ""
        row["prompt"] = row.get("prompt") or row.get("question_text") or ""
        row["question_text"] = row.get("question_text") or row["prompt"]
        row["base_surface"] = row.get("base_surface") or row.get("dictionary_form") or ""
        row["base_reading"] = row.get("base_reading") or row.get("reading") or ""
        row["conjugation_type"] = row.get("conjugation_type") or row.get("question_type") or ""
        row["wrong_count"] = int(row.get("mistake_count") or 0)
        row["correct_count"] = int(row.get("correct_count") or 0)
        row["review_due_at"] = row.get("review_due_at") or row.get("next_review_date") or ""
        row["error_type"] = row.get("error_type") or ("verb_conjugation_wrong" if row.get("question_type") in QUESTION_TYPES else "unknown")
        try:
            row["verb_group_label"] = group_label(row["verb_group"]) if row.get("verb_group") is not None else ""
        except (TypeError, ValueError):
            row["verb_group_label"] = ""
    return rows


@app.get("/api/mistakes/stats")
def api_mistake_stats():
    due_count = sqlite_one(
        f"""
        SELECT COUNT(*) AS count
        FROM mistake_logs m
        WHERE {mistake_due_where("m")}
        """,
        mistake_due_params(),
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
                next_review_date = NULL,
                review_due_at = NULL,
                mastered_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, now, mistake_id),
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
    question_type = normalize_practice_question_type(row["question_type"])
    correct = row[question_type]
    answer_context = verb_answer_context(row, question_type, correct)
    accepted_answers = answer_context["accepted_answers"]
    is_correct = smart_answer_equal(answer, correct, accepted_answers)
    report = make_debug_report_payload(
        question_type=question_type,
        prompt=f"請寫出「{row['dictionary_form']}（{row['reading']}）」的{VERB_FORM_LABELS.get(question_type, question_type)}。",
        user_answer=answer,
        correct_answer=answer_context["primary_answer"] or correct,
        target_text=row["dictionary_form"],
        target_reading=row["reading"],
        target_form=question_type,
        error_category=error_category,
        extra={
            "base_surface": row["dictionary_form"],
            "base_reading": row["reading"],
            "conjugation_type": question_type,
            "primary_answer": answer_context["primary_answer"],
            "accepted_answers": accepted_answers,
        },
    )
    report_json = debug_report_to_json(report)
    accepted_answers_json = json.dumps(accepted_answers, ensure_ascii=False)
    explanation = " ".join(filter(None, [report.get("diagnosis", ""), report.get("rule", "")]))
    now = datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds")
    due_date = today_iso_date()
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        if is_correct:
            next_interval = next_interval_after_success(row.get("review_interval"))
            new_correct_count = int(row.get("correct_count") or 0) + 1
            mastered_now = new_correct_count >= MISTAKE_MASTERY_CORRECT_THRESHOLD
            next_due = None if mastered_now else iso_date_after(next_interval)
            conn.execute(
                """
                UPDATE mistake_logs
                SET last_reviewed_at = ?,
                    review_interval = ?,
                    review_count = COALESCE(review_count, 0) + 1,
                    next_review_date = ?,
                    review_due_at = ?,
                    correct_count = ?,
                    mastered = ?,
                    mastered_at = ?,
                    status = ?,
                    updated_at = ?,
                    error_category = COALESCE(NULLIF(error_category, ''), ?),
                    primary_answer = ?,
                    accepted_answers_json = ?
                WHERE id = ?
                """,
                (
                    now,
                    next_interval,
                    next_due,
                    next_due,
                    new_correct_count,
                    1 if mastered_now else 0,
                    now if mastered_now else row.get("mastered_at"),
                    "mastered" if mastered_now else "learning",
                    now,
                    error_category,
                    answer_context["primary_answer"],
                    accepted_answers_json,
                    mistake_id,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE mistake_logs
                SET user_wrong_answer = ?,
                    mistake_count = mistake_count + 1,
                    last_reviewed_at = ?,
                    next_review_date = ?,
                    review_due_at = ?,
                    review_interval = 1,
                    mastered = 0,
                    status = 'review_due',
                    last_wrong_at = ?,
                    updated_at = ?,
                    error_category = ?,
                    error_type = 'verb_conjugation_wrong',
                    debug_report_json = ?,
                    question_text = ?,
                    prompt = ?,
                    base_surface = ?,
                    base_reading = ?,
                    conjugation_type = ?,
                    primary_answer = ?,
                    accepted_answers_json = ?,
                    explanation = ?
                WHERE id = ?
                """,
                (
                    f"{row['user_wrong_answer']} / {answer}",
                    now,
                    due_date,
                    now,
                    now,
                    now,
                    error_category,
                    report_json,
                    f"請寫出「{row['dictionary_form']}（{row['reading']}）」的{VERB_FORM_LABELS.get(question_type, question_type)}。",
                    f"請寫出「{row['dictionary_form']}（{row['reading']}）」的{VERB_FORM_LABELS.get(question_type, question_type)}。",
                    row["dictionary_form"],
                    row["reading"],
                    question_type,
                    answer_context["primary_answer"],
                    accepted_answers_json,
                    explanation,
                    mistake_id,
                ),
            )
        conn.commit()
    return jsonify(
        {
            "correct": is_correct,
            "correct_answer": answer_context["display_answer"],
            "primary_answer": answer_context["primary_answer"],
            "accepted_answers": accepted_answers,
            "rule": form_rule_explanation(row, question_type),
            "next_review_date": next_due if is_correct else due_date,
            "review_interval": next_interval if is_correct else 1,
            "status": ("mastered" if mastered_now else "learning") if is_correct else "review_due",
            "mastered": bool(mastered_now) if is_correct else False,
            "correct_count": new_correct_count if is_correct else int(row.get("correct_count") or 0),
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
    question_type = normalize_practice_question_type(row.get("question_type", ""))
    correct = row[question_type] if question_type in QUESTION_TYPES else ""
    accepted_answers = accepted_verb_form_answers(row, question_type, correct) if question_type in QUESTION_TYPES else []
    primary_answer = primary_accepted_answer(accepted_answers, correct)
    report = make_debug_report_payload(
        question_type=question_type,
        prompt=f"請寫出「{row.get('dictionary_form') or question_type}」的{VERB_FORM_LABELS.get(question_type, question_type)}。",
        user_answer=row.get("user_wrong_answer", ""),
        correct_answer=primary_answer or correct,
        target_text=row.get("dictionary_form", ""),
        target_reading=row.get("reading", ""),
        target_form=question_type,
        error_category=row.get("error_category", ""),
        extra={
            "base_surface": row.get("dictionary_form", ""),
            "base_reading": row.get("reading", ""),
            "conjugation_type": question_type,
            "primary_answer": primary_answer,
            "accepted_answers": accepted_answers,
        },
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

    question_type = normalize_practice_question_type(mistake.get("question_type", ""))
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
            (lambda context: {
                "type": "verb",
                "verb_id": row["id"],
                "question_type": question_type,
                "prompt": f"請寫出「{row['dictionary_form']}（{row['reading']}）・{row['meaning']}」的{VERB_FORM_LABELS[question_type]}。",
                "answer": context["primary_answer"] or clean_answer_value(row[question_type]),
                "display_answer": context["display_answer"],
                "accepted_answers": context["accepted_answers"],
            })(verb_answer_context(row, question_type, row[question_type]))
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


@app.get("/api/grammar/debug/analyze-smoke")
def api_grammar_debug_analyze_smoke():
    if not grammar_debug_enabled():
        return jsonify({"ok": False, "error": "debug_endpoint_disabled", "message": "Debug endpoint is disabled."}), 404
    started = time.perf_counter()
    text = request.args.get("text") or "\u4eca\u65e5\u306f\u96e8\u3067\u3059\u304c\u3001\u6563\u6b69\u306b\u884c\u304d\u305f\u3044\u3067\u3059\u3002"
    payload, status = analyze_grammar_with_gemini(text)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    return jsonify(
        {
            "ok": bool(payload.get("ok")),
            "api_key_present": bool(GEMINI_API_KEY),
            "model": payload.get("model") or choose_gemini_model(),
            "grammar_analyzer_ok": bool(payload.get("ok")),
            "reason": payload.get("reason", ""),
            "elapsed_ms": elapsed_ms,
            "http_status": status,
        }
    ), status


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
                        f"""
                        SELECT DISTINCT COALESCE(material_date::text, date)
                        FROM materials
                        WHERE material_date::text IN ({placeholders}) OR date IN ({placeholders})
                        """,
                        tuple(candidate_dates + candidate_dates),
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
        "grammar_count": 0,
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
            "material_date": today_iso_date(),
            "material_key": "",
            "version_no": 0,
            "version_count": 0,
            "generation_source": "",
            "target_level": settings["target_level"],
            "word_count": 0,
            "verb_count": 0,
            "grammar_count": 0,
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
        material_metadata = today_material.get("metadata", {}) if today_material else {}
        material_resolved = today_material.get("resolved_settings") or material_metadata.get("resolved_settings", {}) if today_material else {}
        material_validation = today_material.get("count_validation") or material_metadata.get("count_validation", {}) if today_material else {}
        actual_vocab_count = len(today_material["vocabulary"]) if today_material else 0
        actual_verb_count = len(today_material["verbs"]) if today_material else 0
        actual_grammar_count = len(today_material.get("grammar_points", [])) if today_material else 0
        payload["vocab_count"] = trace_int(material_validation.get("word_count_actual")) or actual_vocab_count
        payload["verb_count"] = trace_int(material_validation.get("verb_count_actual")) or actual_verb_count
        payload["grammar_count"] = trace_int(material_metadata.get("grammar_total_actual")) or actual_grammar_count
        material_target_level = material_resolved.get("target_level") or today_material.get("targetLevel") if today_material else settings["target_level"]
        payload["today_material"] = {
            "status": "generated" if today_material else "not_generated",
            "date": today,
            "material_date": today_material.get("material_date") if today_material else today_iso,
            "material_key": today_material.get("material_key") if today_material else "",
            "version_no": today_material.get("version_no") if today_material else 0,
            "version_count": len(today_material.get("available_versions", [])) if today_material else 0,
            "generation_source": today_material.get("generation_source") if today_material else "",
            "target_level": material_target_level,
            "word_count": payload["vocab_count"],
            "verb_count": payload["verb_count"],
            "grammar_count": payload["grammar_count"],
            "resolved_settings": material_resolved,
            "count_validation": material_validation,
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
            f"""
            SELECT id FROM mistake_logs
            WHERE {mistake_created_date_expr("")} = ?
            """,
            (today_iso,),
        )
        due_review = sqlite_one(
            f"""
            SELECT COUNT(*) AS count
            FROM mistake_logs m
            WHERE {mistake_due_where("m")}
            """,
            mistake_due_params(),
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
        f"""
        SELECT *
        FROM mistake_logs
        WHERE date({mistake_created_date_expr('')}) >= date(?)
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
        f"SELECT COUNT(*) AS count FROM mistake_logs WHERE {mistake_created_date_expr('')} = ?",
        (today_iso,),
    )
    due_review = sqlite_one(
        f"""
        SELECT COUNT(*) AS count
        FROM mistake_logs m
        WHERE {mistake_due_where("m")}
        """,
        mistake_due_params(),
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
        ensure_settings_store()
        if not RUN_MIGRATIONS_ON_STARTUP:
            print("[startup] database schema initialization skipped; RUN_MIGRATIONS_ON_STARTUP=false")
            return
        ensure_database()
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
