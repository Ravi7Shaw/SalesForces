# BE-1 — Salesforce Bulk API v2 Ingestion Service & UI

A complete local implementation of **Task 1 / BE-1** from the Backend Engineering Platform specification. The task requires a Salesforce Bulk API v2 client, 10+ object extraction, MinIO landing, ClickHouse tables, a monitoring UI, and retry handling. The official task document also calls for demonstrating a bulk ingestion run, landed MinIO files, and a ClickHouse query.

## Architecture

```text
Salesforce Bulk API v2 / local Salesforce mock
                 |
                 v
        FastAPI ingestion API
                 |
        background ingestion worker
          /       |          \
         v        v           v
   PostgreSQL   MinIO      ClickHouse
   job state    raw CSV    analytics tables
                 |              |
                 +-------> dashboard
```

## BE-1 requirements implemented

- Salesforce Bulk API v2-style create/status/results flow
- OAuth2 refresh-token authentication path for real Salesforce
- Local synthetic Salesforce mock for credential-free testing
- 10 Salesforce objects:
  - Accounts
  - Contacts
  - Opportunities
  - Leads
  - Tasks
  - Cases
  - Products
  - PricebookEntries
  - Contracts
  - Assets
- Raw files in MinIO under:
  `salesforce/{object}/{organization_id}/{YYYY-MM-DD}/{job_id}.csv`
- Dynamically created typed ClickHouse tables
- `organisation_id` table partitioning and `(organisation_id, id)` ordering
- Idempotent ClickHouse storage using `ReplacingMergeTree`
- PostgreSQL job metadata/checkpoints
- Job status, row counts, current object, completed objects and errors
- Pause / resume / cancel controls
- Exponential backoff with `Retry-After` support for 408/429/5xx responses
- Monitoring web console
- MinIO landed-file browser in the dashboard
- ClickHouse table inspection API
- Automated unit/API tests
- Docker Compose deployment

These map directly to the BE-1 task breakdown and acceptance criteria in the supplied specification.

## Start

```bash
cp .env.example .env
docker compose up --build -d
```

Check:

```bash
docker compose ps
curl http://localhost:8000/health
```

Open:

- API docs: http://localhost:8000/docs
- Dashboard: http://localhost:8000/dashboard
- MinIO console: http://localhost:9001
- ClickHouse HTTP: http://localhost:8123
- Mock Salesforce: http://localhost:9003

MinIO credentials:

```text
minioadmin / minioadmin
```

## Run the 10-object demo

```bash
curl -X POST http://localhost:8000/api/v1/jobs/sync \
  -H 'Content-Type: application/json' \
  -d '{"organization_id":"demo-org","records_per_object":100}'
```

Copy the returned `job_id`, then:

```bash
curl http://localhost:8000/api/v1/jobs/<job_id>
```

A successful run ends with:

```json
{
  "status": "completed",
  "processed_objects": 10,
  "total_rows": 1000
}
```

Open the dashboard to show status, row counts, pause/resume/cancel controls and landed MinIO files.

## Verify ClickHouse

```bash
docker compose exec clickhouse clickhouse-client --query \
  "SELECT count() FROM analytics.salesforce_accounts FINAL"
```

Inspect the generated table:

```bash
docker compose exec clickhouse clickhouse-client --query \
  "DESCRIBE TABLE analytics.salesforce_accounts"
```

Or through the API:

```bash
curl http://localhost:8000/api/v1/clickhouse/Accounts
```

## Verify MinIO

```bash
curl 'http://localhost:8000/api/v1/storage?prefix=salesforce/'
```

The dashboard also displays the landed objects.

## Tests

Run locally with the project environment:

```bash
pytest -q
```

Run inside Docker after building:

```bash
docker compose run --rm api pytest -q
```

## Real Salesforce

Set these values in `.env` and use `SALESFORCE_MODE=real`:

```text
SALESFORCE_BASE_URL=https://your-domain.my.salesforce.com
SALESFORCE_CLIENT_ID=...
SALESFORCE_CLIENT_SECRET=...
SALESFORCE_REFRESH_TOKEN=...
```

The service then uses the OAuth2 refresh-token flow and Salesforce Bulk API v2 endpoints.

## Notes for the review/demo

The supplied BE-1 review question asks for a demonstration of a bulk run across 10 Salesforce objects, MinIO landed files, and a ClickHouse query.

For production deployment, the next hardening steps would be external secret management, distributed workers/locks, authentication/authorization, metrics/tracing, migrations, and deployment-specific security policies. The local implementation intentionally uses Docker Compose and a synthetic API so the entire acceptance flow can be demonstrated without paid Salesforce production credentials, which the task explicitly permits.
