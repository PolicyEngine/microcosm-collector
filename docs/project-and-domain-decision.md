# The collector's Google Cloud project and address: decision memo

Status: **decided.** Anthony made decisions D2 and D3 of the collector plan,
and the choices that follow from them, on 2026-10-27. Nothing described here
has been created yet. The memo was written on 2026-10-20 to set out the
options; they are kept below as the record of what was weighed. The steps
that carry out the decisions are in
[`project-move-runbook.md`](project-move-runbook.md).

- **D2:** does the collector stay in the Google Cloud project it shares with
  the Public API, or move to a project of its own?
- **D3:** do builds and consumers keep using Cloud Run's default address for
  the collector, or a hostname of our own?

This memo and the runbook contain no hostnames, domain names, IP addresses,
project IDs or numbers, account IDs, instance, database or user names,
connection names, service-account emails or secret values. Where one is
needed they use a named placeholder, such as `<production hostname>` or
`<new project ID>`, and say where the value is kept.

## The decisions

Numbered as the choices were offered (the last section of the 2026-10-20
version of this memo).

| Choice | Decision |
|---|---|
| 1. D2, the project | **B: a dedicated project,** holding both environments, each moved in one database copy. |
| 2. The project's details | It uses the **same organization and billing account as the current project.** It has **one owner.** Its ID (`<new project ID>`) is chosen when it is created and recorded by the operator; after each environment's copy it is also the value of that environment's `TELEMETRY_GCP_PROJECT_ID` variable. |
| 3. D3, the address | **C: a global external Application Load Balancer,** with a Google-managed certificate from Certificate Manager. |
| 4. Hostnames | **One load balancer and one hostname per environment,** `<production hostname>` and `<staging hostname>`, both under a PolicyEngine-owned domain (`<PolicyEngine domain>`, the domain both hostnames end with). The operator records the hostnames; from C2 they are also the values of the dashboard's `MICROCOSM_TELEMETRY_COLLECTOR_URL` settings, and from C3 the production hostname is in the emitter's `PRODUCTION_COLLECTOR_URL`. |
| 5. DNS | The domain's DNS is **managed outside Google Cloud and edited by hand by Anthony.** Each environment needs two records: an `A` record for its load balancer's static IP, and the `CNAME` for its Certificate Manager DNS authorization. The runbook lists them (step 5.3). |
| 6. Order | **Hostname first.** Each environment's load balancer is built in the new project from the start. Until that environment moves, it sends requests across projects to today's service, through a backend service and a serverless network endpoint group left in the current project. Clients switch to the hostname; each environment's move is then one change to its load balancer's URL map. |
| 7. The old address | **Maintenance mode after production's copy.** The old production service is deleted once no build uses emitter 0.2.1 and it has had no requests for seven days. |
| 8. Windows | **No scheduled maintenance windows.** Each environment's database copy still puts that environment's service in maintenance mode while it runs. |
| 9. Staging data | **Copied with a dump and restore,** as the rehearsal for production's copy. |
| 10. The dashboard's Google identity | **It stays in the current project,** and is granted read access to the new project's read secrets. That access is removed when the dashboard switches to its Vercel OIDC token (C8). |
| 11. microcosm-runs | **Its address is left unchanged** until the emitter's direct upload (O7) and microcosm-runs' upload API (O8) are removed. |

### What follows from them

The plan's items for this work, as renumbered on 2026-10-27:

1. **C2, the hostname:** each environment's load balancer, certificate and
   DNS records, in front of today's service. The dashboard then reads the
   collector through the hostname.
2. **C3, the emitter's default:** an emitter release whose fixed set of
   trusted origins (PolicyEngine/microcosm-emitter#29) includes the
   production hostname, which is also its default.
3. **C4, builds run that release,** including the Modal image. Microcosm has
   to leave its `microcosm-emitter==0.2.1` pin first (H2a).
4. **C5, the move:** prepare the new project, then copy staging, then
   production. Production's copy waits for C4.
5. **After the move, in this order:** the collector's own Hugging Face token
   and its daily check (S1, S2); switching artifact intake on; read API v2
   (C6); ES256 session-token signing (C7). Each creates its cloud resources
   once, in the new project.

### Why

- **The same organization** is what lets the new project's load balancer use
  a backend service in the current project. Google's external Application
  Load Balancer overview says the frontend and URL map "can reference backend
  services or backend buckets from any project within the same
  organization".
- **A serverless network endpoint group can't point at a Cloud Run service in
  another project** ("the serverless NEG must be created in the same project
  as the backing Cloud Run" service, in Google's serverless NEG overview). So
  the endpoint group and backend service for today's service stay in the
  current project, and only the frontend is in the new project.
- **The load balancer's certificate is issued before any client uses the
  hostname,** through the DNS authorization, so there is never a period
  without a valid certificate. Because the static IP is in the new project
  from the start, **the DNS records never change during the move**: each
  move, and each rollback, is one URL map change, with no DNS caching delay.
- **Hostname first** means builds use the hostname before production's copy,
  so production's old service can stay in maintenance mode afterwards
  instead of serving the new database across projects. The cost is the wait
  for C4.
- **Keeping the dashboard's identity** avoids building a provider in the new
  project that C8 would then remove, and avoids changing
  `MICROCOSM_TELEMETRY_GOOGLE_IDENTITY_PROVIDER`, one Vercel setting that
  Production and Preview share.
- **Switching intake on after the move** creates its bucket in the new
  project from the start. Moving a bucket later would need a one-off change,
  because each accepted submission records its files' Cloud Storage
  generation numbers, and a copy into another bucket gets new ones.

## Today

Taken from this repo's deployment code, the GitHub environment settings (names
compared, values not printed), the Vercel project settings (names only), and
calibration-diagnostics#233's read-only checks of the live resources. On
2026-10-22 a read-only check of the current project (`describe`, `list` and
`get-iam-policy`) found the services, migration jobs, instances, accounts,
secret names, image store and Workload Identity providers as described here.

| What | Where |
|---|---|
| Google Cloud project | The current project, `<current project ID>`, which also hosts the Public API. Both GitHub environments, `staging` and `production`, name it in `TELEMETRY_GCP_PROJECT_ID`. |
| Per environment | a Cloud Run service, a Cloud Run migration job, a Cloud SQL for PostgreSQL instance, a runtime service account, a migration service account, and four Secret Manager secrets: the runtime database URL, the migration database URL, the session-token signing secret and the dashboard's read token. Production also keeps an older database URL secret that no deployment uses; its only version was disabled on 2026-10-27. |
| Database users | the migration user, which owns the tables, and the runtime user, which the service connects as, besides Cloud SQL's built-in admin user. Production's unused third user was dropped on 2026-10-27. |
| Shared by both environments | one Artifact Registry Docker store and image name; the deployment service account, which calibration-diagnostics also uses; the Workload Identity providers for calibration-diagnostics and for this repo |
| The dashboard's Google identity | a Workload Identity provider for Vercel's OIDC tokens, in the same project, named by its project number |
| Address | Cloud Run's default address for the production service, which contains the service name, the project number and the region. The project number in it is the current project's. |

Where the address appears:

- **The emitter** fixes it as `PRODUCTION_COLLECTOR_URL` in
  `src/microcosm_emitter/core/constants.py`, in 0.2.1 (which Microcosm pins),
  0.3.0 and `main`. PolicyEngine/microcosm-emitter#29 adds a fixed set of
  trusted origins and a `collector_origin` module setting, without changing
  the default.
- **microcosm-runs** fixes it as `PRODUCTION_COLLECTOR_ORIGIN` in
  `lib/auth.ts`, and repeats it in `.env.example`.
- **The calibration dashboard** reads it from the setting
  `MICROCOSM_TELEMETRY_COLLECTOR_URL`.
- **Microcosm** doesn't name it. `docs/orrery-publication.md` says the origin
  is the emitter's tracked constant, with no override.

## D2: which Google Cloud project

| Option | Cost | Risks and limits |
|---|---|---|
| **A. Stay in the current project** | Nothing new. | The collector's deployment identity is the service account calibration-diagnostics also uses, in the project that runs the Public API, so a mistake in the collector's permissions, quotas or deployments can reach the Public API, and the other way round. Billing and audit logs stay mixed. Everything the collector adds next goes into that project too: S1's Hugging Face read token (#13), C7's signing key, and #12's intake bucket, Cloud Tasks queue and worker jobs. The address doesn't change, so D3 becomes optional. |
| **B. A dedicated project, moved in one database copy per environment** (**chosen**) | Recurring cost stays about the same: the same Cloud Run, Cloud SQL, Secret Manager and image storage, billed to the new project, plus the load balancers (D3 C). One-off: until the old resources are deleted, both projects run a Cloud SQL instance for each environment, and the old Cloud Run services each keep one instance running (`--min 1`). A small export bucket. People's time. | **Downtime:** telemetry ingestion and the dashboard's collector reads pause while each database is exported and imported and the service is redeployed. With the hostname first, the dashboard's reads recover at the URL map change, with no dashboard deployment. The export and import take minutes; the redeploy is a full release (two Push runs, then staging's and production's deployments), whose length depends on the queue for GitHub's runners. In all, about 40 minutes on a quiet day, 2 to 3 hours when runners are queued, as on 2026-10-19 and 2026-10-20 (the runbook's section 8 gives the measurements). Staging's copy measures it. **The database move:** the database roles and their privileges were provisioned by scripts kept outside this repo, so they have to be read from the old instance and recreated by hand before the import, and the migration user's default privileges set again. **Every project-specific setting changes:** in each GitHub environment, five of the 15 variables (the project, both Cloud SQL connections and both service accounts) and both secrets, while the other ten can keep their values if the new resources reuse the old names; later, one Vercel setting and the dashboard's read-secret check. |
| **C. A dedicated project, with the database moved later** (Cloud Run in the new project connects to Cloud SQL in the old one for a while) | As B, spread over two copies. | Needs deployment-script changes first: `create-database-backup` refuses a Cloud SQL connection whose project isn't `PROJECT_ID`. Needs cross-project grants for Cloud SQL and Secret Manager. It shortens the first copy but adds a second one, and leaves the collector split over two projects in between. Worth it only if one copy of B is too long. |

What a dedicated project gives: its own permissions (a deployment identity
that can reach only the collector), its own quotas and billing, audit logs
that contain only the collector, and room for the intake and worker resources
without adding them to the Public API's project. Intake is switched on only
after the move, so its bucket is in the new project from the start.

## D3: which address

| Option | Cost | Risks and limits |
|---|---|---|
| **A. Keep Cloud Run's default address** | Nothing. | The address contains the project number, so D2 B or C changes it. Every change needs an emitter release, and builds on older emitters lose delivery until they move. |
| **B. Cloud Run domain mapping** | No charge. | Google lists it as Preview: "not production-ready", and "not recommended for production services" because of latency. TLS 1.0 and 1.1 can't be switched off, and only Google's certificate can be used. The certificate is issued after the mapping exists and DNS points at it: "usually about 15 minutes but can take up to 24 hours." A hostname has one active mapping, so moving the project means re-creating the mapping there and waiting for a new certificate. |
| **C. Global external Application Load Balancer** with a serverless network endpoint group and a Certificate Manager certificate (**chosen**) | About $0.025 an hour for up to five forwarding rules in a project (about $18 a month), so a second load balancer in the same project adds no forwarding-rule charge, plus about $0.008 per GB processed; Certificate Manager's first 100 certificates are free. Check Google's current price lists. | Generally available, and Google's recommended option. With DNS authorization the certificate is issued before any traffic moves. More resources to keep: a static IP, a network endpoint group, a backend service, a URL map, an HTTPS proxy, a forwarding rule and a certificate map per environment. Cloud Armor and an SSL policy (for example TLS 1.2 as the minimum version) can be added later. |
| **D. Route through Vercel or Cloudflare** | Varies. | Not recommended: another provider would end TLS for requests that carry builders' Hugging Face tokens. |

DNS, as decided:

- **The records are plain DNS records** at the domain's provider, outside
  Google Cloud, with no proxying or forwarding in front of them, so that no
  other provider ends TLS and Google can check the authorization record.
- **The authorization `CNAME` stays for good.** Certificate Manager uses it
  for renewals as well as the first issue.
- **CAA records:** the domain had none when checked. The runbook uses
  per-project DNS authorizations, for which Certificate Manager issues only
  Google Trust Services certificates, so any CAA record added later must allow
  Google Trust Services. A CAA record at the domain's apex applies to every
  hostname under it.
- Domain mapping (B) would have needed the domain verified in Google Search
  Console. The load balancer's certificate doesn't: the DNS authorization
  needs only the `CNAME` record.

Three things stay as they are:

- **Cloud Run's default addresses stay on** (`--default-url` in
  `scripts/deploy.sh`). The deployment checks the maintenance and candidate
  revisions through their tagged default addresses, and builds on older
  emitters use the production default address.
- **Ingress stays open to the internet.** Allowing only load-balancer traffic
  would break the tagged-address checks and older emitters.
- **The old address can't redirect to the new one.** The emitter doesn't
  follow redirects on requests that carry tokens (`_NoRedirectHandler` in
  `core/auth.py`). After a move, the old service either keeps serving or stops;
  choice 7 keeps it in maintenance mode.

## What changes for each consumer

### The emitter (C3, C4)

- **Where:** `PRODUCTION_COLLECTOR_URL`, and after
  PolicyEngine/microcosm-emitter#29 also `TRUSTED_COLLECTOR_ORIGINS`, both in
  `core/constants.py`. Every service module resolves the origin through
  `core/collector_origin.py`. In the emitter stack, #27 makes a third service
  module, `huggingface/service/module.py` (added by #23), send submissions to
  the collector; it chooses the origin with its own copy of the old expression
  and should use the same resolver.
- **The change (C3):** one emitter PR adds `https://<production hostname>` to
  `TRUSTED_COLLECTOR_ORIGINS` and points `PRODUCTION_COLLECTOR_URL` at it,
  keeping the old default address in the trusted set until the old service is
  deleted. Merging it publishes a release to PyPI. It needs only C2: the
  hostname already reaches today's service.
- **Who picks it up (C4):**
  - Builds that start the separately installed emitter (emitter #21 and #22,
    and Microcosm's H2a) use the new release from their next start once it is
    installed on that machine or in that build image, the Modal image
    included. A running build keeps the emitter it started with.
  - Builds pinned to `microcosm-emitter==0.2.1` (Microcosm's
    `packages/microcosm-build/pyproject.toml` today) keep the old address
    until Microcosm moves off that pin. 0.2.1 has the address built in, with
    no setting that changes it.
- **If the old address stops answering,** a build that still uses it keeps
  its telemetry on its own machine: the emitter retries with a backoff of up
  to 60 seconds and keeps undelivered events for seven days (up to 100 MiB),
  and a later emitter on the same machine sends them. A machine discarded
  when its build ends, such as a Modal container, loses the events not sent
  by then. A build never fails because of telemetry.

### The calibration dashboard (calibration-diagnostics)

| Setting | Provider | Read by | Changes |
|---|---|---|---|
| `MICROCOSM_TELEMETRY_COLLECTOR_URL` | GitHub `Production` environment variable; also set in the Vercel project for Production and Preview | `deploy.yml` passes the GitHub value to `.github/scripts/deploy-dashboard.sh`, which requires it and sets it on the production deployment with `vercel deploy --env`. Preview deployments use the Vercel value. The server reads it in `frontend/lib/server/microcosm/telemetry-collector-client.ts`. | In C2, to `https://<production hostname>` (GitHub `Production` and Vercel Production) and `https://<staging hostname>` (Vercel Preview). Not again. |
| `MICROCOSM_TELEMETRY_READ_SECRET_VERSION` | Vercel project setting, Production and Preview | `telemetry-collector-client.ts`: the full Secret Manager version resource, which names the project | After each environment's copy, in an ordinary dashboard deployment, to the new project's read secret. The old project's copy holds the same value, so the dashboard keeps working until then. |
| `MICROCOSM_TELEMETRY_GOOGLE_IDENTITY_PROVIDER` | Vercel project setting, one entry for Production and Preview | `frontend/lib/server/secret-manager.ts`, on Vercel: the Workload Identity provider resource, named by project number | No: the provider stays in the current project (choice 10). |
| `TELEMETRY_GCP_PROJECT_ID`, `TELEMETRY_READ_SECRET_NAME` | GitHub `Production` environment variables | `deploy.yml` step "Check collector read secret exists" | With `MICROCOSM_TELEMETRY_READ_SECRET_VERSION` for production: the new project, and the secret name if it changed. |
| `TELEMETRY_GCP_WIF_PROVIDER`, `TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT` | GitHub `Production` environment secrets | the same step's Google authentication | No: the dashboard's deployment identity in the current project is granted metadata read on the new read secret. |

Two things found while checking:

- **Preview reads staging's collector.** The identity-provider setting covers
  Production and Preview, the provider accepts both environments' Vercel
  identities, and staging's read secret grants the Preview identity access. So
  both identities get access to the new project's read secrets, each to its
  own environment's.
- **After C8,** the dashboard authenticates to the collector with its own
  Vercel OIDC token instead of a shared read token, so it no longer reads
  Secret Manager for the collector, and the grants above are removed.

Settings take effect on the next deployment: a merge to calibration-diagnostics
`main`, or a re-run of its latest "Deploy production" run. Whether a re-run
reads a changed GitHub variable such as `MICROCOSM_TELEMETRY_COLLECTOR_URL`
hasn't been checked (the runbook's step 8.7 has the same question), so a merge
is the reliable way.

### microcosm-runs

- **Where:** `PRODUCTION_COLLECTOR_ORIGIN` in `lib/auth.ts`, overridable with
  the server-only setting `ORRERY_COLLECTOR_ORIGIN`; `.env.example` repeats the
  address, and the README names the setting. The Vercel project sets no
  `ORRERY_COLLECTOR_ORIGIN` (checked with `vercel env ls`), so production uses
  the tracked default.
- **What uses it:** only the upload API's authorization call,
  `POST /v1/auth/graph-publication/authorize`. The collector doesn't implement
  that endpoint (calibration-diagnostics#208 was closed).
- **So:** the move breaks nothing there, even after the old service is
  deleted. The constant and `.env.example` stay as they are until O7 removes
  the emitter's direct upload and O8 removes microcosm-runs' upload API
  (choice 11). If O6 later reads collector outputs through the collector's
  API, it uses the hostname from the start.

### Microcosm

It doesn't name the address, and builds don't set `collector_origin`. Its
emitter pin decides how long builds keep the old address, so C4 needs H2a,
which runs the separately installed emitter, including in the Modal image.

### The collector itself

- **GitHub `staging` and `production` environments:** every project-specific
  value changes in each environment's copy. The runbook lists them.
- **Session tokens:** their issuer and audience are fixed names
  (`TELEMETRY_JWT_ISSUER` and `TELEMETRY_JWT_AUDIENCE`), not addresses, so
  they don't change. C7 publishes a JWKS; whoever fetches it will use the
  hostname.
- **The README** says the production address isn't repeated in
  documentation. A runbook PR (its section 16) updates the README for the new
  project, without naming the hostname.

## Order and timing

The plan's constraints:

- The move happens only after the collector deploy switch-over (C1) is
  verified. From then on, every merge to this repo's `main` deploys
  production, so nothing else merges while a copy runs.
- calibration-diagnostics' `telemetry-service.yml` must never be run after the
  move: its settings name the old project, and its code predates migration
  `0002_artifact_intake`, so it can't run against either database.
  calibration-diagnostics#234 removes it.
- #12 merged with intake off: migration `0002_artifact_intake` has added the
  two intake tables, which stay empty, and nothing else of intake exists in
  either project. Its bucket, queue, jobs and schedule are created in the new
  project after the move.
- The migration user's default privileges give the runtime user access to
  every table a migration creates; the migrations grant nothing (#12). Both
  environments have them today. The copy recreates only two database users,
  the migration user and the runtime user (production's unused third user was
  dropped, and Cloud SQL makes its admin user on every new instance), so the
  runbook sets the default privileges again on each new instance before any
  migration runs there.

In order:

1. **C2:** each environment's load balancer in the new project, in front of
   today's service; the DNS records; the dashboard reads through the
   hostname. No downtime.
2. **C3 and C4:** the emitter release with the hostname as its default, and
   builds running it.
3. **C5:** the new project's resources for each environment (no downtime);
   then staging's copy; then, once C4 holds, production's copy. Each copy puts
   that environment's service in maintenance mode, copies the database,
   deploys into the new project, and changes the URL map. Nothing is
   scheduled or announced in advance.
4. **After the move:** S1 and S2, switching intake on, C6, then C7, each
   created once in the new project.
5. **Retiring the old resources:** once no build uses emitter 0.2.1 and the
   old production service has had no requests for seven days.
