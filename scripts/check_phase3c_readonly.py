"""GET-only archive/history verification; raw material responses stay private."""

import argparse
import datetime as dt
import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://japanese-learning-kasuga72.onrender.com"


def main(args):
    snapshot = json.loads(args.history.read_text(encoding="utf-8"))
    receipts = []
    args.private_dir.mkdir(parents=True, exist_ok=True)

    def get(path, name):
        request = urllib.request.Request(BASE + path, method="GET", headers={"User-Agent": "Phase3C-readonly-audit"})
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            receipts.append({"path": path.split("?", 1)[0], "http_status": response.status,
                             "sha256": hashlib.sha256(raw).hexdigest()})
        (args.private_dir / name).write_bytes(raw)
        return json.loads(raw)

    archive = get("/api/archive-dates?limit=365", "archive_current.json")
    dates = archive.get("dates", []) if isinstance(archive, dict) else archive
    keys = {v["material_key"] for row in dates for v in row.get("versions", [])}
    if not keys:
        raise ValueError("Unexpected archive schema; no conclusion about missing dates")
    old = {m["material_key"] for m in snapshot["raw_material_projection"]}
    selected = set()
    for item in snapshot["items"]:
        if (item.get("dictionary_form") or item.get("word")) in {"浴びる", "掛ける", "着く"}:
            selected.update(r["material_key"] for r in item.get("seen_records") or [])
    for key in sorted(selected):
        get("/api/materials?" + urllib.parse.urlencode({"material_key": key}), f"material_{key}.json")
    today = dt.date(2026, 11, 1)
    recent = [(dt.date(2026, 10, 29) + dt.timedelta(days=i)).isoformat() for i in range(4)]
    material_days = {key[:10] for key in keys}
    report = {"checked_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "method": "GET only",
              "archive_versions": len(keys), "archive_matches_saved_snapshot": keys == old,
              "latest_material_key": max(keys), "new_material_versions": len(keys - old),
              "historical_reading_samples_fetched": len(selected), "missing_recent_dates": [d for d in recent if d not in material_days],
              "today_still_pending": today.isoformat() not in material_days,
              "receipts": receipts, "formal_writes": 0, "gemini_calls": 0,
              "live_commit": "NOT VERIFIED; no authenticated Render deployment metadata available",
              "transaction_consistent_snapshot": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
