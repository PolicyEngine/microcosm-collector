// Adds copies of original files to builds the store already holds. The
// collector's backfill runs it as its own Bun process, with the store's token in
// CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN:
//
//   bun run src/calibration-tree-source-copies.ts list <country>
//   bun run src/calibration-tree-source-copies.ts store REQUEST.json
//
// `list` only reads. It prints the copies stored for each of the country's
// builds, by the name each is stored under:
//
//   {"builds": {"<build ID>": ["calibration_diagnostics.json", ...]}}
//
// `store` takes REQUEST.json:
//
//   {
//     "country": "us",
//     "buildArtifactId": "<build ID>",
//     "files": [{"file": "reformValidation", "path": "<the file on disk>", "sha256": "<its SHA-256>"}]
//   }
//
// and stores a gzip copy of each file for a build whose tree index the store
// holds, at calibration-tree-sources/<country>/<build ID>/<name>.gz. It writes
// nothing else: no part, index or manifest. One of the four files a tree is
// built from (calibrationDiagnostics, buildManifest, releaseManifest,
// demographics) is stored only when the stored index records it with the same
// SHA-256, so a copy is always a file the tree was built from; the other two
// (parentBuildManifest, reformValidation) have no record and are stored as
// given. A copy already stored stays as it is, whatever it holds. It prints:
//
//   {"uploaded": [...], "kept": [...], "conflicts": [...], "refused": [{"file": ..., "reason": ...}]}
//
// where "kept" are copies already stored with the same bytes, "conflicts"
// copies already stored with other bytes, and "refused" the tree files the index
// does not record with the same SHA-256. Every file is read from disk and
// checked against its SHA-256 before anything is stored. Each prints one JSON
// line and exits 0. The other exit codes:
//
//   1  the request could not be followed: no token, unknown arguments, a
//      malformed request, or a file on disk that differs from its SHA-256;
//   2  the store holds no tree index for the build, so nothing was stored;
//   3  anything else, including any error from the store; running again may
//      succeed.
import { get, list, put } from "@vercel/blob";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";

import { isSha256 } from "./calibration-tree-artifact";
import {
  calibrationTreeBlobText,
  getCalibrationTreeBlob,
  type CalibrationTreeBlobClient,
} from "./calibration-tree-blob";
import { CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE } from "./calibration-tree-publication";
import {
  CALIBRATION_TREE_SOURCE_FILE_NAMES,
  isCalibrationTreeSourceFile,
  storeCalibrationTreeSourceFile,
  type CalibrationTreeSourceFile,
} from "./calibration-tree-source-files";
import { isCountry, type MicrocosmCountry } from "./countries";

export const CALIBRATION_TREE_SOURCE_COPIES_EXIT_REQUEST_ERROR = 1;
export const CALIBRATION_TREE_SOURCE_COPIES_EXIT_REFUSED = 2;
export const CALIBRATION_TREE_SOURCE_COPIES_EXIT_STORE_ERROR = 3;

// As the worker compresses the copies it writes.
const SOURCE_FILE_GZIP_LEVEL = 6;
const LIST_PAGE_SIZE = 1_000;
const BUILD_ID_SEGMENT = /^[0-9a-f]{64}$/;

/** The files a tree is built from, which its index records with their SHA-256. */
export const CALIBRATION_TREE_INDEXED_SOURCE_FILES: ReadonlySet<CalibrationTreeSourceFile> = new Set([
  "calibrationDiagnostics",
  "buildManifest",
  "releaseManifest",
  "demographics",
]);

const STORED_NAMES: ReadonlySet<string> = new Set(Object.values(CALIBRATION_TREE_SOURCE_FILE_NAMES));

/** The request could not be followed; nothing was stored. */
export class CalibrationTreeSourceCopiesRequestError extends Error {}

/** The store holds no tree for the build; nothing was stored. */
export class CalibrationTreeSourceCopiesRefusedError extends Error {}

export interface CalibrationTreeSourceCopyFile {
  file: CalibrationTreeSourceFile;
  /** The original file on disk. */
  path: string;
  sha256: string;
}

export interface CalibrationTreeSourceCopiesRequest {
  country: MicrocosmCountry;
  buildArtifactId: string;
  files: CalibrationTreeSourceCopyFile[];
}

export interface CalibrationTreeSourceCopiesResult {
  uploaded: CalibrationTreeSourceFile[];
  kept: CalibrationTreeSourceFile[];
  conflicts: CalibrationTreeSourceFile[];
  refused: Array<{ file: CalibrationTreeSourceFile; reason: string }>;
}

type JsonObject = Record<string, unknown>;

function requestObject(value: unknown, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new CalibrationTreeSourceCopiesRequestError(`${label} must be a JSON object.`);
  }
  return value as JsonObject;
}

function countryArgument(value: unknown): MicrocosmCountry {
  if (typeof value !== "string" || !isCountry(value)) {
    throw new CalibrationTreeSourceCopiesRequestError(`${String(value)} is not a registered country.`);
  }
  return value;
}

/** Checks a store request read from JSON. */
export function parseCalibrationTreeSourceCopiesRequest(value: unknown): CalibrationTreeSourceCopiesRequest {
  const request = requestObject(value, "The request");
  const unknown = Object.keys(request).filter((field) => !["country", "buildArtifactId", "files"].includes(field));
  if (unknown.length > 0) {
    throw new CalibrationTreeSourceCopiesRequestError(`The request has unknown fields: ${unknown.join(", ")}.`);
  }
  const country = countryArgument(request.country);
  const buildArtifactId = request.buildArtifactId;
  if (typeof buildArtifactId !== "string" || !isSha256(buildArtifactId)) {
    throw new CalibrationTreeSourceCopiesRequestError("buildArtifactId must be a build ID.");
  }
  if (!Array.isArray(request.files) || request.files.length === 0) {
    throw new CalibrationTreeSourceCopiesRequestError("files must list the files to store.");
  }
  const files = request.files.map((listed): CalibrationTreeSourceCopyFile => {
    const entry = requestObject(listed, "files[]");
    if (!isCalibrationTreeSourceFile(entry.file)) {
      throw new CalibrationTreeSourceCopiesRequestError(`files[] names an unknown file ${String(entry.file)}.`);
    }
    if (typeof entry.path !== "string" || entry.path.length === 0) {
      throw new CalibrationTreeSourceCopiesRequestError(`File ${entry.file} needs a path.`);
    }
    if (typeof entry.sha256 !== "string" || !isSha256(entry.sha256)) {
      throw new CalibrationTreeSourceCopiesRequestError(`File ${entry.file} needs a SHA-256.`);
    }
    return { file: entry.file, path: entry.path, sha256: entry.sha256 };
  });
  const named = files.map((file) => file.file);
  if (new Set(named).size !== named.length) {
    throw new CalibrationTreeSourceCopiesRequestError("files must name each file at most once.");
  }
  return { country, buildArtifactId, files };
}

const DEFAULT_CLIENT: CalibrationTreeBlobClient = { get, put, list };

/** The copies stored for each of the country's builds, by the names they are stored under. */
export async function listCalibrationTreeSourceCopies(options: {
  country: MicrocosmCountry;
  token: string;
  client?: CalibrationTreeBlobClient;
}): Promise<Record<string, string[]>> {
  const client = options.client ?? DEFAULT_CLIENT;
  if (!client.list) throw new Error("Calibration tree Blob listing is unavailable.");
  const prefix = `calibration-tree-sources/${options.country}/`;
  const builds = new Map<string, Set<string>>();
  let cursor: string | undefined;
  do {
    const page = await client.list({
      prefix,
      limit: LIST_PAGE_SIZE,
      token: options.token,
      ...(cursor ? { cursor } : {}),
    });
    for (const blob of page.blobs) {
      const [buildArtifactId, storedName, ...rest] = blob.pathname.slice(prefix.length).split("/");
      if (
        !blob.pathname.startsWith(prefix) ||
        rest.length > 0 ||
        !BUILD_ID_SEGMENT.test(buildArtifactId ?? "") ||
        !storedName?.endsWith(".gz") ||
        !STORED_NAMES.has(storedName.slice(0, -".gz".length))
      ) {
        continue;
      }
      const names = builds.get(buildArtifactId) ?? new Set<string>();
      names.add(storedName.slice(0, -".gz".length));
      builds.set(buildArtifactId, names);
    }
    cursor = page.hasMore ? page.cursor : undefined;
    if (page.hasMore && !cursor) throw new Error("Calibration tree Blob listing omitted its next cursor.");
  } while (cursor);
  return Object.fromEntries(
    [...builds.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([buildArtifactId, names]) => [
      buildArtifactId,
      [...names].sort(),
    ]),
  );
}

// The SHA-256 the stored index records for each file the tree was built from.
async function recordedSourceDigests(
  request: CalibrationTreeSourceCopiesRequest,
  token: string,
  client: CalibrationTreeBlobClient,
): Promise<Map<string, string>> {
  const stored = await getCalibrationTreeBlob({
    country: request.country,
    buildArtifactId: request.buildArtifactId,
    part: "index",
    token,
    consistent: true,
    client,
  });
  if (!stored) {
    throw new CalibrationTreeSourceCopiesRefusedError(
      `The store holds no tree for build ${request.buildArtifactId}, so no copy of its files is stored.`,
    );
  }
  const index = JSON.parse(await calibrationTreeBlobText(stored.stream)) as {
    buildArtifactId?: unknown;
    build?: { sourceArtifacts?: unknown };
  };
  if (index.buildArtifactId !== undefined && index.buildArtifactId !== request.buildArtifactId) {
    throw new Error(`The index stored for build ${request.buildArtifactId} names another build.`);
  }
  const sources = index.build?.sourceArtifacts;
  if (!sources || typeof sources !== "object" || Array.isArray(sources)) {
    throw new Error(`Tree ${request.buildArtifactId} records no source files.`);
  }
  const digests = new Map<string, string>();
  for (const [file, record] of Object.entries(sources as JsonObject)) {
    const sha256 = record && typeof record === "object" ? (record as JsonObject).sha256 : undefined;
    if (typeof sha256 === "string" && isSha256(sha256)) digests.set(file, sha256);
  }
  return digests;
}

interface FileOnDisk extends CalibrationTreeSourceCopyFile {
  bytes: Uint8Array;
}

// Every file is read and checked before anything is stored, so a damaged
// download stores nothing.
function filesOnDisk(request: CalibrationTreeSourceCopiesRequest): FileOnDisk[] {
  return request.files.map((file) => {
    let bytes: Uint8Array;
    try {
      bytes = new Uint8Array(readFileSync(file.path));
    } catch (error) {
      throw new CalibrationTreeSourceCopiesRequestError(
        `File ${file.file} could not be read from ${file.path}: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    if (createHash("sha256").update(bytes).digest("hex") !== file.sha256) {
      throw new CalibrationTreeSourceCopiesRequestError(`File ${file.file} on disk differs from its SHA-256.`);
    }
    return { ...file, bytes };
  });
}

/** Stores the copies a build lacks, of the files the request names. */
export async function storeCalibrationTreeSourceCopies(options: {
  request: CalibrationTreeSourceCopiesRequest;
  token: string;
  client?: CalibrationTreeBlobClient;
}): Promise<CalibrationTreeSourceCopiesResult> {
  const { request, token, client = DEFAULT_CLIENT } = options;
  const files = filesOnDisk(request);
  const recorded = await recordedSourceDigests(request, token, client);
  const result: CalibrationTreeSourceCopiesResult = { uploaded: [], kept: [], conflicts: [], refused: [] };
  for (const file of files) {
    if (CALIBRATION_TREE_INDEXED_SOURCE_FILES.has(file.file)) {
      const digest = recorded.get(file.file);
      if (digest !== file.sha256) {
        result.refused.push({
          file: file.file,
          reason: digest === undefined
            ? "The stored tree was not built from this file."
            : "The stored tree was built from another version of this file.",
        });
        continue;
      }
    }
    const stored = await storeCalibrationTreeSourceFile({
      country: request.country,
      buildArtifactId: request.buildArtifactId,
      file: file.file,
      sha256: file.sha256,
      compressed: new Uint8Array(gzipSync(file.bytes, { level: SOURCE_FILE_GZIP_LEVEL })),
      token,
      client,
    });
    if (stored === "uploaded") result.uploaded.push(file.file);
    else if (stored === "kept") result.kept.push(file.file);
    else result.conflicts.push(file.file);
  }
  return result;
}

const USAGE = "Usage: bun run src/calibration-tree-source-copies.ts list <country> | store REQUEST.json";

function requestFromFile(path: string): CalibrationTreeSourceCopiesRequest {
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new CalibrationTreeSourceCopiesRequestError(
      `The request ${path} could not be read as JSON: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  return parseCalibrationTreeSourceCopiesRequest(parsed);
}

async function commandAnswer(argv: string[], token: string, client: CalibrationTreeBlobClient): Promise<unknown> {
  const [action, ...operands] = argv;
  if (action === "list" && operands.length === 1) {
    return { builds: await listCalibrationTreeSourceCopies({ country: countryArgument(operands[0]), token, client }) };
  }
  if (action === "store" && operands.length === 1) {
    return storeCalibrationTreeSourceCopies({ request: requestFromFile(operands[0]), token, client });
  }
  throw new CalibrationTreeSourceCopiesRequestError(USAGE);
}

/** The command line: returns the process exit code. */
export async function calibrationTreeSourceCopiesMain(
  argv: string[],
  environment: Record<string, string | undefined> = process.env,
  client: CalibrationTreeBlobClient = DEFAULT_CLIENT,
): Promise<number> {
  try {
    const token = environment[CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE]?.trim();
    if (!token) {
      throw new CalibrationTreeSourceCopiesRequestError(`${CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE} is required.`);
    }
    console.log(JSON.stringify(await commandAnswer(argv, token, client)));
    return 0;
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    if (error instanceof CalibrationTreeSourceCopiesRequestError) return CALIBRATION_TREE_SOURCE_COPIES_EXIT_REQUEST_ERROR;
    if (error instanceof CalibrationTreeSourceCopiesRefusedError) return CALIBRATION_TREE_SOURCE_COPIES_EXIT_REFUSED;
    return CALIBRATION_TREE_SOURCE_COPIES_EXIT_STORE_ERROR;
  }
}

if (import.meta.main) {
  process.exitCode = await calibrationTreeSourceCopiesMain(Bun.argv.slice(2));
}
