# Data Processing Plugin (`data`)

`app/plugins/data.py` · permissions: `data:read, artifact:write`. Stdlib `csv`/`json` only. No user code, SQL or formulas are executed.

| Tool | Purpose |
|---|---|
| `data.profile` | rows, per-column types, nulls, distinct values, min/max/mean, duplicate rows, parse warnings |
| `data.validate` | column types (`int`/`float`/`bool`/`str`), required (no nulls), unique |
| `data.transform` | ordered ops: `select`, `rename`, `filter` (`equals`/`not_equals`/`gt`/`lt`/`gte`/`lte`/`in`), `sort`, `dedupe`, `cast`, `limit` |
| `data.aggregate` | group-by with `sum`/`count`/`mean`/`min`/`max`; non-numeric values are skipped and counted |
| `data.join` | inner/left join on one column; column clashes renamed `_right`; unmatched rows counted |
| `data.convert` | CSV ↔ JSON |

**Workflow** (`transform`, `aggregate`, `convert`, `join`):

```
profile_in → [schema_in → schema_gate] → produce → data.output_valid gate → profile_out → summary report
```

A schema violation blocks the export: no output artifact is created.

**Reproducibility.** Every output is a **new** artifact. Its `data_meta` records:
- source and source ref
- the exact operations, with rows before/after and the discarded count for each
- failed conversions, rows in/out, output columns, format, warnings

The original input is never modified.

**Gate `data.output_valid`.**
- Re-parses the output artifact.
- The row count and column list must match the recorded metadata, and a transform must not produce more rows than it read.
- Applies an optional output schema.

**Limits.** 200 000 rows, 500 columns, attachment size limit. Nested JSON values are rejected (`unsupported`).

**Example** (demo P5-C, real):

```json
{"objective": "aggregate the sales by region", "attachment_ids": ["att_…"], "data": {"group_by": ["region"], "metrics": {"amount": ["sum", "count", "mean"]}}}
```
