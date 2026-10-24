"""Bounded ID scans with resumable keysets and byte-safe small hydration."""

import time
from collections import Counter


TEXT_COLUMNS = (
    "surface", "base_form", "reading_hiragana", "part_of_speech", "jlpt_level",
    "quality", "normalized_key", "category", "source", "status", "last_used_at", "domain_tags",
)
NUMERIC_COLUMNS = ("id", "verb_group", "commonness_score", "priority", "is_active", "used_in_material_count")


def id_query(postgres, blocked_categories, blocked_sources, tier):
    p = "%s" if postgres else "?"
    clauses = [
        "COALESCE(vp.is_active, TRUE) = TRUE",
        f"vp.jlpt_level = {p}",
        "LOWER(COALESCE(NULLIF(vp.status, ''), 'active')) IN ('active','approved','manual_core','enabled','unused')",
        "LOWER(COALESCE(vp.quality, '')) NOT IN ('rejected','experimental','low_quality')",
        f"(vp.commonness_score IS NULL OR vp.commonness_score >= {p})",
    ]
    for column, values in (("category", blocked_categories), ("source", blocked_sources)):
        clauses.append(f"LOWER(TRIM(COALESCE(vp.{column}, ''))) NOT IN ({','.join([p] * len(values))})")
    preferred = "LOWER(TRIM(COALESCE(vp.source, ''))) = 'openjlpt'"
    clauses.extend([preferred if tier == 0 else f"NOT ({preferred})", f"vp.id > {p}"])
    return f"SELECT vp.id FROM vocabulary_pool vp WHERE {' AND '.join(clauses)} ORDER BY vp.id ASC LIMIT {p}"


def select_slice(conn, *, postgres, item_type, level, limit, state, excluded,
                 blocked_categories, blocked_sources, min_commonness, sanitize, reject,
                 candidate_key, deadline=None, clock=time.perf_counter):
    slice_started = clock()
    deadline = min(deadline or clock() + 4.0, clock() + 4.0)
    categories, sources = sorted(blocked_categories), sorted(blocked_sources)
    p = "%s" if postgres else "?"
    rows, bad = [], set(state.get("bad_rows") or [])
    rejected = Counter(state.get("rejected_by_filter") or {})
    scanned = 0
    id_ms = hydration_ms = 0.0
    timed_out = False
    timeout_kind = ""
    cur = conn.cursor()
    print("[vocabulary-pool] selector_sql_mode=id_batch_safe_hydration")
    print("[vocabulary-pool] candidate_id_fetch_sql_mode=id_only")
    print(f"[vocabulary-pool] selector_sql_filters blocked_categories={categories} blocked_sources={sources} preferred_source=openjlpt")

    def query(sql, params):
        if clock() >= deadline:
            raise TimeoutError("selector slice deadline")
        if postgres:
            remaining_ms = max(1, int((deadline - clock()) * 1000))
            cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(remaining_ms),))
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def hydrate(ids):
        nonlocal hydration_ms
        if not ids:
            return {}
        columns = [f"vp.{c}" for c in NUMERIC_COLUMNS]
        columns += [
            f"convert_to(COALESCE(vp.{c}::text, ''), 'UTF8') AS {c}" if postgres else f"vp.{c}"
            for c in TEXT_COLUMNS
        ]
        started = clock()
        try:
            values = query(
                f"SELECT {','.join(columns)} FROM vocabulary_pool vp WHERE vp.id IN ({','.join([p] * len(ids))}) ORDER BY vp.id",
                tuple(ids),
            )
            return {int(row["id"]): row for row in values}
        except UnicodeDecodeError:
            print("[vocabulary-pool] candidate_decode_error_recoverable=true")
            if len(ids) == 1:
                bad.add(int(ids[0]))
                rejected["bad_encoding"] += 1
                print(f"[vocabulary-pool] bad_candidate_row id={ids[0]} reason=utf8_decode_error")
                print("[vocabulary-pool] continue_scan_after_decode_error=true")
                return {}
            midpoint = len(ids) // 2
            return {**hydrate(ids[:midpoint]), **hydrate(ids[midpoint:])}
        finally:
            hydration_ms += (clock() - started) * 1000

    try:
        # A slice is bounded; the persisted (source tier, id) keyset has no scan ceiling.
        for _ in range(4):
            tier = int(state.get("source_tier") or 0)
            if tier >= 2 or len(rows) >= limit:
                break
            started = clock()
            ids = [r["id"] for r in query(
                id_query(postgres, categories, sources, tier),
                (level, min_commonness, *categories, *sources, int(state.get("last_id") or 0), 800),
            )]
            id_ms += (clock() - started) * 1000
            if not ids:
                state.update(source_tier=tier + 1, last_id=0)
                continue
            for offset in range(0, len(ids), 50):
                chunk = ids[offset:offset + 50]
                hydrated = hydrate([i for i in chunk if i not in bad])
                for row_id in chunk:
                    if clock() >= deadline:
                        raise TimeoutError("selector slice deadline")
                    row = hydrated.get(row_id)
                    if row is not None:
                        row = sanitize(row)
                        if any("\ufffd" in str(row.get(c) or "") for c in TEXT_COLUMNS):
                            bad.add(row_id)
                            rejected["bad_encoding"] += 1
                            print(f"[vocabulary-pool] bad_candidate_row id={row_id} reason=utf8_decode_error")
                        else:
                            reason = reject(row, item_type, level, excluded)
                            if reason:
                                rejected[reason] += 1
                            else:
                                rows.append(row)
                                excluded.add(candidate_key(row))
                    state["last_id"] = row_id
                    scanned += 1
                    if len(rows) >= limit:
                        break
                if len(rows) >= limit:
                    break
            if len(rows) < limit and len(ids) < 800:
                state.update(source_tier=tier + 1, last_id=0)
        if int(state.get("source_tier") or 0) >= 2 and len(rows) < limit and not state.get("sql_diagnostics_done"):
            # Only an exhausted scan pays for exact SQL exclusion counts.
            conditions = [
                ("COALESCE(vp.is_active, TRUE) = FALSE", "sql_inactive"),
                ("LOWER(COALESCE(NULLIF(vp.status, ''), 'active')) NOT IN ('active','approved','manual_core','enabled','unused')", "sql_status"),
                ("LOWER(COALESCE(vp.quality, '')) IN ('rejected','experimental','low_quality')", "sql_quality"),
                (f"vp.commonness_score < {p}", "sql_commonness"),
                (f"LOWER(TRIM(COALESCE(vp.category, ''))) IN ({','.join([p] * len(categories))})", "sql_category"),
                (f"LOWER(TRIM(COALESCE(vp.source, ''))) IN ({','.join([p] * len(sources))})", "sql_source"),
            ]
            case = "CASE " + " ".join(f"WHEN {condition} THEN '{reason}'" for condition, reason in conditions) + " ELSE 'eligible_sql' END"
            diagnostics = query(
                f"SELECT {case} AS reason, COUNT(*) AS count FROM vocabulary_pool vp WHERE vp.jlpt_level = {p} GROUP BY 1",
                (min_commonness, *categories, *sources, level),
            )
            counts = {row["reason"]: int(row["count"]) for row in diagnostics}
            state["sql_exclusion_counts"] = counts
            state["sql_diagnostics_done"] = True
            rejected.update({reason: count for reason, count in counts.items() if reason != "eligible_sql"})
    except Exception as exc:
        if isinstance(exc, TimeoutError) or "statement timeout" in str(exc).lower() or "interrupted" in str(exc).lower():
            timed_out = True
            timeout_kind = "database_statement_timeout" if "statement timeout" in str(exc).lower() else "slice_deadline"
            print("[vocabulary-pool] selector_timeout_controlled=true")
            print("[vocabulary-pool] selector_aborted_before_worker_timeout=true")
        else:
            raise
    finally:
        cur.close()
        if not postgres:
            conn.set_progress_handler(None, 0)
    state["bad_rows"] = sorted(bad)
    state["rejected_by_filter"] = dict(rejected)
    state["scanned_ids"] = int(state.get("scanned_ids") or 0) + scanned
    state["source_exhausted"] = int(state.get("source_tier") or 0) >= 2 and bool(state.get("sql_diagnostics_done"))
    state["stalled_slices"] = (int(state.get("stalled_slices") or 0) + 1) if not scanned and timed_out else 0
    incomplete = len(rows) < limit and not state["source_exhausted"]
    error = "selector_timeout" if state["stalled_slices"] >= 3 else ""
    for name, value in (
        ("selector_id_query_ms", round(id_ms, 2)), ("selector_hydration_ms", round(hydration_ms, 2)),
        ("selector_selected", len(rows)), ("selector_scanned_ids", state["scanned_ids"]),
        ("selector_bad_rows", len(bad)), ("selector_rejected_by_filter", dict(rejected)),
        ("selector_elapsed_ms", round((clock() - slice_started) * 1000, 2)),
    ):
        print(f"[vocabulary-pool] {name}={value} item_type={item_type} level={level}")
    print(f"[vocabulary-pool] selector_cursor source_tier={state.get('source_tier', 0)} last_id={state.get('last_id', 0)}")
    print(f"[vocabulary-pool] selector_scan_incomplete={str(incomplete).lower()} progress_saved=true")
    print(f"[vocabulary-pool] candidate_fetch_success item_type={item_type} level={level} selected={len(rows)}")
    if state["source_exhausted"] and len(rows) < limit:
        print(f"[vocabulary-pool] candidate_exhausted=true item_type={item_type} level={level}")
    rows.append({
        "__selector_meta": True, "candidate_exhausted": state["source_exhausted"],
        "selector_scan_incomplete": incomplete, "selector_timeout_warning": timed_out,
        "selector_error": error, "candidate_validation_failed": bool(bad) and state["source_exhausted"] and len(rows) < limit,
        "timeout_kind": timeout_kind,
        "source_state": dict(state), "bad_rows": sorted(bad), "rejected_by_filter": dict(rejected),
    })
    return rows
