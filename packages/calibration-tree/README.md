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
printing a JSON description of the written parts, 1 when the request cannot be
followed, 2 when the builder refuses the files, so another run on the same
files cannot help, and 3 for anything else: a size limit that is not a
positive integer, or an unexpected error such as a defect in the builder.

## A build's original files

When the worker builds a tree it also writes a gzip copy of each file its
request names, and the publication command uploads the copies to private blobs
before the tree's parts:

```text
calibration-tree-sources/<country>/<build ID>/<file name>.gz
```

The files are the diagnostics, `build_manifest.json`, `release_manifest.json`,
`demographics.json`, `parent_build_manifest.json` and `reform_validation.json`.
A build that lacks one has no copy of it, and neither does a build whose file
over 64 MiB was read from Hugging Face.
`src/calibration-tree-source-files.ts` reads them:

```ts
import { readCalibrationTreeSourceJson } from "@policyengine/calibration-tree/calibration-tree-source-files";

const manifest = await readCalibrationTreeSourceJson({
  country: "us",
  buildArtifactId,
  file: "buildManifest",
  token,
});
```

It returns the parsed `payload` and the file's `sha256`, or `null` without a
copy. `readCalibrationTreeSourceFile` returns the original bytes instead.

For builds the store held before it kept copies, the collector's backfill adds
them with `src/calibration-tree-source-copies.ts`. Its `list <country>` prints
the copies stored for each build, its `sources <country> <build ID>` the files
a stored tree's index records it was built from, and its `store REQUEST.json`
stores the copies a build lacks: one of the four files a tree is built from
only when the tree's index records it with the same SHA-256, and the other two
as given. The index's digest is over the file's text as `TextDecoder` decodes
it (a leading byte-order mark dropped), so a file matches by its bytes or that
text, and its copy is of its bytes. The first copy of a file stands, whichever
writer stored it.

## Copies of staging run files

The collector's webhook-queued job and backfill keep private copies of the files
the dashboard reads for staging runs:

```text
staging-run-files/<country>/index.json
staging-run-files/<country>/runs/<run ID>/<SHA-256 of the file>.gz
```

`src/staging-run-files.ts` reads them. `loadCopiedStagingRuns` returns the shape
of `loadStagingRuns` from the index alone, and `loadCopiedStagingRun` returns one
run's parsed documents, or `null` without a copy:

```ts
import {
  loadCopiedStagingRun,
  loadCopiedStagingRuns,
} from "@policyengine/calibration-tree/staging-run-files";

const { runs } = await loadCopiedStagingRuns({ country: "uk", token });
const run = await loadCopiedStagingRun({ country: "uk", runId, token });
```

The index names each copy by its path in the run folder (`STAGING_RUN_FILE_NAMES`).
A version 1 run's reform validation is `reform_validation.json`; a version 2 run's,
as Microcosm's version 2 writer stages it, is `artifacts/reform_validation.json`.
`stagingReformValidationFileName(runManifest, progress)` names the copy to read: for a
structured run, the one its parsed manifest declares, or `null` when it declares none or
has no manifest; for a version 1 run, `reform_validation.json`. A run is structured when
its run manifest, or its progress when it has no run manifest, is version 2 or later. `loadCopiedStagingRun` reads a structured run's declared
copy only when its digest is the SHA-256 the manifest declares, and gives the run no
reform validation otherwise; a structured run never gets a top-level copy. A reader that
knows fewer names reads an index whose `files` list more, without those files.

The copies hold what the run's files say. Read an unfinished run's current status
from telemetry. `src/staging-run-files-command.ts` is the command the collector
runs to read the index, store copies and read a stored tree's source digests.

## A country's stored releases.json

The collector keeps each country's `releases.json` at its release repo's head
as one private blob, replaced in place:

```text
calibration-tree-sources/<country>/releases-record.json
```

`src/calibration-tree-releases-record.ts` reads it:

```ts
import { readStoredReleasesRecord } from "@policyengine/calibration-tree/calibration-tree-releases-record";

const stored = await readStoredReleasesRecord({ country: "us", token });
```

`stored.state` is one of:

- `record`: `stored.record` is the file, checked by the rules of
  `parseReleasesRecordBytes`, with `hfRepo`, `hfCommitSha`, `copiedAt` and the
  file's `sha256`.
- `no_record`: the repo had no `releases.json` at `hfCommitSha`.
- `not_copied`: the collector has stored nothing for the country yet. This says
  nothing about its repo, so a caller must not treat it as `no_record`.

A stored copy that breaks these rules, and a failed read, throw. A read uses
the store's cache unless it passes `consistent: true`, and the copy is written
with a 60-second cache. `src/calibration-tree-releases-record-copy.ts` is the
command the collector replaces the copy with. Its opening comment describes its
two steps and its exit codes.

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
