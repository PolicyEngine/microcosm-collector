# @policyengine/microcosm-collector-client

A typed client for version 2 of the Microcosm collector's read API: the runs a
collector has recorded, and for each run its state together with the telemetry
events its build sent. It runs on the server; neither credential it sends may
reach a browser.

```ts
import { createCollectorClient } from "@policyengine/microcosm-collector-client";

const collector = createCollectorClient({
  baseUrl: process.env.MICROCOSM_TELEMETRY_COLLECTOR_URL!,
  // A string, or a function called before each request.
  readToken: () => readCredentialFromSecretManager(),
});

const runs = await collector.listRecentRuns({ country: "GB", count: 60 });
const detail = await collector.getRun(runs[0].run_id); // null if unknown
console.log(detail?.run.status, detail?.events.length);
```

On Vercel, a server function can send its deployment's Vercel OIDC token
instead, once the collector lists the deployment's project and environment
among its readers (`TELEMETRY_VERCEL_OIDC_READERS`):

```ts
import { getVercelOidcToken } from "@vercel/oidc";
import { createCollectorClient } from "@policyengine/microcosm-collector-client";

const collector = createCollectorClient({
  baseUrl: process.env.MICROCOSM_TELEMETRY_COLLECTOR_URL!,
  // Called before each request, so each request carries the current token.
  vercelOidcToken: () => getVercelOidcToken(),
});
```

A client takes exactly one of `readToken` and `vercelOidcToken`; both or
neither throws a `TypeError`. The read credential is sent in
`X-Telemetry-Read-Token`, the Vercel OIDC token as `Authorization: Bearer`. An
empty or blank credential throws before any request is sent. The token's
audience is the Vercel team, not the collector, so never log it or pass it to
another service. This package does not depend on `@vercel/oidc`.

- `listRuns({ country, limit, before })` returns one page, newest registration
  first; pass a page's `next_before` as `before` for the next one.
- `listRecentRuns({ country, count })` reads pages until it has `count` runs or
  the collector has no more.
- `getRun(runId)` returns `{ run, events }`: the run's state, rebuilt from
  exactly the events returned, and every event in the order the collector
  applies them. Timestamps are in UTC.

An error answer throws `CollectorRequestError`, with `status`, `detail`, and
`retryable` for 408, 429 and 5xx, including the 503 that a collector answers
with while it deploys (`retryAfterSeconds` holds its `Retry-After`) or while
it cannot read Vercel's signing keys (without `Retry-After`). A Vercel OIDC
token the collector refuses answers 401, or 403 when its project and
environment may not read that collector. An answer
that is not a read API v2 answer throws `CollectorResponseError`. A request
that takes longer than `timeoutMs` (30 seconds by default), including reading
its answer, rejects with `fetch`'s `TimeoutError`. Arguments the collector
would refuse throw `RangeError` before any request, and so does creating a
client with a `timeoutMs` that is not a whole number of milliseconds from 1 to
2,147,483,647, the longest delay Node's timers allow.

The version of this package is the version of the collector release whose read
API it describes. Its types are generated from that release's OpenAPI document,
`contracts/read-api-v2/openapi.json` in PolicyEngine/microcosm-collector, and
its tests answer it with the interactions recorded there.

## Development

```bash
npm ci
npm run generate   # src/schema.ts from the OpenAPI document
npm run typecheck
npm test
npm run build
```
