// The stored copies of a build's original files. When the collector builds a
// calibration tree, its worker writes a gzip copy of each file the build came
// with, and its publication command uploads them, before the tree's parts, to
// private blobs at calibration-tree-sources/<country>/<build ID>/<name>.gz. The
// folder is apart from calibration-trees/, so nothing that lists or reads the
// trees sees the copies. A copy exists only for a file some attempt to publish
// the build was given.
import { get } from "@vercel/blob";
import { createHash } from "node:crypto";
import { gunzipSync } from "node:zlib";

import { isSha256 } from "./calibration-tree-artifact";
import type { CalibrationTreeBlobClient } from "./calibration-tree-blob";
import type { MicrocosmCountry } from "./countries";
import { parseHashedJsonArtifact } from "./latest-artifact";

/** The name each original file is stored under, whatever the build called it. */
export const CALIBRATION_TREE_SOURCE_FILE_NAMES = {
  calibrationDiagnostics: "calibration_diagnostics.json",
  buildManifest: "build_manifest.json",
  releaseManifest: "release_manifest.json",
  demographics: "demographics.json",
  parentBuildManifest: "parent_build_manifest.json",
  reformValidation: "reform_validation.json",
} as const;

export type CalibrationTreeSourceFile = keyof typeof CALIBRATION_TREE_SOURCE_FILE_NAMES;

export const CALIBRATION_TREE_SOURCE_FILES = Object.keys(
  CALIBRATION_TREE_SOURCE_FILE_NAMES,
) as CalibrationTreeSourceFile[];

export function isCalibrationTreeSourceFile(value: unknown): value is CalibrationTreeSourceFile {
  return typeof value === "string" && Object.hasOwn(CALIBRATION_TREE_SOURCE_FILE_NAMES, value);
}

/** The blob path of one build's copy of one original file. */
export function calibrationTreeSourceFilePath(
  country: MicrocosmCountry,
  buildArtifactId: string,
  file: CalibrationTreeSourceFile,
): string {
  if (!isSha256(buildArtifactId)) throw new Error("Invalid calibration tree build ID.");
  if (!isCalibrationTreeSourceFile(file)) throw new Error("Invalid calibration tree source file.");
  return `calibration-tree-sources/${country}/${buildArtifactId}/${CALIBRATION_TREE_SOURCE_FILE_NAMES[file]}.gz`;
}

export interface CalibrationTreeSourceFileRequest {
  country: MicrocosmCountry;
  buildArtifactId: string;
  file: CalibrationTreeSourceFile;
  token?: string;
  /** Read past the cache, for a reader that has just written the file. */
  consistent?: boolean;
  client?: Pick<CalibrationTreeBlobClient, "get">;
}

export interface StoredCalibrationTreeSourceFile {
  /** The original file's bytes. */
  bytes: Uint8Array;
  /** The SHA-256 of those bytes. */
  sha256: string;
}

/** A build's original file as it was given to the worker; null without a copy. */
export async function readCalibrationTreeSourceFile(
  options: CalibrationTreeSourceFileRequest,
): Promise<StoredCalibrationTreeSourceFile | null> {
  const path = calibrationTreeSourceFilePath(options.country, options.buildArtifactId, options.file);
  const result = await (options.client ?? { get }).get(path, {
    access: "private",
    useCache: options.consistent !== true,
    ...(options.token ? { token: options.token } : {}),
  });
  if (!result) return null;
  if (result.statusCode !== 200 || !result.stream) {
    throw new Error(`Unexpected status ${result.statusCode} reading ${path}.`);
  }
  const bytes = new Uint8Array(gunzipSync(await new Response(result.stream).arrayBuffer()));
  return { bytes, sha256: createHash("sha256").update(bytes).digest("hex") };
}

/**
 * A build's original JSON file, with the payload and digest the package's
 * Hugging Face reader gives for the same file; null without a copy.
 */
export async function readCalibrationTreeSourceJson(
  options: CalibrationTreeSourceFileRequest,
): Promise<ReturnType<typeof parseHashedJsonArtifact> | null> {
  const stored = await readCalibrationTreeSourceFile(options);
  return stored && parseHashedJsonArtifact(stored.bytes);
}
