# Moving the collector to its own project and hostname (runbook)

This is plan item C2. Use it only when both of these hold:

- **The collector deploy switch-over (C1) is verified:** this repo deploys
  staging and production, and every check in the "Verify the deployment from
  microcosm-collector" section of calibration-diagnostics#233 has passed.
- **Decisions D2 and D3 are made.** See
  [`project-and-domain-decision.md`](project-and-domain-decision.md); the
  numbered choices there are referred to below as "choice N".

The runbook assumes D2 option B (a dedicated project, one maintenance window
per environment) and a custom hostname (D3 B, Cloud Run domain mapping, or D3
C, a load balancer). Steps that depend on a choice say so. It hasn't been
rehearsed. Moving staging first is the rehearsal, so check each command against
the decided names there before running it for production.

No command here prints a secret value. Values are generated in the shell and
piped into Secret Manager, or piped from one Secret Manager secret into
another.

## 1. People and access

- **Google Cloud, the new project:** permission to create a project under the
  chosen parent and link the billing account; then the owner role on the new
  project.
- **Google Cloud, `policyengine-api`:** the steps change resources there, not
  only read them. The project owner role covers all of these, if that is
  simpler for the windows:

  | Steps | What they do in `policyengine-api` | Role |
  |---|---|---|
  | 2, 3.2, 6.1, 9 | read resources and their IAM policies | `roles/viewer` and `roles/iam.securityReviewer` |
  | 3.2, 5.3 | read the collector's secret values (step 3.2's database connection uses the migration user's password) | `roles/secretmanager.secretAccessor` on the collector's secrets |
  | 3.2, 5.2, 5.5, 6.3, 11 | connect to and export the old databases; delete the old instances | `roles/cloudsql.admin` |
  | 5.3, 11 | change the intake bucket's IAM policy (#12) | `roles/storage.admin` on that bucket |
  | 6.2, 8.4, 10, 11 | update the old service and its traffic; run and cancel job executions; delete the old services and jobs | `roles/run.admin`, and `roles/iam.serviceAccountUser` on the old runtime account, because a service update deploys a revision that runs as it |
  | 6.2, 10, 11 | pause, resume and delete #12's queue | `roles/cloudtasks.queueAdmin` |
  | 6.2, 6.7, 10, 11 | pause, resume and delete #12's and #19's schedules | `roles/cloudscheduler.admin` |
  | 6.7, 10, 11 | disable, enable and delete #19's alert policies; delete its notification channel | `roles/monitoring.alertPolicyEditor` and `roles/monitoring.notificationChannelEditor` |
  | 11 | delete secrets, service accounts, this repo's Workload Identity provider and the collector's images | `roles/secretmanager.admin`, `roles/iam.serviceAccountAdmin`, `roles/iam.workloadIdentityPoolAdmin` and `roles/artifactregistry.repoAdmin` |
- **DNS:** edit access to the chosen zone (choice 5). For D3 B only, a Google
  account that is a verified owner of the base domain in Search Console; D3
  C's certificate needs only a DNS record.
- **GitHub:** an admin of PolicyEngine/microcosm-collector (environment
  settings), and someone who can change the calibration-diagnostics
  `Production` environment and merge in both repos.
- **Vercel:** access to the calibration-diagnostics project's settings.
- **The emitter:** someone who can merge in PolicyEngine/microcosm-emitter
  (each merge publishes a release).

## 2. Names used below

Set these once per environment, in a `bash` shell. Every name a choice decides
is marked.

```bash
OLD_PROJECT_ID=policyengine-api
OLD_PROJECT_NUMBER=$(gcloud projects describe "$OLD_PROJECT_ID" --format='value(projectNumber)')
OLD_DEPLOY_SERVICE_ACCOUNT=...   # the current deployment account, from step 3.2
NEW_PROJECT_ID=...           # choice 2
ENVIRONMENT_NAME=staging     # then production
COLLECTOR_HOSTNAME=...       # choice 4, for this environment

collector_variable() {
  gh api "repos/PolicyEngine/microcosm-collector/environments/$ENVIRONMENT_NAME/variables/$1" --jq .value
}
REGION=$(collector_variable TELEMETRY_GCP_REGION)
SERVICE=$(collector_variable TELEMETRY_SERVICE)
MIGRATION_JOB=$(collector_variable TELEMETRY_MIGRATION_JOB)
ARTIFACT_REGISTRY_NAME=$(collector_variable TELEMETRY_ARTIFACT_REGISTRY_NAME)
OLD_CLOUD_SQL_CONNECTION=$(collector_variable TELEMETRY_CLOUD_SQL_CONNECTION)
OLD_INSTANCE=${OLD_CLOUD_SQL_CONNECTION##*:}
DATABASE_SECRET_NAME=$(collector_variable TELEMETRY_DATABASE_SECRET_NAME)
MIGRATION_DATABASE_SECRET_NAME=$(collector_variable TELEMETRY_MIGRATION_DATABASE_SECRET_NAME)
JWT_SECRET_NAME=$(collector_variable TELEMETRY_JWT_SECRET_NAME)
READ_SECRET_NAME=$(collector_variable TELEMETRY_READ_SECRET_NAME)
HF_CHECK_JOB=$(collector_variable TELEMETRY_HF_CHECK_JOB)   # production, once #19 has merged

# Names are scoped to a project, so the new resources can reuse the old ones.
NEW_INSTANCE=$OLD_INSTANCE
NEW_CLOUD_SQL_CONNECTION="$NEW_PROJECT_ID:$REGION:$NEW_INSTANCE"
RUNTIME_SERVICE_ACCOUNT="collector-runtime-$ENVIRONMENT_NAME@$NEW_PROJECT_ID.iam.gserviceaccount.com"
MIGRATION_SERVICE_ACCOUNT="collector-migration-$ENVIRONMENT_NAME@$NEW_PROJECT_ID.iam.gserviceaccount.com"
DEPLOY_SERVICE_ACCOUNT="collector-deploy@$NEW_PROJECT_ID.iam.gserviceaccount.com"
EXPORT_BUCKET="gs://$NEW_PROJECT_ID-collector-move"
```

`DATABASE_NAME`, `RUNTIME_DATABASE_USER`, `MIGRATION_DATABASE_USER` and
`OTHER_DATABASE_USERS` come from the old instance in step 3.2.

## 3. Before the windows

### 3.1 Check the prerequisites

- `COLLECTOR_DEPLOY_ENABLED` is `true`, and C1 is verified.
- calibration-diagnostics#234 has merged, or everyone has agreed never to
  dispatch calibration-diagnostics' `telemetry-service.yml` again. Its settings
  name the old project.
- PolicyEngine/microcosm-emitter#29 has merged, so changing the emitter's
  default is a constant change (section 8.2).
- List what has merged here that needs its own resources in the new project,
  and add those steps where this runbook marks them:
  - #12's artifact intake: a Cloud Tasks queue, the worker job, the recovery
    job and its Cloud Scheduler job, the invoker account and their
    permissions, as in the README's "Artifact intake" section. Its bucket
    stays in the old project (step 5.3), which ties the collector's files to
    `policyengine-api` (the memo's D2 B). Intake was off on 2026-10-20: the
    Cloud Tasks and Cloud Scheduler APIs weren't enabled there, and no intake
    bucket existed. Switching it on only after the move avoids this;
  - S1's Hugging Face read token (#13, PR #18);
  - C5's session signing key (#16), a fifth Secret Manager secret per
    environment;
  - S2's daily check (#19): production's check job, its Cloud Scheduler job
    and the account that job runs as, a notification channel and two Cloud
    Monitoring alert policies, and three production variables.

### 3.2 Record the current state

For each environment (read-only):

- The service's URL, serving revision, image, Cloud SQL connection and secret
  names: the `gcloud run services describe` command in step 0 of
  calibration-diagnostics#233. This serving revision isn't the rollback
  target: production redeploys between the windows, so step 6.1 records the
  target again just before each window's changes.
- The instance's settings:
  `gcloud sql instances describe "$OLD_INSTANCE" --project "$OLD_PROJECT_ID" --format=yaml`.
  Keep the database version, edition, tier, disk size, availability type,
  backup and point-in-time-recovery settings, maintenance window, database
  flags, and IP and SSL configuration.
- The database and its users:
  `gcloud sql databases list` and `gcloud sql users list` with
  `--instance "$OLD_INSTANCE" --project "$OLD_PROJECT_ID"`. On 2026-10-20
  both instances held the databases `postgres` and `telemetry`, and listed
  these users:

  | Environment | Users |
  |---|---|
  | staging | `postgres`, `telemetry_migrator`, `telemetry_runtime` |
  | production | `postgres`, `telemetry_app`, `telemetry_migrator`, `telemetry_runtime` |

  Set `DATABASE_NAME`, `RUNTIME_DATABASE_USER` and `MIGRATION_DATABASE_USER`
  from them. Set `OTHER_DATABASE_USERS` to every other role the roles query
  below lists, apart from `postgres`; as on 2026-10-20:

  ```bash
  case "$ENVIRONMENT_NAME" in
    production) OTHER_DATABASE_USERS=(telemetry_app) ;;
    *) OTHER_DATABASE_USERS=() ;;
  esac
  ```

  Change it if the record shows otherwise.
- **The database's roles and privileges.** Connect as the migration user
  through Cloud SQL Studio, or with `psql` through the Cloud SQL Auth Proxy,
  and save the output of `\l`, `\dn+ public`, `\dp public.*` and `\ddp`, and
  of the roles query below. Note which role owns the tables and what the
  runtime user is granted. These were provisioned by scripts kept outside
  this repo, so this record is the only specification of them. The roles
  query lists every role's attributes and memberships in one result; psql 16
  and later no longer show memberships in `\du`:

  ```sql
  SELECT r.rolname, r.rolcanlogin, r.rolcreaterole, r.rolcreatedb, r.rolinherit,
         ARRAY(SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
               WHERE m.member = r.oid ORDER BY 1) AS member_of
  FROM pg_roles r
  WHERE r.rolname !~ '^(pg_|cloudsql)'
  ORDER BY 1;
  ```
- Who may read each secret:
  `gcloud secrets get-iam-policy SECRET --project "$OLD_PROJECT_ID" --format=yaml`
  for each secret the environment uses: the four named above, and, if
  merged, #16's signing-key secret and S1's Hugging Face token secret. The
  read secret's policy names the dashboard's Vercel identity.
- What the open collector PRs added, if merged (step 3.1), each command with
  `--project "$OLD_PROJECT_ID"`:
  - #12: `gcloud tasks queues describe WORKER_QUEUE --location "$REGION"`;
    `gcloud run jobs describe` with `--region "$REGION"` for the worker job
    and the recovery job; `gcloud scheduler jobs describe RECOVERY_SCHEDULE --location "$REGION"`;
    the bucket's roles with `gcloud storage buckets get-iam-policy gs://INTAKE_BUCKET`;
    the invoker account's with `gcloud iam service-accounts get-iam-policy`;
    and the project-level roles of the old runtime, worker and recovery
    accounts, with the `gcloud projects get-iam-policy` command shown below for
    the deployment account. #12's deployment wiring isn't written, so its
    storage grants may be on the project rather than on the bucket, and the
    bucket's policy wouldn't show them.
  - #19: `gcloud run jobs describe "$HF_CHECK_JOB" --region "$REGION"`;
    `gcloud scheduler jobs describe HF_CHECK_SCHEDULE --location "$REGION"`,
    which shows the account it starts the job as; the two alert policies'
    names with `gcloud monitoring policies list --format='value(name,displayName,enabled)'`;
    the notification channel with `gcloud beta monitoring channels list`.
- The deployment account's roles. Its email is the
  `TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT` secret, which GitHub can't show. It is
  the account whose IAM policy grants `roles/iam.workloadIdentityUser` to this
  repo's principal set (calibration-diagnostics#233 checked that binding);
  `gcloud iam service-accounts get-iam-policy ACCOUNT --project "$OLD_PROJECT_ID"`
  shows it. Set `OLD_DEPLOY_SERVICE_ACCOUNT` to it. Then list its roles on the
  project with
  `gcloud projects get-iam-policy "$OLD_PROJECT_ID" --flatten=bindings --filter="bindings.members:serviceAccount:DEPLOY_ACCOUNT" --format="value(bindings.role)"`,
  and its roles on individual resources (the runtime and migration accounts,
  the secrets and the Artifact Registry store) from their IAM policies.
- The dashboard's Vercel identity provider: the resource named by the Vercel
  setting `MICROCOSM_TELEMETRY_GOOGLE_IDENTITY_PROVIDER`, read with
  `gcloud iam workload-identity-pools providers describe` (issuer, allowed
  audiences, attribute mapping and condition).

### 3.3 Agree the windows

One for staging, one for production (choice 8). From the start of a window
until its verification passes, nothing merges to this repo's `main`: every
merge deploys.

## 4. Create the project (once, no downtime)

```bash
gcloud projects create "$NEW_PROJECT_ID" --name "DISPLAY_NAME" --organization ORGANIZATION_ID   # or --folder FOLDER_ID
gcloud billing projects link "$NEW_PROJECT_ID" --billing-account BILLING_ACCOUNT_ID
NEW_PROJECT_NUMBER=$(gcloud projects describe "$NEW_PROJECT_ID" --format='value(projectNumber)')
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com iam.googleapis.com \
  iamcredentials.googleapis.com sts.googleapis.com storage.googleapis.com \
  --project "$NEW_PROJECT_ID"
```

Also enable `compute.googleapis.com` and `certificatemanager.googleapis.com`
for a load balancer (D3 C); `cloudtasks.googleapis.com` and
`cloudscheduler.googleapis.com` if #12's intake is switched on; and
`cloudscheduler.googleapis.com` and `monitoring.googleapis.com` if #19's daily
check has merged.

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

**Let the old project's production deployments read it.** `deploy-staging`
builds the image with staging's `PROJECT_ID`, and `deploy-production` deploys
that same image. So from staging's window until production's window, production
deploys an image from the new project's store into the old project. Cloud Run
then needs both the deploying account and the deploying project's Cloud Run
service agent to hold Artifact Registry Reader on that store. Without it,
production's "Deploy an isolated maintenance revision" step fails (no traffic
moves) in staging's window and on every merge until production's window. The
new project holds only the collector's store, so grant the role on the project
before staging's window:

```bash
for old_production_reader in "serviceAccount:$OLD_DEPLOY_SERVICE_ACCOUNT" \
  "serviceAccount:service-$OLD_PROJECT_NUMBER@serverless-robot-prod.iam.gserviceaccount.com"; do
  gcloud projects add-iam-policy-binding "$NEW_PROJECT_ID" \
    --member "$old_production_reader" --role roles/artifactregistry.reader
done
```

Remove both bindings in section 11.

### 4.2 The deployment account and Workload Identity Federation for this repo

The new project gets a deployment account used only by this repo. The dashboard
keeps its own deployment identity in `policyengine-api`.

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
allows it. Compare with the record from step 3.2 before granting:

| `deploy.sh` command | Needs | Grant |
|---|---|---|
| `gcloud run deploy`, `run jobs deploy` and `execute`, `services describe`, `update-traffic`, `--allow-unauthenticated` | Cloud Run administration | `roles/run.admin` on the project |
| `--service-account` for the service and the migration job | acting as those accounts | `roles/iam.serviceAccountUser` on each runtime and migration account (step 5.1) |
| `build-and-push-image` | pushing images | `roles/artifactregistry.writer` on the store |
| `check-secrets-exist` (`gcloud secrets describe`) | reading secret metadata | `roles/secretmanager.viewer` on each secret (step 5.3) |
| `create-database-backup` (production) | `cloudsql.backupRuns.create` | `roles/cloudsql.editor` on the project, or a custom role with `cloudsql.backupRuns.create` and `cloudsql.instances.get` |

### 4.3 The export bucket

It will hold complete copies of the telemetry databases, so it is private and
is deleted after the move (section 11).

```bash
gcloud storage buckets create "$EXPORT_BUCKET" --project "$NEW_PROJECT_ID" \
  --location "$REGION" --uniform-bucket-level-access --public-access-prevention
```

## 5. Create each environment's resources (no downtime)

Staging first. Production's can be created days before its window.

### 5.1 Service accounts

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

If the record from step 3.2 shows these accounts limited further today (for
example, an IAM condition on the instance name), repeat that here.

### 5.2 Cloud SQL

Create the instance with the settings recorded in step 3.2. Keep deletion
protection on.

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

Create every user the step 3.2 record lists, except `postgres`, with the
**same names** as on the old instance and new random passwords. Google's import
guide says every user that owns objects or was granted permissions on objects
in the dumped database must exist on the target instance, or the import
fails. Besides the migration and runtime users, create each role in
`OTHER_DATABASE_USERS` (step 3.2: `telemetry_app` for production today, which
the collector doesn't use, and none for staging), with a random password that
isn't kept.

```bash
migration_password="$(openssl rand -hex 32)"
runtime_password="$(openssl rand -hex 32)"
gcloud sql users create "$MIGRATION_DATABASE_USER" --instance "$NEW_INSTANCE" \
  --project "$NEW_PROJECT_ID" --password "$migration_password"
gcloud sql users create "$RUNTIME_DATABASE_USER" --instance "$NEW_INSTANCE" \
  --project "$NEW_PROJECT_ID" --password "$runtime_password"
for other_database_user in "${OTHER_DATABASE_USERS[@]}"; do   # none for staging
  gcloud sql users create "$other_database_user" --instance "$NEW_INSTANCE" \
    --project "$NEW_PROJECT_ID" --password "$(openssl rand -hex 32)"
done
```

Keep the two password variables in this shell for step 5.3.

**Give each user no more than its record shows.** Cloud SQL's pages "About
PostgreSQL users and roles" and "Create and manage users" say a user created
this way is a member of `cloudsqlsuperuser`, with the `CREATEROLE`,
`CREATEDB` and `LOGIN` attributes. An old user created in SQL may have less,
and an export carries no roles, so nothing corrects it later. For each new
user, compare with its row from the roles query in step 3.2 and remove what
the row doesn't show:

- **Membership of `cloudsqlsuperuser`:**
  `gcloud sql users assign-roles USER --instance "$NEW_INSTANCE" --project "$NEW_PROJECT_ID" --type BUILT_IN --revoke-existing-roles`,
  with `--database-roles` for any membership the row does show, once that
  role exists. Google's page gives this as the way to take
  `cloudsqlsuperuser` away from a user.
- **Attributes:** in SQL, connected as `postgres`, for example
  `ALTER ROLE telemetry_runtime NOCREATEROLE NOCREATEDB;`, and
  `ALTER ROLE telemetry_app NOLOGIN;` if its row shows no `LOGIN`. Google's
  page says `ALTER ROLE` changes any user's attributes.

Step 6.4 compares the result. Also recreate what an export doesn't carry: the
database-level privileges (`GRANT CONNECT` and `CREATE ON DATABASE`) from the
`\l` record, and role memberships from the roles query. Table privileges,
schema privileges and default privileges (`\dp`, `\dn+`, `\ddp`) come with the
export, provided the roles exist.

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

### 5.3 Secret Manager

```bash
for secret_name in "$DATABASE_SECRET_NAME" "$MIGRATION_DATABASE_SECRET_NAME" "$JWT_SECRET_NAME" "$READ_SECRET_NAME"; do
  gcloud secrets create "$secret_name" --project "$NEW_PROJECT_ID" --replication-policy automatic
done
```

**The two database URLs are new:** the new instance and the new passwords.
First see the form of the old value with its credentials hidden:

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
before the move stay valid afterwards (which also lets the old service keep
serving, choice 7), and the dashboard's credential doesn't change:

```bash
for secret_name in "$JWT_SECRET_NAME" "$READ_SECRET_NAME"; do
  gcloud secrets versions access latest --secret "$secret_name" --project "$OLD_PROJECT_ID" \
    | gcloud secrets versions add "$secret_name" --project "$NEW_PROJECT_ID" --data-file=-
done
```

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

The dashboard's access to the read secret is set in section 8.1.

**If merged by now:**

- **S1's Hugging Face read token (#13):** create the secret under the name S1
  records, and copy its value with the same `versions access | versions add`
  pipe, or rotate it: create a new fine-grained token with the same scope in
  Hugging Face, store that, and revoke the old token in section 11. Grant
  `roles/secretmanager.secretAccessor` only to the account the worker jobs run
  as, and mount it into the worker jobs as `COLLECTOR_HF_READ_TOKEN`.
- **C5's session signing key (#16):** create the secret named by
  `TELEMETRY_JWT_SIGNING_KEY_SECRET_NAME` in the new project and copy its
  value the same way, so tokens signed before the move verify afterwards and
  the key ID published in the JWKS doesn't change. Add it to both grant loops
  above: `roles/secretmanager.secretAccessor` for the runtime account and
  `roles/secretmanager.viewer` for the deployment account. The deployment
  checks that it exists and mounts it as `TELEMETRY_JWT_SIGNING_KEY` in both
  environments, so without it that environment's deployment stops at "Check
  required Secret Manager entries exist".
- **#12's intake:** in the new project, the Cloud Tasks queue, the worker
  job, the recovery job (its three `COLLECTOR_WORKER_*` settings naming the
  new queue, worker job and invoker account), the invoker account, the
  account Cloud Scheduler starts the recovery job as, and the permissions
  from the README's "Artifact intake" section. Create the recovery job's
  Cloud Scheduler job only in step 6.7: until the window's deployment, the
  new database is empty or holds step 5.5's rehearsal copy, and a recovery
  run would dispatch jobs from it.

  **Keep the intake bucket in the old project.** When a submission is
  completed, the collector records each stored object's generation number in
  the submission's row, and the worker reads every file at that generation.
  Cloud Storage assigns generation numbers, and they can't be set, so a copy
  in another bucket has different ones, and the worker would reject every
  accepted submission as `object_missing`. On the old bucket, grant the new
  runtime account `storage.objects.create` (its signed upload URLs create
  the objects) and `storage.objects.list` (completion lists them), and the
  new worker job's account `storage.objects.get` (it reads each file); the
  recovery job doesn't use the bucket. Use the roles step 3.2 recorded for the
  old accounts, whether they were granted on the bucket or on the project,
  and grant them on the bucket, for example `roles/storage.objectCreator` and
  `roles/storage.objectViewer` for the runtime account and
  `roles/storage.objectViewer` for the worker's:

  ```bash
  gcloud storage buckets add-iam-policy-binding gs://INTAKE_BUCKET --project "$OLD_PROJECT_ID" \
    --member "serviceAccount:$RUNTIME_SERVICE_ACCOUNT" --role ROLE
  ```

  `COLLECTOR_INTAKE_BUCKET` keeps its value. This ties the collector's files
  to `policyengine-api`: their storage cost, data-access audit logs and
  administration stay there, and shutting that project down, or removing its
  billing, makes them unreadable (the memo's D2 B). Moving the bucket later
  needs a one-off change that copies the files, checks their SHA-256 values
  again and rewrites each submission's recorded generation numbers; none is
  written.
- **S2's daily check (#19), production only:** the worker service account
  (the one that may read S1's token), with `roles/iam.serviceAccountUser` on
  it for the deployment account; `roles/secretmanager.viewer` on S1's secret
  for the deployment account; the account Cloud Scheduler starts the check
  as; the notification channel and both alert policies, as in the README.
  Production's deployment configures the check job itself, before it changes
  the service, and stops at that step without S1's secret, the worker
  account, or either of the deployment account's two roles. The channel and
  the policies don't affect the deployment. Create the schedule in step 6.7,
  once the deployment has created the job.

### 5.4 Create the Cloud Run service once

`deploy-maintenance` deploys with `--no-traffic`, which Cloud Run refuses when
it creates a service. So create the service once with Google's sample image.
Nothing uses its address yet, and the first deployment replaces the revision.

```bash
gcloud run deploy "$SERVICE" --project "$NEW_PROJECT_ID" --region "$REGION" \
  --image us-docker.pkg.dev/cloudrun/container/hello \
  --service-account "$RUNTIME_SERVICE_ACCOUNT" --no-allow-unauthenticated --quiet
```

The migration job needs no step: `configure-migration-job` runs
`gcloud run jobs deploy`, which creates it.

### 5.5 Production only: time the database copy

Before production's window, export the production database with `--offload`
(a serverless export, billed separately, which doesn't load the serving
instance), import it into the new instance as in step 6.3, and note both
durations. Then delete and re-create the new database
(`gcloud sql databases delete`, then `create`), so the window's import starts
empty, and grant the database-level privileges from step 5.2 again: deleting
the database drops them. Staging's window gives the same timings for staging's
size.

## 6. The window

Do staging end to end, verify it (section 9), then production.

**How long it lasts:** from step 6.2 until the deployment in step 6.6
finishes. Exporting and importing take minutes at today's size (10 GB disks).
Most of the time is the release that step 6.6 starts: the merge's Push run
(versioning), then the release commit's Push run (checks, tag, staging's
deployment, then production's). From merge to tag this took 25 minutes on
2026-10-15 and 88 minutes on 2026-10-19 and 2026-10-20, when the
organization's runners were queued; on 2026-10-20, calibration-diagnostics'
last collector deploy run took 2 hours 25 minutes in all, of which its staging
and production deploy jobs ran for 4 and 6 minutes. So plan for about 40
minutes on a quiet day (25 minutes to the tag, about 10 for the two deploy
jobs, and the export and import) and 2 to 3 hours when runners are queued,
and measure it in staging's window. If #12's intake is switched on, add the
wait for its worker executions in step 6.2, up to about 3 hours, unless they
are cancelled or the queue is paused early. The dashboard's collector reads
stay down until section 8.1's dashboard deployment finishes, unless the old
service keeps serving (section 8.4).

While the window lasts:

- **Builds:** the emitter gets HTTP 503 from the collector and retries, with
  a backoff of up to 60 seconds. Events wait on the build machine (kept for
  seven days, up to 100 MiB). A machine discarded when its build ends, such as
  a Modal container, loses the events not sent by then. A build never fails
  because of telemetry.
- **The dashboard:** the build monitor lists `collector` under "Runs that
  could not be read" and still shows the Hugging Face runs.
- **Orrery uploads:** the emitter gets their credential from the collector's
  token exchange, so they wait during the window. They already wait today,
  because microcosm-runs' authorization call needs a collector endpoint that
  doesn't exist, so nothing changes for them.

### 6.1 Check that no deployment is running, and record the rollback target

```bash
gh run list -R PolicyEngine/microcosm-collector --workflow push.yaml --status in_progress
gh run list -R PolicyEngine/microcosm-collector --workflow push.yaml --status queued
```

Both must list nothing. Then record the revision the old service serves now.
It is the rollback target (section 10). Every merge since step 3.2 has
redeployed production, possibly with migrations, so an earlier revision may
not match the database schema.

```bash
rollback_revision=$(gcloud run services describe "$SERVICE" --project "$OLD_PROJECT_ID" \
  --region "$REGION" --format=json \
  | jq -r '.status.traffic[] | select((.percent // 0) > 0) | .revisionName')
echo "$rollback_revision"
```

It must print one revision name. Write it in the window's notes too.

### 6.2 Put the old service in maintenance mode

`services update` makes a new revision from the serving one with only
`TELEMETRY_MAINTENANCE_MODE` changed, so it keeps the same image, secrets and
database:

```bash
gcloud run services update "$SERVICE" --project "$OLD_PROJECT_ID" --region "$REGION" \
  --update-env-vars TELEMETRY_MAINTENANCE_MODE=1 --no-traffic --tag project-move --quiet
gcloud run services update-traffic "$SERVICE" --project "$OLD_PROJECT_ID" --region "$REGION" \
  --to-tags project-move=100 --quiet
old_service_url=$(gcloud run services describe "$SERVICE" --project "$OLD_PROJECT_ID" \
  --region "$REGION" --format='value(status.url)')
curl --silent --output /dev/null --write-out '%{http_code}\n' \
  --request POST "$old_service_url/v1/auth/huggingface/exchange"
sleep 35
```

The `curl` must print `503`, as `resolve-and-verify-maintenance` checks. The
pause outlasts the 30-second request timeout, so no request to the service is
still writing.

**If #12's intake is switched on,** its worker job and its recovery job also
write to the database, and maintenance mode doesn't stop them. In the old
project, pause the queue and the recovery schedule together, then make sure
no execution of either job is still running:

```bash
gcloud tasks queues pause WORKER_QUEUE --location "$REGION" --project "$OLD_PROJECT_ID"
gcloud scheduler jobs pause RECOVERY_SCHEDULE --location "$REGION" --project "$OLD_PROJECT_ID"
for intake_job in WORKER_JOB RECOVERY_JOB; do
  gcloud run jobs executions list --job "$intake_job" --region "$REGION" \
    --project "$OLD_PROJECT_ID" --format=json \
    | jq -r '.[] | select(.status.completionTime == null) | .metadata.name'
done
```

The loop prints the executions that haven't finished; repeat it until it
prints nothing. An operator's `reprocess` run is an execution of the recovery
job, so the loop covers it too.

- **A recovery execution** only updates job rows and creates Cloud Tasks.
  Wait for it.
- **A worker execution** can run for about 3 hours: three attempts
  (`--max-retries 2`) of up to 1 hour each (`--task-timeout 1h`). Either wait,
  which lengthens the window, or cancel it with
  `gcloud run jobs executions cancel EXECUTION --region "$REGION" --project "$OLD_PROJECT_ID" --quiet`.
  A cancelled attempt records no result. Its job stays `running` in the
  copied database until its lease expires, up to 65 minutes after the attempt
  started; then the new project's recovery job dispatches it again, or fails
  it if it has used its three attempts or three dispatches.
- **To keep the window short,** pause the queue and the schedule up to 3
  hours before it starts, so worker executions finish first (resume both if
  the window is called off). Submissions completed in the meantime wait in
  the paused queue, and the new project's recovery job dispatches them after
  the move (step 6.7).
- **Never pause the queue alone.** Tasks can still be added to a paused
  queue, and recovery dispatches a job again each time it has waited 30
  minutes without a claim, and fails it after its third dispatch. With the
  queue paused and recovery running, queued jobs fail about 90 minutes after
  their first dispatch.

Uploads in flight need nothing. A signed upload URL (valid for 30 minutes)
names the bucket, which stays where it is (step 5.3), so a build's `complete`
request to the new service finds the files there.

From here the old database doesn't change.

### 6.3 Export and import

```bash
export_object="$EXPORT_BUCKET/$ENVIRONMENT_NAME-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
gcloud sql export sql "$OLD_INSTANCE" "$export_object" \
  --database "$DATABASE_NAME" --project "$OLD_PROJECT_ID"
gcloud sql import sql "$NEW_INSTANCE" "$export_object" \
  --database "$DATABASE_NAME" --user "$MIGRATION_DATABASE_USER" --project "$NEW_PROJECT_ID" --quiet
```

Import as the role that owns the tables in the step 3.2 record (normally the
migration user), so the restored objects keep their owner. If #12's intake is
switched on, its bucket isn't copied: it stays in the old project (step 5.3).

### 6.4 Check the copy

On both instances, as the migration user:

- `SELECT version_num FROM alembic_version;` gives the same revision.
- `SELECT count(*)` on every table in the `public` schema gives the same
  counts. At revision `0001` these are `telemetry_runs`, `telemetry_producers`
  and `telemetry_events`; later migrations add more.
- `\l`, `\dp public.*` and `\ddp` match the step 3.2 record, so the
  database-level, table and default privileges all came across.
- The roles query from step 3.2 gives the same rows, so every user has the
  same attributes and memberships. If a new user has more, such as
  `cloudsqlsuperuser`, `CREATEROLE` or `CREATEDB` left from
  `gcloud sql users create`, remove it as in step 5.2 and compare again.

If anything else differs, stop and roll back (section 10, first row).

### 6.5 Point the GitHub environment at the new project

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

In production's window, also set `TELEMETRY_PRODUCTION_CLOUD_SQL_CONNECTION`
to production's new connection in **both** environments, since staging's
`validate-environment-target` compares against it. If #19 has merged, also
set production's `TELEMETRY_WORKER_SERVICE_ACCOUNT` to the new worker account.
The other variables keep their values if the new resources reused the old
names (section 12).

If #12's intake is switched on, point its settings at the new project, through
whichever environment variables #12's deployment wiring reads (not yet
written):

- `COLLECTOR_WORKER_QUEUE` and `COLLECTOR_WORKER_JOB`:
  `projects/$NEW_PROJECT_ID/locations/$REGION/queues/...` and `.../jobs/...`;
- `COLLECTOR_INTAKE_SIGNING_SERVICE_ACCOUNT`: the new runtime account;
- `COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT`: the new invoker account.

`COLLECTOR_INTAKE_BUCKET` keeps its value (step 5.3). With the old values, the
new service would try to sign uploads as the old runtime account and enqueue
into the old project's paused queue, so intake would stop.

### 6.6 Deploy

Each window needs its own merge. Its release runs the Push workflow, which
deploys staging and then production with the environments' current settings.

- **Staging's window:** merge the C2 code PR (section 13). Production then
  redeploys in the old project with unchanged runtime code, from the image
  staging built in the new project's store. That works only with the
  Artifact Registry Reader grant in step 4.1; it is otherwise a normal
  deployment, with its usual short maintenance phase.
- **Production's window:** merge a second PR prepared for it, for example the
  README update in section 13. This repo requires branches to be up to date
  with `main`, so update it and let its checks pass before the window starts.

Re-running the deployment jobs of the latest Push run might do the same
without a merge, but whether a re-run reads the environments' changed values
hasn't been checked. In staging's window, confirm it from the run log (the
"Require deployment configuration" step shows `PROJECT_ID`) before relying on
it for production. In production, a re-run that used the old values would take
the old service out of maintenance mode, on the old database.

The first deployment replaces the sample revision with a maintenance revision,
runs the migration job (the restored database is already at the current
revision, and the job checks the schema against the SQLAlchemy metadata),
deploys and checks a candidate revision, and moves traffic to it. In
production it also takes a backup of the new instance first.

### 6.7 Start the scheduled jobs in the new project

After the deployment in step 6.6 has finished.

**#12's intake, if switched on:** create the recovery job's schedule in the
new project, every 10 minutes as the README says, as the account created for
it in step 5.3:

```bash
gcloud scheduler jobs create http RECOVERY_SCHEDULE --location "$REGION" --project "$NEW_PROJECT_ID" \
  --schedule '*/10 * * * *' \
  --uri "https://run.googleapis.com/v2/projects/$NEW_PROJECT_ID/locations/$REGION/jobs/RECOVERY_JOB:run" \
  --http-method post --oauth-service-account-email RECOVERY_SCHEDULER_ACCOUNT
```

Compare its settings with the old schedule's record from step 3.2. Jobs that
the old service queued before step 6.2 are still in the old, paused queue.
The new recovery job dispatches each one again into the new queue once 30
minutes have passed since its last dispatch, or fails it if it has already
been dispatched three times. The old queue and schedule stay paused until
section 11.

**S2's daily check (#19), in production's window:** production's deployment
has created the check job in the new project. Create its schedule, then run
the job once by hand and wait for it:

```bash
gcloud scheduler jobs create http HF_CHECK_SCHEDULE --location "$REGION" --project "$NEW_PROJECT_ID" \
  --schedule '0 6,18 * * *' --time-zone Etc/UTC \
  --uri "https://run.googleapis.com/v2/projects/$NEW_PROJECT_ID/locations/$REGION/jobs/$HF_CHECK_JOB:run" \
  --http-method post --oauth-service-account-email HF_CHECK_SCHEDULER_ACCOUNT
gcloud run jobs execute "$HF_CHECK_JOB" --region "$REGION" --project "$NEW_PROJECT_ID" --wait
```

The execution must succeed. The README says a metric-absence condition "is
met only after the job writes at least one data point after the policy is
created", so until this run the new "no successful execution for 23 hours"
policy can't report a stopped schedule.

Then stop the old project's check, alerts first:

```bash
for old_alert_policy in OLD_FAILURE_POLICY OLD_ABSENCE_POLICY; do
  gcloud monitoring policies update "$old_alert_policy" --no-enabled --project "$OLD_PROJECT_ID"
done
gcloud scheduler jobs pause HF_CHECK_SCHEDULE --location "$REGION" --project "$OLD_PROJECT_ID"
```

The two policy names are in the step 3.2 record. Disable the policies before
pausing the schedule, and do both before section 11 removes the old check job
or revokes S1's old token. Otherwise the old project emails false alerts:
the failure policy at the next failed run, and the absence policy 23 hours
after the last successful one.

## 7. The hostname and DNS

Records at Cloudflare must be "DNS only". If the zone ever gets CAA records,
they must allow `pki.goog` and `letsencrypt.org`.

### D3 B: Cloud Run domain mapping

```bash
gcloud domains verify BASE_DOMAIN   # once, by the account that creates mappings
gcloud beta run domain-mappings create --service "$SERVICE" --domain "$COLLECTOR_HOSTNAME" \
  --region "$REGION" --project "$NEW_PROJECT_ID"
gcloud beta run domain-mappings describe --domain "$COLLECTOR_HOSTNAME" --region "$REGION" \
  --project "$NEW_PROJECT_ID" --format='yaml(status.resourceRecords,status.conditions)'
```

Add the records `describe` lists at the DNS provider, then wait for the
`CertificateProvisioned` condition to be `True`: "usually about 15 minutes but
can take up to 24 hours."

- **Both together (choice 6):** nothing uses the hostname yet, so map it any
  time after the first deployment, and wait for the certificate before the
  emitter release (section 8.2).
- **Hostname first:** the hostname is already mapped to the old project's
  service. Re-create the mapping in the new project with `--force-override`
  inside production's window, after the deployment. That moves the hostname
  at once, and requests fail TLS checks until the new certificate is issued;
  the emitter retries.

### D3 C: load balancer

```bash
gcloud compute addresses create collector-address --global --project "$NEW_PROJECT_ID"
gcloud certificate-manager dns-authorizations create collector-hostname \
  --domain "$COLLECTOR_HOSTNAME" --project "$NEW_PROJECT_ID"
gcloud certificate-manager dns-authorizations describe collector-hostname \
  --project "$NEW_PROJECT_ID" --format='yaml(dnsResourceRecord)'
```

Add that CNAME at the DNS provider. It authorizes the certificate and moves no
traffic. Then:

```bash
gcloud certificate-manager certificates create collector-certificate \
  --domains "$COLLECTOR_HOSTNAME" --dns-authorizations collector-hostname --project "$NEW_PROJECT_ID"
gcloud certificate-manager maps create collector-certificates --project "$NEW_PROJECT_ID"
gcloud certificate-manager maps entries create collector-hostname --map collector-certificates \
  --certificates collector-certificate --hostname "$COLLECTOR_HOSTNAME" --project "$NEW_PROJECT_ID"
gcloud compute network-endpoint-groups create collector-endpoints --region "$REGION" \
  --network-endpoint-type serverless --cloud-run-service "$SERVICE" --project "$NEW_PROJECT_ID"
gcloud compute backend-services create collector-backend --global \
  --load-balancing-scheme EXTERNAL_MANAGED --project "$NEW_PROJECT_ID"
gcloud compute backend-services add-backend collector-backend --global \
  --network-endpoint-group collector-endpoints --network-endpoint-group-region "$REGION" \
  --project "$NEW_PROJECT_ID"
gcloud compute url-maps create collector-routes --default-service collector-backend \
  --global --project "$NEW_PROJECT_ID"
gcloud compute target-https-proxies create collector-proxy --url-map collector-routes \
  --certificate-map collector-certificates --global --project "$NEW_PROJECT_ID"
gcloud compute forwarding-rules create collector-https --global \
  --load-balancing-scheme EXTERNAL_MANAGED --address collector-address \
  --target-https-proxy collector-proxy --ports 443 --project "$NEW_PROJECT_ID"
```

Wait until
`gcloud certificate-manager certificates describe collector-certificate --project "$NEW_PROJECT_ID" --format='value(managed.state)'`
prints `ACTIVE`. Then point the hostname's `A` record at the address
(`gcloud compute addresses describe collector-address --global --project "$NEW_PROJECT_ID" --format='value(address)'`).
Lower the record's TTL to 300 seconds a day before, so a rollback propagates
quickly. In the hostname-first order, the old project's certificate
authorization record may still exist at the same name; Certificate Manager's
per-project DNS authorizations (`--type=PER_PROJECT_RECORD`) let both exist at
once. Check its documentation before relying on that.

### Check the hostname

```bash
curl -fsS "https://$COLLECTOR_HOSTNAME/ready"
curl --silent --output /dev/null --write-out '%{http_code}\n' \
  --request POST "https://$COLLECTOR_HOSTNAME/v1/auth/huggingface/exchange"
```

The first must succeed. The second must print `401` (no credential sent), which
shows the request reached the application rather than a maintenance revision.

## 8. Point the consumers at the new collector

### 8.1 The calibration dashboard

**Give the dashboard's identity access to the new read secrets first** (choice
10):

- **Keep its provider in `policyengine-api`:** grant the principal (or
  principals) that the step 3.2 record lists on the old read secret the same
  role on the new one:
  `gcloud secrets add-iam-policy-binding "$READ_SECRET_NAME" --project "$NEW_PROJECT_ID" --member "PRINCIPAL" --role roles/secretmanager.secretAccessor`.
  Production's identity gets only production's read secret, and Preview's only
  staging's.
- **A new provider in the new project:** create a pool and provider with the
  issuer, allowed audiences, attribute mapping and condition recorded in step
  3.2, grant its principals as above, and set the Vercel setting
  `MICROCOSM_TELEMETRY_GOOGLE_IDENTITY_PROVIDER` to the new provider's
  resource.
- **C6 done:** nothing; the dashboard no longer reads Secret Manager for the
  collector.

The dashboard's deployment checks that its read secret exists ("Check
collector read secret exists" in `deploy.yml`). Grant its own deployment
identity, the account in calibration-diagnostics' `Production` secret
`TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT`, `roles/secretmanager.viewer` on
production's new read secret.

**Then change the settings:**

| Setting | Where | New value |
|---|---|---|
| `MICROCOSM_TELEMETRY_COLLECTOR_URL` | calibration-diagnostics GitHub environment `Production` | `https://$COLLECTOR_HOSTNAME` for production |
| `MICROCOSM_TELEMETRY_COLLECTOR_URL` | Vercel project calibration-diagnostics, Production and Preview | production's and staging's addresses |
| `MICROCOSM_TELEMETRY_READ_SECRET_VERSION` | Vercel, Production and Preview | `projects/$NEW_PROJECT_ID/secrets/READ_SECRET/versions/VERSION`, for production's and staging's read secrets, in the form used today |
| `TELEMETRY_GCP_PROJECT_ID` | calibration-diagnostics GitHub environment `Production` | `$NEW_PROJECT_ID` |
| `TELEMETRY_READ_SECRET_NAME` | calibration-diagnostics GitHub environment `Production` | unchanged if the name was reused |

Settings take effect on the next deployment. Merge calibration-diagnostics'
next PR, and open a new Preview deployment. Re-running its latest "Deploy
production" run may also work, but whether a re-run reads the changed GitHub
variable is the unchecked question from step 6.6. The build monitor should
then list collector runs from before the move.

### 8.2 The emitter

In PolicyEngine/microcosm-emitter, one PR: add `https://COLLECTOR_HOSTNAME`
(production's) to `TRUSTED_COLLECTOR_ORIGINS` and point
`PRODUCTION_COLLECTOR_URL` at it in `src/microcosm_emitter/core/constants.py`.
Keep the old origin in the set while the old address serves. Merging it
publishes the release.

- Builds that start the separately installed emitter pick it up at their next
  start, once it is installed on that machine or in that build image. A
  running build keeps the emitter it started with.
- Builds pinned to `microcosm-emitter==0.2.1` keep the old address until
  Microcosm moves off that pin.

### 8.3 microcosm-runs (choice 11)

Either change `PRODUCTION_COLLECTOR_ORIGIN` in `lib/auth.ts` and the value in
`.env.example`, or leave both for O7, which removes the upload API that uses
them. Its Vercel project sets no `ORRERY_COLLECTOR_ORIGIN`.

### 8.4 The old service (choice 7)

- **Keep serving, connected to the new database:** grant the old runtime
  account `roles/cloudsql.client` in the new project and
  `roles/secretmanager.secretAccessor` on the new runtime database secret.
  Then update the old service, which keeps the image it ran before the move:

  ```bash
  gcloud run services update "$SERVICE" --project "$OLD_PROJECT_ID" --region "$REGION" \
    --set-cloudsql-instances "$NEW_CLOUD_SQL_CONNECTION" \
    --update-secrets "DATABASE_URL=projects/$NEW_PROJECT_NUMBER/secrets/$DATABASE_SECRET_NAME:latest" \
    --update-env-vars TELEMETRY_MAINTENANCE_MODE=0 --no-traffic --tag new-database --quiet
  ```

  Check `/ready` on the `new-database` tag's address, then route traffic to it
  with `update-traffic --to-tags new-database=100`. Retire it before any
  migration that its older code can't work with, and as soon as no build uses
  emitter 0.2.1.
- **Stay in maintenance mode:** nothing to do. Builds still on the old address
  keep their telemetry locally.
- **Delete it:** see section 11.

## 9. Verify

For each environment:

- `gcloud run services describe "$SERVICE" --project "$NEW_PROJECT_ID" --region "$REGION" --format=json`
  shows an image in the new store, tagged with this repo's release commit (whose
  message is "Update microcosm-collector"), the new Cloud SQL connection, and
  the four secret names (five once #16 has merged).
- `curl -fsS "$(gcloud run services describe "$SERVICE" --project "$NEW_PROJECT_ID" --region "$REGION" --format='value(status.url)')/ready"`
  succeeds, and so do the hostname checks in section 7.
- `gcloud run jobs executions list --job "$MIGRATION_JOB" --project "$NEW_PROJECT_ID" --region "$REGION" --limit 1`
  shows a successful execution.
- Production: `gcloud sql backups list --instance "$NEW_INSTANCE" --project "$NEW_PROJECT_ID" --limit 1 --format='table(id,windowStartTime,status,description)'`
  shows `telemetry-release-RUN_ID-ATTEMPT` for the Push run. The default
  columns leave out the description.
- The row counts from step 6.4 still match.
- After section 8.1: the build monitor lists runs from before the move and
  doesn't list `collector` under "Runs that could not be read".
- After section 8.2: the next build that starts the new emitter shows its
  events in the build monitor.
- The old service answers as choice 7 says: `503` in maintenance mode, or
  `/ready` when kept serving.
- If #19 has merged: step 6.7's execution of the check job succeeded, the
  old project's two alert policies are disabled, and its check schedule is
  paused.
- If #12's intake is switched on:
  `gcloud storage buckets get-iam-policy gs://INTAKE_BUCKET --project "$OLD_PROJECT_ID"`
  lists the new runtime and worker accounts with the roles from step 5.3, and
  one small test submission, made through the flow in #12's
  `docs/artifact-submissions.md` (token exchange for `artifacts:submit`,
  create, upload, complete), reaches the status `succeeded` at
  `GET /v1/submissions/{submission_id}`. That shows the new runtime account
  can sign uploads and list the bucket, the new queue starts the new worker
  job, and that job can read the files. The test files stay in the bucket,
  which has no lifecycle deletion.

## 10. Rollback

Keep every old resource until section 11. The old database is unchanged from
the moment the old service entered maintenance mode.

| Situation | What to do |
|---|---|
| In the window, before section 8 | Route the old service back: `gcloud run services update-traffic "$SERVICE" --project "$OLD_PROJECT_ID" --region "$REGION" --to-revisions "$rollback_revision=100"`, with the revision step 6.1 recorded in this window. Set back, to the step 3.2 record, only the GitHub values step 6.5 changed in this window: in staging's window, staging's; in production's window, production's and `TELEMETRY_PRODUCTION_CLOUD_SQL_CONNECTION` in staging's environment. Staging's other values stay pointed at the new project, where its database now is. If step 6.2 paused #12's queue and recovery schedule, resume them (`gcloud tasks queues resume`, `gcloud scheduler jobs resume`). If step 6.7 ran, pause the schedules it created in the new project and disable the new alert policies; for #19, resume the old check schedule and enable the old policies again (`--enabled`). Builds kept their events and deliver them to the old service, except builds on a machine discarded when the build ended, such as a Modal container, which lost the events not sent by then. |
| A deployment into the new project fails | As in calibration-diagnostics#233: before the migration step, fix and re-run the failed jobs. The new service isn't in use yet, so the first row also applies. |
| After the dashboard switched (8.1), before the emitter release (8.2) | Builds still use only the old address. If the old service was in maintenance mode the whole time, nothing new is in the new database: do the first row, then set the dashboard's settings back and redeploy it. If the old service was kept serving against the new database (8.4), new events are in the new database: do the next row. |
| After the emitter release | New events are in the new database. Put the new service in maintenance mode (step 6.2's commands with the new project), export the new database, import it into a fresh database on the old instance (drop and re-create the old database first, or import under a new name and update the old runtime database secret), route the old service back to the revision step 6.1 recorded in production's window (if a release with a migration has deployed in the new project since, that revision's code is older than the imported schema: set the GitHub values back first, and let the next merge deploy the current release to the old project instead), set the GitHub and dashboard settings back, and release the emitter with the old `PRODUCTION_COLLECTOR_URL` (the old origin is still trusted, so only that constant changes). Builds that started on the new default keep it until they finish, and keep their events locally while the new service is in maintenance mode. |
| The hostname | D3 B: delete the mapping, or map the hostname back to the old project's service with `--force-override`. D3 C: point the `A` record back to what it named before, if anything. |

## 11. Retire the old resources

Only when no build uses the old address (Microcosm has left the 0.2.1 pin, and
the old service's request count in Cloud Monitoring has stayed at zero), and
after an agreed holding period:

- The old Cloud SQL instances: export a last copy into the bucket if wanted,
  then `gcloud sql instances patch "$OLD_INSTANCE" --project "$OLD_PROJECT_ID" --no-deletion-protection`
  and `gcloud sql instances delete`.
- The old Cloud Run services and migration jobs, every secret step 3.2
  recorded (the four per environment, and #16's signing key and S1's token
  if merged), and the old runtime, migration and worker service accounts.
- If #12 merged: the queue (with any tasks still in it), the worker and
  recovery jobs, the recovery schedule and the account it used, the invoker
  account, and the old accounts' roles on the intake bucket, or on the
  project if step 3.2 found them there. Keep the bucket: it holds every
  submission's files and is still in use (step 5.3). So `policyengine-api`
  has to stay active, and billed, for as long as the collector reads them.
- If #19 merged: the old check job, its schedule and the account the
  schedule used, both alert policies and the notification channel. Step 6.7
  already disabled the policies and paused the schedule.
- The collector's images in the old Artifact Registry store (only the
  collector's image name, unless the store holds nothing else).
- The two `roles/artifactregistry.reader` bindings on the new project from
  step 4.1 (the old deployment account and the old project's Cloud Run service
  agent).
- This repo's Workload Identity provider in `policyengine-api`
  (`github/microcosm-collector`) and the old deployment account's
  `roles/iam.workloadIdentityUser` binding for this repo's principal set.
  Keep calibration-diagnostics' provider and the shared deployment account:
  the dashboard's deployment still uses them.
- The export bucket and every dump in it.
- calibration-diagnostics' leftover collector settings, as listed in
  calibration-diagnostics#233's "Manual steps".
- S1's old Hugging Face token, if it was rotated. If #19 merged, revoke it
  only after step 6.7 has paused the old check, which uses it.

## 12. Settings this runbook changes

| Setting | Provider | Read by | After the move |
|---|---|---|---|
| `TELEMETRY_GCP_PROJECT_ID` | this repo's GitHub environments `staging` and `production` (variable) | `push.yaml` job `env:` as `PROJECT_ID`, then every `scripts/deploy.sh` command | the new project ID |
| `TELEMETRY_CLOUD_SQL_CONNECTION` | same (variable) | `CLOUD_SQL_CONNECTION`: `--add-cloudsql-instances`, `--set-cloudsql-instances`, `create-database-backup` | `NEW_PROJECT_ID:REGION:INSTANCE` |
| `TELEMETRY_PRODUCTION_CLOUD_SQL_CONNECTION` | same, in both environments (variable) | `validate-environment-target` | production's new connection |
| `TELEMETRY_RUNTIME_SERVICE_ACCOUNT`, `TELEMETRY_MIGRATION_SERVICE_ACCOUNT` | same (variables) | `--service-account` for the service and the migration job | the new accounts |
| `TELEMETRY_GCP_REGION`, `TELEMETRY_ARTIFACT_REGISTRY_NAME`, `TELEMETRY_IMAGE_NAME`, `TELEMETRY_SERVICE`, `TELEMETRY_PRODUCTION_SERVICE`, `TELEMETRY_MIGRATION_JOB`, and the four `TELEMETRY_*_SECRET_NAME` variables | same (variables) | image path, Cloud Run names, `--set-secrets`, `check-secrets-exist` | unchanged when the new resources reuse the names |
| `TELEMETRY_GCP_WIF_PROVIDER` | same (secret) | `google-github-actions/auth`, `workload_identity_provider` | the new provider's resource |
| `TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT` | same (secret) | `google-github-actions/auth`, `service_account` | the new deployment account |
| `COLLECTOR_DEPLOY_ENABLED` | this repo's variable | the deployment jobs' `if:` | unchanged (`true`) |
| `roles/artifactregistry.reader` on the new project for the old deployment account and the old project's Cloud Run service agent | Google Cloud IAM (step 4.1) | Cloud Run, when `deploy-production` deploys staging's image into the old project | removed in section 11 |
| `TELEMETRY_JWT_SIGNING_KEY_SECRET_NAME` (if #16 merged) | same, in both environments (variable) | `check-secrets-exist`, `--set-secrets` | unchanged when the new secret reuses the name |
| `TELEMETRY_WORKER_SERVICE_ACCOUNT` (if #19 merged) | `production` (variable) | `configure-huggingface-check-job`, `--service-account` | the new worker account |
| `TELEMETRY_HF_CHECK_JOB`, `TELEMETRY_HF_READ_SECRET_NAME` (if #19 merged) | `production` (variables) | `configure-huggingface-check-job` | unchanged when the new resources reuse the names |
| `COLLECTOR_WORKER_QUEUE`, `COLLECTOR_WORKER_JOB` (if #12's intake is switched on) | the environment variables #12's deployment wiring will read (not yet written); set by hand on the recovery job | the service and the recovery job | the new project's queue and worker job |
| `COLLECTOR_INTAKE_SIGNING_SERVICE_ACCOUNT`, `COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT` (same) | same | the service signs upload URLs as the first; the service and the recovery job dispatch as the second | the new runtime account and the new invoker account |
| `COLLECTOR_INTAKE_BUCKET` (same) | same | the service and the worker job | unchanged: the bucket stays in the old project (step 5.3) |
| `DATABASE_URL`, `TELEMETRY_JWT_SECRET`, `TELEMETRY_READ_TOKEN` | Google Secret Manager in the new project | the service, through `--set-secrets` | new database URL; copied signing secret and read token |
| `TELEMETRY_JWT_SIGNING_KEY` (if #16 merged) | Secret Manager in the new project | the service, through `--set-secrets` | copied key |
| `DATABASE_URL` (migration job) | Secret Manager in the new project | the migration job, through `--set-secrets` | new migration database URL |
| `COLLECTOR_HF_READ_TOKEN` (if S1 merged) | Secret Manager in the new project | the worker jobs, through a secret mount | copied or rotated |
| `TELEMETRY_MAINTENANCE_MODE` | `scripts/deploy.sh` (`--set-env-vars`), and step 6.2 on the old service | the service | unchanged |
| Dashboard settings | calibration-diagnostics GitHub `Production` environment and the Vercel project | see section 8.1 | see section 8.1 |
| `PRODUCTION_COLLECTOR_URL`, `TRUSTED_COLLECTOR_ORIGINS` | emitter source (`core/constants.py`) | every emitter service module | the hostname, by an emitter release |

**Unverified:** nothing in this runbook has been run. The current deployment
account's roles, the database roles and privileges (including whether
production's `telemetry_app` owns or holds anything, and whether the old
users are members of `cloudsqlsuperuser`), and the dashboard
identity's bindings were not read for it (they are recorded in step 3.2).
Whether a workflow re-run reads changed environment values is unchecked (step
6.6). Every setting above needs checking end to end in the staging pass.

## 13. Code changes that go with this runbook (not yet written)

- **This repo:** a step after "Verify the stable service address" that checks
  `https://$PUBLIC_ORIGIN/ready`, from a new environment variable
  `TELEMETRY_PUBLIC_ORIGIN`, so every deployment proves the hostname reaches
  the new revision. The step skips when the variable is unset: in the "both
  together" order the hostname reaches the new service only after section 7,
  so set the variable once section 7's checks pass. Its merge is the release
  that deploys staging into the new project (step 6.6).
- **This repo, for production's window:** the README's "Cloud Run deployment"
  and "Google Cloud access" sections updated to describe the new project and
  the hostname. Its merge is the release that deploys production into the new
  project (step 6.6).
- **The emitter:** the release in section 8.2.
- **microcosm-runs:** section 8.3, if chosen.
