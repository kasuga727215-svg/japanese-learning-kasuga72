"""Versioned source-sense evidence, separate from material usage and bank state."""

import hashlib
import json
import unicodedata
from functools import lru_cache
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "data/lexical_evidence_n5.json"
VERSION = "openjlpt-jmdict-sense-v1"


def text_digest(path):
    # Public text evidence must have the same identity on Windows and Linux.
    return hashlib.sha256(path.read_text(encoding="utf-8-sig").encode("utf-8")).hexdigest()


def normalize(text):
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def reading_key(text):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in normalize(text))


@lru_cache(maxsize=1)
def manifest():
    raw = PATH.read_bytes()
    data = json.loads(raw)
    root = PATH.parents[1]
    source = root / data["source"]
    alignments = root / "data/lexical_sense_alignments.json"
    if data["version"] != VERSION or text_digest(source) != data["source_sha256"]:
        raise ValueError("lexical_evidence_source_version_mismatch")
    if text_digest(alignments) != data["alignment_sha256"]:
        raise ValueError("lexical_evidence_alignment_version_mismatch")
    data["fingerprint"] = text_digest(PATH)
    data["by_surface"] = {normalize(r["surface"]): r for r in data["rows"]}
    return data


def source_record(row, require_source=True):
    if normalize(row.get("jlpt_level") or row.get("level")) != "N5":
        return None
    if require_source and str(row.get("source") or "").lower() != "openjlpt":
        return None
    surface = normalize(row.get("surface") or row.get("d") or row.get("w") or row.get("dictionary_form") or row.get("word"))
    data = manifest()["by_surface"].get(surface)
    if not data or data["status"] == "unknown":
        return None
    reading = reading_key(row.get("reading_hiragana") or row.get("reading"))
    if reading and reading not in {reading_key(m["reading"]) for m in data["matches"]}:
        return None
    return data


def classify_source_row(row, fallback):
    record = source_record(row)
    if record is None:
        return fallback
    data = manifest()
    result = dict(fallback)
    result.update(status=record["status"], reason="verified_source_dictionary_sense",
                  source="OpenJLPT + JMdict", retryable=False, verification_route="",
                  source_fingerprint=hashlib.sha256(json.dumps([record, data["fingerprint"]], ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                  source_sense_evidence={"version": data["version"], "manifest_sha256": data["fingerprint"],
                                         "source_sha256": data["source_sha256"], "source_line": record["source_line"],
                                         "source_gloss": record["source_gloss"], "dictionary_sha256": data["dictionary_sha256"],
                                         "matches": record["matches"]})
    return result


def identity_options(row):
    record = source_record(row, require_source=False)
    if record is None:
        return []
    return [{"dictionary_entry": m["entry_id"], "sense": m["sense"], "reading": m["reading"],
             "aliases": m["aliases"]} for m in record["matches"]]


def source_sense_hints(candidates):
    hints = []
    for candidate in candidates:
        record = source_record(candidate)
        if record:
            hints.append({"surface": record["surface"], "source_gloss": record["source_gloss"],
                          "readings": sorted({m["reading"] for m in record["matches"]})})
    return hints


def append_source_sense_hints(prompt, candidates):
    hints = source_sense_hints(candidates)
    if not hints:
        return prompt
    return (prompt + " Preserve the following verified source senses and readings; do not substitute a homonym or another sense. "
            "Keep the existing compact output format. Source sense hints: " + json.dumps(hints, ensure_ascii=False))


def enrichment_reading_matches(payload, candidate):
    record = source_record(candidate)
    if record is None:
        return True
    actual = reading_key(payload.get("reading_hiragana") or payload.get("reading") or payload.get("r"))
    return actual in {reading_key(m["reading"]) for m in record["matches"]}


class ExclusionKeys(set):
    def __init__(self, keys, readings=None):
        super().__init__(keys)
        self.readings = readings or {}


def history_overlap(row, keys, history_identities=None):
    """No sound-based merging; unresolved historical senses are conservatively held."""
    readings = getattr(keys, "readings", {})
    keys = {normalize(k) for k in keys}
    for option in identity_options(row):
        aliases = set(option["aliases"]) & keys
        for alias in aliases:
            observed = {reading_key(r) for r in readings.get(alias, []) if r}
            if observed and reading_key(option["reading"]) not in observed:
                continue
            known = (history_identities or {}).get(alias)
            identity = (option["dictionary_entry"], option["sense"])
            if known is not None and identity not in {tuple(x) for x in known}:
                continue
            return {"reason": "history_lexical_identity_overlap" if known is not None else "history_lexical_identity_uncertain",
                    "matched_alias": alias, "entry_id": identity[0], "sense": identity[1]}
    return None
