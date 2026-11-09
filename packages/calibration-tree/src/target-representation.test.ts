import { describe, expect, test } from "bun:test";

import {
  targetRepresentationForDiagnosticsArtifact,
  targetRepresentationForSchema,
  UnsupportedCalibrationDiagnosticsSchemaError,
} from "./target-representation";

describe("calibration target representation", () => {
  test("dispatches historical and current readers only by top-level version", () => {
    for (const schemaVersion of [1, 2, 3, 4, 5, 6]) {
      expect(targetRepresentationForSchema(schemaVersion)).toBe("legacy");
    }
    expect(targetRepresentationForSchema(7)).toBe("structured");
    expect(targetRepresentationForSchema(8)).toBe("hierarchy");
    // Schema 9 declares hierarchy labels in catalogs; its reader expands
    // them into the schema 8 rows, so it shares the hierarchy reader.
    expect(targetRepresentationForSchema(9)).toBe("hierarchy");
  });

  test("rejects versions without a supported compatibility contract", () => {
    for (const schemaVersion of [undefined, null, "8", 0, 10, 7.5]) {
      expect(() => targetRepresentationForSchema(schemaVersion)).toThrow(
        UnsupportedCalibrationDiagnosticsSchemaError,
      );
    }
  });

  test("reads only the exact audited unversioned local-area artifacts", () => {
    const auditedArtifacts = [
      {
        releaseId:
          "populace-us-2024-buildo-acs-local-77e2061-20260724T110908Z",
        sha256:
          "54251f2209c2e71bb1cce9f5626a3e59879847747156e873de51011c8e8b508f",
      },
      {
        releaseId:
          "populace-us-2024-buildp-acs-local-592ae5d6-20260819T020303Z",
        sha256:
          "b6f05b652049f88c044f1907eeed13c378aea47aec9c6dd6518faa90d1a0dcd0",
      },
      {
        releaseId:
          "populace-us-2024-buildo-acs-local-767312d60-20260923T074941Z",
        sha256:
          "f39d10a72415eb85ac0faa231f7970bffcc408bd53ef0f05985ea8dd0ac94903",
      },
    ];

    for (const identity of auditedArtifacts) {
      expect(
        targetRepresentationForDiagnosticsArtifact(undefined, identity),
      ).toBe("legacy");
    }
    const { releaseId, sha256 } = auditedArtifacts.at(-1)!;
    expect(() =>
      targetRepresentationForDiagnosticsArtifact(undefined, {
        releaseId,
        sha256: "0".repeat(64),
      }),
    ).toThrow(UnsupportedCalibrationDiagnosticsSchemaError);
    expect(() =>
      targetRepresentationForDiagnosticsArtifact(undefined, {
        releaseId: "unrecognized-release",
        sha256,
      }),
    ).toThrow(UnsupportedCalibrationDiagnosticsSchemaError);
  });
});
