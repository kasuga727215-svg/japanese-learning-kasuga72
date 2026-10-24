"""Dictionary-backed verb evidence; this never adds words or assigns JLPT levels."""

import csv
import os
import threading
import unicodedata
from functools import lru_cache


_local = threading.local()
# UniDic's best path splits this kana spelling into two auxiliaries.
# Verify the attested orthographic entry and its reading instead of a suffix rule.
_ORTHOGRAPHIC_ALIASES = {"だます": ("騙す", "ダマス")}


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


@lru_cache(maxsize=32768)
def verified_verb_evidence(surface):
    text = unicodedata.normalize("NFKC", str(surface or "")).strip()
    nodes = _nodes(text)
    if len(nodes) == 1 and _dictionary_form(nodes[0]):
        return {"accepted": True, "reason": "verified_dictionary_form", "evidence": "unidic-lite:1.0.8"}
    # Noun + suru is accepted only when the dictionary marks the stem サ変可能.
    if len(nodes) >= 2 and text.endswith(("する", "ずる")) and _dictionary_form(nodes[-1]):
        stems = nodes[:-1]
        if all(stat == 0 and f[0] == "名詞" for _, stat, f in stems) and any(
            "サ変" in field for _, _, f in stems for field in f[:4]
        ):
            return {"accepted": True, "reason": "verified_suru_stem", "evidence": "unidic-lite:1.0.8"}
    if text in _ORTHOGRAPHIC_ALIASES:
        orthography, reading = _ORTHOGRAPHIC_ALIASES[text]
        alias_nodes = _nodes(orthography)
        if len(alias_nodes) == 1 and _dictionary_form(alias_nodes[0]) and alias_nodes[0][2][6] == reading:
            return {"accepted": True, "reason": "verified_dictionary_alias", "evidence": f"unidic-lite:1.0.8:{orthography}"}
    return {"accepted": False, "reason": "not_verified_dictionary_verb", "suggested_type": "word"}


@lru_cache(maxsize=32768)
def verified_word_evidence(surface):
    text = unicodedata.normalize("NFKC", str(surface or "")).strip()
    if verified_verb_evidence(text)["accepted"]:
        return False
    nodes = _nodes(text)
    allowed = {"名詞", "形容詞", "形状詞", "副詞", "接続詞", "連体詞", "感動詞", "代名詞"}
    # Verify whole lexical entries, not a guessed kana suffix (e.g. いす).
    if len(nodes) == 1:
        _, stat, fields = nodes[0]
        return stat == 0 and fields[0] in allowed
    return bool(nodes) and all(stat == 0 and fields[0] in allowed for _, stat, fields in nodes)
