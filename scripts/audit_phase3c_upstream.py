"""Evaluate a pinned public source update; never creates or imports pool rows."""

import argparse
import csv
import hashlib
import json
import re
import zipfile
from pathlib import Path

from audit_phase3c import load_snapshot
from build_lexical_evidence import DICT_SHA

UPSTREAM_COMMIT = "bf4e1eecd5d11406aaecdee910464507aef7fcdc"
UPSTREAM_SHA = "cbb221c9a3a92108fcd02fb088d9afacc70bc8c96570d5730425de0a49dad5f3"


def main(args):
    raw = args.upstream.read_bytes()
    if hashlib.sha256(raw).hexdigest() != UPSTREAM_SHA:
        raise ValueError("Unreviewed OpenJLPT revision")
    words = json.loads(raw)
    pool = load_snapshot(args.snapshot)
    history = set(json.loads(args.history.read_text(encoding="utf-8"))["history_keys"])
    old = {r["word"] for r in csv.DictReader((Path(__file__).resolve().parents[1] / "data/external_lexicon/openjlpt-vocab-n5.csv").open(encoding="utf-8-sig"))}
    if hashlib.sha256(args.dictionary.read_bytes()).hexdigest() != DICT_SHA:
        raise ValueError("Unreviewed dictionary revision")
    with zipfile.ZipFile(args.dictionary) as archive:
        dictionary = {int(r["id"]): r for r in json.loads(archive.read(archive.namelist()[0]))["words"]}
    verbs = [r for r in words if any(re.match(r"^(?:v[1-5]|vk|vz|vn|vr)", p) for p in r["pos"])
             or r["word"].endswith("する") and any(p.startswith("vs") for p in r["pos"])]
    new = []
    for row in verbs:
        if row["word"] in old:
            continue
        entry = dictionary[row["jmdict_id"]]
        aliases = {k["text"] for k in entry["kanji"] + entry["kana"]}
        new.append({k: row[k] for k in ("word", "reading", "meanings", "pos", "jmdict_id", "level")}
                   | {"present_in_formal_pool": row["word"] in {r["surface"] for r in pool},
                      "history_alias_hold": bool(aliases & history), "approved_for_import": False})
    report = {"upstream_commit": UPSTREAM_COMMIT, "upstream_sha256": UPSTREAM_SHA,
              "url": f"https://github.com/evanclan/OpenJLPT/blob/{UPSTREAM_COMMIT}/data/json/vocab/n5.json",
              "license": "CC BY-SA 4.0", "upstream_rows": len(words), "dictionary_form_verbs": len(verbs),
              "old_public_rows": len(old), "old_public_surfaces_missing_from_formal_pool": len(old - {r["surface"] for r in pool}),
              "new_verb_surfaces": new, "new_confirmed_history_fresh_verbs_from_update": sum(not r["history_alias_hold"] for r in new),
              "import_performed": False,
              "proposal": "Do not reimport the same source to claim extra capacity. Review additional licensed N5 curriculum evidence and historical senses first.",
              "required_additional_verified_fresh_verbs": {"seven_days": 6, "thirty_days": 29},
              "basis": "One confirmed fresh existing-pool verb; one N5 verb consumed daily in the all-level configuration. Reserve failures require additional headroom."}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("upstream", "snapshot", "history", "dictionary", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    main(parser.parse_args())
