# Knowledge ingestion and MCP validation

Validation on 22 October 2026 found working repository code retrieval, alongside
document conversion and retrieval gaps. Registration alone was not counted as
successful ingestion or retrieval. Deployment identifiers and raw receipts are
kept outside this public report.

## Public sources and observed results

| Source | Ingestion result in the tested deployment | Retrieval result |
| --- | --- | --- |
| [pallets/click](https://github.com/pallets/click) | Complete: clone, code search, 405 vectors, 1,932 structural symbols, wiki and dependency scan verified. GraphRAG and SCIP skipped by configuration. | MCP code search returned `Context` at `src/click/core.py:238` and `Group` at line 1703. `understand(target="pallets/click::Context")` returned the correct definition first. |
| [Python graphlib reference](https://docs.python.org/3/library/graphlib.html) | Complete: one page fetched and stored. | MCP `search(scope="docs")` returned `knowledge_admission_unavailable`; semantic retrieval was not demonstrated. |
| [Click README](https://github.com/pallets/click/blob/main/README.md), registered as a personal S3 document | Complete: 1,778 bytes fetched, Markdown converted and stored. GraphRAG skipped because disabled. | Document search returned the same admission error. |
| [Attention Is All You Need](https://arxiv.org/abs/1706.03762), registered as a personal S3 PDF | Fetch verified; conversion failed because MarkItDown's PDF dependencies were missing. The asset incorrectly reported `complete`. | No successful document retrieval. With the corrected dependency set, the same PDF converted locally to 40,174 characters containing its title and `Transformer`. This is local validation, not a deployed ingestion pass. |

Control queries against existing repositories returned `HTTPAdapter` at
`psf/requests/src/requests/adapters.py:158` and `frontendServer` at
`GoogleCloudPlatform/microservices-demo/src/frontend/main.go:63`. The files and
line numbers were checked against upstream source. An absent-symbol query
returned zero results. Repository directory listing also returned the expected
Requests source files.

## Remaining retrieval limitations

- The gateway explicitly refuses non-code search without supported admission.
  Fixing ingestion does not enable semantic document retrieval.
- `browse` retrieved Click's generated wiki, but that wiki incorrectly described
  `Group(MultiCommand)` while the ingested source declares `Group(Command)`.
  Successful retrieval of generated documentation does not establish its accuracy.
- The structural lookup uses `code-index-fallback`. SCIP was disabled, so a full
  call graph was not established. A correctly qualified `impact` request was
  rejected by the gateway; earlier empty results with abbreviated targets are
  not evidence of zero impact. Use the full `owner/repository::Symbol` target.
- `secure(action="identify", repo="pallets/click")` returned dependency findings
  with presence-only reachability. This verifies access to scan results, not
  runtime exploitability or independent confirmation of every advisory.
- Some MCP calls were rejected with HTTP 403. They were recorded as rejected,
  not retried as successful queries. The pre-existing Requests capability
  manifest also reported code search unavailable despite successful searches.

## Corrections covered by this change

- Knowledge asset and status icons share the navigation's SVG rendering style.
- Document registration sends an existing authorized `s3://` object URI, replacing
  the unsupported `doc://` placeholder. Local file upload remains unavailable.
- URL and document details show their own ingestion stages with readable labels.
- Asset routes support both native CLI paths and the legacy UI paths using the
  same handlers and authorization dependencies.
- The ingestion image includes PDF, DOCX and PPTX conversion dependencies.
- Early document failures carry failed status; only a complete document result
  exits successfully, preventing the queue worker from publishing false success.

Regression checks cover both API paths and their scope enforcement, document
form submissions, fetch/conversion failures, scoped storage, stage rendering,
and responsive browser behavior. The remaining retrieval limitations above
require separate backend or deployment work and are not claimed fixed here.
