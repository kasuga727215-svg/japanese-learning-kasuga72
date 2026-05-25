import csv
import html
import json
import os
import random
import re
import sqlite3
from collections import Counter
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from flask import Flask, jsonify, render_template, request


app = Flask(__name__, template_folder=".")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_FILE = os.path.join(BASE_DIR, "database.csv")
SQLITE_SETTINGS_FILE = os.path.join(BASE_DIR, "state.sqlite3")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()
TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:5000").rstrip("/")
CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()

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
    "讀音錯",
    "文法判斷錯",
    "其他",
]
REVIEW_INTERVAL_STEPS = [3, 7, 14, 30]

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
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
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
        migrate_mistake_logs(conn)
        conn.commit()
    seed_verbs_if_empty()


def migrate_mistake_logs(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(mistake_logs)").fetchall()}
    migrations = {
        "next_review_date": "ALTER TABLE mistake_logs ADD COLUMN next_review_date TEXT",
        "review_interval": "ALTER TABLE mistake_logs ADD COLUMN review_interval INTEGER NOT NULL DEFAULT 1",
        "review_count": "ALTER TABLE mistake_logs ADD COLUMN review_count INTEGER NOT NULL DEFAULT 0",
        "last_reviewed_at": "ALTER TABLE mistake_logs ADD COLUMN last_reviewed_at TEXT",
        "mastered": "ALTER TABLE mistake_logs ADD COLUMN mastered INTEGER NOT NULL DEFAULT 0",
        "error_category": "ALTER TABLE mistake_logs ADD COLUMN error_category TEXT",
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


def seed_verbs_if_empty():
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
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


def sqlite_dicts(query, params=()):
    ensure_settings_store()
    with sqlite3.connect(SQLITE_SETTINGS_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def sqlite_one(query, params=()):
    rows = sqlite_dicts(query, params)
    return rows[0] if rows else None


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
    return current


def get_db_connection():
    import psycopg

    return psycopg.connect(DATABASE_URL)


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


def choose_gemini_model():
    if GEMINI_MODEL:
        return GEMINI_MODEL

    try:
        models = list_gemini_models()
    except Exception:
        return "gemini-3.1-flash-lite"

    usable = []
    for model in models:
        name = model.get("name", "").replace("models/", "")
        methods = model.get("supportedGenerationMethods", [])
        if name and "generateContent" in methods:
            usable.append(name)

    preferred = [
        "gemini-3.1-flash-lite",
        "gemini-3-flash-lite",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash-lite",
        "gemini-3.1-flash",
        "gemini-3-flash",
        "gemini-2.5-flash",
    ]
    for keyword in preferred:
        for name in usable:
            if keyword in name:
                return name
    return usable[0] if usable else "gemini-3.1-flash-lite"


def call_gemini(prompt):
    if not GEMINI_API_KEY:
        raise RuntimeError("尚未設定 Gemini API Key。")

    model_name = choose_gemini_model()
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
        with urllib.request.urlopen(req, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"AI 服務請求失敗：{detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"無法連接 AI 服務：{e.reason}") from e

    if "error" in data:
        raise RuntimeError(data["error"].get("message", "AI 服務回傳錯誤。"))

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError("AI 回傳格式不正確。") from e


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


def save_material_for_today(material, settings):
    ensure_database()
    date = today_string()
    vocab_list = material.get("vocab") or []
    verb_list = material.get("verbs") or []
    grammar = material.get("grammar") or {}
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
            }
        )

    if DATABASE_URL:
        placeholders = ", ".join(["%s"] * len(COLUMNS))
        columns_sql = ", ".join(COLUMNS)
        rows = [tuple(row[col] for col in COLUMNS) for row in new_rows]
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
            vocabulary.append({"word": row["vocab_word"], "reading": row["vocab_reading"], "meaning": row["vocab_meaning"]})
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

    return {
        "date": target_date,
        "targetLevel": first.get("target_level", ""),
        "vocabulary": vocabulary,
        "verbs": verbs,
        "grammar": {"title": first["grammar_title"], "exp": first["grammar_exp"], "examples": examples},
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


def generate_daily_material(use_sample=False, posted_settings=None, app_url=None):
    settings = save_settings_file(posted_settings) if posted_settings else load_settings()
    raw_material = sample_material(settings) if use_sample else parse_json_from_ai(call_gemini(build_prompt(settings)))
    date = save_material_for_today(raw_material, settings)
    material = material_by_date(date)

    telegram_status = "未發送"
    try:
        send_telegram_message(build_telegram_notification(material, date, app_url))
        telegram_status = "Telegram 通知已發送"
    except Exception as e:
        telegram_status = f"Telegram 通知發送失敗：{e}"

    return {
        "message": f"{date} 的 {settings['target_level']} 學習材料已經生成並保存。{telegram_status}",
        "date": date,
        "telegram": telegram_status,
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
                    error_category = ?
                WHERE id = ?
                """,
                (" / ".join(answers), int(existing["mistake_count"]) + 1, now, iso_date_after(1), category, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO mistake_logs
                (
                    verb_id, question_type, user_wrong_answer, mistake_count,
                    status, last_reviewed_at, next_review_date, review_interval,
                    review_count, mastered, error_category
                )
                VALUES (?, ?, ?, 1, 'learning', ?, ?, 1, 0, 0, ?)
                """,
                (verb_id, question_type, wrong_answer, now, iso_date_after(1), category),
            )
        conn.commit()


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
    candidates = []
    for value in features:
        if value and value != "*" and re.search(r"[\u30a1-\u30f6]", value):
            candidates.append(value)
    return kana_to_hiragana(candidates[-1] if candidates else surface)


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


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/verb-practice")
def verb_practice_page():
    return render_template("verb_practice.html", form_labels=VERB_FORM_LABELS)


@app.get("/mistake-review")
def mistake_review_page():
    return render_template("mistake_review.html", form_labels=VERB_FORM_LABELS)


@app.get("/grammar-analyzer")
def grammar_analyzer_page():
    return render_template("grammar_analyzer.html")


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
        return jsonify(
            generate_daily_material(
                use_sample=request.args.get("sample") == "1",
                posted_settings=request.get_json(silent=True) or {},
                app_url=request.host_url.rstrip("/"),
            )
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/cron/daily-push")
def api_cron_daily_push():
    if CRON_SECRET and request.args.get("secret") != CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    try:
        return jsonify(generate_daily_material(app_url=APP_URL)), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    answer = str(data.get("answer", "")).strip()
    if not verb_id or question_type not in QUESTION_TYPES or not answer:
        return jsonify({"error": "題目或答案不完整。"}), 400
    verb = sqlite_one("SELECT * FROM verbs WHERE id = ?", (verb_id,))
    if not verb:
        return jsonify({"error": "找不到動詞題目。"}), 404
    correct = verb[question_type]
    is_correct = answer == correct
    if not is_correct:
        add_mistake(int(verb_id), question_type, answer)
    return jsonify(
        {
            "correct": is_correct,
            "correct_answer": correct,
            "verb_group": group_label(verb["verb_group"]),
            "rule": form_rule_explanation(verb, question_type),
            "mistake_added": not is_correct,
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
        m.mastered, m.error_category,
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
        row["correct_answer"] = row[row["question_type"]]
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
    answer = str(data.get("answer", "")).strip()
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
    is_correct = answer == correct
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
                    error_category = ?
                WHERE id = ?
                """,
                (f"{row['user_wrong_answer']} / {answer}", now, iso_date_after(1), error_category, mistake_id),
            )
        conn.commit()
    return jsonify(
        {
            "correct": is_correct,
            "correct_answer": correct,
            "rule": form_rule_explanation(row, row["question_type"]),
            "next_review_date": iso_date_after(next_interval) if is_correct else iso_date_after(1),
            "review_interval": next_interval if is_correct else 1,
        }
    )


@app.post("/api/analyze_japanese")
def api_analyze_japanese():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify({"success": False, "error": "請輸入日文句子。"}), 400
    parsed, error = analyze_with_mecab(text)
    if error:
        return jsonify({"success": False, "error": error})
    grammar_patterns, notes = detect_grammar_patterns(text)
    return jsonify(
        {
            "success": True,
            "original": text,
            "reading_hiragana": parsed["reading_hiragana"],
            "tokens": parsed["tokens"],
            "particles": parsed["particles"],
            "verb_forms": parsed["verb_forms"],
            "grammar_patterns": grammar_patterns,
            "common_misunderstandings": common_misunderstandings_for(text, grammar_patterns),
            "notes": notes,
        }
    )


@app.get("/api/dashboard")
def api_dashboard():
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
    review_items = query_mistakes({}, limit=5)
    return jsonify(
        {
            "today": today,
            "has_today_material": bool(today_material),
            "target_level": settings["target_level"],
            "vocab_count": len(today_material["vocabulary"]) if today_material else 0,
            "verb_count": len(today_material["verbs"]) if today_material else 0,
            "quiz_total": int(settings["mcq_count"]) + int(settings["fill_count"]),
            "today_new_mistakes": len(today_mistakes),
            "last_7_days": [{"date": date, "studied": date in material_dates} for date in reversed(last_7_dates)],
            "streak_days": len(active_days),
            "review_items": review_items,
        }
    )


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
        questions.append({"type": "FILL", "q": f"請寫出「{base}」的 {form_name}。", "ans": row[column]})

    return jsonify(questions if questions else {"error": "目前沒有足夠資料可以產生測驗。"})


if __name__ == "__main__":
    ensure_database()
    ensure_settings_store()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
