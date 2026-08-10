# Review of `findings.md` (grouped FROM-subquery re-executed per outer row)

Reviewed against `OrthelT/turso-dev` @ `d14a446`. All code citations in the
findings were re-verified in this session: the affinity gate
(`core/vdbe/affinity.rs:300`, `core/translate/optimizer/constraints.rs:198`),
the fallback returns in `find_best_access_method_for_subquery`
(`core/translate/optimizer/access_method.rs`), the `Operation::Scan → Coroutine`
mapping (`core/translate/subquery.rs:1293-1327`), and the
`InitCoroutine`-inside-the-outer-loop emission
(`core/translate/main_loop/open.rs:196`). The cited commits (`d33016ff8`,
`4f9f028d9`) could not be re-resolved here (shallow clone) — confirm against
upstream before quoting publicly, per the findings' own caveat.

## 1. Framing

The document is rigorous but buries its thesis. Recommended lead:

> SQLite makes two independent decisions: (A) materialize the uncorrelated
> subquery's result once; (B) optionally build an automatic index on that
> cache so probes become seeks. Turso fused them: materialization exists only
> *as* the probe index. When the index is disqualified (affinity mismatch is
> merely the most reproducible trigger), materialization vanishes with it and
> the plan collapses to re-running the entire aggregation per outer row.
> SQLite degrades seek → re-scan of ~2.5k cached rows; Turso degrades
> seek → re-execute a 200k-row GROUP BY.

"Affinity mismatch is only the easiest trigger" is the most important sentence
in the findings and should be promoted — it tells maintainers the fix belongs
at the fallback level, not in the affinity check. Move the debug-build caveat
next to the first timing table.

## 2. Adequacy of the proposed fix

The primary fix (decouple materialization from seekability; reuse the CTE
table-then-index machinery) is correct and genuinely SQLite-aligned. Four
caveats:

1. **Missing fallback path.** The fix list names `access_method.rs` returns at
   1538, 1552, 1596, 1658 — but the intrinsic-order return at ~1528 is guarded
   by `extremum_constraints_compatible || usable.is_empty()`, and the affinity
   mismatch is precisely what empties `usable`. With an outer order target it
   produces the identical collapse. It must be covered too.
2. **`input_cardinality > 1` gating is fragile.** It's an estimate; if the
   planner underestimates the outer side the coroutine survives and the bug
   returns. SQLite's rule is structural (coroutine only in leftmost /
   single-scan position). Prefer the structural condition; let cost decide
   only table-vs-index, not materialize-vs-coroutine.
3. **The secondary fix (coerce the ephemeral index key to comparison
   affinity) is riskier than presented.** The IN-subquery precedent is not
   equivalent: IN probes only test membership. The FROM-subquery ephemeral
   index is *covering* — the key column is also a result column, so coercion
   (`'007'` → `7`) would leak into output. Split into a follow-up issue.
4. **Test should assert both directions.** The `.sqltest` plan-shape test for
   the INTEGER/TEXT case should also assert the matched-affinity variant still
   gets its seek index.

Do not touch `index_affinity_ok` — verified faithful to
`sqlite3IndexAffinityOk`.

## 3. Why post-fix numbers still lag SQLite

1. **Debug vs optimized C is most of the gap.** 33.6 s debug ÷ (30–60×
   overhead) ≈ 0.6–1.1 s release vs SQLite 0.273 s. Re-measure on a release
   build before concluding anything.
2. **Plan-shape parity, not deficit.** The fixed plan re-scans ~2.5k cached
   rows per outer row — exactly SQLite's plan on the mismatched-affinity
   query. SQLite's *matched* case additionally gets a bloom filter + automatic
   covering index; only the secondary fix (or bloom filters) closes that.
3. **Constant-factor engine maturity.** Remaining ~2–4× release-adjusted:
   VDBE dispatch, per-row record deserialization, result-column copies,
   ephemeral B-tree/sorter tuning.

## PostgreSQL differential (this session)

Same schema/data (2,000 × 200,000) on PostgreSQL 16.13,
`EXPLAIN (ANALYZE, BUFFERS)` in `postgres-explain-{int,txt}.txt`,
visualized via pgexplain.dev (screenshots `pgexplain-{int,txt}.png`):

- INTEGER key: https://www.pgexplain.dev/plan/804d122b-c457-4d2f-b2cd-190685c871c4 — 43.4 ms
- VARCHAR key: https://www.pgexplain.dev/plan/4482dd7f-a925-469c-822c-34d5267361ac — 49.2 ms

Identical plan shape in both: Seq Scan → HashAggregate (GROUP BY runs once) →
Hash Right Join. The type mismatch costs ~13%, not 80×. Postgres refuses
`INTEGER = VARCHAR` at compile time — the join needs an explicit
`h.type_id::integer` cast, after which it simply hashes the casted value:
the moral equivalent of the findings' secondary fix, for free. Third
independent confirmation that a mature planner never demotes this query to
per-outer-row re-execution.

## Suggested next steps

1. ~~Confirm `d33016ff8` / `4f9f028d9` hashes against `tursodatabase/turso`.~~
   **Done (2026-08-10)** — both resolve upstream; full hashes, subjects, and
   dates recorded in the findings' caveats section. The commit-body quote is
   verbatim.
2. File the issue with the two-decision framing above. Embed the plan-graph
   captures in [`planviz/`](planviz/README.md) (made with PR #8316's
   provisional `--planviz` visualizer): coroutine-bug vs
   matched-affinity-indexed is the bug in one side-by-side;
   materialized-workaround is the target plan shape. The structured JSON
   (`subquery.execution: "coroutine"`) also re-confirms the repro on a branch
   newer than `d14a446` (`dc3ada223`).
3. Implement the primary fix (structural gating), covering all five fallback
   paths including the intrinsic-order return.
4. `.sqltest` plan-shape coverage, both directions.
5. Separate follow-up issue for probe-index key coercion.
6. Release-build benchmark before any further "slower than SQLite" claims.
