# The collector's Google Cloud project and address: decision memo

Status: **for decision.** Decisions D2 and D3 of the collector plan are not
made, and nothing described here has been created. Written 2026-10-20. The
steps that carry out the decisions are in
[`project-move-runbook.md`](project-move-runbook.md).

- **D2:** does the collector stay in the Google Cloud project it shares with
  the Public API, or move to a project of its own?
- **D3:** do builds and consumers keep using Cloud Run's default address for
  the collector, or a hostname of our own?

The plan recommends both a dedicated project and a custom hostname, and pairs
them: moving the project changes the default address, so without D3 every
consumer changes twice. The [choices](#the-choices-to-make) are listed at the
end.

## Today

Taken from this repo's deployment code, the GitHub environment settings (names
compared, values not printed), the Vercel project settings (names only), and
calibration-diagnostics#233's read-only checks of the live resources. The live
Google Cloud resources were not re-read for this memo.

| What | Where |
|---|---|
| Google Cloud project | `policyengine-api`, which also hosts the Public API. Both GitHub environments, `staging` and `production`, name it. |
| Per environment | a Cloud Run service, a Cloud Run migration job, a Cloud SQL for PostgreSQL instance, a runtime service account, a migration service account, and four Secret Manager secrets: the runtime database URL, the migration database URL, the session-token signing secret and the dashboard's read token |
| Shared by both environments | one Artifact Registry Docker store and image name; the deployment service account, which calibration-diagnostics also uses; the Workload Identity providers for calibration-diagnostics and for this repo |
| The dashboard's Google identity | a Workload Identity provider for Vercel's OIDC tokens, in the same project, named by its project number |
| Address | Cloud Run's default address for the production service (`https://<service>-<project number>.<region>.run.app`). The project number in it is `policyengine-api`'s. |

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
| **A. Stay in `policyengine-api`** | Nothing new. | The collector's deployment identity is the service account calibration-diagnostics also uses, in the project that runs the Public API, so a mistake in the collector's permissions, quotas or deployments can reach the Public API, and the other way round. Billing and audit logs stay mixed. Everything the collector adds next goes into that project too: S1's Hugging Face read token (#13), C5's signing key, and #12's intake bucket, Cloud Tasks queue and worker jobs. The address doesn't change, so D3 becomes optional. |
| **B. A dedicated project, moved in one maintenance window per environment** (the plan's recommendation) | Recurring cost stays about the same: the same Cloud Run, Cloud SQL, Secret Manager and image storage, billed to the new project. One-off: until the old resources are deleted, both projects run a Cloud SQL instance for each environment, and the old Cloud Run services each keep one instance running (`--min 1`). A small export bucket. People's time. | **Downtime:** telemetry ingestion and the dashboard's collector reads pause while each database is exported and imported and the service is redeployed, and the dashboard's reads stay down until its own settings change is deployed. The export and import take minutes; the redeploy is a full release (two Push runs, then staging's and production's deployments), whose length depends on the queue for GitHub's runners: about 30 minutes on a quiet day, 2 to 3 hours when runners are queued, as on 2026-10-19 and 2026-10-20 (the runbook's section 6 gives the measurements). The staging rehearsal measures it. **The database move:** the database roles and their privileges were provisioned by scripts kept outside this repo, so they have to be read from the old instance and recreated by hand before the import. **Every project-specific setting changes:** in each GitHub environment, five of the 15 variables (the project, both Cloud SQL connections and both service accounts) and both secrets, while the other ten can keep their values if the new resources reuse the old names; up to three Vercel settings; and the dashboard's read-secret check. **The default address changes,** so without D3 the emitter needs a release and builds on older emitters lose delivery. |
| **C. A dedicated project, with the database moved later** (Cloud Run in the new project connects to Cloud SQL in the old one for a while) | As B, spread over two windows. | Needs deployment-script changes first: `create-database-backup` refuses a Cloud SQL connection whose project isn't `PROJECT_ID`. Needs cross-project grants for Cloud SQL and Secret Manager. It shortens the first window but adds a second one, and leaves the collector split over two projects in between. Worth it only if one window of B is too long. |

What a dedicated project gives: its own permissions (a deployment identity
that can reach only the collector), its own quotas and billing, audit logs
that contain only the collector, and room for the intake and worker resources
without adding them to the Public API's project.

## D3: which address

| Option | Cost | Risks and limits |
|---|---|---|
| **A. Keep Cloud Run's default address** | Nothing. | The address contains the project number, so D2 B or C changes it. Every change needs an emitter release, and builds on older emitters lose delivery until they move. |
| **B. Cloud Run domain mapping** | No charge. | Google lists it as Preview: "not production-ready", and "not recommended for production services" because of latency. TLS 1.0 and 1.1 can't be switched off, and only Google's certificate can be used. The certificate is issued after the mapping exists and DNS points at it: "usually about 15 minutes but can take up to 24 hours." A hostname has one active mapping, so moving the project means re-creating the mapping there and waiting for a new certificate. `us-central1` supports mappings. |
| **C. Global external Application Load Balancer** with a serverless network endpoint group and a Certificate Manager certificate | About $0.025 an hour for the forwarding rule (about $18 a month) plus about $0.008 per GB processed; Certificate Manager's first 100 certificates are free. Check Google's current price lists. | Generally available, and Google's recommended option. With DNS authorization the certificate is issued before any traffic moves, so a later project move is a DNS change with no certificate gap. More resources to keep: a static IP, a network endpoint group, a backend service, a URL map, an HTTPS proxy, a forwarding rule and a certificate map. Cloud Armor can be added later. |
| **D. Route through Vercel or Cloudflare** | Varies. | Not recommended: another provider would end TLS for requests that carry builders' Hugging Face tokens. |

Which DNS zone (from public name-server records, 2026-10-20):

- **`microcosm.institute`** is served by Cloudflare's name servers; its apex
  points at Vercel, which serves the dashboard. A collector record there must
  be "DNS only", not proxied, so that Cloudflare doesn't end TLS and doesn't
  interfere with Google's certificate checks.
- **`policyengine.org`** is served by Google Cloud DNS name servers. Which
  project holds that zone wasn't checked.
- Neither zone has CAA records today. If one is added, it must allow
  `pki.goog` and `letsencrypt.org`.
- Domain mapping (B) needs the base domain verified in Google Search Console
  by the account that creates the mapping. The load balancer's certificate (C)
  doesn't: Certificate Manager's DNS authorization needs only a `CNAME` record
  in the zone.

Whatever is chosen, three things stay as they are:

- **Cloud Run's default addresses stay on** (`--default-url` in
  `scripts/deploy.sh`). The deployment checks the maintenance and candidate
  revisions through their tagged default addresses, and builds on older
  emitters use the production default address.
- **Ingress stays open to the internet.** Build machines and Modal containers
  reach the collector directly. Allowing only load-balancer traffic would
  break the tagged-address checks and older emitters.
- **The old address can't redirect to the new one.** The emitter doesn't
  follow redirects on requests that carry tokens (`_NoRedirectHandler` in
  `core/auth.py`). After a move, the old service either keeps serving or stops.

## What changes for each consumer

### The emitter

- **Where:** `PRODUCTION_COLLECTOR_URL`, and after
  PolicyEngine/microcosm-emitter#29 also `TRUSTED_COLLECTOR_ORIGINS`, both in
  `core/constants.py`. Every service module resolves the origin through
  `core/collector_origin.py`. In the emitter stack, #27 makes a third service
  module, `huggingface/service/module.py` (added by #23), send submissions to
  the collector; it chooses the origin with its own copy of the old expression
  and should use the same resolver.
- **The change:** one emitter PR adds the new origin to
  `TRUSTED_COLLECTOR_ORIGINS` and points `PRODUCTION_COLLECTOR_URL` at it.
  Merging it publishes a release to PyPI.
- **Who picks it up:**
  - Builds that start the separately installed emitter (emitter #21 and #22,
    and Microcosm's H2a) use the new release from their next start once it is
    installed on that machine or in that build image. They need no Microcosm
    change. A running build keeps the emitter it started with.
  - Builds pinned to `microcosm-emitter==0.2.1` (Microcosm's
    `packages/microcosm-build/pyproject.toml` today) keep the old address
    until Microcosm moves off that pin.
- **If the old address stops answering,** a build that still uses it keeps
  its telemetry on its own machine: the emitter retries with a backoff of up
  to 60 seconds and keeps undelivered events for seven days (up to 100 MiB),
  and a later emitter on the same machine sends them. A machine discarded
  when its build ends, such as a Modal container, loses the events not sent
  by then. A build never fails because of telemetry.

### The calibration dashboard (calibration-diagnostics)

| Setting | Provider | Read by | Changes under D2 B | Changes under D3 B or C |
|---|---|---|---|---|
| `MICROCOSM_TELEMETRY_COLLECTOR_URL` | GitHub `Production` environment variable; also set in the Vercel project for Production and Preview | `deploy.yml` passes the GitHub value to `.github/scripts/deploy-dashboard.sh`, which requires it and sets it on the production deployment with `vercel deploy --env`. Preview deployments use the Vercel value. The server reads it in `frontend/lib/server/microcosm/telemetry-collector-client.ts`. | the new default address (if D3 is A) | the custom hostname, in all three places (GitHub Production, Vercel Production, and Vercel Preview with the staging address) |
| `MICROCOSM_TELEMETRY_READ_SECRET_VERSION` | Vercel project setting, Production and Preview | `telemetry-collector-client.ts`: the full Secret Manager version resource, which names the project | the new project's read secrets | no |
| `MICROCOSM_TELEMETRY_GOOGLE_IDENTITY_PROVIDER` | Vercel project setting, one entry for Production and Preview | `frontend/lib/server/secret-manager.ts`, on Vercel: the Workload Identity provider resource, named by project number | only if the provider moves (see choice 10) | no |
| `TELEMETRY_GCP_PROJECT_ID`, `TELEMETRY_READ_SECRET_NAME` | GitHub `Production` environment variables | `deploy.yml` step "Check collector read secret exists" | the new project | no |
| `TELEMETRY_GCP_WIF_PROVIDER`, `TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT` | GitHub `Production` environment secrets | the same step's Google authentication | no, if the dashboard's own identity in `policyengine-api` is granted metadata read on the new read secret | no |

Two things found while checking:

- **Preview reads staging's collector.** The identity-provider setting covers
  Production and Preview, the provider accepts both environments' Vercel
  identities, and staging's read secret grants the Preview identity access. So
  under choice 10 both identities need access to the new project's read
  secrets, each to its own environment's.
- **After C6,** the dashboard authenticates to the collector with its own
  Vercel OIDC token instead of a shared read token, so it no longer reads
  Secret Manager for the collector. If C6 lands before the move, only
  `MICROCOSM_TELEMETRY_COLLECTOR_URL` changes.

Settings take effect on the next deployment: a merge to calibration-diagnostics
`main`, or a re-run of its latest "Deploy production" run. Whether a re-run
reads a changed GitHub variable such as `MICROCOSM_TELEMETRY_COLLECTOR_URL`
hasn't been checked (the runbook's step 6.6 has the same question), so a merge
is the reliable way.

### microcosm-runs

- **Where:** `PRODUCTION_COLLECTOR_ORIGIN` in `lib/auth.ts`, overridable with
  the server-only setting `ORRERY_COLLECTOR_ORIGIN`; `.env.example` repeats the
  address, and the README names the setting. The Vercel project sets no
  `ORRERY_COLLECTOR_ORIGIN` (checked with `vercel env ls`), so production uses
  the tracked default.
- **What uses it:** only the upload API's authorization call,
  `POST /v1/auth/graph-publication/authorize`. The collector doesn't implement
  that endpoint (calibration-diagnostics#208 was closed), and O7 removes the
  upload API.
- **So:** the move breaks nothing there. Either update the constant and
  `.env.example` in a small PR when the emitter's default changes, or leave
  them for O7 to delete (choice 11). If O6 later reads collector outputs
  through its API, it should use the custom hostname from the start.

### Microcosm

No change. It doesn't name the address, and builds don't set
`collector_origin`. Its emitter pin decides how long builds keep the old
address.

### The collector itself

- **GitHub `staging` and `production` environments:** every project-specific
  value changes. The runbook lists them.
- **Session tokens:** their issuer and audience are names
  (`TELEMETRY_JWT_ISSUER` and `TELEMETRY_JWT_AUDIENCE` default to
  `policyengine-telemetry` and `microcosm-telemetry-collector`), not
  addresses, so they don't change. C5 publishes a JWKS; whoever fetches it
  will use the collector's address, which is one more reason to fix the
  address before consumers depend on it.
- **The README** says the production address isn't repeated in
  documentation. A custom hostname is stable, so it could be documented.

## Order and timing

The plan's constraints:

- The move happens only after the collector deploy switch-over (C1) is
  verified. From then on, every merge to this repo's `main` deploys
  production, so nothing else merges during a window.
- calibration-diagnostics' `telemetry-service.yml` can still redeploy the
  collector for rollback until calibration-diagnostics#234 merges. Its
  settings name the old project, so merge #234 before the move, or never
  dispatch that workflow afterwards.
- The new project has to include whatever has merged by then: #12's intake
  resources (including its recovery job and schedule), S1's secret and its
  mount, C5's signing key, and S2's daily check job, schedule and alert
  policies (#19).

If both D2 and D3 are adopted, there are two orders:

1. **Hostname first.** Point the custom hostname at today's service in
   `policyengine-api`, release the emitter with the hostname as its default,
   and wait until builds use it (after H2a, and after Microcosm leaves the
   0.2.1 pin). Then move the project behind the hostname: only the hostname's
   target changes, and the old default address can be retired, because no
   build uses it. With the load balancer (D3 C), the certificate is issued in
   the new project before the DNS change. With domain mapping (D3 B), the
   mapping is re-created in the new project, and requests fail TLS checks
   until the new certificate is issued (the emitter retries).
2. **Together.** Create the new project and the hostname, move each database
   in one window, then release the emitter. Builds keep the old address until
   they start a newer emitter, and builds pinned to 0.2.1 keep it until
   Microcosm moves. Meanwhile the old service either keeps serving, connected
   to the new database across projects, or stays in maintenance mode, and
   those builds' telemetry stays on their machines (choice 7).

## The choices to make

1. **D2:** A (stay), B (a dedicated project, one window per environment) or C
   (a dedicated project, database later). The plan recommends B.
2. **If B or C:** the project ID and display name; its parent organization or
   folder; the billing account; who holds the owner and IAM-admin roles.
3. **D3:** A (default address), B (Cloud Run domain mapping) or C (load
   balancer). The plan recommends a custom domain without naming a mechanism.
4. **Hostnames:** the production hostname, for example
   `collector.microcosm.institute`; and whether staging gets one, for example
   `collector-staging.microcosm.institute`, or keeps its default address.
5. **DNS:** which zone (`microcosm.institute` on Cloudflare, or
   `policyengine.org` on Cloud DNS); who adds the records; for D3 B, which
   Google account verifies the base domain in Search Console.
6. **Order:** hostname first, or both together.
7. **The old address after the move:** keep the old service serving,
   connected to the new database, until no build uses emitter 0.2.1; or leave
   it in maintenance mode, so builds still on it keep their telemetry locally;
   or delete it. A redirect isn't possible.
8. **The window:** when, and how long is acceptable. During it, running builds
   keep their events locally and deliver them afterwards, except on a machine
   discarded when its build ends, such as a Modal container, which loses the
   events not sent by then. The build monitor shows only Hugging Face runs.
9. **Staging data:** dump and restore it like production (which also rehearses
   the production move), or start staging empty.
10. **The dashboard's Google identity:** create a new Vercel Workload Identity
    provider in the new project (changes
    `MICROCOSM_TELEMETRY_GOOGLE_IDENTITY_PROVIDER`), or keep it in
    `policyengine-api` and grant its identities access to the new secrets
    across projects (no Vercel change, but a lasting dependency on the shared
    project), or finish C6 first so the dashboard needs neither.
11. **microcosm-runs:** update its default address with the emitter release,
    or leave it for O7.
