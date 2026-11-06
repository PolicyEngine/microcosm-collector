# @policyengine/microcosm-collector-client

A typed client for version 2 of the Microcosm collector's read API: the runs a
collector has recorded, and for each run its state together with the telemetry
events its build sent. It runs on the server; the read credential must never
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

- `listRuns({ country, limit, before })` returns one page, newest registration
  first; pass a page's `next_before` as `before` for the next one.
- `listRecentRuns({ country, count })` reads pages until it has `count` runs or
  the collector has no more.
- `getRun(runId)` returns `{ run, events }`: the run's state, rebuilt from
  exactly the events returned, and every event in the order the collector
  applies them. Timestamps are in UTC.

An error answer throws `CollectorRequestError`, with `status`, `detail`, and
`retryable` for 408, 429 and 5xx, including the 503 that a collector answers
with while it deploys (`retryAfterSeconds` holds its `Retry-After`). An answer
that is not a read API v2 answer throws `CollectorResponseError`. A request
that takes longer than `timeoutMs` (30 seconds by default), including reading
its answer, rejects with `fetch`'s `TimeoutError`. Arguments the collector
would refuse throw `RangeError` before any request, and so does creating a
client with a `timeoutMs` that is not a positive whole number.

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
