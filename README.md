# San Francisco SWE Job Tracker

A job tracker for SWE jobs near San Francisco. Because I miss her.

This is an unattended GitHub Actions tracker for public software-engineering internships and new-graduate jobs whose displayed location is in the San Francisco Bay Area.

It combines public listings from three providers:

| Provider | Feed | Roles used |
|---|---|---|
| [SpeedyApply](https://github.com/speedyapply/2027-SWE-College-Jobs) | `README.md` and `NEW_GRAD_USA.md` on `main` | USA internships and new-grad roles in the existing `FAANG+`, `Quant`, and `Other` tables |
| [ApplyGuy internships](https://github.com/ApplyGuy/2027-Internships) | `data/internships.json` on `main` | `Software Engineering` internships |
| [ApplyGuy new grads](https://github.com/ApplyGuy/2027-New-Grad-Jobs) | `data/new-grad-jobs.json` on `main` | SWE `New Grad` and `Entry Level` records from that dedicated feed |
| [Simplify](https://github.com/SimplifyJobs/Summer2027-Internships) | active `README.md` on `dev` | the active Software Engineering Internship Roles table only |

The tracker fetches listing feeds only. It does not scrape employer career sites, use private provider data, or need an external database.

## Nearby-city location rule

The tracker uses a deterministic, curated San Francisco Bay Area allowlist for cities that are roughly within a one-hour drive of San Francisco in favorable traffic. It matches only source-provided location text and leaves that displayed location unchanged in the dashboard; it does not call a routing API or make a live traffic-time claim.

The policy includes:

- San Francisco and Peninsula cities, including `SF`, `S.F.`, South San Francisco, San Mateo, Redwood City, Palo Alto, and Mountain View.
- South Bay cities including Sunnyvale, Cupertino, Santa Clara, San Jose, Milpitas, Campbell, Los Gatos, and Saratoga.
- East Bay cities including Oakland, Berkeley, Fremont, Hayward, Union City, Pleasanton, San Ramon, Walnut Creek, and Concord.
- The nearest North Bay cities including Sausalito, Mill Valley, San Rafael, Novato, Vallejo, and Benicia.
- State-qualified regional forms such as `Bay Area, CA` and `Silicon Valley, California`, plus unambiguous `San Francisco Bay Area` forms.

City aliases require a California spelling, so `SF, NY`, `San Jose, Costa Rica`, and `Fremont, NE` do not match. The deliberately bounded scope also excludes farther locations such as Santa Cruz, Morgan Hill, Gilroy, and Livermore. The editable complete allowlist and alias policy are in [src/config.py](src/config.py).

## How collection and deduplication work

1. Each provider has its own adapter and is fetched independently. A failed fetch or parser/schema validation error is reported as a source failure; it is never treated as an empty feed.
2. The SpeedyApply adapter preserves its category, salary, source table, and application URL. The ApplyGuy adapter prefers the direct `listingUrl`; its ApplyGuy `url` wrapper is retained only as provenance. The Simplify adapter reads only the active SWE table, carries `↳` company continuations forward, removes presentation markers from company names, handles multi-location cells, and prefers an employer Apply link over a `simplify.jobs` link.
3. All matching observations are normalized into a common record and then reconciled into canonical employer requisitions. A canonical job can retain observations from SpeedyApply, ApplyGuy, and Simplify while producing only one dashboard row and at most one alert.
4. Canonical identity uses a stable ATS requisition identity when one is safely known (including common Ashby, Greenhouse, Lever, Workday, Rippling, TikTok, and Amazon forms). Host case, fragments, safe trailing slashes, and tracking parameters are normalized conservatively; Ashby job URLs and their `/application?embed=true` presentation variants collapse. Distinct known requisition IDs remain distinct.
5. When no direct/stable employer URL exists, an exact, conservative fingerprint of company, title, type, location, and season is used only when it cannot conflict with direct requisitions. Similar-looking jobs are never fuzzy-merged.

The generated [jobs.md](jobs.md) has one row per canonical job, a direct application link, and visible source names. It orders active jobs before historical jobs deterministically.

## State, baselines, and alerts

The permanent state in [data/seen_jobs.json](data/seen_jobs.json) uses schema version 2. It keeps canonical jobs, per-source membership, provenance URLs, lifecycle timestamps, source initialization state, and retry-safe pending notification batches. [data/current_jobs.json](data/current_jobs.json) is the active canonical snapshot.

Existing schema-version-1 SpeedyApply state is migrated in memory on the next successful normal run. Its original `first_seen` and lifecycle data are preserved, prior notification batches keep their legacy URL markers, and the original SpeedyApply sources remain initialized. This prevents a migration or a newly added provider from flooding you with old-job alerts.

The first successful normal run establishes a silent baseline. Each newly added source is also silently onboarded on its first successful run. A job that was already known and later appears from another source is updated with that source provenance but does not alert. A job is active while at least one successfully checked source still reports it; a source failure leaves its previous membership unknown rather than incorrectly marking the job inactive.

Only new canonical jobs after their relevant source has been initialized enter a pending notification batch. The workflow persists state before delivery. Each batch has a deterministic hidden marker; retries search open and closed GitHub Issues for that marker before creating an Issue, making delivery idempotent even after an interrupted run. A single Issue lists every provider that reported each canonical job. The optional `new-job` label is best-effort and cannot block Issue creation.

## Application Question Enrichment

For a genuinely new canonical job, the tracker first creates its normal GitHub Issue and only then attempts best-effort, read-only inspection of the public employer application page. When questions can be found, the tracker updates that same Issue with an application-preparation section. A scan failure never prevents the original job alert, and one canonical job is never scanned once per listing provider; a bounded retry is reserved for a transient failed scan.

The scanner uses the least brittle public mechanism available:

| Application provider | Strategy | Expected completeness |
|---|---|---|
| Greenhouse | Public structured job-board data | High when the board exposes the form definition |
| Ashby | Public rendered-form inspection | Partial or high, depending on what the public form exposes |
| Lever | Public rendered-form inspection | Partial or high, depending on what the public form exposes |
| Workday | Conservative public-page inspection | Often partial |
| Other providers | Static-form parsing, then a conservative browser fallback | Best effort |

Each result has an explicit status: `complete` means a structured or otherwise authoritative form definition was available; `partial` means only public, currently visible questions were safely found; `unsupported` means there is no safe inspector yet; `unavailable` covers closed, protected, login-required, or inaccessible pages; and `failed` records an unexpected technical error. An empty result is never treated as a complete scan.

Question definitions are persisted in [data/application_questions.json](data/application_questions.json), keyed by canonical job ID. They include labels, required flags, field types, options, categories, provider, application URL, and scan status—not answers or personal data. The file is deterministic, so unchanged scans do not create timestamp-only commits.

### Read-only safety boundary

Application Question Enrichment exists only to help prepare for an application. The tracker never submits applications, answers questions, uploads files, enters personal information, creates candidate accounts, authenticates, accepts agreements, bypasses authentication, or solves CAPTCHAs. Browser inspection may open a public page, follow an obvious public application link, scroll, expand a clearly non-destructive control, and read visible form controls. It never selects answers, fills fields, or clicks a control whose effect might change an application.

Some public applications are multi-step, conditional, JavaScript-heavy, protected by CAPTCHA, or require login before later questions appear. The scanner does not fabricate data or credentials to move past those boundaries, so such results are intentionally `partial` or `unavailable` rather than claimed complete. It also does not scan historical jobs: the initial tracker/source baseline remains silent, and only jobs newly discovered after their source is initialized are eligible for enrichment. Set `APPLICATION_SCAN_ENABLED=false` to disable enrichment while leaving job discovery and original notifications running normally.

## Repository files

| Path | Purpose |
|---|---|
| `src/adapters.py` | ApplyGuy JSON and Simplify active-table adapters |
| `src/parser.py` | Existing SpeedyApply Markdown adapter and parser dispatch |
| `src/canonical.py` | URL normalization, requisition identity, and cross-source aggregation |
| `src/tracker.py` | Nearby-city filtering and source-aware lifecycle reconciliation |
| `src/storage.py` | Validated atomic storage and v1-to-v2 state migration |
| `src/notifier.py` | Canonical GitHub Issue formatting and idempotent delivery |
| `src/application_inspection/` | Read-only provider detection, question extraction, normalization, and Issue rendering |
| `data/seen_jobs.json` | Permanent canonical history and notification batches |
| `data/current_jobs.json` | Active canonical snapshot |
| `data/application_questions.json` | Canonical-job application-question scan results; no answers or personal data |
| `jobs.md` | Generated dashboard; do not edit manually |
| `tests/` | Offline adapter, location, URL, aggregation, migration, renderer, notifier, and orchestration tests |
| `.github/workflows/check-jobs.yml` | Hourly and manually triggered automation |

State output is deterministic: ordinary unchanged polls do not advance `last_seen` or create meaningless diffs. Timestamps are UTC ISO 8601.

## Local use

Requires Python 3.10+.

```bash
python -m pip install -r requirements.txt
python -m pytest
```

Safely fetch, parse, filter, aggregate, and report every provider without changing files or creating a notification:

```bash
python -m src.check_jobs --dry-run
```

Run collection normally (this updates `data/` and `jobs.md`, but does not itself call GitHub):

```bash
python -m src.check_jobs
```

Explicitly establish or refresh a silent baseline:

```bash
python -m src.check_jobs --initialize
```

Deliver only persisted pending alerts. This needs `GITHUB_TOKEN` with Issues write permission and uses `GITHUB_REPOSITORY` when supplied:

```bash
python -m src.check_jobs --deliver-pending
```

For a local end-to-end notification test, set those environment variables and run:

```bash
python -m src.check_jobs --send-test-notification
```

That command creates a fresh clearly marked test Issue on every invocation. It does not fetch jobs or modify `data/` or `jobs.md`. Use `--log-level DEBUG` for source diagnostics. If every source fails or persisted state is invalid, the command exits nonzero before replacing generated state; an individual provider failure is retained as unknown while healthy providers continue safely.

## GitHub Actions and phone/email notifications

`Check San Francisco Jobs` runs at the top of every hour and can also be started from **Actions -> Check San Francisco Jobs -> Run workflow**. Scheduled production runs operate from `main`; it has `contents: write` and `issues: write` permissions, no `push` trigger, and a concurrency group so two runs cannot mutate state at once.

The collection job runs tests, updates state/dashboard only if files changed, pushes that state, then delivers pending GitHub Issue alerts. For new jobs, Application Question Enrichment happens only after the original alert exists and records its scan state during the notification-state commit. If an alert or scan delivery fails, the original alert remains intact; notification retries remain visible and safe on a later run. Scheduled and deliberate production runs enable application scanning. Manual dry runs disable it, so they remain read-only and never create or update Issues.

To test your GitHub Mobile or email notifications safely:

1. Enable repository notifications in GitHub and enable GitHub Mobile and/or email notifications in your account settings.
2. Open **Actions -> Check San Francisco Jobs -> Run workflow**.
3. Check `send_test_notification` and run it.
4. The normal collection job is skipped; the test job creates one fresh clearly marked San Francisco Bay Area tracker test Issue.

Every explicit test run creates a new Issue, which gives GitHub a fresh event to deliver by email or mobile. These test Issues are not tracked in `data/seen_jobs.json` or `data/current_jobs.json`; production job-alert batches remain independently idempotent. A successful test job and a visible test Issue confirm the repository can create Issues; delivery to a phone or inbox is then governed by your GitHub notification preferences.

After merging to `main`, enable GitHub Actions if necessary. If repository policy prevents the default `GITHUB_TOKEN` from writing, allow workflow read/write permissions for the repository. No personal access token, email service, or third-party notification integration is required.

## Configuration and limits

Edit [src/config.py](src/config.py) to change source feeds, the nearby-city allowlist and aliases, request behavior, SpeedyApply category markers, or the application-scan policy. `APPLICATION_SCAN_ENABLED`, `APPLICATION_SCAN_ONLY_NEW_JOBS`, `APPLICATION_SCAN_HTTP_TIMEOUT_SECONDS`, `APPLICATION_SCAN_BROWSER_TIMEOUT_SECONDS`, and `APPLICATION_SCAN_MAX_ATTEMPTS` can be set through environment variables. `APPLICATION_SCAN_ONLY_NEW_JOBS` defaults to `true`; leave it enabled to avoid a deliberate backfill of already-alerted jobs. Keep the matcher deliberately bounded unless you intentionally want a different product scope. The tracker relies on the feeds' displayed locations and does not expand locations by visiting employer pages or use live routing/geocoding. Application scans use only public pages and store no credentials, personal application notes, or application answers.
