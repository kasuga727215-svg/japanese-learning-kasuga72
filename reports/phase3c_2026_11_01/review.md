# Phase 3C: Evidence Repair and Capacity Acceptance

## Release Decision: NOT READY

Code regressions pass, but the required history-aware seven-day supply gate
does not. Do not deploy this branch as a restoration of sustained daily supply.
Do not import data, generate/backfill formal materials or change fallback.

## Inputs and Version Safety

- Repair parent: `3c6b2a6e08364b3bc11c90aced1f81ff47139a41`.
- Independent branch: `codex/phase3c-n5-lexical-evidence-20261101`.
- Last user-reported deployed version: `69207c261d84a0fdb21f967a68cfa2e643713951`.
  Current Render Live SHA/deploy ID/rollback point are **NOT VERIFIED** without
  authenticated Render deployment metadata. No deployment was attempted.
- Original dirty main checkout and all unrelated modifications were preserved.
  Work took place in the separate `tmp-starter-db/phase3c-repair` worktree.
- The actual formal N5 UTF-8 export was read and its SHA-256 verified:
  `334ea156d466a359b8297b100e8e29c28208ce8e6d7cdc2c65c52c6c33420b7a`.
- History: 151 saved material versions, 1,169 canonical word/verb keys, from
  the retained read-only history snapshot. `used_in_material_count` was not
  used as evidence of freshness. Failed jobs and bank rows were not history.
- On 2026-11-01 the live GET-only archive returned exactly the same material
  keys, latest `2026-10-28-1`. Four relevant saved material bodies were also
  fetched read-only and their readings/senses checked. See `readonly_receipt.json`.
- **Not transaction-consistent:** pool export is dated 11/01, history capture
  10/30, and archive/body checks 11/01. Equal archive keys do not prove that
  every one of the 151 material bodies is unchanged. No direct formal DB
  read-only DSN or formal N1-N4 pool snapshot was available.

Raw pool rows, private history, full row audit and response bodies remain in
ignored/local files. They are not publication artifacts.

## Exact 10/30 Predicate Replay: CONFIRMED

The actual baseline `69207c2` application and classifier predicates were
loaded from Git. Current ID-only SQL eligibility was run over the actual
formal export in isolated SQLite; the old rejection predicates then received
those rows and the saved formal history. This reproduces the reported counts
exactly, without synthesizing extra N5 words:

| First exclusion | Rows |
|---|---:|
| SQL category | 1,281 |
| Saved materials history | 374 |
| not_verified_dictionary_verb | 244 |
| surface variant separator | 11 |
| known nonverb | 4 |
| number/counter | 36 |
| Selected | 0 |

These are mutually exclusive first-reason counts and sum to 1,950. In this
replay the 374 exclusions arise from saved history alone; no extra current
batch is needed. This is a predicate replay, not a claim that old broken
hydration was executed against formal PostgreSQL.

The 1,281 excluded rows are `seed_advanced`, business=651 / advanced=630.
They have no independently verified N5 source grading in this export. They
were not relabeled or unblocked. A grammatically analyzable synthetic compound
does not establish naturalness, beginner suitability or N5 provenance.

The 669 eligible rows contain 609 OpenJLPT rows with blank POS and 60
seed_basic rows (18 marked verb). The export also confirms 160 missing
OpenJLPT readings and 11 slash-separated surface entries. Those 11 entries
are not dictionary-form verbs and were not split into invented verb supply.

## Classification Repair: CONFIRMED

The Phase 3B-R lattice classifier is retained. A pinned, reproducible
source-sense evidence layer supplements it; unknowns are not blanket-accepted.

- Exact source surface and N5 source assignment are required.
- Dictionary spelling/reading/sense restrictions and source-gloss alignment
  are checked. Evidence retains entry ID, sense number, POS, reading,
  aliases, source line and source/dictionary/review digests.
- Three reviewed paraphrases cover shower/bathe, calling by phone, and taking
  time/resources. They are source-to-dictionary sense alignments, not a list
  that accepts arbitrary strings as verbs.
- Evidence is versioned, separate from material usage and invalidated when
  source or review digests mismatch at load. The manifest cache is process-local;
  source updates require a restart/new release and a new selection job rather
  than reusing an old exhausted selector cursor. No formal schema or row was changed.
  Public text fingerprints normalize BOM/newlines so Windows CRLF and Render
  Linux LF checkouts agree; formal CSV and dictionary ZIP hashes remain exact
  byte hashes. A cross-platform regression covers this distinction.
- Source evidence survives candidate/job caching and enrich validation.
- Fixed-candidate enrichment receives the verified reading/source sense as
  additional context. The compact JSON helper and all Gemini config remain
  unchanged. A returned contradictory reading is rejected before cache reuse
  or insertion. Existing cache tests now use attested readings rather than
  the fake literal reading previously used by their fixtures.
- Word validation uses the same source-aware classification contract as
  selection; a classifier unknown is not silently converted into a Word.

On the same 669 formal eligible rows (row counts, not distinct sense counts):

| Classification | Phase 3B-R lattice only | With source-sense evidence |
|---|---:|---:|
| verified verb | 108 | 111 |
| verified word | 456 | 521 |
| unknown | 104 | 37 |
| rejected invalid | 1 | 0 |

These formal-row counts must not be confused with the earlier 105-verb
public-CSV/empty-history result, which used a different population.

The actual repair parent `3c6b2a6` was replayed separately on this same export
and history: it selects two surface-fresh candidates (`あびる`, `かける`).
That is not two certified new lexical senses: bathing overlaps existing
history, while the phone-call sense needs explicit sense-aware history review.
The deployed baseline selected zero; this repair confirms one safely fresh
candidate (`はく`) without turning the parent's optimistic surface count into
a claim of increased sustainable supply. Full parent exclusions are recorded
in `replay.json`.

The old **244** not_verified rows now divide into **3 verified verbs,
234 verified words, 7 still unknown under the runtime contract**. A private
per-row audit records their evidence, first exclusion and history relation.
Neither 244 nor 1,950 is a count of available new verbs.

## Lexical Identity and Real Remaining Supply

**CONFIRMED:** only **one** current formal-pool verb is presently both verified
and safely history-fresh: `はく`, the clothing/trousers sense. Its missing
reading is resolved by dictionary evidence. The relevant historical `着く`
was read back with reading `つく` and arrival meaning; it is not the same
reading/sense as clothing `はく`. Reading alone is never used to merge words.

**CONFIRMED:** `あびる` is a legal verb, but its bathing sense overlaps saved
`浴びる`; the actual saved body confirms the bath/shower meaning. It cannot
be counted as fresh merely because the pool uses kana.

**PENDING SENSE REVIEW/INTEGRATION:** source `かける` means making a phone call
(JMdict entry 1207610, sense 4); saved `掛ける` samples mean hanging (sense 1).
They must not be declared the same sense just because their reading/spelling
overlaps. The identity helper supports explicit reviewed sense identities,
and a regression verifies the distinction, but the production history reader
does not yet supply such reviewed identities. It conservatively holds this
candidate and does **not** count it as usable supply. This limitation is
explicit rather than hidden inside a successful capacity number.

`ある` and `かかる` gain source-sense verification but already hit saved
history, so they do not increase fresh capacity. Nine alias/identity holds
(two verbs and seven words) remain separate from seven unresolved POS rows.
An unresolved historical identity is reported as uncertainty, not exhaustion.

The seven post-history POS-unknown surfaces were also compared with a pinned
newer OpenJLPT source. Its evidence identifies expressions/adverbs/nouns or
reading variants for six of them; the comparison construction remains outside
the supported single-lexeme contract. This supplemental review is not silently
substituted into the runtime classifier and does not establish another verb.

## Executed Tests

| Suite | PASS | SKIP | FAIL |
|---|---:|---:|---:|
| Existing generation | 18 | 0 | 0 |
| Existing SQLite/PostgreSQL selector | 20 | 3 | 0 |
| Phase 3B-R lexical contracts | 24 | 0 | 0 |
| New Phase 3C evidence/formal snapshot | 15 | 0 | 0 |
| Total | 77 | 3 | 0 |

PostgreSQL was an actual local PostgreSQL 17 server on loopback with
randomized isolated schemas, not SQL mocked as PostgreSQL. The three inherited
skips concern SQLite-only invalid-byte insertion, driver fault injection and
CSV history setup; PostgreSQL keysets, actual statement cancellation, selector
queries and save/reload were exercised separately. Gemini responses were
mocked; tests do not prove real translation/conjugation quality.

The original empty-history controls still show all-level/N1 30-day success;
N5-only succeeds **21/30**, then stops before API calls. The separate
all-level source-contract tests with empty initial history pass 7/7 and 30/30
on SQLite and PostgreSQL. **None is a formal capacity claim.**

### Required History-Aware Capacity Gate: FAIL

| Data/Backend | Seven days | Thirty days |
|---|---:|---:|
| Formal N5 + formal history, SQLite | 1/7 | 1/30 |
| Formal N5 + formal history, isolated PostgreSQL 17 | 1/7 | 1/30 |

Other levels use the public source fixtures, explicitly not formal N1-N4
inventories. This is enough to expose the formal N5 bottleneck, but not to
certify the full formal system's capacity.

Day one executes real prepare -> mocked enrich -> finalize -> actual isolated
save -> reload, achieving 8/5/5, five verb levels, five grammar levels and
fallback=0. History grows from 1,169 to 1,182 keys, never resets. N5 fresh verb
supply falls 1 -> 0. Subsequent days return `classifier_uncertainty` in Phase A,
with **zero additional Gemini calls and no saved material/history mutation**.
They do not falsely claim exhaustion while unresolved candidates remain.
Daily counts are in `daily_inventory.csv`; full scenario results in
`supply_tests.json`. Safe-stop tests PASS; sustained-capacity acceptance FAILS.

## Source Augmentation Plan (No Import Performed)

All 662 surfaces in the checked-in OpenJLPT N5 CSV already occur in the formal
pool. Reimporting that file cannot add new lexical capacity. A newer pinned
[OpenJLPT revision](https://github.com/evanclan/OpenJLPT/tree/bf4e1eecd5d11406aaecdee910464507aef7fcdc)
has 674 N5 entries and POS/dictionary IDs. Its additional verb spellings
`いる` and `見る` are held by existing-history identities; no additional
confirmed fresh verb is established by that update. See `source_proposal.json`.

At least **six further verified, genuinely unused N5 verb senses** are needed
for a seven-day no-failure run, or 29 for thirty days, beyond the one verified
current candidate. Replacement failures require additional same-level reserve.
If the phone-call sense is explicitly reviewed and integrated, these minimum
deficits decrease by one; it is not yet counted.

Candidate acquisition must use a licensed N5 learning source for grading and
JMdict for POS/reading/sense, then compare actual saved lexical senses. JMdict
alone does not establish JLPT level. The [JLPT FAQ](https://www.jlpt.jp/e/faq/index.html)
explains that current official vocabulary specifications are not published.
Do not manufacture an authoritative N5 grade from a dictionary POS tag.

A possible additional curriculum source is MLC's N5 list, but its original
PDF/page could not be fetched reliably in this run; contents, redistributable
license and net new eligible verbs are **NOT VERIFIED**. It is not an approved
import list. No six-candidate augmentation list with all required provenance
has been established, and no such list is claimed here.

Before any import: provide one reviewed row per proposed surface/reading/sense,
exact curriculum reference/page/grade, JMdict ID/sense/POS, license, normalized
identity and history comparison. Dry-run in isolated PostgreSQL with exact
insert counts; preserve a manifest of newly inserted IDs/fingerprints for an
additive-only rollback plan. Obtain explicit approval for the final list and
formal import separately. Never relabel N4/N3, unblock synthetic rows, reset
history or delete materials to pass capacity.

## Scope, Risks and Release Gates

Changed application functions: `audit_verb_candidate`,
`classify_daily_fresh_pool_row`, `build_daily_fresh_candidate_payload`,
`daily_fresh_candidate_rejection_reason`, new `daily_fresh_history_readings`,
the context adapter in `fetch_daily_fresh_candidate_rows`,
`filter_enriched_items_to_candidates`, and the enrich call site's source hints.
New helpers/data live in `services/lexical_evidence.py` and the three
`data/lexical_*`/notice files. Classifier version is bumped; its existing
lattice algorithm is not replaced.

`services/candidate_selector.py` is unchanged: SQL eligibility, ID-only query,
byte-safe hydration, keyset cursor and resumability are preserved. Micro-step
state transitions, primary/reserve/refill allocation, grammar rotation,
Gemini model/config/compact helper, bank fallback, settings/UI, Telegram,
quiz/mistakes/verb practice/grammar analysis were not modified. Protected
function AST hashes, including `save_material_for_date()` and
`/api/materials`, match the prior baseline. No migration was added.

Remaining risks: unresolved historical senses can conservatively withhold
legitimate capacity; source fingerprint changes require rebuilding evidence;
cache meaning/sense quality and real Gemini output are not proven by mocked
translations/forms. Full formal N1-N4 pools and bank were not exported. The
7-day gate is failed, current Render metadata/backup/rollback point unverified,
and live Gemini smoke testing is not authorized or performed.

No deployment is safe to advertise as daily-generation recovery yet. Required
next approvals are limited to a reviewed data augmentation/sense-evidence
plan, any formal import, later explicit Render deployment and a single real
generation smoke test after all gates pass.

## Missing Dates and Safe Backfill

Read-only archive confirms no saved version for 2026-10-29, 10-30, 10-31 and
11-01 (today pending). No existing material was overwritten. Backfill is not
authorized and current supply cannot safely fill these dates.

After capacity/import/deployment approval, separately authorize backfill dates.
Recheck each exact date before writing; process oldest first with one target
date/job at a time, carry forward newly saved history, stop on any shortage,
and save/reload/verify each date independently. Never silently store several
dates as one day's material or overwrite an existing version.

## Publication and Rollback

Publish only code, public derived evidence with CC BY-SA attribution, tests and
aggregate reports. Keep the formal export, retained history, downloaded full
dictionary, private per-row audit and fetched material bodies out of Git.
Credential/secret and raw-snapshot checks must run before commit.

This branch is not deployed. Reviewing or reverting its commit does not alter
formal data. If later deployed, first verify the actual prior successful Render
SHA/deploy ID (last known 69207c2), backup/recovery point and unchanged DB config.
Rollback means redeploying that known good code; it does not undo database
writes. There are no Phase 3C formal writes or migrations to undo now.
