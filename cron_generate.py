import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


def build_cron_url():
    app_url = os.environ.get("APP_URL", "").strip().rstrip("/")
    if not app_url:
        raise RuntimeError("APP_URL is required for cron_generate.py")

    params = {"mode": os.environ.get("CRON_GENERATION_MODE", "local")}
    secret = os.environ.get("CRON_SECRET", "").strip()
    if secret:
        params["secret"] = secret
    return f"{app_url}/api/cron/daily-push?{urlencode(params)}"


def main():
    url = build_cron_url()
    print(f"[cron-generate] calling {url.split('secret=', 1)[0]}secret=***" if "secret=" in url else f"[cron-generate] calling {url}")
    try:
        with urlopen(url, timeout=120) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"[cron-generate] http_error status={exc.code} body={body[:4000]}")
        return 1
    except URLError as exc:
        print(f"[cron-generate] request_failed reason={exc}")
        return 1

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        print(f"[cron-generate] non_json_response status={status} body={body[:4000]}")
        return 1

    print(json.dumps(payload, ensure_ascii=False))
    if not payload.get("ok"):
        return 1
    print(payload.get("message", "Cron material generation completed."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
