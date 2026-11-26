// A US fiscal refresh build's staging run as the version 2 writer stages it
// (PolicyEngine/microcosm#1135): its documents at the run folder's top level,
// and each result file at `artifacts/<logical name>.json`, declared in the run
// manifest with its SHA-256. Version 1 runs put `build_manifest.json`,
// `release_manifest.json` and `reform_validation.json` at the top level.
import { createHash } from "node:crypto";

export const US_VERSION_2_RUN_ID = "us-fiscal-refresh-20261127T090000Z-1a2b3c4d";
export const US_VERSION_2_CANDIDATE = "microcosm-us-2025";

const UPDATED_AT = "2026-11-27T11:00:00+00:00";
const STARTED_AT = "2026-11-27T09:00:00+00:00";

function sha256(text: string): string {
  return createHash("sha256").update(text).digest("hex");
}

/** The result files the run stages, by logical name, as JSON text. */
export function usVersion2ResultFiles(): Record<string, string> {
  return {
    calibration_diagnostics: JSON.stringify({
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
          name: "irs_soi/adjusted_gross_income/total@2025",
          target_name: "irs_soi/adjusted_gross_income/total",
          period: 2025,
          entity: "household",
          source: "irs_soi",
          metadata: {
            variable: "adjusted_gross_income",
            geography: "United States",
            geography_level: "national",
          },
          target: 100,
          initial_estimate: 80,
          final_estimate: 95,
          relative_error: -0.05,
          within_tolerance: true,
        },
      ],
    }),
    build_manifest: JSON.stringify({
      build_id: "us-fiscal-refresh-build-1a2b3c4d",
      candidate_release_id: US_VERSION_2_CANDIDATE,
      code_commit: "c".repeat(40),
    }),
    release_manifest: JSON.stringify({
      release_id: US_VERSION_2_CANDIDATE,
      build_id: "us-fiscal-refresh-build-1a2b3c4d",
    }),
    reform_validation: JSON.stringify({
      schema_version: 1,
      release_id: US_VERSION_2_CANDIDATE,
      baseline_period: 2025,
      scoring_window: "2025",
      reforms: [],
    }),
  };
}

const ARTIFACT_REVIEW: Record<string, { kind: string; classification: string }> = {
  calibration_diagnostics: { kind: "aggregate_diagnostics", classification: "aggregate" },
  build_manifest: { kind: "build_metadata", classification: "non_row_level" },
  release_manifest: { kind: "build_metadata", classification: "non_row_level" },
  reform_validation: { kind: "validation_summary", classification: "aggregate" },
};

function identity(runId: string) {
  return {
    run_id: runId,
    country_code: "US",
    operation_id: "us_fiscal_refresh_build",
    pipeline: { id: "us_fiscal_refresh", version: "0.40.0" },
    candidate_id: US_VERSION_2_CANDIDATE,
    release_id: US_VERSION_2_CANDIDATE,
    run_kind: "release",
    non_release: false,
    started_at: STARTED_AT,
    updated_at: UPDATED_AT,
    status: "completed",
    current_stage: "complete",
  };
}

function delivery(runId: string) {
  return {
    contract_version: 2,
    enabled: true,
    mode: "local_and_remote",
    run_id: runId,
    configured_repository: "policyengine/populace-us-staging",
    upload_attempts: 6,
    upload_successes: 6,
    read_back: "passed",
    last_error_code: null,
    opt_out_reason: null,
  };
}

/**
 * The run's files by their path inside its folder. `declared`, by logical
 * name, is what the manifest declares, each with the SHA-256 of its text;
 * by default, the result files the folder holds.
 */
export function usVersion2RunFolder(
  options: {
    runId?: string;
    resultFiles?: Record<string, string>;
    declared?: Record<string, string>;
  } = {},
): Record<string, string> {
  const runId = options.runId ?? US_VERSION_2_RUN_ID;
  const resultFiles = options.resultFiles ?? usVersion2ResultFiles();
  const declared = options.declared ?? resultFiles;
  const manifest = {
    schema_name: "microcosm.staging.run-manifest",
    schema_version: 2,
    ...identity(runId),
    sample: { mode: "full" },
    delivery: delivery(runId),
    artifacts: Object.entries(declared).map(([name, text]) => ({
      logical_name: name,
      artifact_kind: ARTIFACT_REVIEW[name]?.kind ?? "validation_summary",
      contract_relative_path: `artifacts/${name}.json`,
      media_type: "application/json",
      sha256: sha256(text),
      classification: ARTIFACT_REVIEW[name]?.classification ?? "aggregate",
    })),
    failure: null,
    paths: {
      progress: `runs/${runId}/progress.json`,
      events: `runs/${runId}/events.ndjson`,
      calibration_progress: null,
    },
  };
  const progress = {
    schema_name: "microcosm.staging.progress",
    schema_version: 2,
    ...identity(runId),
    sample: { mode: "full" },
    delivery: delivery(runId),
    message: "Staging run completed.",
    details: {},
    failure: null,
  };
  const events = [
    ["created", "started", "Staging run created.", STARTED_AT],
    ["calibrate", "completed", null, "2026-11-27T10:00:00+00:00"],
    ["reform_validation", "completed", null, "2026-11-27T10:30:00+00:00"],
    ["complete", "completed", "Staging run completed.", UPDATED_AT],
  ].map(([stage, status, message, timestamp], index) =>
    JSON.stringify({
      schema_name: "microcosm.staging.event",
      schema_version: 2,
      sequence: index + 1,
      timestamp,
      event_type: "stage",
      run_id: runId,
      stage_id: stage,
      status,
      message,
      details: {},
    })
  );
  return {
    "run_manifest.json": JSON.stringify(manifest),
    "progress.json": JSON.stringify(progress),
    "events.ndjson": `${events.join("\n")}\n`,
    ...Object.fromEntries(
      Object.entries(resultFiles).map(([name, text]) => [`artifacts/${name}.json`, text]),
    ),
  };
}
