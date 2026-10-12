# FactLock

**Monthly fund commentary, drafted by Gemini in English and Japanese — where the model is not allowed to type a single number.**

Built for the Google AI Builder Cup 2026 (Build in JAPAC), theme: *Intelligent Risk, Fraud & Financial Experiences*.
The live demo runs on Cloud Run; its address is given in the submission.

---

## The problem

Every month, an asset manager publishes commentary for each of its funds: what markets did, how the fund
performed, what is in the portfolio. In Japan alone this is a few thousand documents a month, written to a
deadline, in two languages, and then checked line by line because a single wrong figure is a disclosure
incident, not a typo.

Large language models write this kind of text well. They also produce numbers that look right and are not,
and reasons for market moves that nobody can source. That is why most reporting teams cannot use them for
anything that leaves the building.

## What FactLock does

FactLock separates the two jobs. **Arithmetic is done by code. Language is done by Gemini. Nothing the model
writes reaches the reader until it has been checked.**

The writer agent receives a fact sheet and must refer to every number by ID:

```
"In {{report.month|name}}, {{etf.SPY.label|name}} {{etf.SPY.ret_1m|verb}}."
        ↓ rendered from data, not from the model
"In August 2026, U.S. large-cap equities (SPY) rose 2.71%."
```

Because the `verb` format renders the direction word from the sign of the value, the model cannot write
"fell" about a month that rose. Because no digit may appear in the text the model authors, it cannot invent
a figure. Everything else — causes, comparisons, tone — is caught by the checks below or sent to a human.

The reader sees each number as a locked token; selecting it shows the value, the as-of date, the source,
the formula and the inputs it was computed from.

## How it works

A multi-agent pipeline on the [Agent Development Kit](https://google.github.io/adk-docs/) (ADK):

```
fact_builder   public data snapshot → 72 facts, each with source, as-of date and derivation (no model)
     │
     ├─► revise loop, up to 3 rounds
     │     writer     Gemini   drafts EN + JA, placeholders only
     │     checker    rules    renders the numbers, runs 8 groups of checks (no model)
     │     reviewer   Gemini   looks for unsupported causes, comparisons, EN/JA drift
     │     gate       rules    passes the draft, or sends the exact failures back to the writer
     │
finalizer      verified, or handed to a human with the open issues listed
     │
ledger         Firestore: every run, its verdict, who approved it and when, and the latest verified draft
```

**Where each Google Cloud service sits.** *Gemini* (through ADK) writes and reviews. *Cloud Run* runs the
pipeline, the rule checks and the web app, with its own service-account credentials. *Firestore* is the
audit ledger: every drafting run is recorded with its verdict, the approval is written next to it, the
latest verified draft is kept there so it survives restarts, and the usage limits are counted there so they
hold across instances; when Gemini is too busy to answer (429) the app serves the latest verified draft
from the ledger and says so on the page and in the record. *Firebase Authentication* (anonymous sign-in)
gives every visitor an identity without a password, so the demo is public yet each visitor's drafts are
limited and attributable.

The reviewer is skipped whenever the rule checks already failed — there is no point paying for a second
opinion on a draft that is going back anyway.

### What is checked, deterministically

| Check | What it rejects |
|---|---|
| Fact resolution | A placeholder that names a fact or format that does not exist |
| No typed numbers | Any digit, spelled-out count, or kanji numeral in the text the model wrote |
| Direction agreement | "fell" in a sentence whose facts are all positive, and the reverse |
| Bilingual parity | English and Japanese citing different facts in the same sentence |
| As-of disclosure | Holdings dated after month end used without stating that date in the same sentence |
| Claims policy | Guarantees, recommendations, certainty about the future, "this indicator leads markets by N months" |
| Cautions (warnings) | Unhedged forecasts, dramatic wording, causal language — flagged for a human, not blocked |
| Required content | A performance section that never states the fund's return |

Before any of that, the fact sheet itself is checked: target weights add to 100%, contributions add to the
fund return, and each issuer-published monthly return is reconciled against the issuer's own NAV history
(for a monthly distributor the gap is the distribution, and is reported as such).

## Data

Only public data, taken directly from the original publisher:

| Source | Used for | Terms |
|---|---|---|
| OECD, Composite Leading Indicators (amplitude adjusted) | Business-cycle level, monthly change, phase, run length | CC BY 4.0 |
| OECD, Financial market indicators | Long-term government bond yields, monthly averages | CC BY 4.0 |
| State Street Global Advisors, SPDR ETF product data | Month-end total returns of each component | Public fund disclosure; issuer's website terms |
| State Street Global Advisors, SPDR ETF NAV history | Independent cross-check of those returns | Public fund disclosure |
| State Street Global Advisors, SPDR ETF daily holdings | Largest look-through holdings | Public fund disclosure |

OECD (2026), *Composite leading indicators* and *Financial market indicators*, OECD Data Explorer.
The repository stores only the small set of derived values FactLock needs, never the original files.

The fund shown, **Demo Global Balanced Fund**, is hypothetical: a fixed allocation to four public ETFs,
rebalanced monthly, gross of fees. It is a demonstration, not a product, and nothing here is investment advice.

## What we measured

Measured on 24–25 September 2026 with `gemini-3.8-flash` on Google Cloud (global endpoint, temperature 0.2),
against a fault catalogue fixed before the run: 65 faults (57 fatal, 8 that need review) and 40 legitimate
sentences, with the catalogue's checksum recorded before any model call.

| Measure | Result |
|---|---|
| Fatal injected errors stopped before a reader | **57 / 57** (95% CI 94–100%): 24 by the rules, 33 by the reviewer |
| Injected errors that need review, stopped | 8 / 8, all by the reviewer |
| Legitimate sentences wrongly blocked | **1 / 40**: the name "S&P 500", whose digits the writer may not type, by design. The reviewer blocked none |
| Numbers in the text matching an independent recomputation | **1,860 / 1,860** across 39 drafts |
| Numbers traceable to a source, formula and as-of date | 2,066 / 2,066 |
| First drafts that passed | 38 / 39 (the one send-back: English and Japanese citing different facts, caught by the rules); 39 / 39 in the end |
| Japanese drafts with broken grammar | 0 / 39 |
| Cost per draft (median) | $0.0496 at Gemini's introductory price; $0.0993 at the standard price |
| Time per draft (median) | 98.5 s, including waits for retries while the endpoint was busy; 30.5 s at best |

**What this does and does not show.** The same model, asked to write the same commentary without FactLock —
given the fact sheet, or given only the published inputs and left to compute the figures itself — made no
numeric errors either (0 / 40 drafts in each case). At this scale, locking the numbers is not what removes
errors. What FactLock adds is proof: every number is recomputed and traced before a reader sees it, a reviewer
catches errors of meaning that rules cannot, and the printed record shows how each draft was checked, so a
person approves the draft instead of re-checking it. One of the 40 planned FactLock drafts was not obtained
because the endpoint was busy (429); it was not retried. `scripts/measure_report.py` rebuilds these figures
from the run records, which are not included in this repository.

## Run it locally

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests -q                 # 238 tests, no network, no model calls
                                           # (10 of them check web/format.js and need Node.js; they are
                                           #  skipped with a message when Node.js is not installed)
FACTLOCK_LLM_MODE=demo python3 -m uvicorn app.main:app --port 8080
```

`demo` mode runs the whole pipeline with a scripted writer instead of Gemini, so the checking machinery can
be demonstrated offline. Choose *Show a correction* in the UI to watch a flawed first draft being rejected
and rewritten.

To rebuild the public-data snapshot for a month:

```bash
python3 -m app.data.snapshot --month 2026-08
```

To reproduce the measurements (every figure recomputed independently, provenance of every figure, fault
injection against the rule checks, and the data checks):

```bash
python3 scripts/measure_offline.py        # no network, no model calls
python3 scripts/measure_publisher.py      # downloads the publishers' files again and compares
python3 scripts/measure_batch.py --n 40   # with Gemini: 40 drafts, 40 ordinary-use drafts (with the fact sheet, and
                                          # with published inputs only), the reviewer on every fault case;
                                          # resumable; stops at a cost cap (--cap-usd) or after --max-failures
                                          # failed calls in a row (default 6, 30 s pause after each failure)
python3 scripts/measure_report.py         # summarises the batch; no model calls
```

## Deploy to Cloud Run

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
    artifactregistry.googleapis.com aiplatform.googleapis.com

python3 scripts/check_gemini.py            # confirms the model name this project can call
python3 scripts/check_firestore.py         # confirms the ledger: writes a probe run, reads it back, cleans up
python3 scripts/run_once.py                # one full pipeline run, saved for review, with tokens and cost

firebase deploy --only firestore:rules     # deny-all rules: only the service writes the ledger

gcloud run deploy factlock --source . --region asia-northeast1 --allow-unauthenticated \
    --service-account factlock-run@PROJECT_ID.iam.gserviceaccount.com \
    --set-env-vars GOOGLE_CLOUD_PROJECT=PROJECT_ID,FACTLOCK_LLM_MODE=gemini,FACTLOCK_MODEL=gemini-3.8-flash,FACTLOCK_LOCATION=global,FACTLOCK_LEDGER=firestore,FACTLOCK_AUTH=firebase,FACTLOCK_FIREBASE_API_KEY=...,FACTLOCK_FIREBASE_AUTH_DOMAIN=PROJECT_ID.firebaseapp.com,FACTLOCK_FIREBASE_APP_ID=... \
    --max-instances 2 --memory 1Gi --timeout 300
```

The service account needs `roles/aiplatform.user` (Gemini) and `roles/datastore.user` (the Firestore
ledger). No API key is used for Gemini or Firestore: the container calls them with its own service-account
credentials. The Firebase web API key in the settings is a public identifier for the browser SDK, not a
secret; anonymous sign-in must be enabled in Firebase Authentication and the service's domain added to its
authorized domains.

| Setting | Default | Purpose |
|---|---|---|
| `FACTLOCK_LLM_MODE` | `gemini` when a project is detected | `gemini` or `demo` |
| `FACTLOCK_MODEL` | `gemini-3.8-flash` | Writer and reviewer model |
| `FACTLOCK_LOCATION` | `global` | Model location |
| `FACTLOCK_FALLBACK_LOCATION` | `none` | A second location for the same model, used when the first refuses a request with 429 (quota). Off by default: on 2026-09-24 gemini-3.8-flash was served from `global` only. The record of each draft names the location that answered |
| `FACTLOCK_MAX_ITERATIONS` | `3` | Rounds before a draft goes to a human |
| `FACTLOCK_RUNS_PER_HOUR` | `30` | Cap on new live drafts per hour, for all visitors together (counted on the ledger) |
| `FACTLOCK_RUNS_PER_HOUR_PER_USER` | `5` | Cap per visitor (needs `FACTLOCK_AUTH=firebase`) |
| `FACTLOCK_LEDGER` | `memory` | `firestore` for the audit ledger, `memory` for local previews |
| `FACTLOCK_AUTH` | `passphrase` if a passphrase is set, else `none` | `firebase` (anonymous sign-in), `passphrase`, or `none` |
| `FACTLOCK_FIREBASE_API_KEY`, `_AUTH_DOMAIN`, `_APP_ID` | not set | The Firebase web app settings the page needs for `firebase` auth |
| `FACTLOCK_PASSPHRASE` | not set | When set (and `FACTLOCK_AUTH` is not `firebase`), the whole app sits behind one shared passphrase |

### Who may draft

For the public demo, `FACTLOCK_AUTH=firebase`: the page signs each visitor in anonymously with Firebase
Authentication and sends the ID token with every API call; the server verifies it with the Firebase Admin
SDK. No password is needed, yet every draft is attributed to a visitor id on the ledger, each visitor may
start at most five live drafts an hour, and the demo as a whole thirty. When a limit is reached, or when
Gemini is too busy, the latest verified draft is still shown.

### Passphrase

For a private preview, the demo can instead be kept behind a single shared passphrase. Set
`FACTLOCK_PASSPHRASE` (ideally from Secret Manager) and every page and API call requires it; the login page,
its stylesheet and the health endpoint stay open. The cookie carries its issue time and an HMAC keyed by the
passphrase, so it cannot be forged and the server itself refuses it after 12 hours. It is HttpOnly, SameSite=Lax
and, on HTTPS or Cloud Run, Secure. **Sign out** (top right) removes it from that browser; changing the
passphrase signs everyone out at once. Repeated wrong guesses are blocked for ten minutes. Every response
carries `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff`, and
pages and API answers are sent with `Cache-Control: no-store`. Leave the variable unset and the app is open.

## Design notes and limits

- **Why placeholders instead of asking the model to be careful.** A rule that can be checked by a parser is
  worth more than an instruction that is usually followed. The placeholder design turns "do not hallucinate
  numbers" into a property the system can prove for every sentence it publishes.
- **Why a reviewer model as well as rules.** Rules cannot see an invented cause or a comparison that the
  values do not support. That is the part of the job a model is genuinely better at, so it is the only part
  the second model is asked to do.
- **Failure is visible.** If three rounds do not produce a clean draft, the app says so and shows what is
  still wrong, rather than publishing the closest attempt.
- **Known limits.** One hypothetical fund and one month are shipped; the fact sheet is rebuilt from a
  snapshot rather than a live warehouse; the ledger keeps runs and approvals but there is no screen yet to
  browse past runs; ADK's `SequentialAgent`/`LoopAgent` are pinned because the newer graph `Workflow` API
  landed mid-project.

## Roadmap

A browsable history of runs and approvals from the ledger, and its export to BigQuery; migration to the ADK `Workflow` graph API; more
funds and months; a check pack per asset class; and a client-side editor that keeps the locks intact when a
human edits a sentence.

---

Copyright © 2026 SEMANTICBASE Co., Ltd. All rights reserved.
Built during the AI Builder Cup 2026 timeline; no pre-existing code or assets were reused.
