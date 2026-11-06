// A typed client for version 2 of the Microcosm collector's read API. This is
// its interface only: until the client is written, it sends no request and
// answers every call with an empty result.
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
export type RunStatus = RunSummary["status"];

export const READ_TOKEN_HEADER = "X-Telemetry-Read-Token";
export const MAX_PAGE_SIZE = 200;
export const DEFAULT_TIMEOUT_MS = 30_000;

export interface CollectorClientOptions {
  baseUrl: string;
  readToken: string | (() => string | Promise<string>);
  fetch?: typeof globalThis.fetch;
  timeoutMs?: number;
}

export interface ListRunsQuery {
  country?: string;
  limit?: number;
  before?: string | null;
}

export interface RecentRunsQuery {
  country?: string;
  count: number;
}

export interface CollectorClient {
  listRuns(query?: ListRunsQuery): Promise<RunPage>;
  listRecentRuns(query: RecentRunsQuery): Promise<RunSummary[]>;
  getRun(runId: string): Promise<RunDetail | null>;
}

export class CollectorRequestError extends Error {
  readonly status: number;
  readonly retryable: boolean;
  readonly retryAfterSeconds: number | null;
  readonly detail: unknown;

  constructor(
    status: number,
    detail: unknown,
    retryAfterSeconds: number | null,
  ) {
    super(`Collector request failed (${status}).`);
    this.name = "CollectorRequestError";
    this.status = status;
    this.retryable = false;
    this.retryAfterSeconds = retryAfterSeconds;
    this.detail = detail;
  }
}

export class CollectorResponseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CollectorResponseError";
  }
}

export function createCollectorClient(
  _options: CollectorClientOptions,
): CollectorClient {
  return {
    listRuns: async () => ({ runs: [], next_before: null }),
    listRecentRuns: async () => [],
    getRun: async () => ({ events: [] }) as unknown as RunDetail,
  };
}
