# QA report

What was actually run, with what result. Four kinds of evidence are kept apart because they prove different things:

| Kind | What is real | What is replaced | What it proves |
|---|---|---|---|
| **Unit / integration** | code, SQLite, real dataset or synthetic datasets | nothing (no model involved) | deterministic logic is correct |
| **Mocked-model** | orchestrator, tools, API, citations, visuals | **only the language model** (scripted) | application logic around the model; *not* that Gemma behaves well |
| **Browser** | real UI in Edge, real backend, real data | the language model (scripted) | the UI is wired to real endpoints |
| **Live model** | everything, including hosted `gemma-4-26b-a4b-it` | nothing | end-to-end behaviour on the example questions |

Environment: Windows 11, Python 3.13, Edge via Playwright, google-genai 2.31.0. Dataset: the challenge dataset (200 wells), untouched.
"Today" for the data is 2026-10-17 (day after the newest inspection).

## 1. Automated test suite

`python -m pytest` → **481 passed, 1 skipped** (~155 s). The skipped test is the opt-in live provider check (`COPILOT_LIVE=1`);
live results are in section 2.

| File | Tests | Kind |
|---|---|---|
| test_tools | 128 | integration (registry, schemas, limits, write gates) |
| test_orchestrator | 81 | **mocked-model** |
| test_llm_provider | 42 | mocked transport (SDK replaced) |
| test_credentials | 37 | unit (fake keyring) |
| test_ui_e2e | 34 | browser, model scripted |
| test_api | 24 | integration, model scripted |
| test_llm_pacing | 23 | unit (fake clock) |
| test_domain_eval, test_visuals | 22 each | integration / unit on the real dataset |
| test_feedback_eval | 16 | integration |
| test_generalization | 15 | integration on a second dataset (section 4) |
| test_findings, test_ingest, test_rules_validation | 13, 12, 12 | integration |
| test_llm_live | 1 | live, opt-in (skipped in the default run) |

## 2. Live-model verification of the 18 official questions

Runner: `scripts/run_live_eval.py` (real orchestrator, real tools, real hosted model; multi-step chains for the memory, correction,
rule and claim questions). Matrix: `python scripts/summarize_live_eval.py`. Each row is the **most recent** run of that step;
7 report files were read. Hard checks assert facts computed independently from the data (ids, counts, dates, flags); soft checks
(`~`) are wording hints and are not decisive. Statuses: PASS / FAIL / BLOCKED (provider failure) / NOT TESTED.

| Q | Step | Status | Hard | Visual | Time |
|---|---|---|---|---|---|
| 1 | status card, one well | PASS | 8/8 | card | 21.6 s |
| 2 | wells not producing + reasons | PASS | 4/4 | table | 88.9 s |
| 3 | 13-week line chart, not-producing marked | PASS | 2/2 | line | 52.7 s |
| 4 | spill count/total, corrected excluded | PASS | 4/4 (soft 0/1) | table | 111.2 s |
| 5 | downtime in a year (hot-day trap) | PASS | 2/2 (soft 0/1) | bar | 11.4 s |
| 6 | top-3 downtime, bar | PASS | 4/4 | bar | 18.8 s |
| 7 | spills in 90 days, map | PASS | 2/2 | map | 126.2 s |
| 8 | spills area × month, heatmap | PASS | 2/2 | heatmap | 33.8 s |
| 9 | rules broken + open items | PASS | 5/5 | table | 125.6 s |
| 10 | spills not contained in SOP time | PASS | 3/3 | table | 78.9 s |
| 11 | a: ask on day 1 / b: "the wells we discussed yesterday" | PASS / PASS | 1/1, 4/4 | bar | 171.6 / 82.2 s |
| 12 | a: correct raw data / b: Q2 re-asked | PASS / PASS | 5/5, 2/2 | line / table | 77.9 / 31.7 s |
| 13 | correct AI estimate 4.5 → 5.4 bbl, consequences | PASS | 5/5 | – | 213.1 s |
| 14 | a: RCA (manual) / b: team rule / c: RCA (team rule) | PASS ×3 | 1/1, 1/1, 3/3 | table | 176.1 / 18.4 / 70.6 s |
| 15 | a: correction reaches other user | PASS | 4/4 | card | 155.4 s |
| 15 | b: spills needing a regulator report | **FAIL (automated check) – see note** | 2/3 | table | 115.3 s |
| 16 | a: shared claim / b: other user asks / c: privacy | PASS ×3 | 1/1, 3/3, 2/2 | table | 18.5 / 75.5 / 37.8 s |
| 17 | a, b, c: claims, conflict, contradicted by data | PASS ×3 | 1/1, 4/4, 3/3 | card | 109.0 / 67.7 / 111.5 s |
| 18 | disagreement about AI spill detection | PASS | 4/4 | table | 59.4 s |

**27 of 28 steps pass; 1 recorded as FAIL.**

**Q15b note (read carefully).** The automated check requires one of a fixed list of phrases for "overdue / no notice on record".
The model answered, for WELL-0151 (5.4 bbl): *"A written notice was due by 2026-09-25 15:30 UTC, but no report is currently on
record"*, which is the correct substance, in wording the phrase list did not contain. I judged this by reading the answer; the
check was **not** amended and the step was **not** re-run, so the matrix still shows FAIL. Treat it as "probably correct, unproven by the automated check".

**Soft-check misses** (not decisive, listed for honesty): Q4 did not mention the corrected detection by name; Q5's answer text
did not contain the dates.

**Provider reliability** (from run logs; every one was retried, counts are failed *attempts*): 44 server errors, 31 rate limits,
11 timeouts, 3 network errors. Several questions needed more than one run because of provider failures (see "Runs" in the
summary output). Latency is high (11–213 s) on the free tier.

**Limits of this evidence.** One run per question per round, one model, one dataset. LLM output varies run to run; a PASS means the
checks held in the latest run, not that every run will pass.

## 3. UI verified against the real backend and real model

* **Browser tests (34, model scripted):** navigation, fleet overview and KPI drill-downs, asset list filters, asset detail,
  every visual type with its data table, evidence drawer for each record type, correction / claim / rule forms with confirmation
  dialogs and role gates, error and empty states, keyboard operation, phone-width layout, no console errors besides expected 403/429.
* **One real end-to-end check (real Gemma, real backend, real browser):** user *Michael Turner*, question "Which 3 wells in
  Northeast Block 7 had the most downtime in the last 4 months? Show a bar chart." Result: progress indicator shown; answer in
  ~18 s (2 model calls, 1 tool call); bar rows WELL-0121 (T200) 49 days, WELL-0107 (T350) 42, WELL-0138 (T350) 42; three citation
  chips; opening the first showed the real record `INS-0121-0189` with `assets/WELL-0121/inspections.jsonl, line 189`; no console
  errors or warnings. Only this one question was run through the UI with the live model; the other UI flows were exercised with the scripted model.
* Not verified: screen readers (only structure, roles and keyboard were checked), browsers other than Edge, real mobile devices.

## 4. Generalisation on a second dataset

`scripts/make_variant_dataset.py` generates a synthetic dataset in the same format but different (different ids, areas, types and
thresholds, and differently worded documents) together with an **independent answer key** that does not use
the application's code. `tests/test_generalization.py` (15 tests) ingests it and checks: current status, downtime, spills and
SR-01/SR-02 verdicts, RCA candidates, repair costs, compliance, the tool layer, the API, and incremental re-ingestion
(new asset folder, edited file). **15/15 pass.** This covers the deterministic layers, tools and API only; Gemma was not run on the variant dataset.

## 5. Defects found by this testing and fixed (each with a regression test)

* Write then rate-limit produced "no answer" without saying a change had been recorded → the response now lists recorded changes even on failure (Q12a).
* False "nothing planned" when a planned-downtime claim existed → status and notes include planned/upcoming items (Q16b).
* Gemma citation formats (comma lists, tool names) → normalised against retrieved evidence.
* Empty reply when reasoning consumed the token budget → budgets raised, one larger retry, clear error.
* Tool timeout counted lock wait; abandoned calls could run late → started/abandoned events and a `busy` error.
* Key retained on the provider object (found by test) → read from env/credential store only when needed.
* Rules silently overwritten or applied when out of range/conflicting → validation, quarantine, status column.
* Generality bugs (inheritance wording, hardcoded id pattern, missing "no production rule" warning, example prompts naming challenge ids).
* Incremental ingest hiding earlier errors → outstanding issues are always reported.
* UI: stale renders overwriting newer pages, charts filled by CSS, link roles overridden, forms mounted into closed dialogs, `hidden` overridden, correction id not shown, focus stolen, header overflow.

## 6. Limitations

* No live drone, sensor, work-order or control integration; the data is a synthetic snapshot. Not production ready.
* The user picker asserts identity; it is not authentication. Server binds to localhost.
* Rule extraction is pattern-based; unrecognised wording gives "unknown" plus a warning.
* Free-tier provider: slow, intermittent 429/500. Failures are shown; there is no fallback model.
* Only `gemma-4-26b-a4b-it` was tested. Ollama/local models are not implemented.
* The demo video and two-page write-up were not produced.
