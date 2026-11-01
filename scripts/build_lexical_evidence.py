"""Build auditable evidence from public sources; never imports a database."""

import argparse
import csv
import hashlib
import json
import re
import sys
import unicodedata
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.lexical_evidence import text_digest
DICT_SHA = "c25f1594482fcb55d02f66609584367a04136af9e4cfaa15c207eb41500bc0d3"
DICT_URL = "https://github.com/scriptin/jmdict-simplified/releases/tag/3.6.2%2B20261026175559"


def norm(value):
    return unicodedata.normalize("NFKC", value).strip()


def gloss_key(value):
    value = re.sub(r"\([^)]*\)|\[[^]]*\]", "", value.lower())
    return " ".join(re.findall(r"[a-z]+", value))


def allowed(values, value):
    return "*" in values or value in values


def build(dictionary, output):
    raw = dictionary.read_bytes()
    if hashlib.sha256(raw).hexdigest() != DICT_SHA:
        raise ValueError("Unreviewed dictionary version/digest")
    with zipfile.ZipFile(dictionary) as archive:
        data = json.loads(archive.read(archive.namelist()[0]))
    index = defaultdict(list)
    for entry in data["words"]:
        for text in {x["text"] for x in entry["kanji"] + entry["kana"]}:
            index[norm(text)].append(entry)
    source = ROOT / "data/external_lexicon/openjlpt-vocab-n5.csv"
    alignment_file = ROOT / "data/lexical_sense_alignments.json"
    alignments = json.loads(alignment_file.read_text(encoding="utf-8"))
    if alignments["dictionary_sha256"] != DICT_SHA:
        raise ValueError("Paraphrase review is for a different dictionary")
    rows = []
    with source.open(encoding="utf-8-sig", newline="") as handle:
        for line, row in enumerate(csv.DictReader(handle), 2):
            surface, reading = norm(row["word"]), norm(row.get("reading", ""))
            source_glosses = {gloss_key(x) for x in row["meanings"].split(";")} - {""}
            matches = []
            for entry in index[surface]:
                kanji = {k["text"] for k in entry["kanji"]}
                for kana in entry["kana"]:
                    if reading and reading != kana["text"]:
                        continue
                    if surface in kanji and not allowed(kana["appliesToKanji"], surface):
                        continue
                    if surface not in kanji and surface != kana["text"]:
                        continue
                    for number, sense in enumerate(entry["sense"], 1):
                        if not allowed(sense["appliesToKana"], kana["text"]):
                            continue
                        if surface in kanji and not allowed(sense["appliesToKanji"], surface):
                            continue
                        matching_glosses = [g["text"] for g in sense["gloss"] if gloss_key(g["text"]) in source_glosses]
                        reviewed = [a for a in alignments["alignments"] if a["surface"] == surface and a["source_gloss"] == row["meanings"]
                                    and a["entry_id"] == entry["id"] and a["sense"] == number
                                    and a["dictionary_gloss"] in {g["text"] for g in sense["gloss"]}]
                        matching_glosses += [a["dictionary_gloss"] for a in reviewed]
                        if not matching_glosses or set(sense["misc"]) & {"arch", "obs"}:
                            continue
                        pos = sense["partOfSpeech"]
                        verb = any(re.match(r"^(?:v[1-5]|vk|vz|vn|vr)", p) for p in pos)
                        verb |= surface.endswith(("する", "ずる")) and any(p.startswith("vs") for p in pos)
                        word = any(p in {"n", "adj-i", "adj-na", "adj-no", "adj-pn", "adv", "adv-to", "conj", "int", "pn", "prt", "pref", "suf", "num", "ctr"} for p in pos)
                        kind = "verified_verb" if verb and not word else "verified_word" if word and not verb else "unknown"
                        aliases = {kana["text"]}
                        aliases.update(k for k in kanji if allowed(kana["appliesToKanji"], k) and allowed(sense["appliesToKanji"], k))
                        matches.append({"entry_id": entry["id"], "sense": number, "pos": pos, "status": kind,
                                        "reading": kana["text"], "aliases": sorted(aliases), "source_gloss_match": matching_glosses,
                                        "alignment": "reviewed_paraphrase" if reviewed else "normalized_exact_gloss",
                                        "sense_glosses": [g["text"] for g in sense["gloss"]]})
            statuses = {m["status"] for m in matches}
            status = next(iter(statuses)) if len(statuses) == 1 else "unknown"
            rows.append({"surface": surface, "source_reading": reading, "source_gloss": row["meanings"],
                         "source_line": line, "jlpt_level": "N5", "status": status, "matches": matches})
    result = {"version": "openjlpt-jmdict-sense-v1", "source": str(source.relative_to(ROOT)).replace("\\", "/"),
              "source_sha256": text_digest(source), "dictionary_url": DICT_URL,
              "dictionary_sha256": DICT_SHA, "dictionary_date": data["dictDate"], "license": "CC BY-SA 4.0",
              "alignment_sha256": text_digest(alignment_file),
              "text_hash_normalization": "UTF-8 without BOM, universal LF newlines; dictionary archive hash remains raw bytes",
              "matching_policy": "exact surface, reading/kanji/sense restrictions, exact normalized source gloss",
              "rows": rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    from collections import Counter
    print(dict(Counter(row["status"] for row in rows)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dictionary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.dictionary, args.output)
