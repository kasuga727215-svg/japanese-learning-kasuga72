# Source-Sense Evidence Attribution

`lexical_evidence_n5.json` and `lexical_sense_alignments.json` are derived data
licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
The license applies to these data files, not to unrelated application code.

- [OpenJLPT](https://github.com/evanclan/OpenJLPT), its contributors and Jonathan
  Waller's JLPT lists supply the N5 source surfaces, readings, level assignments
  and source glosses. The input is the checked-in
  `data/external_lexicon/openjlpt-vocab-n5.csv`; its SHA-256 is in the manifest.
  Public text digests normalize UTF-8 BOM/newlines for Windows/Linux checkouts;
  the formal CSV audit and dictionary archive hashes remain raw-byte hashes.
- [JMdict](https://www.edrdg.org/wiki/JMdict-EDICT_Dictionary_Project.html),
  copyright the Electronic Dictionary Research and Development Group (EDRDG),
  supplies dictionary entry IDs, readings, spellings, senses and POS evidence.
  Used under the [EDRDG licence](https://www.edrdg.org/edrdg/licence.html).
- [jmdict-simplified](https://github.com/scriptin/jmdict-simplified) supplies the
  structured conversion, release `3.6.2+20261026175559` (dictionary date
  2026-10-26). The archive SHA-256 is
  `c25f1594482fcb55d02f66609584367a04136af9e4cfaa15c207eb41500bc0d3`.

Changes: exact source/sense matching, POS classification, a compact evidence
projection, and three explicit source-gloss paraphrase alignments. Alignment
records retain the source gloss, dictionary entry and sense, dictionary gloss
and rationale. This is not an official JLPT vocabulary list: levels remain the
source provider's assignments, not grades inferred from JMdict.

Reproduce with `scripts/build_lexical_evidence.py --dictionary <pinned-zip>
--output data/lexical_evidence_n5.json`. No formal PostgreSQL export, material
history, example sentence corpus, credentials or private logs are included in
these data files. Historical usage is never baked into the public evidence.
