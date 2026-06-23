import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TRANSIENT_HTTP_STATUSES = {502, 503, 504}


def build_cron_url():
    app_url = os.environ.get("APP_URL", "").strip().rstrip("/")
    if not app_url:
        raise RuntimeError("APP_URL is required for cron_generate.py")

    params = {"mode": os.environ.get("CRON_GENERATION_MODE", "local")}
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
    print(payload.get("message", "Cron material generation completed."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
