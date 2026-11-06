import {
  CountryNotConfiguredError,
  isCountry,
  type MicrocosmCountry,
} from "./countries";
import {
  CALIBRATION_TREE_SCHEMA_VERSION,
  isHfCommitSha,
  isSha256,
  type CalibrationTreeBuildKind,
} from "./calibration-tree-artifact";
import { isReleaseDirectory } from "./release-cut";

export const CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION = 6 as const;
export const CALIBRATION_TREE_MANIFEST_PATH = "calibration-trees/manifest.json";

export interface CalibrationTreeManifestEntry {
  buildArtifactId: string;
  kind: Exclude<CalibrationTreeBuildKind, "comparison">;
  sourceId: string;
  label: string;
  releaseId: string | null;
  /**
   * The `releases/<folder>/` holding the build's files, recorded only when it
   * differs from releaseId: a Microcosm cut is released as its tag but read
   * from its line's folder (microcosm#823). Absent means the folder is
   * releaseId.
   */
  releaseDirectory?: string;
  stagingRunId: string | null;
  hfRepo: string;
  hfCommitSha: string;
  treeSchemaVersion: typeof CALIBRATION_TREE_SCHEMA_VERSION;
  indexSha256: string;
  indexBytes: number;
  createdAt: string | null;
  /** Null when the publisher could not date the build; never a placeholder. */
  updatedAt: string | null;
}

export interface CalibrationTreeCountryManifest {
  latestReleaseBuildArtifactId: string | null;
  builds: CalibrationTreeManifestEntry[];
}

export interface CalibrationTreeManifest {
  schemaVersion: typeof CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION;
  countries: Partial<Record<MicrocosmCountry, CalibrationTreeCountryManifest>>;
}

export function emptyCalibrationTreeManifest(): CalibrationTreeManifest {
  return {
    schemaVersion: CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION,
    countries: {},
  };
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function optionalDate(value: unknown, label: string): string | null {
  if (value == null) return null;
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) {
    throw new Error(`${label} must be null or an RFC 3339 date-time.`);
  }
  return value;
}

function optionalIdentifier(value: unknown, label: string): string | null {
  if (value == null) return null;
  if (typeof value !== "string" || !value) {
    throw new Error(`${label} must be null or a non-empty string.`);
  }
  return value;
}

function parseEntry(value: unknown, country: string): CalibrationTreeManifestEntry {
  const entry = record(value, `Calibration tree manifest entry ${country}`);
  if (
    typeof entry.buildArtifactId !== "string" ||
    !isSha256(entry.buildArtifactId)
  ) {
    throw new Error(`Calibration tree manifest entry ${country} has an invalid build id.`);
  }
  if (entry.kind !== "release" && entry.kind !== "staging") {
    throw new Error(`Calibration tree manifest entry ${country} has an invalid kind.`);
  }
  for (const key of ["sourceId", "label", "hfRepo"] as const) {
    if (typeof entry[key] !== "string" || !entry[key]) {
      throw new Error(`Calibration tree manifest entry ${country} has no ${key}.`);
    }
  }
  if (
    typeof entry.hfCommitSha !== "string" ||
    !isHfCommitSha(entry.hfCommitSha)
  ) {
    throw new Error(`Calibration tree manifest entry ${country} has an invalid HF commit SHA.`);
  }
  const releaseId = optionalIdentifier(entry.releaseId, `${country}.releaseId`);
  const stagingRunId = optionalIdentifier(
    entry.stagingRunId,
    `${country}.stagingRunId`,
  );
  if (
    (entry.kind === "release" && (!releaseId || stagingRunId)) ||
    (entry.kind === "staging" && (!stagingRunId || releaseId))
  ) {
    throw new Error(`Calibration tree manifest entry ${country} has inconsistent aliases.`);
  }
  // The folder is interpolated into authenticated Hugging Face URLs.
  if (entry.releaseDirectory != null && !isReleaseDirectory(entry.releaseDirectory)) {
    throw new Error(`Calibration tree manifest entry ${country} has an invalid release directory.`);
  }
  if (entry.treeSchemaVersion !== CALIBRATION_TREE_SCHEMA_VERSION) {
    throw new Error(`Calibration tree manifest entry ${country} has an unsupported tree schema.`);
  }
  if (typeof entry.indexSha256 !== "string" || !isSha256(entry.indexSha256)) {
    throw new Error(`Calibration tree manifest entry ${country} has an invalid index hash.`);
  }
  if (!Number.isSafeInteger(entry.indexBytes) || (entry.indexBytes as number) <= 0) {
    throw new Error(`Calibration tree manifest entry ${country} has an invalid index byte count.`);
  }
  optionalDate(entry.createdAt, `${country}.createdAt`);
  optionalDate(entry.updatedAt, `${country}.updatedAt`);
  return entry as unknown as CalibrationTreeManifestEntry;
}

function parseCountryManifest(
  value: unknown,
  country: MicrocosmCountry,
): CalibrationTreeCountryManifest {
  const parsed = record(value, `Calibration tree manifest country ${country}`);
  if (!Array.isArray(parsed.builds)) {
    throw new Error(`Calibration tree manifest country ${country} has no build list.`);
  }
  const builds = parsed.builds.map((entry) => parseEntry(entry, country));
  const ids = new Set(builds.map((entry) => entry.buildArtifactId));
  if (ids.size !== builds.length) {
    throw new Error(`Calibration tree manifest country ${country} repeats a build id.`);
  }
  const aliases = builds.map((entry) =>
    entry.kind === "release"
      ? `release:${entry.releaseId}`
      : `staging:${entry.stagingRunId}`
  );
  if (new Set(aliases).size !== aliases.length) {
    throw new Error(`Calibration tree manifest country ${country} repeats a source alias.`);
  }
  const latest = optionalIdentifier(
    parsed.latestReleaseBuildArtifactId,
    `${country}.latestReleaseBuildArtifactId`,
  );
  if (
    latest &&
    !builds.some(
      (entry) => entry.buildArtifactId === latest && entry.kind === "release",
    )
  ) {
    throw new Error(`Calibration tree manifest country ${country} has an invalid latest build.`);
  }
  return { latestReleaseBuildArtifactId: latest, builds };
}

export function parseCalibrationTreeManifest(value: unknown): CalibrationTreeManifest {
  const manifest = record(value, "Calibration tree manifest");
  if (manifest.schemaVersion !== CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION) {
    throw new Error(
      `Unsupported calibration tree manifest schema ${String(manifest.schemaVersion)}.`,
    );
  }
  const countries = record(manifest.countries, "Calibration tree manifest countries");
  const parsedCountries: CalibrationTreeManifest["countries"] = {};
  for (const [country, entry] of Object.entries(countries)) {
    if (!isCountry(country)) {
      throw new Error(`Calibration tree manifest contains unknown country ${country}.`);
    }
    parsedCountries[country] = parseCountryManifest(entry, country);
  }
  return {
    schemaVersion: CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION,
    countries: parsedCountries,
  };
}

export function serializeCalibrationTreeManifest(
  manifest: CalibrationTreeManifest,
): string {
  const parsed = parseCalibrationTreeManifest(manifest);
  const countries = Object.fromEntries(
    Object.entries(parsed.countries)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([country, entry]) => [
        country,
        {
          ...entry,
          builds: [...entry.builds].sort((left, right) =>
            left.buildArtifactId.localeCompare(right.buildArtifactId),
          ),
        },
      ]),
  );
  return `${JSON.stringify({ ...parsed, countries })}\n`;
}

export function calibrationTreeCountryManifest(
  manifest: CalibrationTreeManifest,
  country: MicrocosmCountry,
): CalibrationTreeCountryManifest {
  const entry = manifest.countries[country];
  // A registered country the publisher has not written yet: an honest
  // not-configured state, never another country's builds.
  if (!entry) throw new CountryNotConfiguredError(country, "no_published_release");
  return entry;
}

/** The `releases/` folder a release build's files are read from. */
export function releaseDirectoryOf(
  entry: Pick<CalibrationTreeManifestEntry, "releaseId" | "releaseDirectory">,
): string | null {
  return entry.releaseDirectory ?? entry.releaseId;
}

export function calibrationTreeManifestEntry(
  manifest: CalibrationTreeManifest,
  country: MicrocosmCountry,
  selector: { buildArtifactId?: string; releaseId?: string; stagingRunId?: string },
): CalibrationTreeManifestEntry {
  const countryManifest = calibrationTreeCountryManifest(manifest, country);
  const wantsLatest = !selector.buildArtifactId && (
    selector.releaseId === "latest" ||
      (!selector.releaseId && !selector.stagingRunId)
  );
  // Builds (staging runs, say) without a published release: the country has
  // no current release to serve yet.
  if (wantsLatest && !countryManifest.latestReleaseBuildArtifactId) {
    throw new CountryNotConfiguredError(country, "no_published_release");
  }
  const buildArtifactId = selector.buildArtifactId ?? (
    wantsLatest ? countryManifest.latestReleaseBuildArtifactId ?? undefined : undefined
  );
  const result = countryManifest.builds.find((entry) =>
    buildArtifactId
      ? entry.buildArtifactId === buildArtifactId
      : selector.releaseId
        ? entry.releaseId === selector.releaseId
        : entry.stagingRunId === selector.stagingRunId
  );
  if (!result) throw new Error(`No matching calibration build exists for ${country}.`);
  return result;
}

export function withCalibrationTreeManifestEntry(
  manifest: CalibrationTreeManifest,
  country: MicrocosmCountry,
  entry: CalibrationTreeManifestEntry,
  makeLatest = false,
): CalibrationTreeManifest {
  parseEntry(entry, country);
  const current = manifest.countries[country] ?? {
    latestReleaseBuildArtifactId: null,
    builds: [],
  };
  const previous = current.builds.find(
    (build) => build.buildArtifactId === entry.buildArtifactId,
  );
  const aliasConflict = current.builds.find((build) =>
    build.buildArtifactId !== entry.buildArtifactId &&
    build.kind === entry.kind &&
    (entry.kind === "release"
      ? build.releaseId === entry.releaseId
      : build.stagingRunId === entry.stagingRunId)
  );
  if (aliasConflict) {
    throw new Error(
      `Calibration source ${entry.sourceId} already resolves to build ${aliasConflict.buildArtifactId}.`,
    );
  }
  if (previous) {
    const immutableFields: Array<keyof CalibrationTreeManifestEntry> = [
      "buildArtifactId",
      "kind",
      "sourceId",
      "releaseId",
      "releaseDirectory",
      "stagingRunId",
      "hfRepo",
      "hfCommitSha",
      "treeSchemaVersion",
      "indexSha256",
      "indexBytes",
    ];
    if (immutableFields.some((field) => previous[field] !== entry[field])) {
      throw new Error(`Calibration build ${entry.buildArtifactId} is immutable.`);
    }
  }
  // A build's content identity is immutable; its metadata (label and the
  // publication timestamps) is replaced, so a reconciliation can correct a
  // stored timestamp without re-publishing the bundle.
  const builds = previous
    ? current.builds.map((build) =>
        build.buildArtifactId === entry.buildArtifactId ? entry : build,
      )
    : [...current.builds, entry];
  const latestReleaseBuildArtifactId = makeLatest
    ? entry.buildArtifactId
    : current.latestReleaseBuildArtifactId;
  if (makeLatest && entry.kind !== "release") {
    throw new Error("Only a release build can become the latest release.");
  }
  return {
    schemaVersion: CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION,
    countries: {
      ...manifest.countries,
      [country]: { latestReleaseBuildArtifactId, builds },
    },
  };
}

interface TimestampedCalibrationBuild {
  buildArtifactId: string;
  createdAt: string | null;
  updatedAt: string | null;
}

// When a build was published: createdAt, else updatedAt. An undated build
// sorts last rather than wherever NaN lands.
function buildPublishedInstant(build: TimestampedCalibrationBuild): number {
  const value = build.createdAt ?? build.updatedAt;
  const time = value == null ? Number.NaN : Date.parse(value);
  return Number.isFinite(time) ? time : Number.NEGATIVE_INFINITY;
}

/**
 * Build records newest first, by their actual RFC 3339 instant rather than
 * timestamp text; builds published at the same instant are ordered by build
 * ID.
 */
export function calibrationBuildsNewestFirst<
  T extends TimestampedCalibrationBuild,
>(builds: readonly T[]): T[] {
  return [...builds].sort((left, right) =>
    buildPublishedInstant(right) - buildPublishedInstant(left) ||
    left.buildArtifactId.localeCompare(right.buildArtifactId)
  );
}
