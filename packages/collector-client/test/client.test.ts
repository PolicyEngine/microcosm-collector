// The client against the collector's recorded read API v2 interactions
// (`contracts/read-api-v2/interactions`), and its own refusals and errors.
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { describe, test } from "node:test";

import {
  CollectorRequestError,
  CollectorResponseError,
  createCollectorClient,
  READ_TOKEN_HEADER,
  type CollectorClient,
  type CollectorClientOptions,
} from "../src/index.ts";

const BASE_URL = "https://collector.example";
const READ_TOKEN = "dashboard-read-token";
const INTERACTIONS_URL = new URL(
  "../../../contracts/read-api-v2/interactions/",
  import.meta.url,
);

interface RecordedRequest {
  method: string;
  path: string;
  headers?: Record<string, string>;
}

interface Interaction {
  request: RecordedRequest;
  response: { status: number; body: unknown };
}

const interactions = new Map<string, Interaction>(
  readdirSync(INTERACTIONS_URL)
    .filter((name) => name.endsWith(".json"))
    .map((name) => [
      name.replace(/\.json$/, ""),
      JSON.parse(
        readFileSync(new URL(name, INTERACTIONS_URL), "utf8"),
      ) as Interaction,
    ]),
);

function interaction(name: string): Interaction {
  const recorded = interactions.get(name);
  assert.ok(recorded, `No recorded interaction ${name}.`);
  return recorded;
}

interface SentRequest {
  url: string;
  method: string;
  headers: Headers;
}

// A fetch that answers each request from the recorded interactions, and fails
// a request that none of them recorded.
function recordedCollector(): { fetch: typeof fetch; sent: SentRequest[] } {
  const sent: SentRequest[] = [];
  const fetchStub = async (
    input: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> => {
    const url = String(input);
    const headers = new Headers(init?.headers);
    const method = init?.method ?? "GET";
    sent.push({ url, method, headers });
    for (const { request, response } of interactions.values()) {
      const recordedHeaders = Object.entries(request.headers ?? {});
      if (
        `${BASE_URL}${request.path}` === url &&
        request.method === method &&
        recordedHeaders.every(([name, value]) => headers.get(name) === value) &&
        (request.headers?.[READ_TOKEN_HEADER] !== undefined ||
          !headers.has(READ_TOKEN_HEADER))
      ) {
        return Response.json(response.body, { status: response.status });
      }
    }
    throw new Error(`No recorded interaction for ${method} ${url}.`);
  };
  return { fetch: fetchStub as typeof fetch, sent };
}

function clientFor(
  fetchFunction: typeof fetch,
  overrides: Partial<CollectorClientOptions> = {},
): CollectorClient {
  return createCollectorClient({
    baseUrl: BASE_URL,
    readToken: READ_TOKEN,
    fetch: fetchFunction,
    ...overrides,
  });
}

function body(name: string): unknown {
  return interaction(name).response.body;
}

function nextBefore(name: string): string {
  const cursor = (body(name) as { next_before: string | null }).next_before;
  assert.ok(cursor, `${name} has no cursor.`);
  return cursor;
}

// Each recorded interaction, and how the client makes its request.
const CASES: Record<string, (client: CollectorClient) => Promise<unknown>> = {
  "list-runs": (client) => client.listRuns(),
  "list-runs-gb-first-page": (client) =>
    client.listRuns({ country: "GB", limit: 1 }),
  "list-runs-gb-second-page": (client) =>
    client.listRuns({
      country: "GB",
      limit: 1,
      before: nextBefore("list-runs-gb-first-page"),
    }),
  "list-runs-gb-after-the-last-page": (client) =>
    client.listRuns({
      country: "GB",
      limit: 1,
      before: nextBefore("list-runs-gb-second-page"),
    }),
  "list-runs-of-a-country-without-runs": (client) =>
    client.listRuns({ country: "FR" }),
  "get-run-us-run-completed": (client) => client.getRun("us-run-completed"),
  "get-run-us-run-failed": (client) => client.getRun("us-run-failed"),
  "get-run-gb-run-blocked": (client) => client.getRun("gb-run-blocked"),
  "get-run-gb-run-restarted": (client) => client.getRun("gb-run-restarted"),
  "get-run-us-run-registered": (client) => client.getRun("us-run-registered"),
};

// Recorded refusals, and how the client meets each.
const REFUSALS: Record<string, (client: CollectorClient) => Promise<void>> = {
  "get-run-not-found": async (client) => {
    assert.equal(await client.getRun("no-such-run"), null);
  },
  "get-run-with-an-invalid-id": async (client) => {
    await assert.rejects(client.getRun("not a run id"), RangeError);
  },
  "list-runs-without-a-credential": async () => {
    const collector = recordedCollector();
    const client = clientFor(collector.fetch, { readToken: " " });
    await assert.rejects(client.listRuns(), /read credential is empty/);
    assert.equal(collector.sent.length, 0);
  },
  "list-runs-with-a-wrong-credential": async () => {
    const collector = recordedCollector();
    const client = clientFor(collector.fetch, {
      readToken: "not-the-read-token",
    });
    await assert.rejects(client.listRuns(), (error: unknown) => {
      assert.ok(error instanceof CollectorRequestError);
      assert.equal(error.status, 401);
      assert.equal(error.retryable, false);
      assert.deepEqual(
        error.detail,
        (body("list-runs-with-a-wrong-credential") as { detail: unknown })
          .detail,
      );
      return true;
    });
  },
  "list-runs-with-an-invalid-cursor": async (client) => {
    await assert.rejects(client.listRuns({ before: "not-a-cursor" }), (error) => {
      assert.ok(error instanceof CollectorRequestError);
      assert.equal(error.status, 422);
      assert.deepEqual(
        error.detail,
        (body("list-runs-with-an-invalid-cursor") as { detail: unknown })
          .detail,
      );
      return true;
    });
  },
  "list-runs-with-an-invalid-limit": async (client) => {
    await assert.rejects(client.listRuns({ limit: 0 }), RangeError);
  },
};

describe("recorded interactions", () => {
  test("every recorded interaction has a client case", () => {
    assert.deepEqual(
      [...interactions.keys()].sort(),
      [...Object.keys(CASES), ...Object.keys(REFUSALS)].sort(),
    );
  });

  for (const [name, call] of Object.entries(CASES)) {
    test(`${name} returns the recorded answer`, async () => {
      const collector = recordedCollector();
      const result = await call(clientFor(collector.fetch));

      assert.deepEqual(result, body(name));
      assert.equal(collector.sent.length, 1);
      const [request] = collector.sent;
      assert.equal(request?.url, `${BASE_URL}${interaction(name).request.path}`);
      assert.equal(request?.headers.get(READ_TOKEN_HEADER), READ_TOKEN);
    });
  }

  for (const [name, check] of Object.entries(REFUSALS)) {
    test(`${name} is refused`, async () => {
      await check(clientFor(recordedCollector().fetch));
    });
  }
});

// A collector serving `total` runs, newest first, in pages of at most
// `largestPage`, with a cursor whenever a page is full.
function pagingCollector(total: number, largestPage = 200) {
  const runs = Array.from({ length: total }, (_, index) => ({
    ...(body("list-runs") as { runs: Record<string, unknown>[] }).runs[0],
    run_id: `run-${String(total - index).padStart(4, "0")}`,
  }));
  const limits: number[] = [];
  const fetchStub = async (input: string | URL | Request) => {
    const url = new URL(String(input));
    const limit = Number(url.searchParams.get("limit") ?? "60");
    limits.push(limit);
    const before = url.searchParams.get("before");
    const start = before === null ? 0 : Number(before);
    const size = Math.min(limit, largestPage);
    const page = runs.slice(start, start + size);
    return Response.json({
      runs: page,
      next_before: page.length === size ? String(start + size) : null,
    });
  };
  return { fetch: fetchStub as typeof fetch, limits, runs };
}

describe("listRecentRuns", () => {
  test("asks each page only for the runs still needed", async () => {
    const collector = pagingCollector(500, 150);
    const runs = await clientFor(collector.fetch).listRecentRuns({ count: 320 });

    assert.deepEqual(
      runs.map((run) => run.run_id),
      collector.runs.slice(0, 320).map((run) => run.run_id),
    );
    assert.deepEqual(collector.limits, [200, 170, 20]);
  });

  test("stops when the collector has no more runs", async () => {
    const collector = pagingCollector(3);
    const runs = await clientFor(collector.fetch).listRecentRuns({
      country: "US",
      count: 10,
    });

    assert.equal(runs.length, 3);
    assert.deepEqual(collector.limits, [10]);
  });

  test("refuses a repeated cursor", async () => {
    const fetchStub = async () =>
      Response.json({ runs: [], next_before: "same-cursor" });
    await assert.rejects(
      clientFor(fetchStub as typeof fetch).listRecentRuns({ count: 5 }),
      CollectorResponseError,
    );
  });

  test("refuses a count below 1", async () => {
    await assert.rejects(
      clientFor(recordedCollector().fetch).listRecentRuns({ count: 0 }),
      RangeError,
    );
  });
});

function answering(response: Response): typeof fetch {
  return (async () => response) as typeof fetch;
}

describe("errors", () => {
  test("a deployment's 503 is retryable after its Retry-After", async () => {
    const response = Response.json(
      { detail: "Telemetry service is temporarily unavailable." },
      { status: 503, headers: { "Retry-After": "60" } },
    );
    await assert.rejects(clientFor(answering(response)).listRuns(), (error) => {
      assert.ok(error instanceof CollectorRequestError);
      assert.equal(error.retryable, true);
      assert.equal(error.retryAfterSeconds, 60);
      return true;
    });
  });

  test("a 404 that is not a missing run is an error", async () => {
    const response = Response.json({ detail: "Not Found" }, { status: 404 });
    await assert.rejects(
      clientFor(answering(response)).getRun("us-run-completed"),
      (error) => error instanceof CollectorRequestError && error.status === 404,
    );
  });

  test("an answer that is not a run page is refused", async () => {
    await assert.rejects(
      clientFor(answering(Response.json({ items: [] }))).listRuns(),
      CollectorResponseError,
    );
    await assert.rejects(
      clientFor(answering(new Response("<html>", { status: 200 }))).listRuns(),
      CollectorResponseError,
    );
  });

  test("an answer for another run is refused", async () => {
    const response = Response.json(body("get-run-us-run-failed"));
    await assert.rejects(
      clientFor(answering(response)).getRun("us-run-completed"),
      CollectorResponseError,
    );
  });

  test("a null answer for a run is refused", async () => {
    await assert.rejects(
      clientFor(answering(Response.json(null))).getRun("us-run-completed"),
      CollectorResponseError,
    );
  });

  test("a request that takes too long is abandoned", async () => {
    const fetchStub = (_input: unknown, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(init.signal?.reason),
        );
      });
    await assert.rejects(
      clientFor(fetchStub as typeof fetch, { timeoutMs: 10 }).listRuns(),
      { name: "TimeoutError" },
    );
  });
});

describe("configuration", () => {
  test("refuses plain HTTP except on localhost", () => {
    assert.throws(() =>
      createCollectorClient({ baseUrl: "http://collector.example", readToken: "x" }),
    );
    createCollectorClient({ baseUrl: "http://localhost:8080", readToken: "x" });
    createCollectorClient({ baseUrl: "http://127.0.0.1:8080", readToken: "x" });
  });

  test("refuses credentials and query data in the address", () => {
    for (const baseUrl of [
      "https://user:secret@collector.example",
      "https://collector.example/?token=x",
      "https://collector.example/#x",
    ]) {
      assert.throws(() => createCollectorClient({ baseUrl, readToken: "x" }));
    }
  });

  test("keeps a path prefix and drops a trailing slash", async () => {
    const collector = pagingCollector(1);
    const requestedUrls: string[] = [];
    await createCollectorClient({
      baseUrl: "https://collector.example/prefix/",
      readToken: "x",
      fetch: async (input, init) => {
        requestedUrls.push(String(input));
        return collector.fetch(`${BASE_URL}/v2/runs`, init);
      },
    }).listRuns();

    assert.deepEqual(requestedUrls, ["https://collector.example/prefix/v2/runs"]);
  });

  test("asks a credential function for each request", async () => {
    let calls = 0;
    const collector = recordedCollector();
    const client = clientFor(collector.fetch, {
      readToken: async () => {
        calls += 1;
        return READ_TOKEN;
      },
    });
    await client.listRuns();
    await client.getRun("us-run-completed");

    assert.equal(calls, 2);
  });

  test("refuses a bad country before sending anything", async () => {
    const collector = recordedCollector();
    await assert.rejects(
      clientFor(collector.fetch).listRuns({ country: "uk" }),
      RangeError,
    );
    assert.equal(collector.sent.length, 0);
  });
});
