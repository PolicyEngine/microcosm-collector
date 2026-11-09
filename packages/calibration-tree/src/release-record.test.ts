import { expect, test } from "bun:test";

import {
  ReleasesRecordError,
  buildFolderId,
  currentDefaultBuild,
  isBuildFolderId,
  isReleaseDirectory,
  parseBuildFolderId,
  parseReleasesRecord,
  recordedBuilds,
  releaseManifestNamesBuild,
  releasesRecordFromResponse,
} from "./release-record";

// The record's documented example (microcosm-emitter#24's
// tests/interface/test_releases_record.py). All values are synthetic.
const NATIONAL = "microcosm-uk-2024-25-national";
const DENSE = "microcosm-uk-2024-25-dense";
const OLDER_BUILD = "uk-frs-calibration-attempt-20261014T214443Z-d12ff9b3";
const NEWER_BUILD = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71";
const DENSE_BUILD = "uk-local-candidate-f100-s42-20261019T000432Z-ee5c2dbe";

function documentedExample(): Record<string, unknown> {
  return {
    schema_version: 1,
    default: NATIONAL,
    releases: {
      [NATIONAL]: {
        role: "national_default",
        current: NEWER_BUILD,
        builds: {
          [OLDER_BUILD]: { published_at: "2026-10-14T21:44:43+00:00", withdrawn: false },
          [NEWER_BUILD]: { published_at: "2026-10-19T08:33:23+00:00", withdrawn: false },
        },
      },
    },
  };
}

function withDense(document: Record<string, unknown>, current: string | null = null) {
  const releases = document.releases as Record<string, unknown>;
  releases[DENSE] = {
    role: "non_default_local_area",
    current,
    builds: {
      [DENSE_BUILD]: { published_at: "2026-10-19T07:53:18+00:00", withdrawn: true },
    },
  };
  return document;
}

function nationalRelease(document: Record<string, unknown>): Record<string, unknown> {
  return (document.releases as Record<string, Record<string, unknown>>)[NATIONAL];
}

test("the documented releases.json example parses into its releases and builds", () => {
  const record = parseReleasesRecord(documentedExample());
  expect(record.default).toBe(NATIONAL);
  expect(record.releases[NATIONAL]).toEqual({
    role: "national_default",
    current: NEWER_BUILD,
    builds: {
      [OLDER_BUILD]: { publishedAt: "2026-10-14T21:44:43+00:00", withdrawn: false },
      [NEWER_BUILD]: { publishedAt: "2026-10-19T08:33:23+00:00", withdrawn: false },
    },
  });
});

test("the repository's current build is the current build of the default release", () => {
  expect(currentDefaultBuild(parseReleasesRecord(documentedExample()))).toEqual({
    release: NATIONAL,
    build: NEWER_BUILD,
    id: `${NATIONAL}/${NEWER_BUILD}`,
  });
});

test("a release may have no current build, and a record may name no default release", () => {
  const record = parseReleasesRecord(withDense(documentedExample()));
  expect(record.releases[DENSE].current).toBeNull();

  const noDefault = documentedExample();
  noDefault.default = null;
  expect(currentDefaultBuild(parseReleasesRecord(noDefault))).toBeNull();

  const noCurrent = documentedExample();
  nationalRelease(noCurrent).current = null;
  expect(currentDefaultBuild(parseReleasesRecord(noCurrent))).toBeNull();
});

test("every listed build is listed once, with its release's role and its own publication time", () => {
  const builds = recordedBuilds(parseReleasesRecord(withDense(documentedExample())));
  expect(builds).toEqual([
    {
      release: DENSE,
      build: DENSE_BUILD,
      id: `${DENSE}/${DENSE_BUILD}`,
      role: "non_default_local_area",
      publishedAt: "2026-10-19T07:53:18+00:00",
      withdrawn: true,
      current: false,
      inDefaultRelease: false,
    },
    {
      release: NATIONAL,
      build: OLDER_BUILD,
      id: `${NATIONAL}/${OLDER_BUILD}`,
      role: "national_default",
      publishedAt: "2026-10-14T21:44:43+00:00",
      withdrawn: false,
      current: false,
      inDefaultRelease: true,
    },
    {
      release: NATIONAL,
      build: NEWER_BUILD,
      id: `${NATIONAL}/${NEWER_BUILD}`,
      role: "national_default",
      publishedAt: "2026-10-19T08:33:23+00:00",
      withdrawn: false,
      current: true,
      inDefaultRelease: true,
    },
  ]);
});

test("a record whose current build is unlisted or withdrawn is refused", () => {
  const unlisted = documentedExample();
  nationalRelease(unlisted).current = "uk-frs-calibration-attempt-20261101T000000Z-00000000";
  expect(() => parseReleasesRecord(unlisted)).toThrow(ReleasesRecordError);
  expect(() => parseReleasesRecord(unlisted)).toThrow("is not listed");

  const withdrawn = documentedExample();
  const builds = nationalRelease(withdrawn).builds as Record<string, Record<string, unknown>>;
  builds[NEWER_BUILD].withdrawn = true;
  expect(() => parseReleasesRecord(withdrawn)).toThrow("is withdrawn");
});

test("a current build named like a built-in object property is refused unless it is listed", () => {
  // The writer's check_record looks the name up among the listed builds only.
  for (const builtInName of ["constructor", "toString", "valueOf", "hasOwnProperty", "__proto__"]) {
    const document = documentedExample();
    nationalRelease(document).current = builtInName;
    expect(() => parseReleasesRecord(document), builtInName).toThrow(ReleasesRecordError);
    expect(() => parseReleasesRecord(document), builtInName).toThrow("is not listed");
  }

  const listed = documentedExample();
  const release = nationalRelease(listed);
  release.builds = {
    ...(release.builds as Record<string, unknown>),
    constructor: { published_at: "2026-10-20T00:00:00+00:00", withdrawn: false },
  };
  release.current = "constructor";
  expect(parseReleasesRecord(listed).releases[NATIONAL].current).toBe("constructor");
});

test("a record whose default release is not listed is refused", () => {
  const document = documentedExample();
  document.default = DENSE;
  expect(() => parseReleasesRecord(document)).toThrow("is not listed");
});

test("a record that is not the documented shape is refused", () => {
  const cases: Array<[string, (document: Record<string, unknown>) => unknown]> = [
    ["not an object", () => []],
    ["an extra top-level key", (document) => ({ ...document, latest: NATIONAL })],
    ["a missing top-level key", ({ default: _default, ...rest }) => rest],
    ["another schema version", (document) => ({ ...document, schema_version: 2 })],
    ["an unsafe release name", (document) => ({
      ...document,
      default: null,
      releases: { "../other": nationalRelease(document) },
    })],
    ["an invalid release role", (document) => {
      nationalRelease(document).role = "National Default";
      return document;
    }],
    ["a release without its role", (document) => {
      delete nationalRelease(document).role;
      return document;
    }],
    ["an unsafe build name", (document) => {
      const release = nationalRelease(document);
      release.current = null;
      release.builds = { "a/b": { published_at: "2026-10-19T08:33:23+00:00", withdrawn: false } };
      return document;
    }],
    ["a build without its publication time", (document) => {
      const builds = nationalRelease(document).builds as Record<string, Record<string, unknown>>;
      builds[OLDER_BUILD] = { withdrawn: false };
      return document;
    }],
    ["a build whose withdrawn flag is not a boolean", (document) => {
      const builds = nationalRelease(document).builds as Record<string, Record<string, unknown>>;
      builds[OLDER_BUILD] = { published_at: "2026-10-14T21:44:43+00:00", withdrawn: "no" };
      return document;
    }],
    ["a current build that is not a name", (document) => {
      nationalRelease(document).current = 7;
      return document;
    }],
  ];
  for (const [label, change] of cases) {
    expect(() => parseReleasesRecord(change(documentedExample())), label).toThrow(
      ReleasesRecordError,
    );
  }
});

test("a build is identified by its folder under releases/, release then build", () => {
  expect(buildFolderId(NATIONAL, NEWER_BUILD)).toBe(`${NATIONAL}/${NEWER_BUILD}`);
  expect(parseBuildFolderId(`${NATIONAL}/${NEWER_BUILD}`)).toEqual({
    release: NATIONAL,
    build: NEWER_BUILD,
  });
  for (const value of [
    NATIONAL,
    `${NATIONAL}/`,
    `/${NEWER_BUILD}`,
    `${NATIONAL}/${NEWER_BUILD}/release_manifest.json`,
    `${NATIONAL}/..`,
    `${NATIONAL}/.hidden`,
    "latest",
    "",
  ]) {
    expect(parseBuildFolderId(value), value).toBeNull();
    expect(isBuildFolderId(value), value).toBe(false);
  }
  expect(() => buildFolderId(NATIONAL, "a/b")).toThrow();
});

test("a release directory is a release folder or a build folder, never a path that leaves releases/", () => {
  expect(isReleaseDirectory(NATIONAL)).toBe(true);
  expect(isReleaseDirectory(`${NATIONAL}/${NEWER_BUILD}`)).toBe(true);
  for (const value of ["", "..", `${NATIONAL}/..`, `${NATIONAL}/${NEWER_BUILD}/extra`, "a b", 7, null]) {
    expect(isReleaseDirectory(value), String(value)).toBe(false);
  }
});

test("a release manifest names its build by its top-level release and build", () => {
  expect(releaseManifestNamesBuild({ release: NATIONAL, build: NEWER_BUILD }, NATIONAL, NEWER_BUILD))
    .toBe(true);
  // Today's manifests: `build` is an object, and its build_id is the release.
  expect(releaseManifestNamesBuild(
    { build: { build_id: NATIONAL, attempt_id: NEWER_BUILD } },
    NATIONAL,
    NEWER_BUILD,
  )).toBe(false);
  expect(releaseManifestNamesBuild({ release: NATIONAL, build: OLDER_BUILD }, NATIONAL, NEWER_BUILD))
    .toBe(false);
  expect(releaseManifestNamesBuild({ release: DENSE, build: NEWER_BUILD }, NATIONAL, NEWER_BUILD))
    .toBe(false);
  expect(releaseManifestNamesBuild(null, NATIONAL, NEWER_BUILD)).toBe(false);
});

// Only Hugging Face's 404 for the file means a repository has no
// releases.json; every other answer is an error, never a fallback to tags.
const notFound = (errorCode?: string) => new Response("Not found", {
  status: 404,
  headers: errorCode ? { "X-Error-Code": errorCode } : {},
});

test("a 404 for the file, with or without its error code, means the repository has no releases.json", async () => {
  await expect(releasesRecordFromResponse(notFound("EntryNotFound"))).resolves.toBeNull();
  await expect(releasesRecordFromResponse(notFound())).resolves.toBeNull();
});

test("a 404 that says the repository or the revision is missing is an error", async () => {
  await expect(releasesRecordFromResponse(notFound("RepoNotFound")))
    .rejects.toThrow("Hugging Face returned 404 (RepoNotFound) for releases.json.");
  await expect(releasesRecordFromResponse(notFound("RevisionNotFound")))
    .rejects.toThrow("Hugging Face returned 404 (RevisionNotFound) for releases.json.");
});

test("a server error or a refused token for releases.json is an error", async () => {
  await expect(releasesRecordFromResponse(new Response("unavailable", { status: 503 })))
    .rejects.toThrow("Hugging Face returned 503 for releases.json.");
  await expect(releasesRecordFromResponse(new Response("unauthorized", {
    status: 401,
    headers: { "X-Error-Code": "RepoNotFound" },
  }))).rejects.toThrow("Hugging Face returned 401 (RepoNotFound) for releases.json.");
});
