# Research & Documents Plugin (`documents`)

`app/plugins/documents.py` · permissions: `data:read, artifact:write`.

| Operation | Needs | Behaviour | Verified |
|---|---|---|---|
| `report` | `documents.extract` | Every text/Markdown input is extracted in parallel (title, headings, `Key: value` facts, quantitative sentences with **line numbers**, links). Then `documents.build_report` writes a cited Markdown report, and the `documents.report_valid` gate runs | **real (demo P5-B)** |
| `compare` | `documents.compare` | similarity ratio, common/unique headings, unified diff artifact | real (tests) |
| `cross_report` | + `data.profile` | document report plus a data summary of attached CSV/JSON (cross-domain via the generic tool registry) | **real (demo P5-E)** |
| `summarize` | + `model.text` | model summary grounded in the supplied documents, cited `[S1]…`, labelled "generated; verify against sources" | mocked provider only; real: NOT CONFIGURED |
| `web_research` | `search.web` | refused: no search provider exists, so no search is pretended | NOT CONFIGURED |

**Provenance.**
- Every fact is tagged `[S<n>, line <k>]`, and sources list file name, title and input id.
- The report states that all facts are user-provided and no external sources were consulted.
- Unsupported attachments (e.g. PDF) are listed under Limitations, never silently treated as processed.

**Gate `documents.report_valid`.**
- The artifact exists and is readable.
- Required sections are present (`## Sources`, `## Extracted facts`, `## Document structure`, `## Limitations`).
- Every citation maps to a source, and links are well-formed.
- Links are **not** fetched (no network tool).

**Limits.** UTF-8 text/Markdown only, 500 000 chars per document. Extraction is rule-based.
