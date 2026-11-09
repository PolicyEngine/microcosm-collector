import { expect, test } from "bun:test";
import { gunzipSync, gzipSync } from "node:zlib";

import { buildCalibrationTreeBundle } from "./calibration-tree-bundle";
import {
  auditCalibrationTreeBuild,
  listCalibrationTreeBlobs,
  readCalibrationTreeManifest,
  updateCalibrationTreeManifest,
  uploadCalibrationTreeBundle,
  type CalibrationTreeBlobClient,
} from "./calibration-tree-blob";

function memoryClient(transientManifestConflicts = 0) {
  const content = new Map<string, BlobPart>();
  const etags = new Map<string, string>();
  const putCalls: Array<{ pathname: string; options: Record<string, unknown> }> = [];
  let version = 0;
  const client: CalibrationTreeBlobClient = {
    get: (async (pathname: string, options: { ifNoneMatch?: string }) => {
      const body = content.get(pathname);
      if (body == null) return null;
      const etag = etags.get(pathname)!;
      const metadata = {
        url: `https://blob.example/${pathname}`,
        downloadUrl: `https://blob.example/${pathname}?download=1`,
        pathname,
        contentDisposition: "inline",
        cacheControl: "public, max-age=60",
        uploadedAt: new Date("2026-09-15T12:00:00.000Z"),
        etag,
      };
      if (options.ifNoneMatch === etag) {
        return {
          statusCode: 304 as const,
          stream: null,
          headers: new Headers(),
          blob: { ...metadata, contentType: null, size: null },
        };
      }
      return {
        statusCode: 200 as const,
        stream: new Blob([body]).stream(),
        headers: new Headers(),
        blob: {
          ...metadata,
          contentType: "application/json; charset=utf-8",
          size: new Blob([body]).size,
        },
      };
    }) as CalibrationTreeBlobClient["get"],
    put: (async (
      pathname: string,
      body: BlobPart,
      options: Record<string, unknown>,
    ) => {
      putCalls.push({ pathname, options });
      if (
        pathname.endsWith("/manifest.json") &&
        version > 0 &&
        options.ifMatch != null &&
        transientManifestConflicts > 0
      ) {
        transientManifestConflicts -= 1;
        throw new Error("Vercel Blob: Precondition failed: ETag mismatch.");
      }
      const existingEtag = etags.get(pathname);
      if (
        existingEtag &&
        options.ifMatch != null &&
        options.ifMatch !== existingEtag
      ) {
        throw new Error("precondition failed");
      }
      if (existingEtag && !options.ifMatch && options.allowOverwrite !== true) {
        throw new Error("already exists");
      }
      version += 1;
      const etag = `\"etag-${version}\"`;
      content.set(pathname, body);
      etags.set(pathname, etag);
      return {
        url: `https://blob.example/${pathname}`,
        downloadUrl: `https://blob.example/${pathname}?download=1`,
        pathname,
        contentType: "application/json; charset=utf-8",
        contentDisposition: "inline",
      };
    }) as unknown as CalibrationTreeBlobClient["put"],
    list: (async (options: { prefix?: string }) => {
      const blobs = await Promise.all(
        [...content.entries()]
          .filter(([pathname]) => pathname.startsWith(options.prefix ?? ""))
          .map(async ([pathname, body]) => ({
            url: `https://blob.example/${pathname}`,
            downloadUrl: `https://blob.example/${pathname}?download=1`,
            pathname,
            size: new Blob([body]).size,
            uploadedAt: new Date("2026-09-15T12:00:00.000Z"),
            etag: etags.get(pathname)!,
          })),
      );
      return { blobs, hasMore: false };
    }) as CalibrationTreeBlobClient["list"],
  };
  return { client, content, putCalls };
}

function treeBundle() {
  return buildCalibrationTreeBundle({
    country: "us",
    releaseId: "microcosm-us-test",
    hfRepo: "policyengine/populace-us",
    hfCommitSha: "1234567890abcdef1234567890abcdef12345678",
    sourceArtifacts: {
      calibrationDiagnostics: {
        path: "releases/microcosm-us-test/calibration_diagnostics.json",
        sha256: "a".repeat(64),
      },
      buildManifest: null,
      releaseManifest: null,
      demographics: null,
    },
    rows: [{
      comparison_id: "target",
      name: "target",
      target_label: "Target",
      target_representation: "hierarchy",
      source: "chronicle",
      variable: "snap",
      geography: "United States",
      level: "national",
      abs_relative_error: 0.01,
      final_loss_contribution: 0.01,
      target_loss_weight_share: 1,
      calibration_status: "included",
      target_dimensions: [],
    }],
  });
}

test("bundle upload writes the index last, verifies every part, and is idempotent", async () => {
  const store = memoryClient();
  const bundle = treeBundle();
  const first = await uploadCalibrationTreeBundle({
    bundle,
    token: "publisher-token",
    client: store.client,
  });
  const second = await uploadCalibrationTreeBundle({
    bundle,
    token: "publisher-token",
    client: store.client,
  });
  expect(first.files.every((file) => file.created)).toBe(true);
  expect(second.files.every((file) => !file.created)).toBe(true);
  expect(store.putCalls.map((call) => call.pathname).at(-1)).toEndWith("/index.json.gz");
  expect(store.putCalls[0].options).toMatchObject({
    access: "private",
    token: "publisher-token",
    addRandomSuffix: false,
    multipart: true,
    contentType: "application/gzip",
  });
  const storedIndex = store.content.get(`${bundle.files.at(-1)!.path}`);
  expect(gunzipSync(await new Blob([storedIndex!]).arrayBuffer()).toString("utf8"))
    .toBe(bundle.files.at(-1)!.serialized);

  store.content.set(first.files[0].pathname, gzipSync("changed"));
  await expect(uploadCalibrationTreeBundle({
    bundle,
    token: "publisher-token",
    client: store.client,
  })).rejects.toThrow("different content");
});

test("bundle audit verifies the index and every referenced compressed part", async () => {
  const store = memoryClient();
  const bundle = treeBundle();
  const stored = await uploadCalibrationTreeBundle({
    bundle,
    token: "publisher-token",
    client: store.client,
  });
  const entry = {
    buildArtifactId: bundle.index.buildArtifactId,
    kind: "release" as const,
    sourceId: "microcosm-us-test",
    label: "microcosm-us-test",
    releaseId: "microcosm-us-test",
    stagingRunId: null,
    hfRepo: "policyengine/populace-us",
    hfCommitSha: "1234567890abcdef1234567890abcdef12345678",
    treeSchemaVersion: 6 as const,
    indexSha256: stored.index.sha256,
    indexBytes: stored.index.bytes,
    createdAt: null,
    updatedAt: "1970-01-01T00:00:00.000Z",
  };
  let blobs = await listCalibrationTreeBlobs({
    country: "us",
    token: "publisher-token",
    client: store.client,
  });
  await expect(auditCalibrationTreeBuild({
    country: "us",
    entry,
    blobs,
    token: "publisher-token",
    client: store.client,
  })).resolves.toMatchObject({ complete: true, repairable: true, reasons: [] });

  const missingPath = bundle.index.parts.targetDetails[0].path;
  store.content.delete(missingPath);
  blobs = await listCalibrationTreeBlobs({
    country: "us",
    token: "publisher-token",
    client: store.client,
  });
  await expect(auditCalibrationTreeBuild({
    country: "us",
    entry,
    blobs,
    token: "publisher-token",
    client: store.client,
  })).resolves.toMatchObject({
    complete: false,
    repairable: true,
    reasons: [`missing ${missingPath}`],
  });

  store.content.set(missingPath, gzipSync("wrong-size"));
  blobs = await listCalibrationTreeBlobs({
    country: "us",
    token: "publisher-token",
    client: store.client,
  });
  await expect(auditCalibrationTreeBuild({
    country: "us",
    entry,
    blobs,
    token: "publisher-token",
    client: store.client,
  })).resolves.toMatchObject({ complete: false, repairable: false });
});

test("manifest writes merge countries and conditionally replace the prior version", async () => {
  const store = memoryClient();
  const baseEntry = {
    buildArtifactId: "b".repeat(64),
    kind: "release" as const,
    sourceId: "microcosm-test",
    label: "microcosm-test",
    releaseId: "microcosm-test",
    stagingRunId: null,
    hfRepo: "policyengine/populace-us",
    hfCommitSha: "1234567890abcdef1234567890abcdef12345678",
    treeSchemaVersion: 6 as const,
    indexSha256: "c".repeat(64),
    indexBytes: 100,
    createdAt: "2026-09-15T12:00:00.000Z",
    updatedAt: "2026-09-15T12:00:00.000Z",
  };
  await updateCalibrationTreeManifest({
    country: "us",
    entry: {
      ...baseEntry,
      sourceId: "microcosm-us-test",
      label: "microcosm-us-test",
      releaseId: "microcosm-us-test",
    },
    token: "publisher-token",
    client: store.client,
  });
  await updateCalibrationTreeManifest({
    country: "uk",
    entry: {
      ...baseEntry,
      buildArtifactId: "d".repeat(64),
      sourceId: "microcosm-uk-test",
      label: "microcosm-uk-test",
      releaseId: "microcosm-uk-test",
      hfRepo: "policyengine/populace-uk-private",
    },
    token: "publisher-token",
    client: store.client,
  });
  const { manifest } = await readCalibrationTreeManifest({
    token: "publisher-token",
    consistent: true,
    client: store.client,
  });
  expect(manifest.countries.us?.builds[0].releaseId).toBe("microcosm-us-test");
  expect(manifest.countries.uk?.builds[0].releaseId).toBe("microcosm-uk-test");
  expect(store.putCalls[1].options.ifMatch).toBe('"etag-1"');
});

test("manifest writes refresh and replace after wrapped Blob ETag conflicts", async () => {
  const store = memoryClient(1);
  const entry = {
    buildArtifactId: "e".repeat(64),
    kind: "release" as const,
    sourceId: "microcosm-us-test",
    label: "microcosm-us-test",
    releaseId: "microcosm-us-test",
    stagingRunId: null,
    hfRepo: "policyengine/populace-us",
    hfCommitSha: "1234567890abcdef1234567890abcdef12345678",
    treeSchemaVersion: 6 as const,
    indexSha256: "f".repeat(64),
    indexBytes: 100,
    createdAt: "2026-09-15T12:00:00.000Z",
    updatedAt: "2026-09-15T12:00:00.000Z",
  };
  await updateCalibrationTreeManifest({
    country: "us",
    entry,
    token: "publisher-token",
    client: store.client,
  });
  await updateCalibrationTreeManifest({
    country: "us",
    entry: {
      ...entry,
      buildArtifactId: "a".repeat(64),
      sourceId: "microcosm-us-second-test",
      label: "microcosm-us-second-test",
      releaseId: "microcosm-us-second-test",
    },
    token: "publisher-token",
    client: store.client,
  });
  expect(store.putCalls).toHaveLength(3);
});
