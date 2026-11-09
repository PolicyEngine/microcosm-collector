import {
  CALIBRATION_TREE_SCHEMA_VERSION,
  type CalibrationTreeSourceArtifact,
} from "./calibration-tree-artifact";
import {
  buildCalibrationTreeBundle,
  type CalibrationTreeBundle,
  type CalibrationTreeBundleFile,
} from "./calibration-tree-bundle";
import {
  auditCalibrationTreeBuild,
  listCalibrationTreeBlobs,
  readCalibrationTreeManifest,
  updateCalibrationTreeManifest,
  uploadCalibrationTreeBundle,
  type CalibrationTreeBuildAudit,
} from "./calibration-tree-blob";
import type { ListBlobResultBlob } from "@vercel/blob";
import {
  releaseDirectoryOf,
  type CalibrationTreeManifest,
  type CalibrationTreeManifestEntry,
} from "./calibration-tree-manifest";
import { createHash } from "node:crypto";
import {
  CalibrationReleaseNotFoundError,
  isReleaseId,
  resolveHfBuildFolderSha,
  resolveHfCommitPublishedAt,
  resolveHfReleaseDirectorySha,
  resolveHfRepositoryCommitPublishedAt,
  resolveHfRevisionSha,
} from "./calibration-release-locator";
import {
  countryPointerPath,
  countryRegistration,
  isCountry,
  selectableCountries,
  type MicrocosmCountry,
} from "./countries";
import { parseReleaseCutTag } from "./release-cut";
import {
  RELEASE_MANIFEST_NAME,
  RELEASES_RECORD_PATH,
  currentDefaultBuild,
  isBuildFolderId,
  isReleaseDirectory,
  recordedBuilds,
  releaseManifestNamesBuild,
  releasesRecordFromResponse,
  type RecordedBuild,
  type ReleasesRecord,
} from "./release-record";
import {
  buildCalibration,
  loadReleases,
  microcosmRepo,
  microcosmRevision,
} from "./latest-artifact";
import {
  loadStagingRuns,
  resolveStagingCalibrationSource,
  resolveStagingRevisionSha,
  stagingRepository,
} from "./staging-artifact";

type JsonObject = Record<string, unknown>;

export interface PublisherOptions {
  country: MicrocosmCountry;
  mode:
    | "latest"
    | "release"
    | "backfill"
    | "staging-finalized"
    | "reconcile-releases"
    | "reconcile-staging"
    | "migrate-release-timestamps";
  releaseId?: string;
  hfCommitSha?: string;
  dryRun?: boolean;
}

interface UpstreamLatest {
  releaseId: string;
  /** The `releases/` folder holding its files: releaseId unless a cut tag. */
  releaseDirectory: string;
  hfCommitSha: string;
  updatedAt: string | null;
}

interface FetchedArtifact {
  json: JsonObject;
  source: CalibrationTreeSourceArtifact;
}

const DEFAULT_MAX_RAW_BYTES = 100_000_000;
const FINAL_STAGING_STATUSES = new Set(["passed", "published", "completed"]);

export class CalibrationTreeArtifactSizeError extends Error {}

export interface ReconciliationOutcome {
  sourceId: string;
  // `failed`: the source could not be published or repaired; `reason` gives
  // the error. A failed release gets no new manifest entry unless the
  // manifest write was stored and only its read-back check failed; an entry
  // it already had is left as it was, and files uploaded before the error
  // stay under its build ID. A failed staging run can already have its
  // entry, because staging writes the entry before it audits the upload.
  status: "complete" | "published" | "repaired" | "ineligible" | "failed";
  buildArtifactId: string | null;
  reason: string | null;
}

export interface ReconciliationReport {
  country: MicrocosmCountry;
  sourceKind: "releases" | "staging";
  dryRun: boolean;
  outcomes: ReconciliationOutcome[];
  /**
   * Release reconciliation only: why the latest-release selection could not
   * be read or updated. Absent when it was updated. In a dry run, present
   * only when the upstream pointer cannot be read or names a release that
   * failed in this run.
   */
  latestReleaseFailure?: string;
}

function sha256Text(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function object(value: unknown, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object.`);
  }
  return value as JsonObject;
}

function releaseId(value: string | undefined): string {
  const result = value?.trim() ?? "";
  if (!isReleaseId(result)) {
    throw new Error("A valid immutable release id is required.");
  }
  return result;
}

// A release id (a release folder or a tag) or a build folder,
// `<release>/<build>`. Both are interpolated into authenticated Hugging Face
// URLs.
function releaseOrBuildFolderId(value: string | undefined): string {
  const result = value?.trim() ?? "";
  if (!isReleaseDirectory(result) || result === "latest") {
    throw new Error("A valid immutable release id is required.");
  }
  return result;
}

function exactCommitSha(value: string | undefined, label: string): string {
  const result = value?.trim().toLowerCase() ?? "";
  if (!/^[0-9a-f]{40,64}$/.test(result)) {
    throw new Error(`${label} must be an exact Hugging Face commit SHA.`);
  }
  return result;
}

function hfHeaders(): HeadersInit | undefined {
  const token = process.env.HF_TOKEN ?? process.env.HUGGINGFACE_TOKEN;
  return token ? { Authorization: `Bearer ${token}` } : undefined;
}

function sourceUrl(
  country: MicrocosmCountry,
  revision: string,
  path: string,
): string {
  return `https://huggingface.co/datasets/${microcosmRepo(country)}/resolve/${revision}/${path}`;
}

function repoSourceUrl(repo: string, revision: string, path: string): string {
  return `https://huggingface.co/datasets/${repo}/resolve/${revision}/${path}`;
}

async function fetchJsonArtifactFromRepo(
  repo: string,
  revision: string,
  path: string,
  required: boolean,
): Promise<FetchedArtifact | null> {
  const response = await fetch(repoSourceUrl(repo, revision, path), {
    headers: hfHeaders(),
    cache: "no-store",
    signal: AbortSignal.timeout(120_000),
  });
  if (response.status === 404 && !required) return null;
  if (!response.ok) {
    throw new Error(`Hugging Face returned ${response.status} for ${path}.`);
  }
  const text = await response.text();
  return {
    json: object(JSON.parse(text), path),
    source: { path, sha256: sha256Text(text) },
  };
}

async function fetchJsonArtifact(
  country: MicrocosmCountry,
  revision: string,
  path: string,
  required: boolean,
): Promise<FetchedArtifact | null> {
  return fetchJsonArtifactFromRepo(
    microcosmRepo(country),
    revision,
    path,
    required,
  );
}

export function parsePublisherOptions(argv: string[]): PublisherOptions {
  const values = new Map<string, string>();
  const flags = new Set<string>();
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (
      argument === "--latest" ||
      argument === "--backfill" ||
      argument === "--staging-finalized" ||
      argument === "--reconcile-releases" ||
      argument === "--reconcile-staging" ||
      argument === "--migrate-release-timestamps" ||
      argument === "--dry-run"
    ) {
      flags.add(argument);
      continue;
    }
    if (["--country", "--release", "--sha"].includes(argument)) {
      const value = argv[index + 1];
      if (!value || value.startsWith("--")) {
        throw new Error(`${argument} requires a value.`);
      }
      values.set(argument, value);
      index += 1;
      continue;
    }
    throw new Error(`Unknown publisher argument ${argument}.`);
  }

  const countryValue = values.get("--country") ?? "us";
  if (!isCountry(countryValue) || !selectableCountries().includes(countryValue)) {
    throw new Error(`Unknown calibration country ${countryValue}.`);
  }
  const selectedModes = [
    flags.has("--latest"),
    flags.has("--backfill"),
    flags.has("--staging-finalized"),
    flags.has("--reconcile-releases"),
    flags.has("--reconcile-staging"),
    flags.has("--migrate-release-timestamps"),
    values.has("--release"),
  ].filter(Boolean).length;
  if (selectedModes !== 1) {
    throw new Error(
      "Specify exactly one publication mode.",
    );
  }
  const dryRun = flags.has("--dry-run") || undefined;
  if (
    dryRun &&
    (flags.has("--latest") || values.has("--release"))
  ) {
    throw new Error("--dry-run is supported only by reconciliation modes.");
  }
  if (
    values.has("--sha") &&
    (flags.has("--backfill") || flags.has("--reconcile-releases"))
  ) {
    throw new Error("--sha is not valid for release-history reconciliation.");
  }
  if (values.has("--sha") && flags.has("--migrate-release-timestamps")) {
    throw new Error(
      "--sha is not valid for the timestamp migration; it reads each entry's own commit.",
    );
  }
  if (flags.has("--migrate-release-timestamps")) {
    return { country: countryValue, mode: "migrate-release-timestamps", dryRun };
  }
  if (flags.has("--backfill")) {
    return { country: countryValue, mode: "backfill", dryRun };
  }
  if (flags.has("--staging-finalized")) {
    return {
      country: countryValue,
      mode: "staging-finalized",
      dryRun,
      ...(values.has("--sha")
        ? { hfCommitSha: exactCommitSha(values.get("--sha"), "--sha") }
        : {}),
    };
  }
  if (flags.has("--reconcile-releases")) {
    return {
      country: countryValue,
      mode: "reconcile-releases",
      dryRun,
    };
  }
  if (flags.has("--reconcile-staging")) {
    return {
      country: countryValue,
      mode: "reconcile-staging",
      dryRun,
      ...(values.has("--sha")
        ? { hfCommitSha: exactCommitSha(values.get("--sha"), "--sha") }
        : {}),
    };
  }
  if (flags.has("--latest")) {
    return {
      country: countryValue,
      mode: "latest",
      dryRun,
      ...(values.has("--sha")
        ? { hfCommitSha: exactCommitSha(values.get("--sha"), "--sha") }
        : {}),
    };
  }
  return {
    country: countryValue,
    mode: "release",
    releaseId: releaseOrBuildFolderId(values.get("--release")),
    dryRun,
    ...(values.has("--sha")
      ? { hfCommitSha: exactCommitSha(values.get("--sha"), "--sha") }
      : {}),
  };
}

/**
 * The country's current release, as its pointer file names it. A tag the
 * pointer names must pass the same check as in reconciliation: its line
 * folder's release_manifest.json names the tag as its cut. A tag that
 * `manifest` already pins at the same commit was checked when it was
 * published and is not probed again.
 */
export async function readUpstreamLatest(
  country: MicrocosmCountry,
  expectedSourceSha?: string,
  manifest?: CalibrationTreeManifest,
): Promise<UpstreamLatest> {
  const sourceSha = expectedSourceSha
    ? exactCommitSha(expectedSourceSha, "Source revision")
    : await resolveHfRevisionSha(country, microcosmRevision(country), 0);
  const path = countryPointerPath(country);
  const response = await fetch(
    sourceUrl(country, sourceSha, path),
    {
      headers: hfHeaders(),
      cache: "no-store",
      signal: AbortSignal.timeout(30_000),
    },
  );
  if (!response.ok) {
    throw new Error(`Hugging Face returned ${response.status} for ${path}.`);
  }
  const pointer = object(await response.json(), path);
  const id = releaseId(
    typeof pointer.release_id === "string" ? pointer.release_id : undefined,
  );
  const updatedAt =
    typeof pointer.updated_at === "string" && Number.isFinite(Date.parse(pointer.updated_at))
      ? pointer.updated_at
      : null;
  // A per-line pointer (microcosm#823) names the immutable cut tag the
  // line's current release is read at; the release id is the line folder.
  // A country registered with such a pointer file gets its checks whatever
  // the file contains, so a pointer that drops `revision` is refused rather
  // than read as a folder release.
  if (countryRegistration(country).pointer_path != null || pointer.revision !== undefined) {
    if (pointer.schema_version !== 1) {
      throw new Error(`${path} has unsupported schema_version ${String(pointer.schema_version)}.`);
    }
    if (typeof pointer.line !== "string" || `latest-${pointer.line}.json` !== path) {
      throw new Error(`${path} declares line ${String(pointer.line)}.`);
    }
    if (pointer.revision === undefined) {
      throw new Error(`${path} names no revision for ${id}.`);
    }
    const revision = releaseId(
      typeof pointer.revision === "string" ? pointer.revision : undefined,
    );
    if (parseReleaseCutTag(revision)?.releaseId !== id) {
      throw new Error(`${path} names revision ${revision}, which is not a cut of ${id}.`);
    }
    const commit = await resolveHfRevisionSha(country, revision, 0);
    const pinned = manifest
      ? manifestBuildForSource(manifest, country, "release", revision)
      : null;
    const directory = pinned && pinned.hfCommitSha === commit
      ? releaseDirectoryOf(pinned) ?? revision
      : await releaseDirectoryForTag(country, revision, commit);
    if (directory !== id) {
      throw new Error(
        `${path} names revision ${revision}, whose files are under releases/${directory}/, not releases/${id}/.`,
      );
    }
    return {
      releaseId: revision,
      releaseDirectory: id,
      hfCommitSha: commit,
      updatedAt,
    };
  }
  return {
    releaseId: id,
    releaseDirectory: id,
    hfCommitSha: await resolveHfRevisionSha(country, id, 0).catch((error) => {
      if (error instanceof CalibrationReleaseNotFoundError) return sourceSha;
      throw error;
    }),
    updatedAt,
  };
}

interface ReleaseCandidate {
  releaseId: string;
  /** The `releases/` folder holding its files: releaseId unless a cut tag. */
  releaseDirectory: string;
  hfCommitSha: string;
  /**
   * A build folder's publication time, as `releases.json` records it (null
   * when that cannot be read as a date-time). Absent for release folders and
   * tags, which are dated by their commit.
   */
  publishedAt?: string | null;
}

interface ReleaseTag {
  name: string;
  /** The commit the tag points at. */
  commit: string;
}

/**
 * The repository's release tags, each with the commit it points at, and the
 * error for each tag whose commit could not be read.
 *
 * `/refs` gives an annotated tag's `targetCommit` as the ID of the tag
 * object, not of its commit. Hugging Face serves files at that ID, but its
 * commit log read there starts at the tag's commit, so the build could not
 * be dated, and a build pinned to it would not match one pinned to the
 * commit. Microcosm makes an annotated tag whenever it publishes a release
 * with a tag message (microcosm#1301), so each tag's commit is read from
 * `/revision/<tag>`, which follows an annotated tag to its commit: one
 * request per tag, made concurrently. A failed lookup is returned rather
 * than thrown, so it fails only its own release.
 */
async function listReleaseTags(
  country: MicrocosmCountry,
): Promise<{ tags: ReleaseTag[]; unreadable: Map<string, string> }> {
  const response = await fetch(
    `https://huggingface.co/api/datasets/${microcosmRepo(country)}/refs`,
    {
      headers: hfHeaders(),
      cache: "no-store",
      signal: AbortSignal.timeout(30_000),
    },
  );
  if (!response.ok) {
    throw new Error(`Hugging Face returned ${response.status} while listing release tags.`);
  }
  const payload = object(await response.json(), "Hugging Face refs");
  if (!Array.isArray(payload.tags)) {
    throw new Error("Hugging Face refs response has no tag list.");
  }
  const names = payload.tags.flatMap((value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    const { name } = value as JsonObject;
    // A name the revision lookup refuses, such as `latest`, is never a release.
    return isReleaseId(name) ? [name] : [];
  });
  const lookups = await Promise.allSettled(
    names.map((name) => resolveHfRevisionSha(country, name, 0)),
  );
  const tags: ReleaseTag[] = [];
  const unreadable = new Map<string, string>();
  lookups.forEach((lookup, index) => {
    if (lookup.status === "fulfilled") {
      tags.push({ name: names[index], commit: lookup.value });
    } else {
      unreadable.set(
        names[index],
        lookup.reason instanceof Error ? lookup.reason.message : String(lookup.reason),
      );
    }
  });
  return { tags, unreadable };
}

async function releaseFolderHasDiagnostics(
  country: MicrocosmCountry,
  commit: string,
  directory: string,
): Promise<boolean> {
  const response = await fetch(
    `https://huggingface.co/api/datasets/${microcosmRepo(country)}/tree/` +
      `${commit}/releases/${encodeURIComponent(directory)}?recursive=false`,
    {
      headers: hfHeaders(),
      cache: "no-store",
      signal: AbortSignal.timeout(30_000),
    },
  );
  if (response.status === 404) return false;
  if (!response.ok) {
    throw new Error(
      `Hugging Face returned ${response.status} while inspecting releases/${directory} at ${commit}.`,
    );
  }
  const entries = await response.json();
  return Array.isArray(entries) && entries.some((value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    return (value as JsonObject).path ===
      `releases/${directory}/calibration_diagnostics.json`;
  });
}

type TaggedRelease =
  | { releaseDirectory: string; reason?: never }
  | { releaseDirectory?: never; reason: string };

/**
 * Where a tag's release files live. A tag named like its folder is a release
 * (`releases/<tag>/`). A Microcosm cut tag (microcosm#823) keeps its files
 * under its line's folder, `releases/<release id>/`, and counts only when
 * that folder's release_manifest.json names the tag as its cut, so a stray
 * tag can never publish another cut's files under its own name.
 */
async function resolveTaggedReleaseDirectory(
  country: MicrocosmCountry,
  tag: ReleaseTag,
): Promise<TaggedRelease> {
  if (await releaseFolderHasDiagnostics(country, tag.commit, tag.name)) {
    return { releaseDirectory: tag.name };
  }
  const cut = parseReleaseCutTag(tag.name);
  if (!cut) return { reason: "Release has no calibration diagnostics." };
  if (!await releaseFolderHasDiagnostics(country, tag.commit, cut.releaseId)) {
    return {
      reason: `Release cut has no calibration diagnostics under releases/${cut.releaseId}/.`,
    };
  }
  const releaseManifest = await fetchJsonArtifact(
    country,
    tag.commit,
    `releases/${cut.releaseId}/release_manifest.json`,
    false,
  );
  const build = releaseManifest?.json.build;
  const cutTag = build && typeof build === "object" && !Array.isArray(build)
    ? (build as JsonObject).cut_tag
    : undefined;
  if (cutTag !== tag.name) {
    return {
      reason: `releases/${cut.releaseId}/release_manifest.json at this tag does not name it as its cut.`,
    };
  }
  return { releaseDirectory: cut.releaseId };
}

async function enumerateReleaseCandidates(
  country: MicrocosmCountry,
  manifest: CalibrationTreeManifest,
): Promise<{
  candidates: ReleaseCandidate[];
  ineligible: ReconciliationOutcome[];
  /** Releases whose commit could not be read, from their tag or their folder. */
  failed: ReconciliationOutcome[];
}> {
  const [releases, { tags, unreadable }] = await Promise.all([
    loadReleases(0, country),
    listReleaseTags(country),
  ]);
  const tagsByName = new Map(tags.map((tag) => [tag.name, tag]));
  const calibratedFolders = new Set(
    releases
      .filter((release) => release.has_calibration)
      .map((release) => release.release_id),
  );
  const candidates = new Map<string, ReleaseCandidate>();
  const ineligible: ReconciliationOutcome[] = [];
  // A release whose commit cannot be read fails on its own and keeps any
  // build it already has, as a release that cannot be published does.
  const failed: ReconciliationOutcome[] = [];
  const reportFailure = (sourceId: string, reason: string) => failed.push({
    sourceId,
    status: "failed",
    buildArtifactId:
      manifestBuildForSource(manifest, country, "release", sourceId)?.buildArtifactId ?? null,
    reason,
  });
  for (const [name, reason] of unreadable) reportFailure(name, reason);

  // Tags are resolved first: a verified cut tag makes its line's folder on
  // main a mutable copy of whichever cut was published last.
  const taggedReleases = new Map<string, TaggedRelease>();
  for (const tag of tags) {
    if (calibratedFolders.has(tag.name)) continue;
    // A tag the manifest already pins at this commit keeps its folder; it is
    // not probed again on every reconciliation.
    const known = manifestBuildForSource(manifest, country, "release", tag.name);
    taggedReleases.set(
      tag.name,
      known && known.hfCommitSha === tag.commit
        ? { releaseDirectory: releaseDirectoryOf(known) ?? tag.name }
        : await resolveTaggedReleaseDirectory(country, tag),
    );
  }
  const lineFolders = new Set(
    [...taggedReleases].flatMap(([name, tagged]) =>
      tagged.releaseDirectory && tagged.releaseDirectory !== name
        ? [tagged.releaseDirectory]
        : []
    ),
  );
  // A tag named as one snapshot of a release series whose commit could not
  // be read may be the series' only such tag, so the series' folder is held
  // back in this run: it is not published from main as a release of its own.
  // Each held-back folder maps to the tags that hold it back.
  const heldBackFolders = new Map<string, string[]>();
  for (const name of unreadable.keys()) {
    const folder = parseReleaseCutTag(name)?.releaseId;
    if (folder) heldBackFolders.set(folder, [...(heldBackFolders.get(folder) ?? []), name]);
  }

  for (const release of releases) {
    // A folder whose tag's commit could not be read is reported as failed,
    // not pinned to another commit.
    if (!release.has_calibration || unreadable.has(release.release_id)) continue;
    const existing = manifestBuildForSource(
      manifest,
      country,
      "release",
      release.release_id,
    );
    const heldBackBy = heldBackFolders.get(release.release_id);
    if (lineFolders.has(release.release_id) || heldBackBy) {
      // The cut tags publish the line. A build already published from the
      // folder stays pinned at its own commit, so it audits and repairs as
      // before instead of following the folder to a newer cut.
      if (existing) {
        candidates.set(release.release_id, {
          releaseId: release.release_id,
          releaseDirectory: releaseDirectoryOf(existing) ?? release.release_id,
          hfCommitSha: existing.hfCommitSha,
        });
      } else {
        ineligible.push({
          sourceId: release.release_id,
          status: "ineligible",
          buildArtifactId: null,
          // A folder that only a failed lookup holds back is not known to
          // be a series' folder, so it gets that reason instead.
          reason: heldBackBy && !lineFolders.has(release.release_id)
            ? `Held back in this run because the commit of its release snapshot ` +
              `${heldBackBy.length === 1 ? "tag" : "tags"} ${heldBackBy.join(", ")} ` +
              "could not be read. The next run retries the lookup."
            : "A release line folder; its cut tags are published instead.",
        });
      }
      continue;
    }
    // The build pins the tag's commit, or the newest commit behind the
    // release directory when no tag exists. Its publication timestamp is
    // that commit's date, read by publishRelease when a bundle is built.
    let hfCommitSha = tagsByName.get(release.release_id)?.commit;
    if (hfCommitSha == null) {
      try {
        hfCommitSha = await resolveHfReleaseDirectorySha(country, release.release_id, 0);
      } catch (error) {
        reportFailure(release.release_id, error instanceof Error ? error.message : String(error));
        continue;
      }
    }
    candidates.set(release.release_id, {
      releaseId: release.release_id,
      releaseDirectory: release.release_id,
      hfCommitSha,
    });
  }
  for (const [name, tagged] of taggedReleases) {
    if (tagged.releaseDirectory) {
      candidates.set(name, {
        releaseId: name,
        releaseDirectory: tagged.releaseDirectory,
        hfCommitSha: tagsByName.get(name)!.commit,
      });
    } else {
      ineligible.push({
        sourceId: name,
        status: "ineligible",
        buildArtifactId: null,
        reason: tagged.reason ?? null,
      });
    }
  }
  for (const release of releases) {
    if (
      !release.has_calibration &&
      !unreadable.has(release.release_id) &&
      !ineligible.some((item) => item.sourceId === release.release_id)
    ) {
      ineligible.push({
        sourceId: release.release_id,
        status: "ineligible",
        buildArtifactId: null,
        reason: "Release has no calibration diagnostics.",
      });
    }
  }
  return {
    candidates: [...candidates.values()].sort((left, right) =>
      left.releaseId.localeCompare(right.releaseId)
    ),
    ineligible,
    failed,
  };
}

// -- Build folders and releases.json ----------------------------------------
//
// A dataset repository with a root `releases.json` (microcosm-emitter#24)
// publishes each build in `releases/<release>/<build>/`, never changed after
// publishing, and lists every build and each release's current build in that
// record. Its builds are read from the record alone: no tags, no pointer
// files, no listing of `releases/`. A repository without `releases.json` is
// read from its tags and pointer files, as before. Which applies is read
// from the repository at the commit being published, never from the
// country's registration. A build's release id in the dashboard manifest is
// its folder, `<release>/<build>`.

/**
 * `releases.json` at an exact commit, checked as the release writer checks
 * it, or null when the repository has none there. Hugging Face's 404 for the
 * file is the only "none"; any other failure, and a record the writer would
 * refuse, throws (releasesRecordFromResponse).
 */
async function readReleasesRecord(
  country: MicrocosmCountry,
  commitSha: string,
): Promise<ReleasesRecord | null> {
  const response = await fetch(sourceUrl(country, commitSha, RELEASES_RECORD_PATH), {
    headers: hfHeaders(),
    cache: "no-store",
    signal: AbortSignal.timeout(30_000),
  });
  return releasesRecordFromResponse(response);
}

function requireReleasesRecord(
  country: MicrocosmCountry,
  record: ReleasesRecord | null,
): ReleasesRecord {
  if (!record) {
    throw new Error(`${country}'s dataset repository has no ${RELEASES_RECORD_PATH}.`);
  }
  return record;
}

// The record's `published_at`, as the dashboard manifest's RFC 3339 form.
function recordedPublicationTime(publishedAt: string): string | null {
  const instant = Date.parse(publishedAt);
  return Number.isFinite(instant) ? new Date(instant).toISOString() : null;
}

/**
 * Where a listed build's files are: its folder at the commit that wrote it.
 * The build counts only when that folder holds calibration diagnostics and
 * its release manifest names the same release and build, so a folder can
 * never publish another build's files under its own name.
 */
async function resolveRecordedBuild(
  country: MicrocosmCountry,
  recorded: RecordedBuild,
  revisionSha: string,
): Promise<{ candidate: ReleaseCandidate; reason?: never } | { candidate?: never; reason: string }> {
  let commit: string;
  try {
    commit = await resolveHfBuildFolderSha(country, recorded.id, revisionSha, 0);
  } catch (error) {
    if (error instanceof CalibrationReleaseNotFoundError) {
      return { reason: `releases/${recorded.id}/ has no calibration diagnostics.` };
    }
    throw error;
  }
  const releaseManifest = await fetchJsonArtifact(
    country,
    commit,
    `releases/${recorded.id}/${RELEASE_MANIFEST_NAME}`,
    false,
  );
  if (!releaseManifestNamesBuild(releaseManifest?.json, recorded.release, recorded.build)) {
    return {
      reason:
        `releases/${recorded.id}/${RELEASE_MANIFEST_NAME} does not name release ` +
        `${recorded.release} and build ${recorded.build}.`,
    };
  }
  return {
    candidate: {
      releaseId: recorded.id,
      releaseDirectory: recorded.id,
      hfCommitSha: commit,
      publishedAt: recordedPublicationTime(recorded.publishedAt),
    },
  };
}

// A build the dashboard manifest already holds keeps the commit and date it
// was published with; its folder is not listed again.
function publishedRecordedBuild(
  manifest: CalibrationTreeManifest | undefined,
  country: MicrocosmCountry,
  id: string,
): ReleaseCandidate | null {
  const existing = manifest ? manifestBuildForSource(manifest, country, "release", id) : null;
  return existing
    ? {
        releaseId: id,
        releaseDirectory: id,
        hfCommitSha: existing.hfCommitSha,
        publishedAt: existing.createdAt,
      }
    : null;
}

async function enumerateRecordedBuildCandidates(
  country: MicrocosmCountry,
  manifest: CalibrationTreeManifest,
  record: ReleasesRecord,
  headSha: string,
): Promise<{
  candidates: ReleaseCandidate[];
  ineligible: ReconciliationOutcome[];
  failed: ReconciliationOutcome[];
}> {
  const candidates: ReleaseCandidate[] = [];
  const ineligible: ReconciliationOutcome[] = [];
  // A build whose folder or release manifest cannot be read fails on its
  // own, as a release whose commit cannot be read does; the other listed
  // builds are still reconciled.
  const failed: ReconciliationOutcome[] = [];
  for (const recorded of recordedBuilds(record)) {
    const published = publishedRecordedBuild(manifest, country, recorded.id);
    if (published) {
      candidates.push(published);
      continue;
    }
    let resolved: Awaited<ReturnType<typeof resolveRecordedBuild>>;
    try {
      resolved = await resolveRecordedBuild(country, recorded, headSha);
    } catch (error) {
      failed.push({
        sourceId: recorded.id,
        status: "failed",
        buildArtifactId: null,
        reason: error instanceof Error ? error.message : String(error),
      });
      continue;
    }
    if (resolved.candidate) {
      candidates.push(resolved.candidate);
    } else {
      ineligible.push({
        sourceId: recorded.id,
        status: "ineligible",
        buildArtifactId: null,
        reason: resolved.reason,
      });
    }
  }
  return { candidates, ineligible, failed };
}

async function selectRecordedBuild(
  country: MicrocosmCountry,
  record: ReleasesRecord,
  id: string,
  revisionSha: string,
  manifest?: CalibrationTreeManifest,
): Promise<Required<ReleaseCandidate>> {
  const recorded = recordedBuilds(record).find((build) => build.id === id);
  if (!recorded) throw new Error(`${RELEASES_RECORD_PATH} does not list build ${id}.`);
  let selected = publishedRecordedBuild(manifest, country, id);
  if (!selected) {
    const resolved = await resolveRecordedBuild(country, recorded, revisionSha);
    if (!resolved.candidate) {
      throw new Error(`Release ${id} cannot be published: ${resolved.reason}`);
    }
    selected = resolved.candidate;
  }
  return { ...selected, publishedAt: selected.publishedAt ?? null };
}

function requireCurrentDefaultBuild(record: ReleasesRecord) {
  const current = currentDefaultBuild(record);
  if (!current) {
    throw new Error(`${RELEASES_RECORD_PATH} names no current build of its default release.`);
  }
  return current;
}

/**
 * The country's current build, as `releases.json` at `expectedSourceSha`
 * (or at the head of the configured revision) names it: the current build
 * of the record's default release, pinned to the commit that wrote its
 * folder. A build `manifest` already holds keeps its published commit.
 */
export async function readRecordedCurrentBuild(
  country: MicrocosmCountry,
  expectedSourceSha?: string,
  manifest?: CalibrationTreeManifest,
): Promise<Required<ReleaseCandidate>> {
  const revisionSha = await publicationRevisionSha(country, expectedSourceSha);
  const record = requireReleasesRecord(country, await readReleasesRecord(country, revisionSha));
  return recordedCurrentBuild(country, record, revisionSha, manifest);
}

function recordedCurrentBuild(
  country: MicrocosmCountry,
  record: ReleasesRecord,
  revisionSha: string,
  manifest?: CalibrationTreeManifest,
): Promise<Required<ReleaseCandidate>> {
  return selectRecordedBuild(
    country,
    record,
    requireCurrentDefaultBuild(record).id,
    revisionSha,
    manifest,
  );
}

// The exact commit a publication reads: the one given, or the head of the
// country's configured revision.
async function publicationRevisionSha(
  country: MicrocosmCountry,
  expectedSourceSha?: string,
): Promise<string> {
  return expectedSourceSha
    ? exactCommitSha(expectedSourceSha, "Source revision")
    : resolveHfRevisionSha(country, microcosmRevision(country), 0);
}

/** The folder a tag publishes from, for the single-release modes. */
async function releaseDirectoryForTag(
  country: MicrocosmCountry,
  id: string,
  commit: string,
): Promise<string> {
  const tagged = await resolveTaggedReleaseDirectory(country, {
    name: id,
    commit,
  });
  if (!tagged.releaseDirectory) {
    throw new Error(`Release ${id} cannot be published: ${tagged.reason}`);
  }
  return tagged.releaseDirectory;
}

export function manifestBuildForSource(
  manifest: CalibrationTreeManifest,
  country: MicrocosmCountry,
  kind: "release" | "staging",
  sourceId: string,
): CalibrationTreeManifestEntry | null {
  const matches = (manifest.countries[country]?.builds ?? []).filter((entry) =>
    entry.kind === kind &&
    (kind === "release"
      ? entry.releaseId === sourceId
      : entry.stagingRunId === sourceId)
  );
  if (matches.length > 1) {
    throw new Error(`Calibration source ${sourceId} has multiple manifest entries.`);
  }
  return matches[0] ?? null;
}

export function enforceConfiguredSizeLimits(file: CalibrationTreeBundleFile): {
  rawBytes: number;
  gzipBytes: number;
} {
  const { rawBytes, gzipBytes } = file;
  const limits = [
    ["CALIBRATION_TREE_MAX_RAW_BYTES", rawBytes, DEFAULT_MAX_RAW_BYTES],
    ["CALIBRATION_TREE_MAX_GZIP_BYTES", gzipBytes],
  ] as const;
  for (const [name, actual, defaultLimit] of limits) {
    const configured = process.env[name]?.trim();
    const limit = configured ? Number(configured) : defaultLimit;
    if (limit == null) continue;
    if (!Number.isSafeInteger(limit) || limit <= 0) {
      throw new Error(`${name} must be a positive integer.`);
    }
    if (actual > limit) {
      throw new CalibrationTreeArtifactSizeError(
        `${file.path} exceeded ${name}: ${actual} bytes is greater than ${limit}.`,
      );
    }
  }
  return { rawBytes, gzipBytes };
}

export async function resolvePublicationSourceSha(
  country: MicrocosmCountry,
  id: string,
  expectedSha: string | undefined,
  allowReleaseDirectoryLookup: boolean,
): Promise<string> {
  try {
    return await resolveHfRevisionSha(country, id, 0);
  } catch (error) {
    if (error instanceof CalibrationReleaseNotFoundError && expectedSha) {
      return exactCommitSha(expectedSha, "Fallback source revision");
    }
    if (
      error instanceof CalibrationReleaseNotFoundError &&
      allowReleaseDirectoryLookup
    ) {
      return resolveHfReleaseDirectorySha(country, id, 0);
    }
    throw error;
  }
}

interface PublishReleaseOptions {
  country: MicrocosmCountry;
  /** The build's release id: its folder name, or a Microcosm cut tag. */
  id: string;
  /** The `releases/` folder holding its files; defaults to id. */
  releaseDirectory?: string;
  expectedSha: string | undefined;
  blobToken: string;
  allowMissingReleaseTag?: boolean;
  useExpectedShaExactly?: boolean;
  buildArtifactId?: string;
  dryRun?: boolean;
  expectedIndexSha256?: string;
  indexCreatedAt?: string | null;
  uploadBundle: CalibrationManifestStore["uploadBundle"];
}

async function publishRelease(options: PublishReleaseOptions) {
  const {
    country,
    id,
    expectedSha,
    blobToken,
    allowMissingReleaseTag = false,
    useExpectedShaExactly = false,
    buildArtifactId,
    dryRun = false,
    expectedIndexSha256,
    indexCreatedAt,
    uploadBundle,
  } = options;
  // The folder is interpolated into authenticated Hugging Face URLs.
  const releaseDirectory = releaseOrBuildFolderId(options.releaseDirectory ?? id);
  const resolvedSha = useExpectedShaExactly && expectedSha
    ? exactCommitSha(expectedSha, "Exact source revision")
    : await resolvePublicationSourceSha(
        country,
        id,
        expectedSha,
        allowMissingReleaseTag,
      );
  if (expectedSha && expectedSha.toLowerCase() !== resolvedSha) {
    throw new Error(
      `Webhook commit ${expectedSha} does not match release ${id} commit ${resolvedSha}.`,
    );
  }
  // A new publication stamps the index with the resolved commit's date, the
  // one publication timestamp. A repair of an existing build reproduces the
  // immutable index instead, so it carries the stored entry's createdAt
  // (possibly null) and the rebuilt digest matches expectedIndexSha256.
  const publishedAt = indexCreatedAt === undefined
    ? await resolveHfCommitPublishedAt(country, resolvedSha, 0)
    : indexCreatedAt;
  const prefix = `releases/${releaseDirectory}`;
  const [diagnostics, buildManifest, releaseManifest, demographics] =
    await Promise.all([
      fetchJsonArtifact(
        country,
        resolvedSha,
        `${prefix}/calibration_diagnostics.json`,
        true,
      ),
      fetchJsonArtifact(country, resolvedSha, `${prefix}/build_manifest.json`, false),
      fetchJsonArtifact(country, resolvedSha, `${prefix}/release_manifest.json`, false),
      fetchJsonArtifact(country, resolvedSha, `${prefix}/demographics.json`, false),
    ]);
  if (!diagnostics) throw new Error("Calibration diagnostics are missing.");

  const calibration = buildCalibration(
    diagnostics.json,
    id,
    null,
    buildManifest?.json ?? {},
    releaseManifest?.json ?? {},
    demographics?.json ?? {},
    country,
    "huggingface_immutable",
    diagnostics.source.sha256,
    resolvedSha,
  );
  const bundle = buildCalibrationTreeBundle({
    country,
    ...(buildArtifactId ? { buildArtifactId } : {}),
    createdAt: publishedAt,
    releaseId: id,
    hfRepo: microcosmRepo(country),
    hfCommitSha: resolvedSha,
    sourceArtifacts: {
      calibrationDiagnostics: diagnostics.source,
      buildManifest: buildManifest?.source ?? null,
      releaseManifest: releaseManifest?.source ?? null,
      demographics: demographics?.source ?? null,
    },
    rows: calibration.rows,
    calibrationProvenance: calibration.calibration_provenance,
    lossAttributionAvailable:
      calibration.target_loss_attribution.status !== "unavailable",
    comparison: {
      // A tag's diagnostics name its folder, and a build folder's may name
      // its release; like the release reader, keep the tag or the build
      // folder so two builds of one release compare as different builds.
      // A folder release keeps the id its diagnostics carry, as before.
      releaseId: releaseDirectory === id && !isBuildFolderId(id)
        ? calibration.release_id
        : id,
      calibrationProvenance: calibration.calibration_provenance,
      status: calibration.target_loss_attribution.status,
      aggregate: calibration.target_loss_attribution.aggregate,
      cap: calibration.target_loss_attribution.cap,
      basisIdentifier: calibration.target_loss_attribution.basis_identifier,
      targetRepresentation: calibration.target_schema.target_representation,
    },
  });
  const sizes = Object.fromEntries(
    bundle.files.map((file) => [file.part, enforceConfiguredSizeLimits(file)]),
  );
  const builtIndex = bundle.files.find((candidate) => candidate.part === "index");
  if (!builtIndex) throw new Error("Calibration tree bundle has no index.");
  if (expectedIndexSha256 && builtIndex.sha256 !== expectedIndexSha256) {
    throw new Error(
      `Rebuilt calibration index differs from immutable manifest entry ${buildArtifactId}.`,
    );
  }
  const stored = dryRun
    ? {
        files: bundle.files.map((file) => ({
          pathname: file.path,
          sha256: file.sha256,
          bytes: file.rawBytes,
          created: false,
        })),
        index: (() => {
          return {
            pathname: builtIndex.path,
            sha256: builtIndex.sha256,
            bytes: builtIndex.rawBytes,
            created: false,
          };
        })(),
      }
    : await uploadBundle(bundle);
  return { bundle, stored, sizes, releaseDirectory };
}

async function promoteIfCurrent(
  country: MicrocosmCountry,
  candidate: Awaited<ReturnType<typeof publishRelease>>,
  entry: CalibrationTreeManifestEntry,
  store: CalibrationManifestStore,
): Promise<boolean> {
  const headSha = await publicationRevisionSha(country);
  const record = await readReleasesRecord(country, headSha);
  const published = candidate.bundle.index.build;
  let latest: { releaseId: string; hfCommitSha: string };
  if (record) {
    // Names first: another build's folder is never read for this check.
    const current = currentDefaultBuild(record);
    if (!current || current.id !== published.releaseId) return false;
    try {
      latest = await selectRecordedBuild(country, record, current.id, headSha);
    } catch (error) {
      // The build is registered; the next reconciliation moves the latest
      // release once the current build's folder can be read.
      console.warn(
        `The latest release was not moved to ${current.id}: ` +
          `${error instanceof Error ? error.message : String(error)}`,
      );
      return false;
    }
  } else {
    latest = await readUpstreamLatest(country, headSha);
  }
  if (latest.releaseId !== published.releaseId || latest.hfCommitSha !== published.hfCommitSha) {
    return false;
  }
  await store.updateManifest(entry, true);
  return true;
}

function releaseManifestEntry(
  published: Awaited<ReturnType<typeof publishRelease>>,
): CalibrationTreeManifestEntry {
  const build = published.bundle.index.build;
  if (!build.releaseId || !build.hfRepo || !build.hfCommitSha) {
    throw new Error("Published release build is missing release provenance.");
  }
  return {
    buildArtifactId: build.buildArtifactId,
    kind: "release",
    sourceId: build.sourceId,
    label: build.label,
    releaseId: build.releaseId,
    // Recorded only for a cut, so a folder-named entry stays as it was.
    ...(published.releaseDirectory !== build.releaseId
      ? { releaseDirectory: published.releaseDirectory }
      : {}),
    stagingRunId: null,
    hfRepo: build.hfRepo,
    hfCommitSha: build.hfCommitSha,
    treeSchemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
    indexSha256: published.stored.index.sha256,
    indexBytes: published.stored.index.bytes,
    createdAt: build.createdAt,
    updatedAt: build.createdAt,
  };
}

/** The manifest and bundle store the release modes read and write. */
export interface CalibrationManifestStore {
  readManifest(): Promise<CalibrationTreeManifest>;
  listBlobs(): Promise<Map<string, ListBlobResultBlob>>;
  audit(
    entry: CalibrationTreeManifestEntry,
    blobs: Map<string, ListBlobResultBlob>,
  ): Promise<CalibrationTreeBuildAudit>;
  updateManifest(
    entry: CalibrationTreeManifestEntry,
    makeLatest?: boolean,
  ): Promise<CalibrationTreeManifest>;
  /**
   * Uploads a built bundle. Required: the store is the only route by which
   * a publication writes to Blob, so a store in a test never falls back to
   * the real upload.
   */
  uploadBundle(
    bundle: CalibrationTreeBundle,
  ): ReturnType<typeof uploadCalibrationTreeBundle>;
}

function blobCalibrationManifestStore(
  country: MicrocosmCountry,
  blobToken: string,
): CalibrationManifestStore {
  return {
    uploadBundle: (bundle) => uploadCalibrationTreeBundle({ bundle, token: blobToken }),
    readManifest: async () =>
      (await readCalibrationTreeManifest({ token: blobToken, consistent: true })).manifest,
    listBlobs: () => listCalibrationTreeBlobs({ country, token: blobToken }),
    audit: (entry, blobs) =>
      auditCalibrationTreeBuild({ country, entry, blobs, token: blobToken }),
    updateManifest: (entry, makeLatest) =>
      updateCalibrationTreeManifest({
        country,
        entry,
        token: blobToken,
        ...(makeLatest ? { makeLatest } : {}),
      }),
  };
}

async function registerReleaseBuild(
  published: Awaited<ReturnType<typeof publishRelease>>,
  store: CalibrationManifestStore,
): Promise<CalibrationTreeManifestEntry> {
  const entry = releaseManifestEntry(published);
  await store.updateManifest(entry);
  return entry;
}

type ResolvedStagingSource = NonNullable<
  Awaited<ReturnType<typeof resolveStagingCalibrationSource>>
>;

async function publishStagingSource(options: {
  country: MicrocosmCountry;
  runId: string;
  source: ResolvedStagingSource;
  repository: { repo: string };
  revisionSha: string;
  uploadBundle: CalibrationManifestStore["uploadBundle"];
  existing: CalibrationTreeManifestEntry | null;
  existingIndex: Awaited<ReturnType<typeof auditCalibrationTreeBuild>>["index"];
  dryRun: boolean;
}) {
  const {
    country,
    runId,
    source,
    repository,
    revisionSha,
    uploadBundle,
    existing,
    existingIndex,
    dryRun,
  } = options;
  const { calibration } = source;
  const candidateReleaseId = source.candidateReleaseId;
  const bundle = buildCalibrationTreeBundle({
    country,
    ...(existing ? { buildArtifactId: existing.buildArtifactId } : {}),
    buildKind: "staging",
    sourceId: runId,
    label: existingIndex?.build.label ?? candidateReleaseId,
    createdAt: existingIndex?.build.createdAt ?? source.updatedAt,
    releaseId: existingIndex?.build.releaseId ?? candidateReleaseId,
    hfRepo: existing?.hfRepo ?? repository.repo,
    hfCommitSha: existing?.hfCommitSha ?? revisionSha,
    sourceArtifacts: existingIndex?.build.sourceArtifacts ?? {
      ...source.sourceArtifacts,
      demographics: null,
    },
    rows: calibration.rows,
    calibrationProvenance: calibration.calibration_provenance,
    lossAttributionAvailable:
      calibration.target_loss_attribution.status !== "unavailable",
    comparison: {
      releaseId: calibration.release_id,
      calibrationProvenance: calibration.calibration_provenance,
      status: calibration.target_loss_attribution.status,
      aggregate: calibration.target_loss_attribution.aggregate,
      cap: calibration.target_loss_attribution.cap,
      basisIdentifier: calibration.target_loss_attribution.basis_identifier,
      targetRepresentation: calibration.target_schema.target_representation,
    },
  });
  for (const file of bundle.files) enforceConfiguredSizeLimits(file);
  const indexFile = bundle.files.find((file) => file.part === "index");
  if (!indexFile) throw new Error("Calibration tree bundle has no index.");
  if (existing && indexFile.sha256 !== existing.indexSha256) {
    throw new Error(
      `Rebuilt staging index differs from immutable manifest entry ${existing.buildArtifactId}.`,
    );
  }
  const stored = dryRun
    ? {
        files: bundle.files.map((file) => ({
          pathname: file.path,
          sha256: file.sha256,
          bytes: file.rawBytes,
          created: false,
        })),
        index: {
          pathname: indexFile.path,
          sha256: indexFile.sha256,
          bytes: indexFile.rawBytes,
          created: false,
        },
      }
    : await uploadBundle(bundle);
  const entry: CalibrationTreeManifestEntry = existing ?? {
    buildArtifactId: bundle.index.buildArtifactId,
    kind: "staging",
    sourceId: runId,
    label: candidateReleaseId,
    releaseId: null,
    stagingRunId: runId,
    hfRepo: repository.repo,
    hfCommitSha: revisionSha,
    treeSchemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
    indexSha256: stored.index.sha256,
    indexBytes: stored.index.bytes,
    createdAt: source.updatedAt,
    updatedAt: source.updatedAt,
  };
  return { bundle, stored, entry };
}

export async function reconcileStagingBuilds(
  country: MicrocosmCountry,
  expectedSha: string | undefined,
  blobToken: string,
  dryRun: boolean,
  store: CalibrationManifestStore = blobCalibrationManifestStore(country, blobToken),
): Promise<ReconciliationReport> {
  const repository = stagingRepository(country);
  if (!repository) {
    throw new Error(`Staging is not configured for ${country}.`);
  }
  const revisionSha = expectedSha
    ? exactCommitSha(expectedSha, "Staging source revision")
    : await resolveStagingRevisionSha(country);
  const runs = await loadStagingRuns(0, country, revisionSha);
  if (!runs.available) {
    throw new Error(runs.detail ?? "The staging repository is unavailable.");
  }
  if (runs.incompatible_runs.length > 0) {
    throw new Error(
      `Staging inventory contains incompatible runs: ${runs.incompatible_runs
        .map((run) => run.run_id)
        .join(", ")}.`,
    );
  }
  const finalized = runs.runs.filter((run) =>
    FINAL_STAGING_STATUSES.has(run.status ?? ""),
  );
  const uploadBundle = store.uploadBundle;
  let manifest = await store.readManifest();
  let blobs = await store.listBlobs();
  const outcomes: ReconciliationOutcome[] = [];

  for (const run of finalized) {
    const existing = manifestBuildForSource(
      manifest,
      country,
      "staging",
      run.run_id,
    );
    // A run whose build cannot be published or repaired is reported with
    // the reason, and the runs after it are still reconciled: stopping here
    // would leave every later run unpublished on every later invocation.
    // `runPublisher` exits with an error once the report is printed.
    let buildArtifactId = existing?.buildArtifactId ?? null;
    try {
      const audit = existing ? await store.audit(existing, blobs) : null;
      if (existing && audit?.complete) {
        outcomes.push({
          sourceId: run.run_id,
          status: "complete",
          buildArtifactId: existing.buildArtifactId,
          reason: null,
        });
        continue;
      }
      if (audit && !audit.repairable) {
        throw new Error(
          `Staging build ${run.run_id} is corrupt and cannot be repaired automatically: ` +
            audit.reasons.join("; "),
        );
      }
      const source = await resolveStagingCalibrationSource(
        run.run_id,
        0,
        country,
        existing?.hfCommitSha ?? revisionSha,
      );
      if (!source) {
        if (existing) {
          throw new Error(
            `Incomplete staging build ${run.run_id} no longer has calibration diagnostics.`,
          );
        }
        outcomes.push({
          sourceId: run.run_id,
          status: "ineligible",
          buildArtifactId: null,
          reason: "Finalized staging run has no calibration diagnostics.",
        });
        continue;
      }
      const published = await publishStagingSource({
        country,
        runId: run.run_id,
        source,
        repository,
        revisionSha: existing?.hfCommitSha ?? revisionSha,
        uploadBundle,
        existing,
        existingIndex: audit?.index ?? null,
        dryRun,
      });
      buildArtifactId = published.entry.buildArtifactId;
      if (!dryRun) {
        manifest = await store.updateManifest(published.entry);
        blobs = await store.listBlobs();
        const verified = await store.audit(published.entry, blobs);
        if (!verified.complete) {
          throw new Error(
            `Staging build ${run.run_id} remains incomplete: ${verified.reasons.join("; ")}`,
          );
        }
      }
      outcomes.push({
        sourceId: run.run_id,
        status: existing ? "repaired" : "published",
        buildArtifactId: published.entry.buildArtifactId,
        reason: dryRun ? "Dry run; no Blob writes performed." : null,
      });
    } catch (error) {
      outcomes.push({
        sourceId: run.run_id,
        status: "failed",
        buildArtifactId,
        reason: error instanceof Error ? error.message : String(error),
      });
    }
  }
  return { country, sourceKind: "staging", dryRun, outcomes };
}

export async function reconcileReleaseBuilds(
  country: MicrocosmCountry,
  blobToken: string,
  dryRun: boolean,
  store: CalibrationManifestStore = blobCalibrationManifestStore(country, blobToken),
): Promise<ReconciliationReport> {
  let manifest = await store.readManifest();
  // releases.json at main's head decides the inventory. A repository with it
  // reconciles every build it lists; entries published from tags or release
  // folders stay in the manifest as they are, still readable, and are not
  // reconciled again. A repository without it reconciles its release folders
  // and tags. A record that cannot be read or is refused stops the run here,
  // before anything is published or the latest release is moved.
  const headSha = await publicationRevisionSha(country);
  const record = await readReleasesRecord(country, headSha);
  const inventory = record
    ? await enumerateRecordedBuildCandidates(country, manifest, record, headSha)
    : await enumerateReleaseCandidates(country, manifest);
  let blobs = await store.listBlobs();
  const outcomes = [...inventory.ineligible, ...inventory.failed];

  for (const candidate of inventory.candidates) {
    const existing = manifestBuildForSource(
      manifest,
      country,
      "release",
      candidate.releaseId,
    );
    // A release whose build cannot be published or repaired is reported with
    // the reason, and the releases after it are still reconciled: stopping
    // here would leave every later release, and the latest-release
    // selection, unreconciled on every later invocation. `runPublisher`
    // exits with an error once the report is printed. The manifest write is
    // a release's last step, after its files are uploaded and audited, so a
    // release that fails gets no new manifest entry (unless that write's own
    // read-back check fails after the write was stored); a release that
    // already had an entry keeps it unchanged. The latest-release selection
    // is never moved to a failed release.
    try {
      if (existing && existing.hfCommitSha !== candidate.hfCommitSha) {
        throw new Error(
          `Released dataset ${candidate.releaseId} changed from ${existing.hfCommitSha} ` +
            `to ${candidate.hfCommitSha}.`,
        );
      }
      if (existing && releaseDirectoryOf(existing) !== candidate.releaseDirectory) {
        throw new Error(
          `Released dataset ${candidate.releaseId} moved from releases/${releaseDirectoryOf(existing)}/ ` +
            `to releases/${candidate.releaseDirectory}/.`,
        );
      }
      const audit = existing ? await store.audit(existing, blobs) : null;
      if (existing && audit?.complete) {
        // Reconciliation validates and restores stored parts only; a stored
        // entry's metadata is the timestamp migration's business.
        outcomes.push({
          sourceId: candidate.releaseId,
          status: "complete",
          buildArtifactId: existing.buildArtifactId,
          reason: null,
        });
        continue;
      }
      if (audit && !audit.repairable) {
        throw new Error(
          `Release build ${candidate.releaseId} is corrupt and cannot be repaired automatically: ` +
            audit.reasons.join("; "),
        );
      }
      const published = await publishRelease({
        country,
        id: candidate.releaseId,
        releaseDirectory: candidate.releaseDirectory,
        expectedSha: existing?.hfCommitSha ?? candidate.hfCommitSha,
        blobToken,
        useExpectedShaExactly: true,
        buildArtifactId: existing?.buildArtifactId,
        dryRun,
        expectedIndexSha256: existing?.indexSha256,
        // A build folder is dated by releases.json; anything else by its commit.
        indexCreatedAt: existing ? existing.createdAt : candidate.publishedAt,
        uploadBundle: store.uploadBundle,
      });
      const entry = existing ?? releaseManifestEntry(published);
      if (!dryRun) {
        // The uploaded files are audited before the manifest names them, so
        // an incomplete upload never becomes a manifest entry. The audit
        // reads the build's own index and the Blob listing, not the
        // manifest.
        blobs = await store.listBlobs();
        const verified = await store.audit(entry, blobs);
        if (!verified.complete) {
          throw new Error(
            `Release build ${candidate.releaseId} remains incomplete: ${verified.reasons.join("; ")}`,
          );
        }
        manifest = await store.updateManifest(entry);
      }
      outcomes.push({
        sourceId: candidate.releaseId,
        status: existing ? "repaired" : "published",
        buildArtifactId: entry.buildArtifactId,
        reason: dryRun ? "Dry run; no Blob writes performed." : null,
      });
    } catch (error) {
      outcomes.push({
        sourceId: candidate.releaseId,
        status: "failed",
        buildArtifactId: existing?.buildArtifactId ?? null,
        reason: error instanceof Error ? error.message : String(error),
      });
    }
  }

  const failedReleaseIds = new Set(
    outcomes.filter((outcome) => outcome.status === "failed").map((outcome) => outcome.sourceId),
  );
  const latestReleaseFailure = await selectLatestRelease(
    country,
    manifest,
    failedReleaseIds,
    dryRun,
    store,
    record
      ? async () =>
        recordedLatest(record, inventory.candidates, [...inventory.ineligible, ...inventory.failed])
      : (current) => readUpstreamLatest(country, headSha, current),
  );
  return {
    country,
    sourceKind: "releases",
    dryRun,
    outcomes,
    ...(latestReleaseFailure == null ? {} : { latestReleaseFailure }),
  };
}

/**
 * Point the country's latest-release selection at the release `readLatest`
 * names (the upstream pointer's release, or releases.json's current build of
 * its default release), once that release is completely published. Returns
 * why the selection could not be read or updated, or null. A release that
 * failed in this reconciliation is never selected, so the selection keeps
 * naming the build it named before. A dry run reads the latest release and
 * reports one that failed, but neither checks nor changes the selection
 * otherwise.
 */
async function selectLatestRelease(
  country: MicrocosmCountry,
  manifest: CalibrationTreeManifest,
  failedReleaseIds: ReadonlySet<string>,
  dryRun: boolean,
  store: CalibrationManifestStore,
  readLatest: (
    manifest: CalibrationTreeManifest,
  ) => Promise<{ releaseId: string; hfCommitSha: string }>,
): Promise<string | null> {
  try {
    const latest = await readLatest(manifest);
    // Checked before the dry-run return, so a dry run reports a failed
    // latest release exactly as a run that writes does.
    if (failedReleaseIds.has(latest.releaseId)) {
      return `Latest release ${latest.releaseId} failed to reconcile, so the latest-release selection was not changed.`;
    }
    // A dry run writes no manifest entries, so it cannot check whether the
    // release is completely published.
    if (dryRun) return null;
    const latestEntry = manifestBuildForSource(
      manifest,
      country,
      "release",
      latest.releaseId,
    );
    if (!latestEntry || latestEntry.hfCommitSha !== latest.hfCommitSha) {
      return `Latest release ${latest.releaseId}@${latest.hfCommitSha} is not completely published.`;
    }
    await store.updateManifest(latestEntry, true);
    return null;
  } catch (error) {
    return error instanceof Error ? error.message : String(error);
  }
}

// The latest release for a repository with releases.json: the record's
// current build of its default release, at the commit reconciliation pinned
// it to. A current build that was not reconciled gives its reason: why it is
// ineligible, or why its folder or release manifest could not be read.
function recordedLatest(
  record: ReleasesRecord,
  candidates: readonly ReleaseCandidate[],
  notReconciled: readonly ReconciliationOutcome[],
): { releaseId: string; hfCommitSha: string } {
  const current = requireCurrentDefaultBuild(record);
  const candidate = candidates.find((entry) => entry.releaseId === current.id);
  if (!candidate) {
    const outcome = notReconciled.find((entry) => entry.sourceId === current.id);
    const reason = outcome?.reason ?? "releases.json lists it, but reconciliation found no candidate.";
    throw new Error(
      outcome?.status === "failed"
        ? `Latest release ${current.id} failed to reconcile, so the latest-release selection ` +
          `was not changed: ${reason}`
        : `Latest release ${current.id} cannot be published: ${reason}`,
    );
  }
  return { releaseId: current.id, hfCommitSha: candidate.hfCommitSha };
}

interface TimestampMigrationOutcome {
  buildArtifactId: string;
  releaseId: string;
  hfRepo: string;
  hfCommitSha: string;
  from: string | null;
  to: string;
  status: "current" | "planned" | "migrated";
}

interface TimestampMigrationReport {
  country: MicrocosmCountry;
  mode: "migrate-release-timestamps";
  dryRun: boolean;
  outcomes: TimestampMigrationOutcome[];
}

/**
 * Set each stored release entry's updatedAt to its pinned commit's date.
 *
 * A one-time metadata migration for entries written by earlier publishers,
 * which dated a release from its id or tag name and wrote the 1970
 * placeholder when that failed. It reads only the manifest: each release
 * entry's own hfRepo and hfCommitSha name the authoritative commit, whose
 * date becomes updatedAt; the current deployment's repository configuration
 * plays no part. Nothing else moves: createdAt stays as stored (it is
 * sealed in the immutable index, null on legacy builds), and the build id,
 * index digest, shard descriptors and the latest-release selection are
 * preserved. Staging entries keep their telemetry time. Idempotent; a dry
 * run lists every old and new value without writing. Remove once the
 * legacy entries are corrected.
 */
export async function migrateReleaseTimestamps(
  country: MicrocosmCountry,
  blobToken: string,
  dryRun: boolean,
  store: CalibrationManifestStore = blobCalibrationManifestStore(country, blobToken),
): Promise<TimestampMigrationReport> {
  const manifest = await store.readManifest();
  const outcomes: TimestampMigrationOutcome[] = [];
  for (const entry of manifest.countries[country]?.builds ?? []) {
    if (entry.kind !== "release" || !entry.releaseId) continue;
    // A build folder is dated by releases.json, not by the commit that wrote
    // the folder (a migrated build's folder is written after publication),
    // and was never stored with the placeholder.
    if (isBuildFolderId(entry.releaseId)) continue;
    const publishedAt = await resolveHfRepositoryCommitPublishedAt(
      entry.hfRepo,
      entry.hfCommitSha,
      0,
    );
    const outcome = {
      buildArtifactId: entry.buildArtifactId,
      releaseId: entry.releaseId,
      hfRepo: entry.hfRepo,
      hfCommitSha: entry.hfCommitSha,
      from: entry.updatedAt,
      to: publishedAt,
    };
    if (entry.updatedAt === publishedAt) {
      outcomes.push({ ...outcome, status: "current" });
      continue;
    }
    if (!dryRun) {
      await store.updateManifest({ ...entry, updatedAt: publishedAt });
    }
    outcomes.push({ ...outcome, status: dryRun ? "planned" : "migrated" });
  }
  return { country, mode: "migrate-release-timestamps", dryRun, outcomes };
}

interface ReleaseSelection {
  releaseId: string;
  releaseDirectory: string;
  hfCommitSha: string | undefined;
}

// `--release <id>`: a Microcosm cut tag publishes from its line's folder at the
// tag's commit; any other id publishes from its own folder, as before.
async function requestedReleaseSelection(
  country: MicrocosmCountry,
  id: string,
  hfCommitSha: string | undefined,
): Promise<ReleaseSelection> {
  const selection = { releaseId: id, releaseDirectory: id, hfCommitSha };
  if (!parseReleaseCutTag(id)) return selection;
  let commit: string;
  try {
    commit = await resolveHfRevisionSha(country, id, 0);
  } catch (error) {
    if (error instanceof CalibrationReleaseNotFoundError) return selection;
    throw error;
  }
  return {
    ...selection,
    releaseDirectory: await releaseDirectoryForTag(country, id, commit),
  };
}

/**
 * The error a report's failed outcomes, or a latest-release selection that
 * could not be updated, end the publisher with, if any.
 */
export function reconciliationFailureMessage(report: ReconciliationReport): string | null {
  const failed = report.outcomes.filter((outcome) => outcome.status === "failed");
  const messages: string[] = [];
  if (failed.length > 0) {
    const sources = report.sourceKind === "releases" ? "releases" : "staging sources";
    messages.push(
      `${failed.length} of ${report.outcomes.length} ${sources} failed to ` +
        `reconcile: ${failed.map((outcome) => outcome.sourceId).join(", ")}.`,
    );
  }
  if (report.latestReleaseFailure) {
    messages.push(`The latest release was not selected: ${report.latestReleaseFailure}`);
  }
  if (messages.length === 0) return null;
  return [...messages, "The printed report gives each reason."].join(" ");
}

export interface PublisherDependencies {
  /** The manifest and Blob store every release mode reads and writes. */
  store?: CalibrationManifestStore;
}

// `--latest` and `--release <release>/<build>` for a repository with
// releases.json: the build the record at `revisionSha` lists, pinned to the
// commit that wrote its folder.
async function requestedRecordedBuild(
  country: MicrocosmCountry,
  options: PublisherOptions,
  record: ReleasesRecord,
  revisionSha: string,
): Promise<Required<ReleaseCandidate>> {
  if (options.mode === "latest") {
    return recordedCurrentBuild(country, record, revisionSha);
  }
  const id = releaseOrBuildFolderId(options.releaseId);
  if (!isBuildFolderId(id)) {
    throw new Error(
      `${country}'s dataset repository has ${RELEASES_RECORD_PATH}; --release takes ` +
        `<release>/<build>, not ${id}.`,
    );
  }
  return selectRecordedBuild(country, record, id, revisionSha);
}

export async function runPublisher(
  options: PublisherOptions,
  dependencies: PublisherDependencies = {},
): Promise<void> {
  const blobToken = process.env.BLOB_READ_WRITE_TOKEN?.trim();
  if (!blobToken) throw new Error("BLOB_READ_WRITE_TOKEN is required.");
  const store = dependencies.store ?? blobCalibrationManifestStore(options.country, blobToken);

  if (options.mode === "migrate-release-timestamps") {
    const report = await migrateReleaseTimestamps(
      options.country,
      blobToken,
      options.dryRun === true,
      store,
    );
    console.log(JSON.stringify(report));
    return;
  }

  if (
    options.mode === "staging-finalized" ||
    options.mode === "reconcile-staging"
  ) {
    const report = await reconcileStagingBuilds(
      options.country,
      options.hfCommitSha,
      blobToken,
      options.dryRun === true,
      store,
    );
    console.log(JSON.stringify(report));
    const failure = reconciliationFailureMessage(report);
    if (failure) throw new Error(failure);
    return;
  }

  if (
    options.mode === "backfill" ||
    options.mode === "reconcile-releases"
  ) {
    const report = await reconcileReleaseBuilds(
      options.country,
      blobToken,
      options.dryRun === true,
      store,
    );
    console.log(JSON.stringify(report));
    const failure = reconciliationFailureMessage(report);
    if (failure) throw new Error(failure);
    return;
  }

  // releases.json at the commit being published decides how the release is
  // read: from the record and its build folders, or, where the repository
  // has none, from its tags and pointer files.
  const revisionSha = await publicationRevisionSha(options.country, options.hfCommitSha);
  const record = await readReleasesRecord(options.country, revisionSha);
  if (!record && options.mode === "release" && isBuildFolderId(options.releaseId)) {
    throw new Error(
      `${options.country}'s dataset repository has no ${RELEASES_RECORD_PATH}, so ` +
        `${options.releaseId} cannot name a build folder.`,
    );
  }
  let published: Awaited<ReturnType<typeof publishRelease>>;
  if (record) {
    const selection = await requestedRecordedBuild(options.country, options, record, revisionSha);
    published = await publishRelease({
      country: options.country,
      id: selection.releaseId,
      releaseDirectory: selection.releaseDirectory,
      expectedSha: selection.hfCommitSha,
      blobToken,
      useExpectedShaExactly: true,
      indexCreatedAt: selection.publishedAt,
      uploadBundle: store.uploadBundle,
    });
  } else {
    // The pointer file is read at the commit releases.json was looked for.
    const selection: ReleaseSelection = options.mode === "latest"
      ? await readUpstreamLatest(options.country, revisionSha)
      : await requestedReleaseSelection(
          options.country,
          releaseId(options.releaseId),
          options.hfCommitSha,
        );
    published = await publishRelease({
      country: options.country,
      id: selection.releaseId,
      releaseDirectory: selection.releaseDirectory,
      expectedSha: selection.hfCommitSha,
      blobToken,
      allowMissingReleaseTag: options.mode === "latest",
      uploadBundle: store.uploadBundle,
    });
  }
  const entry = await registerReleaseBuild(published, store);
  const promoted = await promoteIfCurrent(options.country, published, entry, store);
  console.log(JSON.stringify({
    country: options.country,
    releaseId: published.bundle.index.build.releaseId,
    hfCommitSha: published.bundle.index.build.hfCommitSha,
    buildArtifactId: published.bundle.index.buildArtifactId,
    files: published.stored.files,
    sizes: published.sizes,
    promoted,
  }));
}

if (import.meta.main) {
  try {
    await runPublisher(parsePublisherOptions(Bun.argv.slice(2)));
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}
