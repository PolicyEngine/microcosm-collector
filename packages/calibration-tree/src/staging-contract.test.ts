import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, test } from "bun:test";

import {
  IncompatibleStagingDataError,
  parseStagingCalibrationProgress,
  parseStagingEvents,
  parseStagingManifest,
  parseStagingProgress,
  parseStagingRunIndex,
  validateStagingRunConsistency,
} from "./staging-contract";

const FIXTURES = join(import.meta.dir, "fixtures", "staging-contract");
// Version 3 is copied byte for byte from microcosm
// packages/microcosm-build/tests/fixtures/staging/v3.
const MANIFEST_DIGESTS = {
  v1: "165d24caf29b82afdb0ce241b65d088da552abd59d6ddabf4b2183b9a61be75b",
  v2: "0d54c630088152b4fb4a7b7a9853c851233d0be76579f949331ae4bcd7b455e5",
  v3: "e31a5e337840e24d7a1203b83eb1a5039eeb5f35777a895ab6ab28163bf3def5",
};

type FixtureVersion = keyof typeof MANIFEST_DIGESTS;

function bytes(version: FixtureVersion, path: string): Buffer {
  return readFileSync(join(FIXTURES, version, path));
}

function json(version: FixtureVersion, path: string): Record<string, unknown> {
  return JSON.parse(bytes(version, path).toString());
}

for (const version of ["v1", "v2", "v3"] as const) {
  test(`${version} fixture files match the Microcosm digest manifest`, () => {
    const manifest = bytes(version, "SHA256SUMS");
    expect(createHash("sha256").update(manifest).digest("hex")).toBe(
      MANIFEST_DIGESTS[version],
    );
    for (const line of manifest.toString().trim().split("\n")) {
      const [expected, path] = line.split("  ", 2);
      expect(createHash("sha256").update(bytes(version, path)).digest("hex")).toBe(
        expected,
      );
    }
  });
}

describe("version 1", () => {
  test("preserves current summaries and unversioned events", () => {
    const runs = parseStagingRunIndex(json("v1", "runs.json"));
    const progress = parseStagingProgress(json("v1", "progress.json"));
    const manifest = parseStagingManifest(json("v1", "run_manifest.json"));
    const calibration = parseStagingCalibrationProgress(
      json("v1", "calibration_progress.json"),
    );
    const events = parseStagingEvents(bytes("v1", "events.ndjson").toString());

    expect(runs[0]).toMatchObject({
      run_id: "v1-us-fixture",
      candidate_release_id: "populace-us-2024-v1-fixture",
      status: "passed",
      stage: "complete",
      schema_version: 1,
    });
    expect(progress.candidate_release_id).toBe("populace-us-2024-v1-fixture");
    expect(manifest.artifacts).toEqual({});
    expect((calibration.events as unknown[]).length).toBe(1);
    expect(events.map((event) => event.stage)).toEqual([
      "created",
      "calibrating",
      "complete",
    ]);
    expect(events.every((event) => event.schema_version == null)).toBe(true);
  });
});

describe("version 2", () => {
  const runRoot = "completed-spine/runs/uk-spine-v2-fixture";

  test("does not accept the removed shared run index contract", () => {
    expect(() =>
      parseStagingRunIndex({
        schema_name: "microcosm.staging.run-index",
        schema_version: 2,
        runs: [],
      }),
    ).toThrow(/run index must use schema version 1/);
  });

  test("normalizes identities, lifecycle, sampling, delivery, and events", () => {
    const progress = parseStagingProgress(json("v2", `${runRoot}/progress.json`));
    const manifest = parseStagingManifest(json("v2", `${runRoot}/run_manifest.json`));
    const events = parseStagingEvents(bytes("v2", `${runRoot}/events.ndjson`).toString());

    expect(manifest).toMatchObject({
      run_id: "uk-spine-v2-fixture",
      candidate_release_id: "uk-spine-v2-fixture",
      release_id: null,
      country_code: "GB",
      run_kind: "smoke",
      non_release: true,
      status: "completed",
      stage: "complete",
      schema_version: 2,
    });
    expect(progress).toMatchObject({
      candidate_release_id: "uk-spine-v2-fixture",
      stage: "complete",
      status: "completed",
      non_release: true,
    });
    expect(progress.delivery).toMatchObject({ mode: "local_only", contract_version: 2 });
    expect(progress.sample).toEqual({ mode: "full" });
    expect(manifest.artifacts).toEqual({});
    expect(events[1]).toMatchObject({
      sequence: 2,
      stage: "input_verification",
      time: "2026-01-02T00:00:01+00:00",
      status: "completed",
    });
  });

  test("normalizes calibration data and preserves sanitized failures", () => {
    const calibration = parseStagingCalibrationProgress(
      json(
        "v2",
        "calibration/runs/uk-calibration-v2-fixture/calibration_progress.json",
      ),
    );
    const failed = parseStagingProgress(
      json("v2", "failed/runs/uk-failed-v2-fixture/progress.json"),
    );
    const event = (calibration.events as Record<string, unknown>[])[0];

    expect(event.time).toBe(event.timestamp);
    expect(calibration.candidate_release_id).toBe("uk-calibration-candidate");
    expect(failed.status).toBe("failed");
    expect(failed.failure).toMatchObject({
      error_code: "BUILD_FAILED",
      error_type: "RuntimeError",
    });
  });

  test("rejects invalid run manifest date-time strings", () => {
    const source = json("v2", `${runRoot}/run_manifest.json`);
    const invalidTimestamps = [
      ["started_at", "not-a-date"],
      ["updated_at", "2026-02-30T00:00:00+00:00"],
      ["updated_at", "2026-01-01 00:00:00"],
      ["updated_at", "2026-01-01T00:00:00"],
    ] as const;

    for (const [field, value] of invalidTimestamps) {
      expect(() => parseStagingManifest({ ...source, [field]: value })).toThrow(
        new RegExp(`${field} must be a valid RFC 3339 date-time string`),
      );
    }
  });

  test("accepts reviewed typed artifacts and rejects unknown artifact kinds", () => {
    const source = json("v2", `${runRoot}/run_manifest.json`);
    const artifact = {
      logical_name: "validation-summary",
      artifact_kind: "validation_summary",
      contract_relative_path: "artifacts/validation-summary.json",
      media_type: "application/json",
      sha256: "a".repeat(64),
      classification: "aggregate",
    };
    expect(
      (parseStagingManifest({ ...source, artifacts: [artifact] }).artifacts as Record<
        string,
        unknown
      >)["validation-summary"],
    ).toMatchObject({
      ...artifact,
      staging_path:
        "runs/uk-spine-v2-fixture/artifacts/validation-summary.json",
    });
    expect(() =>
      parseStagingManifest({
        ...source,
        artifacts: [{ ...artifact, artifact_kind: "population_rows" }],
      }),
    ).toThrow(IncompatibleStagingDataError);
    expect(() =>
      parseStagingManifest({
        ...source,
        artifacts: [artifact, artifact],
      }),
    ).toThrow(/appears more than once/);
    expect(() =>
      parseStagingManifest({
        ...source,
        artifacts: [{ ...artifact, sha256: "not-a-digest" }],
      }),
    ).toThrow(/invalid digest/);
  });

  test("reports unknown versions as incompatible data", () => {
    const cases = json("v2", "contract-cases.json");
    expect(() => parseStagingManifest(cases.unknown_version)).toThrow(
      /Incompatible staging data: unsupported manifest schema version 999/,
    );
  });

  test("rejects missing and additional fields in run documents and nested objects", () => {
    const manifest = json("v2", `${runRoot}/run_manifest.json`);
    const withoutOperation = { ...manifest };
    delete withoutOperation.operation_id;
    expect(() => parseStagingManifest(withoutOperation)).toThrow(
      /run manifest is missing operation_id/,
    );
    expect(() =>
      parseStagingManifest({ ...manifest, undocumented: true }),
    ).toThrow(/unexpected field undocumented/);
    expect(() =>
      parseStagingManifest({
        ...manifest,
        pipeline: { ...(manifest.pipeline as Record<string, unknown>), extra: true },
      }),
    ).toThrow(/pipeline contains unexpected field extra/);
    expect(() =>
      parseStagingProgress({
        ...json("v2", `${runRoot}/progress.json`),
        details: [],
      }),
    ).toThrow(/progress details must be an object/);
  });

  test("enforces version 2 semantic relationships", () => {
    const manifest = json("v2", `${runRoot}/run_manifest.json`);
    const delivery = manifest.delivery as Record<string, unknown>;
    expect(() =>
      parseStagingManifest({ ...manifest, non_release: false }),
    ).toThrow(/non_release must be true exactly when release_id is absent/);
    expect(() =>
      parseStagingManifest({
        ...manifest,
        delivery: { ...delivery, upload_attempts: 0, upload_successes: 1 },
      }),
    ).toThrow(/upload_successes cannot exceed upload_attempts/);
    expect(() =>
      parseStagingManifest({
        ...manifest,
        delivery: { ...delivery, run_id: "another-run" },
      }),
    ).toThrow(/delivery run_id does not match/);
    expect(() =>
      parseStagingManifest({
        ...manifest,
        status: "failed",
        current_stage: "failed",
      }),
    ).toThrow(/failed status requires failure data/);
  });

  test("fully validates calibration progress records", () => {
    const calibration = json(
      "v2",
      "calibration/runs/uk-calibration-v2-fixture/calibration_progress.json",
    );
    const event = (calibration.events as Record<string, unknown>[])[0];
    expect(() =>
      parseStagingCalibrationProgress({
        ...calibration,
        updated_at: "2026-02-30T00:00:00Z",
      }),
    ).toThrow(/updated_at must be a valid RFC 3339 date-time string/);
    expect(() =>
      parseStagingCalibrationProgress({
        ...calibration,
        events: [{ ...event, epoch: -1 }],
      }),
    ).toThrow(/epoch must be an integer greater than or equal to 0/);
    expect(() =>
      parseStagingCalibrationProgress({
        ...calibration,
        events: [{ ...event, extra: true }],
      }),
    ).toThrow(/calibration event 0 contains unexpected field extra/);
  });

  test("fully validates individual event records", () => {
    const event = JSON.parse(
      bytes("v2", `${runRoot}/events.ndjson`).toString().trim().split("\n")[0],
    ) as Record<string, unknown>;
    expect(() =>
      parseStagingEvents(JSON.stringify({ ...event, timestamp: "not-a-date" })),
    ).toThrow(/timestamp must be a valid RFC 3339 date-time string/);
    expect(() =>
      parseStagingEvents(JSON.stringify({ ...event, status: "waiting" })),
    ).toThrow(/status is unsupported/);
    expect(() =>
      parseStagingEvents(JSON.stringify({ ...event, extra: true })),
    ).toThrow(/event 0 contains unexpected field extra/);
  });

  test("accepts lifecycle differences while enforcing stable run identity", () => {
    const manifest = parseStagingManifest(
      json("v2", `${runRoot}/run_manifest.json`),
    );
    const progressSource = json("v2", `${runRoot}/progress.json`);
    const delivery = progressSource.delivery as Record<string, unknown>;
    const progress = parseStagingProgress({
      ...progressSource,
      status: "running",
      current_stage: "validation",
      updated_at: "2026-01-02T00:00:05+00:00",
      message: "Validation is running.",
      delivery: {
        ...delivery,
        upload_attempts: 2,
        upload_successes: 1,
        read_back: "passed",
      },
    });
    const events = parseStagingEvents(
      bytes("v2", `${runRoot}/events.ndjson`).toString(),
    );

    expect(() =>
      validateStagingRunConsistency("uk-spine-v2-fixture", {
        progress,
        runManifest: manifest,
        events,
      }),
    ).not.toThrow();

    const mismatchedProgress = parseStagingProgress({
      ...progressSource,
      candidate_id: "different-candidate",
    });
    expect(() =>
      validateStagingRunConsistency("uk-spine-v2-fixture", {
        progress: mismatchedProgress,
        runManifest: manifest,
      }),
    ).toThrow(/disagree on stable field candidate_id/);
  });

  test("validates paths and identities across all available run files", () => {
    const manifestSource = json("v2", `${runRoot}/run_manifest.json`);
    const manifest = parseStagingManifest(manifestSource);
    const events = parseStagingEvents(
      bytes("v2", `${runRoot}/events.ndjson`).toString(),
    );
    expect(() =>
      validateStagingRunConsistency("uk-spine-v2-fixture", {
        runManifest: parseStagingManifest({
          ...manifestSource,
          paths: {
            ...(manifestSource.paths as Record<string, unknown>),
            events: "runs/another-run/events.ndjson",
          },
        }),
      }),
    ).toThrow(/paths do not identify the requested run/);
    expect(() =>
      validateStagingRunConsistency("uk-spine-v2-fixture", {
        runManifest: manifest,
        events: [{ ...events[0], run_id: "another-run" }],
      }),
    ).toThrow(/event 0 identifies run another-run/);

    const calibrationRoot = "calibration/runs/uk-calibration-v2-fixture";
    const calibrationManifest = parseStagingManifest(
      json("v2", `${calibrationRoot}/run_manifest.json`),
    );
    const calibrationProgress = parseStagingCalibrationProgress({
      ...json("v2", `${calibrationRoot}/calibration_progress.json`),
      candidate_id: "another-candidate",
    });
    expect(() =>
      validateStagingRunConsistency("uk-calibration-v2-fixture", {
        runManifest: calibrationManifest,
        calibrationProgress,
      }),
    ).toThrow(/disagree on candidate_id/);
  });
});

describe("version 3", () => {
  const blockedRoot = "blocked/runs/uk-blocked-v3-fixture";
  const failedRoot = "failed/runs/uk-failed-v3-fixture";
  const runs = [
    ["uk-spine-v3-fixture", "completed-spine/runs/uk-spine-v3-fixture"],
    ["uk-calibration-v3-fixture", "calibration/runs/uk-calibration-v3-fixture"],
    ["uk-failed-v3-fixture", failedRoot],
    ["uk-blocked-v3-fixture", blockedRoot],
  ] as const;

  test("reads every fixture run as one consistent version 3 run", () => {
    for (const [runId, root] of runs) {
      const manifest = parseStagingManifest(json("v3", `${root}/run_manifest.json`));
      const paths = manifest.paths as Record<string, unknown>;
      expect(() =>
        validateStagingRunConsistency(runId, {
          progress: parseStagingProgress(json("v3", `${root}/progress.json`)),
          runManifest: manifest,
          calibrationProgress: paths.calibration_progress
            ? parseStagingCalibrationProgress(
                json("v3", `${root}/calibration_progress.json`),
              )
            : null,
          events: parseStagingEvents(bytes("v3", `${root}/events.ndjson`).toString()),
        }),
      ).not.toThrow();
      expect(manifest.schema_version).toBe(3);
    }
  });

  test("a blocked run carries its block and ends with a blocked event", () => {
    const block = {
      phase: "terminal",
      blocking_failure_count: 1,
      blocking_gate_ids: ["uk_target_fit"],
    };
    const manifest = parseStagingManifest(json("v3", `${blockedRoot}/run_manifest.json`));
    const progress = parseStagingProgress(json("v3", `${blockedRoot}/progress.json`));
    const events = parseStagingEvents(
      bytes("v3", `${blockedRoot}/events.ndjson`).toString(),
    );

    expect(manifest).toMatchObject({ status: "blocked", failure: null, block });
    expect(progress).toMatchObject({ status: "blocked", stage: "blocked", failure: null, block });
    expect(events[events.length - 1]).toMatchObject({
      stage: "blocked",
      status: "blocked",
      details: { ...block, gate_statuses: { uk_target_fit: "failed" } },
    });
  });

  test("a failure carries its class, and a block may name no gate", () => {
    const failed = parseStagingProgress(json("v3", `${failedRoot}/progress.json`));
    expect(failed.failure).toMatchObject({
      error_code: "BUILD_FAILED",
      failure_class: "error",
    });
    expect(failed.block).toBeNull();

    const source = json("v3", `${blockedRoot}/progress.json`);
    const unnamed = parseStagingProgress({
      ...source,
      block: { phase: "preflight", blocking_failure_count: 2, blocking_gate_ids: [] },
    });
    expect(unnamed.block).toEqual({
      phase: "preflight",
      blocking_failure_count: 2,
      blocking_gate_ids: [],
    });
    expect(
      parseStagingProgress({
        ...json("v3", `${failedRoot}/progress.json`),
        failure: {
          ...(failed.failure as Record<string, unknown>),
          failure_class: null,
        },
      }).failure,
    ).toMatchObject({ failure_class: null });
  });

  test("rejects failures and blocks that break the version 3 rules", () => {
    const failedSource = json("v3", `${failedRoot}/progress.json`);
    const failure = failedSource.failure as Record<string, unknown>;
    const blockedSource = json("v3", `${blockedRoot}/progress.json`);
    const block = blockedSource.block as Record<string, unknown>;
    const withoutBlock = { ...blockedSource };
    delete withoutBlock.block;
    const { failure_class: _class, ...version2Failure } = failure;

    const cases: [Record<string, unknown>, RegExp][] = [
      [
        { ...failedSource, failure: { ...failure, traceback: "File \"x.py\"" } },
        /version 3 failure contains unexpected field traceback/,
      ],
      [{ ...failedSource, failure: version2Failure }, /version 3 failure is missing failure_class/],
      [
        { ...failedSource, failure: { ...failure, failure_class: "OutOfMemory" } },
        /failure_class is invalid/,
      ],
      [{ ...failedSource, block }, /non-blocked status cannot contain block data/],
      [{ ...blockedSource, block: null }, /blocked status requires block data/],
      [
        { ...blockedSource, failure, current_stage: "blocked" },
        /non-failed status cannot contain failure data/,
      ],
      [withoutBlock, /version 3 progress is missing block/],
      [
        { ...blockedSource, block: { ...block, blocking_failure_count: 0 } },
        /blocking_failure_count must be an integer greater than or equal to 1/,
      ],
      [
        { ...blockedSource, block: { ...block, blocking_gate_ids: [""] } },
        /blocking_gate_ids 0 must be a string between 1 and 200 characters/,
      ],
      [
        { ...blockedSource, block: { ...block, gate_statuses: {} } },
        /version 3 block contains unexpected field gate_statuses/,
      ],
      [
        { ...blockedSource, block: { ...block, phase: "pre flight" } },
        /block phase must be a safe identifier/,
      ],
    ];
    for (const [payload, error] of cases) {
      expect(() => parseStagingProgress(payload)).toThrow(error);
    }
    expect(() =>
      parseStagingManifest({
        ...json("v3", `${failedRoot}/run_manifest.json`),
        block,
      }),
    ).toThrow(/non-blocked status cannot contain block data/);
  });

  test("version 2 documents do not accept version 3 fields", () => {
    const root = "completed-spine/runs/uk-spine-v2-fixture";
    const progress = json("v2", `${root}/progress.json`);
    const failedProgress = json("v2", "failed/runs/uk-failed-v2-fixture/progress.json");
    const event = JSON.parse(
      bytes("v2", `${root}/events.ndjson`).toString().trim().split("\n")[0],
    ) as Record<string, unknown>;

    expect(() => parseStagingProgress({ ...progress, block: null })).toThrow(
      /version 2 progress contains unexpected field block/,
    );
    expect(() =>
      parseStagingProgress({ ...progress, status: "blocked", current_stage: "blocked" }),
    ).toThrow(/progress status is unsupported/);
    expect(() =>
      parseStagingProgress({
        ...failedProgress,
        failure: {
          ...(failedProgress.failure as Record<string, unknown>),
          failure_class: "error",
        },
      }),
    ).toThrow(/version 2 failure contains unexpected field failure_class/);
    expect(() =>
      parseStagingEvents(JSON.stringify({ ...event, status: "blocked" })),
    ).toThrow(/status is unsupported/);
  });

  test("events stay contiguous and a run cannot mix versions 2 and 3", () => {
    const lines = bytes("v3", `${blockedRoot}/events.ndjson`).toString().trim().split("\n");
    expect(() => parseStagingEvents([lines[0], lines[2]].join("\n"))).toThrow(
      /version 3 event sequence is not contiguous/,
    );

    const v2Root = "completed-spine/runs/uk-spine-v2-fixture";
    const v3Root = "completed-spine/runs/uk-spine-v3-fixture";
    const v2Progress = parseStagingProgress({
      ...json("v2", `${v2Root}/progress.json`),
      run_id: "uk-spine-v3-fixture",
      candidate_id: "uk-spine-v3-fixture",
      delivery: {
        ...(json("v2", `${v2Root}/progress.json`).delivery as Record<string, unknown>),
        run_id: "uk-spine-v3-fixture",
      },
    });
    expect(() =>
      validateStagingRunConsistency("uk-spine-v3-fixture", {
        progress: v2Progress,
        runManifest: parseStagingManifest(json("v3", `${v3Root}/run_manifest.json`)),
      }),
    ).toThrow(/a run cannot mix document versions 2 and 3/);
  });
});

test("version 1 failure tracebacks are not served", () => {
  const failed = {
    time: "2026-09-26T13:48:00+00:00",
    type: "stage",
    status: "failed",
    stage: "failed",
    message: "Release gates failed",
    details: { error_type: "RuntimeError", traceback: "File \"/Users/builder/run.py\", line 1" },
  };
  const [event] = parseStagingEvents(`${JSON.stringify(failed)}\n`);
  expect(event.details).toEqual({ error_type: "RuntimeError" });
  expect(event.message).toBe("Release gates failed");

  const progress = parseStagingProgress({
    schema_version: 1,
    run_id: "run-a",
    status: "failed",
    stage: "failed",
    details: failed.details,
  });
  expect(progress.details).toEqual({ error_type: "RuntimeError" });
});
