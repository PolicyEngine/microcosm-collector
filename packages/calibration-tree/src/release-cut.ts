// Microcosm publishes a constant-id release line (microcosm#823) as one
// immutable Hugging Face tag per cut, `<release id>-<YYYYMMDDTHHMMSSZ>-<attempt>`,
// while the cut's files stay under the line's own folder, `releases/<release id>/`.
// The tag names the build; the folder names where its files live. Isomorphic:
// the release picker labels cuts with the same grammar the publisher reads.

export interface ReleaseCut {
  /** The line's constant release id, which is also its `releases/` folder. */
  releaseId: string;
  /** The calibration attempt's instant, `YYYYMMDDTHHMMSSZ`. */
  cutAt: string;
  /** The calibration attempt's short identifier (8 hex digits). */
  attempt: string;
}

const RELEASE_CUT_TAG_RE =
  /^([A-Za-z0-9][A-Za-z0-9._-]*)-(\d{8}T\d{6}Z)-([0-9a-f]{8})$/;

export function parseReleaseCutTag(tag: string): ReleaseCut | null {
  const match = RELEASE_CUT_TAG_RE.exec(tag);
  return match
    ? { releaseId: match[1], cutAt: match[2], attempt: match[3] }
    : null;
}

// A release folder is interpolated into Hugging Face URLs that carry the
// server's token, so it passes the same allowlist as a release id.
const RELEASE_DIRECTORY_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export function isReleaseDirectory(value: unknown): value is string {
  return typeof value === "string" && RELEASE_DIRECTORY_RE.test(value);
}
