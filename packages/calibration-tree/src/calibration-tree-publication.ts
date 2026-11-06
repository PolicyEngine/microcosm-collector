// Skeleton: the publication command's interface, without its behaviour yet.
import type { CalibrationTreeBlobClient } from "./calibration-tree-blob";

export const CALIBRATION_TREE_PUBLICATION_EXIT_REQUEST_ERROR = 1;
export const CALIBRATION_TREE_PUBLICATION_EXIT_ENTRY_REFUSED = 2;
export const CALIBRATION_TREE_PUBLICATION_EXIT_PUBLICATION_ERROR = 3;

export class CalibrationTreePublicationRequestError extends Error {}
export class CalibrationTreeEntryRefusedError extends Error {}
export class CalibrationTreeManifestConflictError extends Error {}

export interface CalibrationTreePublicationRequest {
  bundleDirectory: string;
  worker: unknown;
  makeLatest: boolean;
}

export interface CalibrationTreePublicationResult {
  buildArtifactId: string;
  kind: "release" | "staging";
  uploadedParts: number;
  keptParts: number;
  madeLatest: boolean;
}

export function parseCalibrationTreePublicationRequest(value: unknown): CalibrationTreePublicationRequest {
  return value as CalibrationTreePublicationRequest;
}

export async function publishBuiltCalibrationTree(_options: {
  request: CalibrationTreePublicationRequest;
  token: string;
  client?: CalibrationTreeBlobClient;
}): Promise<CalibrationTreePublicationResult> {
  return { buildArtifactId: "", kind: "release", uploadedParts: 0, keptParts: 0, madeLatest: false };
}

export function calibrationTreePublicationExitCode(_error: unknown): number {
  return 0;
}
