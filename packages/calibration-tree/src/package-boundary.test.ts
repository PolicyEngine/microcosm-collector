// The package must stay self-contained so it can leave the dashboard's
// repository: its modules import only each other, Node built-ins and the
// dependencies its manifest declares, never the dashboard's `@/` aliases,
// Next.js or `server-only`. Consumers reach it only through its exports.
import { afterEach, expect, test } from "bun:test";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, relative, resolve } from "node:path";

import {
  isTestCode,
  packageBoundaryViolations,
  packageSourceFiles,
  readPackageManifest,
} from "./test-support/package-boundary";

const PACKAGE_ROOT = resolve(import.meta.dir, "..");
const SOURCE_ROOT = join(PACKAGE_ROOT, "src");

test("package modules import only package files, Node built-ins and declared dependencies", () => {
  expect(packageBoundaryViolations(PACKAGE_ROOT)).toEqual([]);
});

test("every package export resolves to a package module", () => {
  const manifest = readPackageManifest(PACKAGE_ROOT);
  const unresolved = Object.entries(manifest.exports).flatMap(([subpath, target]) => {
    const file = resolve(PACKAGE_ROOT, target);
    const valid =
      subpath === `./${basename(target, ".ts")}` &&
      !relative(SOURCE_ROOT, file).startsWith("..") &&
      existsSync(file);
    return valid ? [] : [`${subpath} -> ${target}`];
  });

  expect(Object.keys(manifest.exports).length).toBeGreaterThan(0);
  expect(unresolved).toEqual([]);
});

test("every package module is exported", () => {
  const manifest = readPackageManifest(PACKAGE_ROOT);
  const exportedTargets = new Set(Object.values(manifest.exports));
  const unexported = packageSourceFiles(PACKAGE_ROOT)
    .filter((relativePath) => relativePath.startsWith("src/") && !relativePath.endsWith(".d.ts"))
    .filter((relativePath) => !isTestCode(relativePath))
    .filter((relativePath) => !exportedTargets.has(`./${relativePath}`));

  expect(unexported).toEqual([]);
});

// The boundary check itself, run over packages planted in a temporary
// directory beside a stand-in for the dashboard's own modules, so no real
// package source is changed.
const FIXTURE_PACKAGE_NAME = "@policyengine/boundary-fixture";
const plantedDirectories: string[] = [];

afterEach(() => {
  for (const plantedDirectory of plantedDirectories.splice(0)) {
    rmSync(plantedDirectory, { recursive: true, force: true });
  }
});

function writeFiles(root: string, files: Record<string, string>): void {
  for (const [relativePath, contents] of Object.entries(files)) {
    mkdirSync(dirname(join(root, relativePath)), { recursive: true });
    writeFileSync(join(root, relativePath), contents);
  }
}

// Lays out <temp>/frontend/lib (the dashboard) and
// <temp>/frontend/packages/boundary-fixture (the package), and returns the
// package root. From the package's src/, `../../../lib/` is the dashboard.
function plantPackage(sourceFiles: Record<string, string>): string {
  const plantedDirectory = mkdtempSync(join(tmpdir(), "package-boundary-"));
  plantedDirectories.push(plantedDirectory);
  writeFiles(join(plantedDirectory, "frontend/lib"), {
    "base-path.ts": "export const withBasePath = (route: string) => `/calibration${route}`;\n",
    "globals.d.ts": "declare const dashboardBuildId: string;\n",
  });
  const packageRoot = join(plantedDirectory, "frontend/packages/boundary-fixture");
  writeFiles(packageRoot, {
    "package.json": JSON.stringify({
      name: FIXTURE_PACKAGE_NAME,
      exports: { "./blob-store": "./src/blob-store.ts" },
      dependencies: { "@vercel/blob": "^2.6.1" },
      devDependencies: { "@types/bun": "^1.3.14", typescript: "^5.8.0" },
    }),
    ...sourceFiles,
  });
  return packageRoot;
}

function sorted(violations: string[]): string[] {
  return [...violations].sort();
}

test("the boundary check reports a relative require() of a dashboard module", () => {
  const packageRoot = plantPackage({
    "src/base-path-consumer.ts": [
      'const { withBasePath } = require("../../../lib/base-path");',
      'export const dashboardRoot: string = withBasePath("/");',
      "",
    ].join("\n"),
  });

  expect(packageBoundaryViolations(packageRoot)).toEqual([
    'src/base-path-consumer.ts: "../../../lib/base-path" does not resolve inside the package',
  ]);
});

test("the boundary check reads every JavaScript and TypeScript source format", () => {
  const packageRoot = plantPackage({
    "src/common-loader.cjs": 'module.exports = require("server-only");\n',
    "src/common-types.cts": 'import fs = require("fs");\nexport = fs;\n',
    "src/explorer-view.tsx":
      'import { useState } from "react";\nexport const useExplorer = () => useState(0);\n',
    "src/explorer-link.jsx":
      'import Link from "next/link";\nexport const ExplorerLink = () => <Link href="/" />;\n',
    "src/legacy-loader.js":
      'const blob = require("@vercel/blob");\nmodule.exports = { blob, server: require("next/server") };\n',
    "src/module-loader.mjs": 'export { withBasePath } from "@/lib/base-path";\n',
    "src/module-types.mts": 'export type { NextRequest } from "next/server";\n',
  });

  expect(sorted(packageBoundaryViolations(packageRoot))).toEqual(
    sorted([
      'src/common-loader.cjs: "server-only" is not a declared dependency',
      'src/common-types.cts: "fs" must use the node: prefix',
      'src/explorer-link.jsx: "next/link" is not a declared dependency',
      'src/explorer-view.tsx: "react" is not a declared dependency',
      'src/legacy-loader.js: "next/server" is not a declared dependency',
      'src/module-loader.mjs: "@/lib/base-path" is a dashboard path alias',
      'src/module-types.mts: "next/server" is not a declared dependency',
    ]),
  );
});

test("the boundary check follows triple-slash path and types references", () => {
  const packageRoot = plantPackage({
    "src/dashboard-globals.ts": [
      '/// <reference path="../../../lib/globals.d.ts" />',
      '/// <reference types="next" />',
      '/// <reference lib="dom" />',
      "export const buildId = dashboardBuildId;",
      "",
    ].join("\n"),
  });

  expect(sorted(packageBoundaryViolations(packageRoot))).toEqual(
    sorted([
      'src/dashboard-globals.ts: "../../../lib/globals.d.ts" does not resolve inside the package',
      'src/dashboard-globals.ts: "next" is not a declared dependency',
    ]),
  );
});

test("the boundary check reports type-only imports and import types", () => {
  const packageRoot = plantPackage({
    "src/dashboard-types.ts": [
      'import type { NextRequest } from "next/server";',
      'export type BasePathModule = typeof import("@/lib/base-path");',
      'export type { RouteConfig } from "../../../lib/route-config";',
      "export type Request = NextRequest;",
      "",
    ].join("\n"),
  });

  expect(sorted(packageBoundaryViolations(packageRoot))).toEqual(
    sorted([
      'src/dashboard-types.ts: "../../../lib/route-config" does not resolve inside the package',
      'src/dashboard-types.ts: "@/lib/base-path" is a dashboard path alias',
      'src/dashboard-types.ts: "next/server" is not a declared dependency',
    ]),
  );
});

test("the boundary check reports module references it cannot read statically", () => {
  const packageRoot = plantPackage({
    "src/computed-loader.ts": [
      'import { createRequire as makeRequire } from "node:module";',
      'const dashboardModule = "../../../lib/base-path";',
      "export const loadDashboard = () => require(dashboardModule);",
      "export const loadLazily = () => import(`../../../lib/${dashboardModule}`);",
      "const requireFromHere = makeRequire(import.meta.url);",
      'export const viaCreatedRequire = requireFromHere("../../../lib/base-path");',
      "",
    ].join("\n"),
  });

  expect(sorted(packageBoundaryViolations(packageRoot))).toEqual(
    sorted([
      "src/computed-loader.ts: createRequire makes a require function whose imports cannot be checked",
      "src/computed-loader.ts: import(`../../../lib/${dashboardModule}`) has a module specifier that is not a string literal",
      "src/computed-loader.ts: require(dashboardModule) has a module specifier that is not a string literal",
    ]),
  );
});

test("the boundary check accepts package files, Node built-ins and declared dependencies in every source format", () => {
  const packageRoot = plantPackage({
    "src/ambient.d.ts": "declare const fixtureBuildId: string;\n",
    "src/blob-store.ts": [
      '/// <reference path="./ambient.d.ts" />',
      '/// <reference lib="dom" />',
      'import { put } from "@vercel/blob";',
      'import { readFileSync } from "node:fs";',
      "export { put, readFileSync };",
      "",
    ].join("\n"),
    "src/blob-store.test.ts": [
      'import { expect, test } from "bun:test";',
      'import ts from "typescript";',
      `import { put } from "${FIXTURE_PACKAGE_NAME}/blob-store";`,
      'test("loads", () => expect([put, ts]).toHaveLength(2));',
      "",
    ].join("\n"),
    "src/fixtures/labels.json": "{}\n",
    "src/labels.ts": 'export const labels = 1;\nexport const load = () => import("./labels.js");\n',
    "src/legacy-labels.cjs":
      'module.exports = { labels: require("./labels.js"), raw: require("./fixtures/labels.json") };\n',
    "src/label-view.tsx":
      'import { labels } from "./labels";\nexport const LabelView = () => <span>{labels}</span>;\n',
    "src/label-paths.mjs": 'export const labelsPath = require.resolve("./labels.ts");\n',
  });

  expect(packageBoundaryViolations(packageRoot)).toEqual([]);
});
