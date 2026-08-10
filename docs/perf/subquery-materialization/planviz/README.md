# Visual evidence via PR #8316's plan visualizer

Plan-shape visualizations of the coroutine-collapse bug described in
[`../findings.md`](../findings.md), rendered with the provisional `tursodb
--planviz` scaffolding from upstream PR
[#8316](https://github.com/tursodatabase/turso/pull/8316) (branch
`claude/turso-plan-visualizer-jt5p13` @ `dc3ada223`). That branch is based on
a main newer than `d14a446`, so these captures also confirm the bug still
reproduces there.

Dataset: same shape as the findings repro — `watchlist` 2,000 rows,
`history_txt`/`history_int` 200,000 rows each. The visualizer only *prepares*
`EXPLAIN QUERY PLAN`; nothing is executed.

## Structured-JSON confirmation

PR #8316 also adds a machine-readable plan export (`POST /plan`), which states
the defect without any bytecode reading. For the subquery reader node `h`:

| join key | `op.type` | `subquery.execution` | full plan JSON |
|---|---|---|---|
| INTEGER = VARCHAR (bug) | `scan` | **`"coroutine"`** | [`plan-txt.json`](plan-txt.json) |
| INTEGER = INTEGER | `search` (ephemeral index `type_id=?`) | **`"indexed_materialized"`** | [`plan-int.json`](plan-int.json) |

## Captures

**`coroutine-bug.png`** — the mismatched-affinity plan (`history_txt`,
VARCHAR key). The entire subquery body — 200,000-row `SCAN history_txt` plus
the GROUP BY sorter — sits in a `CO-ROUTINE` container feeding the join's
inner side, whose edge is labeled **"per outer row ↻"**: the aggregation
re-runs once per outer row (2,000×).

**`coroutine-bug-detail.png`** — same plan with the node detail panel open on
`SCAN h`, showing the raw structured field `"execution": "coroutine"`.

**`matched-affinity-indexed.png`** — the identical query against
`history_int` (INTEGER key). The same subquery body now lives in a
`MATERIALIZE → EPHEMERAL INDEX` container whose edge is **"run once, store"**,
and the join's inner side is a `SEARCH` seek on
`ephemeral_subquery_t3 (type_id=?)`.

**`materialized-workaround.png`** — the `WITH h AS MATERIALIZED (...)`
workaround on the VARCHAR variant. The aggregation is hoisted into a
`MATERIALIZE CTE · run once, shared` container; the reader re-scans the
~2,000-row cached result per outer row (no seek — exactly SQLite's plan shape
for the mismatched-affinity query). This is the plan the primary fix should
produce by default.

The side-by-side story for the issue: `coroutine-bug.png` vs
`matched-affinity-indexed.png` is the bug (one affinity change moves 200k-row
aggregation into or out of the outer loop); `materialized-workaround.png` is
the missing middle rung of the ladder.

## Regenerating

```console
$ git fetch https://github.com/tursodatabase/turso claude/turso-plan-visualizer-jt5p13
$ cargo build --bin tursodb            # on that branch
$ tursodb repro.db --planviz 127.0.0.1:8321
# open http://127.0.0.1:8321/#sql=<url-encoded repro query>
# JSON: curl -X POST http://127.0.0.1:8321/plan -d '{"sql": "..."}'
```
