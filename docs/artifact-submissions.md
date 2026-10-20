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
   `{"scope": "artifacts:submit"}` returns a collector session token whose
   `scope` claim is `artifacts:submit`. An exchange without a body, or with
   `{}`, still returns a `telemetry:write` token, as today. Each scope works
   only on its own endpoints.
2. **Create.** `POST /v1/submissions` with the submission (below). The
   collector validates it, derives the submission ID, records it, and returns
   its status with one signed upload URL for each file that is not yet stored.
3. **Upload.** The client sends each file with `PUT` to its URL, with exactly
   the headers the response lists. An object can be created only once, so a
   repeated upload of a stored file gets 412, which means it is already there.
4. **Complete.** `POST /v1/submissions/{submission_id}/complete`. The collector
   lists the submission's stored objects and checks that every inventory file
   exists, has the declared size, and carries the declared SHA-256 in its
   metadata. It records each object's generation, so later overwrites cannot
   change what is processed, then records a job and enqueues it (Cloud Tasks
   starts a worker job execution).
5. **Process.** The worker claims the job, reads each file at its recorded
   generation and recomputes its SHA-256, then runs the kind's handler.
6. **Result.** The client polls `GET /v1/submissions/{submission_id}` and,
   once the status is final, reads `GET /v1/submissions/{submission_id}/receipt`.

The API checks sizes and recorded digests without reading file bytes; the
worker checks the bytes. Hashing gigabytes inside a 30 s request is not safe,
so the byte-level SHA-256 check belongs to the worker.

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
| Job attempts | 3 | After the third failure that is not a rejection, the job is `failed`. |

Each signed URL allows one `PUT` of one object. Its signature covers the
headers `Content-Type: application/octet-stream`, `x-goog-meta-sha256: <sha256>`,
`x-goog-content-length-range: <size>,<size>` and `x-goog-if-generation-match: 0`,
so the storage service refuses an upload of any other size, and refuses to
replace an object once it exists.

## Identifiers

The collector derives every identifier; clients never send one. Canonical JSON
means UTF-8, keys sorted, no whitespace, non-ASCII characters escaped.

| Identifier | Derivation |
|---|---|
| `files_sha256` | SHA-256 of the canonical JSON of `files`, sorted by `name`, each with exactly `name`, `role`, `size`, `sha256` |
| `submission_id` | `sub-` + the first 32 hex characters of the SHA-256 of the canonical JSON of `{contract_version, kind, licensed_data, provenance, files}` (files sorted by name). The hashed `provenance` always holds all five keys, with `null` written as JSON `null`; `huggingface`, when present, holds exactly its three keys. |
| `job_id` | `job-` + the first 32 hex characters of the SHA-256 of the canonical JSON of `{submission_id, job_type}`; `job_type` is the kind |
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
| `queued` | Completion found every object with the declared size and recorded digest; a job is recorded. Also the status between a failed attempt and the next one. | no |
| `processing` | A worker holds the job. | no |
| `succeeded` | The worker matched every file's SHA-256 and the kind's handler finished. | yes |
| `rejected` | The bytes did not match the inventory, or the handler refused the content. Repeating the same submission cannot succeed. | yes |
| `failed` | The handler failed for another reason on every attempt. An operator can re-run it. | yes |

## Responses

| Request | Response | What the client does |
|---|---|---|
| Exchange for `artifacts:submit` | 404: intake is not enabled on this collector | Keep the files queued; retry later. |
| Create | 201 (new) or 200 (exists), with the status and `uploads` | Upload each listed file. `uploads` is empty once the status is past `awaiting_upload`. |
| Complete | 202 (`queued` or `processing`), 200 (final) | Poll the status. |
| Complete | 409 with `problems`, each `{name, problem}` where `problem` is `missing`, `size_mismatch` or `sha256_mismatch` | Create again for fresh URLs, upload those files, complete again. |
| Complete | 503: the worker queue is unavailable | Retry completion; the job stays recorded until it is enqueued. |
| Receipt | 200 when final, 409 before | Poll the status first. |
| Any | 401 | Exchange again. |
| Any | 403: another user's submission, or a token without `artifacts:submit` | Final. |
| Any | 404 on a submission path | The submission is unknown (for example after a database reset): create it again. |
| Any | 413 or 422 | Final: the submission breaks this contract. |

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
  "job": {"job_id": "job-…", "attempts": 1},
  "result": {"…": "the handler's output, for example the published build's reference"},
  "rejection": null,
  "receipt_sha256": "…"
}
```

`rejection` is `{code, message}` for a rejected or failed submission, and
`result` is `null` then. The collector's own codes are `content_mismatch` and
`object_missing` (rejected: the stored bytes differ from the inventory, or an
object is gone), and `worker_error` and `attempts_exhausted` (failed); a
handler may reject with its own code. The emitter stores the receipt beside
its queued upload.

## Disclosure

- **Intake storage is private for every submission.** The bucket uses uniform
  bucket-level access with public access prevention enforced, and only the
  collector's own service accounts can read it. A signed URL allows only the
  upload of one object of one size.
- **`licensed_data: true` keeps derived outputs private.** Workers record every
  output derived from such a submission with visibility `private`, and never
  make it public themselves; it becomes public only after output clearance
  (decision D5). `licensed_data` has no default, so a producer must state it.
- **Native evidence is never published,** whatever `licensed_data` says. Public
  Orrery artifacts do not link to it (O4).
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
storage, with the result `{"processed_by": "intake-only", "verified_files": <count>}`.
