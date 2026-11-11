import {
  readStructuredDimensions,
  type StructuredDimensionDefinition,
  type StructuredTargetDimension,
} from "./structured-dimension-reader";

type JsonObject = Record<string, unknown>;

export interface StructuredTargetIdentity {
  source: string;
  sourceLabel: string | null;
  sourceCitation: string | null;
  sourceUrl: string | null;
  variable: string;
  variableLabel: string | null;
  measure: string | null;
  geography: string;
  geographyId: string | null;
  geographyDimensionId: string | null;
  geographyRank: number | null;
  level: string;
  dimensions: StructuredTargetDimension[];
  breakdown: string;
}

function isPlainObject(value: unknown): value is JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function readStructuredTarget(
  row: JsonObject,
  definitions: Record<string, StructuredDimensionDefinition>,
  nationalGeography: string,
): StructuredTargetIdentity {
  if (!isPlainObject(row.source) || !stringValue(row.source.id)) {
    throw new Error("Schema 7 target source.id must be a non-empty string.");
  }
  if (!isPlainObject(row.variable) || !stringValue(row.variable.id)) {
    throw new Error("Schema 7 target variable.id must be a non-empty string.");
  }
  if (!isPlainObject(row.dimensions)) {
    throw new Error("Schema 7 target dimensions must be an object.");
  }
  const source = row.source;
  const variable = row.variable;
  const values = row.dimensions;
  const sourceId = stringValue(source.id)!;
  const variableId = stringValue(variable.id)!;
  const structured = readStructuredDimensions(values, definitions);
  const geography = structured.geography ?? nationalGeography;
  const level = structured.level ?? "national";
  const { dimensions } = structured;

  return {
    source: sourceId,
    sourceLabel: stringValue(source.label),
    sourceCitation: stringValue(source.citation),
    sourceUrl: stringValue(source.url),
    variable: variableId,
    variableLabel: stringValue(variable.label),
    measure: stringValue(variable.measure),
    geography,
    geographyId: structured.geographyId,
    geographyDimensionId: structured.geographyDimensionId,
    geographyRank: structured.geographyRank,
    level,
    dimensions,
    breakdown: dimensions.map((dimension) => dimension.value).join(" · "),
  };
}
