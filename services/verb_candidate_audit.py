"""Dictionary-backed verb evidence; this never adds words or assigns JLPT levels."""

import csv
import hashlib
import json
import os
import re
import threading
import unicodedata
from functools import lru_cache


_local = threading.local()
CLASSIFIER_VERSION = "unidic-lattice-contract-v2"
DICTIONARY_VERSION = "unidic-lite:1.0.8/mecab-python3:1.0.12"
WORD_POS = {"名詞", "形容詞", "形状詞", "副詞", "接続詞", "連体詞", "感動詞", "代名詞", "助詞", "接頭辞", "接尾辞"}


def _tagger():
    if not hasattr(_local, "tagger"):
        import MeCab
        import unidic_lite

        null = "nul" if os.name == "nt" else "/dev/null"
        _local.tagger = MeCab.Tagger(f'-r {null} -d "{unidic_lite.DICDIR}"')
    return _local.tagger


def _nodes(text):
    result = []
    node = _tagger().parseToNode(text)
    while node:
        if node.surface:
            result.append((node.surface, node.stat, next(csv.reader([node.feature]))))
        node = node.next
    return result


def _dictionary_form(node):
    surface, stat, fields = node
    return (
        stat == 0
        and len(fields) > 10
        and fields[0] == "動詞"
        and fields[10] == surface
        and fields[5].startswith(("終止形", "連体形"))
    )


def _full_entries(text):
    import MeCab

    lattice = MeCab.Lattice()
    lattice.set_sentence(text)
    _tagger().parse(lattice)
    node = lattice.begin_nodes(0)
    entries = []
    # The best path is context-dependent (あびる/かける can be personal names).
    # Inspect all attested entries spanning the exact input, not guessed readings.
    while node:
        if node.surface == text and node.stat == 0:
            entries.append((node.surface, node.stat, next(csv.reader([node.feature]))))
        node = node.bnext
    return entries


def _reading(text):
    text = unicodedata.normalize("NFKC", str(text or "")).strip()
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def pos_contract(pos):
    """POS is supporting evidence, never a suffix-based lexical classifier."""
    text = unicodedata.normalize("NFKC", str(pos or "")).strip().lower()
    if not text or text in {"general", "unknown", "*", "未確認"}:
        return "unknown"
    parts = [p for p in re.split(r"[・/／,，、;\s]+", text) if p]
    verb = any(re.match(r"^(?:動詞|自動詞|他動詞|五段|一段|サ変(?:動詞|$)|サ行変格|する動詞|ずる動詞|verb|godan|ichidan|suru_verb|kuru_verb|v[1-5]|vs|vz|vk|v$)", p) for p in parts)
    word = any(re.match(r"^(?:名詞|普通名詞|形容詞|[いイなナ]形容詞|形容動詞|形状詞|副詞|接続詞|接續詞|連体詞|感動詞|代名詞|助詞|助数詞|接頭辞|接尾辞|noun|adjective|[ina]+-adjective|adverb|conjunction|interjection|pronoun|adj-|adv$|n$)", p) for p in parts)
    if verb and word:
        return "ambiguous"
    return "verb" if verb else "word" if word else "unknown"


def _entry_evidence(node):
    surface, _, f = node
    return {"surface": surface, "pos": f[:4], "conjugation_type": f[4], "conjugation_form": f[5],
            "lemma_reading": f[6], "lemma": f[7], "orthBase": f[10], "reading": f[11]}


@lru_cache(maxsize=32768)
def classify_lexeme(surface, reading="", source_pos=""):
    text = unicodedata.normalize("NFKC", str(surface or "")).strip()
    fingerprint = hashlib.sha256(json.dumps([text, reading, source_pos, CLASSIFIER_VERSION], ensure_ascii=False).encode()).hexdigest()
    def result(status, reason, entries=()):
        return {"status": status, "reason": reason, "source": DICTIONARY_VERSION,
                "classifier_version": CLASSIFIER_VERSION, "source_fingerprint": fingerprint,
                "evidence": [_entry_evidence(n) for n in entries],
                "retryable": status == "unknown", "verification_route": "source_pos_or_dictionary_review" if status == "unknown" else ""}
    if not text:
        return result("rejected_invalid", "empty_candidate")
    try:
        entries = _full_entries(text)
        if reading:
            entries = [n for n in entries if _reading(reading) in {_reading(n[2][6]), _reading(n[2][9]), _reading(n[2][11])}]
        verbs = [n for n in entries if _dictionary_form(n)]
        words = [n for n in entries if len(n[2]) > 11 and n[2][0] in WORD_POS and "固有名詞" not in n[2][:4]
                 and (n[2][0] != "形容詞" or n[2][10] == text)]
        if not verbs and text.endswith(("する", "ずる")):
            nodes = _nodes(text)
            stems = nodes[:-1]
            if (len(nodes) >= 2 and _dictionary_form(nodes[-1])
                    and all(stat == 0 and f[0] == "名詞" and "固有名詞" not in f[:4] for _, stat, f in stems)
                    and any("サ変" in field for _, _, f in stems for field in f[:4])
                    and (not reading or _reading(reading) == _reading("".join(f[9] for _, _, f in nodes)))):
                return result("verified_verb", "verified_suru_stem", nodes)
        pos = pos_contract(source_pos)
        if verbs and words and pos not in {"word", "verb"}:
            return result("unknown", "ambiguous_word_verb_sense", verbs + words)
        if verbs and words and pos == "word":
            return result("verified_word", "source_pos_resolves_attested_word_sense", words)
        if verbs:
            if pos == "word":
                return result("unknown", "source_pos_dictionary_conflict", verbs)
            return result("verified_verb", "verified_dictionary_form", verbs)
        if words:
            if pos == "verb":
                return result("unknown", "source_pos_dictionary_conflict", words)
            return result("verified_word", "verified_nonverb_lexeme", words)
        if entries and all("固有名詞" in n[2][:4] for n in entries):
            return result("rejected_invalid", "proper_noun_only", entries)
        return result("unknown", "reading_or_segmentation_unresolved" if reading else "pos_or_segmentation_unresolved", entries)
    except (ImportError, RuntimeError) as exc:
        data = result("unknown", "dictionary_unavailable")
        data["error_type"] = type(exc).__name__
        return data


def verified_verb_evidence(surface, reading="", source_pos=""):
    data = classify_lexeme(surface, reading, source_pos)
    return {**data, "accepted": data["status"] == "verified_verb",
            "suggested_type": "word" if data["status"] == "verified_word" else ""}


def verified_word_evidence(surface, reading="", source_pos=""):
    return classify_lexeme(surface, reading, source_pos)["status"] == "verified_word"
