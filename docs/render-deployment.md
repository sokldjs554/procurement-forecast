# Render production deployment

This is a deployable configuration, **not evidence of a live deployment**. As of
2026-09-30, the connected workspace has no service or dedicated free database/queue
capacity available for this project. Other projects' resources were left untouched.
No paid resource was created. The user's current constraint is **free only**, so the
paid Blueprint below is a reference configuration and has not been applied.

Free local Docker and finite GitHub Actions runs verify execution and restart
persistence; they do not supply an externally accessible, continuously running backend.
A dedicated existing always-on host (with access and capacity) would be needed to deploy
this full stack without new service charges. No such host is currently connected.

## Resources and cost

The Blueprint creates five resources in Singapore in a `procurement-forecast` project:

| Resource | Current plan ID | Monthly baseline (USD) |
| --- | --- | ---: |
| FastAPI web service | `0.5c-512mb` | $7.00 |
| Next.js web service | `0.5c-512mb` | $7.00 |
| arq background worker, two concurrent jobs | `1c-2g` | $25.00 |
| PostgreSQL 16 with pgvector and pg_trgm | `0.1c-256mb` | $6.00 |
| PostgreSQL storage, 1 GB | — | $0.30 |
| Persistent Redis-compatible Key Value | `256mb` | $10.00 |
| **Configured Render total** | | **$55.30/month** |

Prices were checked against [Render pricing](https://render.com/pricing) on 2026-09-29.
The cheapest paid compute alternative uses a 512 MB worker and totals **$37.30/month**;
that worker size has not been validated for PDF rendering and Korean OCR and is not selected
in the Blueprint. Even the selected 2 GB worker is an initial capacity choice, not a load-test
result. Start at two concurrent jobs and monitor memory, CPU, and queue age. The 256 MB
Postgres instance and 1 GB database storage are also entry-level capacity, not a promise of
production-scale throughput. Monitor usage and explicitly review any resize.

This baseline assumes a Hobby workspace with paid compute. It excludes the existing workspace's
subscription, GCS storage/operations/egress, outbound bandwidth, extra build minutes, custom
domains, source-provider fees, model APIs, email, payment processing, and taxes. The current Pro
workspace subscription adds $25/month if selected; it is not required by this Blueprint. Render
bills compute by elapsed usage; this is a full-month estimate, not a spending cap.

Free resources cannot provide the requested durable production stack. Render's
[free service documentation](https://render.com/docs/free) states that free web services sleep,
free databases expire after 30 days and have no backups, and free Key Value loses data on
restart. Background workers require paid compute. A free Next.js deployment would also consume
the workspace's shared free-hours allowance and lose always-on availability.

## Required inputs before provisioning

Provision only after the resource cost has been approved and the following values are available.
Enter secrets in the Render setup form or a Render secret file, never in Git or a chat transcript.
`sync: false` prompts only during initial Blueprint creation; later changes use the Dashboard.

| Setting | Required value / owner |
| --- | --- |
| `APP_STORAGE_URL` | Existing private GCS bucket and prefix, `gs://<bucket>/raw` |
| `RENDER_GCP_CREDENTIALS_JSON` | Service-account JSON with object get/create on that bucket |
| `APP_PUBLIC_WEB_URL` | The actual HTTPS frontend origin, no path, credentials, or query |
| `APP_DATA_GO_KR_SERVICE_KEY` | Approved public-data API key for the implemented procurement operations |
| `APP_TOSS_CLIENT_KEY`, `APP_TOSS_SECRET_KEY` | Corresponding live Toss merchant keys for real billing |
| `APP_SMTP_HOST`, `APP_SMTP_USERNAME`, `APP_SMTP_PASSWORD` | Authenticated SMTP relay supporting STARTTLS on port 587 |
| `APP_MAIL_FROM` | Verified sender address/domain accepted by that relay |

The wrapper refuses local file storage, missing required values, fake billing, localhost SMTP,
insecure cookies, invalid encryption keys, and an HTTP/localhost frontend origin. It does not
prove the supplied data-provider, Toss, or SMTP credentials work. Provider validation and a
real end-to-end acceptance check remain required. Do not enter dummy values to make startup pass.

The Blueprint generates shared JWT and billing-encryption secrets once. Render documents
[`generateValue` as a base64-encoded 256-bit value](https://render.com/docs/configure-environment-variables)
(32 bytes, not a hexadecimal string). Preserve the billing key
when recovering or migrating the database: replacing it makes saved encrypted billing keys
unreadable. Render's generated base64 key is normalized to the URL-safe Fernet alphabet.
The wrapper also converts Render's `postgres://` connection URL to SQLAlchemy's
`postgresql+asyncpg://` without changing credentials.

## Shared raw documents

Both API and worker use the existing `gs://` storage backend. Render
[persistent disks](https://render.com/docs/disks) belong to one service instance and cannot be
shared between these two services. Their default filesystems are ephemeral. This Blueprint
therefore deliberately does not attach separate local disks and pretend they are shared storage.
Render does not provision the GCS bucket or service account.

Grant the service account `storage.objects.get` and `storage.objects.create` on the bucket
(for example an appropriately scoped object role), and enable the Cloud Storage API in its GCP
project. Choose the bucket location and retention/backup policy deliberately; storage and
cross-cloud egress are separately billed by GCP. Configure object versioning/retention according
to the required recovery policy. The startup IAM check is read-only and never creates a probe
object; an actual ingest/read cycle is still needed to prove cross-service access.

The initial Blueprint form accepts the service-account JSON once on the API and references it
from the worker. The wrapper writes it to a mode-0600 ephemeral credentials file and removes the
JSON value from the child process environment. A secret file is preferable for ongoing operation:
attach `gcp-credentials.json` to both services (or a shared environment group), set
`GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/gcp-credentials.json`, and remove the JSON env
variable/reference from the Blueprint after both services are configured. Secrets are never
passed to a Docker build argument or printed by the wrapper.

## Deploy sequence

1. Push the reviewed commit and `render.yaml` to the GitHub repository. Create a Blueprint from
   that exact branch in the confirmed workspace `tea-da4jo3bbc2fs73bf8090` (`My workspace`).
   The Blueprint inherits its selected branch; it does not hardcode `main` or a work branch.
2. Review all five resource names, the Singapore region, their selected paid plans, and the
   $55.30/month Render estimate. Names in a Blueprint can match existing resources, so stop if
   the preview proposes modifying an unrelated resource. Select only this project's resources.
3. Supply the required settings. Use the actual frontend HTTPS domain; if Render allocates a
   different `onrender.com` hostname, update `APP_PUBLIC_WEB_URL` on the API, sync the worker's
   reference, and redeploy both before user acceptance.
4. Render builds the API and worker with `scripts/render-api.Dockerfile`, including the locked
   Python dependencies, GCS extra, Tesseract, and Korean language data. Next.js uses its existing
   standalone Docker build. The frontend calls the API over Render's internal host/port and
   keeps browser session cookies first-party through its `/api/*` proxy.
5. API pre-deploy runs `python /app/render-runtime.py migrate`. A Postgres advisory lock
   serializes retries. Alembic enables `vector` and `pg_trgm` in the initial migration; both
   extensions are [supported on Render PostgreSQL 16](https://render.com/docs/postgresql-extensions).
   The initializer inserts the institution catalog and real-source definitions. It does not
   create demo users, organizations, fixture sources, documents, payments, or alerts.
6. API and worker startup verify Redis, GCS permissions, and the image's exact Alembic head.
   The worker waits up to ten minutes for initial API migrations. API uses `/readyz` for Render
   health checks; the Next.js service uses `/login`. Confirm each deploy is actually `live`.
7. Run the read-only smoke checks with the assigned URLs:

   ```bash
   python scripts/render-smoke.py \
     --web https://ACTUAL-WEB-HOST \
     --api https://ACTUAL-API-HOST
   ```

   This checks frontend HTML, unauthenticated API/staff rejection, API liveness, and DB/Redis
   readiness. It does not sign up users, mutate data, submit charges, or send messages.
   Background workers have no public URL. From the worker's Render Shell, verify its local
   heartbeat after the startup grace period:

   ```bash
   python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:10000/healthz").status)'
   ```

   For dependency checks or management commands inside either Python container, always use
   `python /app/render-runtime.py check` or
   `python /app/render-runtime.py exec manage <command>`. These apply the Render URL/secret
   adaptation before loading app settings. Plain `manage` in a fresh shell does not.

Automatic code deploys and preview environments are disabled to prevent unreviewed migrations
or duplicate paid stacks. For subsequent releases, deploy the reviewed API commit (including its
migrations), then the same worker commit, then the frontend. Schema changes must remain compatible
with the running older processes until the rollout completes. A frontend-only green deploy is not
proof that the API, queue worker, raw storage, or ingestion work.

## Enable and accept real features

The initial real-source catalog enables available keyed providers and preserves existing source
configuration on subsequent deploys. `APP_DATA_GO_KR_SERVICE_KEY` enables procurement records;
it does not supply council minutes or budget books. For early forecast inputs, configure either
valid CLIK/LOFIN credentials and required provider parameters, or vetted real government boards
in `sources.config` as described in the existing real-data runbooks. Supply optional source keys
to both API and worker. Enable the matching sources explicitly when adding a key after initial
bootstrap. Test provider permission, response shape, quota, and a real stored document before
claiming live source coverage. The source check command consumes provider quota but does not run
paid LLM inference:

```bash
python /app/render-runtime.py exec manage sources check
```

This deployment deliberately selects the existing heuristic extractor and hashing embeddings.
It must not be described as paid LLM extraction or semantic embeddings. Enabling Anthropic or
Voyage requires valid keys, tested model identifiers, the matching provider settings on both
services, a reviewed API budget, and the repository's evaluation gates. Do not run a paid LLM
evaluation merely to deploy infrastructure. Kakao notifications additionally need Solapi and
approved Kakao sender/template settings. No such credentials are supplied by this Blueprint.

Never run `manage seed` or `manage demo run` against production. The former creates demo
accounts with documented demo passwords. Register the real operator account normally. Resolve
that account's exact email and immutable `users.id` with a read-only database query, then have
the authorized workspace operator run the following in the API Render Shell. Replace both
values with the verified account; the bound query requires both to match and refuses an
unmatched account. This grants staff access and does not set or reveal a password:

```bash
OPERATOR_EMAIL='verified-registered-email' OPERATOR_USER_ID='verified-numeric-id' \
python /app/render-runtime.py exec python - <<'PY'
import asyncio
import os
from sqlalchemy import select
from app.db.models import User
from app.db.session import dispose_engine, session_scope

async def grant():
    email = os.environ['OPERATOR_EMAIL']
    user_id = int(os.environ['OPERATOR_USER_ID'])
    try:
        async with session_scope() as session:
            user = (await session.execute(
                select(User).where(User.id == user_id, User.email == email).with_for_update()
            )).scalar_one_or_none()
            if user is None:
                raise SystemExit('Refused: verified user ID and email do not match')
            user.is_staff = True
        print('Granted staff to verified user ID', user_id)
    finally:
        await dispose_engine()

asyncio.run(grant())
PY
```

Log in again and verify `/api/admin/overview` and the review queue. There is intentionally no
public first-admin signup shortcut and no default administrator account.

Acceptance requires evidence from the deployed environment: signup/login over HTTPS with secure
cookies; a real source ingest and raw document read from both services; document processing,
opportunity publication, and worker/cron progress; an authorized email delivery; and separately
approved merchant/payment validation. Record deployment IDs and the source/document IDs used.
Do not equate a passing `/healthz` or empty dashboard with these outcomes.

## Recovery and limitations

- Paid Render Postgres provides point-in-time recovery; the window depends on workspace plan.
  Test a restore and keep the matching billing encryption key and GCS recovery policy.
- Key Value uses `noeviction` and `journal-snapshot` persistence so queue entries are not silently
  evicted on memory pressure. Monitor available memory and failed writes; this is not an
  exactly-once delivery guarantee.
- Workers have no Render HTTP health-check field. Inspect the local heartbeat, Redis queue age,
  job failures, and logs. A separate uptime/metrics alert should be configured before relying on
  unattended ingestion.
- Render waits at most 300 seconds on shutdown, while application jobs can run for 900 seconds.
  Interrupted jobs must be retried; verify the pipeline's idempotency and shutdown handling.
- A paid 256 MB Postgres instance is not highly available. Instance resize/maintenance can cause
  downtime. Neither compute sizing nor peak-throughput capacity was established by schema checks.
- The available Render connector can inspect resources and create web services, Postgres, Key
  Value, and cron jobs. It does not expose Blueprint sync, background-worker creation, secret-file
  upload, or general shell execution. Use the Dashboard Blueprint flow or an independently
  authenticated supported CLI/API for those steps; do not substitute a free web service for the
  real worker. The read-only Postgres tool cannot run migrations.

## Local validation completed

`render.yaml` was validated against the live official
[`render.yaml` JSON schema](https://render.com/schema/render.yaml.json). Focused tests cover
Render URL adaptation, missing/unsafe production settings, redacted validation errors, and
bounded worker concurrency. The repository's API lint and test commands remain required.
The editing workspace had no Docker engine. The [verified CI run](https://github.com/sokldjs554/procurement-forecast/actions/runs/36557807923)
built the API, web, and Render production images successfully. Resource creation, live health
responses, provider connectivity, and the full production user flow remain unverified.
For Render-side semantic validation, use `render blueprints validate render.yaml` with a current
authenticated Render CLI before creating the Blueprint.
