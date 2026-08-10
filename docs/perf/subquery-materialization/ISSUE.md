# DRAFT — upstream issue for tursodatabase/turso

**Title:** Affinity mismatch on a join key drops FROM-subquery materialization entirely, re-running the aggregation per outer row (follow-up to #2974)

**Suggested labels:** `performance`, `query planner`, `optimizer`

Everything below the rule is the proposed issue body, ready to paste.

---
Turso's query planner drops materialized aggregation entirely when it finds a type-affinity mismatch between the columns of a LEFT JOIN (e.g. INTEGER outer and TEXT subquery), and falls back to running a coroutine that rebuilds the aggregation for each outer row. This results in significantly degraded performance relative to SQLite.

Why the fallback lands on a coroutine: SQLite makes two independent decisions when planning an uncorrelated FROM-clause subquery on the inner side of a join — **(A)** materialize the subquery's result once, and **(B)** optionally build an automatic index on that cached result so probes become seeks. Turso implements them as a single decision: the only way it materializes such a subquery is by building the ephemeral probe index on it. So when the planner rejects the index — the affinity mismatch is the most reproducible reason, but any rejection behaves the same — there is no "materialized table without an index" plan to fall back to, as there is in SQLite. The plan falls all the way back to a coroutine, which re-runs the entire subquery (including its GROUP BY over the full inner table) once per outer row.

- SQLite's degradation ladder: index seek → **re-scan of the materialized result** (~2.5k cached rows per outer row)
- Turso's ladder: index seek → **re-execute the aggregation** (200k raw rows per outer row)

Results are correct in every variant; this is purely a performance defect. In the production workload that surfaced it (2,354 × 797,817 rows), the query takes **117 s in Turso vs 0.27 s in SQLite**.

This is the unhandled case of #2974's fix, not a new class of defect — details in "Prior art" below.

## Reproduction

```sql
CREATE TABLE watchlist   (type_id INTEGER PRIMARY KEY, name TEXT);   -- 2,000 rows
CREATE TABLE history_txt (type_id VARCHAR, price REAL, ts INTEGER);  -- 200,000 rows
CREATE TABLE history_int (type_id INTEGER, price REAL, ts INTEGER);  -- 200,000 rows, same data
```

This script generates the data used for the timings below. The specific values are not important — the bug reproduces with any data of this shape:

```python
import random
random.seed(42)
print("BEGIN;")
for i in range(1, 2001):
    print(f"INSERT INTO watchlist VALUES ({i}, 'item_{i}');")
for i in range(200000):
    tid = random.randint(1, 2000)
    price = round(random.uniform(1, 1000), 2)
    print(f"INSERT INTO history_txt VALUES ('{tid}', {price}, {1700000000+i});")
    print(f"INSERT INTO history_int VALUES ({tid}, {price}, {1700000000+i});")
print("COMMIT;")
```

Run the following query twice: once as written, and once with `history_int` substituted for `history_txt`. The two tables hold the same data — the only difference is the declared type of `type_id` (VARCHAR vs INTEGER), which determines how it compares against `watchlist.type_id` (an INTEGER) in the join condition:

```sql
SELECT count(*), sum(h.avgp)
FROM watchlist w
LEFT JOIN (SELECT type_id, avg(price) AS avgp FROM history_txt GROUP BY type_id) h
  ON w.type_id = h.type_id;
-- run again with history_int in place of history_txt
```

> **Reproducing this requires `LEFT JOIN`, not plain `JOIN`.** The LEFT JOIN forces `watchlist` to stay on the outer side of the join. With an inner `JOIN`, the optimizer moves the subquery to the outer position and the slowdown does not occur.

Two notes on the timings below. First, they were measured on a debug build, which is roughly 30–60× slower than a release build across the board — so compare the two rows against each other rather than reading the absolute times. Second, for these runs `watchlist` was loaded with 100 rows instead of 2,000: the slow variant re-runs the 200,000-row aggregation once per `watchlist` row, so measuring it at the full 2,000 rows on a debug build would take about an hour.

| subquery source | join-key comparison | plan | time (100 `watchlist` rows) |
|---|---|---|---|
| `history_int` | INTEGER = INTEGER | `SEARCH h USING INDEX ephemeral_subquery_t3 (type_id=?)` | **2.1 s** |
| `history_txt` | INTEGER = VARCHAR | `SCAN h` (coroutine, re-executed per row) | **167.5 s (~80×)** |

The gap grows linearly with the `watchlist` row count, which matches the production report (2,354 rows: 117 s vs SQLite's 0.27 s).

## The two plans, visualized

Rendered with the plan visualizer from draft PR #8316 (`tursodb --planviz`), which conveniently ships both a dataflow view and a machine-readable plan export.

**Mismatched affinity (`history_txt`, the bug).** The entire subquery body — the 200,000-row scan plus the GROUP BY — sits inside a coroutine container on the inner side of the join, re-armed **per outer row**:

![coroutine collapse: subquery body re-executed per outer row](https://raw.githubusercontent.com/OrthelT/turso-dev/df331ba86e34de5fa8c3b8603193ac76174cf35f/docs/perf/subquery-materialization/planviz/coroutine-bug.png)

**Matched affinity (`history_int`).** The same query reading `history_int` instead: the subquery body now runs **once**, materialized into an ephemeral index, and the join probes it with seeks:

![matched affinity: materialized once into an ephemeral index, probed by seeks](https://raw.githubusercontent.com/OrthelT/turso-dev/df331ba86e34de5fa8c3b8603193ac76174cf35f/docs/perf/subquery-materialization/planviz/matched-affinity-indexed.png)

PR #8316's structured plan JSON states the difference directly — the subquery reader node `h` carries `"subquery": {"execution": "coroutine"}` in the VARCHAR case vs `"execution": "indexed_materialized"` (with `"index": {"name": "ephemeral_subquery_t3", "ephemeral": true}`) in the INTEGER case.

Bytecode (`EXPLAIN`) confirms the mechanism. Mismatched case — the coroutine is re-initialized *inside* the outer loop:

```
47  Rewind        3 -> 63    Rewind table watchlist   <-- outer loop starts
49    InitCoroutine 1  0  2                           <-- subquery re-armed EVERY outer row
50    Yield         1 59
...
62  Next          3 -> 48
```

Matched case: the subquery runs once, before the outer `Rewind`, writing into `ephemeral_subquery_t3`; the loop body only does `SeekGE`/`IdxGT`.

## Root cause chain

Verified at [`d14a446`](https://github.com/tursodatabase/turso/commit/d14a446dae10c90740afb1f8d9c9c4fe025f2c74) (line numbers refer to that commit):

1. [`access_method.rs:1496`](https://github.com/tursodatabase/turso/blob/d14a446dae10c90740afb1f8d9c9c4fe025f2c74/core/translate/optimizer/access_method.rs#L1496) — in `find_best_access_method_for_subquery`, candidate seek constraints are filtered by `c.can_drive_index_seek(&subquery.columns, false)`.
2. [`constraints.rs:198-209`](https://github.com/tursodatabase/turso/blob/d14a446dae10c90740afb1f8d9c9c4fe025f2c74/core/translate/optimizer/constraints.rs#L198-L209) — `can_drive_index_seek` → `satisfies_index_affinity`.
3. [`affinity.rs:300`](https://github.com/tursodatabase/turso/blob/d14a446dae10c90740afb1f8d9c9c4fe025f2c74/core/vdbe/affinity.rs#L300) — `Affinity::index_affinity_ok`, a faithful port of `sqlite3IndexAffinityOk`. INTEGER vs TEXT ⇒ comparison affinity NUMERIC; the ephemeral index column inherits the subquery column's TEXT affinity; TEXT is not numeric ⇒ **rejected**. *This check is correct and matches SQLite — it is not the bug.*
4. [`access_method.rs:1538-1548`](https://github.com/tursodatabase/turso/blob/d14a446dae10c90740afb1f8d9c9c4fe025f2c74/core/translate/optimizer/access_method.rs#L1538-L1548) — with no usable constraints left, the function early-returns `AccessMethodParams::Subquery` (a plain scan), never reaching the `MaterializedSubquery` construction at line 1678.
5. [`subquery.rs:1293-1327`](https://github.com/tursodatabase/turso/blob/d14a446dae10c90740afb1f8d9c9c4fe025f2c74/core/translate/subquery.rs#L1293-L1327) — `choose_from_clause_subquery_execution_mode` materializes only for an ephemeral-index `Seek`, or when `requires_table_materialization()` (explicit `AS MATERIALIZED`, or a shared CTE with >1 reference). A plain `Operation::Scan` on an uncorrelated subquery falls to `FromClauseSubqueryExecutionMode::Coroutine`.
6. [`open.rs:196-211`](https://github.com/tursodatabase/turso/blob/d14a446dae10c90740afb1f8d9c9c4fe025f2c74/core/translate/main_loop/open.rs#L196-L211) — the coroutine path emits `InitCoroutine` inside the outer loop body ("In case the subquery is an inner loop, it needs to be reinitialized on each iteration of the outer loop") ⇒ O(N × M).

**The affinity mismatch is only the easiest trigger.** Any reason the seek index is rejected — including having no usable join constraint at all (`access_method.rs:1596-1611`) — produces the identical collapse. That is why the fix belongs at the fallback level, not in the affinity check.

**The cost model already knows.** `access_method.rs:1445-1447` charges `coroutine_reexecution_overhead = (input_cardinality - 1) * base_row_count * cpu_cost_per_seek` — but with no materialize-without-index candidate in the choice set, that penalty can only influence join order, and a LEFT JOIN pins the order.

Only the **subquery-side** column affinity is gated (verified via `EXPLAIN QUERY PLAN`):

| outer col | subquery col | comparison affinity | plan |
|---|---|---|---|
| TEXT | TEXT | BLOB | index |
| INTEGER | INTEGER | NUMERIC | index |
| TEXT | INTEGER | NUMERIC | index |
| **INTEGER** | **TEXT** | **NUMERIC** | **coroutine ← bug** |
| INTEGER | `CAST(x AS INTEGER)` | NUMERIC | index |

## Differential evidence

**SQLite 3.45.1**, same data, applies the *same* affinity rule and also declines the index — but materializes regardless:

```
-- mismatched affinity (INTEGER vs VARCHAR)
MATERIALIZE h                      <-- GROUP BY runs ONCE
  SCAN history_txt
  USE TEMP B-TREE FOR GROUP BY
SCAN w
SCAN h LEFT-JOIN                   <-- re-scans ~2,500 cached rows, not 200k raw rows
```

0.273 s for the full 2,000 × 200,000 case. In the matched-affinity case SQLite additionally gets `BLOOM FILTER` + `AUTOMATIC COVERING INDEX`; on mismatch it loses only the index, never the materialization.

**PostgreSQL 16.13**, same schema/data: identical plan shape for both key types — Seq Scan → HashAggregate (GROUP BY once) → Hash Right Join. The type mismatch costs ~13% (43.4 ms vs 49.2 ms), not 80×. (Postgres refuses `INTEGER = VARCHAR` at compile time; with the required explicit cast it simply hashes the casted value.) Plans: [INTEGER](https://www.pgexplain.dev/plan/804d122b-c457-4d2f-b2cd-190685c871c4), [VARCHAR](https://www.pgexplain.dev/plan/4482dd7f-a925-469c-822c-34d5267361ac). A mature planner never demotes this query to per-outer-row re-execution.

## Prior art

#2974 ("Materialize FROM-clause subqueries using some heuristic", closed completed) is the direct ancestor. Its fix, [`d33016ff8`](https://github.com/tursodatabase/turso/commit/d33016ff8a43ee265ab2455d7a355d354b8ed471) (merged as [`ee3c2bfc5`](https://github.com/tursodatabase/turso/commit/ee3c2bfc55c788ace0dd09e3cee596cb928d09df)), states the coupling as design — commit message, verbatim:

> When a FROM clause subquery (derived table) is not in the leftmost join position and is not correlated, materialize it into an ephemeral index keyed on columns used in join conditions. This enables index seeks instead of re-executing the subquery for each outer row.

"Materialize" was *defined as* "build an ephemeral index keyed on the join columns"; there was never a branch for the case where such an index cannot be keyed.

The needed two-stage machinery already exists, scoped to CTEs: [`4f9f028d9`](https://github.com/tursodatabase/turso/commit/4f9f028d9194841a959fcf3f39c52490f5fcfc83) "translate: align CTE materialization logic with sqlite" —

> always materialize CTEs into tables to preserve row order of the original SQL statement, and then build autoindexes using our existing ephemeral index ad-hoc mechanism […] this is also what sqlite does

— applied to CTEs, never extended to plain derived tables.

Related but distinct open issues: #7393 (collation on the same access-method path), #8260 (per-outer-row rescan, different mechanism: `IN (SELECT …)` NULL-detection loop), #8274 (redundant re-sort family). Checked and **not** duplicates: #2926 (closed by an unrelated constraint-usability fix) and #8276 (outer-WHERE pushdown; here the constraint arrives fine, and pushdown would make the subquery correlated — re-executing per row by definition).

## Workarounds (both verified)

| approach | effect |
|---|---|
| `CAST(type_id AS INTEGER)` inside the subquery | Restores INTEGER affinity, re-opening the ephemeral-index path. 117 s → 0.10 s in production. |
| `WITH h AS MATERIALIZED (…)` | Forces table materialization, hoisting the aggregation out of the loop. On the debug build: 167.5 s → 5.6 s with 100 `watchlist` rows; with the full 2,000 rows the default plan was abandoned after several minutes while the `MATERIALIZED` plan completes in 33.6 s. |

The `AS MATERIALIZED` plan is the shape the planner should reach on its own — the aggregation runs once and the join re-scans the small cached result, exactly SQLite's plan for this query:

![AS MATERIALIZED workaround: aggregation hoisted, runs once](https://raw.githubusercontent.com/OrthelT/turso-dev/df331ba86e34de5fa8c3b8603193ac76174cf35f/docs/perf/subquery-materialization/planviz/materialized-workaround.png)

Note: a plain single-reference `WITH h AS (…)` (no hint) reproduces the bug identically; only the explicit `MATERIALIZED` keyword avoids it.

## Suggested fix direction

**Primary — decouple decision (A) from decision (B).** Add a table-backed materialized access method (no probe index) and return it, instead of `AccessMethodParams::Subquery`, from the fallback paths in `find_best_access_method_for_subquery` whenever the subquery is uncorrelated and not in the driving position. There are five such paths at `d14a446`: the returns near lines [1538](https://github.com/tursodatabase/turso/blob/d14a446dae10c90740afb1f8d9c9c4fe025f2c74/core/translate/optimizer/access_method.rs#L1538), 1552, 1596, 1658 — **and the intrinsic-order return near line 1528**, which is guarded by `extremum_constraints_compatible || usable.is_empty()` and is reachable through exactly this affinity mismatch (it empties `usable`). `choose_from_clause_subquery_execution_mode` needs a matching arm so `Operation::Scan` + the new marker maps to table materialization rather than `Coroutine`.

Most of the machinery exists already — the plausible shape of the fix is extending `4f9f028d9`'s CTE "materialize into a table, then build the autoindex if useful" path to uncorrelated derived tables. Emit-side pieces: `emit_materialized_subquery_table` (`subquery.rs:1785`) and the `EphemeralTable` Rewind/Next scan path (`open.rs:213`). Threading the new access method through the cost model is the non-trivial part; the codegen is not.

Two design notes:

- Prefer **structural** gating (coroutine only when the subquery is in the driving/leftmost position, as SQLite does) over gating on the `input_cardinality > 1` estimate — if the planner underestimates the outer side, an estimate-based gate silently resurrects the bug. Let cost decide only table-vs-index, not materialize-vs-coroutine.
- **Do not change `index_affinity_ok`** — it matches `sqlite3IndexAffinityOk` exactly, and SQLite makes the same index-rejection decision on this query.

**Secondary (separate follow-up, deliberately not part of this issue's ask):** coercing the ephemeral probe index key to the comparison affinity at build time would recover the *seek* even on mismatch — going beyond SQLite. But the FROM-subquery ephemeral index is covering (the key column is also a result column), so naive coercion (`'007'` → `7`) would leak into query output. The `IN`-subquery path (`subquery.rs:990`) coerces safely only because IN probes test bare membership. Needs its own issue.

**Testing:** a `.sqltest` in `sqlite/conformance/sqlite-sqltests/` asserting the `EXPLAIN QUERY PLAN` shape in both directions — the INTEGER-outer/TEXT-subquery LEFT JOIN must show materialization (not a coroutine `SCAN h`), and the matched-affinity variant must still get its ephemeral seek index.

## Environment

Reproduced at [`d14a446`](https://github.com/tursodatabase/turso/commit/d14a446dae10c90740afb1f8d9c9c4fe025f2c74) (2026-08 main) and re-confirmed on the newer PR #8316 branch (`dc3ada223`) via its structured plan JSON. All Turso timings from a debug build as noted; SQLite timings from an optimized 3.45.1 library.
