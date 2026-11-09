import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, expect, test } from "bun:test";

import {
  loadStagingCalibration,
  loadStagingComparison,
  loadStagingRun,
  loadStagingRuns,
  loadStagingTargetDiagnostics,
  MICROCOSM_STAGING_HF_REPO,
  MICROCOSM_STAGING_HF_REVISION,
  resolveStagingCalibrationSource,
  stagedBundleDiagnosticsFile,
  stagingRepository,
  stagingResolveUrl,
  stagingTargetChangeCacheTtlSeconds,
  stagingUnavailableReason,
} from "./staging-artifact";

test("resolves staging repositories from each country registration", () => {
  expect(stagingRepository("us")).toEqual({
    repo: "policyengine/populace-us-staging",
    revision: "main",
  });
  expect(MICROCOSM_STAGING_HF_REPO).toBe("policyengine/populace-us-staging");
  expect(MICROCOSM_STAGING_HF_REVISION).toBe("main");

  // Armenia is a fixture-only registration: this asserts country-specific
  // repository selection without publishing a country or telemetry artifact.
  expect(stagingRepository("am")).toEqual({
    repo: "policyengine/microcosm-am-staging-fixture",
    revision: "main",
  });
  expect(stagingResolveUrl("runs/am-fixture/progress.json", "am")).toBe(
    "https://huggingface.co/datasets/policyengine/microcosm-am-staging-fixture/resolve/main/runs/am-fixture/progress.json",
  );

  const environment = {
    POPULACE_STAGING_HF_REPO: "policyengine/us-staging-override",
    POPULACE_STAGING_HF_REVISION: "test-revision",
  };
  expect(stagingRepository("us", environment)).toEqual({
    repo: "policyengine/us-staging-override",
    revision: "test-revision",
  });
  expect(stagingRepository("am", environment)).toEqual({
    repo: "policyengine/microcosm-am-staging-fixture",
    revision: "main",
  });
});

test("loads Armenia staging telemetry from Armenia's registered repository", async () => {
  const originalFetch = globalThis.fetch;
  const urls: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    urls.push(url);
    if (url.endsWith("/runs.json")) {
      return Response.json({
        schema_version: 1,
        runs: [
          {
            run_id: "am-fixture",
            candidate_release_id: "am-candidate",
            status: "running",
          },
        ],
      });
    }
    if (url.includes("/tree/main/runs?recursive=true")) {
      return new Response(null, { status: 404 });
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  try {
    await expect(loadStagingRuns(0, "am")).resolves.toMatchObject({
      available: true,
      source_repo: "policyengine/microcosm-am-staging-fixture",
      revision: "main",
      runs: [{ run_id: "am-fixture", candidate_release_id: "am-candidate" }],
    });
    expect(urls).toEqual([
      "https://huggingface.co/api/datasets/policyengine/microcosm-am-staging-fixture/tree/main/runs?recursive=true",
      "https://huggingface.co/datasets/policyengine/microcosm-am-staging-fixture/resolve/main/runs.json",
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("names the country when staging is unavailable", () => {
  expect(stagingUnavailableReason("us")).toBeNull();
  expect(stagingUnavailableReason("uk")).toBeNull();
  expect(stagingUnavailableReason("be")).toBe(
    "Belgium has no staging repository.",
  );
  expect(stagingUnavailableReason("zz")).toBe(
    "Zedland has no staging repository.",
  );
});

const originalFetch = globalThis.fetch;
const originalUkRepo = process.env.POPULACE_UK_STAGING_HF_REPO;
const originalUkRevision = process.env.POPULACE_UK_STAGING_HF_REVISION;
const originalUkToken = process.env.POPULACE_UK_STAGING_HF_TOKEN;
const originalHfToken = process.env.HF_TOKEN;

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalHfToken == null) delete process.env.HF_TOKEN;
  else process.env.HF_TOKEN = originalHfToken;
  if (originalUkRepo == null) delete process.env.POPULACE_UK_STAGING_HF_REPO;
  else process.env.POPULACE_UK_STAGING_HF_REPO = originalUkRepo;
  if (originalUkRevision == null) delete process.env.POPULACE_UK_STAGING_HF_REVISION;
  else process.env.POPULACE_UK_STAGING_HF_REVISION = originalUkRevision;
  if (originalUkToken == null) delete process.env.POPULACE_UK_STAGING_HF_TOKEN;
  else process.env.POPULACE_UK_STAGING_HF_TOKEN = originalUkToken;
});

function v2RunManifest(
  runId: string,
  updatedAt: string,
  startedAt = "2026-01-01T00:00:00+00:00",
) {
  return {
    schema_name: "microcosm.staging.run-manifest",
    schema_version: 2,
    run_id: runId,
    country_code: "GB",
    operation_id: "uk_frs_spine",
    pipeline: { id: "uk_national_spine", version: "2026.09" },
    candidate_id: `${runId}-candidate`,
    release_id: null,
    run_kind: "smoke",
    non_release: true,
    started_at: startedAt,
    updated_at: updatedAt,
    status: "completed",
    current_stage: "complete",
    sample: { mode: "full" },
    delivery: {
      contract_version: 2,
      enabled: true,
      mode: "local_only",
      run_id: runId,
      configured_repository: null,
      upload_attempts: 0,
      upload_successes: 0,
      read_back: "not_requested",
      last_error_code: null,
      opt_out_reason: null,
    },
    artifacts: [],
    failure: null,
    paths: {
      progress: `runs/${runId}/progress.json`,
      events: `runs/${runId}/events.ndjson`,
      calibration_progress: null,
    },
  };
}

// Version 3 adds `block`, required on every run document (null unless blocked).
function v3RunManifest(
  runId: string,
  updatedAt: string,
  overrides: Record<string, unknown> = {},
) {
  return {
    ...v2RunManifest(runId, updatedAt),
    schema_version: 3,
    block: null,
    ...overrides,
  };
}

function calibrationDiagnostics() {
  return {
    schema_version: 6,
    weight_entity: "household",
    options: {},
    n_nonzero: 2,
    n_records: 2,
    initial_loss: 0.2,
    final_loss: 0.05,
    fraction_within_10pct: 1,
    loss_trajectory: [0.2, 0.05],
    skipped: [],
    targets: [
      {
        name: "ons/employment_income/total@2025",
        target_name: "ons/employment_income/total",
        period: 2025,
        entity: "household",
        source: "ons",
        metadata: {
          variable: "employment_income",
          geography: "United Kingdom",
          geography_level: "national",
        },
        target: 100,
        initial_estimate: 80,
        final_estimate: 95,
        relative_error: -0.05,
        within_tolerance: true,
      },
    ],
  };
}

function serializedDiagnostics(): { body: string; sha256: string } {
  const body = JSON.stringify(calibrationDiagnostics());
  return {
    body,
    sha256: createHash("sha256").update(body).digest("hex"),
  };
}

test("loads version 2 calibration diagnostics from the declared artifact path", async () => {
  const runId = "uk-calibration-with-diagnostics";
  const diagnostics = serializedDiagnostics();
  const manifest = {
    ...v2RunManifest(runId, "2026-01-03T00:00:03+00:00"),
    operation_id: "uk_national_calibration",
    run_kind: "calibration",
    artifacts: [
      {
        logical_name: "calibration_diagnostics",
        artifact_kind: "aggregate_diagnostics",
        contract_relative_path: "artifacts/calibration_diagnostics.json",
        media_type: "application/json",
        sha256: diagnostics.sha256,
        classification: "aggregate",
      },
    ],
  };
  const requested: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    requested.push(url);
    if (url.endsWith(`/runs/${runId}/run_manifest.json`)) {
      return Response.json(manifest);
    }
    if (url.endsWith(`/runs/${runId}/artifacts/calibration_diagnostics.json`)) {
      return new Response(diagnostics.body, {
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  const calibration = await loadStagingCalibration(runId, 0, "uk");

  expect(calibration?.release_id).toBe(`${runId}-candidate`);
  expect(calibration?.rows).toHaveLength(1);
  expect(calibration?.rows[0]?.final_estimate).toBe(95);
  expect(
    requested.some((url) =>
      url.endsWith(`/runs/${runId}/artifacts/calibration_diagnostics.json`),
    ),
  ).toBe(true);
  expect(
    requested.some((url) =>
      url.endsWith(`/runs/${runId}/calibration_diagnostics.json`),
    ),
  ).toBe(false);
});

test("resolves publisher provenance at an explicit immutable staging revision", async () => {
  const runId = "uk-calibration-publisher-source";
  const revision = "a".repeat(40);
  const diagnostics = serializedDiagnostics();
  const manifest = {
    ...v2RunManifest(runId, "2026-01-03T00:00:03+00:00"),
    operation_id: "uk_national_calibration",
    run_kind: "calibration",
    artifacts: [{
      logical_name: "calibration_diagnostics",
      artifact_kind: "aggregate_diagnostics",
      contract_relative_path: "artifacts/calibration_diagnostics.json",
      media_type: "application/json",
      sha256: diagnostics.sha256,
      classification: "aggregate",
    }],
  };
  globalThis.fetch = (async (input) => {
    const url = String(input);
    expect(url).toContain(`/resolve/${revision}/`);
    if (url.endsWith(`/runs/${runId}/run_manifest.json`)) {
      return Response.json(manifest);
    }
    if (url.endsWith(`/runs/${runId}/progress.json`)) {
      return new Response(null, { status: 404 });
    }
    if (url.endsWith(`/runs/${runId}/artifacts/calibration_diagnostics.json`)) {
      return new Response(diagnostics.body);
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  const source = await resolveStagingCalibrationSource(
    runId,
    0,
    "uk",
    revision,
  );

  expect(source?.calibration.rows).toHaveLength(1);
  expect(source?.stagingRevision).toBe(revision);
  expect(source?.sourceArtifacts.calibrationDiagnostics).toMatchObject({
    path: `runs/${runId}/artifacts/calibration_diagnostics.json`,
    sha256: diagnostics.sha256,
    hfRepo: "policyengine/populace-uk-staging",
    hfCommitSha: revision,
  });
  expect(source?.sourceArtifacts.stagingRunManifest).toMatchObject({
    path: `runs/${runId}/run_manifest.json`,
    hfCommitSha: revision,
  });
});

test("rejects version 2 calibration diagnostics whose bytes do not match the declared digest", async () => {
  const runId = "uk-calibration-digest-mismatch";
  const manifest = {
    ...v2RunManifest(runId, "2026-01-03T00:00:03+00:00"),
    operation_id: "uk_national_calibration",
    run_kind: "calibration",
    artifacts: [
      {
        logical_name: "calibration_diagnostics",
        artifact_kind: "aggregate_diagnostics",
        contract_relative_path: "artifacts/calibration_diagnostics.json",
        media_type: "application/json",
        sha256: "0".repeat(64),
        classification: "aggregate",
      },
    ],
  };
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.endsWith(`/runs/${runId}/run_manifest.json`)) {
      return Response.json(manifest);
    }
    if (url.endsWith(`/runs/${runId}/artifacts/calibration_diagnostics.json`)) {
      return new Response(serializedDiagnostics().body);
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  await expect(loadStagingCalibration(runId, 0, "uk")).rejects.toThrow(
    /digest does not match its run manifest declaration/,
  );
});

test("a version 3 run reads its declared diagnostics and checks their digest", async () => {
  const diagnostics = serializedDiagnostics();
  const declared = (runId: string, sha256: string) =>
    v3RunManifest(runId, "2026-01-03T00:00:03+00:00", {
      operation_id: "uk_national_calibration",
      run_kind: "calibration",
      artifacts: [
        {
          logical_name: "calibration_diagnostics",
          artifact_kind: "aggregate_diagnostics",
          contract_relative_path: "artifacts/calibration_diagnostics.json",
          media_type: "application/json",
          sha256,
          classification: "aggregate",
        },
      ],
    });
  const manifests = new Map([
    ["uk-v3-diagnostics", declared("uk-v3-diagnostics", diagnostics.sha256)],
    ["uk-v3-digest-mismatch", declared("uk-v3-digest-mismatch", "0".repeat(64))],
  ]);
  const requested: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    requested.push(url);
    const manifest = /\/runs\/([^/]+)\/run_manifest\.json$/.exec(url);
    if (manifest && manifests.has(manifest[1])) {
      return Response.json(manifests.get(manifest[1]));
    }
    if (url.endsWith("/artifacts/calibration_diagnostics.json")) {
      return new Response(diagnostics.body);
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  const calibration = await loadStagingCalibration("uk-v3-diagnostics", 0, "uk");
  expect(calibration?.rows).toHaveLength(1);
  expect(
    requested.some((url) => url.endsWith("/runs/uk-v3-diagnostics/calibration_diagnostics.json")),
  ).toBe(false);
  await expect(
    loadStagingCalibration("uk-v3-digest-mismatch", 0, "uk"),
  ).rejects.toThrow(/digest does not match its run manifest declaration/);
});

test("keeps the version 1 calibration diagnostics path", async () => {
  const runId = "legacy-calibration";
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.endsWith(`/runs/${runId}/progress.json`)) {
      return Response.json({
        schema_version: 1,
        run_id: runId,
        candidate_release_id: "legacy-candidate",
      });
    }
    if (url.endsWith(`/runs/${runId}/run_manifest.json`)) {
      return Response.json({ schema_version: 1, run_id: runId });
    }
    if (url.endsWith(`/runs/${runId}/calibration_diagnostics.json`)) {
      return Response.json(calibrationDiagnostics());
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  const calibration = await loadStagingCalibration(runId, 0, "us");

  expect(calibration?.release_id).toBe("legacy-candidate");
  expect(calibration?.rows[0]?.final_estimate).toBe(95);
});

test("UK staging reads use the private repository credential only on server fetches", async () => {
  const token = "hf_test_server_only_credential";
  const repo = "policyengine/test-uk-staging-private";
  const revision = "test-revision";
  process.env.POPULACE_UK_STAGING_HF_REPO = repo;
  process.env.POPULACE_UK_STAGING_HF_REVISION = revision;
  process.env.POPULACE_UK_STAGING_HF_TOKEN = token;

  const fixtureRoot = join(
    import.meta.dir,
    "fixtures",
    "staging-contract",
    "v2",
    "completed-spine",
  );
  const runId = "uk-spine-v2-fixture";
  const requested: { url: string; authorization: string | null }[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    const headers = new Headers(init?.headers);
    requested.push({ url, authorization: headers.get("Authorization") });
    if (url.includes(`/tree/${revision}/runs`)) {
      return Response.json([
        {
          type: "file",
          path: `runs/${runId}/run_manifest.json`,
        },
      ]);
    }
    const marker = `/resolve/${revision}/`;
    const relative = url.includes(marker) ? url.split(marker, 2)[1] : null;
    if (!relative) return new Response(null, { status: 404 });
    if (relative === "runs.json" || relative === "latest_staging.json") {
      return new Response(null, { status: 404 });
    }
    const path = join(fixtureRoot, relative);
    try {
      const content = readFileSync(path);
      return new Response(content, {
        status: 200,
        headers: {
          "Content-Type": relative.endsWith(".ndjson")
            ? "application/x-ndjson"
            : "application/json",
        },
      });
    } catch {
      return new Response(null, { status: 404 });
    }
  }) as typeof fetch;

  const runs = await loadStagingRuns(0, "uk");
  const detail = await loadStagingRun(runId, 0, "uk");

  expect(runs).toMatchObject({
    available: true,
    source_repo: repo,
    revision,
    runs: [
      {
        run_id: runId,
        country_code: "GB",
        run_kind: "smoke",
        non_release: true,
        schema_version: 2,
      },
    ],
  });
  expect(detail).toMatchObject({
    available: true,
    source_repo: repo,
    revision,
    run_id: runId,
    candidate_release_id: runId,
    release_id: null,
    country_code: "GB",
    run_kind: "smoke",
    non_release: true,
    schema_version: 2,
    delivery: { contract_version: 2, mode: "local_only" },
    calibration_progress: null,
    has_calibration: false,
  });
  expect(detail.events?.map((event) => event.stage)).toEqual([
    "created",
    "input_verification",
    "sampling",
    "construction",
    "validation",
    "spine_h5_creation",
    "sidecar_creation",
    "complete",
  ]);
  expect(requested.length).toBeGreaterThan(0);
  expect(requested.every((request) => request.url.includes(repo))).toBe(true);
  expect(requested.every((request) => request.authorization === `Bearer ${token}`)).toBe(
    true,
  );
  expect(requested.some((request) => request.url.endsWith("/runs.json"))).toBe(false);
  expect(
    requested.some((request) => request.url.endsWith("/latest_staging.json")),
  ).toBe(false);
  expect(JSON.stringify({ runs, detail })).not.toContain(token);
});

test("discovers and orders version 2 runs from paginated manifests only", async () => {
  const repo = "policyengine/test-uk-staging-private";
  const revision = "test-revision";
  process.env.POPULACE_UK_STAGING_HF_REPO = repo;
  process.env.POPULACE_UK_STAGING_HF_REVISION = revision;
  const treeUrl =
    `https://huggingface.co/api/datasets/${repo}/tree/${revision}/runs` +
    "?recursive=true";
  const nextUrl = `${treeUrl}&cursor=next`;
  const manifests = new Map<string, Record<string, unknown>>([
    [
      "alpha",
      v2RunManifest(
        "alpha",
        "2026-01-03T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00",
      ),
    ],
    [
      "beta",
      v2RunManifest(
        "beta",
        "2026-01-03T00:00:00+00:00",
        "2026-01-02T00:00:00+00:00",
      ),
    ],
    [
      "zeta",
      v2RunManifest(
        "zeta",
        "2026-01-03T00:00:00+00:00",
        "2026-01-02T00:00:00+00:00",
      ),
    ],
    ["older", v2RunManifest("older", "2026-01-02T00:00:00+00:00")],
  ]);
  const requested: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    requested.push(url);
    if (url === treeUrl) {
      return Response.json(
        [
          { type: "file", path: "runs/older/run_manifest.json" },
          { type: "file", path: "runs/alpha/run_manifest.json" },
          { type: "file", path: "runs/ignored/progress.json" },
          { type: "directory", path: "runs/directory/run_manifest.json" },
          { type: "file", path: "runs/nested/run_manifest.json/extra" },
        ],
        { headers: { Link: `<${nextUrl}>; rel="next"` } },
      );
    }
    if (url === nextUrl) {
      return Response.json([
        { type: "file", path: "runs/beta/run_manifest.json" },
        { type: "file", path: "runs/zeta/run_manifest.json" },
      ]);
    }
    const match = /\/runs\/([^/]+)\/run_manifest\.json$/.exec(url);
    if (match && manifests.has(match[1])) {
      return Response.json(manifests.get(match[1]));
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  const result = await loadStagingRuns(0, "uk");

  expect(result.runs.map((run) => run.run_id)).toEqual([
    "zeta",
    "beta",
    "alpha",
    "older",
  ]);
  expect(result.runs[0]?.run_id).toBe("zeta");
  expect(result.runs.every((run) => run.schema_version === 2)).toBe(true);
  expect(requested).not.toContain(
    `https://huggingface.co/datasets/${repo}/resolve/${revision}/runs.json`,
  );
  expect(requested.some((url) => url.endsWith("/latest_staging.json"))).toBe(false);
  expect(requested.some((url) => url.includes("/ignored/"))).toBe(false);
  expect(requested.some((url) => url.includes("/directory/"))).toBe(false);
});

test("lists version 3 runs, blocked ones included, beside version 2 runs", async () => {
  const manifests = new Map<string, Record<string, unknown>>([
    ["older-v2", v2RunManifest("older-v2", "2026-01-02T00:00:00+00:00")],
    [
      "blocked-v3",
      v3RunManifest("blocked-v3", "2026-01-03T00:00:00+00:00", {
        status: "blocked",
        current_stage: "blocked",
        block: {
          phase: "preflight",
          blocking_failure_count: 1,
          blocking_gate_ids: ["uk_target_fit"],
        },
      }),
    ],
  ]);
  const requested: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    requested.push(url);
    if (url.includes("/api/datasets/")) {
      return Response.json([
        { type: "file", path: "runs/older-v2/run_manifest.json" },
        { type: "file", path: "runs/blocked-v3/run_manifest.json" },
      ]);
    }
    const match = /\/runs\/([^/]+)\/run_manifest\.json$/.exec(url);
    if (match && manifests.has(match[1])) {
      return Response.json(manifests.get(match[1]));
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  const result = await loadStagingRuns(0, "uk");

  expect(result.incompatible_runs).toEqual([]);
  expect(
    result.runs.map((run) => [run.run_id, run.schema_version, run.status, run.stage]),
  ).toEqual([
    ["blocked-v3", 3, "blocked", "blocked"],
    ["older-v2", 2, "completed", "complete"],
  ]);
  // Structured runs never fall back to the version 1 run index.
  expect(requested.some((url) => url.endsWith("/runs.json"))).toBe(false);
});

test("reports a version 2 manifest whose run id differs from its path", async () => {
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.includes("/tree/")) {
      return Response.json([
        { type: "file", path: "runs/listed/run_manifest.json" },
      ]);
    }
    if (url.endsWith("/runs/listed/run_manifest.json")) {
      return Response.json(
        v2RunManifest("different", "2026-01-01T00:00:00+00:00"),
      );
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  await expect(loadStagingRuns(0, "uk")).resolves.toMatchObject({
    runs: [],
    incompatible_runs: [
      {
        run_id: "listed",
        run_manifest_path: "runs/listed/run_manifest.json",
        detail: expect.stringMatching(/does not match/),
      },
    ],
  });
});

test("reports an unreadable listed manifest without hiding valid runs", async () => {
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.includes("/tree/")) {
      return Response.json([
        { type: "file", path: "runs/missing/run_manifest.json" },
        { type: "file", path: "runs/valid/run_manifest.json" },
      ]);
    }
    if (url.endsWith("/runs/valid/run_manifest.json")) {
      return Response.json(v2RunManifest("valid", "2026-01-01T00:00:00+00:00"));
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  await expect(loadStagingRuns(0, "uk")).resolves.toMatchObject({
    runs: [{ run_id: "valid" }],
    incompatible_runs: [
      {
        run_id: "missing",
        run_manifest_path: "runs/missing/run_manifest.json",
        detail: expect.stringMatching(
          /Staging artifact not found.*run_manifest\.json/,
        ),
      },
    ],
  });
});

test("reports a malformed listed manifest", async () => {
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.includes("/tree/")) {
      return Response.json([
        { type: "file", path: "runs/malformed/run_manifest.json" },
      ]);
    }
    if (url.endsWith("/runs/malformed/run_manifest.json")) {
      return Response.json({
        ...v2RunManifest("malformed", "2026-01-01T00:00:00+00:00"),
        updated_at: null,
      });
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  await expect(loadStagingRuns(0, "uk")).resolves.toMatchObject({
    runs: [],
    incompatible_runs: [
      {
        run_id: "malformed",
        run_manifest_path: "runs/malformed/run_manifest.json",
        detail: expect.stringMatching(/updated_at/),
      },
    ],
  });
});

test("fails explicitly when the staging repository tree cannot be read", async () => {
  globalThis.fetch = (async (_input: RequestInfo | URL) =>
    new Response(null, { status: 500 })) as typeof fetch;

  await expect(loadStagingRuns(0, "uk")).rejects.toThrow(
    /Staging fetch failed 500: runs tree/,
  );
});

test("propagates a repository tree request error", async () => {
  globalThis.fetch = (async (_input: RequestInfo | URL): Promise<Response> => {
    throw new Error("tree request failed");
  }) as typeof fetch;

  await expect(loadStagingRuns(0, "uk")).rejects.toThrow(/tree request failed/);
});

test("reports an authenticated empty staging repository as available", async () => {
  const requested: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input);
    requested.push(url);
    if (url.includes("/tree/")) return Response.json([]);
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  await expect(loadStagingRuns(0, "uk")).resolves.toEqual({
    available: true,
    source_repo: "policyengine/populace-uk-staging",
    revision: "main",
    truncated: false,
    runs: [],
    incompatible_runs: [],
  });
  expect(requested).toHaveLength(1);
  expect(requested[0]).toContain("/tree/");
});

test("reports a staging repository that cannot be read as unavailable", async () => {
  globalThis.fetch = (async (_input: RequestInfo | URL) =>
    new Response(null, { status: 404 })) as typeof fetch;

  await expect(loadStagingRuns(0, "uk")).resolves.toMatchObject({
    available: false,
    source_repo: "policyengine/populace-uk-staging",
    revision: "main",
    detail: expect.stringContaining("is not visible (HTTP 404)"),
    runs: [],
  });
});

test("target-change cache duration follows whether a run can still change", () => {
  expect(stagingTargetChangeCacheTtlSeconds("passed")).toBe(21_600);
  expect(stagingTargetChangeCacheTtlSeconds("published")).toBe(21_600);
  expect(stagingTargetChangeCacheTtlSeconds("completed")).toBe(21_600);
  expect(stagingTargetChangeCacheTtlSeconds("failed")).toBe(21_600);
  expect(stagingTargetChangeCacheTtlSeconds("blocked")).toBe(21_600);
  expect(stagingTargetChangeCacheTtlSeconds("running")).toBe(30);
  expect(stagingTargetChangeCacheTtlSeconds("stalled")).toBe(30);
  expect(stagingTargetChangeCacheTtlSeconds(null)).toBe(30);
});

test("Belgium staging loaders return an empty state before resolving artifacts", async () => {
  const unavailable = {
    available: false as const,
    source_repo: null,
    revision: null,
    detail: "Belgium has no staging repository.",
  };

  expect(await loadStagingRuns(0, "be")).toEqual({
    ...unavailable,
    truncated: false,
    runs: [],
    incompatible_runs: [],
  });
  expect(await loadStagingRun("", 0, "be")).toMatchObject({
    ...unavailable,
    run_id: "",
    has_calibration: false,
    calibration: null,
  });
  expect(
    await loadStagingTargetDiagnostics("http://example.test", "", 0, "be"),
  ).toEqual({ ...unavailable, run_id: "" });
  expect(await loadStagingComparison("", "latest", 0, "be")).toEqual({
    ...unavailable,
    run_id: "",
  });
});

test("orders version 2 runs by date-time instant across UTC offsets", async () => {
  const manifests = new Map([
    [
      "earlier",
      v2RunManifest("earlier", "2026-01-01T01:00:00+01:00"),
    ],
    ["later", v2RunManifest("later", "2026-01-01T00:30:00Z")],
  ]);

  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.includes("/tree/")) {
      return Response.json(
        [...manifests.keys()].map((runId) => ({
          type: "file",
          path: `runs/${runId}/run_manifest.json`,
        })),
      );
    }
    const match = /\/runs\/([^/]+)\/run_manifest\.json$/.exec(url);
    if (match && manifests.has(match[1])) {
      return Response.json(manifests.get(match[1]));
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  const result = await loadStagingRuns(0, "uk");

  expect(result.runs.map((run) => run.run_id)).toEqual(["later", "earlier"]);
});

test("rejects a successful non-array repository tree response", async () => {
  globalThis.fetch = (async (input) => {
    const url = String(input);
    if (url.includes("/tree/")) {
      return Response.json({ entries: [] });
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  await expect(loadStagingRuns(0, "uk")).rejects.toThrow(
    /Incompatible staging data: runs tree response must be an array/,
  );
});

// --- staged dataset bundles (unreleased candidates) -------------------------

const STAGED_RUN_ID = "uk-frs-calibration-attempt-20260920T170811Z-ce339e7c";
const STAGED_REVISION = "40439a3f8d241cd361bb693488b42e7258623a00";
const UK_RELEASE_REPO = "policyengine/populace-uk-private";

function stagedDatasetReceipt(
  runId: string,
  diagnosticsSha256: string,
  overrides: Record<string, unknown> = {},
) {
  return {
    contract_version: 1,
    run_id: runId,
    status: "uploaded",
    mode: "local_and_remote",
    repository: UK_RELEASE_REPO,
    prefix: `staged/${runId}`,
    revision: STAGED_REVISION,
    error_code: null,
    opt_out_reason: null,
    files: {
      "calibration_diagnostics.json": { bytes: 1, sha256: diagnosticsSha256 },
      "microcosm_uk_2024_25.h5": { bytes: 1, sha256: "0".repeat(64) },
    },
    ...overrides,
  };
}

function stagedRun(
  runId: string,
  receipt: Record<string, unknown>,
): { manifest: Record<string, unknown>; receiptBody: string } {
  const receiptBody = JSON.stringify(receipt);
  const manifest = {
    ...v2RunManifest(runId, "2026-09-20T17:16:00+00:00"),
    operation_id: "uk_national_calibration",
    pipeline: { id: "uk-frs-calibration", version: "2026.09" },
    run_kind: "calibration",
    artifacts: [
      {
        logical_name: "staged_dataset",
        artifact_kind: "build_metadata",
        contract_relative_path: "artifacts/staged_dataset.json",
        media_type: "application/json",
        sha256: createHash("sha256").update(receiptBody).digest("hex"),
        classification: "non_row_level",
      },
    ],
  };
  return { manifest, receiptBody };
}

function stagedBundleFetch(
  runId: string,
  manifest: Record<string, unknown>,
  receiptBody: string,
  bundleBody: string | null,
  requested: { url: string; authorization: string | null }[],
  diagnosticsFile = "calibration_diagnostics.json",
): typeof fetch {
  return (async (input, init) => {
    const url = String(input);
    requested.push({ url, authorization: new Headers(init?.headers).get("Authorization") });
    if (url.endsWith(`/runs/${runId}/run_manifest.json`)) return Response.json(manifest);
    if (url.endsWith(`/runs/${runId}/artifacts/staged_dataset.json`)) {
      return new Response(receiptBody, { headers: { "Content-Type": "application/json" } });
    }
    if (
      bundleBody != null &&
      url ===
        `https://huggingface.co/datasets/${UK_RELEASE_REPO}/resolve/${STAGED_REVISION}/staged/${runId}/${diagnosticsFile}`
    ) {
      return new Response(bundleBody, { headers: { "Content-Type": "application/json" } });
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;
}

test("reads an unreleased candidate's calibration diagnostics from its staged bundle", async () => {
  process.env.POPULACE_UK_STAGING_HF_TOKEN = "hf_test_staging_token";
  process.env.HF_TOKEN = "hf_test_release_token";
  const diagnostics = serializedDiagnostics();
  const { manifest, receiptBody } = stagedRun(
    STAGED_RUN_ID,
    stagedDatasetReceipt(STAGED_RUN_ID, diagnostics.sha256),
  );
  const requested: { url: string; authorization: string | null }[] = [];
  globalThis.fetch = stagedBundleFetch(
    STAGED_RUN_ID,
    manifest,
    receiptBody,
    diagnostics.body,
    requested,
  );

  const calibration = await loadStagingCalibration(STAGED_RUN_ID, 0, "uk");
  const detail = await loadStagingRun(STAGED_RUN_ID, 0, "uk");

  expect(calibration?.source).toBe("huggingface_staged_bundle");
  expect(calibration?.release_id).toBe(`${STAGED_RUN_ID}-candidate`);
  expect(calibration?.rows).toHaveLength(1);
  expect(calibration?.rows[0]?.final_estimate).toBe(95);
  expect(detail.has_calibration).toBe(true);
  expect(detail.calibration).not.toBeNull();
  // The bundle is read at the commit the run recorded, with the release
  // credential; the telemetry files keep the staging credential; the legacy
  // runs/<id>/calibration_diagnostics.json path is never tried.
  const bundle = requested.filter((r) => r.url.includes(`/resolve/${STAGED_REVISION}/`));
  // Once per loader call above; the same pinned URL both times.
  expect(new Set(bundle.map((r) => r.url))).toEqual(
    new Set([
      `https://huggingface.co/datasets/${UK_RELEASE_REPO}/resolve/${STAGED_REVISION}/staged/${STAGED_RUN_ID}/calibration_diagnostics.json`,
    ]),
  );
  expect(bundle.length).toBeGreaterThan(0);
  expect(bundle.every((r) => r.authorization === "Bearer hf_test_release_token")).toBe(true);
  expect(
    requested
      .filter((r) => r.url.includes("populace-uk-staging"))
      .every((r) => r.authorization === "Bearer hf_test_staging_token"),
  ).toBe(true);
  expect(
    requested.some((r) => r.url.endsWith(`/runs/${STAGED_RUN_ID}/calibration_diagnostics.json`)),
  ).toBe(false);
  expect(JSON.stringify(detail)).not.toContain("hf_test_");
});

test("a run whose dataset staging was skipped has no calibration to inspect", async () => {
  const diagnostics = serializedDiagnostics();
  const { manifest, receiptBody } = stagedRun(
    STAGED_RUN_ID,
    stagedDatasetReceipt(STAGED_RUN_ID, diagnostics.sha256, {
      status: "skipped",
      repository: null,
      revision: null,
      files: {},
    }),
  );
  const requested: { url: string; authorization: string | null }[] = [];
  globalThis.fetch = stagedBundleFetch(STAGED_RUN_ID, manifest, receiptBody, null, requested);

  expect(await loadStagingCalibration(STAGED_RUN_ID, 0, "uk")).toBeNull();
  expect((await loadStagingRun(STAGED_RUN_ID, 0, "uk")).has_calibration).toBe(false);
  expect(requested.some((r) => r.url.includes(UK_RELEASE_REPO))).toBe(false);
});

test("rejects a staged bundle whose diagnostics bytes do not match the recorded digest", async () => {
  const diagnostics = serializedDiagnostics();
  const { manifest, receiptBody } = stagedRun(
    STAGED_RUN_ID,
    stagedDatasetReceipt(STAGED_RUN_ID, "f".repeat(64)),
  );
  globalThis.fetch = stagedBundleFetch(STAGED_RUN_ID, manifest, receiptBody, diagnostics.body, []);

  await expect(loadStagingCalibration(STAGED_RUN_ID, 0, "uk")).rejects.toThrow(
    "does not match the digest the run recorded",
  );
});

test("never sends the release credential to a repository the telemetry names", async () => {
  process.env.HF_TOKEN = "hf_test_release_token";
  const diagnostics = serializedDiagnostics();
  const { manifest, receiptBody } = stagedRun(
    STAGED_RUN_ID,
    stagedDatasetReceipt(STAGED_RUN_ID, diagnostics.sha256, {
      repository: "someone-else/not-the-release-repository",
    }),
  );
  const requested: { url: string; authorization: string | null }[] = [];
  globalThis.fetch = stagedBundleFetch(STAGED_RUN_ID, manifest, receiptBody, diagnostics.body, requested);

  await expect(loadStagingCalibration(STAGED_RUN_ID, 0, "uk")).rejects.toThrow(
    "registered release repository",
  );
  expect(requested.some((r) => r.url.includes("someone-else"))).toBe(false);
});

test("a staged bundle without calibration diagnostics has no target view", async () => {
  const { manifest, receiptBody } = stagedRun(
    STAGED_RUN_ID,
    stagedDatasetReceipt(STAGED_RUN_ID, "0".repeat(64), {
      files: { "spine.h5": { bytes: 1, sha256: "0".repeat(64) } },
    }),
  );
  const requested: { url: string; authorization: string | null }[] = [];
  globalThis.fetch = stagedBundleFetch(STAGED_RUN_ID, manifest, receiptBody, null, requested);

  expect(await loadStagingCalibration(STAGED_RUN_ID, 0, "uk")).toBeNull();
  expect(requested.some((r) => r.url.includes(UK_RELEASE_REPO))).toBe(false);
});

// Microcosm's UK local lane stages its outputs under the dataset's stem.
const LOCAL_RUN_ID = "uk-local-candidate-f100-s42-20261019T000432Z-0c0c0c0c";
const LOCAL_DIAGNOSTICS = "microcosm_uk_2024_25_local.diagnostics.json";

test("reads a local candidate's diagnostics staged under the dataset's stem", async () => {
  process.env.HF_TOKEN = "hf_test_release_token";
  const diagnostics = serializedDiagnostics();
  const { manifest, receiptBody } = stagedRun(
    LOCAL_RUN_ID,
    stagedDatasetReceipt(LOCAL_RUN_ID, diagnostics.sha256, {
      files: {
        [LOCAL_DIAGNOSTICS]: { bytes: 1, sha256: diagnostics.sha256 },
        "microcosm_uk_2024_25_local.h5": { bytes: 1, sha256: "0".repeat(64) },
        "uk.full.gates.calibrated.gate_report.json": { bytes: 1, sha256: "0".repeat(64) },
      },
    }),
  );
  const requested: { url: string; authorization: string | null }[] = [];
  globalThis.fetch = stagedBundleFetch(
    LOCAL_RUN_ID,
    manifest,
    receiptBody,
    diagnostics.body,
    requested,
    LOCAL_DIAGNOSTICS,
  );

  const source = await resolveStagingCalibrationSource(LOCAL_RUN_ID, 0, "uk");

  expect(source?.calibration.rows).toHaveLength(1);
  expect(source?.sourceArtifacts.calibrationDiagnostics.path).toBe(
    `staged/${LOCAL_RUN_ID}/${LOCAL_DIAGNOSTICS}`,
  );
  expect(requested.map((r) => r.url)).toContain(
    `https://huggingface.co/datasets/${UK_RELEASE_REPO}/resolve/${STAGED_REVISION}/staged/${LOCAL_RUN_ID}/${LOCAL_DIAGNOSTICS}`,
  );
});

test("the staged diagnostics file is the canonical name, or else the one stem file", () => {
  const file = { bytes: 1, sha256: "0".repeat(64) };
  expect(stagedBundleDiagnosticsFile({
    "calibration_diagnostics.json": file,
    [LOCAL_DIAGNOSTICS]: file,
  })).toBe("calibration_diagnostics.json");
  expect(stagedBundleDiagnosticsFile({ [LOCAL_DIAGNOSTICS]: file, "data.h5": file }))
    .toBe(LOCAL_DIAGNOSTICS);
  // Two stem files, a recorded name with a path, or none: no target view.
  expect(stagedBundleDiagnosticsFile({
    [LOCAL_DIAGNOSTICS]: file,
    "microcosm_uk_2024_25_dense.diagnostics.json": file,
  })).toBeNull();
  expect(stagedBundleDiagnosticsFile({ "../other.diagnostics.json": file })).toBeNull();
  expect(stagedBundleDiagnosticsFile({ "spine.h5": file })).toBeNull();
  expect(stagedBundleDiagnosticsFile(null)).toBeNull();
});

// Codex review of #223: a path-containing stem name was dropped before the
// count, so it no longer made the receipt ambiguous.
test("a receipt with one valid stem file and one stem name containing a path has no diagnostics file", () => {
  const file = { bytes: 1, sha256: "0".repeat(64) };
  expect(stagedBundleDiagnosticsFile({
    [LOCAL_DIAGNOSTICS]: file,
    "nested/dense.diagnostics.json": file,
  })).toBeNull();
  expect(stagedBundleDiagnosticsFile({
    "../other.diagnostics.json": file,
    [LOCAL_DIAGNOSTICS]: file,
  })).toBeNull();
});
