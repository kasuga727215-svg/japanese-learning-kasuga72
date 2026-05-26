import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402


SEED_FILE = ROOT / "data" / "vocabulary_seed_n5_n3.json"


def main():
    with SEED_FILE.open("r", encoding="utf-8") as handle:
        items = json.load(handle)
    result = app.upsert_vocabulary_pool(items)
    print(
        json.dumps(
            {
                "seed_file": str(SEED_FILE),
                "total": len(items),
                "success": result.get("success", 0),
                "failed": result.get("failed", 0),
                "skipped": result.get("skipped", 0),
                "database": "postgres" if app.DATABASE_URL else "sqlite",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if result.get("failed", 0):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
