import { readCalibrationTreeManifest } from "./calibration-tree-blob";
import {
  calibrationTreeManifestEntry,
  releaseDirectoryOf,
} from "./calibration-tree-manifest";
import type { CalibrationTreeManifest } from "./calibration-tree-manifest";
import {
  countryRegistration,
  type MicrocosmCountry,
} from "./countries";
import { isBuildFolderId, parseBuildFolderId } from "./release-record";

export interface CalibrationReleaseLocation {
  buildArtifactId: string;
  releaseId: string;
  /**
   * The `releases/` folder holding the files: releaseId, which for a build
   * in its own folder is `<release>/<build>`, unless releaseId is a tag read
   * from its release's folder.
   */
  releaseDirectory: string;
  hfCommitSha: string;
  updatedAt: string | null;
}

export class CalibrationReleaseNotFoundError extends Error {}

function hfHeaders(): HeadersInit | undefined {
  const token = process.env.HF_TOKEN ?? process.env.HUGGINGFACE_TOKEN;
  return token ? { Authorization: `Bearer ${token}` } : undefined;
}

/**
 * Whether a value may name a release or Hugging Face revision: safe to put
 * in a Hugging Face URL, and never `latest`, which names no fixed release.
 */
export function isReleaseId(value: unknown): value is string {
  return typeof value === "string" &&
    /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value) &&
    value !== "latest";
}

function safeReleaseId(value: string, label = "release"): string {
  if (!isReleaseId(value)) {
    throw new Error(`Invalid ${label} id.`);
  }
  return value;
}

function repository(country: MicrocosmCountry): string {
  const registration = countryRegistration(country);
  return (
    (registration.repo_env ? process.env[registration.repo_env] : undefined) ??
    registration.repo
  );
}

function repositoryRevision(country: MicrocosmCountry): string {
  const registration = countryRegistration(country);
  return (
    (registration.revision_env
      ? process.env[registration.revision_env]
      : undefined) ?? registration.revision
  );
}

export async function resolveHfRevisionSha(
  country: MicrocosmCountry,
  revision: string,
  revalidate = 3600,
): Promise<string> {
  const safeRevision = safeReleaseId(revision, "Hugging Face revision");
  const repo = repository(country);
  const response = await fetch(
    `https://huggingface.co/api/datasets/${repo}/revision/${encodeURIComponent(safeRevision)}`,
    {
      headers: hfHeaders(),
      // Each Hugging Face lookup in this module stops after 30 seconds. The
      // publisher reads one revision per release tag, so no slow response
      // may hold its run indefinitely.
      signal: AbortSignal.timeout(30_000),
      ...(revalidate <= 0
        ? { cache: "no-store" as const }
        : { next: { revalidate } }),
    },
  );
  if (response.status === 404) {
    throw new CalibrationReleaseNotFoundError(
      `Hugging Face release ${revision} was not found for ${country}.`,
    );
  }
  if (!response.ok) {
    throw new Error(`Hugging Face revision lookup failed with ${response.status}.`);
  }
  const value = (await response.json()) as { sha?: unknown };
  const sha = typeof value.sha === "string" ? value.sha.toLowerCase() : "";
  if (!/^[0-9a-f]{40,64}$/.test(sha)) {
    throw new Error(`Hugging Face returned an invalid commit SHA for ${revision}.`);
  }
  return sha;
}

/**
 * Resolve a release directory that was published without a matching HF tag.
 *
 * The expanded tree response records the commit which last changed each file.
 * The newest of those commits is the first immutable snapshot that contains the
 * current versions of every release artifact in a linear repository history.
 * Callers still fetch every required artifact at the returned commit, so an
 * incomplete or inconsistent snapshot fails publication.
 */
export async function resolveHfReleaseDirectorySha(
  country: MicrocosmCountry,
  release: string,
  revalidate = 0,
): Promise<string> {
  const revision = repositoryRevision(country);
  const safeRelease = safeReleaseId(release);
  const repo = repository(country);
  const prefix = `releases/${safeRelease}/`;
  const response = await fetch(
    `https://huggingface.co/api/datasets/${repo}/tree/` +
      `${encodeURIComponent(revision)}/releases/${encodeURIComponent(safeRelease)}` +
      "?recursive=false&expand=true",
    {
      headers: hfHeaders(),
      signal: AbortSignal.timeout(30_000),
      ...(revalidate <= 0
        ? { cache: "no-store" as const }
        : { next: { revalidate } }),
    },
  );
  if (response.status === 404) {
    throw new CalibrationReleaseNotFoundError(
      `Hugging Face release directory ${release} was not found for ${country}.`,
    );
  }
  if (!response.ok) {
    throw new Error(
      `Hugging Face release directory lookup failed with ${response.status}.`,
    );
  }

  return newestCommitOfCalibratedFolder(
    await response.json(),
    prefix,
    `release ${release}`,
    country,
  );
}

/**
 * The commit that wrote a build folder, `releases/<release>/<build>/`, as
 * the folder is at `revision` (an exact commit, so the folder and the
 * `releases.json` read beside it come from one snapshot). A build folder is
 * never changed after publishing, so the newest commit behind its files is
 * the one that wrote it, and every file is read there.
 */
export async function resolveHfBuildFolderSha(
  country: MicrocosmCountry,
  folderId: string,
  revision: string,
  revalidate = 0,
): Promise<string> {
  const folder = parseBuildFolderId(folderId);
  if (!folder) throw new Error("Invalid build folder.");
  const safeRevision = safeReleaseId(revision, "Hugging Face revision");
  const repo = repository(country);
  const response = await fetch(
    `https://huggingface.co/api/datasets/${repo}/tree/${encodeURIComponent(safeRevision)}` +
      `/releases/${encodeURIComponent(folder.release)}/${encodeURIComponent(folder.build)}` +
      "?recursive=false&expand=true",
    {
      headers: hfHeaders(),
      signal: AbortSignal.timeout(30_000),
      ...(revalidate <= 0
        ? { cache: "no-store" as const }
        : { next: { revalidate } }),
    },
  );
  if (response.status === 404) {
    throw new CalibrationReleaseNotFoundError(
      `Hugging Face build folder releases/${folderId}/ was not found for ${country}.`,
    );
  }
  if (!response.ok) {
    throw new Error(`Hugging Face build folder lookup failed with ${response.status}.`);
  }
  return newestCommitOfCalibratedFolder(
    await response.json(),
    `releases/${folderId}/`,
    `build folder releases/${folderId}/`,
    country,
  );
}

// The newest commit behind a folder listing's files (an expanded Hugging
// Face tree response), refusing a folder without calibration diagnostics.
function newestCommitOfCalibratedFolder(
  listing: unknown,
  prefix: string,
  subject: string,
  country: MicrocosmCountry,
): string {
  const entries = listing as Array<{
    path?: unknown;
    type?: unknown;
    lastCommit?: { id?: unknown; date?: unknown };
  }>;
  if (!Array.isArray(entries)) {
    throw new Error("Hugging Face returned an invalid release directory listing.");
  }
  const files = entries.filter(
    (entry) =>
      entry.type === "file" &&
      typeof entry.path === "string" &&
      entry.path.startsWith(prefix),
  );
  if (!files.some(
    (entry) => entry.path === `${prefix}calibration_diagnostics.json`,
  )) {
    throw new CalibrationReleaseNotFoundError(
      `Hugging Face ${subject} has no calibration diagnostics for ${country}.`,
    );
  }

  const commits = files.map((entry) => {
    const id = typeof entry.lastCommit?.id === "string"
      ? entry.lastCommit.id.toLowerCase()
      : "";
    const date = typeof entry.lastCommit?.date === "string"
      ? Date.parse(entry.lastCommit.date)
      : Number.NaN;
    if (!/^[0-9a-f]{40,64}$/.test(id) || !Number.isFinite(date)) {
      throw new Error(
        `Hugging Face returned invalid commit metadata for ${subject}.`,
      );
    }
    return { id, date };
  });
  commits.sort((left, right) => right.date - left.date);
  if (!commits[0]) {
    throw new CalibrationReleaseNotFoundError(
      `Hugging Face ${subject} has no source files for ${country}.`,
    );
  }
  return commits[0].id;
}

/**
 * When a Hugging Face commit was made: the one publication timestamp.
 *
 * Every calibration build pins the exact repository commit its bytes come
 * from (`hfRepo` and `hfCommitSha`), so its publication time is that
 * commit's date, read from that repository's commit log, whether the build
 * is being published, migrated or loaded. A commit is identified by
 * repository and SHA together, so callers holding recorded provenance pass
 * the recorded repository rather than the one the current deployment
 * configures. Release ids and tag names are not a source: microcosm's
 * national releases are named by line (`microcosm-uk-2024-25-national`)
 * and their immutable cut tags end in an attempt suffix. When the commit
 * cannot be read this fails rather than substituting a timestamp with
 * different semantics.
 */
export async function resolveHfRepositoryCommitPublishedAt(
  repo: string,
  sha: string,
  revalidate = 0,
): Promise<string> {
  if (!/^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(repo)) {
    throw new Error(`Hugging Face repository ${repo} is not an owner/name id.`);
  }
  const commitSha = sha.toLowerCase();
  if (!/^[0-9a-f]{40,64}$/.test(commitSha)) {
    throw new Error(`Hugging Face commit ${sha} is not a commit SHA.`);
  }
  const response = await fetch(
    `https://huggingface.co/api/datasets/${repo}/commits/${commitSha}?limit=1`,
    {
      headers: hfHeaders(),
      signal: AbortSignal.timeout(30_000),
      ...(revalidate <= 0
        ? { cache: "no-store" as const }
        : { next: { revalidate } }),
    },
  );
  if (response.status === 404) {
    throw new CalibrationReleaseNotFoundError(
      `Hugging Face commit ${sha} was not found in ${repo}.`,
    );
  }
  if (!response.ok) {
    throw new Error(`Hugging Face commit lookup failed with ${response.status}.`);
  }
  const commits = (await response.json()) as Array<{ id?: unknown; date?: unknown }>;
  const first = Array.isArray(commits) ? commits[0] : undefined;
  const id = typeof first?.id === "string" ? first.id.toLowerCase() : "";
  const date = typeof first?.date === "string" ? Date.parse(first.date) : Number.NaN;
  if (id !== commitSha || !Number.isFinite(date)) {
    throw new Error(`Hugging Face returned invalid commit metadata for ${sha}.`);
  }
  return new Date(date).toISOString();
}

/** The publication timestamp of a commit in the country's configured repository. */
export async function resolveHfCommitPublishedAt(
  country: MicrocosmCountry,
  sha: string,
  revalidate = 0,
): Promise<string> {
  return resolveHfRepositoryCommitPublishedAt(repository(country), sha, revalidate);
}

/**
 * A requested release: "latest" (also for an empty request), a release id
 * (a release folder or a tag), or a build folder, `<release>/<build>`.
 */
export function requestedReleaseId(requestedRelease: string): string {
  if (!requestedRelease || requestedRelease === "latest") return "latest";
  if (isBuildFolderId(requestedRelease)) return requestedRelease;
  return safeReleaseId(requestedRelease);
}

export async function resolveCalibrationRelease(
  country: MicrocosmCountry,
  requestedRelease: string,
  _revalidate = 3600,
): Promise<CalibrationReleaseLocation> {
  const releaseId = requestedReleaseId(requestedRelease);
  const { manifest } = await readCalibrationTreeManifest();
  return calibrationReleaseFromManifest(manifest, country, releaseId);
}

export function calibrationReleaseFromManifest(
  manifest: CalibrationTreeManifest,
  country: MicrocosmCountry,
  releaseId = "latest",
): CalibrationReleaseLocation {
  const entry = calibrationTreeManifestEntry(manifest, country, { releaseId });
  if (!entry.releaseId) {
    throw new Error(`Calibration build ${entry.buildArtifactId} is not a release.`);
  }
  return {
    buildArtifactId: entry.buildArtifactId,
    releaseId: entry.releaseId,
    releaseDirectory: releaseDirectoryOf(entry) ?? entry.releaseId,
    hfCommitSha: entry.hfCommitSha,
    // Null only on an entry written before the publication timestamp was
    // read from the pinned commit; the release loader then reads it from
    // that same commit.
    updatedAt: entry.updatedAt,
  };
}
