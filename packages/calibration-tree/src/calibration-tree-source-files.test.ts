// The stored copies of a build's original files: where they live in the tree
// store, and the package's reader for them.
import { expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

const BUILD_ID = "a".repeat(64);
const TOKEN = "vercel_blob_rw_test_token";

async function sourceFiles() {
  return await import("./calibration-tree-source-files");
}

interface RecordedGet {
  pathname: string;
  options: Record<string, unknown>;
}

function clientAnswering(answer: { statusCode: number; body: Uint8Array } | null) {
  const gets: RecordedGet[] = [];
  const client = {
    get: async (pathname: string, options: Record<string, unknown>) => {
      gets.push({ pathname, options });
      return answer && {
        statusCode: answer.statusCode,
        stream: new Response(Buffer.from(answer.body)).body,
      };
    },
  };
  return { client: client as never, gets };
}

function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

test("each original file has its own path, apart from everything under calibration-trees/", async () => {
  const { CALIBRATION_TREE_SOURCE_FILES, calibrationTreeSourceFilePath } = await sourceFiles();

  const paths = CALIBRATION_TREE_SOURCE_FILES.map((file) => calibrationTreeSourceFilePath("us", BUILD_ID, file));

  expect(paths).toEqual([
    `calibration-tree-sources/us/${BUILD_ID}/calibration_diagnostics.json.gz`,
    `calibration-tree-sources/us/${BUILD_ID}/build_manifest.json.gz`,
    `calibration-tree-sources/us/${BUILD_ID}/release_manifest.json.gz`,
    `calibration-tree-sources/us/${BUILD_ID}/demographics.json.gz`,
    `calibration-tree-sources/us/${BUILD_ID}/parent_build_manifest.json.gz`,
    `calibration-tree-sources/us/${BUILD_ID}/reform_validation.json.gz`,
  ]);
});

test("a path is given only for a build ID and a known file", async () => {
  const { calibrationTreeSourceFilePath } = await sourceFiles();

  expect(() => calibrationTreeSourceFilePath("us", "../manifest", "buildManifest")).toThrow();
  expect(() => calibrationTreeSourceFilePath("us", BUILD_ID, "unknown" as never)).toThrow();
});

test("the reader returns a stored file's original bytes and their digest, read privately", async () => {
  const { readCalibrationTreeSourceFile } = await sourceFiles();
  const original = new TextEncoder().encode('{"loss_trajectory":[1,0.5]}');
  const { client, gets } = clientAnswering({ statusCode: 200, body: gzipSync(original) });

  const stored = await readCalibrationTreeSourceFile({
    country: "uk",
    buildArtifactId: BUILD_ID,
    file: "calibrationDiagnostics",
    token: TOKEN,
    client,
  });

  expect(stored).toEqual({ bytes: original, sha256: sha256(original) });
  expect(gets).toEqual([{
    pathname: `calibration-tree-sources/uk/${BUILD_ID}/calibration_diagnostics.json.gz`,
    options: { access: "private", useCache: true, token: TOKEN },
  }]);
});

test("a consistent read skips the cache", async () => {
  const { readCalibrationTreeSourceFile } = await sourceFiles();
  const { client, gets } = clientAnswering(null);

  await readCalibrationTreeSourceFile({
    country: "uk",
    buildArtifactId: BUILD_ID,
    file: "buildManifest",
    consistent: true,
    client,
  });

  expect(gets[0].options).toEqual({ access: "private", useCache: false });
});

test("a file the store does not hold is null", async () => {
  const { readCalibrationTreeSourceFile, readCalibrationTreeSourceJson } = await sourceFiles();
  const { client } = clientAnswering(null);
  const request = { country: "us", buildArtifactId: BUILD_ID, file: "reformValidation", client } as const;

  expect(await readCalibrationTreeSourceFile(request)).toBeNull();
  expect(await readCalibrationTreeSourceJson(request)).toBeNull();
});

test("an unexpected answer or damaged bytes are errors, never an empty file", async () => {
  const { readCalibrationTreeSourceFile } = await sourceFiles();
  const request = { country: "us", buildArtifactId: BUILD_ID, file: "buildManifest" } as const;

  await expect(readCalibrationTreeSourceFile({
    ...request,
    client: clientAnswering({ statusCode: 304, body: new Uint8Array() }).client,
  })).rejects.toThrow("status 304");
  await expect(readCalibrationTreeSourceFile({
    ...request,
    client: clientAnswering({ statusCode: 200, body: new TextEncoder().encode("not gzip") }).client,
  })).rejects.toThrow();
});

test("the JSON reader gives the payload and digest the Hugging Face reader gives", async () => {
  const { readCalibrationTreeSourceJson } = await sourceFiles();
  const original = new TextEncoder().encode('{"build_id":"the-parent-build"}');
  const { client } = clientAnswering({ statusCode: 200, body: gzipSync(original) });

  const stored = await readCalibrationTreeSourceJson({
    country: "us",
    buildArtifactId: BUILD_ID,
    file: "parentBuildManifest",
    client,
  });

  expect(stored).toEqual({ payload: { build_id: "the-parent-build" }, sha256: sha256(original) });
});

test("the package exports the reader", () => {
  const manifest = JSON.parse(readFileSync(join(import.meta.dir, "..", "package.json"), "utf8")) as {
    exports: Record<string, string>;
  };

  expect(manifest.exports["./calibration-tree-source-files"]).toBe("./src/calibration-tree-source-files.ts");
});
