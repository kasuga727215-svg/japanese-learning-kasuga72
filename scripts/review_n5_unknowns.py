"""Prepare an offline review queue, never approve unknowns or mutate a pool."""

import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.verb_candidate_audit import CLASSIFIER_VERSION, classify_lexeme, _nodes


def main():
    directory = ROOT / "reports/phase3br_2026_10_30"
    source = json.loads((directory / "after.json").read_text(encoding="utf-8"))
    rows, details = [], []
    for row in source["records"]:
        if row["level"] != "N5" or row["classification"]["status"] != "unknown":
            continue
        evidence = classify_lexeme(row["surface"], row["reading"])
        assert evidence == row["classification"], "Stored evidence differs from the actual classifier"
        path = ROOT / row["source"]
        entries = evidence["evidence"]
        review_type = ("sense_disambiguation_required" if evidence["reason"] == "ambiguous_word_verb_sense"
                       else "reading_segmentation_or_dictionary_review_required")
        rows.append({"surface": row["surface"], "reading": row["reading"], "meaning": row["meaning"],
                     "jlpt_level": row["level"], "level_evidence": "checked-in OpenJLPT file; not official JLPT list",
                     "source_file": row["source"], "source_line": row["line"],
                     "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                     "classifier_version": CLASSIFIER_VERSION, "status": evidence["status"], "reason": evidence["reason"],
                     "lemmas": " | ".join(sorted({e["lemma"] for e in entries})),
                     "orthBases": " | ".join(sorted({e["orthBase"] for e in entries})),
                     "pos_evidence": " | ".join(sorted({"/".join(e["pos"]) for e in entries})),
                     "review_required": review_type, "approved_for_generation": False})
        details.append({"source": row, "best_path": _nodes(row["surface"]), "current_evidence": evidence})
    assert len(rows) == 102
    with (directory / "n5_unknown_review.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    result = {"scope": "Local checked-in OpenJLPT N5 only, NOT formal PostgreSQL unknowns",
              "inventory_verified": False, "review_queue_count": len(rows), "approved_count": 0,
              "reason_counts": dict(Counter(row["reason"] for row in rows)),
              "specific_sense_probes_not_source_pos_overrides": [],
              "pending_external_checks": "JMdict sense/POS/reading restrictions and source gloss comparison; no bulk acceptance",
              "formal_database_writes": 0, "gemini_calls": 0, "details": details}
    for surface in ("ある", "かかる", "はく"):
        original = next(r for r in rows if r["surface"] == surface)
        result["specific_sense_probes_not_source_pos_overrides"].append({
            "surface": surface, "source_gloss": original["meaning"],
            "without_source_pos": classify_lexeme(surface, original["reading"]),
            "hypothetical_trusted_verb_pos": classify_lexeme(surface, original["reading"], "動詞"),
            "persisted_override": False})
    (directory / "n5_unknown_review.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in {"details", "specific_sense_probes_not_source_pos_overrides"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
