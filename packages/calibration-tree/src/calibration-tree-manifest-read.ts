// Reads the calibration tree manifest from the store and prints it, for the
// collector's backfill, which compares it with production's manifest. It
// only reads: it never writes the manifest or any part. The collector runs it
// as its own Bun process:
//
//   bun run src/calibration-tree-manifest-read.ts
//
// with the store's token in CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN, the
// variable the publication command reads. It reads the manifest without the
// cache, checks it with the manifest's own rules (parseCalibrationTreeManifest),
// prints it as one JSON line, an empty manifest when the store has none yet,
// and exits 0. The other exit codes:
//
//   1  the request could not be followed: no token, or arguments given;
//   3  anything else: the store could not be read, or the stored manifest
//      breaks the manifest's rules.
import { get, type put } from "@vercel/blob";

import { readCalibrationTreeManifest, type CalibrationTreeBlobClient } from "./calibration-tree-blob";
import type { CalibrationTreeManifest } from "./calibration-tree-manifest";
import { CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE } from "./calibration-tree-publication";

export const CALIBRATION_TREE_MANIFEST_READ_EXIT_REQUEST_ERROR = 1;
export const CALIBRATION_TREE_MANIFEST_READ_EXIT_STORE_ERROR = 3;

/** The request could not be followed; nothing was read. */
export class CalibrationTreeManifestReadRequestError extends Error {}

// A client that can only read: a write through it fails before it is sent.
const refuseWrite = (async () => {
  throw new Error("The calibration tree manifest read never writes to the store.");
}) as unknown as typeof put;
const READ_ONLY_CLIENT: CalibrationTreeBlobClient = { get, put: refuseWrite };

/** The stored manifest, read without the cache; an empty one while there is none. */
export async function readStoredCalibrationTreeManifest(options: {
  token: string;
  client?: CalibrationTreeBlobClient;
}): Promise<CalibrationTreeManifest> {
  const { manifest } = await readCalibrationTreeManifest({
    token: options.token,
    consistent: true,
    client: options.client ?? READ_ONLY_CLIENT,
  });
  return manifest;
}

/** Runs the command with its arguments and returns the process exit code. */
export async function calibrationTreeManifestReadMain(
  argv: string[],
  environment: Record<string, string | undefined> = process.env,
  client?: CalibrationTreeBlobClient,
): Promise<number> {
  try {
    if (argv.length !== 0) {
      throw new CalibrationTreeManifestReadRequestError(
        "Usage: bun run src/calibration-tree-manifest-read.ts",
      );
    }
    const token = environment[CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE]?.trim();
    if (!token) {
      throw new CalibrationTreeManifestReadRequestError(
        `${CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE} is required.`,
      );
    }
    const manifest = await readStoredCalibrationTreeManifest({ token, client });
    console.log(JSON.stringify(manifest));
    return 0;
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    return error instanceof CalibrationTreeManifestReadRequestError
      ? CALIBRATION_TREE_MANIFEST_READ_EXIT_REQUEST_ERROR
      : CALIBRATION_TREE_MANIFEST_READ_EXIT_STORE_ERROR;
  }
}

if (import.meta.main) {
  process.exitCode = await calibrationTreeManifestReadMain(Bun.argv.slice(2));
}
