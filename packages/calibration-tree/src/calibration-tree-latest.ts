// Points a country's latest release tree at a release build the manifest
// already records, for the collector's handler of releases.json changes:
// when releases.json makes a published build current later, without a new
// tree, the latest follows it, whether that build is newer or older than the
// current latest. It writes only the manifest, never a part, with a
// compare-and-swap: it replaces the manifest only if its ETag is still the
// one read. The collector runs it as its own Bun process, under its one
// manifest lock:
//
//   bun run src/calibration-tree-latest.ts LATEST.json
//
// with the store's read-write token in CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN
// and LATEST.json holding:
//
//   {"country": "uk", "releaseId": "<release>/<build>"}
//
// On success it prints one JSON line,
//
//   {"country": ..., "releaseId": ..., "buildArtifactId": ..., "moved": true}
//
// with `moved` false when the build already was the latest (nothing is then
// written), and exits 0. The other exit codes:
//
//   1  the request could not be followed: no token, or a malformed request;
//   2  the manifest records no release build of the country with that
//      release ID; nothing changed;
//   3  anything else, including a manifest another writer changed meanwhile,
//      one read without an ETag, and any error from the store; running again
//      may succeed.
import { readFileSync } from "node:fs";
import { get, put } from "@vercel/blob";

import { readCalibrationTreeManifest, type CalibrationTreeBlobClient } from "./calibration-tree-blob";
import {
  CALIBRATION_TREE_MANIFEST_PATH,
  serializeCalibrationTreeManifest,
  withCalibrationTreeManifestEntry,
} from "./calibration-tree-manifest";
import { CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE } from "./calibration-tree-publication";
import { isCountry, type MicrocosmCountry } from "./countries";

export const CALIBRATION_TREE_LATEST_EXIT_REQUEST_ERROR = 1;
export const CALIBRATION_TREE_LATEST_EXIT_NOT_RECORDED = 2;
export const CALIBRATION_TREE_LATEST_EXIT_STORE_ERROR = 3;

// As the publication command writes the manifest.
const MANIFEST_CACHE_SECONDS = 60;
// `<release>/<build>`, each one safe path segment, as releases.json names them.
const SAFE_SEGMENT = "[A-Za-z0-9][A-Za-z0-9._-]{0,127}";
const RELEASE_BUILD_RE = new RegExp(`^${SAFE_SEGMENT}/${SAFE_SEGMENT}$`);

/** The request could not be followed; nothing was read or written. */
export class CalibrationTreeLatestRequestError extends Error {}

/** The manifest records no such release build; nothing changed. */
export class CalibrationTreeNotRecordedError extends Error {}

export interface CalibrationTreeLatestRequest {
  country: MicrocosmCountry;
  /** The release build, as `<release>/<build>`. */
  releaseId: string;
}

export interface CalibrationTreeLatestResult {
  country: MicrocosmCountry;
  releaseId: string;
  buildArtifactId: string;
  /** False when the build already was the latest, and nothing was written. */
  moved: boolean;
}

/** Checks a latest request read from JSON. */
export function parseCalibrationTreeLatestRequest(value: unknown): CalibrationTreeLatestRequest {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new CalibrationTreeLatestRequestError("The request must be a JSON object.");
  }
  const request = value as Record<string, unknown>;
  const unknown = Object.keys(request).filter((field) => !["country", "releaseId"].includes(field));
  if (unknown.length > 0) {
    throw new CalibrationTreeLatestRequestError(`The request has unknown fields: ${unknown.join(", ")}.`);
  }
  const { country, releaseId } = request;
  if (typeof country !== "string" || !isCountry(country)) {
    throw new CalibrationTreeLatestRequestError(`country ${String(country)} is not a registered country.`);
  }
  if (typeof releaseId !== "string" || !RELEASE_BUILD_RE.test(releaseId)) {
    throw new CalibrationTreeLatestRequestError("releaseId must be <release>/<build>.");
  }
  return { country, releaseId };
}

const DEFAULT_CLIENT: CalibrationTreeBlobClient = { get, put };

/**
 * Makes the release build `request` names the country's latest. Throws
 * CalibrationTreeNotRecordedError, before anything is written, when the
 * manifest records no release build of the country with that release ID.
 */
export async function moveCalibrationTreeLatest(options: {
  request: CalibrationTreeLatestRequest;
  token: string;
  client?: CalibrationTreeBlobClient;
}): Promise<CalibrationTreeLatestResult> {
  const { request, token, client = DEFAULT_CLIENT } = options;
  const current = await readCalibrationTreeManifest({ token, consistent: true, client });
  const recorded = current.manifest.countries[request.country];
  const entry = recorded?.builds.find(
    (build) => build.kind === "release" && build.releaseId === request.releaseId,
  );
  if (!recorded || !entry) {
    throw new CalibrationTreeNotRecordedError(
      `The manifest records no ${request.country} release build ${request.releaseId}.`,
    );
  }
  const result = { country: request.country, releaseId: request.releaseId, buildArtifactId: entry.buildArtifactId };
  if (recorded.latestReleaseBuildArtifactId === entry.buildArtifactId) {
    return { ...result, moved: false };
  }
  // The manifest exists, since it records the build; without its ETag the
  // write would carry no condition.
  if (!current.etag) {
    throw new Error("The stored calibration tree manifest was read without an ETag, so it cannot be replaced safely.");
  }
  // The recorded entry itself, so only the latest changes.
  const updated = withCalibrationTreeManifestEntry(current.manifest, request.country, entry, true);
  try {
    await client.put(CALIBRATION_TREE_MANIFEST_PATH, serializeCalibrationTreeManifest(updated), {
      access: "private",
      token,
      addRandomSuffix: false,
      allowOverwrite: true,
      ifMatch: current.etag,
      contentType: "application/json; charset=utf-8",
      cacheControlMaxAge: MANIFEST_CACHE_SECONDS,
    });
  } catch (error) {
    throw new Error(
      "The calibration tree manifest was not written, because another writer changed it or the store refused the write: " +
        (error instanceof Error ? error.message : String(error)),
    );
  }
  const verified = await readCalibrationTreeManifest({ token, consistent: true, client });
  if (verified.manifest.countries[request.country]?.latestReleaseBuildArtifactId !== entry.buildArtifactId) {
    throw new Error("Calibration tree manifest verification failed.");
  }
  return { ...result, moved: true };
}

/** The exit code for an error that stopped the command. */
export function calibrationTreeLatestExitCode(error: unknown): number {
  if (error instanceof CalibrationTreeLatestRequestError) return CALIBRATION_TREE_LATEST_EXIT_REQUEST_ERROR;
  if (error instanceof CalibrationTreeNotRecordedError) return CALIBRATION_TREE_LATEST_EXIT_NOT_RECORDED;
  return CALIBRATION_TREE_LATEST_EXIT_STORE_ERROR;
}

/** The command line: returns the process exit code. */
export async function calibrationTreeLatestMain(
  argv: string[],
  environment: Record<string, string | undefined> = process.env,
  client?: CalibrationTreeBlobClient,
): Promise<number> {
  try {
    if (argv.length !== 1) {
      throw new CalibrationTreeLatestRequestError("Usage: bun run src/calibration-tree-latest.ts LATEST.json");
    }
    const token = environment[CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE]?.trim();
    if (!token) {
      throw new CalibrationTreeLatestRequestError(`${CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE} is required.`);
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(readFileSync(argv[0], "utf8"));
    } catch (error) {
      throw new CalibrationTreeLatestRequestError(
        `The request ${argv[0]} could not be read as JSON: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    const result = await moveCalibrationTreeLatest({
      request: parseCalibrationTreeLatestRequest(parsed),
      token,
      client,
    });
    console.log(JSON.stringify(result));
    return 0;
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    return calibrationTreeLatestExitCode(error);
  }
}

if (import.meta.main) {
  process.exitCode = await calibrationTreeLatestMain(Bun.argv.slice(2));
}
