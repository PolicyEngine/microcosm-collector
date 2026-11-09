// A typed client for version 2 of the Microcosm collector's read API. The
// types come from `contracts/read-api-v2/openapi.json` in the collector's
// source tree (see `src/schema.ts`), and the tests answer the client with the
// interactions recorded there.
import type { components } from "./schema.js";

export type { components, operations, paths } from "./schema.js";

type Schemas = components["schemas"];
export type RunSummary = Schemas["RunSummary"];
export type RunPage = Schemas["RunPage"];
export type RunState = Schemas["RunState"];
export type RunDetail = Schemas["RunDetail"];
export type RawEvent = Schemas["RawEvent"];
export type Resources = Schemas["Resources"];
export type RunWork = Schemas["RunWork"];
export type RunFailure = Schemas["RunFailure"];
export type RunBlock = Schemas["RunBlock"];
export type MachineEnvironment = Schemas["MachineEnvironment"];
export type ModalContainer = Schemas["ModalContainer"];
export type TimedReading = Schemas["TimedReading"];
export type ProducerSleep = Schemas["ProducerSleep"];
export type RunStatus = RunSummary["status"];

/** The request header that carries the dashboard read credential. */
export const READ_TOKEN_HEADER = "X-Telemetry-Read-Token";
/** The largest page the collector answers; it refuses a larger `limit`. */
export const MAX_PAGE_SIZE = 200;
/** How long a request may take, from sending it to reading its whole answer. */
export const DEFAULT_TIMEOUT_MS = 30_000;

/** A run id the collector accepts; the OpenAPI document gives the same. */
export const RUN_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$/;
/** A country code the collector accepts, such as "US" or "GB". */
export const COUNTRY_PATTERN = /^[A-Z]{2}$/;
// The collector's answer for a run it has no record of. Any other 404, such
// as a collector without version 2, is an error.
const RUN_NOT_FOUND = "Run was not found.";
const MISSING_RUN = Symbol("missing run");

export interface CollectorClientOptions {
  /** The collector's address: HTTPS, or HTTP on localhost or 127.0.0.1. */
  baseUrl: string;
  /**
   * The dashboard read credential, or a function that returns it. The
   * function is called for every request, so the credential may rotate.
   */
  readToken: string | (() => string | Promise<string>);
  /** The `fetch` to send requests with; the global one by default. */
  fetch?: typeof globalThis.fetch | undefined;
  /**
   * How long a request may take, in whole milliseconds, from sending it to
   * reading its whole answer; `DEFAULT_TIMEOUT_MS` by default.
   */
  timeoutMs?: number | undefined;
}

export interface ListRunsQuery {
  /** Only runs of this country: two capital letters, such as "US" or "GB". */
  country?: string | undefined;
  /** From 1 to 200; the collector's default is 60. */
  limit?: number | undefined;
  /** The previous page's `next_before`. */
  before?: string | null | undefined;
}

export interface RecentRunsQuery {
  /** Only runs of this country: two capital letters, such as "US" or "GB". */
  country?: string | undefined;
  /** How many of the most recently registered runs to return, at least 1. */
  count: number;
}

export interface CollectorClient {
  /** One page of runs, newest registration first. */
  listRuns(query?: ListRunsQuery): Promise<RunPage>;
  /**
   * The `count` most recently registered runs, newest first, read page by
   * page; each page asks only for the runs still needed.
   */
  listRecentRuns(query: RecentRunsQuery): Promise<RunSummary[]>;
  /**
   * A run's state and the events it was rebuilt from, or null when the
   * collector has no record of the run.
   */
  getRun(runId: string): Promise<RunDetail | null>;
}

/** The collector answered with an error status. */
export class CollectorRequestError extends Error {
  readonly status: number;
  /** True for 408, 429 and 5xx, including the 503 a deployment answers with. */
  readonly retryable: boolean;
  /** The answer's `Retry-After` in seconds, when it gave one. */
  readonly retryAfterSeconds: number | null;
  /** The answer's `detail`: a sentence, or FastAPI's validation errors. */
  readonly detail: unknown;

  constructor(
    status: number,
    detail: unknown,
    retryAfterSeconds: number | null,
  ) {
    super(`Collector request failed (${status}).`);
    this.name = "CollectorRequestError";
    this.status = status;
    this.retryable = status === 408 || status === 429 || status >= 500;
    this.retryAfterSeconds = retryAfterSeconds;
    this.detail = detail;
  }
}

/** The collector answered 200 with something that is not a read API v2 answer. */
export class CollectorResponseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CollectorResponseError";
  }
}

type JsonObject = Record<string, unknown>;

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireObject(value: unknown, label: string): JsonObject {
  if (!isObject(value)) {
    throw new CollectorResponseError(`Collector ${label} must be an object.`);
  }
  return value;
}

function requireString(value: unknown, label: string): string {
  if (typeof value !== "string" || !value) {
    throw new CollectorResponseError(
      `Collector ${label} must be a non-empty string.`,
    );
  }
  return value;
}

function collectorBaseUrl(value: string): string {
  const url = new URL(value);
  const local = url.hostname === "localhost" || url.hostname === "127.0.0.1";
  if (url.protocol !== "https:" && !(local && url.protocol === "http:")) {
    throw new Error("The collector address must use HTTPS except on localhost.");
  }
  // A bare trailing "?" or "#" leaves `search` and `hash` empty but would
  // still end up in every request's URL.
  if (url.username || url.password || /[?#]/.test(url.href)) {
    throw new Error(
      "The collector address must not contain credentials or query data.",
    );
  }
  return url.toString().replace(/\/+$/, "");
}

// Node's timers take at most this many milliseconds: a longer delay fires
// after 1 millisecond, and from 2^32 `AbortSignal.timeout` throws.
const LONGEST_TIMEOUT_MS = 2 ** 31 - 1;

function requestTimeout(value: number | undefined): number {
  if (value === undefined) return DEFAULT_TIMEOUT_MS;
  if (!Number.isInteger(value) || value < 1 || value > LONGEST_TIMEOUT_MS) {
    throw new RangeError(
      `A timeout is a whole number of milliseconds from 1 to ${LONGEST_TIMEOUT_MS}.`,
    );
  }
  return value;
}

// The timeout aborted the request, possibly while its answer was being read.
function isAbandoned(error: unknown): boolean {
  return (
    error instanceof Error &&
    (error.name === "TimeoutError" || error.name === "AbortError")
  );
}

function retryAfterSeconds(response: Response): number | null {
  const value = response.headers.get("Retry-After");
  if (value === null || !/^\d+$/.test(value.trim())) return null;
  return Number(value.trim());
}

async function errorDetail(response: Response): Promise<unknown> {
  try {
    const body: unknown = await response.json();
    return isObject(body) ? body.detail : undefined;
  } catch (error) {
    if (isAbandoned(error)) throw error;
    return undefined;
  }
}

function checkListQuery(query: ListRunsQuery): URLSearchParams {
  const params = new URLSearchParams();
  if (query.country !== undefined) {
    if (!COUNTRY_PATTERN.test(query.country)) {
      throw new RangeError("A country is two capital letters, such as US.");
    }
    params.set("country", query.country);
  }
  if (query.limit !== undefined) {
    if (
      !Number.isInteger(query.limit) ||
      query.limit < 1 ||
      query.limit > MAX_PAGE_SIZE
    ) {
      throw new RangeError(`A limit is an integer from 1 to ${MAX_PAGE_SIZE}.`);
    }
    params.set("limit", String(query.limit));
  }
  if (query.before !== undefined && query.before !== null) {
    if (!query.before) throw new RangeError("A cursor is never empty.");
    params.set("before", query.before);
  }
  return params;
}

function checkRunSummary(value: unknown, index: number): void {
  const run = requireObject(value, `run ${index}`);
  for (const field of ["run_id", "country_code", "status", "registered_at"]) {
    requireString(run[field], `run ${index} ${field}`);
  }
}

function checkRunPage(value: unknown): RunPage {
  const page = requireObject(value, "run page");
  if (!Array.isArray(page.runs)) {
    throw new CollectorResponseError("Collector run page must list runs.");
  }
  page.runs.forEach(checkRunSummary);
  if (page.next_before !== null) {
    requireString(page.next_before, "pagination cursor");
  }
  return page as unknown as RunPage;
}

function checkRunDetail(value: unknown, runId: string): RunDetail {
  const detail = requireObject(value, "run");
  const run = requireObject(detail.run, "run state");
  if (requireString(run.run_id, "run_id") !== runId) {
    throw new CollectorResponseError("Collector returned a different run.");
  }
  if (!Array.isArray(detail.events)) {
    throw new CollectorResponseError("Collector run must list its events.");
  }
  detail.events.forEach((item: unknown, index: number) => {
    const event = requireObject(item, `event ${index}`);
    requireString(event.event_id, `event ${index} event_id`);
    if (event.run_id !== runId) {
      throw new CollectorResponseError(
        `Collector event ${index} belongs to a different run.`,
      );
    }
  });
  return detail as unknown as RunDetail;
}

export function createCollectorClient(
  options: CollectorClientOptions,
): CollectorClient {
  const baseUrl = collectorBaseUrl(options.baseUrl);
  const timeoutMs = requestTimeout(options.timeoutMs);

  async function readToken(): Promise<string> {
    const token =
      typeof options.readToken === "function"
        ? await options.readToken()
        : options.readToken;
    if (typeof token !== "string" || !token.trim()) {
      throw new Error("The collector read credential is empty.");
    }
    return token;
  }

  // The parsed JSON body of a 200 answer, or `MISSING_RUN` for a run the
  // collector has no record of.
  async function getJson(
    pathAndQuery: string,
    { missingRunAllowed = false } = {},
  ): Promise<unknown> {
    // The global fetch is looked up for each request, so one that a test or
    // framework installs after the client is created is used.
    const fetchFunction = options.fetch ?? globalThis.fetch;
    const response = await fetchFunction(`${baseUrl}${pathAndQuery}`, {
      method: "GET",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        [READ_TOKEN_HEADER]: await readToken(),
      },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (response.ok) {
      try {
        return await response.json();
      } catch (error) {
        if (isAbandoned(error)) throw error;
        throw new CollectorResponseError("Collector answer is not JSON.");
      }
    }
    const detail = await errorDetail(response);
    if (missingRunAllowed && response.status === 404 && detail === RUN_NOT_FOUND) {
      return MISSING_RUN;
    }
    throw new CollectorRequestError(
      response.status,
      detail,
      retryAfterSeconds(response),
    );
  }

  async function listRuns(query: ListRunsQuery = {}): Promise<RunPage> {
    const queryText = checkListQuery(query).toString();
    return checkRunPage(
      await getJson(queryText ? `/v2/runs?${queryText}` : "/v2/runs"),
    );
  }

  async function listRecentRuns(query: RecentRunsQuery): Promise<RunSummary[]> {
    if (!Number.isInteger(query.count) || query.count < 1) {
      throw new RangeError("A count is a positive integer.");
    }
    const runs: RunSummary[] = [];
    const visited = new Set<string>();
    let before: string | null = null;
    do {
      const page: RunPage = await listRuns({
        country: query.country,
        limit: Math.min(query.count - runs.length, MAX_PAGE_SIZE),
        before,
      });
      runs.push(...page.runs);
      before = page.next_before;
      if (before !== null) {
        if (visited.has(before)) {
          throw new CollectorResponseError(
            "Collector pagination repeated a cursor.",
          );
        }
        visited.add(before);
      }
    } while (before !== null && runs.length < query.count);
    return runs.slice(0, query.count);
  }

  async function getRun(runId: string): Promise<RunDetail | null> {
    if (!RUN_ID_PATTERN.test(runId)) {
      throw new RangeError("The run id is not a collector run id.");
    }
    const body = await getJson(`/v2/runs/${encodeURIComponent(runId)}`, {
      missingRunAllowed: true,
    });
    return body === MISSING_RUN ? null : checkRunDetail(body, runId);
  }

  return { listRuns, listRecentRuns, getRun };
}
