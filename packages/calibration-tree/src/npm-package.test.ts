// What npm would publish: the package's TypeScript modules and declarations,
// never its tests, fixtures, test support, lockfile or provenance record.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

const PACKAGE_ROOT = resolve(import.meta.dir, "..");
// npm packs from a temporary cache, so it never touches the user's own.
const PACK_TIMEOUT_MS = 60_000;

interface PackageManifest {
  name: string;
  version: string;
  private?: boolean;
  license?: string;
  repository?: { type?: string; url?: string; directory?: string };
  publishConfig?: { access?: string };
  exports: Record<string, string>;
}

function manifest(): PackageManifest {
  return JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8")) as PackageManifest;
}

function packedFiles(): string[] {
  const result = Bun.spawnSync({
    cmd: ["npm", "pack", "--dry-run", "--json", "--ignore-scripts"],
    cwd: PACKAGE_ROOT,
    env: {
      PATH: process.env.PATH ?? "",
      HOME: process.env.HOME ?? "",
      npm_config_cache: join(PACKAGE_ROOT, "node_modules", ".npm-pack-cache"),
    },
    stdout: "pipe",
    stderr: "pipe",
  });
  expect({ exitCode: result.exitCode, stderr: result.stderr.toString() })
    .toEqual({ exitCode: 0, stderr: expect.any(String) });
  const [packed] = JSON.parse(result.stdout.toString()) as Array<{ files: Array<{ path: string }> }>;
  return packed.files.map((file) => file.path).sort();
}

test("the manifest can be published publicly from the collector's repository", () => {
  const packageManifest = manifest();

  expect(packageManifest.name).toBe("@policyengine/calibration-tree");
  expect(packageManifest.private).toBeUndefined();
  expect(packageManifest.license).toBe("MIT");
  expect(packageManifest.publishConfig?.access).toBe("public");
  // npm checks a trusted publication's provenance against this repository.
  expect(packageManifest.repository).toEqual({
    type: "git",
    url: "git+https://github.com/PolicyEngine/microcosm-collector.git",
    directory: "packages/calibration-tree",
  });
});

test(
  "the published files are the exported modules, the declarations, the manifest and the README",
  () => {
    const exportedModules = Object.values(manifest().exports).map((target) => target.replace(/^\.\//, ""));

    expect(packedFiles()).toEqual(
      [...exportedModules, "types/next-fetch-cache.d.ts", "package.json", "README.md"].sort(),
    );
  },
  PACK_TIMEOUT_MS,
);
