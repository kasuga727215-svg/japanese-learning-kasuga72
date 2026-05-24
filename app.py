import csv
import html
import json
import os
import random
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
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
        raise ValueError("AI 没有返回可读取的 JSON 内容。")
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
        raise RuntimeError("没有设置 Gemini API Key。")

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
        raise RuntimeError(f"AI 服务请求失败：{detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 AI 服务：{e.reason}") from e

    if "error" in data:
        raise RuntimeError(data["error"].get("message", "AI 服务返回错误。"))

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError("AI 返回格式不正确。") from e


def build_prompt(settings):
    level = settings["target_level"]
    topics = ["日常生活", "学校学习", "工作会话", "旅行交通", "购物点餐", "天气季节", "人际关系", "新闻社会", "抽象议题"]
    topic = random.choice(topics)
    seed = random.randint(100000, 999999)

    return f"""
你是一位专业日语老师。请为中文母语学习者生成一份 JLPT {level} 难度的日语教材。

主题：{topic}
随机编号：{seed}

请严格只输出 JSON，不要输出 Markdown，不要解释。所有中文说明必须使用简体中文。
难度必须符合 {level}，不要混入过高或过低级别的内容。

JSON 格式：
{{
  "vocab": [
    {{"word": "日本语单词", "reading": "假名读音", "meaning": "简体中文意思"}}
  ],
  "verbs": [
    {{
      "base": "辞书形（假名） - 简体中文意思",
      "masuStem": "连用形，也就是ます形去掉ます后的形态（假名）",
      "te": "て形（假名）",
      "ta": "た形（假名）",
      "nai": "ない形（假名）",
      "ba": "ば形（假名）",
      "causative": "使役形（假名）",
      "passive": "被动形（假名）",
      "causativePassive": "使役被动形（假名）"
    }}
  ],
  "grammar": {{
    "title": "文法标题",
    "exp": "简体中文说明",
    "examples": [
      {{"jp": "日文例句", "cn": "简体中文翻译"}}
    ]
  }}
}}

请严格生成刚好 {settings["vocab_count"]} 个单词、刚好 {settings["verb_count"]} 个动词、1 个文法点，并至少给 2 个例句。
""".strip()


def sample_material(settings=None):
    settings = normalize_settings(settings or load_settings())
    vocab_count = int(settings["vocab_count"])
    verb_count = int(settings["verb_count"])
    vocab = [
        {"word": "予定", "reading": "よてい", "meaning": "计划；预定"},
        {"word": "準備", "reading": "じゅんび", "meaning": "准备"},
        {"word": "確認", "reading": "かくにん", "meaning": "确认"},
        {"word": "資料", "reading": "しりょう", "meaning": "资料"},
        {"word": "進捗", "reading": "しんちょく", "meaning": "进度"},
        {"word": "提案", "reading": "ていあん", "meaning": "提案"},
    ]
    verbs = [
        {
            "base": "決める（きめる） - 决定",
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
            "base": "確認する（かくにんする） - 确认",
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
            "exp": "表示努力养成某个习惯，或尽量做到某件事。",
            "examples": [
                {"jp": "毎日日本語を聞くようにしています。", "cn": "我尽量每天听日语。"},
                {"jp": "忘れないようにメモしてください。", "cn": "请做笔记，以免忘记。"},
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
        f"<b>日语学习自动化系统</b>\n"
        f"日期：{html.escape(date)}\n"
        f"等级：{level}\n\n"
        f"<b>今日单词：</b>{words or '暂无'}\n"
        f"<b>今日文法：</b>{grammar_title}\n\n"
        f'<a href="{link}">点击打开学习页面</a>'
    )


def send_telegram_message(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("Telegram Token 或 Chat ID 没有设置。")

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"Telegram 返回错误：{data}")
    return data


def generate_daily_material(use_sample=False, posted_settings=None, app_url=None):
    settings = save_settings_file(posted_settings) if posted_settings else load_settings()
    raw_material = sample_material(settings) if use_sample else parse_json_from_ai(call_gemini(build_prompt(settings)))
    date = save_material_for_today(raw_material, settings)
    material = material_by_date(date)

    telegram_status = "未发送"
    try:
        send_telegram_message(build_telegram_notification(material, date, app_url))
        telegram_status = "Telegram 通知已发送"
    except Exception as e:
        telegram_status = f"Telegram 通知发送失败：{e}"

    return {
        "message": f"{date} 的 {settings['target_level']} 学习材料已经生成并保存。{telegram_status}",
        "date": date,
        "telegram": telegram_status,
    }


def shuffled(items):
    items = list(items)
    random.shuffle(items)
    return items


@app.route("/")
def index():
    return render_template("index.html")


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
            f"<b>Telegram 测试成功</b>\nFlask 日语学习系统可以发送消息。\n时间：{datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return jsonify({"message": "Telegram 测试消息已经发送成功。"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/quiz")
def api_quiz():
    df = read_database()
    if len(df) < 2:
        return jsonify({"error": "资料太少，先生成或累积几天学习材料后再测验。"})

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
                "q": f"「{row['vocab_word']}」的正确读音是哪一个？",
                "options": shuffled(options),
                "ans": row["vocab_reading"],
            }
        )

    forms = [
        ("连用形", "verb_masu_stem"),
        ("て形", "verb_te"),
        ("た形", "verb_ta"),
        ("ない形", "verb_nai"),
        ("ば形", "verb_ba"),
        ("使役形", "verb_causative"),
        ("被动形", "verb_passive"),
        ("使役被动形", "verb_causative_passive"),
    ]
    for _ in range(fill_count):
        if verb_rows.empty:
            break
        row = verb_rows.sample(1).iloc[0]
        form_name, column = random.choice(forms)
        base = row["verb_base"].split("-")[0].strip()
        questions.append({"type": "FILL", "q": f"请写出「{base}」的 {form_name}。", "ans": row[column]})

    return jsonify(questions if questions else {"error": "目前没有足够资料可以产生测验。"})


if __name__ == "__main__":
    ensure_database()
    ensure_settings_store()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
