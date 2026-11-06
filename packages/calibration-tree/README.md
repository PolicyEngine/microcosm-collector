# @policyengine/calibration-tree

Builds, validates, reads and publishes Microcosm calibration tree artifacts
(tree schema 6), with the release and staging readers they are built from.
The PolicyEngine calibration dashboard reads trees with it, and the Microcosm
collector builds them with it.

## Using it

The package ships TypeScript source, not compiled JavaScript. Each module is
its own entry point, for example:

```ts
import { parseCalibrationTreeIndex } from "@policyengine/calibration-tree/calibration-tree-artifact";
import { COUNTRY_REGISTRY } from "@policyengine/calibration-tree/countries";
```

- **Bun** runs the modules directly.
- **Next.js** compiles them when the package is listed in `transpilePackages`
  in `next.config.ts`.
- **TypeScript** checks them with `moduleResolution` set to `bundler`. Some
  modules use Bun's globals (`Bun.argv`, `import.meta.main`), so the checking
  project needs `@types/bun`; `types/next-fetch-cache.d.ts` declares the
  `next.revalidate` fetch option the readers pass, for projects without
  Next.js.

Plain Node.js cannot load the modules, because they are TypeScript.

## The tree worker

`src/calibration-tree-worker.ts` builds one release's tree from local copies
of the files the publisher reads from Hugging Face, and writes each part's
gzip bytes under an output directory:

```bash
bun run src/calibration-tree-worker.ts REQUEST.json OUTPUT_DIRECTORY
```

The module's opening comment describes the request. The worker writes the
same bytes as the publisher (`src/publish-calibration-tree.ts`) for the same
files, commit and publication time; the golden tests hold both to the trees
recorded in `src/fixtures/calibration-tree-golden.json`. It exits 0 after
printing a JSON description of the written parts, 2 when the files cannot be
built into a tree, and 1 when the request cannot be followed.

## Development

```bash
bun install --frozen-lockfile
bun test src/calibration-tree-worker.test.ts
bun run typecheck
```

The source lives in
[PolicyEngine/microcosm-collector](https://github.com/PolicyEngine/microcosm-collector)
under `packages/calibration-tree`; its README describes how the package is
released.
