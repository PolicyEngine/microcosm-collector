// The package was copied from calibration-diagnostics, which keeps its own
// copy until the dashboard imports the published package. provenance.json
// records the commit it was copied from and each copied file's git blob ID,
// so a later change on either side shows up here and can be carried over.
import { expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";

const PACKAGE_ROOT = resolve(import.meta.dir, "..");
const PROVENANCE_PATH = join(PACKAGE_ROOT, "provenance.json");

interface Provenance {
  repository: string;
  commit: string;
  directory: string;
  files: Record<string, string>;
  changed: Record<string, string>;
  collectorFiles: string[];
}

function readProvenance(): Provenance {
  return JSON.parse(readFileSync(PROVENANCE_PATH, "utf8")) as Provenance;
}

// The ID git gives a file's contents, as `git hash-object` prints it.
function gitBlobId(path: string): string {
  const contents = readFileSync(path);
  return createHash("sha1")
    .update(Buffer.concat([Buffer.from(`blob ${contents.byteLength}\0`), contents]))
    .digest("hex");
}

// Every file in the package directory, except installed dependencies.
function packageFiles(): string[] {
  return readdirSync(PACKAGE_ROOT, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => relative(PACKAGE_ROOT, join(entry.parentPath, entry.name)).split(sep).join("/"))
    .filter((path) => !path.split("/").includes("node_modules"))
    .sort();
}

test("the record names the calibration-diagnostics commit and directory the package was copied from", () => {
  expect(existsSync(PROVENANCE_PATH)).toBe(true);
  const provenance = readProvenance();

  expect(provenance.repository).toBe("PolicyEngine/calibration-diagnostics");
  expect(provenance.commit).toMatch(/^[0-9a-f]{40}$/);
  expect(provenance.directory).toBe("frontend/packages/calibration-tree");
});

test("every package file is either copied from calibration-diagnostics or the collector's own", () => {
  expect(existsSync(PROVENANCE_PATH)).toBe(true);
  const provenance = readProvenance();
  const copied = Object.keys(provenance.files);

  expect(copied.filter((path) => provenance.collectorFiles.includes(path))).toEqual([]);
  expect(packageFiles()).toEqual([...copied, ...provenance.collectorFiles].sort());
  expect(Object.keys(provenance.changed).filter((path) => !copied.includes(path))).toEqual([]);
});

test("copied files keep their calibration-diagnostics blob IDs, except the ones recorded as changed", () => {
  expect(existsSync(PROVENANCE_PATH)).toBe(true);
  const provenance = readProvenance();
  const mismatched = Object.entries(provenance.files)
    .filter(([path]) => !(path in provenance.changed))
    .filter(([path, blobId]) => gitBlobId(join(PACKAGE_ROOT, path)) !== blobId)
    .map(([path]) => path);
  const unchangedButRecordedAsChanged = Object.keys(provenance.changed)
    .filter((path) => gitBlobId(join(PACKAGE_ROOT, path)) === provenance.files[path]);

  expect(Object.keys(provenance.files).length).toBeGreaterThan(100);
  expect(mismatched).toEqual([]);
  expect(unchangedButRecordedAsChanged).toEqual([]);
});
