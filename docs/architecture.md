# Architecture

This describes what is implemented. Where something is a design intention only, it says so.

## 1. The shape of the problem

Information about a well is scattered: weekly inspections (sensor readings and spill detections), repair records, free-text repair
reports, regulator events, human notes, and rules that live in long manuals and SOPs. Questions combine them, and several answers
depend on rules (what counts as "producing", deadlines, spill classes) that must be *applied exactly*, not recalled.

Design stance: **the language model reads, plans and explains; deterministic code decides.** Every number, window, threshold,
deadline and classification is computed from records and from rules extracted from the documents. The model never calculates and
never sees the database.

## 2. Ingestion (`copilot/ingest.py`, `extract.py`, `db.py`)

* **Dynamic discovery.** Files are found by glob and classified by name/location (`fleet/…`, `assets/<id>/…`). No asset id is
  hardcoded; a new asset folder is picked up on the next run.
* **Validation.** Each record is checked (required fields, timestamps, plausible temperatures, non-negative volumes, known
  asset, duplicate ids, dangling references). Bad records are rejected with an issue row (file, line, code, message); a bad file
  never aborts the run.
* **Provenance.** Every row keeps `src_path` and `src_line`; reports and manuals are stored as documents (also indexed with SQLite
  FTS5). The evidence viewer opens records by those references.
* **Incremental.** Files are hashed; unchanged files are skipped, changed files replace exactly their own rows, removed files remove
  their rows. The report lists outstanding issues for the files currently present, so a re-run never makes a known problem look fixed.
* **Rules from documents** (`extract.parse_manual`, `parse_sop`). Thresholds (production temperature difference, alarms, spill
  classes, SR-01..03 and RC-01..04 deadlines, RCA counts) are read from the manuals/SOPs with document, section and the matching
  passage. Inheritance between manuals ("follow the H100 manual") is a rule. Values outside plausible ranges are **quarantined**;
  documents that disagree make a rule **unavailable** and are listed for review; a manual with no readable production rule produces a
  warning. A rule that cannot be read yields "unknown", never a default.
* **Facts from report text.** Containment time, soil sample counts, TPH and the compliance table are extracted from free-text
  repair reports (the containment time exists nowhere else).
* **Two databases.** `source.db` is derived from the dataset and can be discarded and rebuilt (an older schema is rebuilt
  automatically). `state.db` holds user state (corrections, claims, conversations, memory, audit log) and is never touched by
  ingestion. Corrections whose target disappeared from the data are flagged, not dropped.
* The dataset folder is only ever read.

## 3. Deterministic domain layer (`copilot/domain.py`)

* **Effective inspection** = source row + overlays: dataset correction notes (a spill detection later judged to be rain/shadow),
  and user corrections (latest wins; competing corrections by different people are kept and flagged). The original values are always
  retained and shown.
* **Production status** = wellhead − ambient ≥ the well type's threshold (inherited if the manual says so). Absolute temperature is
  deliberately ignored (a hot idle wellhead is still idle). Missing data or a missing rule gives "unknown".
* **Downtime** = 7 days per weekly inspection in the window that shows the well not producing (missed inspections add nothing).
* **Spills** exclude corrected detections, classify with the SOP's thresholds, link repairs/regulator events/informal notes.
* **Compliance** (`spill_compliance`, `asset_compliance`): SR-01 notice deadline (24 h / major), SR-02 containment (from the repair
  report), SR-03 verification, RC-01 inspection gaps, RC-02 annual flowline test (only for fully observed years), RC-03 long
  downtime, RC-04 notice responses, RCA required, unrecorded planned shut-ins. Verdicts are *met / breached / pending / unknown /
  no report on record*; absence of a record is reported as such.
* **Rules overrides.** Only company-policy keys (RCA count/window) can be overridden, at well-type or asset scope, by a permitted
  role; regulatory thresholds and the production definition cannot be changed from chat or the UI.

## 4. Tools (`copilot/tools.py`, `tool_impl.py`)

An explicit **allow-list of 22 named tools** (19 read-only, 3 that record user statements) with strict pydantic argument schemas
(unknown fields rejected). One uniform result envelope for success and every failure kind; errors are sanitised (no paths, SQL or
stack traces). Calls are serialised, time-bounded (the SQLite statement is interrupted on timeout), and results are capped (rows,
evidence, argument size). There is no code, SQL, shell or file access through the registry (checked by tests that inspect the source).

Each result carries: a narrative with `[type:id]` citations, an evidence list (record, computed, rule, note, claim, correction,
document), warnings and assumptions, and **prebuilt, schema-validated visuals** (the backend builds them from the same rows).

Write tools are gated: the user's own message must contain a verbatim quote (and the stated value), the message must contain a
statement (not only a question), the user must be known, and at most three changes happen per message. Tools can additionally be
configured to require an explicit `confirm()` step. Text retrieved from documents is flagged untrusted and can never satisfy the
quote check.

## 5. Orchestrator and model (`orchestrator.py`, `llm/`)

```
question + user ──► system prompt (user, role, data date, areas/types, conventions)
   loop (max 6 rounds, 10 tool calls, 4 per round):
      Gemma 4 ──► function calls ──► registry validates + executes ──► function responses (compact view + evidence index)
   final text ──► post-processing (all deterministic)
```

* **Provider** (`llm/gemma_api.py`): the official `google-genai` SDK, `gemma-*` models only, no fallback. Reasoning tokens share the
  output budget, so budgets are generous; a truncated reply is retried once with a larger budget, then fails clearly (never shown
  half-finished). Requests are paced (one at a time, minimum gap that widens after rate limits), retries honour `Retry-After` / the
  API's retry hint with jittered exponential backoff, are bounded, and never retry authentication or validation errors.
  Tool declarations are sent in a slim form (types, enums, required, short descriptions) to cut per-request tokens by ~40%.
* **Post-processing**: citations are normalised against the evidence actually retrieved (comma lists accepted; invented ids and tool
  names removed and reported); numbers in the answer that no tool returned are flagged; the visual is chosen from what the tools
  built (a visual the user named wins; the model may suggest one with a trailing `[visual: table]`); conflicts, uncertainty,
  assumptions and confirmations are taken from the tools' output, not from the model's prose; changes recorded by the request are
  listed even if a later step fails.
* **Memory**: each answered question stores, for that user only, the wells it covered. Recall is a tool restricted to the caller.
  Conversation history is owner-only. Shared knowledge is the set of notes and *shared* claims.

## 6. Evidence model

Evidence kinds: *source record*, *calculated value*, *rule* (with the manual passage), *correction* (original kept), *claim*
(unverified unless the data supports it), *note*, *document*. `GET /evidence?ref=type:id` resolves a reference to the stored record
with source file/line, related records and the audit trail. Calculated values have no stored record; the UI says so and shows
their inputs.

## 7. API and UI (`api.py`, `views.py`, `web/`)

FastAPI serves JSON and the static UI. Read models (`views.py`) reuse the domain functions and registry tools, so a dashboard number
and a chat number cannot disagree. Direct feedback forms call the same `Feedback` service as the chat tools, so role gates,
scope checks and audit entries are identical. Consequential forms require `confirmed=true`. Security headers and a strict CSP are
sent; the UI uses no `innerHTML`, no external resources and no inline scripts.

The UI is plain ES modules (no build step): safe DOM builder, accessible SVG/HTML renderers for each visual with a data-table
alternative, an evidence drawer, confirmation dialogs, skeleton/empty/error states, and a layout that works down to phone width.
Status is always text plus a shape, never colour alone.

## 8. Trust boundaries

| Input | Treated as |
|---|---|
| Dataset files and report text | data; never instructions (document text is flagged untrusted and cannot trigger writes) |
| Model output | untrusted: tool calls are validated; citations, numbers and visuals are checked; writes need the user's own quote |
| User identity | **asserted by the client** (a picker, not authentication) |
| Browser | untrusted: every permission is enforced in the backend |

## 9. What is not built

Live drone, sensor, work-order or control integrations; local (Ollama) models; authentication; multi-user concurrency beyond one
request at a time; a production deployment story; the demo video and the challenge write-up.
