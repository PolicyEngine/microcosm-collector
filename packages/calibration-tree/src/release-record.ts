// `releases.json`: the one record of which build of each release a Microcosm
// dataset repository has published and which is current. The repository
// publishes every build in its own folder, never changed after publishing:
//
//   releases.json
//   releases/<release>/<build>/release_manifest.json
//   releases/<release>/<build>/...
//
// Every reader finds the layout this way: a repository with releases.json is
// read from it and its build folders only; a repository without one is read
// from its tags and pointer files (`latest*.json`). No country registration
// names the layout, so a repository's switch needs no dashboard change.
//
// The record's shape and rules are microcosm-emitter's
// `microcosm_emitter_interface.releases` (microcosm-emitter#24), whose release
// writer is the only program that changes the file. This module reads the
// record with the same rules, so a record the writer would refuse is refused
// here too, with one exception: JSON.parse reads `1.0` as the number 1, so a
// `schema_version` of `1.0` passes here although the writer, which requires
// an integer, refuses it. The writer itself always writes `1`. Isomorphic:
// the release picker and the tree publisher share it.

export const RELEASES_RECORD_PATH = "releases.json";
export const RELEASES_RECORD_SCHEMA_VERSION = 1;
export const RELEASE_MANIFEST_NAME = "release_manifest.json";
// The interface's MAX_RECORD_BYTES: the largest releases.json the writer's
// parser accepts, and the size its checks keep every record within.
export const MAX_RELEASES_RECORD_BYTES = 4 * 1024 * 1024;

// The interface's SAFE_IDENTIFIER and ROLE_IDENTIFIER.
const SAFE_IDENTIFIER_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const ROLE_IDENTIFIER_RE = /^[a-z][a-z0-9_]{0,63}$/;
const RECORD_KEYS = ["default", "releases", "schema_version"];
const RELEASE_KEYS = ["builds", "current", "role"];
const BUILD_KEYS = ["published_at", "withdrawn"];

export interface RecordedBuildEntry {
  /** As the writer recorded it; not reformatted. */
  publishedAt: string;
  withdrawn: boolean;
}

export interface RecordedRelease {
  role: string;
  /** Null while the release has no current build. */
  current: string | null;
  builds: Record<string, RecordedBuildEntry>;
}

export interface ReleasesRecord {
  /** The release a repository's default dataset comes from, or null. */
  default: string | null;
  releases: Record<string, RecordedRelease>;
}

/** One listed build, with what the record says about it and its release. */
export interface RecordedBuild {
  release: string;
  build: string;
  /** The build's folder under `releases/`: `<release>/<build>`. */
  id: string;
  role: string;
  publishedAt: string;
  withdrawn: boolean;
  /** The build is its release's current build. */
  current: boolean;
  /** The build's release is the record's default release. */
  inDefaultRelease: boolean;
}

export class ReleasesRecordError extends Error {}

function isSafeIdentifier(value: unknown): value is string {
  return typeof value === "string" && SAFE_IDENTIFIER_RE.test(value);
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value);
}

function hasExactly(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const present = Object.keys(value).sort();
  return present.length === keys.length && present.every((key, index) => key === keys[index]);
}

function refuse(message: string): never {
  throw new ReleasesRecordError(message);
}

/**
 * Parse `releases.json` and check it as the writer does: its exact shape,
 * safe release and build names, and (`check_record`) that each `current`
 * names a listed build that is not withdrawn and that `default` names a
 * listed release.
 */
export function parseReleasesRecord(document: unknown): ReleasesRecord {
  if (!isPlainObject(document) || !hasExactly(document, RECORD_KEYS)) {
    refuse(`${RELEASES_RECORD_PATH} must have exactly ${RECORD_KEYS.join(", ")}.`);
  }
  if (document.schema_version !== RELEASES_RECORD_SCHEMA_VERSION) {
    refuse(`${RELEASES_RECORD_PATH} schema_version must be ${RELEASES_RECORD_SCHEMA_VERSION}.`);
  }
  if (!isPlainObject(document.releases)) refuse("releases must be an object.");
  const releases: Record<string, RecordedRelease> = {};
  for (const [release, entry] of Object.entries(document.releases)) {
    if (!isSafeIdentifier(release)) refuse("A release name is not a safe identifier.");
    if (!isPlainObject(entry) || !hasExactly(entry, RELEASE_KEYS)) {
      refuse(`Release ${release} must have ${RELEASE_KEYS.join(", ")}.`);
    }
    if (typeof entry.role !== "string" || !ROLE_IDENTIFIER_RE.test(entry.role)) {
      refuse(`Release ${release} has an invalid role.`);
    }
    if (entry.current !== null && typeof entry.current !== "string") {
      refuse(`Release ${release} current must be a build or null.`);
    }
    if (!isPlainObject(entry.builds)) refuse(`Release ${release} builds must be an object.`);
    const builds: Record<string, RecordedBuildEntry> = {};
    for (const [build, buildEntry] of Object.entries(entry.builds)) {
      if (!isSafeIdentifier(build)) refuse("A build name is not a safe identifier.");
      if (
        !isPlainObject(buildEntry) ||
        !hasExactly(buildEntry, BUILD_KEYS) ||
        typeof buildEntry.published_at !== "string" ||
        !buildEntry.published_at ||
        typeof buildEntry.withdrawn !== "boolean"
      ) {
        refuse(`Build ${build} of ${release} is malformed.`);
      }
      builds[build] = {
        publishedAt: buildEntry.published_at,
        withdrawn: buildEntry.withdrawn,
      };
    }
    releases[release] = { role: entry.role, current: entry.current, builds };
  }
  if (document.default !== null && typeof document.default !== "string") {
    refuse("default must be a release name or null.");
  }
  const record: ReleasesRecord = { default: document.default, releases };
  if (record.default != null && !Object.hasOwn(releases, record.default)) {
    refuse(`Default ${record.default} is not listed.`);
  }
  for (const [release, entry] of Object.entries(releases)) {
    if (entry.current == null) continue;
    // Only the listed builds: `builds` is an ordinary object, so a name such
    // as "constructor" would otherwise find an inherited property.
    const current = Object.hasOwn(entry.builds, entry.current)
      ? entry.builds[entry.current]
      : undefined;
    if (!current) refuse(`Current build ${entry.current} of ${release} is not listed.`);
    if (current.withdrawn) refuse(`Current build ${entry.current} of ${release} is withdrawn.`);
  }
  return record;
}

/**
 * Read `releases.json` from its bytes as the writer's `ReleasesRecord.parse`
 * does: refuse a record larger than MAX_RELEASES_RECORD_BYTES or one that is
 * not a readable JSON document, then check it with parseReleasesRecord.
 */
export function parseReleasesRecordBytes(bytes: Uint8Array): ReleasesRecord {
  if (bytes.byteLength > MAX_RELEASES_RECORD_BYTES) {
    refuse(`${RELEASES_RECORD_PATH} is larger than ${MAX_RELEASES_RECORD_BYTES} bytes.`);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    refuse(`${RELEASES_RECORD_PATH} is not a readable JSON document.`);
  }
  return parseReleasesRecord(parsed);
}

// Hugging Face's error codes for a 404 that is not about the file: the
// repository or the revision is missing (or unreadable with this token).
const MISSING_REPOSITORY_OR_REVISION = new Set(["RepoNotFound", "RevisionNotFound"]);

/**
 * Read Hugging Face's answer to a request for releases.json: the record,
 * checked as parseReleasesRecordBytes checks it, or null when the repository
 * has no releases.json at that revision. Only a 404 means "no record", and
 * only when Hugging Face's error code (`X-Error-Code`) does not say that the
 * repository or the revision is missing. Any other failure throws, so a
 * reader never falls back to tags or pointer files because of it.
 */
export async function releasesRecordFromResponse(
  response: Response,
): Promise<ReleasesRecord | null> {
  const errorCode = response.headers.get("x-error-code");
  if (response.status === 404 && (errorCode == null || !MISSING_REPOSITORY_OR_REVISION.has(errorCode))) {
    return null;
  }
  if (!response.ok) {
    throw new Error(
      `Hugging Face returned ${response.status}${errorCode ? ` (${errorCode})` : ""} ` +
        `for ${RELEASES_RECORD_PATH}.`,
    );
  }
  return parseReleasesRecordBytes(new Uint8Array(await response.arrayBuffer()));
}

/** A build's folder under `releases/`, which identifies it: `<release>/<build>`. */
export function buildFolderId(release: string, build: string): string {
  if (!isSafeIdentifier(release) || !isSafeIdentifier(build)) {
    throw new ReleasesRecordError("A release and a build must be safe identifiers.");
  }
  return `${release}/${build}`;
}

export function parseBuildFolderId(
  value: unknown,
): { release: string; build: string } | null {
  if (typeof value !== "string") return null;
  const segments = value.split("/");
  if (segments.length !== 2 || !segments.every(isSafeIdentifier)) return null;
  return { release: segments[0], build: segments[1] };
}

export function isBuildFolderId(value: unknown): value is string {
  return parseBuildFolderId(value) != null;
}

/**
 * A folder under `releases/` that release files are read from: a release
 * folder (`<release id>`) or a build folder (`<release>/<build>`). Folders
 * are interpolated into Hugging Face URLs that carry the server's token, so
 * each segment passes the release-id allowlist and none can leave `releases/`.
 */
export function isReleaseDirectory(value: unknown): value is string {
  return isSafeIdentifier(value) || isBuildFolderId(value);
}

/** Every listed build, ordered by release, then build, as the writer sorts them. */
export function recordedBuilds(record: ReleasesRecord): RecordedBuild[] {
  const byName = (left: string, right: string) => (left < right ? -1 : left > right ? 1 : 0);
  return Object.keys(record.releases).sort(byName).flatMap((release) => {
    const entry = record.releases[release];
    return Object.keys(entry.builds).sort(byName).map((build) => ({
      release,
      build,
      id: buildFolderId(release, build),
      role: entry.role,
      publishedAt: entry.builds[build].publishedAt,
      withdrawn: entry.builds[build].withdrawn,
      current: entry.current === build,
      inDefaultRelease: record.default === release,
    }));
  });
}

/**
 * The repository's current build: the current build of its default release,
 * or null while the record names no default release or that release has no
 * current build.
 */
export function currentDefaultBuild(
  record: ReleasesRecord,
): { release: string; build: string; id: string } | null {
  const release = record.default;
  const build = release == null ? null : record.releases[release]?.current ?? null;
  if (release == null || build == null) return null;
  return { release, build, id: buildFolderId(release, build) };
}

/**
 * Whether a build folder's release manifest names that release and build:
 * the writer's `check_repository` rule, read from the manifest's top-level
 * `release` and `build`. Manifests written before the build folders keep
 * `build` as an object whose `build_id` is the release, and never match.
 */
export function releaseManifestNamesBuild(
  manifest: unknown,
  release: string,
  build: string,
): boolean {
  return isPlainObject(manifest) && manifest.release === release && manifest.build === build;
}
