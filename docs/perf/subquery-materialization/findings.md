# Turso: grouped FROM-subquery re-executed per outer row when the join-key affinity blocks the probe index

Follow-up to closed issue **#2974** ("Materialize FROM-clause subqueries using some heuristic").
Origin: https://github.com/OrthelT/mkts-backend-staging/blob/performance-fixes/docs/turso-subquery-materialization.md
Verified against `OrthelT/turso-dev` @ `d14a446` by reproduction, `EXPLAIN` bytecode, git archaeology, and differential comparison against SQLite 3.45.1.

---

## TL;DR

`d33016ff8` gave the planner exactly one way to materialize an uncorrelated FROM-subquery: build an **ephemeral index keyed on the join columns**. There is no fallback for the case where that index cannot be built. A join-key affinity mismatch (INTEGER outer column vs VARCHAR subquery column) disqualifies the index, and the planner drops all the way back to a coroutine that is re-initialized inside the outer loop — re-running the entire GROUP BY once per outer row.

- SQLite ladder: index seek → **materialized re-scan**
- Turso ladder:  index seek → **re-execute**

The affinity check itself is a faithful port of `sqlite3IndexAffinityOk` and is **not** the bug. The missing rung is.

The two-stage "materialize into a table, then optionally build a probe index on it" model already exists in the codebase — `4f9f028d9` added it for CTEs, explicitly for SQLite parity. It was never extended to plain derived tables.

---

## Reproduction

```sql
CREATE TABLE watchlist   (type_id INTEGER PRIMARY KEY, name TEXT);   -- 2,000 rows
CREATE TABLE history_txt (type_id VARCHAR, price REAL, ts INTEGER);  -- 200,000 rows
CREATE TABLE history_int (type_id INTEGER, price REAL, ts INTEGER);  -- 200,000 rows

SELECT count(*), sum(h.avgp)
FROM watchlist w
LEFT JOIN (SELECT type_id, avg(price) AS avgp FROM history_XXX GROUP BY type_id) h
  ON w.type_id = h.type_id;
```

**`LEFT JOIN` is load-bearing** — it pins the join order with `watchlist` outer. With an inner `JOIN` the optimizer reorders the subquery to the outer position and the bug does not appear. Anyone trying to reproduce with `JOIN` will see nothing.

Debug build; outer table cut to 100 rows so the slow case terminates:

| variant | Turso plan | time (100 outer rows) |
|---|---|---|
| `history_int` (INTEGER) | `SEARCH h USING INDEX ephemeral_subquery_t3 (type_id=?)` | **2.1 s** |
| `history_txt` (VARCHAR) | `SCAN h` | **167.5 s** (~80x) |

Linear in outer-row count, consistent with the production report (2,354 x 797,817 => 117 s vs SQLite's 0.27 s).

Results are **correct** in every variant. This is purely a performance defect.

## Bytecode evidence (`EXPLAIN`)

Mismatched affinity — subquery body (addrs 2-45: full scan of `history_txt` into a sorter, then GROUP BY) sits *inside* the outer loop:

```
47  Rewind        3 -> 63    Rewind table watchlist   <-- outer loop starts
49    InitCoroutine 1  0  2                           <-- subquery re-armed EVERY outer row
50    Yield         1 59
...
62  Next          3 -> 48
```

Matched affinity — subquery runs once at addrs 1-47, *before* the outer `Rewind` at 49, writing into `ephemeral_subquery_t3`; the outer loop only does `SeekGE`/`IdxGT`.

**Visual evidence:** the same three plans rendered as dataflow graphs with PR
[#8316](https://github.com/tursodatabase/turso/pull/8316)'s `tursodb --planviz`
scaffolding — including structured-JSON `subquery.execution` confirmation
(`"coroutine"` vs `"indexed_materialized"`) on a branch newer than `d14a446` —
in [`planviz/`](planviz/README.md).

---

## Root cause chain

1. **`core/translate/optimizer/access_method.rs:1496`** — in `find_best_access_method_for_subquery`, candidate seek constraints are filtered by `c.can_drive_index_seek(&subquery.columns, false)`.

2. **`core/translate/optimizer/constraints.rs:198-209`** — `can_drive_index_seek` -> `satisfies_index_affinity` (`constraints.rs:187`).

3. **`core/vdbe/affinity.rs:300`** — `Affinity::index_affinity_ok`, a port of SQLite's `sqlite3IndexAffinityOk`:
   ```rust
   Affinity::Numeric | Affinity::Integer | Affinity::Real => self.is_numeric(),
   ```
   INTEGER vs TEXT => comparison affinity NUMERIC. The ephemeral index column inherits the subquery column's TEXT affinity. TEXT is not numeric => **rejected**.

4. **`core/translate/optimizer/access_method.rs:1538-1548`** — with `usable` now empty, the function early-returns `AccessMethodParams::Subquery` (a plain scan), never reaching the `MaterializedSubquery` construction at line 1678.

5. **`core/translate/subquery.rs:1293-1327`** — `choose_from_clause_subquery_execution_mode` materializes only for:
   - `Operation::Search(Search::Seek { index: Some(ephemeral) })`, or
   - `requires_table_materialization()` = explicit `WITH ... AS MATERIALIZED`, or a shared CTE with >1 reference (`core/schema.rs:3994`).

   A plain `Operation::Scan` on an uncorrelated subquery therefore falls to `FromClauseSubqueryExecutionMode::Coroutine`.

6. **`core/translate/main_loop/open.rs:196-211`** — the coroutine path emits `InitCoroutine` inside the outer loop body, with the comment *"In case the subquery is an inner loop, it needs to be reinitialized on each iteration of the outer loop."* => O(N x M).

**Affinity mismatch is only the easiest trigger.** Any reason the seek index is rejected — including having no usable join constraint at all (`access_method.rs:1596-1611`) — produces the identical collapse.

**The cost model already knows.** `access_method.rs:1445-1447` charges
`coroutine_reexecution_overhead = (input_cardinality - 1) * base_row_count * cpu_cost_per_seek`.
But with no materialize-and-scan candidate in the choice set, that penalty can only influence join order — and a LEFT JOIN pins the order.

## The gate is asymmetric

Only the **subquery-side** column affinity is checked. Verified via `EXPLAIN QUERY PLAN`:

| outer col | subquery col | comparison affinity | plan |
|---|---|---|---|
| TEXT | TEXT | BLOB | index |
| INTEGER | INTEGER | NUMERIC | index |
| TEXT | INTEGER | NUMERIC | index |
| **INTEGER** | **TEXT** | **NUMERIC** | **coroutine <- bug** |
| INTEGER | `CAST(x AS INTEGER)` | NUMERIC | index |

---

## SQLite comparison (differential, same data)

SQLite 3.45.1 applies the *same* affinity rule and also declines the index, but materializes regardless:

```
-- mismatched affinity (INTEGER vs VARCHAR)
MATERIALIZE h                      <-- GROUP BY runs ONCE
  SCAN history_txt
  USE TEMP B-TREE FOR GROUP BY
SCAN w
SCAN h LEFT-JOIN                   <-- re-scans 2,500 cached rows, not 200k raw rows
```
0.273 s for the full 2,000 x 200,000 case.

```
-- matched affinity (INTEGER vs INTEGER)
MATERIALIZE h
  ...
SCAN w
BLOOM FILTER ON h (type_id=?)
SEARCH h USING AUTOMATIC COVERING INDEX (type_id=?) LEFT-JOIN
```

SQLite loses only the automatic index on mismatch; materialization is unconditional.

---

## Prior art (git archaeology + tracker)

### #2974 is the direct ancestor — and its fix commit states the coupling as design

**#2974 "Materialize FROM-clause subqueries using some heuristic"** · closed **completed** 2026-02-17 · assignee jussisaurio · milestone 1.0 · `performance`. Body describes the general problem: subqueries are optimization barriers, Turso always uses coroutines where SQLite chooses, *"they effectively turn into n^m scans."*

Closed by **`d33016ff8`** (Jussi Saurio, 13 Feb 2026), merged as `ee3c2bfc5` "Add support for FROM-clause subquery materialization and CTE materialization". Commit message, verbatim:

> When a FROM clause subquery (derived table) is not in the leftmost join position and is not correlated, materialize it into **an ephemeral index keyed on columns used in join conditions**. This enables index seeks instead of re-executing the subquery for each outer row.

"Materialize" was *defined as* "build an ephemeral index keyed on the join columns." There was never a branch for the case where such an index cannot be keyed. **This bug is the unhandled case of #2974's fix, not a new class of defect.**

### The needed machinery already exists, scoped to CTEs

**`4f9f028d9`** (Jussi Saurio, 12 Mar 2026) "translate: align CTE materialization logic with sqlite":

> always materialize CTEs into tables to preserve row order of the original SQL statement, and then build autoindexes using our existing ephemeral index ad-hoc mechanism, instead of having specialized code for CTE-based autoindexes specifically.
>
> this is also what sqlite does and allows us to simplify code at the expense of a redundant table-then-index materialization in cases where the CTE is only used for index probing purposes.

That is precisely the two-stage table-then-index model this bug needs, already written and already justified by SQLite parity — applied to CTEs, never extended to plain derived tables. (`cf89f5712`, same day, re-added a "skip CTE table materialization" optimization on top. `07d516733` "Teach materialized CTEs to seek, not rescan" generalized ephemeral seek indexes to range constraints.)

### Siblings worth cross-referencing

- **#7393** (open) "LEFT JOIN over materialized subquery uses RHS collation for range seek" — `optimizer` `correctness` `joins` `collation`. Same access-method path; collation is affinity's neighbor in that code.
- **#8260** (open) "IN (SELECT ...) rescans the whole RHS once per outer row when the value is not in the list" — `performance` `query planner`. Same failure signature (per-outer-row rescan), different mechanism: there the ephemeral index *is* built once and the waste is a NULL-detection scan loop (`expr/translator.rs:291-328`).
- **#8274** (open) "Outer ORDER BY over an already-ordered compound subquery re-sorts from scratch" — same labels, same "redundant work per statement" family.

### Near-misses — do NOT cite as duplicates

- **#2926** (closed) "four-way LEFT JOIN using subqueries is 100x slower than sqlite" — same *shape* (LEFT JOIN + aggregated subqueries, ~100x), but closed by PR **#5659**, which is a constraint-usability fix ("use usable constraint even if preceded by unusable constraint on same column"). Does not touch this code path.
- **#8276** (open) "Outer WHERE is never pushed into compound legs" — different defect: a constraint that exists never *reaches* the inner plan (`optimize_subqueries` never reads `plan.where_clause`). Here the constraint arrives fine — with matching affinity it builds the index from exactly that equality. Pushdown also would not help in principle: the constraint is a join equality against a per-outer-row value, so pushing it down would make the subquery correlated, which re-executes per outer row by definition.

**No open issue covers this bug.**

---

## Workarounds (both verified)

| approach | effect |
|---|---|
| `CAST(type_id AS INTEGER)` inside the subquery | Changes the subquery column's affinity to INTEGER, re-opening the index path. Reported 117 s -> 0.10 s in production. |
| `WITH h AS MATERIALIZED (...)` | Forces `requires_table_materialization()`, hoisting the aggregation out of the outer loop — exactly SQLite's shape. No change to column types or the aggregation expression. |

Measured for the `MATERIALIZED` hint (debug build, ~30-60x constant-factor overhead vs release):

| plan | 100 outer rows | 2,000 outer rows |
|---|---|---|
| coroutine (current default) | 167.5 s | did not finish |
| `AS MATERIALIZED` | **5.6 s** | **33.6 s** |

Note the hint yields a re-scan per outer row, not a seek — which is precisely SQLite's behavior on this query.

A plain single-reference CTE (`WITH h AS (...)`, no hint) reproduces the bug identically; only the explicit `MATERIALIZED` keyword avoids it.

---

## Fix direction

**Primary — decouple materialization from seekability.** Add a table-backed materialized access method (no probe index) and return it, instead of `AccessMethodParams::Subquery`, from the fallback paths in `find_best_access_method_for_subquery` (`access_method.rs:1538`, `1552`, `1596`, `1658`) whenever the subquery is uncorrelated and `input_cardinality > 1`. Correlated subqueries must keep the coroutine (already handled at `access_method.rs:1464`). `choose_from_clause_subquery_execution_mode` needs a matching arm so `Operation::Scan` + the new marker maps to `MaterializedTable`.

Most of this likely already exists — see `4f9f028d9` above. The plausible shape of the fix is extending the CTE "always materialize into a table, then build the autoindex if useful" path to uncorrelated derived tables, rather than writing anything new. Emit-side pieces:
- `emit_materialized_subquery_table` (`core/translate/subquery.rs:1785`)
- `EphemeralTable` Rewind/Next scan path (`core/translate/main_loop/open.rs:213`)

Threading a new access method through the cost model is the non-trivial part; the codegen is not.

**Secondary (optional, goes beyond SQLite) — apply comparison affinity when building the ephemeral probe index.** The index is ephemeral and built solely for this join, so its key could be coerced to the comparison affinity at build time, recovering the *seek* rather than only the materialization. The IN-subquery path already does exactly this via `affinity_str` (`core/translate/subquery.rs:990`, `QueryDestination::EphemeralIndex`).

**Do not change `index_affinity_ok`.** It matches SQLite exactly.

**Testing.** Per `CLAUDE.md`, prefer `sqlite/conformance/sqlite-sqltests/` — a `.sqltest` asserting the `EXPLAIN QUERY PLAN` shape (materialized, not `SCAN h` under a coroutine) for the INTEGER-outer / TEXT-subquery LEFT JOIN case. Must fail on `d14a446` and pass after.

---

## Suggested issue framing

Title: *Affinity mismatch on a join key drops FROM-subquery materialization entirely, re-running the aggregation per outer row (follow-up to #2974)*

Labels: `performance`, `query planner`, `optimizer`.

Lead with `d33016ff8`'s commit message for the design gap and `4f9f028d9` for the existing machinery; cross-reference #7393 / #8260 as siblings; note explicitly that the affinity check is correct and should not be touched.

---

## Artifacts and caveats

Scratchpad (`/tmp/claude-0/-home-user-turso-dev/f07c8054-6767-5fc8-9cf6-b3f5d91c4c45/scratchpad/`):
- `big.db` — 2,000 x 200,000 dataset
- `m.db` — 100 x 200,000 dataset (slow case completes in ~168 s)
- `a.db` — empty schema for the affinity matrix

Caveats:
- All Turso timings are from a **debug build** (`cargo build`, per `CLAUDE.md`). Ratios are meaningful; absolute numbers carry ~30-60x overhead. SQLite timings are from an optimized library.
- #2974's two comments were not read — GitHub's API returned 403 through the session proxy and MCP issue reads were scoped to `orthelt/turso-dev`. The commit-message evidence above was obtained from git history instead and is stronger.
- ~~`git log` evidence came from `OrthelT/turso-dev` unshallowed (18,774 commits); commit hashes should match upstream for these pre-fork commits but are worth confirming against `tursodatabase/turso` before quoting in a public issue.~~ **Confirmed against upstream (2026-08-10)** via the GitHub API: `d33016ff8a43ee265ab2455d7a355d354b8ed471` ("Add support for materialized FROM clause subqueries and CTEs", authored 2026-01-28, committed 2026-02-13), merged as `ee3c2bfc55c788ace0dd09e3cee596cb928d09df` ("Merge 'Add support for FROM-clause subquery materialization and CTE materialization' from Jussi Saurio"), and `4f9f028d9194841a959fcf3f39c52490f5fcfc83` ("translate: align CTE materialization logic with sqlite"). The block quote in "Prior art" is verbatim from the upstream commit body. Note the *commit* subject differs slightly from the *merge* subject; cite whichever hash you quote.
