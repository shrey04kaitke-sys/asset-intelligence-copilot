# Asset Intelligence Copilot

An evidence-first assistant and workspace for people who look after oil wells. Operators ask plain-English questions
("Which wells in Northeast Block 8 are not producing oil right now, and why?"), browse the fleet, open any well, and inspect
the records behind every statement. Built for the FlytBase *Asset Intelligence Copilot* hackathon challenge.

> **Scope, honestly.** This is decision support over a *synthetic dataset* (a snapshot, not a live feed). There is **no** drone,
> sensor, work-order or control-system integration, the copilot cannot operate any equipment, and nothing here is production
> ready, certified, or legal/regulatory advice. The SOPs and manuals in the challenge data are fictional.

## What it does

| | |
|---|---|
| **Answers grounded in data** | Gemma 4 chooses among 22 validated backend tools; every number, date, threshold and deadline is computed by deterministic code from the records, manuals and SOPs. The model writes the explanation and cites evidence. |
| **Evidence you can open** | Every citation is a real record (inspection, repair, report, regulator event, note, rule passage, correction). The UI opens it with its source file and line. Citations that don't match retrieved evidence are removed and reported. |
| **Honest about conflicts and gaps** | Stale recorded status vs the latest inspection, an inspector's note vs a correction, disagreeing users, missing records ("no recorded reason"), and rules that documents disagree about are surfaced, not silently resolved. |
| **Corrections and memory** | Users correct raw readings, AI-estimated spill volumes, or team rules. The original is kept; author, reason, scope and time are audited; the change applies for every user at its stated scope only. Private memory and private claims are visible only to their owner. |
| **Visuals** | Status card, table, line chart, bar chart, map, heatmap (plus timeline and donut). Specs are built and validated by the backend; the browser only draws them, each with a text alternative and a data table. |
| **Workspace UI** | Fleet overview, asset detail, investigation chat, and an evidence & knowledge area (corrections, claims, rules, audit trail). |

## Architecture in one picture

```
 dataset/ (read-only)                         ┌───────────── browser UI (web/, no build step) ─────────────┐
   fleet/ manuals, SOPs, areas, users         │ overview · assets · asset detail · investigate · evidence  │
   assets/<id>/ inspections, repairs,         └──────────────────────────────┬─────────────────────────────┘
     reports, regulator events, notes                                        │ JSON over HTTP
        │ ingest (hash-incremental, validated)                     ┌─────────▼──────────┐
        ▼                                                          │ FastAPI (api.py)   │  /ask /fleet /assets /evidence /feedback/*
  source.db (derived, rebuildable) ──────────────┐                 └───┬────────────┬───┘
   records · FTS5 documents · rules w/ provenance│                     │            │
                                                 ▼                     ▼            ▼
  state.db (persistent) ───────────►  domain.py  (deterministic)   views.py     orchestrator.py ── Gemma 4 (google-genai)
   corrections · claims · memory ·      status, downtime, spills,    read        bounded tool loop  │ function calling
   conversations · audit log            SR/RC checks, RCA, costs     models      cites + validates  │ pacing / retries
                                                 ▲                                      │
                                                 └──── tools.py registry (allow-list, schemas, limits) ◄─┘
```

See [docs/architecture.md](docs/architecture.md) for the data flow, trust boundaries and design decisions, and
[docs/qa-report.md](docs/qa-report.md) for what was actually verified (and what was not).

## Setup (tested on Windows 11 with Python 3.13; `pyproject.toml` declares 3.11+, which has not been tested)

```powershell
git clone <this repository>
cd FlytBaseAHC
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# The challenge dataset is NOT part of this repository. Clone it next to the code:
git clone https://github.com/FlytBaseAILabs/flytbase-ahc-swe-asset-intelligence-dataset.git dataset
```

### Model access (Gemma 4 through Google AI Studio)

The key is never written to a project file. Run this **in your own terminal** (input is hidden; the key is stored in the
Windows Credential Manager and the script never prints it or accepts it as an argument):

```powershell
python scripts/setup_gemini_key.py            # store the key
python scripts/setup_gemini_key.py --status   # is it stored? (never shows the key)
python scripts/setup_gemini_key.py --delete   # remove it
```

If you cannot use the credential store, set `GEMINI_API_KEY` in the environment of the process that starts the server. Never
paste a key into a chat, a source file, or a commit. The model defaults to `gemma-4-26b-a4b-it`
(`gemma-4-31b-it` is the other documented hosted id; **only `gemma-4-26b-a4b-it` has been tested here**). Only `gemma-*` ids are accepted and there is **no fallback** to any other model.

### Load the data and run

```powershell
python -m copilot.ingest                      # parses dataset/sample-data into data/source.db (incremental on re-runs)
python -m copilot.api                         # http://127.0.0.1:8000  (docs at /docs)
```

`python -m copilot.api` also ingests automatically if the database is empty. Point `COPILOT_DATASET_DIR` (or
`python -m copilot.ingest <dir>`) at any dataset in the same format, for example one made by
`python scripts/make_variant_dataset.py <dir>`.

The server binds to `127.0.0.1`. The user picker in the header selects *who is asking*; it is **not authentication**, so do not
expose the server to a network without real authentication in front of it.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `COPILOT_DATASET_DIR` | `dataset/sample-data` | dataset folder (`fleet/`, `assets/`) |
| `COPILOT_DATA_DIR` | `data` | where `source.db` (derived) and `state.db` (persistent) live |
| `COPILOT_TODAY` | day after the newest inspection | the "today" used for windows and deadlines |
| `COPILOT_MODEL` | `gemma-4-26b-a4b-it` | hosted Gemma 4 model id |
| `COPILOT_LLM_MIN_INTERVAL_S` / `_MAX_RETRIES` / `_MAX_RETRY_WAIT_S` | `1.5` / `4` / `75` | request pacing and bounded retry for 429/5xx/timeouts |
| `COPILOT_HOST` / `COPILOT_PORT` | `127.0.0.1` / `8000` | server address |

## Tests

```powershell
python -m pytest                              # unit, integration, API and browser tests (~3 min; the browser tests need Edge or Chrome)
$env:COPILOT_LIVE = "1"; python -m pytest tests/test_llm_live.py   # opt-in: calls the real Gemma 4 API
python scripts/run_live_eval.py --chains read,memory,fix,rule,claims,dispute   # the example questions through the real model
```

* **Mocked** tests replace *only the language model* with a scripted provider (tests are labelled as such in their docstrings).
  They prove application logic, never that Gemma behaves well.
* **Browser** tests drive the real UI in Edge/Chrome against the real backend and data (model scripted).
* **Live** tests and the evaluation runner call the real hosted model; results depend on provider availability and quota.
* The generalisation test runs the whole system on a second, synthetic dataset with a different shape and an independent answer key.

## What you can ask

Single-well status; lists with reasons; trends; spill counts and totals (corrected detections excluded); downtime over any window;
rankings and comparisons; regulatory compliance of one well; spills that missed the SOP containment time (a fact found only in
repair reports); root-cause-analysis candidates under the manual's rule or a team rule; repair costs; heatmaps of area × month;
planned downtime others reported; and statements to record ("The 15 Oct reading is wrong, the crew re-measured 41.0 °C").
Planning or past-conversation questions ("the wells we discussed yesterday") use private, per-user memory.

## Known limitations

* No live drone/sensor/control integration; the dataset is a synthetic snapshot. Images are referenced but not supplied.
* The hosted free-tier model is slow and unreliable at times (HTTP 500/429, long reasoning). Failures are shown, never replaced
  with a made-up answer. Quality was measured on the example questions only (see the QA report); it is not a guarantee.
* Rule extraction is pattern-based. A manual worded differently from the supported patterns yields *no rule* (status "unknown") and
  an ingestion warning, not a guess.
* The browser map is a schematic position plot (no basemap). Requests are processed one at a time. No authentication.
* Ollama/local models are designed for (provider abstraction) but **not implemented**.
* The video and the two-page challenge write-up are not part of this repository.

## License

MIT (see `LICENSE`). Third-party libraries keep their own licenses, all permissive: FastAPI, Pydantic, keyring, pytest (MIT);
httpx, uvicorn, Starlette (BSD-3-Clause); google-genai, Playwright (Apache-2.0). The challenge dataset is a separate MIT-licensed
repository and is not redistributed here.
