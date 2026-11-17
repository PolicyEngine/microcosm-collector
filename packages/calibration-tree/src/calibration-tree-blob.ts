import {
  BlobPreconditionFailedError,
  get,
  list,
  put,
  type GetBlobResult,
  type ListBlobResultBlob,
} from "@vercel/blob";
import { createHash } from "node:crypto";
import { gunzipSync } from "node:zlib";

import {
  calibrationTreePartPath,
  parseCalibrationTreeIndex,
  type CalibrationTreeIndexArtifact,
  type CalibrationTreePart,
} from "./calibration-tree-artifact";
import {
  calibrationTreeBundleFile,
  validateCalibrationTreeBundle,
  type CalibrationTreeBundle,
  type CalibrationTreeBundleFile,
} from "./calibration-tree-bundle";
import {
  CALIBRATION_TREE_MANIFEST_PATH,
  emptyCalibrationTreeManifest,
  parseCalibrationTreeManifest,
  serializeCalibrationTreeManifest,
  withCalibrationTreeManifestEntry,
  type CalibrationTreeManifest,
  type CalibrationTreeManifestEntry,
} from "./calibration-tree-manifest";
import type { MicrocosmCountry } from "./countries";

const IMMUTABLE_CACHE_SECONDS = 31_536_000;
const MANIFEST_CACHE_SECONDS = 60;

export interface CalibrationTreeBlobClient {
  get: typeof get;
  put: typeof put;
  list?: typeof list;
}

const DEFAULT_BLOB_CLIENT: CalibrationTreeBlobClient = { get, put, list };

function isManifestWriteConflict(error: unknown): boolean {
  return error instanceof BlobPreconditionFailedError ||
    (error instanceof Error && /precondition failed.*etag mismatch/i.test(error.message));
}

function retryDelay(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 100));
}

function tokenOption(token?: string): { token: string } | Record<string, never> {
  return token ? { token } : {};
}

async function streamText(stream: ReadableStream<Uint8Array> | null): Promise<string> {
  if (!stream) throw new Error("Blob response did not include a body.");
  return new Response(stream).text();
}

export async function calibrationTreeBlobText(
  stream: ReadableStream<Uint8Array> | null,
): Promise<string> {
  if (!stream) throw new Error("Compressed calibration tree response did not include a body.");
  const compressed = await new Response(stream).arrayBuffer();
  return gunzipSync(compressed).toString("utf8");
}

export interface StoredCalibrationTreeManifest {
  manifest: CalibrationTreeManifest;
  etag: string | null;
}

export async function readCalibrationTreeManifest(options: {
  token?: string;
  consistent?: boolean;
  client?: CalibrationTreeBlobClient;
} = {}): Promise<StoredCalibrationTreeManifest> {
  const result = await (options.client ?? DEFAULT_BLOB_CLIENT).get(
    CALIBRATION_TREE_MANIFEST_PATH,
    {
      access: "private",
      useCache: options.consistent === false,
      ...tokenOption(options.token),
    },
  );
  if (!result) {
    return { manifest: emptyCalibrationTreeManifest(), etag: null };
  }
  if (result.statusCode !== 200) {
    throw new Error(`Unexpected calibration tree manifest status ${result.statusCode}.`);
  }
  return {
    manifest: parseCalibrationTreeManifest(JSON.parse(await streamText(result.stream))),
    etag: result.blob.etag,
  };
}

export async function getCalibrationTreeBlob(options: {
  country: MicrocosmCountry;
  buildArtifactId: string;
  part: CalibrationTreePart;
  token?: string;
  consistent?: boolean;
  ifNoneMatch?: string;
  client?: CalibrationTreeBlobClient;
}): Promise<GetBlobResult | null> {
  return (options.client ?? DEFAULT_BLOB_CLIENT).get(
    calibrationTreePartPath(
      options.country,
      options.buildArtifactId,
      options.part,
    ),
    {
      access: "private",
      useCache: options.consistent === true ? false : true,
      ifNoneMatch: options.ifNoneMatch,
      ...tokenOption(options.token),
    },
  );
}

export async function listCalibrationTreeBlobs(options: {
  country: MicrocosmCountry;
  token?: string;
  client?: CalibrationTreeBlobClient;
}): Promise<Map<string, ListBlobResultBlob>> {
  const client = options.client ?? DEFAULT_BLOB_CLIENT;
  if (!client.list) throw new Error("Calibration tree Blob listing is unavailable.");
  const blobs = new Map<string, ListBlobResultBlob>();
  let cursor: string | undefined;
  do {
    const page = await client.list({
      prefix: `calibration-trees/${options.country}/`,
      limit: 1_000,
      ...(cursor ? { cursor } : {}),
      ...tokenOption(options.token),
    });
    for (const blob of page.blobs) blobs.set(blob.pathname, blob);
    cursor = page.hasMore ? page.cursor : undefined;
    if (page.hasMore && !cursor) {
      throw new Error("Calibration tree Blob listing omitted its next cursor.");
    }
  } while (cursor);
  return blobs;
}

export interface CalibrationTreeBuildAudit {
  complete: boolean;
  repairable: boolean;
  reasons: string[];
  index: CalibrationTreeIndexArtifact | null;
}

export async function auditCalibrationTreeBuild(options: {
  country: MicrocosmCountry;
  entry: CalibrationTreeManifestEntry;
  blobs: Map<string, ListBlobResultBlob>;
  token?: string;
  client?: CalibrationTreeBlobClient;
}): Promise<CalibrationTreeBuildAudit> {
  const reasons: string[] = [];
  const indexResult = await getCalibrationTreeBlob({
    country: options.country,
    buildArtifactId: options.entry.buildArtifactId,
    part: "index",
    token: options.token,
    consistent: true,
    client: options.client,
  });
  if (!indexResult) {
    return {
      complete: false,
      repairable: true,
      reasons: ["index is missing"],
      index: null,
    };
  }
  if (indexResult.statusCode !== 200) {
    return {
      complete: false,
      repairable: false,
      reasons: [`index returned status ${indexResult.statusCode}`],
      index: null,
    };
  }

  let index: CalibrationTreeIndexArtifact | null = null;
  try {
    const text = await calibrationTreeBlobText(indexResult.stream);
    const bytes = Buffer.byteLength(text, "utf8");
    const sha256 = createHash("sha256").update(text, "utf8").digest("hex");
    if (bytes !== options.entry.indexBytes) reasons.push("index raw byte count differs");
    if (sha256 !== options.entry.indexSha256) reasons.push("index digest differs");
    index = parseCalibrationTreeIndex(JSON.parse(text));
    if (index.country !== options.country) reasons.push("index country differs");
    if (index.buildArtifactId !== options.entry.buildArtifactId) {
      reasons.push("index build id differs");
    }
  } catch (error) {
    reasons.push(
      `index cannot be parsed: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  if (!index) {
    return { complete: false, repairable: false, reasons, index: null };
  }

  const descriptors = [
    index.parts.filterIndex,
    ...index.parts.targetSummaries,
    ...index.parts.targetDetails,
    ...index.parts.tiers,
  ];
  for (const descriptor of descriptors) {
    const blob = options.blobs.get(descriptor.path);
    if (!blob) {
      reasons.push(`missing ${descriptor.path}`);
    } else if (blob.size !== descriptor.gzipBytes) {
      reasons.push(
        `compressed byte count differs for ${descriptor.path}: ${blob.size} != ${descriptor.gzipBytes}`,
      );
    }
  }
  return {
    complete: reasons.length === 0,
    repairable: reasons.every((reason) => reason.startsWith("missing ")),
    reasons,
    index,
  };
}

async function uploadCalibrationTreeFile(options: {
  file: CalibrationTreeBundleFile;
  country: MicrocosmCountry;
  buildArtifactId: string;
  token: string;
  client: CalibrationTreeBlobClient;
}): Promise<{ pathname: string; sha256: string; bytes: number; created: boolean }> {
  const { file, country, buildArtifactId, token, client } = options;
  const existing = await getCalibrationTreeBlob({
    country,
    buildArtifactId,
    part: file.part,
    token,
    consistent: true,
    client,
  });
  if (existing) {
    const existingText = await calibrationTreeBlobText(existing.stream);
    const verified = new TextEncoder().encode(existingText);
    const expected = new TextEncoder().encode(file.serialized);
    if (
      verified.length !== expected.length ||
      verified.some((byte, index) => byte !== expected[index])
    ) {
      throw new Error(
        `Immutable calibration tree part ${file.path} already exists with different content.`,
      );
    }
    return {
      pathname: file.path,
      sha256: file.sha256,
      bytes: file.rawBytes,
      created: false,
    };
  }

  await client.put(file.path, Buffer.from(file.compressed), {
    access: "private",
    token,
    addRandomSuffix: false,
    multipart: true,
    contentType: "application/gzip",
    cacheControlMaxAge: IMMUTABLE_CACHE_SECONDS,
  });
  const uploaded = await getCalibrationTreeBlob({
    country,
    buildArtifactId,
    part: file.part,
    token,
    consistent: true,
    client,
  });
  if (!uploaded || await calibrationTreeBlobText(uploaded.stream) !== file.serialized) {
    throw new Error(`Uploaded calibration tree part ${file.path} failed verification.`);
  }
  return {
    pathname: file.path,
    sha256: file.sha256,
    bytes: file.rawBytes,
    created: true,
  };
}

export async function uploadCalibrationTreeBundle(options: {
  bundle: CalibrationTreeBundle;
  token: string;
  client?: CalibrationTreeBlobClient;
}): Promise<{
  files: Array<{ pathname: string; sha256: string; bytes: number; created: boolean }>;
  index: { pathname: string; sha256: string; bytes: number; created: boolean };
}> {
  const { bundle, token, client = DEFAULT_BLOB_CLIENT } = options;
  validateCalibrationTreeBundle(bundle);
  const uploaded = [];
  for (const file of bundle.files) {
    uploaded.push(await uploadCalibrationTreeFile({
      file,
      country: bundle.index.country,
      buildArtifactId: bundle.index.buildArtifactId,
      token,
      client,
    }));
  }
  return {
    files: uploaded,
    index: uploaded[bundle.files.indexOf(calibrationTreeBundleFile(bundle, "index"))],
  };
}

export async function updateCalibrationTreeManifest(options: {
  country: MicrocosmCountry;
  entry: CalibrationTreeManifestEntry;
  makeLatest?: boolean;
  token: string;
  client?: CalibrationTreeBlobClient;
}): Promise<CalibrationTreeManifest> {
  const client = options.client ?? DEFAULT_BLOB_CLIENT;
  let current = await readCalibrationTreeManifest({
    token: options.token,
    consistent: true,
    client,
  });
  let updated = withCalibrationTreeManifestEntry(
    current.manifest,
    options.country,
    options.entry,
    options.makeLatest,
  );
  try {
    await client.put(
      CALIBRATION_TREE_MANIFEST_PATH,
      serializeCalibrationTreeManifest(updated),
      {
        access: "private",
        token: options.token,
        addRandomSuffix: false,
        allowOverwrite: current.etag != null,
        ifMatch: current.etag ?? undefined,
        contentType: "application/json; charset=utf-8",
        cacheControlMaxAge: MANIFEST_CACHE_SECONDS,
      },
    );
  } catch (error) {
    if (!isManifestWriteConflict(error)) throw error;

    await retryDelay();
    current = await readCalibrationTreeManifest({
      token: options.token,
      consistent: true,
      client,
    });
    updated = withCalibrationTreeManifestEntry(
      current.manifest,
      options.country,
      options.entry,
      options.makeLatest,
    );
    await client.put(
      CALIBRATION_TREE_MANIFEST_PATH,
      serializeCalibrationTreeManifest(updated),
      {
        access: "private",
        token: options.token,
        addRandomSuffix: false,
        allowOverwrite: true,
        contentType: "application/json; charset=utf-8",
        cacheControlMaxAge: MANIFEST_CACHE_SECONDS,
      },
    );
  }

  const verified = await readCalibrationTreeManifest({
    token: options.token,
    consistent: true,
    client,
  });
  const verifiedEntry = verified.manifest.countries[options.country]?.builds.find(
    (entry) => entry.buildArtifactId === options.entry.buildArtifactId,
  );
  if (
    !verifiedEntry ||
    verifiedEntry.hfCommitSha !== options.entry.hfCommitSha ||
    verifiedEntry.indexSha256 !== options.entry.indexSha256
  ) {
    throw new Error("Calibration tree manifest verification failed.");
  }
  return verified.manifest;
}
