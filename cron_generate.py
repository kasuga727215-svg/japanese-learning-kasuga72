import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


TRANSIENT_HTTP_STATUSES = {502, 503, 504}


def build_cron_url():
    app_url = os.environ.get("APP_URL", "").strip().rstrip("/")
    if not app_url:
        raise RuntimeError("APP_URL is required for cron_generate.py")

    params = {"mode": os.environ.get("CRON_GENERATION_MODE", "local"), "notify": "0"}
    secret = os.environ.get("CRON_SECRET", "").strip()
    if secret:
        params["secret"] = secret
    return f"{app_url}/api/cron/daily-push?{urlencode(params)}"


def safe_url(url):
    return f"{url.split('secret=', 1)[0]}secret=***" if "secret=" in url else url


def summarize_body(body):
    text = (body or "").strip()
    lowered = text[:300].lower()
    if "<!doctype html" in lowered or "<html" in lowered:
        return "Render returned an HTML error page instead of JSON."
    return text[:1200].replace("\n", " ")


def call_cron_endpoint(url):
    request = Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "japanese-learning-cron/1.0",
        },
    )
    try:
        with urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body, None
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body, exc


def build_fallback_telegram_message(payload):
    app_url = os.environ.get("APP_URL", "").strip().rstrip("/")
    material_key = str(payload.get("material_key") or "").strip()
    link = app_url
    if app_url and material_key:
        link = f"{app_url}?material_key={quote(material_key)}"
    validation = payload.get("count_validation") or {}
    word_count = validation.get("word_count_actual") or validation.get("word_count_requested") or ""
    verb_count = validation.get("verb_count_actual") or validation.get("verb_count_requested") or ""
    date = payload.get("date") or payload.get("material_date") or ""
    mode = payload.get("generation_mode") or "local"
    lines = [
        "每日教材已生成",
        f"日期：{date}",
        f"版本：{material_key}",
        f"單字：{word_count}",
        f"動詞：{verb_count}",
        f"模式：{mode}",
    ]
    if link:
        lines.append(f"開啟學習頁：{link}")
    return "\n".join(line for line in lines if not line.endswith("："))


def send_telegram_from_cron(message):
    token = os.environ.get("TG_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[cron-telegram] skipped reason=missing_token_or_chat_id")
        return False
    timeout = max(1, int(os.environ.get("CRON_TELEGRAM_TIMEOUT_SECONDS", "5") or 5))
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = Request(url, data=payload, method="POST")
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            data = json.loads(body)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if data.get("ok"):
            print(f"[cron-telegram] sent ok=true elapsed_ms={elapsed_ms}")
            return True
        print(f"[cron-telegram] failed reason=telegram_api_not_ok body_summary={summarize_body(body)}")
        return False
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        print(f"[cron-telegram] failed reason={exc} elapsed_ms={elapsed_ms}")
        return False


def main():
    url = build_cron_url()
    retries = max(1, int(os.environ.get("CRON_HTTP_RETRIES", "4") or 4))
    retry_delay = max(1, int(os.environ.get("CRON_HTTP_RETRY_DELAY_SECONDS", "12") or 12))
    print(f"[cron-generate] calling POST {safe_url(url)}")

    body = ""
    status = 0
    for attempt in range(1, retries + 1):
        try:
            status, body, error = call_cron_endpoint(url)
        except URLError as exc:
            print(f"[cron-generate] request_failed attempt={attempt}/{retries} reason={exc}")
            if attempt < retries:
                time.sleep(retry_delay)
                continue
            return 1

        if status in TRANSIENT_HTTP_STATUSES and attempt < retries:
            print(
                "[cron-generate] transient_upstream_error "
                f"code={status} attempt={attempt}/{retries} body_summary={summarize_body(body)}"
            )
            time.sleep(retry_delay)
            continue
        if error:
            print(f"[cron-generate] api_error code={status} body_summary={summarize_body(body)}")
            return 1
        break

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        print(f"[cron-generate] non_json_response code={status} body_summary={summarize_body(body)}")
        return 1

    print(json.dumps(payload, ensure_ascii=False))
    if not payload.get("ok"):
        return 1
    telegram_message = payload.get("telegram_message") or payload.get("notification_message") or build_fallback_telegram_message(payload)
    if telegram_message:
        send_telegram_from_cron(telegram_message)
    print(payload.get("message", "Cron material generation completed."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
