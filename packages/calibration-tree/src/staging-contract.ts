/** Explicit version dispatch and UI normalization for Microcosm staging data. */

export type JsonObject = Record<string, unknown>;

export class IncompatibleStagingDataError extends Error {
  constructor(message: string) {
    super(`Incompatible staging data: ${message}`);
    this.name = "IncompatibleStagingDataError";
  }
}

// Versions 2 and 3 share these names. Version 3 adds the `blocked` run status
// with its `block` details, and `failure_class` on failures.
const STRUCTURED_SCHEMAS = {
  manifest: "microcosm.staging.run-manifest",
  progress: "microcosm.staging.progress",
  calibration: "microcosm.staging.calibration-progress",
  event: "microcosm.staging.event",
} as const;

function objectValue(value: unknown, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new IncompatibleStagingDataError(`${label} must be an object.`);
  }
  return value as JsonObject;
}

function stringValue(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new IncompatibleStagingDataError(`${label} must be a non-empty string.`);
  }
  return value;
}

const RFC_3339_DATE_TIME =
  /^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/;

function dateTimeValue(value: unknown, label: string): string {
  const dateTime = stringValue(value, label);
  const match = RFC_3339_DATE_TIME.exec(dateTime);
  if (!match) {
    throw new IncompatibleStagingDataError(
      `${label} must be a valid RFC 3339 date-time string.`,
    );
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [
    31,
    leapYear ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31,
  ];

  if (
    day > daysInMonth[month - 1] ||
    !Number.isFinite(Date.parse(dateTime))
  ) {
    throw new IncompatibleStagingDataError(
      `${label} must be a valid RFC 3339 date-time string.`,
    );
  }
  return dateTime;
}

function nullableString(value: unknown, label: string): string | null {
  return value == null ? null : stringValue(value, label);
}

const SAFE_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const SHA_256 = /^[0-9a-f]{64}$/;

function assertExactKeys(
  value: JsonObject,
  label: string,
  expectedKeys: readonly string[],
): void {
  const expected = new Set(expectedKeys);
  for (const key of expected) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) {
      throw new IncompatibleStagingDataError(`${label} is missing ${key}.`);
    }
  }
  for (const key of Object.keys(value)) {
    if (!expected.has(key)) {
      throw new IncompatibleStagingDataError(`${label} contains unexpected field ${key}.`);
    }
  }
}

function safeIdentifier(value: unknown, label: string): string {
  if (typeof value !== "string" || !SAFE_IDENTIFIER.test(value)) {
    throw new IncompatibleStagingDataError(`${label} must be a safe identifier.`);
  }
  return value;
}

function nullableSafeIdentifier(value: unknown, label: string): string | null {
  return value === null ? null : safeIdentifier(value, label);
}

function enumValue<const T extends string>(
  value: unknown,
  label: string,
  choices: readonly T[],
): T {
  if (typeof value !== "string" || !choices.includes(value as T)) {
    throw new IncompatibleStagingDataError(`${label} is unsupported.`);
  }
  return value as T;
}

function booleanValue(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new IncompatibleStagingDataError(`${label} must be boolean.`);
  }
  return value;
}

function integerValue(value: unknown, label: string, minimum = 0): number {
  if (!Number.isInteger(value) || Number(value) < minimum) {
    throw new IncompatibleStagingDataError(
      `${label} must be an integer greater than or equal to ${minimum}.`,
    );
  }
  return Number(value);
}

function nullableInteger(value: unknown, label: string): number | null {
  return value === null ? null : integerValue(value, label);
}

function nullableNumber(value: unknown, label: string): number | null {
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new IncompatibleStagingDataError(`${label} must be a finite number or null.`);
  }
  return value;
}

function boundedString(
  value: unknown,
  label: string,
  options: { minimum?: number; maximum?: number } = {},
): string {
  const minimum = options.minimum ?? 0;
  const maximum = options.maximum ?? Number.POSITIVE_INFINITY;
  if (
    typeof value !== "string" ||
    value.length < minimum ||
    value.length > maximum
  ) {
    throw new IncompatibleStagingDataError(
      `${label} must be a string between ${minimum} and ${maximum} characters.`,
    );
  }
  return value;
}

function nullableBoundedString(
  value: unknown,
  label: string,
  options: { minimum?: number; maximum?: number } = {},
): string | null {
  return value === null ? null : boundedString(value, label, options);
}

export type StagingSchemaVersion = 1 | 2 | 3;

// Versions 2 and later are the structured contract; version 1 is the US
// transition layout.
export function isStructuredStagingDocument(
  document: JsonObject | null | undefined,
): boolean {
  return typeof document?.schema_version === "number" && document.schema_version >= 2;
}

function version(
  payload: JsonObject,
  kind: keyof typeof STRUCTURED_SCHEMAS,
  options: { allowUnversionedV1?: boolean } = {},
): StagingSchemaVersion {
  if (payload.schema_version === 1) {
    if (payload.schema_name != null) {
      throw new IncompatibleStagingDataError(
        `version 1 ${kind} data must not declare schema_name.`,
      );
    }
    return 1;
  }
  if (payload.schema_version === 2 || payload.schema_version === 3) {
    if (payload.schema_name !== STRUCTURED_SCHEMAS[kind]) {
      throw new IncompatibleStagingDataError(
        `expected ${STRUCTURED_SCHEMAS[kind]} version ${payload.schema_version}, received ${String(payload.schema_name)}.`,
      );
    }
    return payload.schema_version;
  }
  if (options.allowUnversionedV1 && payload.schema_version == null && payload.schema_name == null) {
    return 1;
  }
  throw new IncompatibleStagingDataError(
    `unsupported ${kind} schema version ${String(payload.schema_version)}.`,
  );
}

export interface ParsedStagingRunSummary {
  run_id: string;
  candidate_release_id: string | null;
  release_id: string | null;
  country_code: string | null;
  run_kind: string | null;
  non_release: boolean | null;
  status: string | null;
  stage: string | null;
  started_at: string | null;
  updated_at: string | null;
  progress_path: string;
  run_manifest_path: string;
  schema_version: 1;
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

export function parseStagingRunIndex(value: unknown): ParsedStagingRunSummary[] {
  const payload = objectValue(value, "run index");
  if (payload.schema_version !== 1 || payload.schema_name != null) {
    throw new IncompatibleStagingDataError(
      `run index must use schema version 1, received ${String(payload.schema_version)}.`,
    );
  }
  if (!Array.isArray(payload.runs)) {
    throw new IncompatibleStagingDataError("run index runs must be an array.");
  }
  return payload.runs.map((entry, index) => {
    const row = objectValue(entry, `run index row ${index}`);
    const runId = stringValue(row.run_id, `run index row ${index} run_id`);
    const candidate = nullableString(
      row.candidate_release_id,
      `run index row ${index} candidate_release_id`,
    );
    return {
      run_id: runId,
      candidate_release_id: candidate,
      release_id: null,
      country_code: null,
      run_kind: null,
      non_release: null,
      status: optionalString(row.status),
      stage: optionalString(row.stage),
      started_at: optionalString(row.started_at),
      updated_at: optionalString(row.updated_at),
      progress_path:
        optionalString(row.progress_path) ?? `runs/${runId}/progress.json`,
      run_manifest_path:
        optionalString(row.run_manifest_path) ?? `runs/${runId}/run_manifest.json`,
      schema_version: 1,
    };
  });
}

const ARTIFACT_KINDS = new Set([
  "aggregate_diagnostics",
  "build_metadata",
  "sampling_receipt",
  "validation_summary",
]);

const RUN_FIELDS = [
  "run_id",
  "country_code",
  "operation_id",
  "pipeline",
  "candidate_id",
  "release_id",
  "run_kind",
  "non_release",
  "started_at",
  "updated_at",
  "status",
  "current_stage",
] as const;

const DELIVERY_FIELDS = [
  "contract_version",
  "enabled",
  "mode",
  "run_id",
  "configured_repository",
  "upload_attempts",
  "upload_successes",
  "read_back",
  "last_error_code",
  "opt_out_reason",
] as const;

function parsePipeline(value: unknown): JsonObject {
  const pipeline = objectValue(value, "version 2 pipeline");
  assertExactKeys(pipeline, "version 2 pipeline", ["id", "version"]);
  return {
    id: safeIdentifier(pipeline.id, "pipeline id"),
    version: boundedString(pipeline.version, "pipeline version", { minimum: 1 }),
  };
}

function parseSample(value: unknown): JsonObject | null {
  if (value === null) return null;
  const sample = objectValue(value, "version 2 sample");
  assertExactKeys(sample, "version 2 sample", ["mode"]);
  if (sample.mode !== "full") {
    throw new IncompatibleStagingDataError("version 2 sample mode must be full.");
  }
  return sample;
}

function parseDelivery(value: unknown, envelopeRunId: string): JsonObject {
  const delivery = objectValue(value, "version 2 delivery");
  assertExactKeys(delivery, "version 2 delivery", DELIVERY_FIELDS);
  if (delivery.contract_version !== 2) {
    throw new IncompatibleStagingDataError("delivery contract_version must be 2.");
  }
  const enabled = booleanValue(delivery.enabled, "delivery enabled");
  const mode = enumValue(delivery.mode, "delivery mode", [
    "local_and_remote",
    "local_only",
    "disabled",
  ] as const);
  const runId = nullableSafeIdentifier(delivery.run_id, "delivery run_id");
  const repository = nullableBoundedString(
    delivery.configured_repository,
    "delivery configured_repository",
    { minimum: 1 },
  );
  const attempts = integerValue(delivery.upload_attempts, "delivery upload_attempts");
  const successes = integerValue(
    delivery.upload_successes,
    "delivery upload_successes",
  );
  enumValue(delivery.read_back, "delivery read_back", [
    "not_requested",
    "passed",
    "failed",
  ] as const);
  if (
    delivery.last_error_code !== null &&
    (typeof delivery.last_error_code !== "string" ||
      !/^[A-Z0-9_]+$/.test(delivery.last_error_code))
  ) {
    throw new IncompatibleStagingDataError("delivery last_error_code is invalid.");
  }
  const reason = nullableBoundedString(
    delivery.opt_out_reason,
    "delivery opt_out_reason",
    { minimum: 1 },
  );
  if (successes > attempts) {
    throw new IncompatibleStagingDataError(
      "delivery upload_successes cannot exceed upload_attempts.",
    );
  }
  if (enabled) {
    if (mode === "disabled" || runId == null || reason != null) {
      throw new IncompatibleStagingDataError(
        "enabled staging has contradictory delivery fields.",
      );
    }
    if (runId !== envelopeRunId) {
      throw new IncompatibleStagingDataError(
        "delivery run_id does not match its enclosing document.",
      );
    }
    if (mode === "local_and_remote" && repository == null) {
      throw new IncompatibleStagingDataError(
        "remote staging requires a configured repository.",
      );
    }
    if (mode === "local_only" && repository != null) {
      throw new IncompatibleStagingDataError(
        "local-only staging cannot name a repository.",
      );
    }
  } else if (
    mode !== "disabled" ||
    runId != null ||
    repository != null ||
    reason == null ||
    attempts !== 0 ||
    successes !== 0
  ) {
    throw new IncompatibleStagingDataError(
      "disabled staging has contradictory delivery fields.",
    );
  }
  return delivery;
}

const FAILURE_FIELDS = [
  "error_code",
  "error_type",
  "message",
  "local_diagnostic_reference",
] as const;

function parseFailure(value: unknown, schemaVersion: 2 | 3): JsonObject | null {
  if (value === null) return null;
  const label = `version ${schemaVersion} failure`;
  const failure = objectValue(value, label);
  assertExactKeys(
    failure,
    label,
    schemaVersion === 3 ? [...FAILURE_FIELDS, "failure_class"] : FAILURE_FIELDS,
  );
  if (typeof failure.error_code !== "string" || !/^[A-Z0-9_]+$/.test(failure.error_code)) {
    throw new IncompatibleStagingDataError("failure error_code is invalid.");
  }
  if (
    typeof failure.error_type !== "string" ||
    !/^[A-Za-z][A-Za-z0-9_]*$/.test(failure.error_type)
  ) {
    throw new IncompatibleStagingDataError("failure error_type is invalid.");
  }
  boundedString(failure.message, "failure message", { minimum: 1, maximum: 500 });
  nullableBoundedString(
    failure.local_diagnostic_reference,
    "failure local_diagnostic_reference",
    { maximum: 200 },
  );
  if (
    schemaVersion === 3 &&
    failure.failure_class !== null &&
    (typeof failure.failure_class !== "string" ||
      !/^[a-z][a-z0-9_]*$/.test(failure.failure_class))
  ) {
    throw new IncompatibleStagingDataError("failure failure_class is invalid.");
  }
  return failure;
}

// Version 3: which gate phase refused the candidate, and which gates. The gate
// ids may be empty when the refusal named no gate; the count is at least one.
function parseBlock(value: unknown): JsonObject | null {
  if (value === null) return null;
  const block = objectValue(value, "version 3 block");
  assertExactKeys(block, "version 3 block", [
    "phase",
    "blocking_failure_count",
    "blocking_gate_ids",
  ]);
  safeIdentifier(block.phase, "block phase");
  integerValue(block.blocking_failure_count, "block blocking_failure_count", 1);
  if (!Array.isArray(block.blocking_gate_ids)) {
    throw new IncompatibleStagingDataError("block blocking_gate_ids must be an array.");
  }
  for (const [index, gateId] of block.blocking_gate_ids.entries()) {
    boundedString(gateId, `block blocking_gate_ids ${index}`, {
      minimum: 1,
      maximum: 200,
    });
  }
  return block;
}

function parseRunEnvelope(
  payload: JsonObject,
  label: string,
  schemaVersion: 2 | 3,
): {
  runId: string;
  candidateId: string;
  releaseId: string | null;
  pipeline: JsonObject;
  failure: JsonObject | null;
  block: JsonObject | null;
  delivery: JsonObject;
} {
  const runId = safeIdentifier(payload.run_id, `${label} run_id`);
  if (typeof payload.country_code !== "string" || !/^[A-Z]{2}$/.test(payload.country_code)) {
    throw new IncompatibleStagingDataError(`${label} country_code must contain two uppercase letters.`);
  }
  safeIdentifier(payload.operation_id, `${label} operation_id`);
  const pipeline = parsePipeline(payload.pipeline);
  const candidateId = safeIdentifier(payload.candidate_id, `${label} candidate_id`);
  const releaseId = nullableSafeIdentifier(payload.release_id, `${label} release_id`);
  safeIdentifier(payload.run_kind, `${label} run_kind`);
  const nonRelease = booleanValue(payload.non_release, `${label} non_release`);
  if (nonRelease !== (releaseId == null)) {
    throw new IncompatibleStagingDataError(
      `${label} non_release must be true exactly when release_id is absent.`,
    );
  }
  dateTimeValue(payload.started_at, `${label} started_at`);
  dateTimeValue(payload.updated_at, `${label} updated_at`);
  const status = enumValue(
    payload.status,
    `${label} status`,
    schemaVersion === 3
      ? (["running", "completed", "blocked", "failed"] as const)
      : (["running", "completed", "failed"] as const),
  );
  safeIdentifier(payload.current_stage, `${label} current_stage`);
  const delivery = parseDelivery(payload.delivery, runId);
  const failure = parseFailure(payload.failure, schemaVersion);
  if (status === "failed" && failure == null) {
    throw new IncompatibleStagingDataError(`${label} failed status requires failure data.`);
  }
  if (status !== "failed" && failure != null) {
    throw new IncompatibleStagingDataError(`${label} non-failed status cannot contain failure data.`);
  }
  const block = schemaVersion === 3 ? parseBlock(payload.block) : null;
  if (status === "blocked" && block == null) {
    throw new IncompatibleStagingDataError(`${label} blocked status requires block data.`);
  }
  if (status !== "blocked" && block != null) {
    throw new IncompatibleStagingDataError(`${label} non-blocked status cannot contain block data.`);
  }
  return { runId, candidateId, releaseId, pipeline, failure, block, delivery };
}

// Version 1 telemetry has no content policy. Its failure path records the
// Python traceback in `details` (microcosm staging.py `fail()`), which can carry
// build-machine paths; the dashboard serves staging documents publicly, so the
// traceback is dropped here. The error type and message are kept.
function withoutTraceback(payload: JsonObject): JsonObject {
  const details = payload.details;
  if (!details || typeof details !== "object" || Array.isArray(details)) return payload;
  if (!("traceback" in details)) return payload;
  const { traceback: _traceback, ...rest } = details as JsonObject;
  return { ...payload, details: rest };
}

export function parseStagingProgress(value: unknown): JsonObject {
  const payload = objectValue(value, "progress");
  const schemaVersion = version(payload, "progress");
  if (schemaVersion === 1) {
    const runId = stringValue(payload.run_id, "progress run_id");
    return { ...withoutTraceback(payload), run_id: runId };
  }
  assertExactKeys(payload, `version ${schemaVersion} progress`, [
    "schema_name",
    "schema_version",
    ...RUN_FIELDS,
    "sample",
    "delivery",
    "message",
    "details",
    "failure",
    ...(schemaVersion === 3 ? ["block"] : []),
  ]);
  const envelope = parseRunEnvelope(payload, "progress", schemaVersion);
  const sample = parseSample(payload.sample);
  nullableBoundedString(payload.message, "progress message", { maximum: 500 });
  objectValue(payload.details, "progress details");
  return {
    ...payload,
    run_id: envelope.runId,
    candidate_release_id: envelope.candidateId,
    release_id: envelope.releaseId,
    pipeline: envelope.pipeline,
    sample,
    failure: envelope.failure,
    block: envelope.block,
    stage: payload.current_stage,
    delivery: envelope.delivery,
  };
}

function parseArtifacts(value: unknown, runId: string): Record<string, JsonObject> {
  if (!Array.isArray(value)) {
    throw new IncompatibleStagingDataError("version 2 artifacts must be an array.");
  }
  const artifacts = new Map<string, JsonObject>();
  for (const [index, entry] of value.entries()) {
    const artifact = objectValue(entry, `artifact ${index}`);
    assertExactKeys(artifact, `artifact ${index}`, [
      "logical_name",
      "artifact_kind",
      "contract_relative_path",
      "media_type",
      "sha256",
      "classification",
    ]);
    const name = safeIdentifier(artifact.logical_name, `artifact ${index} logical_name`);
    const path = stringValue(
      artifact.contract_relative_path,
      `artifact ${index} contract_relative_path`,
    );
    if (path !== `artifacts/${name}.json` || path.includes("..") || path.startsWith("/")) {
      throw new IncompatibleStagingDataError(`artifact ${name} has an unsafe path.`);
    }
    if (!ARTIFACT_KINDS.has(String(artifact.artifact_kind))) {
      throw new IncompatibleStagingDataError(`artifact ${name} has an unsupported kind.`);
    }
    if (typeof artifact.sha256 !== "string" || !SHA_256.test(artifact.sha256)) {
      throw new IncompatibleStagingDataError(`artifact ${name} has an invalid digest.`);
    }
    if (
      artifact.media_type !== "application/json" ||
      !new Set(["aggregate", "non_row_level"]).has(String(artifact.classification))
    ) {
      throw new IncompatibleStagingDataError(`artifact ${name} is not reviewed JSON.`);
    }
    if (artifacts.has(name)) {
      throw new IncompatibleStagingDataError(`artifact ${name} appears more than once.`);
    }
    artifacts.set(name, {
      ...artifact,
      path,
      staging_path: `runs/${runId}/${path}`,
    });
  }
  return Object.fromEntries(artifacts);
}

export function parseStagingManifest(value: unknown): JsonObject {
  const payload = objectValue(value, "run manifest");
  const schemaVersion = version(payload, "manifest");
  if (schemaVersion === 1) {
    const runId = stringValue(payload.run_id, "run manifest run_id");
    return { ...payload, run_id: runId };
  }
  assertExactKeys(payload, `version ${schemaVersion} run manifest`, [
    "schema_name",
    "schema_version",
    ...RUN_FIELDS,
    "sample",
    "delivery",
    "artifacts",
    "failure",
    ...(schemaVersion === 3 ? ["block"] : []),
    "paths",
  ]);
  const envelope = parseRunEnvelope(payload, "run manifest", schemaVersion);
  const sample = parseSample(payload.sample);
  const paths = objectValue(payload.paths, "run manifest paths");
  assertExactKeys(paths, "run manifest paths", [
    "progress",
    "events",
    "calibration_progress",
  ]);
  if (typeof paths.progress !== "string" || typeof paths.events !== "string") {
    throw new IncompatibleStagingDataError(
      "run manifest progress and events paths must be strings.",
    );
  }
  if (
    paths.calibration_progress !== null &&
    typeof paths.calibration_progress !== "string"
  ) {
    throw new IncompatibleStagingDataError(
      "run manifest calibration_progress path must be a string or null.",
    );
  }
  return {
    ...payload,
    run_id: envelope.runId,
    candidate_release_id: envelope.candidateId,
    release_id: envelope.releaseId,
    pipeline: envelope.pipeline,
    sample,
    failure: envelope.failure,
    block: envelope.block,
    stage: payload.current_stage,
    delivery: envelope.delivery,
    artifacts: parseArtifacts(payload.artifacts, envelope.runId),
    paths,
  };
}

export function parseStagingCalibrationProgress(value: unknown): JsonObject {
  const payload = objectValue(value, "calibration progress");
  const schemaVersion = version(payload, "calibration");
  if (!Array.isArray(payload.events)) {
    throw new IncompatibleStagingDataError("calibration events must be an array.");
  }
  if (schemaVersion === 1) return payload;
  assertExactKeys(payload, `version ${schemaVersion} calibration progress`, [
    "schema_name",
    "schema_version",
    "run_id",
    "candidate_id",
    "updated_at",
    "events",
  ]);
  const runId = safeIdentifier(payload.run_id, "calibration progress run_id");
  const candidateId = safeIdentifier(
    payload.candidate_id,
    "calibration progress candidate_id",
  );
  dateTimeValue(payload.updated_at, "calibration progress updated_at");
  return {
    ...payload,
    run_id: runId,
    candidate_release_id: candidateId,
    events: payload.events.map((entry, index) => {
      const event = objectValue(entry, `calibration event ${index}`);
      assertExactKeys(event, `calibration event ${index}`, [
        "timestamp",
        "epoch",
        "epochs",
        "phase",
        "loss",
        "iteration",
        "budget_search",
        "budget_iteration",
        "budget_iters",
        "l0_lambda",
      ]);
      const timestamp = dateTimeValue(
        event.timestamp,
        `calibration event ${index} timestamp`,
      );
      nullableInteger(event.epoch, `calibration event ${index} epoch`);
      nullableInteger(event.epochs, `calibration event ${index} epochs`);
      nullableBoundedString(event.phase, `calibration event ${index} phase`);
      nullableNumber(event.loss, `calibration event ${index} loss`);
      nullableInteger(event.iteration, `calibration event ${index} iteration`);
      nullableInteger(event.budget_search, `calibration event ${index} budget_search`);
      nullableInteger(
        event.budget_iteration,
        `calibration event ${index} budget_iteration`,
      );
      nullableInteger(event.budget_iters, `calibration event ${index} budget_iters`);
      nullableNumber(event.l0_lambda, `calibration event ${index} l0_lambda`);
      return {
        ...event,
        time: timestamp,
      };
    }),
  };
}

export function parseStagingEvents(text: string | null): JsonObject[] {
  if (!text) return [];
  const events = text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, index) => {
      let payload: JsonObject;
      try {
        payload = objectValue(JSON.parse(line), `event ${index}`);
      } catch (error) {
        if (error instanceof IncompatibleStagingDataError) throw error;
        throw new IncompatibleStagingDataError(`event ${index} is not valid JSON.`);
      }
      const schemaVersion = version(payload, "event", { allowUnversionedV1: true });
      if (schemaVersion === 1) return withoutTraceback(payload);
      assertExactKeys(payload, `event ${index}`, [
        "schema_name",
        "schema_version",
        "sequence",
        "timestamp",
        "event_type",
        "run_id",
        "stage_id",
        "status",
        "message",
        "details",
      ]);
      integerValue(payload.sequence, `event ${index} sequence`, 1);
      const timestamp = dateTimeValue(payload.timestamp, `event ${index} timestamp`);
      enumValue(payload.event_type, `event ${index} event_type`, [
        "stage",
        "calibration",
      ] as const);
      safeIdentifier(payload.run_id, `event ${index} run_id`);
      const stageId = safeIdentifier(payload.stage_id, `event ${index} stage_id`);
      enumValue(
        payload.status,
        `event ${index} status`,
        schemaVersion === 3
          ? (["started", "completed", "failed", "progress", "blocked"] as const)
          : (["started", "completed", "failed", "progress"] as const),
      );
      nullableBoundedString(payload.message, `event ${index} message`, { maximum: 500 });
      objectValue(payload.details, `event ${index} details`);
      return {
        ...payload,
        stage: stageId,
        time: timestamp,
      };
    });
  const structured = events.filter(isStructuredStagingDocument);
  if (
    structured.length &&
    structured.some((event, index) => event.sequence !== index + 1)
  ) {
    throw new IncompatibleStagingDataError(
      `version ${String(structured[0].schema_version)} event sequence is not contiguous.`,
    );
  }
  return events;
}

export interface StagingRunDocuments {
  progress?: JsonObject | null;
  runManifest?: JsonObject | null;
  calibrationProgress?: JsonObject | null;
  events?: JsonObject[];
}

function samePipeline(left: unknown, right: unknown): boolean {
  const leftPipeline = objectValue(left, "left pipeline");
  const rightPipeline = objectValue(right, "right pipeline");
  return (
    leftPipeline.id === rightPipeline.id &&
    leftPipeline.version === rightPipeline.version
  );
}

export function validateStagingRunConsistency(
  requestedRunId: string,
  documents: StagingRunDocuments,
): void {
  const runId = safeIdentifier(requestedRunId, "requested run_id");
  const progress = documents.progress ?? null;
  const manifest = documents.runManifest ?? null;
  const calibration = documents.calibrationProgress ?? null;
  const events = documents.events ?? [];
  const allDocuments = [progress, manifest, calibration, ...events].filter(
    (document): document is JsonObject => document != null,
  );
  if (!allDocuments.length) return;

  const versions = new Set(
    allDocuments.map((document) => document.schema_version ?? 1),
  );
  if (versions.size !== 1) {
    throw new IncompatibleStagingDataError(
      `a run cannot mix document versions ${[...versions].sort().join(" and ")}.`,
    );
  }
  if (versions.has(1)) return;

  for (const [label, document] of [
    ["progress", progress],
    ["run manifest", manifest],
    ["calibration progress", calibration],
  ] as const) {
    if (document && document.run_id !== runId) {
      throw new IncompatibleStagingDataError(
        `${label} identifies run ${String(document.run_id)}, not ${runId}.`,
      );
    }
  }
  for (const [index, event] of events.entries()) {
    if (event.run_id !== runId) {
      throw new IncompatibleStagingDataError(
        `event ${index} identifies run ${String(event.run_id)}, not ${runId}.`,
      );
    }
  }

  if (manifest) {
    const paths = objectValue(manifest.paths, "run manifest paths");
    const expectedProgress = `runs/${runId}/progress.json`;
    const expectedEvents = `runs/${runId}/events.ndjson`;
    if (paths.progress !== expectedProgress || paths.events !== expectedEvents) {
      throw new IncompatibleStagingDataError(
        "run manifest paths do not identify the requested run.",
      );
    }
    if (
      paths.calibration_progress !== null &&
      paths.calibration_progress !== `runs/${runId}/calibration_progress.json`
    ) {
      throw new IncompatibleStagingDataError(
        "run manifest calibration progress path does not identify the requested run.",
      );
    }
  }

  if (progress && manifest) {
    const stableFields = [
      "run_id",
      "country_code",
      "operation_id",
      "candidate_id",
      "release_id",
      "run_kind",
      "non_release",
      "started_at",
    ] as const;
    for (const field of stableFields) {
      if (progress[field] !== manifest[field]) {
        throw new IncompatibleStagingDataError(
          `progress and run manifest disagree on stable field ${field}.`,
        );
      }
    }
    if (!samePipeline(progress.pipeline, manifest.pipeline)) {
      throw new IncompatibleStagingDataError(
        "progress and run manifest disagree on stable field pipeline.",
      );
    }
  }

  const identity = manifest ?? progress;
  if (calibration && identity && calibration.candidate_id !== identity.candidate_id) {
    throw new IncompatibleStagingDataError(
      "calibration progress and run documents disagree on candidate_id.",
    );
  }
}
