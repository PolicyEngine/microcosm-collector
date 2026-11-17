// The manifest read whose ETag the collector's conditional manifest writes
// send back as ifMatch. Vercel Blob serves a compressed response with the
// weak form of the blob's ETag (W/"…"), while a write compares ifMatch with
// the strong one, so a consistent read asks for the manifest uncompressed.
// The client here answers reads as the store does.
import { expect, test } from "bun:test";

import { readCalibrationTreeManifest, type CalibrationTreeBlobClient } from "./calibration-tree-blob";
import {
  CALIBRATION_TREE_MANIFEST_PATH,
  CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION,
} from "./calibration-tree-manifest";

const TOKEN = "vercel_blob_rw_test_token";
const STORED_ETAG = '"4602b848005d1000c8209a81f2bcca87"';

interface RecordedRead {
  pathname: string;
  useCache: boolean | undefined;
  acceptEncoding: string | undefined;
}

// A read that accepts a content encoding gets the manifest compressed, with
// the weak form of its ETag; a read that asks for none gets it as stored,
// with the strong ETag that writes compare.
function compressingClient(reads: RecordedRead[]): CalibrationTreeBlobClient {
  const manifestText = JSON.stringify({ schemaVersion: CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION, countries: {} });
  const get = async (pathname: string, options: { useCache?: boolean; headers?: Record<string, string> }) => {
    const acceptEncoding = options.headers?.["accept-encoding"];
    reads.push({ pathname, useCache: options.useCache, acceptEncoding });
    const compressed = acceptEncoding !== "identity";
    return {
      statusCode: 200,
      stream: new Response(manifestText).body,
      headers: new Headers(compressed ? { "content-encoding": "br" } : {}),
      blob: {
        pathname,
        etag: compressed ? `W/${STORED_ETAG}` : STORED_ETAG,
        contentType: "application/json",
        size: manifestText.length,
      },
    };
  };
  const put = async () => {
    throw new Error("Reading the manifest writes nothing.");
  };
  return { get, put } as unknown as CalibrationTreeBlobClient;
}

test("a consistent manifest read asks for it uncompressed, so its ETag is the one writes compare", async () => {
  const reads: RecordedRead[] = [];

  const stored = await readCalibrationTreeManifest({ token: TOKEN, consistent: true, client: compressingClient(reads) });

  expect(stored.etag).toBe(STORED_ETAG);
  expect(reads).toEqual([{ pathname: CALIBRATION_TREE_MANIFEST_PATH, useCache: false, acceptEncoding: "identity" }]);
});

test("a cached manifest read is left as it was: through the cache, with any encoding", async () => {
  const reads: RecordedRead[] = [];

  await readCalibrationTreeManifest({ token: TOKEN, consistent: false, client: compressingClient(reads) });

  expect(reads).toEqual([{ pathname: CALIBRATION_TREE_MANIFEST_PATH, useCache: true, acceptEncoding: undefined }]);
});
