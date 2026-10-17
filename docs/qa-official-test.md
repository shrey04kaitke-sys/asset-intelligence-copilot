# QA report: official test dataset, original Q1–Q11

Written 2026-10-17 ~16:50 IST, after the 16:45 IST development/evaluation cutoff. **Only 3 of the 20 question turns (Q2, Q11, Q10) were attempted against the real model**, and none is a verified PASS. The rest were not run (provider rate limiting, then the deadline). Everything below is what was actually observed.

## 1. Dataset loaded

| | |
|---|---|
| Source | https://github.com/FlytBaseAILabs/flytbase-ahc-swe-asset-intelligence-dataset, `test-data/dataset/` |
| Revision | `009e2b5` ("Add test dataset and final questions"), shallow clone into a temp folder outside the project |
| Local path used | `%TEMP%\official\repo\test-data\dataset` (temporary; re-clone to reproduce) |
| Database | `data/official-test/source.db` (new, separate, git-ignored). The original `data/source.db`, `data/state.db`, `dataset/` and earlier reports were not touched |
| Command | `COPILOT_DATA_DIR=data/official-test COPILOT_TODAY=2027-01-15 python -m copilot.ingest <path>` |
| Reference date | **15 Jan 2027**, confirmed by `test-data/README.md` ("Today is 15 Jan 2027"); set through `COPILOT_TODAY`; ingest reports `as_of: 2027-01-15` |
| Not read | `test-data/final-questions.md` (per instructions it supplements, and does not replace, the original questions) |

Ingest counts (from the ingest report and direct queries of `source.db`): 600 assets in 7 areas and 4 teams; 125,198 inspections;
17,415 maintenance jobs; 328 regulatory events; 1,887 notes (1,687 `operator_note`, 194 `correction`, 3 `data_correction`,
3 `operator_feedback`); 4,437 reports; 9 SOPs/memos and 3 manuals (T200, T350, T500); 28 rules extracted. **Rejected records: 0.
Ingestion issues: 0.** 7,451 files added. These match the README (600 wells, 7 areas, 4 teams).

## 2. Important gap: the implementation does not model the new data features

A search of `copilot/` for the new fields and record kinds found **no support** for: `type_history`, `team_history`,
`recorded_at` (late-entered inspections and jobs), `cost_revision`, `esp_pump`, temporary flowline bypasses, MOC approval memos,
and H2S readings. The official data contains no pressure field (the string appears only in repair-report text), and no H2S field was
found by the same search. Operator notes are stored as notes. `data_correction` rows were stored, but I did not verify that
the domain layer applies them. Consequences, **from code inspection only, not tested**:

* Wells whose type or team changed over time are likely to be evaluated with their current type/team only.
* Late-entered records are ordered by their own timestamps in the database, but the use of `recorded_at` as "known since" is not implemented.
* Revised costs are read from `cost_usd` (which the README says holds the revised amount); the revision history is not shown.
* The 28 extracted rules come from the existing parsers. Whether the new SOPs (operations rev2, regulator letter, recall notice, MOC memos) yielded the rules needed for Q3–Q5 was **not checked**; the recall notice and operations SOP are free text.

## 3. Q1–Q11 status

| Q | User | Status | Evidence kind | Note |
|---|---|---|---|---|
| 1 Reservoir pressure map | Michael Turner | NOT RUN | none | No pressure field was found in the inspection records by search. Likely unsupported by the data; not confirmed by running the question |
| 2 Most oil produced in 2026 | Robert Hayes | NOT RUN | none | No oil-volume field was identified in the inspection schema; not confirmed |
| 3 Gas notes vs SOP response | Laura Simmons | NOT RUN | none | Needs operator-note/SOP deadline logic that does not exist yet |
| 4 Flowline bypasses and MOC approvals | Chris Dalton | NOT RUN | none | No bypass or MOC support in the code |
| 5 Regulator letter, spills | Michael Turner | NOT RUN | none | Letter ingested as a document; no deterministic tool for its rules |
| 6 Inspection vs operator notes | Nathan Pierce | NOT RUN | none | |
| 7 Operator-note risk ranking (2 turns) | Robert Hayes | NOT RUN | none | |
| 8 Recall, private preferences (5 turns, 4 sessions) | Nathan / Robert / Olivia | NOT RUN | none | Claim, correction and private-memory machinery exists and was verified on the old sample questions only; recall data on this dataset was not checked |
| 9 Multi-topic session (6 turns) | Robert Hayes | NOT RUN | none | |
| 10 Shut-in vs repair, waiting reasons | Michael Turner | NOT RUN | none | |
| 11 Hotter region | Olivia Grant | NOT RUN | none | |

There are no deterministic, mocked or live-Gemma results for Q1–Q11 on this dataset. No live-model request was made.
The earlier QA results (`docs/qa-report.md`) apply to the 200-well sample dataset only and must not be read as results here.

## 4. Session isolation and multi-turn behaviour

Q7, Q8 and Q9 turn-by-turn results: **not run.** Session isolation and private preferences were verified earlier only through
the sample-dataset tests, with the model scripted or on the sample questions (see `docs/qa-report.md`).

## 5. Demo and write-up assets

No demo video or two-page write-up exists in the repository. The existing README and `docs/architecture.md` are the only prose assets.

## 6. Environment notes

* The API server started earlier for the UI preview (port 8000, default data = the 200-well sample) was still running when this
  work began. It was not stopped and it is **not** serving the official test data. To serve it:
  `COPILOT_DATA_DIR=data/official-test COPILOT_TODAY=2027-01-15 COPILOT_DATASET_DIR=<test-data path> python -m copilot.api`
  (use a different `COPILOT_PORT`; the same folder also gets its own `state.db`).
* The ingest of 600 wells took about 10 minutes on this machine.

## 7. Live attempts at 17:00-17:09 IST (real `gemma-4-26b-a4b-it`, official data, app serving 600 wells, today = 2027-01-15)

Runner: `scripts/run_official_questions.py` (exact question wording, users and session boundaries; results in `data/official_eval_*.json`, git-ignored).
The hosted model returned repeated rate-limit errors (40-60 s retries), so progress was slow; the runner was stopped at 17:09.

| Q | Status | Kind | What happened |
|---|---|---|---|
| 2 Most oil produced in 2026 | **FAIL** (no answer to the question) | live Gemma | 25 s. The copilot said it has no tool for per-well oil production volumes. Honest, but the question is unanswered; whether the dataset holds production volumes was not checked |
| 11 Hotter region | **FAIL** (likely wrong) | live Gemma | 304 s. Answered that only 2 of 7 areas have temperature data and so no region can be named. This is suspicious: ingest shows 125,198 inspections across all 7 areas, so the cause is probably a tool row limit or filter, not missing data. Not investigated. Treat the answer as unreliable |
| 10 Waiting on parts/crews/contractors/weather | **BLOCKED** | live Gemma | the client timed out after 420 s (rate limiting). No answer recorded |
| 1, 3, 4, 5, 6, 7 (2 turns), 8 (5 turns), 9 (6 turns) | **NOT RUN** | none | |

Not verified for Q8/Q9 on this dataset: private preferences, claim handling, session isolation, recall list. The earlier 18-question results
(`docs/qa-report.md`) are for the 200-well sample and are not repeated here.
