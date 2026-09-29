# BE-1 — Salesforce Bulk API v2 Ingestion Service & UI

A local implementation of **Task 1 / BE-1**: a Salesforce Bulk API v2 client, 10+ object
extraction, MinIO landing, ClickHouse tables, a monitoring UI, retry handling, and API
authentication.

## Architecture

```text
Salesforce Bulk API v2 (real org or local mock, same OAuth2 refresh-token flow)
                 |
                 v
        FastAPI ingestion API  (X-API-Key / HMAC auth on every /api/v1 route)
                 |
        background ingestion worker (per-job thread, concurrency-limited,
        crash-recovered at startup)
          /       |          \
         v        v           v
   PostgreSQL   MinIO      ClickHouse
   job state    raw CSV    analytics tables
   + per-object  |              ^
   checkpoints   +--------------+  (loaded back from MinIO, not from memory)
                 |
                 +-------> dashboard
```

`app/services/schema.py` is the single source of truth: it defines, per object, the real
Salesforce API name, the SOQL query, the CSV→typed-row conversion and the ClickHouse DDL, so
those three can't drift out of sync with each other.

## What's implemented

- Bulk API v2 query flow: `Create Job` → poll `Get Job Status` → `Retrieve Results`, with
  `Sforce-Locator` pagination (both the real client and the mock page results, so multi-page
  responses are exercised, not assumed away)
- OAuth2 refresh-token authentication — the same code path for the mock and a real org; only
  `SALESFORCE_BASE_URL` differs
- 10 Salesforce objects, using their real Salesforce API names in every query:

  | Dashboard name    | Salesforce API name |
  |---|---|
  | Accounts | `Account` |
  | Contacts | `Contact` |
  | Opportunities | `Opportunity` |
  | Leads | `Lead` |
  | Tasks | `Task` |
  | Cases | `Case` |
  | Products | `Product2` |
  | PricebookEntries | `PricebookEntry` |
  | Contracts | `Contract` |
  | Assets | `Asset` |

- Raw files land in MinIO at `salesforce/{object}/{organization_id}/{YYYY-MM-DD}/{object}_{job_id}.csv`
- ClickHouse tables (`ReplacingMergeTree`, partitioned and ordered by `(organisation_id, id)`)
  are created from the same schema definitions and loaded by reading the file back out of
  MinIO — not from the in-memory response — per the acceptance criteria
- Postgres job state **and** a per-object `job_objects` checkpoint table (Salesforce job id,
  row count, landed key, error, timing)
- **API authentication** (`X-API-Key` or HMAC request signing with replay protection) required
  on every `/api/v1/*` route; the app refuses to start with auth on and no credentials set
- **Crash recovery**: a startup sweep marks orphaned `pending`, `running`, `pausing`, and `cancelling` jobs as `interrupted`;
  `/resume` accepts `paused`, `interrupted` and `failed`, and skips objects already recorded in
  the checkpoint. Previously a crash left a job stuck `running` forever with no way to resume it
- Duplicate requested objects are deduplicated in request order
- Pause / resume / cancel, checked between objects (not mid-fetch — see Known limitations)
- Worker concurrency capped by `MAX_CONCURRENT_JOBS`
- Exponential backoff + jitter, retrying 408/429/5xx and transport errors, honouring
  `Retry-After` where Salesforce sends it
- Monitoring dashboard: job list with status badges, per-object progress, row counts, MinIO
  file browser, ClickHouse table inspector (engine, partition/sort keys, schema, sample rows)
- Test suite covering auth (API key, HMAC, replay, staleness), schema/CSV mapping, and the
  pause/resume/crash-recovery state machine against a faked Salesforce/MinIO/ClickHouse layer

## Start

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
curl http://localhost:8000/health
```

Open:

- API docs: http://localhost:8000/docs
- Dashboard: http://localhost:8000/dashboard — paste the `API_KEY` from `.env` into the
  "API key" field in the top bar; it's stored only in your browser
- MinIO console: http://localhost:9001 (`minioadmin` / `minioadmin`)
- ClickHouse HTTP: http://localhost:8123
- Mock Salesforce: http://localhost:9003

Compose passes Salesforce, authentication, and worker settings from `.env` to the API.
Database and storage **addresses** inside Compose use their service names. Storage
credentials are shared by the API and the corresponding service. After changing `.env`,
run `docker compose up -d` to recreate affected containers.

The example `SALESFORCE_BASE_URL=http://mock-salesforce:9000` is for Compose. If you run
`uvicorn app.main:app` on the host against the containers, set it to
`http://localhost:9003`; the mock returns an instance URL matching the incoming request.

## Run the 10-object demo

The default `.env.example` ships with `API_KEY=dev-local-key` for local use.

```bash
curl -X POST http://localhost:8000/api/v1/jobs/sync \
  -H 'X-API-Key: dev-local-key' \
  -H 'Content-Type: application/json' \
  -d '{"organization_id":"demo-org","records_per_object":100}'
```

Copy the returned `job_id`, then:

```bash
curl -H 'X-API-Key: dev-local-key' http://localhost:8000/api/v1/jobs/<job_id>
```

A successful run ends with:

```json
{"status": "completed", "processed_objects": 10, "total_rows": 1000}
```

Per-object detail (Salesforce job id, landed MinIO key, row count):

```bash
curl -H 'X-API-Key: dev-local-key' http://localhost:8000/api/v1/jobs/<job_id>/objects
```

Open the dashboard to see the same thing live: status, per-object progress, pause/resume/cancel,
and the landed MinIO files.

## Verify ClickHouse

```bash
docker compose exec clickhouse clickhouse-client --query \
  "SELECT count() FROM analytics.salesforce_accounts FINAL"
```

Or through the API (also returns engine, partition/sort keys and a few sample rows):

```bash
curl -H 'X-API-Key: dev-local-key' http://localhost:8000/api/v1/clickhouse/Accounts
```

## Verify MinIO

```bash
curl -H 'X-API-Key: dev-local-key' 'http://localhost:8000/api/v1/storage?prefix=salesforce/'
```

## Tests

```bash
python -m pytest -q                  # locally, no services required (sqlite + fakes)
node --test tests/dashboard.test.cjs # dashboard regression tests (Node 18+)
python -m pytest -q tests/test_compose.py # Compose configuration (requires Docker Compose CLI)
docker compose run --rm api pytest -q   # inside the built image
```

## Authentication

Every `/api/v1/*` route requires one of:

- `X-API-Key: <API_KEY>` — what the dashboard uses.
- HMAC request signing — for service-to-service callers. Sign
  `{timestamp}\n{METHOD}\n{path}[?{query}]\n{sha256_hex(body)}` with `HMAC_SECRET` (SHA-256,
  hex) and send `X-Timestamp` + `X-Signature`. Requests older/newer than
  `AUTH_MAX_SKEW_SECONDS` (default 300s) are rejected, and a signature can't be replayed within
  that window. See `app/auth.py::signed_headers` for a reference implementation.

Set `AUTH_ENABLED=false` only for local hacking with no exposed port; the app refuses to boot
with auth on and neither `API_KEY` nor `HMAC_SECRET` set.

## Real Salesforce

Set these in `.env`:

```text
SALESFORCE_BASE_URL=https://your-domain.my.salesforce.com
SALESFORCE_LOGIN_URL=https://login.salesforce.com   # or your My Domain login host
SALESFORCE_CLIENT_ID=...
SALESFORCE_CLIENT_SECRET=...
SALESFORCE_REFRESH_TOKEN=...
```

`SALESFORCE_MODE` is informational only — mock and real mode run the exact same OAuth2 +
Bulk API v2 code path; only the base URL changes. This has **not** been tested against a real
org in this environment (no live credentials available); the object names, SOQL and paging
logic follow the documented Bulk API v2 contract, but please verify against a sandbox before
relying on it.

## Known limitations

- Pause/cancel are checked between objects, not mid-fetch — a large in-flight object still runs
  to completion before a pause takes effect.
- The worker is in-process threads, not a distributed queue; it does not survive more than one
  API replica. `MAX_CONCURRENT_JOBS` limits concurrency within a single process only.
- `AUTO_RESUME_ON_STARTUP=true` will auto-resume every job recovered as `interrupted`; left
  `false` by default so a crash doesn't silently retry something that failed for a real reason.
- ClickHouse column sets are the fields listed in `app/services/schema.py`, not `FIELDS(ALL)` —
  add fields there (and to the matching mock/test fixtures) to widen an object's schema.
