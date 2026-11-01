"""Summarize executed reports, without copying production rows or material bodies."""

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports/phase3c_2026_11_01"


def main():
    reports = {name: json.loads((REPORT / f"{name}_tests.json").read_text(encoding="utf-8"))
               for name in ("generation", "selector", "lexical", "snapshot")}
    results = {}
    for name, data in reports.items():
        skipped = data.get("skipped", data.get("skips", 0))
        skipped = len(skipped) if isinstance(skipped, list) else skipped
        results[name] = {"run": data["tests_run"], "passed": data["tests_run"] - skipped - data["failures"] - data["errors"],
                         "failed": data["failures"], "errors": data["errors"], "skipped": skipped}
    supply = json.loads((REPORT / "supply_tests.json").read_text(encoding="utf-8"))
    summary = {"baseline": "3c6b2a6e08364b3bc11c90aced1f81ff47139a41", "readiness": "NOT READY",
               "snapshot_sha256": supply["snapshot_sha256"], "available_verified_fresh_N5_verbs": supply["available_count"],
               "regression": results, "tests_passed": sum(r["passed"] for r in results.values()),
               "tests_skipped": sum(r["skipped"] for r in results.values()),
               "capacity": [{k: r[k] for k in ("backend", "days", "successful_days", "capacity_acceptance")}
                            for r in supply["supply"]],
               "capacity_scope": "Formal N5 export + saved formal history; other JLPT levels are public fixtures, not formal inventories",
               "formal_postgres_live_inventory": "NOT VERIFIED; no direct read-only DSN",
               "formal_live_commit": "NOT VERIFIED; last known user-provided deployed baseline 69207c2",
               "real_gemini_calls": 0, "formal_writes": 0, "formal_backfill": False, "render_deployed": False}
    (REPORT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = []
    for scenario in supply["supply"]:
        for day in scenario["daily"]:
            rows.append({"backend": scenario["backend"], "horizon": scenario["days"], "day": day["day"], "ok": day["ok"],
                         "error": day["error"], "mock_calls": day["mock_calls"],
                         **{f"before_{key}": value for key, value in day["inventory_before"].items()},
                         **{f"after_{key}": value for key, value in day["inventory_after"].items()}})
    with (REPORT / "daily_inventory.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
