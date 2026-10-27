# The collector's hostname and project move (runbook)

This runbook carries out the decisions recorded in
[`project-and-domain-decision.md`](project-and-domain-decision.md) (the
numbered choices there are called "choice N" below). It covers two plan
items:

- **C2, the hostname:** each environment gets a global external Application
  Load Balancer, built in the new project from the start, with a
  Google-managed certificate. Until the environment moves, the load balancer
  sends requests across projects to today's service, through a backend
  service and a serverless network endpoint group left in the current
  project. The dashboard then reads the collector through the hostname.
- **C5, the move:** each environment's database is copied into the new
  project with a dump and restore, staging first as the rehearsal. The
  service runs in the new project, and the load balancer's URL map is changed
  to send requests to it. The DNS records don't change.

Between them come C3 (the emitter release with the production hostname as its
default) and C4 (builds run that release). Production's copy waits for C4.
After the move come S1 and S2, switching intake on, C6 and C7, in that order,
each created once in the new project (section 12).

Use it only when the collector deploy switch-over (C1) is verified: this repo
deploys staging and production, and every check in the "Verify the
deployment from microcosm-collector" section of calibration-diagnostics#233
has passed.

Nothing here has been run. Staging goes first in every section, so check each
command there before running it for production.

**No concrete values.** This runbook names no hostname, domain, IP address,
project ID or number, organization or billing account ID, instance, database
or user name, connection name, service-account email or secret value. Section
2 sets each one from where it is kept, or from a placeholder in angle brackets
that the operator replaces and records. No command here prints a secret
value: values are generated in the shell and piped into Secret Manager, or
piped from one Secret Manager secret into another.

## 1. People and access

- **Google Cloud, the new project:** permission to create a project under the
  current project's organization (Project Creator) and to link its billing
  account (Billing Account User). The new project has one owner.
- **Google Cloud, the current project:** the steps change resources there,
  not only read them. The project owner role covers all of these:

  | Steps | What they do in the current project | Role |
  |---|---|---|
  | 3, 8.1, 10 | read resources and their IAM policies | `roles/viewer` and `roles/iam.securityReviewer` |
  | 3, 7.3 | read the collector's secret values (step 3's database connection uses the migration user's password; step 7.3 copies two secrets) | `roles/secretmanager.secretAccessor` on the collector's secrets |
  | 3, 7.2, 8.3, 13 | connect to and export the old databases; delete the old instances | `roles/cloudsql.admin` |
  | 5.1, 13 | create and delete the serverless network endpoint group and backend service for today's service, and grant a role on that backend service | `roles/compute.loadBalancerAdmin`, and `compute.backendServices.setIamPolicy` for the grant |
  | 5.2, 8.8, 11 | use that backend service from the new project's URL map | `roles/compute.loadBalancerServiceUser` on that backend service (step 5.1 grants it) |
  | 8.2, 11, 13 | update the old service and its traffic; delete the old services and jobs | `roles/run.admin`, and `roles/iam.serviceAccountUser` on the old runtime account, because a service update deploys a revision that runs as it |
  | 13 | delete secrets, service accounts, this repo's Workload Identity provider and the collector's images | `roles/secretmanager.admin`, `roles/iam.serviceAccountAdmin`, `roles/iam.workloadIdentityPoolAdmin` and `roles/artifactregistry.repoAdmin` |
- **DNS:** the domain's DNS is managed outside Google Cloud. Anthony adds the
  records by hand (step 5.3).
- **GitHub:** an admin of PolicyEngine/microcosm-collector (environment
  settings), and someone who can change the calibration-diagnostics
  `Production` environment and merge in both repos.
- **Vercel:** access to the calibration-diagnostics project's settings.
- **The emitter:** someone who can merge in PolicyEngine/microcosm-emitter
  (each merge publishes a release).

## 2. Names used below

Set these once per environment, in a `bash` shell, before that environment's
part of each section. Step 8.6 changes some of the GitHub variables read
here, so keep the shell open through a copy, or write the values in the
copy's notes, which rollback (section 11) needs.

```bash
OLD_PROJECT_ID='<current project ID>'    # TELEMETRY_GCP_PROJECT_ID in both environments until their copies; recorded by the operator
OLD_PROJECT_NUMBER=$(gcloud projects describe "$OLD_PROJECT_ID" --format='value(projectNumber)')
OLD_DEPLOY_SERVICE_ACCOUNT='<current deployment account email>'   # the TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT secret; found in step 3
NEW_PROJECT_ID='<new project ID>'        # chosen in section 4; recorded by the operator
NEW_PROJECT_NUMBER=$(gcloud projects describe "$NEW_PROJECT_ID" --format='value(projectNumber)')   # empty until section 4 creates the project, which sets it again
OPERATOR_ACCOUNT=$(gcloud config get-value account)   # the Google account gcloud runs these steps as
ENVIRONMENT_NAME=staging                 # then production
COLLECTOR_HOSTNAME='<staging hostname>'  # '<production hostname>' for production; recorded by the operator, and in the dashboard's MICROCOSM_TELEMETRY_COLLECTOR_URL after step 5.5

collector_variable() {
  gh api "repos/PolicyEngine/microcosm-collector/environments/$ENVIRONMENT_NAME/variables/$1" --jq .value
}
REGION=$(collector_variable TELEMETRY_GCP_REGION)
SERVICE=$(collector_variable TELEMETRY_SERVICE)
MIGRATION_JOB=$(collector_variable TELEMETRY_MIGRATION_JOB)
ARTIFACT_REGISTRY_NAME=$(collector_variable TELEMETRY_ARTIFACT_REGISTRY_NAME)
OLD_CLOUD_SQL_CONNECTION=$(collector_variable TELEMETRY_CLOUD_SQL_CONNECTION)   # read before step 8.6
OLD_INSTANCE=${OLD_CLOUD_SQL_CONNECTION##*:}
DATABASE_SECRET_NAME=$(collector_variable TELEMETRY_DATABASE_SECRET_NAME)
MIGRATION_DATABASE_SECRET_NAME=$(collector_variable TELEMETRY_MIGRATION_DATABASE_SECRET_NAME)
JWT_SECRET_NAME=$(collector_variable TELEMETRY_JWT_SECRET_NAME)
READ_SECRET_NAME=$(collector_variable TELEMETRY_READ_SECRET_NAME)

# From step 3's record: the names in the database URL secrets, which `gcloud sql users list` also lists.
DATABASE_NAME='<database name>'
MIGRATION_DATABASE_USER='<migration user>'
RUNTIME_DATABASE_USER='<runtime user>'
ADMIN_DATABASE_USER='<admin user>'       # the admin user Cloud SQL makes on every instance; `gcloud sql users list` lists it

# Names are scoped to a project, so the new resources can reuse the old ones.
NEW_INSTANCE=$OLD_INSTANCE
NEW_CLOUD_SQL_CONNECTION="$NEW_PROJECT_ID:$REGION:$NEW_INSTANCE"
RUNTIME_SERVICE_ACCOUNT="collector-runtime-$ENVIRONMENT_NAME@$NEW_PROJECT_ID.iam.gserviceaccount.com"
MIGRATION_SERVICE_ACCOUNT="collector-migration-$ENVIRONMENT_NAME@$NEW_PROJECT_ID.iam.gserviceaccount.com"
DEPLOY_SERVICE_ACCOUNT="collector-deploy@$NEW_PROJECT_ID.iam.gserviceaccount.com"
EXPORT_BUCKET="gs://$NEW_PROJECT_ID-collector-move"

# Load balancer resources, one set per environment. Google recommends that a
# backend service's name differs between projects, so the console tells them apart.
OLD_ENDPOINT_GROUP="collector-current-endpoints-$ENVIRONMENT_NAME"   # in the current project
OLD_BACKEND="collector-current-backend-$ENVIRONMENT_NAME"            # in the current project
OLD_BACKEND_PATH="projects/$OLD_PROJECT_ID/global/backendServices/$OLD_BACKEND"
NEW_ENDPOINT_GROUP="collector-endpoints-$ENVIRONMENT_NAME"
NEW_BACKEND="collector-backend-$ENVIRONMENT_NAME"
ADDRESS="collector-address-$ENVIRONMENT_NAME"
DNS_AUTHORIZATION="collector-hostname-$ENVIRONMENT_NAME"
CERTIFICATE="collector-certificate-$ENVIRONMENT_NAME"
CERTIFICATE_MAP="collector-certificates-$ENVIRONMENT_NAME"
CERTIFICATE_MAP_ENTRY="collector-hostname-$ENVIRONMENT_NAME"
URL_MAP="collector-routes-$ENVIRONMENT_NAME"
HTTPS_PROXY="collector-proxy-$ENVIRONMENT_NAME"
FORWARDING_RULE="collector-https-$ENVIRONMENT_NAME"
```

## 3. Record the current state (read-only)

For each environment, before section 5. Write everything in the operator's
notes; the later steps compare against them.

- The service's URL, serving revision, image, Cloud SQL connection and secret
  names: the `gcloud run services describe` command in step 0 of
  calibration-diagnostics#233. This serving revision isn't the rollback
  target: step 8.1 records that just before each copy.
- The instance's settings:
  `gcloud sql instances describe "$OLD_INSTANCE" --project "$OLD_PROJECT_ID" --format=yaml`.
  Keep the database version, edition, tier, disk size, availability type,
  backup and point-in-time-recovery settings, maintenance window, database
  flags, and IP and SSL configuration.
- The database and its users:
  `gcloud sql databases list` and `gcloud sql users list` with
  `--instance "$OLD_INSTANCE" --project "$OLD_PROJECT_ID"`. Expect Cloud SQL's
  built-in admin user, the migration user and the runtime user, and no other:
  production's unused third user was dropped on 2026-10-27. The copy
  recreates only the migration and runtime users. Set `DATABASE_NAME`,
  `MIGRATION_DATABASE_USER`, `RUNTIME_DATABASE_USER` and
  `ADMIN_DATABASE_USER` from the record.
- **The database's roles and privileges.** Connect as the migration user
  through Cloud SQL Studio, or with `psql` through the Cloud SQL Auth Proxy,
  and save the output of `\l`, `\dn+ public`, `\dp public.*` and `\ddp`, and
  of the roles query below. Note which role owns the tables and what the
  runtime user is granted. These were provisioned by scripts kept outside
  this repo, so this record is the only specification of them. `\ddp` must
  show the migration user's default privileges for the runtime user (the
  README's "Database access" section); both environments had them on
  2026-10-27. The roles query lists every role's attributes and memberships
  in one result, since psql 16 and later no longer show memberships in
  `\du`. Each membership carries its PostgreSQL 16 options (`admin`,
  `inherit`, `set`, from `pg_auth_members`), which show how each role was
  made (step 7.2):

  ```sql
  SELECT r.rolname, r.rolcanlogin, r.rolcreaterole, r.rolcreatedb, r.rolinherit,
         ARRAY(SELECT g.rolname || ' (' || concat_ws(', ',
                        CASE WHEN m.admin_option THEN 'admin' END,
                        CASE WHEN m.inherit_option THEN 'inherit' END,
                        CASE WHEN m.set_option THEN 'set' END) || ')'
               FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
               WHERE m.member = r.oid ORDER BY 1) AS member_of
  FROM pg_roles r
  WHERE r.rolname !~ '^(pg_|cloudsql)'
  ORDER BY 1;
  ```
- Who may read each secret:
  `gcloud secrets get-iam-policy SECRET --project "$OLD_PROJECT_ID" --format=yaml`
  for each of the four secrets named above. The read secret's policy names
  the dashboard's Vercel identity for that environment (step 7.3 grants it
  the same access in the new project).
- Collector secrets that no deployment uses: list the current project's
  secrets (`gcloud secrets list --project "$OLD_PROJECT_ID"`). Production
  has an older database URL secret that no GitHub variable names and no
  service or job mounts; its only version was disabled on 2026-10-27. Note
  any such secret and don't copy it; section 13 deletes it.
- The deployment account's roles. Its email is the
  `TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT` secret, which GitHub can't show. It is
  the account whose IAM policy grants `roles/iam.workloadIdentityUser` to this
  repo's principal set (calibration-diagnostics#233 checked that binding);
  `gcloud iam service-accounts get-iam-policy ACCOUNT --project "$OLD_PROJECT_ID"`
  shows it. Set `OLD_DEPLOY_SERVICE_ACCOUNT` to it. Then list its roles on the
  project with
  `gcloud projects get-iam-policy "$OLD_PROJECT_ID" --flatten=bindings --filter="bindings.members:serviceAccount:$OLD_DEPLOY_SERVICE_ACCOUNT" --format="value(bindings.role)"`,
  and its roles on individual resources (the runtime and migration accounts,
  the secrets and the Artifact Registry store) from their IAM policies.
- The dashboard's settings: the current values of
  `MICROCOSM_TELEMETRY_COLLECTOR_URL` (calibration-diagnostics' GitHub
  `Production` environment, and Vercel Production and Preview) and
  `MICROCOSM_TELEMETRY_READ_SECRET_VERSION` (Vercel Production and Preview).
  Rolling back step 5.5 or section 9 sets them back.
- The dashboard's Vercel identity provider: the resource named by the Vercel
  setting `MICROCOSM_TELEMETRY_GOOGLE_IDENTITY_PROVIDER`, read with
  `gcloud iam workload-identity-pools providers describe` (issuer, allowed
  audiences, attribute mapping and condition). It stays where it is (choice
  10).

## 4. Create the project (once, no downtime)

The new project goes directly under the organization that holds the current
project, as the current project does, and uses the same billing account
(choice 2). Read
both from the current project, so neither value is typed:

```bash
gcloud projects describe "$OLD_PROJECT_ID" --format='value(parent.type)'   # must print organization
ORGANIZATION_ID=$(gcloud projects describe "$OLD_PROJECT_ID" --format='value(parent.id)')
BILLING_ACCOUNT=$(gcloud billing projects describe "$OLD_PROJECT_ID" --format='value(billingAccountName)')
gcloud projects create "$NEW_PROJECT_ID" --name '<display name>' --organization "$ORGANIZATION_ID"   # the console's name for it, chosen by the operator
gcloud billing projects link "$NEW_PROJECT_ID" --billing-account "${BILLING_ACCOUNT#billingAccounts/}"
NEW_PROJECT_NUMBER=$(gcloud projects describe "$NEW_PROJECT_ID" --format='value(projectNumber)')   # step 4.2 needs it
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com iam.googleapis.com \
  iamcredentials.googleapis.com sts.googleapis.com storage.googleapis.com \
  compute.googleapis.com certificatemanager.googleapis.com \
  --project "$NEW_PROJECT_ID"
```

A project ID can't be changed later, and can't be reused even after its
project is deleted. Record it. A budget alert on the new project, at the
collector's current cost plus the load balancers, shows an unexpected cost
early.

### 4.1 Artifact Registry

Create a Docker-format Artifact Registry store named `$ARTIFACT_REGISTRY_NAME`
in the region `$REGION`, in standard mode (in the console: Artifact Registry,
then Create, format Docker, location type Region). Google's
`gcloud artifacts` command for this, in its quickstart "Store Docker container
images in Artifact Registry", names its subcommand and flag after Google's word
for the store, which this repo's vocabulary test keeps out of documentation.
`scripts/deploy.sh` pushes to
`${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REGISTRY_NAME}/${IMAGE_NAME}`,
so the store must be in the new project.

**Let the current project's production deployments read it.**
`deploy-staging` builds the image with staging's `PROJECT_ID`, and
`deploy-production` deploys that same image. So from staging's copy until
production's copy, which waits for C4 and may be weeks later, production
deploys an image from the new project's store into the current project.
Cloud Run then needs both the deploying account and the deploying project's
Cloud Run service agent to hold Artifact Registry Reader on that store.
Without it, production's "Deploy an isolated maintenance revision" step fails
(no traffic moves) on every merge from staging's copy on. The new project
holds only the collector's store, so grant the role on the project before
staging's copy:

```bash
for old_production_reader in "serviceAccount:$OLD_DEPLOY_SERVICE_ACCOUNT" \
  "serviceAccount:service-$OLD_PROJECT_NUMBER@serverless-robot-prod.iam.gserviceaccount.com"; do
  gcloud projects add-iam-policy-binding "$NEW_PROJECT_ID" \
    --member "$old_production_reader" --role roles/artifactregistry.reader
done
```

Section 13 removes both bindings.

### 4.2 The deployment account and Workload Identity Federation for this repo

The new project gets a deployment account used only by this repo. The dashboard
keeps its own deployment identity in the current project.

```bash
gcloud iam service-accounts create collector-deploy --project "$NEW_PROJECT_ID" \
  --display-name "microcosm-collector deployments"
gcloud iam workload-identity-pools create github --project "$NEW_PROJECT_ID" \
  --location global --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers create-oidc microcosm-collector \
  --project "$NEW_PROJECT_ID" --location global --workload-identity-pool github \
  --issuer-uri https://token.actions.githubusercontent.com \
  --attribute-mapping "google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.ref=assertion.ref,attribute.repository=assertion.repository" \
  --attribute-condition "assertion.repository=='PolicyEngine/microcosm-collector' && assertion.ref=='refs/heads/main'"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SERVICE_ACCOUNT" \
  --project "$NEW_PROJECT_ID" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$NEW_PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/PolicyEngine/microcosm-collector"
```

The mapping and condition are the ones in the README's "Google Cloud access"
section. The provider's resource name, for the `TELEMETRY_GCP_WIF_PROVIDER`
secret, is
`projects/$NEW_PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/microcosm-collector`.

What `scripts/deploy.sh` needs the deployment account to do, and the role that
allows it. Compare with the record from section 3 before granting:

| `deploy.sh` command | Needs | Grant |
|---|---|---|
| `gcloud run deploy`, `run jobs deploy` and `execute`, `services describe`, `update-traffic`, `--allow-unauthenticated` | Cloud Run administration | `roles/run.admin` on the project |
| `--service-account` for the service and the migration job | acting as those accounts | `roles/iam.serviceAccountUser` on each runtime and migration account (step 7.1) |
| `build-and-push-image` | pushing images | `roles/artifactregistry.writer` on the store |
| `check-secrets-exist` (`gcloud secrets describe`) | reading secret metadata | `roles/secretmanager.viewer` on each secret (step 7.3) |
| `create-database-backup` (production) | `cloudsql.backupRuns.create` | `roles/cloudsql.editor` on the project, or a custom role with `cloudsql.backupRuns.create` and `cloudsql.instances.get` |

The deployment account needs nothing on the load balancers: the operator
creates and changes them.

### 4.3 The export bucket

It will hold complete copies of the telemetry databases, so it is private and
is deleted after the move (section 13).

```bash
gcloud storage buckets create "$EXPORT_BUCKET" --project "$NEW_PROJECT_ID" \
  --location "$REGION" --uniform-bucket-level-access --public-access-prevention
```

## 5. C2: put the hostname in front of today's service (no downtime)

Staging first: its load balancer is the first test of the cross-project
backend service (section 15). Production's follows once staging's hostname
checks pass.

### 5.1 In the current project: a backend service for today's service

A serverless network endpoint group has to be in the same project as its
Cloud Run service, so these two resources stay in the current project until
section 13 deletes them. Its Compute Engine API was already enabled on
2026-10-22.

```bash
gcloud compute network-endpoint-groups create "$OLD_ENDPOINT_GROUP" --region "$REGION" \
  --network-endpoint-type serverless --cloud-run-service "$SERVICE" --project "$OLD_PROJECT_ID"
gcloud compute backend-services create "$OLD_BACKEND" --global \
  --load-balancing-scheme EXTERNAL_MANAGED --project "$OLD_PROJECT_ID"
gcloud compute backend-services add-backend "$OLD_BACKEND" --global \
  --network-endpoint-group "$OLD_ENDPOINT_GROUP" --network-endpoint-group-region "$REGION" \
  --project "$OLD_PROJECT_ID"
gcloud compute backend-services add-iam-policy-binding "$OLD_BACKEND" --global \
  --member "user:$OPERATOR_ACCOUNT" --role roles/compute.loadBalancerServiceUser \
  --project "$OLD_PROJECT_ID"
```

The last command lets the operator's account use this backend service from
another project's URL map. Google's external Application Load Balancer
overview says the role "can be granted at the project level or at the
resource level"; this grants it on the backend service only. Keep it until
section 13: step 8.8's rollback and section 11 use it to point the URL map
back here.

### 5.2 In the new project: the load balancer and its certificate

```bash
gcloud compute addresses create "$ADDRESS" --global --project "$NEW_PROJECT_ID"
gcloud certificate-manager dns-authorizations create "$DNS_AUTHORIZATION" \
  --domain "$COLLECTOR_HOSTNAME" --type per-project-record --project "$NEW_PROJECT_ID"
gcloud certificate-manager certificates create "$CERTIFICATE" \
  --domains "$COLLECTOR_HOSTNAME" --dns-authorizations "$DNS_AUTHORIZATION" --project "$NEW_PROJECT_ID"
gcloud certificate-manager maps create "$CERTIFICATE_MAP" --project "$NEW_PROJECT_ID"
gcloud certificate-manager maps entries create "$CERTIFICATE_MAP_ENTRY" --map "$CERTIFICATE_MAP" \
  --certificates "$CERTIFICATE" --hostname "$COLLECTOR_HOSTNAME" --project "$NEW_PROJECT_ID"
gcloud compute url-maps create "$URL_MAP" --global \
  --default-service "$OLD_BACKEND_PATH" --project "$NEW_PROJECT_ID"
gcloud compute target-https-proxies create "$HTTPS_PROXY" --url-map "$URL_MAP" \
  --certificate-map "$CERTIFICATE_MAP" --global --project "$NEW_PROJECT_ID"
gcloud compute forwarding-rules create "$FORWARDING_RULE" --global \
  --load-balancing-scheme EXTERNAL_MANAGED --address "$ADDRESS" \
  --target-https-proxy "$HTTPS_PROXY" --ports 443 --project "$NEW_PROJECT_ID"
```

The URL map's default service is the current project's backend service, named
by its full path. Google: for global external Application Load Balancers, the
frontend and URL map can reference backend services "from any project within
the same organization. No VPC network restrictions apply." The load
balancer's charges go to the new project, where the forwarding rule is.

The DNS authorization is per project: its `CNAME` has a name with a suffix
for the project, instead of a fixed name, so it never clashes with an
authorization for the same hostname in another project. For per-project
authorizations Certificate Manager issues only Google Trust Services
certificates.

### 5.3 The DNS records

Print what Anthony adds for this environment:

```bash
gcloud certificate-manager dns-authorizations describe "$DNS_AUTHORIZATION" \
  --project "$NEW_PROJECT_ID" --format='value(dnsResourceRecord.name,dnsResourceRecord.type,dnsResourceRecord.data)'
gcloud compute addresses describe "$ADDRESS" --global --project "$NEW_PROJECT_ID" --format='value(address)'
```

Anthony adds these records by hand at the domain's DNS provider, outside
Google Cloud. Four in all, two per environment:

| Environment | Type | Name | Value |
|---|---|---|---|
| staging | `CNAME` | `<staging DNS authorization name>`: the `dnsResourceRecord.name` printed above, which starts with `_acme-challenge_` and ends with the staging hostname | `<staging DNS authorization target>`: the `dnsResourceRecord.data` printed above |
| staging | `A` | `<staging hostname>` | `<staging load balancer IP>`: the address printed above |
| production | `CNAME` | `<production DNS authorization name>`, as for staging | `<production DNS authorization target>` |
| production | `A` | `<production hostname>` | `<production load balancer IP>` |

- **Plain records:** no proxying, forwarding or redirection in front of
  them. A proxy would end TLS for requests that carry Hugging Face tokens,
  and would hide the `CNAME` from Google.
- **Names:** if the provider's form adds the domain itself, enter only the
  part of each name before `.<PolicyEngine domain>`, the domain both
  hostnames end with. A trailing dot at the
  end of a printed name marks it as complete; leave it out if the form
  doesn't accept it.
- **Both records at once:** nothing uses the hostname until step 5.5, so the
  `A` record can go in before the certificate is issued.
- **They stay for good.** Certificate Manager uses the `CNAME` for renewals
  as well as the first issue, and the `A` record doesn't change during the
  move, because the static IP is already in the new project. The default TTL
  is fine.
- **CAA records:** the domain had none when checked. Any added later must
  allow Google Trust Services.

Then wait until the certificate is issued:

```bash
gcloud certificate-manager certificates describe "$CERTIFICATE" --project "$NEW_PROJECT_ID" \
  --format='value(managed.state)'
```

It must print `ACTIVE`. While it doesn't, `--format=yaml` shows
`managed.authorizationAttemptInfo`, which says whether Google found the
`CNAME`.

### 5.4 Check the hostname

```bash
curl -fsS "https://$COLLECTOR_HOSTNAME/ready"
curl --silent --output /dev/null --write-out '%{http_code}\n' \
  --request POST "https://$COLLECTOR_HOSTNAME/v1/auth/huggingface/exchange"
```

The first must succeed. The second must print `401` (no credential sent),
which shows the request reached the application through the load balancer
and its certificate. A new load balancer can take a few minutes to answer
after its certificate becomes active.

### 5.5 Point the dashboard at the hostname

Once both environments pass step 5.4:

| Setting | Where | New value |
|---|---|---|
| `MICROCOSM_TELEMETRY_COLLECTOR_URL` | calibration-diagnostics GitHub environment `Production` (variable) | `https://<production hostname>` |
| `MICROCOSM_TELEMETRY_COLLECTOR_URL` | Vercel project calibration-diagnostics, Production | `https://<production hostname>` |
| `MICROCOSM_TELEMETRY_COLLECTOR_URL` | Vercel project calibration-diagnostics, Preview | `https://<staging hostname>` |

Settings take effect on the next deployment: merge calibration-diagnostics'
next PR, and open a new Preview deployment. The build monitor should then
list collector runs as before. The read token doesn't change. To undo, set
the three values back to section 3's record and deploy again.

`MICROCOSM_TELEMETRY_COLLECTOR_URL` doesn't change again: the move changes only
what the hostname reaches.

## 6. C3 and C4: the emitter and builds

Done in the emitter and Microcosm, not here; this section says what must hold
before production's copy.

- **C3:** an emitter release from PolicyEngine/microcosm-emitter#29 and one
  constant change, in `src/microcosm_emitter/core/constants.py`:
  `https://<production hostname>` is added to `TRUSTED_COLLECTOR_ORIGINS` and
  becomes `PRODUCTION_COLLECTOR_URL`. The old default address stays in
  `TRUSTED_COLLECTOR_ORIGINS` until the old service is deleted (section 13).
  It needs only C2: the hostname already reaches today's service.
- **C4:** builds run that release. Builds that start the separately
  installed emitter pick it up at their next start, once it is installed on
  that machine or in that build image, the Modal image included. Builds
  pinned to `microcosm-emitter==0.2.1` keep the old address, which 0.2.1 has
  built in with no setting to change it, until Microcosm leaves that pin
  (H2a).

**Production's copy waits for C4.** Check that Microcosm's
`packages/microcosm-build/pyproject.toml` on `main` no longer pins 0.2.1, and
that the Modal image carries the release. In Cloud Monitoring, compare the
production service's request count (`run.googleapis.com/request_count`) with
its load balancer's (`loadbalancing.googleapis.com/https/request_count`):
once builds use the hostname, nearly every request to the service comes
through the load balancer. Requests still on the default address after
production's copy get 503 (step 8.9).

Staging's copy doesn't wait for C4: no build sends to staging by default.

## 7. C5: prepare each environment in the new project (no downtime)

Staging first. Production's resources can be created any time before its
copy.

**Before production's copy, merge none of these:** S1's and S2's PRs (#18,
#19), intake's deployment wiring, C6, or C7's PR (#16). Each needs resources
of its own, which section 12 creates once, in the new project, after the
move. Merged earlier, each would need them in the current project too.

### 7.1 Service accounts

```bash
gcloud iam service-accounts create "collector-runtime-$ENVIRONMENT_NAME" --project "$NEW_PROJECT_ID"
gcloud iam service-accounts create "collector-migration-$ENVIRONMENT_NAME" --project "$NEW_PROJECT_ID"
for service_account in "$RUNTIME_SERVICE_ACCOUNT" "$MIGRATION_SERVICE_ACCOUNT"; do
  gcloud projects add-iam-policy-binding "$NEW_PROJECT_ID" \
    --member "serviceAccount:$service_account" --role roles/cloudsql.client
  gcloud iam service-accounts add-iam-policy-binding "$service_account" \
    --project "$NEW_PROJECT_ID" --role roles/iam.serviceAccountUser \
    --member "serviceAccount:$DEPLOY_SERVICE_ACCOUNT"
done
```

If the record from section 3 shows these accounts limited further today (for
example, an IAM condition on the instance name), repeat that here.

### 7.2 Cloud SQL

Create the instance with the settings recorded in section 3, except deletion
protection, which is on for both new instances: on 2026-10-22 staging's
instance had it, but production's didn't. Where the record shows automated
backups off, as staging's did on 2026-10-22, use `--no-backup` instead of
`--backup-start-time`; only production's deployment takes a backup
(`create-database-backup`).

```bash
gcloud sql instances create "$NEW_INSTANCE" --project "$NEW_PROJECT_ID" --region "$REGION" \
  --database-version DATABASE_VERSION --edition EDITION --tier TIER \
  --storage-size DISK_SIZE --availability-type AVAILABILITY_TYPE \
  --backup-start-time BACKUP_START_TIME --deletion-protection
gcloud sql databases create "$DATABASE_NAME" --instance "$NEW_INSTANCE" --project "$NEW_PROJECT_ID"
```

Match the record's IP configuration (Cloud Run connects through the Cloud SQL
connector, so a public IP needs no authorized networks), SSL mode, database
flags, maintenance window and point-in-time recovery.

**Create the migration user and the runtime user,** with the **same names**
as on the old instance and new random passwords. Google's import guide says
every user that owns objects or was granted permissions on objects in the
dumped database must exist on the target instance, or the import fails. These
two are the only ones: production's unused third user was dropped on
2026-10-27, and the admin user exists on every new instance.

**Make each user the way its old one was made.** An export carries no roles,
so nothing corrects a role's attributes or memberships later. Made the same
way, each user's row and its creator's row match the record, so step 8.5's
comparison of the roles query shows real differences only. The record shows
how each was made:

- **Its row lists `cloudsqlsuperuser`:** Cloud SQL made it. Cloud SQL's pages
  "About PostgreSQL users and roles" and "Create and manage users" say a user
  created with `gcloud sql users create` is a member of `cloudsqlsuperuser`,
  with the `CREATEROLE`, `CREATEDB` and `LOGIN` attributes. Create it that way.
  If its row lacks one of those attributes, see "If a recorded user lacks an
  attribute" below.
- **Its row doesn't list `cloudsqlsuperuser`:** it was made in SQL, or its
  membership was taken away later. On PostgreSQL 16, a role made by a role
  that isn't a superuser, such as Cloud SQL's admin user, "is automatically
  granted back to the creating user, just as if the bootstrap superuser had
  executed the command `GRANT created_user TO creating_user WITH ADMIN TRUE,
  SET FALSE, INHERIT FALSE`" (PostgreSQL 16, "Role Attributes"). The roles
  query shows that grant as `ROLE (admin)` in the creator's `member_of`,
  normally the admin user's. Create the user in SQL, connected as that
  creator, with exactly the attributes its row shows. PostgreSQL then makes
  the same grant, so both rows match the record, and nothing has to be
  removed afterwards. If no role lists it as `(admin)`, create it as the
  admin user; step 8.5 then accepts that one extra entry in the admin user's
  row.

For the SQL, run `psql` through the Cloud SQL Auth Proxy
(`cloud-sql-proxy "$NEW_CLOUD_SQL_CONNECTION"` in another shell, listening on
port 5432 on this machine). First set the admin user's password on the new
instance, typed rather than printed:
`gcloud sql users set-password "$ADMIN_DATABASE_USER" --instance "$NEW_INSTANCE" --project "$NEW_PROJECT_ID" --prompt-for-password`.

```bash
migration_password="$(openssl rand -hex 32)"
runtime_password="$(openssl rand -hex 32)"
create_user_with_cloud_sql() {   # USER PASSWORD
  gcloud sql users create "$1" --instance "$NEW_INSTANCE" --project "$NEW_PROJECT_ID" --password "$2"
}
create_user_in_sql() {           # CREATOR USER PASSWORD 'ATTRIBUTES'
  psql "host=localhost port=5432 dbname=$DATABASE_NAME user=$1" --set ON_ERROR_STOP=1 \
    --set user_name="$2" --set user_password="$3" --set attributes="$4" <<'SQL'
CREATE ROLE :"user_name" WITH :attributes PASSWORD :'user_password';
SQL
}
# Examples: for each user, make the one call its section 3 row calls for, with
# the attributes its row shows.
create_user_with_cloud_sql "$MIGRATION_DATABASE_USER" "$migration_password"
create_user_in_sql "$ADMIN_DATABASE_USER" "$RUNTIME_DATABASE_USER" "$runtime_password" 'LOGIN NOCREATEROLE NOCREATEDB INHERIT'
```

psql puts the variables' values into the statement, quoted as a name
(`:"user_name"`), as a string (`:'user_password'`) or as written
(`:attributes`), so neither the shell's history nor psql's holds a password.
PostgreSQL's `CREATE ROLE` page warns that a password in the statement "might
also be logged in ... the server log": the server logs it if the statement
fails, or if the `log_statement` flag recorded in section 3 is `ddl` or
`all`. Keep the two password variables in this shell for step 7.3. Then grant
the other memberships the rows show, and run the roles query on the new
instance: it must already match the record, apart from the differences this
step names.

**If a recorded user lacks an attribute.** This matters only for a row that
lists `cloudsqlsuperuser` but lacks `CREATEROLE`, `CREATEDB` or `LOGIN`. The
admin user would then have to remove that attribute with `ALTER ROLE`.
PostgreSQL 16's `ALTER ROLE` page allows that, for a role with `CREATEROLE`
that isn't a superuser, "only for non-superuser and non-replication roles for
which they have been granted `ADMIN OPTION`". Cloud SQL's pages say "You can
change the attributes of any user using the ALTER ROLE command", but not
whether the admin user holds `ADMIN OPTION` on users Cloud SQL makes. So check
it once, on staging's new instance, with a throwaway user, before relying on
it:

```bash
throwaway_user='<throwaway user name>'   # any name the record doesn't use
gcloud sql users create "$throwaway_user" --instance "$NEW_INSTANCE" --project "$NEW_PROJECT_ID" \
  --password "$(openssl rand -hex 32)"
psql "host=localhost port=5432 dbname=$DATABASE_NAME user=$ADMIN_DATABASE_USER" \
  --set user_name="$throwaway_user" <<'SQL'
SELECT g.rolname AS holder, m.admin_option, m.inherit_option, m.set_option
FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.member
WHERE m.roleid = :'user_name'::regrole;
ALTER ROLE :"user_name" NOCREATEROLE NOCREATEDB NOLOGIN;
SQL
gcloud sql users delete "$throwaway_user" --instance "$NEW_INSTANCE" --project "$NEW_PROJECT_ID" --quiet
```

- **`ALTER ROLE` succeeds:** create that user with `gcloud sql users create`,
  then remove what its row lacks with `ALTER ROLE` as the admin user.
- **It fails** with "permission denied to alter role": create that user in
  SQL as the admin user instead, with the attributes its row shows. Its row
  then lacks `cloudsqlsuperuser`, and the admin user's row lists it as
  `(admin)`; step 8.5 accepts exactly these two differences for that user. It
  then has less privilege than its record. The README asks no more of the
  two users: the migration credential needs "schema-modification access",
  and the runtime credential "only the data access required by the
  application". If that user is the migration user, the "Before any import"
  steps below must also grant it `CREATE` on `public`, and step 8.5 accepts
  that difference too.

Write the result in the copy's notes.

**Before any import into the new database** (step 7.6's timing run and step
8.3), give it the record's privileges and owners. An export doesn't carry the
database-level privileges. And the importing user (the migration user) can
create the tables only if it may create objects in schema `public`: on
PostgreSQL 15 and later, `public` belongs to `pg_database_owner`, that is to
the database's owner, and other users no longer have `CREATE` on it
(PostgreSQL 15 release notes). On the old database the migration user had it
in one of three ways, which the `\l` and `\dn+ public` records show: it owns
the database or is a member of its owner (as every `cloudsqlsuperuser` member
is, if `cloudsqlsuperuser` owns it); it owns `public`; or it was granted
`CREATE` on `public`. The dump also sets `public`'s owner and privileges, but
as the importing user and only after creating the tables, so it can't give
the importing user what those tables need. Cloud SQL's users pages don't say
who owns a database that `gcloud sql databases create` makes, so compare
first.

Connected to the new database as the admin user, compare
`\l "$DATABASE_NAME"` and `\dn+ public` with the record, then apply what
differs, in this order:

1. **Privileges:** the database-level ones from `\l` (`GRANT CONNECT`,
   `GRANT CREATE ON DATABASE`) and `public`'s from `\dn+ public`
   (`GRANT USAGE, CREATE ON SCHEMA public TO ...`). The admin user may grant
   them while it acts as the owner, which it does through `cloudsqlsuperuser`
   if that owns the new database. Grant them before changing an owner: after
   that, the admin user no longer acts as the owner.
2. **Owners:** `ALTER DATABASE ... OWNER TO ...` or
   `ALTER SCHEMA public OWNER TO ...`. PostgreSQL 16's `ALTER DATABASE` page
   says: "To alter the owner, you must be able to `SET ROLE` to the new owning
   role, and you must have the `CREATEDB` privilege."
   - **If Cloud SQL made the new owner,** connect as that user: as a
     `cloudsqlsuperuser` member with `CREATEDB`, it can make itself the owner
     of a database that `cloudsqlsuperuser` owns.
   - **If the admin user made it,** the admin user holds it only as
     `(admin)`, without `SET`. The "Role Attributes" page says a creator "can
     gain access to the created role by simply granting that role back to
     themselves with the `INHERIT` and/or `SET` options", so grant `SET` for
     the change and revoke it afterwards. With the names from section 2 in
     place of the placeholders:

     ```sql
     GRANT <migration user> TO <admin user> WITH SET TRUE;
     ALTER DATABASE <database name> OWNER TO <migration user>;
     REVOKE <migration user> FROM <admin user>;
     ```

     The revoke removes only the grant the admin user made; the same page
     says the automatic grant "cannot be removed or changed by the
     `CREATEROLE` user". The roles query's admin user row must match the
     record again.

Table privileges come with the export, provided the users exist. The
migration user's default privileges are set after the import (step 8.4), so
that they don't add privileges to the tables the import creates. Step 8.5
compares all four records.

Let both instances' service agents use the bucket. Google's export and import
pages name `roles/storage.objectAdmin` for both:

```bash
for sql_service_agent in \
  "$(gcloud sql instances describe "$OLD_INSTANCE" --project "$OLD_PROJECT_ID" --format='value(serviceAccountEmailAddress)')" \
  "$(gcloud sql instances describe "$NEW_INSTANCE" --project "$NEW_PROJECT_ID" --format='value(serviceAccountEmailAddress)')"; do
  gcloud storage buckets add-iam-policy-binding "$EXPORT_BUCKET" \
    --member "serviceAccount:$sql_service_agent" --role roles/storage.objectAdmin
done
```

### 7.3 Secret Manager

```bash
for secret_name in "$DATABASE_SECRET_NAME" "$MIGRATION_DATABASE_SECRET_NAME" "$JWT_SECRET_NAME" "$READ_SECRET_NAME"; do
  gcloud secrets create "$secret_name" --project "$NEW_PROJECT_ID" --replication-policy automatic
done
```

**The two database URLs are new:** the new instance and the new passwords.
First see the form of the old value with everything up to the last `@`
hidden:

```bash
gcloud secrets versions access latest --secret "$DATABASE_SECRET_NAME" --project "$OLD_PROJECT_ID" \
  | sed -E 's#//.*@#//USER:PASSWORD@#'
```

Build the new values in the same form. For the usual Cloud SQL socket form:

```bash
printf 'postgresql://%s:%s@/%s?host=/cloudsql/%s' "$RUNTIME_DATABASE_USER" "$runtime_password" \
  "$DATABASE_NAME" "$NEW_CLOUD_SQL_CONNECTION" \
  | gcloud secrets versions add "$DATABASE_SECRET_NAME" --project "$NEW_PROJECT_ID" --data-file=-
printf 'postgresql://%s:%s@/%s?host=/cloudsql/%s' "$MIGRATION_DATABASE_USER" "$migration_password" \
  "$DATABASE_NAME" "$NEW_CLOUD_SQL_CONNECTION" \
  | gcloud secrets versions add "$MIGRATION_DATABASE_SECRET_NAME" --project "$NEW_PROJECT_ID" --data-file=-
unset runtime_password migration_password
```

**The signing secret and the read token are copied,** so session tokens issued
before the copy stay valid afterwards, and the dashboard's credential doesn't
change. The dashboard keeps reading the current project's copy of the read
token until section 9:

```bash
for secret_name in "$JWT_SECRET_NAME" "$READ_SECRET_NAME"; do
  gcloud secrets versions access latest --secret "$secret_name" --project "$OLD_PROJECT_ID" \
    | gcloud secrets versions add "$secret_name" --project "$NEW_PROJECT_ID" --data-file=-
done
```

Production's older, unused database URL secret (section 3) isn't copied.

Who may read them:

```bash
for secret_name in "$DATABASE_SECRET_NAME" "$JWT_SECRET_NAME" "$READ_SECRET_NAME"; do
  gcloud secrets add-iam-policy-binding "$secret_name" --project "$NEW_PROJECT_ID" \
    --member "serviceAccount:$RUNTIME_SERVICE_ACCOUNT" --role roles/secretmanager.secretAccessor
done
gcloud secrets add-iam-policy-binding "$MIGRATION_DATABASE_SECRET_NAME" --project "$NEW_PROJECT_ID" \
  --member "serviceAccount:$MIGRATION_SERVICE_ACCOUNT" --role roles/secretmanager.secretAccessor
for secret_name in "$DATABASE_SECRET_NAME" "$MIGRATION_DATABASE_SECRET_NAME" "$JWT_SECRET_NAME" "$READ_SECRET_NAME"; do
  gcloud secrets add-iam-policy-binding "$secret_name" --project "$NEW_PROJECT_ID" \
    --member "serviceAccount:$DEPLOY_SERVICE_ACCOUNT" --role roles/secretmanager.viewer
done
```

**The dashboard's access to the new read secret** (choice 10). Its Workload
Identity provider stays in the current project. Grant the principal (or
principals) that section 3's record lists on the old read secret the same
role on the new one, with the principal copied from the record:

```bash
gcloud secrets add-iam-policy-binding "$READ_SECRET_NAME" --project "$NEW_PROJECT_ID" \
  --member '<principal from the record>' --role roles/secretmanager.secretAccessor
```

Production's Vercel identity gets only production's read secret, and Preview's
only staging's. For production, also grant the dashboard's own deployment
identity, the account in calibration-diagnostics' `Production` secret
`TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT`, `roles/secretmanager.viewer` on the
new read secret, for its "Check collector read secret exists" step. These
grants are removed when the dashboard switches to its Vercel OIDC token (C8).

### 7.4 Create the Cloud Run service once

`deploy-maintenance` deploys with `--no-traffic`, which Cloud Run refuses when
it creates a service. So create the service once with Google's sample image.
Nothing reaches it yet, and the first deployment replaces the revision.

```bash
gcloud run deploy "$SERVICE" --project "$NEW_PROJECT_ID" --region "$REGION" \
  --image us-docker.pkg.dev/cloudrun/container/hello \
  --service-account "$RUNTIME_SERVICE_ACCOUNT" --no-allow-unauthenticated --quiet
```

The migration job needs no step: `configure-migration-job` runs
`gcloud run jobs deploy`, which creates it.

### 7.5 A backend service for the new service

Created now, used by the URL map only in step 8.8:

```bash
gcloud compute network-endpoint-groups create "$NEW_ENDPOINT_GROUP" --region "$REGION" \
  --network-endpoint-type serverless --cloud-run-service "$SERVICE" --project "$NEW_PROJECT_ID"
gcloud compute backend-services create "$NEW_BACKEND" --global \
  --load-balancing-scheme EXTERNAL_MANAGED --project "$NEW_PROJECT_ID"
gcloud compute backend-services add-backend "$NEW_BACKEND" --global \
  --network-endpoint-group "$NEW_ENDPOINT_GROUP" --network-endpoint-group-region "$REGION" \
  --project "$NEW_PROJECT_ID"
```

### 7.6 Production only: time the database copy

Before production's copy, export the production database with `--offload`
(a serverless export, billed separately, which doesn't load the serving
instance), import it into the new instance as in step 8.3, and note both
durations. Then delete and re-create the new database
(`gcloud sql databases delete`, then `create`), so the copy's import starts
empty, and repeat step 7.2's "Before any import" steps (privileges, then
owners): deleting the database drops them. Staging's copy gives the same
timings for staging's size.

## 8. C5: copy each environment

Staging first, end to end, verified (section 10). Production's copy follows
once C4 holds (section 6). Nothing is scheduled or announced in advance: the
operator starts each copy when ready. From step 8.1 until step 8.8's checks
pass, nothing else merges to this repo's `main`, because every merge
deploys.

**How long the service is in maintenance mode:** from step 8.2 until step
8.8's URL map change. Exporting and importing take minutes at today's size
(10 GB disks). Most of the time is the release that step 8.7 starts: the
merge's Push run (versioning), then the release commit's Push run (checks,
tag, staging's deployment, then production's). From merge to tag this took
25 minutes on 2026-10-15 and 88 minutes on 2026-10-19 and 2026-10-20, when the
organization's runners were queued; on 2026-10-20, calibration-diagnostics'
last collector deploy run took 2 hours 25 minutes in all, of which its staging
and production deploy jobs ran for 4 and 6 minutes. So expect about 40
minutes on a quiet day (25 minutes to the tag, about 10 for the two deploy
jobs, and the export and import) and 2 to 3 hours when runners are queued.
Staging's copy measures it.

While it lasts, requests to that environment, through the hostname or the
default address, get HTTP 503:

- **Builds** (production only): the emitter retries, with a backoff of up to
  60 seconds. Events wait on the build machine (kept for seven days, up to
  100 MiB). A machine discarded when its build ends, such as a Modal
  container, loses the events not sent by then. A build never fails because
  of telemetry.
- **The dashboard:** the build monitor lists `collector` under "Runs that
  could not be read" and still shows the Hugging Face runs. Its reads
  recover at the URL map change, with no dashboard deployment.
- **Orrery uploads:** the emitter gets their credential from the collector's
  token exchange, so they wait. They already wait today, because
  microcosm-runs' authorization call needs a collector endpoint that doesn't
  exist, so nothing changes for them.

### 8.1 Check that no deployment is running, and record the rollback target

```bash
gh run list -R PolicyEngine/microcosm-collector --workflow push.yaml --status in_progress
gh run list -R PolicyEngine/microcosm-collector --workflow push.yaml --status queued
```

Both must list nothing. Then record the revision the old service serves now.
It is the rollback target (section 11). Every merge since section 3 has
redeployed production, possibly with migrations, so an earlier revision may
not match the database schema.

```bash
old_service_json=$(gcloud run services describe "$SERVICE" --project "$OLD_PROJECT_ID" \
  --region "$REGION" --format=json)
rollback_revision=$(jq -r '.status.traffic[] | select((.percent // 0) > 0) | .revisionName' \
  <<< "$old_service_json")
echo "$rollback_revision"
jq -r '.status.latestCreatedRevisionName' <<< "$old_service_json"
```

Both must print the same one revision name. If they differ, the last
deployment didn't finish (its maintenance or candidate revision is newer
than the serving one): resolve that before going on. Write the revision in
the copy's notes too.

### 8.2 Put the old service in maintenance mode

`services update` makes a new revision from the latest created revision's
settings, which step 8.1 checked are the serving revision's, with only
`TELEMETRY_MAINTENANCE_MODE` changed, so it keeps the same image, secrets and
database:

```bash
gcloud run services update "$SERVICE" --project "$OLD_PROJECT_ID" --region "$REGION" \
  --update-env-vars TELEMETRY_MAINTENANCE_MODE=1 --no-traffic --tag project-move --quiet
gcloud run services update-traffic "$SERVICE" --project "$OLD_PROJECT_ID" --region "$REGION" \
  --to-tags project-move=100 --quiet
curl --silent --output /dev/null --write-out '%{http_code}\n' \
  --request POST "https://$COLLECTOR_HOSTNAME/v1/auth/huggingface/exchange"
sleep 35
```

The `curl` must print `503`, as `resolve-and-verify-maintenance` checks, here
through the hostname. The pause outlasts the 30-second request timeout, so no
request to the service is still writing.

Intake is off (#12 merged with it off, and its deployment wiring doesn't
exist yet), so nothing else writes to the database. From here the old
database doesn't change.

### 8.3 Export and import

```bash
export_object="$EXPORT_BUCKET/$ENVIRONMENT_NAME-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
gcloud sql export sql "$OLD_INSTANCE" "$export_object" \
  --database "$DATABASE_NAME" --project "$OLD_PROJECT_ID"
gcloud sql import sql "$NEW_INSTANCE" "$export_object" \
  --database "$DATABASE_NAME" --user "$MIGRATION_DATABASE_USER" --project "$NEW_PROJECT_ID" --quiet
```

Import as the migration user, which owns the tables in section 3's record, so
the restored objects keep their owner. Step 7.2's "Before any import" steps
let it create them in `public`.

### 8.4 Set the migration user's default privileges

The migrations grant nothing: the runtime user gets access to every table a
migration creates from the migration user's default privileges (the README's
"Database access" section). Both environments have them today, but an entry
belongs to the role, not to its name, and this one is new. Set them on the new
database now, before step 8.7's deployment runs any migration there. If the
import already carried the entry, running the statement again changes nothing.

Connected to the new database as the admin user. On PostgreSQL 16 the admin
user needs the `INHERIT` option on the migration user first, so grant it, set
the default privileges, then revoke the grant. With the names from section 2
in place of the placeholders:

```sql
GRANT <migration user> TO <admin user> WITH INHERIT TRUE;
ALTER DEFAULT PRIVILEGES FOR ROLE <migration user> IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO <runtime user>;
REVOKE <migration user> FROM <admin user>;
```

The grant needs `ADMIN` on the migration user, which the admin user holds if
it created that user in SQL (its row lists the migration user as `(admin)`).
If it doesn't, connect as the migration user instead, whose password is in
the new `$MIGRATION_DATABASE_SECRET_NAME` secret, and run only the
`ALTER DEFAULT PRIVILEGES` statement.

Then check with `\ddp`: it lists an entry owned by the migration user for
schema `public` and type `table`, with
`<runtime user>=arwd/<migration user>`, as in section 3's record.

### 8.5 Check the copy

On both instances, as the migration user:

- `SELECT version_num FROM alembic_version;` gives the same revision
  (`0002_artifact_intake` since #12).
- `SELECT count(*)` on every table in the `public` schema gives the same
  counts. At revision `0002_artifact_intake` these are `telemetry_runs`,
  `telemetry_producers`, `telemetry_events`, `artifact_submissions` and
  `artifact_jobs`; the two intake tables are empty while intake is off.
- `\l`, `\dn+ public`, `\dp public.*` and `\ddp` match section 3's record, so
  the database's owner and privileges, `public`'s owner and privileges, and
  the table and default privileges all came across.
- The roles query from section 3 gives the same rows, the admin user's
  included, so every role has the same attributes and the same memberships
  with the same options. Step 7.2 already compared them before the copy; the
  only accepted differences are the ones it names for a user it had to make
  differently, as written in the copy's notes.

If anything else differs, stop and roll back (section 11, first row).

### 8.6 Point the GitHub environment at the new project

```bash
collector_settings=(--repo PolicyEngine/microcosm-collector --env "$ENVIRONMENT_NAME")
gh variable set TELEMETRY_GCP_PROJECT_ID "${collector_settings[@]}" --body "$NEW_PROJECT_ID"
gh variable set TELEMETRY_CLOUD_SQL_CONNECTION "${collector_settings[@]}" --body "$NEW_CLOUD_SQL_CONNECTION"
gh variable set TELEMETRY_RUNTIME_SERVICE_ACCOUNT "${collector_settings[@]}" --body "$RUNTIME_SERVICE_ACCOUNT"
gh variable set TELEMETRY_MIGRATION_SERVICE_ACCOUNT "${collector_settings[@]}" --body "$MIGRATION_SERVICE_ACCOUNT"
gh secret set TELEMETRY_GCP_WIF_PROVIDER "${collector_settings[@]}" \
  --body "projects/$NEW_PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/microcosm-collector"
gh secret set TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT "${collector_settings[@]}" --body "$DEPLOY_SERVICE_ACCOUNT"
```

In production's copy, also set `TELEMETRY_PRODUCTION_CLOUD_SQL_CONNECTION` to
production's new connection in **both** environments, since staging's
`validate-environment-target` compares against it. The other variables keep
their values, because the new resources reuse the old names (section 14).
If section 16's `TELEMETRY_PUBLIC_ORIGIN` check exists, unset that variable in
this environment now: until step 8.8 the hostname reaches the old service,
whose `/ready` answers 503 in maintenance mode.

### 8.7 Deploy

Each copy needs its own merge. Its release runs the Push workflow, which
deploys staging and then production with the environments' current settings.
Prepare one PR for each copy in advance (section 16). This repo requires
branches to be up to date with `main`, so update it and let its checks pass
before step 8.1.

- **Staging's copy:** staging deploys into the new project. Production then
  redeploys in the current project with unchanged runtime code, from the
  image staging built in the new project's store. That works only with the
  Artifact Registry Reader grant in step 4.1; it is otherwise a normal
  deployment, with its usual short maintenance phase.
- **Production's copy:** staging redeploys in the new project, then
  production deploys into it.

Re-running the deployment jobs of the latest Push run might do the same
without a merge, but whether a re-run reads the environments' changed values
hasn't been checked. In staging's copy, confirm it from the run log (the
"Require deployment configuration" step shows `PROJECT_ID`) before relying on
it for production. In production, a re-run that used the old values would take
the old service out of maintenance mode, on the old database.

The first deployment replaces the sample revision with a maintenance revision,
runs the migration job (the restored database is already at the current
revision, and the job checks the schema against the SQLAlchemy metadata),
deploys and checks a candidate revision, moves traffic to it, and checks the
service's default address ("Verify the stable service address"). In
production it also takes a backup of the new instance first.

### 8.8 Send the hostname to the new service

After the deployment has finished, change the URL map's default service to
the new project's backend service:

```bash
gcloud compute url-maps set-default-service "$URL_MAP" --global \
  --default-service "$NEW_BACKEND" --project "$NEW_PROJECT_ID"
```

Then run step 5.4's checks again: `/ready` must succeed and the token exchange
must print `401`. A URL map change can take a few minutes to apply
everywhere; until then some requests still reach the old service and get 503,
which the emitter retries. The DNS records don't change.

If section 16's `TELEMETRY_PUBLIC_ORIGIN` check exists, set that variable
again in this environment.

**To undo this step** (section 11), point the URL map back:

```bash
gcloud compute url-maps set-default-service "$URL_MAP" --global \
  --default-service "$OLD_BACKEND_PATH" --project "$NEW_PROJECT_ID"
```

### 8.9 The old service after the copy

It stays in maintenance mode (choice 7), so it answers 503 at its default
address. After production's copy, builds still on that address, on emitter
0.2.1 or on stale machines, keep their events locally and send them through
a later emitter on the same machine; events on discarded machines, such as
Modal containers, are lost. The old service keeps one instance running
(`--min 1`) until section 13 deletes it.

**From the move on, never run calibration-diagnostics'
`telemetry-service.yml`,** for rollback or anything else. Its settings name
the old project, and its code predates migration `0002_artifact_intake`, so
it can't run against either database. calibration-diagnostics#234 removes
it.

## 9. After each copy: the dashboard reads the new project's read secret

In an ordinary dashboard deployment, not during a copy. Until then the
dashboard reads the current project's copy of the read token, which has the
same value. Step 7.3 has already granted the access this needs.

| Setting | Where | New value | When |
|---|---|---|---|
| `MICROCOSM_TELEMETRY_READ_SECRET_VERSION` | Vercel, Preview | staging's new read secret version, `projects/<new project>/secrets/<staging read secret name>/versions/<version>`, naming the project as section 3's recorded value does (by ID or number), with the version step 7.3 added (`gcloud secrets versions list` shows it) | after staging's copy |
| `MICROCOSM_TELEMETRY_READ_SECRET_VERSION` | Vercel, Production | the same, for production's read secret | after production's copy |
| `TELEMETRY_GCP_PROJECT_ID` | calibration-diagnostics GitHub environment `Production` (variable) | `<new project ID>` | with Vercel Production |
| `TELEMETRY_READ_SECRET_NAME` | calibration-diagnostics GitHub environment `Production` (variable) | unchanged, because the name was reused | — |

The read secrets' names are this repo's `TELEMETRY_READ_SECRET_NAME`
variables. Merge calibration-diagnostics' next PR, and open a new Preview
deployment; the build monitor should still list collector runs.
`MICROCOSM_TELEMETRY_GOOGLE_IDENTITY_PROVIDER`, `TELEMETRY_GCP_WIF_PROVIDER`
and `TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT` don't change. To undo, set the
values back to section 3's record. Until this is done, section 13 keeps the
old read secrets.

## 10. Verify

For each environment, after its copy:

- `gcloud run services describe "$SERVICE" --project "$NEW_PROJECT_ID" --region "$REGION" --format=json`
  shows an image in the new store, tagged with this repo's release commit (whose
  message is "Update microcosm-collector"), the new Cloud SQL connection, and
  the four secret names.
- `curl -fsS "$(gcloud run services describe "$SERVICE" --project "$NEW_PROJECT_ID" --region "$REGION" --format='value(status.url)')/ready"`
  succeeds, and so do step 5.4's hostname checks.
- `gcloud compute url-maps describe "$URL_MAP" --global --project "$NEW_PROJECT_ID" --format='value(defaultService)'`
  ends with `$NEW_BACKEND`.
- `gcloud run jobs executions list --job "$MIGRATION_JOB" --project "$NEW_PROJECT_ID" --region "$REGION" --limit 1`
  shows a successful execution.
- Production: `gcloud sql backups list --instance "$NEW_INSTANCE" --project "$NEW_PROJECT_ID" --limit 1 --format='table(id,windowStartTime,status,description)'`
  shows `telemetry-release-RUN_ID-ATTEMPT` for the Push run. The default
  columns leave out the description.
- The row counts from step 8.5 still match, and `\ddp` still shows step
  8.4's entry.
- The build monitor lists runs from before the copy and doesn't list
  `collector` under "Runs that could not be read".
- After production's copy: the next build that starts the C3 emitter shows
  its events in the build monitor.
- The old service answers `503` at its default address.

## 11. Rollback

Keep every old resource until section 13. The old database is unchanged from
the moment the old service entered maintenance mode. Rollback never changes
the DNS records or the dashboard's `MICROCOSM_TELEMETRY_COLLECTOR_URL`.

| Situation | What to do |
|---|---|
| During a copy, before step 8.8's URL map change | Route the old service back: `gcloud run services update-traffic "$SERVICE" --project "$OLD_PROJECT_ID" --region "$REGION" --to-revisions "$rollback_revision=100"`, with the revision step 8.1 recorded in this copy. Set back, to section 3's record, only the GitHub values step 8.6 changed in this copy: in staging's copy, staging's; in production's copy, production's and `TELEMETRY_PRODUCTION_CLOUD_SQL_CONNECTION` in staging's environment. Staging's other values stay pointed at the new project, where its database now is. The hostname still reaches the old service, so builds and the dashboard recover with no other change. Builds kept their events and deliver them, except builds on a machine discarded when the build ended, such as a Modal container. |
| A deployment into the new project fails | As in calibration-diagnostics#233: before the migration step, fix and re-run the failed jobs. The URL map hasn't changed, so the first row also applies. |
| After step 8.8's URL map change | New events are in the new database, so prefer fixing forward. To go back: put the new service in maintenance mode (step 8.2's commands with `--project "$NEW_PROJECT_ID"`); export the new database; on the old instance, delete and re-create the old database, repeat step 7.2's "Before any import" steps there, import, and repeat step 8.4 there (deleting a database drops its default privileges); route the old service back to the revision step 8.1 recorded; set the GitHub values back as in the first row; then point the URL map back to `$OLD_BACKEND_PATH` (step 8.8's undo command). If a release with a migration has deployed in the new project since, the recorded revision's code is older than the imported schema: set the GitHub values back first, and let the next merge deploy the current release to the current project instead. If section 9 has moved the dashboard's read secret setting, it can stay: both projects' read tokens have the same value. |

Whatever the situation, never use calibration-diagnostics'
`telemetry-service.yml` (step 8.9).

## 12. After the move

Once production's copy is verified, in this order. Each creates its cloud
resources once, in the new project, and needs nothing in the current project.

1. **The collector's own Hugging Face token and its daily check (S1, S2;
   #13, #18, #19).** Before merging each PR, create in the new project what
   its README section names. For S1: the secret, readable only by the
   account the worker jobs run as, which they read as
   `COLLECTOR_HF_READ_TOKEN`. For
   S2: production's check job, its Cloud Scheduler job and the account it
   starts the job as, a notification channel and two Cloud Monitoring alert
   policies, and its production variables (`TELEMETRY_WORKER_SERVICE_ACCOUNT`,
   `TELEMETRY_HF_CHECK_JOB`, `TELEMETRY_HF_READ_SECRET_NAME`). Enable
   `cloudscheduler.googleapis.com` and `monitoring.googleapis.com`.
2. **Switching intake on.** Everything in the README's "Artifact intake"
   section: the bucket, the Cloud Tasks queue, the worker and recovery jobs,
   the recovery job's schedule, the accounts and permissions, and the
   deployment wiring that sets `COLLECTOR_INTAKE_ENABLED`. Enable
   `cloudtasks.googleapis.com`. The runtime user's access to the intake
   tables comes from the default privileges (step 8.4).
3. **Read API v2 (C6).**
4. **ES256 session-token signing (C7, #16).** Before merging it, create each
   environment's signing-key secret, named by
   `TELEMETRY_JWT_SIGNING_KEY_SECRET_NAME`, readable by the runtime account
   and visible to the deployment account (`roles/secretmanager.viewer`). The
   deployment checks that it exists and mounts it as
   `TELEMETRY_JWT_SIGNING_KEY`; without it, that environment's deployment
   stops at "Check required Secret Manager entries exist".

## 13. Retire the old resources

When no build uses emitter 0.2.1 (Microcosm's `main` no longer pins it) and
the old production service's request count in Cloud Monitoring
(`run.googleapis.com/request_count`) has been zero for seven days, the
emitter's local retention period (choice 7). Then, in the current project
unless stated:

- The old Cloud SQL instances: export a last copy into the bucket if wanted,
  then `gcloud sql instances patch "$OLD_INSTANCE" --project "$OLD_PROJECT_ID" --no-deletion-protection`
  and `gcloud sql instances delete`.
- The old Cloud Run services and migration jobs, and the old runtime and
  migration service accounts.
- Every collector secret section 3 recorded: the four per environment, and
  production's older, unused database URL secret. Delete the old read secrets
  only once section 9 has moved the dashboard to the new ones.
- Each environment's backend service and serverless network endpoint group
  from step 5.1, and the operator's `roles/compute.loadBalancerServiceUser`
  grant on them. After this, the URL map can no longer be pointed back.
- The collector's images in the old Artifact Registry store (only the
  collector's image name, unless the store holds nothing else).
- The two `roles/artifactregistry.reader` bindings on the new project from
  step 4.1 (the old deployment account and the current project's Cloud Run
  service agent).
- This repo's Workload Identity provider in the current project
  (`github/microcosm-collector`) and the old deployment account's
  `roles/iam.workloadIdentityUser` binding for this repo's principal set.
  Keep calibration-diagnostics' provider and the shared deployment account:
  the dashboard's deployment still uses them. Keep the dashboard's Vercel
  identity provider until C8.
- The export bucket and every dump in it (in the new project).
- calibration-diagnostics' leftover collector settings, as listed in
  calibration-diagnostics#233's "Manual steps".
- In a later emitter release, the old default address from
  `TRUSTED_COLLECTOR_ORIGINS`.

## 14. Settings this runbook changes

| Setting | Provider | Read by | Changes |
|---|---|---|---|
| `TELEMETRY_GCP_PROJECT_ID` | this repo's GitHub environments `staging` and `production` (variable) | `push.yaml` job `env:` as `PROJECT_ID`, then every `scripts/deploy.sh` command | the new project ID, in step 8.6 |
| `TELEMETRY_CLOUD_SQL_CONNECTION` | same (variable) | `CLOUD_SQL_CONNECTION`: `--add-cloudsql-instances`, `--set-cloudsql-instances`, `create-database-backup` | the new connection, `<new project ID>:<region>:<instance>`, in step 8.6 |
| `TELEMETRY_PRODUCTION_CLOUD_SQL_CONNECTION` | same, in both environments (variable) | `validate-environment-target` | production's new connection, in production's copy |
| `TELEMETRY_RUNTIME_SERVICE_ACCOUNT`, `TELEMETRY_MIGRATION_SERVICE_ACCOUNT` | same (variables) | `--service-account` for the service and the migration job | the new accounts, in step 8.6 |
| `TELEMETRY_GCP_REGION`, `TELEMETRY_ARTIFACT_REGISTRY_NAME`, `TELEMETRY_IMAGE_NAME`, `TELEMETRY_SERVICE`, `TELEMETRY_PRODUCTION_SERVICE`, `TELEMETRY_MIGRATION_JOB`, and the four `TELEMETRY_*_SECRET_NAME` variables | same (variables) | image path, Cloud Run names, `--set-secrets`, `check-secrets-exist` | unchanged, because the new resources reuse the names |
| `TELEMETRY_GCP_WIF_PROVIDER` | same (secret) | `google-github-actions/auth`, `workload_identity_provider` | the new provider's resource, in step 8.6 |
| `TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT` | same (secret) | `google-github-actions/auth`, `service_account` | the new deployment account, in step 8.6 |
| `TELEMETRY_PUBLIC_ORIGIN` (only once section 16's check exists) | same (variable) | the proposed deployment check | `https://<that environment's hostname>` after step 5.4; unset in step 8.6 and set again in step 8.8 |
| `COLLECTOR_DEPLOY_ENABLED` | this repo's variable | the deployment jobs' `if:` | unchanged (`true`) |
| `roles/artifactregistry.reader` on the new project for the old deployment account and the current project's Cloud Run service agent | Google Cloud IAM (step 4.1) | Cloud Run, when `deploy-production` deploys staging's image into the current project | removed in section 13 |
| `DATABASE_URL`, `TELEMETRY_JWT_SECRET`, `TELEMETRY_READ_TOKEN` | Google Secret Manager in the new project | the service, through `--set-secrets` | new database URL; copied signing secret and read token |
| `DATABASE_URL` (migration job) | Secret Manager in the new project | the migration job, through `--set-secrets` | new migration database URL |
| `TELEMETRY_MAINTENANCE_MODE` | `scripts/deploy.sh` (`--set-env-vars`), and step 8.2 on the old service | the service | unchanged by deployments; `1` on the old service from step 8.2 |
| `MICROCOSM_TELEMETRY_COLLECTOR_URL` | calibration-diagnostics GitHub `Production` environment (variable), and the Vercel project's Production and Preview settings | `deploy.yml` and `deploy-dashboard.sh` (production); Vercel (Preview); `telemetry-collector-client.ts` | the hostnames, in step 5.5 |
| `MICROCOSM_TELEMETRY_READ_SECRET_VERSION` | Vercel project, Production and Preview | `telemetry-collector-client.ts` | the new project's read secrets, in section 9 |
| `TELEMETRY_GCP_PROJECT_ID`, `TELEMETRY_READ_SECRET_NAME` (dashboard) | calibration-diagnostics GitHub `Production` environment (variables) | `deploy.yml` "Check collector read secret exists" | the new project, in section 9; the name is unchanged |
| `PRODUCTION_COLLECTOR_URL`, `TRUSTED_COLLECTOR_ORIGINS` | emitter source (`core/constants.py`) | every emitter service module | the production hostname, by the C3 release |

Settings that section 12's work adds (S1, S2, intake, C7) are set up then, in
the new project only.

## 15. Unverified before staging's work

Apart from some read-only commands against the current project, nothing in
this runbook has been run. Still open:

- **The cross-project backend service with a Cloud Run backend.** Google's
  overview says cross-project service referencing works with serverless
  network endpoint groups and needs no Shared VPC for global external
  Application Load Balancers, but its step-by-step cross-project example uses
  an instance group. Staging's load balancer (section 5) tests it first. If
  it fails, staging's and production's hostname phase would need the whole
  load balancer in the current project, and each move a change of the `A`
  record: a change to these decisions, to agree before going further.
- **Organization policies:** the new project inherits the organization's
  policies. In the current project, which sits directly under the
  organization, both collector services grant `roles/run.invoker` to
  `allUsers`, as `--allow-unauthenticated` needs, so the organization
  probably doesn't enforce domain-restricted sharing. Whether it restricts
  cross-project backend services wasn't checked: the Organization Policy API
  isn't enabled in the current project. If the new project refuses
  `allUsers`, step 8.7's deployment can't reach its maintenance revision.
- **VPC Service Controls:** whether either project is inside a service
  perimeter, which could block the URL map's use of the current project's
  backend service (step 5.2), and the current project's image reads from the
  new Artifact Registry store (step 4.1). Checking needs organization-level
  access.
- **Whether the admin user may change a user Cloud SQL made** with
  `ALTER ROLE`, which PostgreSQL 16 allows only with `ADMIN OPTION` on that
  user. Step 7.2's check settles it, if a recorded user needs it.
- **Database owners:** who owns a database that `gcloud sql databases create`
  makes, and the old databases' owners, `public` privileges, roles, grants and
  memberships. These need a database connection; section 3 records the old
  ones, and step 7.2 compares the new database before any import.
- **Whether the export carries the default privileges.** Step 8.4 sets them
  either way.
- **How long a URL map change takes** to reach every Google front end (step
  8.8); staging's copy measures it.
- The current deployment account's roles and the dashboard identity's bindings
  (recorded in section 3), and whether a workflow re-run reads changed
  environment values (step 8.7).

Every setting above needs checking end to end in staging's pass.

## 16. Code changes that go with this runbook (not yet written)

- **This repo, optional:** a step after "Verify the stable service address"
  that checks `https://$PUBLIC_ORIGIN/ready`, from a new environment variable
  `TELEMETRY_PUBLIC_ORIGIN`, so every deployment proves the hostname reaches
  the deployed service. The step skips when the variable is unset. It can be
  set once step 5.4 passes, because the hostname then reaches the service
  this repo deploys; steps 8.6 and 8.8 unset and set it around each copy.
  Its merge can be the one staging's copy needs (step 8.7).
- **This repo, for production's copy:** the README's "Cloud Run deployment"
  and "Google Cloud access" sections updated to describe the new project and
  the load balancer, without naming the hostname. Its merge is the one
  production's copy needs (step 8.7).
- **The emitter:** C3 (section 6), and later the removal of the old default
  address from `TRUSTED_COLLECTOR_ORIGINS` (section 13).
- **calibration-diagnostics#234:** removes `telemetry-service.yml` (step
  8.9).
- **microcosm-runs:** nothing until O7 and O8 remove the code that uses the
  old address (choice 11).
