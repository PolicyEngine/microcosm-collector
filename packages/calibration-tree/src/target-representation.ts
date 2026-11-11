export type TargetRepresentation =
  | "legacy"
  | "structured"
  | "hierarchy"
  | "mixed"
  | "unknown";

export type TargetRowRepresentation = "legacy" | "structured" | "hierarchy";

export class UnsupportedCalibrationDiagnosticsSchemaError extends Error {}

const KNOWN_UNVERSIONED_LEGACY_DIAGNOSTICS = new Map([
  [
    "populace-us-2024-buildo-acs-local-77e2061-20260724T110908Z",
    "54251f2209c2e71bb1cce9f5626a3e59879847747156e873de51011c8e8b508f",
  ],
  [
    "populace-us-2024-buildp-acs-local-592ae5d6-20260819T020303Z",
    "b6f05b652049f88c044f1907eeed13c378aea47aec9c6dd6518faa90d1a0dcd0",
  ],
  [
    "populace-us-2024-buildo-acs-local-767312d60-20260923T074941Z",
    "f39d10a72415eb85ac0faa231f7970bffcc408bd53ef0f05985ea8dd0ac94903",
  ],
]);

/**
 * Select the only target reader permitted for a diagnostics artifact.
 *
 * Row shape is deliberately not inspected: the top-level schema version is
 * the compatibility contract between Microcosm and this dashboard.
 */
export function targetRepresentationForSchema(
  schemaVersion: unknown,
): TargetRowRepresentation {
  if (
    typeof schemaVersion !== "number" ||
    !Number.isInteger(schemaVersion)
  ) {
    throw new UnsupportedCalibrationDiagnosticsSchemaError(
      "Calibration diagnostics must declare an integer schema_version.",
    );
  }
  if (schemaVersion >= 1 && schemaVersion <= 6) return "legacy";
  if (schemaVersion === 7) return "structured";
  // Schema 9 declares the schema 8 hierarchy's labels in catalogs;
  // `expandHierarchyCatalogs` turns its rows back into schema 8 rows.
  if (schemaVersion === 8 || schemaVersion === 9) return "hierarchy";
  throw new UnsupportedCalibrationDiagnosticsSchemaError(
    `Unsupported calibration diagnostics schema_version ${schemaVersion}; ` +
      "this dashboard reads versions 1 through 9.",
  );
}

export function targetRepresentationForDiagnosticsArtifact(
  schemaVersion: unknown,
  identity: { releaseId: string; sha256: string | null },
): TargetRowRepresentation {
  if (schemaVersion != null) {
    return targetRepresentationForSchema(schemaVersion);
  }
  const expectedSha256 = KNOWN_UNVERSIONED_LEGACY_DIAGNOSTICS.get(
    identity.releaseId,
  );
  if (expectedSha256 && identity.sha256 === expectedSha256) {
    return "legacy";
  }
  return targetRepresentationForSchema(schemaVersion);
}
