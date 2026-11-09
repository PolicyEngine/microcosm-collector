// Publishes a tree the calibration tree worker wrote to disk: uploads each of
// its gzip parts to the part's immutable path, then adds the build's entry to
// the manifest with a compare-and-swap. The collector runs it as its own Bun
// process, after the worker and under its one manifest lock:
//
//   bun run src/calibration-tree-publication.ts PUBLICATION.json
//
// with the store's read-write token in CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN
// and PUBLICATION.json holding:
//
//   {
//     "bundleDirectory": "<the worker's output directory>",
//     "worker": <the worker's printed result>,
//     "makeLatest": false
//   }
//
// Each part is read from disk and checked against the worker's record of its
// raw bytes before anything is uploaded. A part already stored with the same
// bytes is kept; one stored with other bytes stops the publication. The index
// goes last. The manifest is created only if it is still absent, and an
// existing one is replaced only if its ETag is still the one read, so a
// manifest another writer changed is never overwritten. A build asked to
// become the country's latest does so only when it is at least as new
// (createdAt) as the current latest, so a retried or reprocessed older build
// never takes the latest back. On success it prints one JSON line and exits
// 0. The other exit codes:
//
//   1  the request could not be followed: no token, a malformed request, or a
//      part on disk that differs from the worker's record;
//   2  the build is refused: another build is already recorded for the same
//      release or staged run, or the same build with other content (another
//      submission of the same files, whose index records another time or
//      label), or a staging build was to become the latest release; running
//      again on the same tree cannot help;
//   3  anything else, including a manifest another writer changed meanwhile
//      and any error from the store; running again may succeed.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { gunzipSync } from "node:zlib";
import { get, put } from "@vercel/blob";

import { calibrationTreePartPath, type CalibrationTreePart } from "./calibration-tree-artifact";
import {
  calibrationTreeBlobText,
  getCalibrationTreeBlob,
  readCalibrationTreeManifest,
  type CalibrationTreeBlobClient,
} from "./calibration-tree-blob";
import {
  CALIBRATION_TREE_MANIFEST_PATH,
  CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION,
  parseCalibrationTreeManifest,
  serializeCalibrationTreeManifest,
  withCalibrationTreeManifestEntry,
  type CalibrationTreeManifest,
  type CalibrationTreeManifestEntry,
} from "./calibration-tree-manifest";
import { isCountry, type MicrocosmCountry } from "./countries";

export const CALIBRATION_TREE_PUBLICATION_EXIT_REQUEST_ERROR = 1;
export const CALIBRATION_TREE_PUBLICATION_EXIT_ENTRY_REFUSED = 2;
export const CALIBRATION_TREE_PUBLICATION_EXIT_PUBLICATION_ERROR = 3;
export const CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE = "CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN";

// As the package's own uploads write them.
const IMMUTABLE_CACHE_SECONDS = 31_536_000;
const MANIFEST_CACHE_SECONDS = 60;
const SHA256_RE = /^[0-9a-f]{64}$/;

/** The request could not be followed; nothing was published. */
export class CalibrationTreePublicationRequestError extends Error {}

/** The build is refused; nothing in the manifest changed. */
export class CalibrationTreeEntryRefusedError extends Error {}

/** Another writer changed the manifest after it was read; nothing was overwritten. */
export class CalibrationTreeManifestConflictError extends Error {}

export interface CalibrationTreePublicationPart {
  part: CalibrationTreePart;
  path: string;
  sha256: string;
  rawBytes: number;
}

export interface CalibrationTreePublicationRequest {
  bundleDirectory: string;
  country: MicrocosmCountry;
  buildArtifactId: string;
  files: CalibrationTreePublicationPart[];
  manifestEntry: CalibrationTreeManifestEntry;
  makeLatest: boolean;
}

export interface CalibrationTreePublicationResult {
  buildArtifactId: string;
  kind: CalibrationTreeManifestEntry["kind"];
  uploadedParts: number;
  keptParts: number;
  madeLatest: boolean;
}

type JsonObject = Record<string, unknown>;

function requestObject(value: unknown, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new CalibrationTreePublicationRequestError(`${label} must be a JSON object.`);
  }
  return value as JsonObject;
}

function requestString(value: JsonObject, field: string, label: string): string {
  const text = value[field];
  if (typeof text !== "string" || text.length === 0) {
    throw new CalibrationTreePublicationRequestError(`${label} must be a non-empty string.`);
  }
  return text;
}

function publicationPart(value: unknown, country: MicrocosmCountry, buildArtifactId: string): CalibrationTreePublicationPart {
  const file = requestObject(value, "worker.files[]");
  const part = requestString(file, "part", "worker.files[].part") as CalibrationTreePart;
  const path = requestString(file, "path", "worker.files[].path");
  const sha256 = requestString(file, "sha256", "worker.files[].sha256");
  const rawBytes = file.rawBytes;
  let expectedPath: string;
  try {
    expectedPath = calibrationTreePartPath(country, buildArtifactId, part);
  } catch {
    throw new CalibrationTreePublicationRequestError(`worker.files[] names an unknown part ${part}.`);
  }
  // A part is only ever written to its own immutable path.
  if (path !== expectedPath) {
    throw new CalibrationTreePublicationRequestError(`Part ${part} must be stored at ${expectedPath}.`);
  }
  if (!SHA256_RE.test(sha256) || !Number.isSafeInteger(rawBytes) || (rawBytes as number) < 0) {
    throw new CalibrationTreePublicationRequestError(`Part ${part} needs a SHA-256 and a raw size.`);
  }
  return { part, path, sha256, rawBytes: rawBytes as number };
}

/** Checks a publication request read from JSON. */
export function parseCalibrationTreePublicationRequest(value: unknown): CalibrationTreePublicationRequest {
  const request = requestObject(value, "The request");
  const unknown = Object.keys(request).filter((field) => !["bundleDirectory", "worker", "makeLatest"].includes(field));
  if (unknown.length > 0) {
    throw new CalibrationTreePublicationRequestError(`The request has unknown fields: ${unknown.join(", ")}.`);
  }
  if (typeof request.makeLatest !== "boolean") {
    throw new CalibrationTreePublicationRequestError("makeLatest must be true or false.");
  }
  const bundleDirectory = requestString(request, "bundleDirectory", "bundleDirectory");
  const worker = requestObject(request.worker, "worker");
  const country = requestString(worker, "country", "worker.country");
  if (!isCountry(country)) {
    throw new CalibrationTreePublicationRequestError(`worker.country ${country} is not a registered country.`);
  }
  const buildArtifactId = requestString(worker, "buildArtifactId", "worker.buildArtifactId");
  if (!SHA256_RE.test(buildArtifactId)) {
    throw new CalibrationTreePublicationRequestError("worker.buildArtifactId must be a build ID.");
  }
  if (!Array.isArray(worker.files) || worker.files.length === 0) {
    throw new CalibrationTreePublicationRequestError("worker.files must list the tree's parts.");
  }
  const files = worker.files.map((file) => publicationPart(file, country, buildArtifactId));
  if (files.filter((file) => file.part === "index").length !== 1) {
    throw new CalibrationTreePublicationRequestError("worker.files must hold exactly one index.");
  }
  const manifestEntry = requestObject(worker.manifestEntry, "worker.manifestEntry") as unknown as CalibrationTreeManifestEntry;
  // The manifest's own rules, checked before anything is uploaded: a
  // malformed entry is a defect in the request, not a refusal of the build.
  try {
    parseCalibrationTreeManifest({
      schemaVersion: CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION,
      countries: { [country]: { latestReleaseBuildArtifactId: null, builds: [manifestEntry] } },
    });
  } catch (error) {
    throw new CalibrationTreePublicationRequestError(
      `worker.manifestEntry is not a manifest entry: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  const index = files.find((file) => file.part === "index")!;
  if (
    manifestEntry.buildArtifactId !== buildArtifactId ||
    manifestEntry.indexSha256 !== index.sha256 ||
    manifestEntry.indexBytes !== index.rawBytes
  ) {
    throw new CalibrationTreePublicationRequestError("worker.manifestEntry does not describe the tree's index.");
  }
  return { bundleDirectory, country, buildArtifactId, files, manifestEntry, makeLatest: request.makeLatest };
}

interface PartOnDisk extends CalibrationTreePublicationPart {
  compressed: Uint8Array;
  serialized: string;
}

// Every part is read and checked before anything is uploaded, so a damaged
// output directory publishes nothing.
function partsOnDisk(request: CalibrationTreePublicationRequest): PartOnDisk[] {
  return request.files.map((file) => {
    let compressed: Uint8Array;
    let raw: Uint8Array;
    try {
      compressed = new Uint8Array(readFileSync(join(request.bundleDirectory, file.path)));
      raw = new Uint8Array(gunzipSync(compressed));
    } catch (error) {
      throw new CalibrationTreePublicationRequestError(
        `Part ${file.part} could not be read from ${request.bundleDirectory}: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    if (raw.byteLength !== file.rawBytes || createHash("sha256").update(raw).digest("hex") !== file.sha256) {
      throw new CalibrationTreePublicationRequestError(`Part ${file.part} on disk differs from the worker's record.`);
    }
    return { ...file, compressed, serialized: new TextDecoder("utf-8").decode(raw) };
  });
}

// As the package's uploadCalibrationTreeFile: an existing part must hold the
// same raw JSON, and an uploaded one is read back.
async function uploadPart(
  part: PartOnDisk,
  request: CalibrationTreePublicationRequest,
  token: string,
  client: CalibrationTreeBlobClient,
): Promise<boolean> {
  const stored = () =>
    getCalibrationTreeBlob({
      country: request.country,
      buildArtifactId: request.buildArtifactId,
      part: part.part,
      token,
      consistent: true,
      client,
    });
  const existing = await stored();
  if (existing) {
    if (await calibrationTreeBlobText(existing.stream) !== part.serialized) {
      // The build ID covers the files, not the time or label the index
      // records, so another submission of the same files lands here.
      throw new CalibrationTreeEntryRefusedError(
        `Immutable calibration tree part ${part.path} already exists with different content: ` +
          "this build is already recorded from another submission.",
      );
    }
    return false;
  }
  await client.put(part.path, Buffer.from(part.compressed), {
    access: "private",
    token,
    addRandomSuffix: false,
    multipart: true,
    contentType: "application/gzip",
    cacheControlMaxAge: IMMUTABLE_CACHE_SECONDS,
  });
  const uploaded = await stored();
  if (!uploaded || await calibrationTreeBlobText(uploaded.stream) !== part.serialized) {
    throw new Error(`Uploaded calibration tree part ${part.path} failed verification.`);
  }
  return true;
}

// When a build was published: createdAt, else updatedAt; an undated build is
// older than any dated one.
function publishedInstant(entry: CalibrationTreeManifestEntry): number {
  const instant = Date.parse(entry.createdAt ?? entry.updatedAt ?? "");
  return Number.isFinite(instant) ? instant : Number.NEGATIVE_INFINITY;
}

// Whether the build is at least as new as the country's current latest.
function isAtLeastAsNewAsLatest(
  manifest: CalibrationTreeManifest,
  country: MicrocosmCountry,
  entry: CalibrationTreeManifestEntry,
): boolean {
  const current = manifest.countries[country];
  const latest = current?.builds.find((build) => build.buildArtifactId === current.latestReleaseBuildArtifactId);
  return !latest || latest.buildArtifactId === entry.buildArtifactId ||
    publishedInstant(entry) >= publishedInstant(latest);
}

async function addManifestEntry(
  request: CalibrationTreePublicationRequest,
  token: string,
  client: CalibrationTreeBlobClient,
): Promise<boolean> {
  const current = await readCalibrationTreeManifest({ token, consistent: true, client });
  // An existing manifest is replaced only on its ETag; without one, the write
  // would carry no condition.
  if (current.etag !== null && !current.etag) {
    throw new CalibrationTreeManifestConflictError(
      "The stored calibration tree manifest was read without an ETag, so it cannot be replaced safely.",
    );
  }
  const makeLatest = request.makeLatest &&
    (request.manifestEntry.kind !== "release" ||
      isAtLeastAsNewAsLatest(current.manifest, request.country, request.manifestEntry));
  let updated;
  try {
    updated = withCalibrationTreeManifestEntry(current.manifest, request.country, request.manifestEntry, makeLatest);
  } catch (error) {
    throw new CalibrationTreeEntryRefusedError(error instanceof Error ? error.message : String(error));
  }
  try {
    await client.put(CALIBRATION_TREE_MANIFEST_PATH, serializeCalibrationTreeManifest(updated), {
      access: "private",
      token,
      addRandomSuffix: false,
      // Create only while absent; otherwise replace only the version read.
      allowOverwrite: current.etag != null,
      ifMatch: current.etag ?? undefined,
      contentType: "application/json; charset=utf-8",
      cacheControlMaxAge: MANIFEST_CACHE_SECONDS,
    });
  } catch (error) {
    throw new CalibrationTreeManifestConflictError(
      `The calibration tree manifest was not written, because another writer changed it or the store refused the write: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  const verified = await readCalibrationTreeManifest({ token, consistent: true, client });
  const recorded = verified.manifest.countries[request.country];
  const entry = recorded?.builds.find((build) => build.buildArtifactId === request.buildArtifactId);
  if (
    !entry ||
    entry.indexSha256 !== request.manifestEntry.indexSha256 ||
    (makeLatest && recorded?.latestReleaseBuildArtifactId !== request.buildArtifactId)
  ) {
    throw new Error("Calibration tree manifest verification failed.");
  }
  return makeLatest;
}

const DEFAULT_CLIENT: CalibrationTreeBlobClient = { get, put };

/** Uploads a built tree and records it in the manifest. */
export async function publishBuiltCalibrationTree(options: {
  request: CalibrationTreePublicationRequest;
  token: string;
  client?: CalibrationTreeBlobClient;
}): Promise<CalibrationTreePublicationResult> {
  const { request, token, client = DEFAULT_CLIENT } = options;
  const parts = partsOnDisk(request);
  const ordered = [...parts.filter((part) => part.part !== "index"), ...parts.filter((part) => part.part === "index")];
  let uploadedParts = 0;
  for (const part of ordered) {
    if (await uploadPart(part, request, token, client)) uploadedParts += 1;
  }
  const madeLatest = await addManifestEntry(request, token, client);
  return {
    buildArtifactId: request.buildArtifactId,
    kind: request.manifestEntry.kind,
    uploadedParts,
    keptParts: parts.length - uploadedParts,
    madeLatest,
  };
}

/** The exit code for an error that stopped the publication. */
export function calibrationTreePublicationExitCode(error: unknown): number {
  if (error instanceof CalibrationTreePublicationRequestError) return CALIBRATION_TREE_PUBLICATION_EXIT_REQUEST_ERROR;
  if (error instanceof CalibrationTreeEntryRefusedError) return CALIBRATION_TREE_PUBLICATION_EXIT_ENTRY_REFUSED;
  return CALIBRATION_TREE_PUBLICATION_EXIT_PUBLICATION_ERROR;
}

/** The command line: returns the process exit code. */
export async function calibrationTreePublicationMain(
  argv: string[],
  environment: Record<string, string | undefined> = process.env,
): Promise<number> {
  try {
    if (argv.length !== 1) {
      throw new CalibrationTreePublicationRequestError("Usage: bun run src/calibration-tree-publication.ts PUBLICATION.json");
    }
    const token = environment[CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE]?.trim();
    if (!token) {
      throw new CalibrationTreePublicationRequestError(`${CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE} is required.`);
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(readFileSync(argv[0], "utf8"));
    } catch (error) {
      throw new CalibrationTreePublicationRequestError(
        `The request ${argv[0]} could not be read as JSON: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    const result = await publishBuiltCalibrationTree({ request: parseCalibrationTreePublicationRequest(parsed), token });
    console.log(JSON.stringify(result));
    return 0;
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    return calibrationTreePublicationExitCode(error);
  }
}

if (import.meta.main) {
  process.exitCode = await calibrationTreePublicationMain(Bun.argv.slice(2));
}
