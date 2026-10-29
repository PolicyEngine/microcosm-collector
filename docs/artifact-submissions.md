# Artifact submissions

Contract version 1. The collector implements it in its intake endpoints and
worker framework; the emitter implements it in its submission module.

A build hands files to the collector as one **submission**: a kind, the build's
provenance, and an inventory of files. The file bytes never pass through the
collector API. They go straight to a private intake bucket through signed
upload URLs, because the API accepts request bodies of at most 1 MiB and runs
with 512 MiB of memory and a 30 s request timeout. Work on the files runs later
in a worker job (Cloud Run Jobs), which has its own memory and time limits.

## Flow

1. **Credential.** `POST /v1/auth/huggingface/exchange` with the body
   `{"scope": "artifacts:submit"}`. A collector with intake switched on
   returns a session token whose `scope` claim is `artifacts:submit`, and the
   response's `scope` field says so (see "Token exchange"). An exchange without
   a body, or with `{}`, still returns a `telemetry:write` token, as before.
   Each scope works only on its own endpoints.
2. **Create.** `POST /v1/submissions` with the submission (below). The
   collector validates it, derives the submission ID, records it, and returns
   its status with one signed upload instruction for each file that is not
   yet stored.
3. **Upload.** The client sends each file with `PUT` to its URL, with exactly
   the headers the instruction lists, plus `Content-Length`. An object can be
   created only once, so a repeated upload of a stored file gets 412, which
   means it is already there.
4. **Complete.** `POST /v1/submissions/{submission_id}/complete`. The collector
   lists the submission's stored objects and checks that every inventory file
   exists, has the declared size, and carries the declared SHA-256 in its
   metadata. A missing file gets a 409, so the client uploads it and completes
   again. A stored object with another size or SHA-256 rejects the submission,
   because stored objects are never replaced. Otherwise the collector records
   each object's generation, so later overwrites cannot change what is
   processed, then records a job and dispatches it: a Cloud Task starts a
   worker job execution.
5. **Process.** The worker claims the job, reads each file at its recorded
   generation and recomputes its SHA-256, then runs the kind's handler.
6. **Result.** The client polls `GET /v1/submissions/{submission_id}` and,
   once the status is final, reads `GET /v1/submissions/{submission_id}/receipt`.

The API checks sizes and recorded digests without reading file bytes; the
worker checks the bytes. Hashing gigabytes inside a 30 s request is not safe,
so the byte-level SHA-256 check belongs to the worker.

## Token exchange

The request body is absent, `{}`, `{"scope": "telemetry:write"}` or
`{"scope": "artifacts:submit"}`. A `telemetry:write` request may also carry
`lifetime_seconds`, a positive integer, for a build on another machine (see the
README's "Tokens for builds on other machines"); `"lifetime_seconds": null` is
the same as leaving the field out. Any other field or scope, and a lifetime
with `artifacts:submit`, is refused with 422.
A successful exchange answers 200 with:

```json
{
  "access_token": "…",
  "token_type": "bearer",
  "expires_in": 3600,
  "scope": "artifacts:submit"
}
```

`scope` repeats the scope of the token, and `expires_in` is its lifetime in
seconds.

### When intake is not available

A collector may have no intake: one released before intake existed, or one
with intake switched off. A client recognises both, and never mistakes either
for an unknown submission:

| Collector | Exchange for `artifacts:submit` | Any path under `/v1/submissions` |
|---|---|---|
| Released before intake | 200 with a `telemetry:write` token and no `scope` field | 404 without a `code` |
| Intake switched off | 404 with `"code": "intake_not_enabled"` | 404 with `"code": "intake_not_enabled"` |
| Intake switched on | 200 with `"scope": "artifacts:submit"` | as in "Responses" |

**Client rule:** use a token for submissions only when the exchange answered
200 with `"scope": "artifacts:submit"`. Any other 200 answer, a 404 from the
exchange, or a 404 from a submission path without
`"code": "submission_not_found"` means intake is not available. The client
then keeps the files queued with their preserved copies, discards any
`artifacts:submit` token, sends no further submission requests to that
collector, and tries the exchange again later (for example, after an hour). It
never treats this as a final refusal, and never creates the submission again
because of it.

## The submission

```json
{
  "contract_version": 1,
  "kind": "calibration-diagnostics",
  "licensed_data": true,
  "provenance": {
    "run_id": "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71",
    "build_id": "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71",
    "country_code": "UK",
    "release": "microcosm-uk-2024-25-national",
    "huggingface": {
      "repo": "policyengine/populace-uk-private",
      "commit": "0123456789abcdef0123456789abcdef01234567",
      "folder": "releases/microcosm-uk-2024-25-national/uk-frs-calibration-attempt-20261019T083323Z-6cba5d71"
    }
  },
  "files": [
    {
      "name": "calibration_diagnostics.json",
      "role": "calibration_diagnostics",
      "size": 18734221,
      "sha256": "…64 lowercase hex characters…"
    }
  ]
}
```

| Field | Rule |
|---|---|
| `contract_version` | The integer `1`. |
| `kind` | `calibration-diagnostics` or `orrery-inputs`. |
| `licensed_data` | Required boolean: whether the build used licensed microdata. See "Disclosure". |
| `provenance` | An object with exactly the five keys below. Every key is required: `release` and `huggingface` may be `null`, but are never left out. |
| `provenance.run_id` | The telemetry run ID: 1–192 characters from `A-Z`, `a-z`, `0-9`, `.`, `_`, `:` and `-`, starting with a letter or digit (`[A-Za-z0-9][A-Za-z0-9._:-]{0,191}`), as for `POST /v1/runs`. The run does not have to be registered with the collector. |
| `provenance.build_id` | The build: the attempt ID Microcosm assigns, the same from staging through release. 1–128 characters from `A-Z`, `a-z`, `0-9`, `.`, `_` and `-`, starting with a letter or digit (`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`). This is the emitter interface's `safe_identifier` rule. |
| `provenance.country_code` | Two upper-case letters, as in run registration. |
| `provenance.release` | The release ID, with the `build_id` syntax, or `null` for a staged build that is not a release. |
| `provenance.huggingface` | Where the same bytes live on Hugging Face: an object with exactly `repo`, `commit` and `folder`, or `null`. Required (not `null`) for `calibration-diagnostics`. For `orrery-inputs` it may be `null`, for a build that exists only on the build machine. |
| `provenance.huggingface.repo` | The dataset repo, `owner/name`. Each of the two parts is 1–96 characters with the `build_id` character set, starting with a letter or digit. |
| `provenance.huggingface.commit` | 40 lowercase hex characters: the full ID of the Hugging Face commit that added the folder, which is the commit that wrote these files there. Never a later commit, a branch or a tag, so the same folder always gives the same submission ID. |
| `provenance.huggingface.folder` | 1–512 characters: one or more segments joined by `/`, each with the `build_id` syntax. There is no leading or trailing `/`, and no empty, `.` or `..` segment. Each file's Hugging Face path is `<folder>/<name>`. |
| `files[].name` | A flat file name: `[A-Za-z0-9][A-Za-z0-9._-]{0,191}`, unique within the submission. |
| `files[].role` | One of the kind's roles below. |
| `files[].size` | The file's length in bytes: an integer, at least 1, within the role's limit. The emitter's file entries (its Orrery `PublicationFile` and its bundle hand-over entries) call the same number `bytes`, and a client copies that value here unchanged. |
| `files[].sha256` | 64 lowercase hex characters. |

The collector compares and hashes every value exactly as sent. Each string
field is ASCII by its syntax, and the collector applies no case folding,
trimming or Unicode normalisation.

Unknown fields are refused (422), so a client cannot believe the collector
recorded something it ignored.

## Kinds and file roles

| Kind | Role | Count | Size limit per file | Consumer |
|---|---|---|---|---|
| `calibration-diagnostics` | `calibration_diagnostics` | exactly 1 | 1 GiB | calibration-tree worker (K4) |
| | `build_manifest` | 0 or 1 | 64 MiB | |
| | `release_manifest` | 0 or 1 | 64 MiB | |
| | `demographics` | 0 or 1 | 64 MiB | |
| `orrery-inputs` | `schema` | exactly 1 | 64 MiB | Orrery worker (O4) |
| | `execution_overlay` | exactly 1 | 32 MiB | |
| | `upstream` | 0 or more | 64 MiB | |
| | `evidence` | 0 or more | 2 GiB | kept private, never published |

The calibration roles are the files the calibration-tree publisher reads for a
build today. The Orrery roles are the inputs the projection needs
(`orrery_json_from_schema(schema, execution=overlay)`), plus the native evidence
files that stay in private intake storage. Intake checks each file's role,
count and size, not its name; the Orrery worker (O4) rejects a submission whose
files it cannot use.

### The roles the emitter sends

The emitter (emitter#27) builds submissions from a staged bundle: each bundle
file has a role in the bundle hand-over, named in emitter#27's
`microcosm_emitter_interface`. The emitter sends the contract role in
`files[].role`; bundle roles never reach the collector.

| Kind | Bundle role | Contract role sent | File Microcosm writes |
|---|---|---|---|
| `calibration-diagnostics` | `calibration_diagnostics` | `calibration_diagnostics` | the build's diagnostics file, such as `calibration_diagnostics.json` or `<dataset>.diagnostics.json` |
| | `build_manifest` | `build_manifest` | `build_manifest.json` |
| | `release_manifest` | `release_manifest` | `release_manifest.json` |
| | `demographics` | `demographics` | the build's demographics file |
| `orrery-inputs` | `orrery_schema` | `schema` | `graph.schema.json` |
| | `execution_overlay` | `execution_overlay` | `graph.execution.json` |
| | `orrery_upstream` | `upstream` | `upstream-<sha256>.json` |
| | `orrery_evidence` | `evidence` | the native evidence files: `execution.evidence.json` and `evidence-<phase>-{graph,manifest,binding,summaries}.json` |

A bundle gives a `calibration-diagnostics` submission when it has a
`calibration_diagnostics` file, and an `orrery-inputs` submission when it has
both an `orrery_schema` and an `execution_overlay` file. Its other files, such
as the dataset and its sidecars, are not submitted.

### The emitter's earlier Orrery publication roles

The emitter's direct Orrery upload to microcosm-runs, which O7 removes, uses
another set of roles: emitter#22's `PublicationFileRole`. When O7 submits a
queued upload from that path as `orrery-inputs`, its files map like this:

| `PublicationFileRole` | File | Contract role |
|---|---|---|
| `schema` | `graph.schema.json` | `schema` |
| `upstream` | `upstream-<sha256>.json` | `upstream` |
| `index` | `execution.evidence.json` | `evidence` |
| `declaration` | `evidence-<phase>-graph.json` | `evidence` |
| `manifest` | `evidence-<phase>-manifest.json` | `evidence` |
| `binding` | `evidence-<phase>-binding.json` | `evidence` |
| `summaries` | `evidence-<phase>-summaries.json` | `evidence` |
| `graph` | `graph.orrery.json` | none: the collector projects its own Orrery artifact (O4) |

That path has no execution overlay role: before microcosm#1295, the overlay
existed only inside `graph.orrery.json`. A queued upload can therefore become
an `orrery-inputs` submission only once its overlay exists as
`graph.execution.json`. How O7 obtains the overlay is decided in O7.

## Limits

| Limit | Value | Why |
|---|---|---|
| Request body | 1 MiB | The collector API refuses larger bodies (413). |
| Files per submission | 256 | Keeps the inventory and the response, with one signed URL per file, well under 1 MiB; the emitter reads at most 1 MiB of any response. |
| Total size per submission | 8 GiB | Bounds one worker job. |
| Signed URL lifetime | 30 minutes | A client that runs out of time creates the submission again for fresh URLs; the request is idempotent. |
| Job attempts | 3 per processing round | See "Attempts, dispatches and recovery". |
| Worker dispatches | 3 per processing round | See "Attempts, dispatches and recovery". |

### Signed uploads

Each upload instruction allows one `PUT` of one object. Its `headers` list
every header the signature covers, and the client sends exactly these,
unchanged, plus `Content-Length: <size>`:

| Header | Value |
|---|---|
| `Content-Type` | `application/octet-stream` |
| `Host` | the URL's host, `storage.googleapis.com`, which HTTP clients send from the URL anyway |
| `x-goog-content-length-range` | `<size>,<size>` |
| `x-goog-if-generation-match` | `0` |
| `x-goog-meta-sha256` | the file's declared SHA-256 |

So the storage service refuses an upload of any other size, records the
declared SHA-256 as object metadata, and refuses to replace an object once it
exists. The client adds no other `x-goog-` header and does not follow
redirects.

## Identifiers

The collector derives every identifier; clients never send one. Canonical JSON
means UTF-8, keys sorted, no whitespace, non-ASCII characters escaped.

| Identifier | Derivation |
|---|---|
| `files_sha256` | SHA-256 of the canonical JSON of `files`, sorted by `name`, each with exactly `name`, `role`, `size`, `sha256` |
| `submission_id` | `sub-` + the first 32 hex characters of the SHA-256 of the canonical JSON of `{contract_version, kind, licensed_data, provenance, files}` (files sorted by name). The hashed `provenance` always holds all five keys, with `null` written as JSON `null`; `huggingface`, when present, holds exactly its three keys. |
| `job_id` | `job-` + the first 32 hex characters of the SHA-256 of the canonical JSON of `{submission_id, job_type, processing_round}`. `job_type` is the kind. `processing_round` is `1` for the job completion records, and one more for each reprocessing (see "Reprocessing"). |
| Cloud Task name | `<job_id>-<dispatch number>`, counting dispatches from 1 within the job |
| Object name | `submissions/<submission_id>/<name>` in the intake bucket |
| `receipt_sha256` | SHA-256 of the canonical JSON of the receipt without `receipt_sha256` |

`files_sha256` is not the emitter's `inventory_sha256`. That digest covers the
emitter inventory's `version` and `publication_id` as well as its files, whose
sizes it calls `bytes`, so the two never match and neither replaces the other.

So the same request always names the same submission, and every request in the
flow can be retried. A staging submission and a release submission of the same
diagnostics bytes are different submissions, because their provenance differs.
A submission belongs to the Hugging Face user who created it; another user
creating the same submission gets 403.

## Statuses

| Status | Meaning | Final |
|---|---|---|
| `awaiting_upload` | Recorded; files may still be missing. | no |
| `queued` | Completion found every object with the declared size and recorded digest; a job is recorded. Also the status between a failed attempt and the next one, and after an operator asks for reprocessing. | no |
| `processing` | A worker holds the job. | no |
| `succeeded` | The worker matched every file's SHA-256 and the kind's handler finished. | yes |
| `rejected` | A stored object or its bytes did not match the inventory, or the handler refused the content. Repeating the same submission cannot succeed. | yes |
| `failed` | The job failed for another reason on its last attempt, or did not finish within its attempts or dispatches. An operator can reprocess it. | yes |

Final means final for the client: only an operator's reprocessing returns a
final submission to `queued`.

## Responses

### Response bodies

Create, completion and status answer with the submission's status document:

```json
{
  "contract_version": 1,
  "submission_id": "sub-…",
  "kind": "calibration-diagnostics",
  "status": "awaiting_upload",
  "files_sha256": "…",
  "created_at": "2026-10-20T09:00:00.123456+00:00",
  "updated_at": "2026-10-20T09:00:00.123456+00:00",
  "accepted_at": null,
  "finished_at": null,
  "job": null,
  "rejection": null,
  "uploads": [
    {
      "name": "calibration_diagnostics.json",
      "method": "PUT",
      "url": "https://storage.googleapis.com/…",
      "headers": {
        "Content-Type": "application/octet-stream",
        "Host": "storage.googleapis.com",
        "x-goog-content-length-range": "18734221,18734221",
        "x-goog-if-generation-match": "0",
        "x-goog-meta-sha256": "…"
      },
      "expires_at": "2026-10-20T09:30:00.123456+00:00"
    }
  ]
}
```

- **Timestamps** are ISO 8601 in UTC with an explicit `+00:00` offset.
  `accepted_at` and `finished_at` stay `null` until the submission gets there.
- **`job`** is `null` until completion records a job, then
  `{"job_id": "job-…", "processing_round": 1, "attempts": 0}` for the
  current processing round, counting the worker's attempts in that round.
- **`rejection`** is `null` unless the status is `rejected` or `failed`, and
  then `{"code": …, "message": …}` (see "Receipt").
- **`uploads`** is only in create responses. It holds one instruction for each
  file not yet stored, in name order, and is empty once the status is past
  `awaiting_upload`. Each instruction's `method` is `PUT`, its `headers` are
  those in "Signed uploads", and `expires_at` is when its URL stops working,
  30 minutes after signing.

Every error body has a `detail` message. Errors specific to intake also have a
`code`, which the tables below name.

### Collector answers

| Request | Answer | What the client does |
|---|---|---|
| Exchange | 200 with `"scope": "artifacts:submit"` | Use the token until shortly before `expires_in` runs out, then exchange again. |
| Exchange | 200 with another `scope`, or none | Intake is not available (see "When intake is not available"). |
| Exchange | 404 `intake_not_enabled` | Intake is not available. |
| Exchange | 401 or 403 | Hugging Face refused the token (401), or its user is not a PolicyEngine organization member (403). Final for that Hugging Face token. |
| Create | 201 (new) or 200 (exists) | Upload each listed file, then complete. |
| Create | 403 `submission_owned_by_another_user` | Final. |
| Complete | 202 (`queued` or `processing`) or 200 (final) | Poll the status. |
| Complete | 200 with status `rejected` and the rejection code `stored_object_mismatch` | Final: a stored object has another size or recorded SHA-256, and stored objects are never replaced. |
| Complete | 409 `files_missing`, with `problems`: one `{"name": …, "problem": "missing"}` for each file not stored | Create again for fresh URLs, upload those files, complete again. |
| Complete | 503 `worker_queue_unavailable`, with `Retry-After: 30` | Retry completion after 30 seconds; the job stays recorded until it is dispatched. |
| Receipt | 200 once final; before that, 409 `submission_not_final`, with `status` | Poll the status first. |
| Complete, status or receipt | 404 `submission_not_found` | The collector has no such submission (for example after a database reset): create it again. |
| Any submission path | 404 `intake_not_enabled`, or 404 without a `code` | Intake is not available. |
| Any | 401 | The session token expired or was refused: exchange again. |
| Any | 403 without a `code` | The token's scope does not allow the request: exchange again for `artifacts:submit`. Final if a fresh token is refused the same way. |
| Any | 413 or 422 | Final: the request breaks this contract. |
| Any | 429 | Too many requests, from Cloud Run or the collector: retry after `Retry-After` seconds when it is given, otherwise with exponential backoff. |
| Any | 503 without a `code`, with `Retry-After: 60` | Maintenance mode: during a deployment the collector answers every path except `/health` this way. Retry after 60 seconds. |
| Any | Another 5xx, a timeout or a connection error | Retry with exponential backoff, keeping the files queued. Every request in the flow can be repeated. |

### Storage answers to an upload

| Answer | Meaning | What the client does |
|---|---|---|
| 200 | Stored. | Upload the next file. |
| 412 | The object already exists. | Treat it as stored; completion checks it. |
| 408, 429, another 5xx, a timeout or a connection error | A transient failure. | Retry the same URL with exponential backoff until `expires_at`, then create again for fresh URLs. |
| Another 4xx, such as 400 or 403 | The URL cannot be used: it expired, or the request does not match its signature. | Create again for fresh URLs (create signs only files not yet stored) and upload with those, without changing any header. Retry with exponential backoff, keeping the files queued. |

## Receipt

```json
{
  "contract_version": 1,
  "submission_id": "sub-…",
  "kind": "calibration-diagnostics",
  "status": "succeeded",
  "licensed_data": true,
  "provenance": {"…": "as submitted"},
  "files_sha256": "…",
  "files": [
    {"name": "calibration_diagnostics.json", "role": "calibration_diagnostics",
     "size": 18734221, "sha256": "…", "generation": "1760866403123456"}
  ],
  "submitted_by": "huggingface-username",
  "created_at": "…",
  "accepted_at": "…",
  "finished_at": "…",
  "job": {"job_id": "job-…", "processing_round": 1, "attempts": 1},
  "result": {"…": "the handler's output, for example the published build's reference"},
  "rejection": null,
  "receipt_sha256": "…"
}
```

`rejection` is `{code, message}` for a rejected or failed submission, and
`result` is `null` then. `job` is `null` for a submission rejected at
completion, which never had a job, and its `files` have no `generation`,
because completion recorded none. The collector's own codes are:

| Code | Status | Meaning |
|---|---|---|
| `stored_object_mismatch` | `rejected` | At completion, a stored object had another size or recorded SHA-256. |
| `content_mismatch` | `rejected` | The worker read bytes that differ from the inventory. |
| `object_missing` | `rejected` | The worker found an object gone at its recorded generation. |
| `worker_error` | `failed` | The last attempt failed with an error that was not a rejection. |
| `attempts_exhausted` | `failed` | The job's last attempt ended without recording a result. |
| `worker_unavailable` | `failed` | No worker execution finished the job within its dispatches. |

A handler may reject with its own code. The emitter stores the receipt beside
its queued upload. The receipt of a reprocessed submission describes its
latest processing round.

## Disclosure

- **Intake storage is private for every submission.** The bucket uses uniform
  bucket-level access with public access prevention enforced, and only the
  collector's own service accounts can read it. A signed URL allows only the
  upload of one object of one size.
- **Orrery outputs of licensed builds stay private until cleared.** The Orrery
  worker (O4) records every Orrery artifact derived from a submission with
  `licensed_data: true` with visibility `private`, and never makes it public
  itself; it becomes public only after output clearance (decision D5). Orrery
  artifacts from submissions with `licensed_data: false` may be public.
- **Calibration data may be public, licensed or not.** Calibration trees built
  from a `calibration-diagnostics` submission (K4) may be published publicly,
  including when `licensed_data` is `true` (decision D10).
- **Native evidence is never published,** whatever `licensed_data` says. Public
  Orrery artifacts do not link to it (O4).
- **`licensed_data` has no default,** so a producer must state it. The worker
  framework applies these rules for every handler, so K4 and O4 never derive
  them again.
- **Status and receipts go only to the submitting user,** and hold names,
  sizes and digests, never file contents.

## Workers

The worker framework claims a job (one holder at a time, through a lease),
checks every file's bytes, and then calls the handler registered for the kind
with the submission record and a reader for each file at its recorded
generation. A handler returns a JSON result, which goes into the receipt, or
refuses the content, which rejects the submission. The calibration-tree worker
(K4) and the Orrery worker (O4) register the handlers for their kinds. Until
they do, both kinds only have their bytes checked and are kept in intake
storage, with the result `{"processed_by": "intake-only", "verified_files": <count>}`;
an operator reprocesses them once a handler exists.

### Attempts, dispatches and recovery

- **A dispatch** creates one Cloud Task, named `<job_id>-<dispatch number>`,
  which starts one execution of the worker job. A task name can be used only
  once, so repeating a dispatch never starts a second execution.
- **An attempt** is one claim of the job by a worker. A claim leases the job
  for 65 minutes, longer than the worker job's one-hour task timeout, so a
  lease expires only after its attempt has ended. Cloud Run runs a failed task
  up to twice more within the same execution, and each of those runs is a new
  attempt by the same lease holder.
- **Limits:** 3 attempts and 3 dispatches in each processing round. An error
  on the third attempt fails the job with `worker_error`.
- **Recovery** runs every 10 minutes and finds attempts that ended without
  recording a result, such as a memory kill, a task timeout, or an execution
  that exited before claiming its job:
  - A job whose lease expired after its third attempt is `failed` with
    `attempts_exhausted`. With fewer attempts, it is queued and dispatched
    again.
  - A queued job with no claim, release or dispatch for 30 minutes is
    dispatched again.
  - A job that would need a fourth dispatch is `failed` with
    `worker_unavailable`.

  So every job reaches a final status within its 3 attempts or 3 dispatches.

### Reprocessing

An operator can reprocess any final submission that has recorded object
generations, which excludes one rejected at completion. Reprocessing records a
new job with the next `processing_round`, which reads the same object
generations, and returns the submission to `queued`. Earlier jobs and their
results stay recorded. This is how stored Orrery inputs are projected again
when the projection or Orrery changes, and how intake-only submissions are
processed once K4 or O4 registers a handler.
