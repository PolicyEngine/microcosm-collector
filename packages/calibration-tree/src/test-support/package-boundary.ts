// Checks that a package stays self-contained: its modules import only each
// other, Node built-ins and the dependencies its manifest declares.
//
// Module references are read from each source file's TypeScript syntax tree,
// so the check sees what the compiler and Bun see: static and type-only
// imports and re-exports, `import x = require()`, `import()` expressions and
// types, `require()`, `require.resolve()`, `import.meta.resolve()`, Bun's
// `mock.module()`, and triple-slash path and types references, in every
// JavaScript and TypeScript source format. Text inside strings and comments
// is not mistaken for an import. Bun's `Transpiler.scanImports()` is not
// used because it drops type-only imports and triple-slash references.
import { Glob } from "bun";
import { existsSync, readFileSync, statSync } from "node:fs";
import { builtinModules } from "node:module";
import { dirname, extname, join, relative, resolve, sep } from "node:path";
import ts from "typescript";

// Bun lists its own modules (`bun`, `bun:test`, ...) among the built-ins.
const NODE_BUILTINS = new Set(builtinModules.filter((name) => !name.startsWith("bun")));
const SOURCE_EXTENSIONS = [".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"];
// TypeScript resolves an import of a compiled JavaScript name to its source.
const SOURCE_EXTENSIONS_FOR_COMPILED: Record<string, string[]> = {
  ".js": [".ts", ".tsx"],
  ".jsx": [".tsx"],
  ".mjs": [".mts"],
  ".cjs": [".cts"],
};
// The test file names Bun's test runner discovers.
const TEST_FILE_NAME = /[._](?:test|spec)\.[cm]?[jt]sx?$/;
// Calls whose first argument names a module.
const MODULE_LOADING_CALLS = new Set(["require", "require.resolve", "import.meta.resolve", "mock.module"]);
const NOT_A_STRING_LITERAL = "has a module specifier that is not a string literal";
const CREATES_REQUIRE = "makes a require function whose imports cannot be checked";

export interface PackageManifest {
  name: string;
  exports: Record<string, string>;
  dependencies?: Record<string, string>;
  devDependencies?: Record<string, string>;
}

// A module the source names as a string, or a reference the check cannot
// follow, described by its source text.
type ModuleReference = { specifier: string } | { expression: string; reason: string };

export function readPackageManifest(packageRoot: string): PackageManifest {
  return JSON.parse(readFileSync(join(packageRoot, "package.json"), "utf8"));
}

// JavaScript and TypeScript sources relative to the package root, such as
// `src/countries.ts` or `types/next-fetch-cache.d.ts`.
export function packageSourceFiles(packageRoot: string): string[] {
  const extensions = SOURCE_EXTENSIONS.map((extension) => extension.slice(1)).join(",");
  return [...new Glob(`**/*.{${extensions}}`).scanSync({ cwd: packageRoot })]
    .filter((relativePath) => !relativePath.split(sep).includes("node_modules"))
    .sort();
}

// Tests and their support code may also use the test runner, the package's
// own entry points and its development dependencies.
export function isTestCode(relativePath: string): boolean {
  return TEST_FILE_NAME.test(relativePath) || relativePath.startsWith("src/test-support/");
}

// The dotted name of a call's callee, such as `require.resolve`, or null.
function calleeName(expression: ts.Expression): string | null {
  if (ts.isIdentifier(expression)) return expression.text;
  if (ts.isMetaProperty(expression)) {
    return `${ts.tokenToString(expression.keywordToken)}.${expression.name.text}`;
  }
  if (ts.isPropertyAccessExpression(expression)) {
    const objectName = calleeName(expression.expression);
    return objectName === null ? null : `${objectName}.${expression.name.text}`;
  }
  return null;
}

function specifierReference(
  specifier: ts.Node | undefined,
  enclosingNode: ts.Node,
  sourceFile: ts.SourceFile,
): ModuleReference[] {
  if (specifier === undefined) return [];
  if (ts.isStringLiteralLike(specifier)) return [{ specifier: specifier.text }];
  return [{ expression: enclosingNode.getText(sourceFile), reason: NOT_A_STRING_LITERAL }];
}

function nodeReferences(node: ts.Node, sourceFile: ts.SourceFile): ModuleReference[] {
  if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) {
    return specifierReference(node.moduleSpecifier, node, sourceFile);
  }
  if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)) {
    return specifierReference(node.moduleReference.expression, node, sourceFile);
  }
  if (ts.isImportTypeNode(node)) {
    const argument = ts.isLiteralTypeNode(node.argument) ? node.argument.literal : node.argument;
    return specifierReference(argument, node, sourceFile);
  }
  if (ts.isCallExpression(node)) {
    const loadsModule =
      node.expression.kind === ts.SyntaxKind.ImportKeyword ||
      MODULE_LOADING_CALLS.has(calleeName(node.expression) ?? "");
    return loadsModule ? specifierReference(node.arguments[0], node, sourceFile) : [];
  }
  // `createRequire(...)` returns a require function under any name, including
  // through `import { createRequire as ... }`.
  if (ts.isIdentifier(node) && node.text === "createRequire") {
    return [{ expression: "createRequire", reason: CREATES_REQUIRE }];
  }
  return [];
}

function moduleReferences(fileName: string, sourceText: string): ModuleReference[] {
  const sourceFile = ts.createSourceFile(fileName, sourceText, ts.ScriptTarget.Latest, true);
  const references: ModuleReference[] = [
    // A triple-slash path is relative to its file even without `./`.
    ...sourceFile.referencedFiles.map(({ fileName: referencedPath }) => ({
      specifier: /^\.{1,2}\//.test(referencedPath) ? referencedPath : `./${referencedPath}`,
    })),
    // Triple-slash `lib` references name the compiler's own declarations.
    ...sourceFile.typeReferenceDirectives.map(({ fileName: typesPackage }) => ({
      specifier: typesPackage,
    })),
  ];
  const visit = (node: ts.Node): void => {
    references.push(...nodeReferences(node, sourceFile));
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return references;
}

function packageName(specifier: string): string {
  const segments = specifier.split("/");
  return specifier.startsWith("@") ? segments.slice(0, 2).join("/") : segments[0];
}

function isFile(candidate: string): boolean {
  return existsSync(candidate) && statSync(candidate).isFile();
}

function resolvesInsidePackage(packageRoot: string, fromFile: string, specifier: string): boolean {
  const base = resolve(dirname(fromFile), specifier);
  if (relative(packageRoot, base).split(sep)[0] === "..") return false;
  const extension = extname(base);
  const withoutExtension = base.slice(0, base.length - extension.length);
  return [
    base,
    `${base}.d.ts`,
    ...SOURCE_EXTENSIONS.map((sourceExtension) => `${base}${sourceExtension}`),
    ...SOURCE_EXTENSIONS.map((sourceExtension) => join(base, `index${sourceExtension}`)),
    ...(SOURCE_EXTENSIONS_FOR_COMPILED[extension] ?? []).map(
      (sourceExtension) => `${withoutExtension}${sourceExtension}`,
    ),
  ].some(isFile);
}

function importViolation(
  packageRoot: string,
  relativePath: string,
  specifier: string,
  manifest: PackageManifest,
): string | null {
  const file = join(packageRoot, relativePath);
  const testCode = isTestCode(relativePath);
  if (specifier.startsWith(".")) {
    return resolvesInsidePackage(packageRoot, file, specifier)
      ? null
      : "does not resolve inside the package";
  }
  if (specifier === "bun" || specifier.startsWith("bun:")) {
    return testCode ? null : "is test-runner code outside a test";
  }
  if (specifier.startsWith("node:")) {
    return NODE_BUILTINS.has(specifier.slice("node:".length)) ? null : "is not a Node built-in";
  }
  if (NODE_BUILTINS.has(specifier)) return "must use the node: prefix";
  if (specifier.startsWith("@/")) return "is a dashboard path alias";
  if (packageName(specifier) === manifest.name) {
    if (!testCode) return "is a self-reference; package modules import each other relatively";
    return `./${specifier.slice(manifest.name.length + 1)}` in manifest.exports
      ? null
      : "is not a package export";
  }
  const declared = {
    ...manifest.dependencies,
    ...(testCode ? manifest.devDependencies : {}),
  };
  return packageName(specifier) in declared ? null : "is not a declared dependency";
}

// Every module reference in the package that leaves its boundary, as
// `<file>: "<specifier>" <reason>`, or `<file>: <source text> <reason>` for a
// reference the check cannot follow. Each distinct violation is listed once.
export function packageBoundaryViolations(packageRoot: string): string[] {
  const manifest = readPackageManifest(packageRoot);
  return packageSourceFiles(packageRoot).flatMap((relativePath) => {
    const sourceText = readFileSync(join(packageRoot, relativePath), "utf8");
    const violations = moduleReferences(relativePath, sourceText).flatMap((reference) => {
      if ("expression" in reference) {
        return [`${relativePath}: ${reference.expression} ${reference.reason}`];
      }
      const violation = importViolation(packageRoot, relativePath, reference.specifier, manifest);
      return violation ? [`${relativePath}: "${reference.specifier}" ${violation}`] : [];
    });
    return [...new Set(violations)];
  });
}
