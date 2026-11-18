// The collector's copy of a country's releases.json. The collector keeps one
// private JSON object per country in the tree store, replaced in place, at
//
//   calibration-tree-sources/<country>/releases-record.json
//
// beside its builds' original files and apart from calibration-trees/, so
// nothing that lists or reads the trees sees it. It holds the file's exact
// text with the Hugging Face commit it was read at, in one object, so the two
// always change together:
//
//   {
//     "schemaVersion": 1,
//     "country": "us",
//     "hfRepo": "<owner>/<name>",
//     "hfCommitSha": "<the release repo's head when this was copied>",
//     "copiedAt": "<when this was copied>",
//     "record": {"sha256": "...", "bytes": 1699, "text": "<the file's text>"}
//   }
//
// `record` is null when the repo has no releases.json at that commit. The
// collector replaces the object through calibration-tree-releases-record-copy.ts.
import { get } from "@vercel/blob";
import { createHash } from "node:crypto";

import { isSha256 } from "./calibration-tree-artifact";
import type { CalibrationTreeBlobClient } from "./calibration-tree-blob";
import { isCountry, type MicrocosmCountry } from "./countries";
import { parseReleasesRecordBytes, type ReleasesRecord } from "./release-record";

export const STORED_RELEASES_RECORD_SCHEMA_VERSION = 1;

const DOCUMENT_KEYS = ["copiedAt", "country", "hfCommitSha", "hfRepo", "record", "schemaVersion"];
const RECORD_KEYS = ["bytes", "sha256", "text"];
const HF_COMMIT_RE = /^[0-9a-f]{40,64}$/;
const HF_REPO_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._-]*$/;

/** A stored copy that breaks the document's rules. */
export class StoredReleasesRecordError extends Error {}

/** Where the record came from and when it was copied. */
export interface StoredReleasesRecordSource {
  country: MicrocosmCountry;
  hfRepo: string;
  hfCommitSha: string;
  copiedAt: string;
}

/**
 * What the store holds for a country:
 *
 * - `record`: the repo's releases.json at `hfCommitSha`, checked by the rules
 *   of parseReleasesRecordBytes.
 * - `no_record`: the repo had no releases.json at `hfCommitSha`.
 * - `not_copied`: the collector has stored nothing for the country yet, which
 *   says nothing about whether its repo has a releases.json.
 */
export type StoredReleasesRecord =
  | { state: "not_copied" }
  | (StoredReleasesRecordSource & { state: "no_record" })
  | (StoredReleasesRecordSource & { state: "record"; record: ReleasesRecord; sha256: string });

export function isHfCommitSha(value: unknown): value is string {
  return typeof value === "string" && HF_COMMIT_RE.test(value);
}

export function isHfRepo(value: unknown): value is string {
  return typeof value === "string" && HF_REPO_RE.test(value);
}

/** The blob path of a country's copy. */
export function storedReleasesRecordPath(country: MicrocosmCountry): string {
  if (!isCountry(country)) throw new Error(`${String(country)} is not a registered country.`);
  return `calibration-tree-sources/${country}/releases-record.json`;
}

function refuse(message: string): never {
  throw new StoredReleasesRecordError(`The stored releases.json copy ${message}`);
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value);
}

function hasExactly(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const present = Object.keys(value).sort();
  return present.length === keys.length && present.every((key, index) => key === keys[index]);
}

function sha256Of(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

/**
 * The stored document for one copy. `recordBytes` is the repo's releases.json,
 * or null when it has none at that commit. Throws ReleasesRecordError for a
 * record the package's rules refuse, so a refused record is never stored.
 */
export function storedReleasesRecordDocument(options: StoredReleasesRecordSource & {
  recordBytes: Uint8Array | null;
}): string {
  const { country, hfRepo, hfCommitSha, copiedAt, recordBytes } = options;
  let record: { sha256: string; bytes: number; text: string } | null = null;
  if (recordBytes) {
    parseReleasesRecordBytes(recordBytes);
    record = {
      sha256: sha256Of(recordBytes),
      bytes: recordBytes.byteLength,
      // A byte order mark stays in the text, so encoding it gives the same bytes.
      text: new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(recordBytes),
    };
  }
  return JSON.stringify({
    schemaVersion: STORED_RELEASES_RECORD_SCHEMA_VERSION,
    country,
    hfRepo,
    hfCommitSha,
    copiedAt,
    record,
  });
}

/** Checks a stored document's text and returns what it says about `country`. */
export function parseStoredReleasesRecord(
  text: string,
  country: MicrocosmCountry,
): Exclude<StoredReleasesRecord, { state: "not_copied" }> {
  let document: unknown;
  try {
    document = JSON.parse(text);
  } catch {
    refuse("is not a JSON document.");
  }
  if (!isPlainObject(document) || !hasExactly(document, DOCUMENT_KEYS)) {
    refuse(`must have exactly ${DOCUMENT_KEYS.join(", ")}.`);
  }
  if (document.schemaVersion !== STORED_RELEASES_RECORD_SCHEMA_VERSION) {
    refuse(`must have schemaVersion ${STORED_RELEASES_RECORD_SCHEMA_VERSION}.`);
  }
  if (document.country !== country) refuse(`is not the copy of ${country}.`);
  if (!isHfRepo(document.hfRepo)) refuse("names no Hugging Face repo.");
  if (!isHfCommitSha(document.hfCommitSha)) refuse("names no Hugging Face commit.");
  if (typeof document.copiedAt !== "string" || !Number.isFinite(Date.parse(document.copiedAt))) {
    refuse("has no copy time.");
  }
  const source: StoredReleasesRecordSource = {
    country,
    hfRepo: document.hfRepo,
    hfCommitSha: document.hfCommitSha,
    copiedAt: document.copiedAt,
  };
  const stored = document.record;
  if (stored === null) return { state: "no_record", ...source };
  if (
    !isPlainObject(stored) ||
    !hasExactly(stored, RECORD_KEYS) ||
    typeof stored.text !== "string" ||
    typeof stored.sha256 !== "string" ||
    !isSha256(stored.sha256)
  ) {
    refuse(`must have a record with exactly ${RECORD_KEYS.join(", ")}.`);
  }
  const bytes = new TextEncoder().encode(stored.text);
  if (bytes.byteLength !== stored.bytes) refuse("records another byte count.");
  if (sha256Of(bytes) !== stored.sha256) refuse("records another digest.");
  return {
    state: "record",
    ...source,
    record: parseReleasesRecordBytes(bytes),
    sha256: stored.sha256,
  };
}

/**
 * What the store holds for `country`. A copy that breaks the document's rules,
 * or a record the package's rules refuse, throws, as does a failed read, so a
 * caller never mistakes a failure for a country without a record.
 */
export async function readStoredReleasesRecord(options: {
  country: MicrocosmCountry;
  token?: string;
  /** Read past the cache, for a reader that has just written the copy. */
  consistent?: boolean;
  client?: Pick<CalibrationTreeBlobClient, "get">;
}): Promise<StoredReleasesRecord> {
  const path = storedReleasesRecordPath(options.country);
  const result = await (options.client ?? { get }).get(path, {
    access: "private",
    useCache: options.consistent !== true,
    ...(options.token ? { token: options.token } : {}),
  });
  if (!result) return { state: "not_copied" };
  if (result.statusCode !== 200 || !result.stream) {
    throw new Error(`Unexpected status ${result.statusCode} reading ${path}.`);
  }
  return parseStoredReleasesRecord(await new Response(result.stream).text(), options.country);
}
