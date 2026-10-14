# Local Power BI knowledge

The Studio now retrieves local reference passages for conversational answers,
initial planning, governed revisions and advanced DAX proposals. This is retrieval
augmentation, not fine-tuning or an autonomous learner. It works with the selected
local runtime; it does not require another model or a vector database.

## Using it

1. Open **Settings**, then scroll to **Power BI knowledge**.
2. Search `weighted average`, `LOD fixed grain`, `duplicate keys`, or a DAX function.
3. Expand a passage to inspect its source, text and content hash. Documentation,
   recipe-only and combined search are available without a model call.
4. Ask the workspace conversation your question as usual. Relevant passages are
   supplied automatically; a successful discussion response includes a deterministic
   **References supplied—not execution validation** footer.
5. Review proposed formulas/relationships and the dashboard before approving a build.

Opening a Microsoft source link uses the internet; local search, chat retrieval
and report generation do not fetch documents at runtime.

## Contents and updates

The distributable code includes 13 original source-linked documentation summaries
and 10 original recipe guides: ratios, weighted averages, fixed grain, scalar/context
pitfalls, rolling windows, inactive dates, header/line grain, key integrity, return
rates and mixed-grain budgets. These are reference-reviewed guides, not certified
Power BI engine fixtures. New guides must go through code review and tests.

This development environment now indexes the reviewed archive's **1,738 unique
Microsoft Learn pages**: 435 DAX, 912 Power Query/M, 220 modeling, 167 report design,
and 4 safety pages. These yield 14,948 section passages; with the existing cache
and original guides there are **15,085 passages and 10 reviewed recipes**. The
14 starter source URLs overlap this larger collection, so they are not counted
as additional documents. The cache records source URLs, revisions, retrieval time
and SHA-256 hashes; originals are retained.
Downloaded pages remain local-only and must not be included in the installer or
redistributed without establishing applicable content rights. Packaged installations
work offline with the original summaries and recipes even without this cache.

Explicit online setup/update (from the repository root):

```powershell
.venv\Scripts\python.exe scripts/snapshot-powerbi-knowledge.py --state-root .runtime/studio-dev-state
```

For a native installation, use its actual state root under
`%LOCALAPPDATA%\AI Power BI Analyst`. Restart the local engine after updating.
The setup script requests only fixed Microsoft documentation URLs, never data or
user prompts. It downloads all selected pages successfully before replacing the
snapshot. No automatic crawler, folder ingestion or writable knowledge API exists.

### Import the reviewed research archive (offline)

```powershell
# Inspect first; this command does not write any files.
.venv\Scripts\python.exe scripts/import-powerbi-knowledge.py "C:\path\to\research.zip" --state-root .runtime/studio-dev-state
# Explicitly import after inspection.
.venv\Scripts\python.exe scripts/import-powerbi-knowledge.py "C:\path\to\research.zip" --state-root .runtime/studio-dev-state --apply
```

The importer reads only `data/data-first-001/reference/pages.jsonl` from
`03-Reference-data-part-04-of-17.zip`. It verifies the nested package and member
against the upload manifest and verifies each page text and its sections. The
manifest establishes integrity within the supplied archive, not a Microsoft
digital signature. Training/validation/test answers, historical dataset versions,
unverified extracted components, notebooks and scripts are **not indexed or run**.

Local files beneath the chosen state root:

| Location | Purpose |
| --- | --- |
| `knowledge/reviewed-archive.json` | Versioned retrieval passages, topic/section metadata, source manifest and omission receipts |
| `knowledge/original-collections/<hash>.jsonl` | Byte-exact original source records, including full source text |
| `knowledge/index.sqlite3` | Rebuildable SQLite FTS5 search index |
| `knowledge/backups/<hash>.json` | Prior imported corpus retained on replacement |

Import uses a lock, a staged reader/index validation and atomic replacement. It
preserves the starter snapshot and user workspaces. Existing processes keep their
current corpus until the local engine restarts; this session's engine has been
restarted with the expanded collection. To roll back a corpus update, stop the
engine, restore the selected backup as `reviewed-archive.json`, and restart. The
index automatically rebuilds against the restored corpus hash.

### Model-facing format and retrieval

Passages are grouped by source title, topic and section. Small API pages get a
**Quick reference** combining definition, syntax, parameters, return value and remarks.
Larger API cards pack whole blocks and explicitly label missing material as a
partial reference, rather than returning an isolated return-type line as the answer.
Long pages are split on paragraph boundaries, retaining section headings. Fenced
formulas remain whole, including nested TMDL fences. Thirty-two oversized atomic
blocks were omitted from retrieval, with their hashes and reasons recorded; their
full contents remain in the original collection. This is deliberately not a claim
that every source paragraph fits the small model's context.

Explicit function names are prioritized over incidental text matches. Ambiguous
ordinary words such as "fixed" do not automatically select the DAX `FIXED` function.
Power Query lookups do not inject unrelated DAX recipes. Reviewed recipes require
topic cues instead of matching generic words such as "date". Search does not pad
an exact-function answer with unrelated documents just to fill the context budget.
Each model receipt contains source-linked text; full archive/page/chunk provenance
is stored as evidence without sending bulky provenance metadata to the model.

Local regression check:

```powershell
.venv\Scripts\python.exe scripts/check-powerbi-knowledge.py --state-root .runtime/studio-dev-state --output .runtime/retrieval-check.json
```

The 20-query development regression retrieved the expected source in 20/20 cases
across DAX, M, relationships, security and report design. Three retrievals per case:
median 7.2 ms, p95 20.2 ms, maximum 21.4 ms; library initialization/index update 1.13
seconds and approximately 91 MiB additional process RSS in this run. All contexts stayed within
2,400 characters. These are local retrieval measurements, **not a model accuracy
score, blind benchmark, or proof of DAX execution**. Re-run on target hardware.

An opt-in, report-read-only model check is available in
`scripts/smoke-powerbi-knowledge.py` with the same `--state-root` and `--output`
arguments. It asks three reference questions and records retrieved context and
streamed answers; it does not approve formulas or alter Power BI projects.
The local Qwen smoke run answered the key facts correctly in all three questions
(Table.NestedJoin, DIVIDE and Table.Buffer). Its wording that an internal-use
parameter is "not available" is stronger than the documentation's "intended for
internal use"; do not treat these answers as verbatim documentation or certified
API behavior. Raw prompts, contexts and responses are retained locally in
`.runtime/research-review/model-reference-smoke.json`.
The final repeat additionally records model identity and generation metrics in
`.runtime/research-review/model-reference-smoke-final.json`. Unit/integration
coverage includes chat/planning injection, strict source validation, context limits,
cross-domain retrieval regressions, archive exclusion and failed-import preservation.

## Design and trust boundaries

- SQLite FTS5/BM25 indexes local passages; no embeddings, cloud calls or extra model.
- Sources and recipes have strict schemas, allowlisted source URLs and content hashes.
- Chat/planning reference context is capped at 2,400 characters; search at 3,600.
  Whole passages are selected, not truncated formulas. These are character budgets,
  not an exact tokenizer guarantee for every model or language.
- Chat workspace context is capped at 5,000 characters using whole-record compaction.
  It marks omitted metadata and favors fields mentioned in the question. Recent
  conversation history is capped separately; oversized requests and very large
  planning catalogs remain a qualification concern for the 4,096-token model window.
- Approved workspace definitions and reviewed relationships are eligible context.
  Approval is never described as proof of numerical correctness. AI conversation
  history is never automatically promoted to trusted recipes.
- Retrieved text is quoted data, not permission to run tools or approve changes.
  Example names must be rebound to the real catalog. Existing deterministic
  compilation, reference checks, project transactions and approval gates remain.
- Retrieval receipts record selected text, source IDs/hashes, corpus/query hashes,
  timestamp and planning stage in workspace evidence (`knowledge.retrieved`).
- A new DAX argument check rejects clear errors such as `SUM(Price * Quantity)`.
  The advanced-proposal repair gets the rejected proposal, validation error and
  relevant references, with one repair attempt. This is not a complete DAX parser.
- No automatic report writes or automatic learning from successful-looking answers.

Read-only authenticated endpoints: `GET /v1/knowledge` and
`GET /v1/knowledge/search?q=...&mode=hybrid|docs|recipes`.

## Local experiment: 6 September 2026

Qwen `qwen3.5:4b-q4_K_M`, digest
`2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd`,
Ollama 0.33.3. Frozen prompts covered weighted averages, fixed grain and duplicate
header totals, each with a changed-table/field-name variant. Each prompt was run
once per condition, not a statistically reliable benchmark. No generated DAX was
executed in Power BI.

| Condition | Review result |
| --- | --- |
| Baseline | 0/6 fully acceptable; invalid aggregate arguments, invented names/functions, wrong filters or wrong grain |
| Documentation | 3/6 fully acceptable; remaining answers retained an unwanted filter, reused an existing measure name recursively, or iterated line grain |
| Documentation + recipes | 5/6 acceptable for further validation; the sixth had the intended fixed-grain logic but used an unqualified `[Clinic]` column, flagged for explicit catalog binding |
| Recipes + thinking (2 cases) | Both exhausted 768 generation tokens without a final answer, about 144 seconds each; inconclusive correctness, poor experience under this budget |

The combined condition correctly addressed all three changed-name concepts; the
qualification flag above is deliberately conservative. One weighted-average answer
returned zero for zero weight; that prompt allowed safe handling without prescribing
BLANK. The separate changed-name prompt explicitly required BLANK and received it.

The run paused once at the 2 GiB memory-headroom gate, unloaded Qwen, and resumed
only missing answers from the frozen design. Non-thinking outputs took roughly
10–32 seconds, but concurrent activity and different prompt lengths prevent a clean
throughput comparison. Thinking stays disabled by default. K2 was not requalified
in this experiment; retrieval integration itself is shared across selected runtimes.

Reproduce with `scripts/evaluate-powerbi-knowledge.py --thinking`; use `--resume`
with its output folder after an interrupted run. Raw prompts, frozen contexts and
responses are local-only under `.runtime/knowledge-evaluation-20260907-002230`.
Do not treat this small review as the release accuracy gate or engine validation.

Remaining qualification: broader randomized schema cases, repeated Qwen/K2 comparisons,
Power BI numerical goldens and filter-context tests, packaged installation, and
existing Desktop acceptance gates. RAG improves grounding; it cannot guarantee DAX.
