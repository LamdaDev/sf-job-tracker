# San Francisco Bay Area SWE Job Tracker

A job tracker for SWE jobs near San Francisco. Because I miss her.

This is an unattended GitHub Actions tracker I made for public USA software-engineering roles in [SpeedyApply's 2027 college-jobs repository](https://github.com/speedyapply/2027-SWE-College-Jobs). It watches both:

- USA internships from `README.md`
- USA new-graduate roles from `NEW_GRAD_USA.md`

For each file it parses the upstream `FAANG+`, `Quant`, and `Other` marker-delimited tables. A role matches when its displayed Location normalizes to an explicit **San Francisco Bay Area** city or regional alias. This includes `SF, CA`, `S.F., California`, `San Francisco, CA +1`, and cities such as San Mateo, Palo Alto, San Jose, Santa Clara, Berkeley, Fremont, and Sunnyvale.

The scope is a deterministic, editable approximation of places roughly one hour from San Francisco by car in favorable traffic. It cannot promise a live one-hour commute: traffic, starting address, route, and time of day can change the result. The complete allowlist is in [src/config.py](src/config.py); plain `Remote`, all of California, and unlisted outer cities such as Santa Cruz, Morgan Hill, Gilroy, Livermore, Napa, and Petaluma do not match.

## How it works

1. The tracker resolves the upstream `main` branch to one commit SHA, then downloads both public USA Markdown files at that exact revision.
2. It validates the three expected table sections and maps columns from each table header. `Other` tables can omit Salary.
3. It extracts the company text and the apply link from the `Posting` cell's Apply anchor—not the company-homepage link.
4. It normalizes the parsed Location field for comparison only, matches the configured Bay Area scope, then deduplicates only exact application URLs. The displayed source location remains unchanged.
5. It records permanent URL-keyed history, active/inactive state, and a deterministic current snapshot.
6. It regenerates [jobs.md](jobs.md), with active roles first and historical roles below.

The application URL is the identity. Similar company/title pairs with different URLs remain separate jobs; a role that disappears and later returns at the same URL is reactivated without a second alert.

## First-run baseline and alerts

The first successful normal run establishes a baseline: it saves all currently matching jobs but creates no GitHub Issue. Later unseen URLs create a persisted pending notification batch. This geographic expansion carries a new scope version, so the next successful run baselines its already-existing nearby roles once rather than sending one large alert. If you later change the allowlist, also bump `LOCATION_SCOPE_VERSION` in `src/config.py` to get the same protection.

The workflow commits that state before calling the GitHub Issues API. Each Issue contains a deterministic hidden batch marker, and retries search both open and closed Issues for that marker first. This prevents duplicate alerts if an Issue was created but the workflow was interrupted before recording success. Failed notifications stay pending for the next run; successful delivery records are committed first, then the workflow reports the remaining failure visibly.

When possible, the notification adds the optional `new-job` label. A missing label never prevents Issue creation.

## Repository files

| Path | Purpose |
|---|---|
| `src/` | Standard-library fetcher, parser, state tracker, renderer, notifier, and CLI |
| `data/seen_jobs.json` | Permanent URL-keyed history and notification batches |
| `data/current_jobs.json` | Latest successfully parsed matching jobs |
| `jobs.md` | Generated human-readable dashboard; do not edit manually |
| `tests/` | Offline parser, tracking, renderer, notifier, and orchestration tests |
| `.github/workflows/check-jobs.yml` | Scheduled/manual automation |

All timestamps are UTC ISO 8601. To avoid a meaningless commit every hour, `last_seen` changes only for a first observation, reactivation, or meaningful source metadata change. An inactive transition is separately recorded in `inactive_at`.

## Local use

Requires Python 3.10+.

```bash
python -m pip install -r requirements.txt
python -m pytest
```

Safely inspect live upstream parsing without changing local state or creating an Issue:

```bash
python -m src.check_jobs --dry-run
```

Run a normal local collection (it updates `data/` and `jobs.md`, but does not send notifications by itself):

```bash
python -m src.check_jobs
```

Explicitly refresh a baseline without alerting for previously unseen URLs:

```bash
python -m src.check_jobs --initialize
```

The notification delivery command is intentionally separate. It needs a GitHub token with Issues write permission and uses `GITHUB_REPOSITORY` when supplied:

```bash
python -m src.check_jobs --deliver-pending
```

Use `--log-level DEBUG` for more diagnostics. Fetch, structural-validation, and state-validation failures exit nonzero before the tracker writes a replacement state.

## GitHub Actions

`Check San Francisco Bay Area Jobs` runs at the top of every hour and also supports **Actions → Check San Francisco Bay Area Jobs → Run workflow**. It has only `contents: write` and `issues: write` permissions, has no `push` trigger, and uses a concurrency group so scheduled and manual runs cannot mutate state at the same time.

After merging this repository to `main`, enable GitHub Actions if it is disabled. If your organization/repository policy prevents the workflow's default `GITHUB_TOKEN` from writing, allow workflow read/write permissions for this repository; no personal access token or external service is required.

The workflow only commits when `data/seen_jobs.json`, `data/current_jobs.json`, or `jobs.md` has actually changed.

## Test your phone or email notification safely

After you enable GitHub notifications for this repository, you can test the exact Issue-creation path without waiting for a real job:

1. Open **Actions** → **Check San Francisco Bay Area Jobs** → **Run workflow**.
2. Check **send_test_notification**, then run the workflow.
3. The normal `check-jobs` job will be skipped. The `send-test-notification` job creates at most one Issue titled `🧪 TEST — San Francisco Bay Area job tracker notification`.

The test never fetches upstream jobs and never changes `data/seen_jobs.json`, `data/current_jobs.json`, or `jobs.md`. Its body states that it is a test, and it has a fixed hidden marker: rerunning it detects the original Issue (even if you closed it) instead of creating duplicates. If the job is green and the test Issue appears, GitHub successfully created the Issue; whether it reaches your phone or inbox then depends only on your GitHub notification settings.

## Configuration and limits

Edit [src/config.py](src/config.py) to change the Bay Area city scope, upstream repository/files, request behavior, or category marker pairs. The initial implementation intentionally reads only the two USA public Markdown files and never accesses SpeedyApply's private Supabase data or crawls individual employer sites.

The tracker filters only the location text SpeedyApply displays. It does not expand `+N` locations by visiting employer pages, and it never uses a live routing/geocoding service. This project stores no credentials or personal application notes; if you later add personal notes, remember that a public repository makes them public.
