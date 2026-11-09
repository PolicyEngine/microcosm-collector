type JsonObject = Record<string, unknown>;

export interface StructuredDimensionDefinition {
  label: string;
  role?: "geography";
  level?: string;
  values?: Record<string, string>;
  order?: string[];
}

export interface StructuredTargetDimension {
  key: string;
  label: string;
  value: string;
  source_key: string;
  raw_value: string;
  rank?: number;
}

export interface StructuredDimensions {
  geography: string | null;
  geographyId: string | null;
  geographyDimensionId: string | null;
  geographyRank: number | null;
  level: string | null;
  dimensions: StructuredTargetDimension[];
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function titleCase(value: string): string {
  return value
    .replace(/[_:./#-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function dimensionLabel(value: string): string {
  if (value === "us:statutes/26/62#adjusted_gross_income") return "Income band";
  if (value === "census_stc.item") return "Item";
  if (value === "hhs_acf_tanf.spending_category") return "Spending category";
  if (value.startsWith("cms_medicaid.")) {
    return titleCase(value.replace(/^cms_medicaid\./, ""));
  }
  const hash = value.split("#").at(-1);
  const last = hash?.split(".").at(-1) ?? value;
  return titleCase(last);
}

function utf8Hex(value: string): string {
  return [...new TextEncoder().encode(value)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

// Simple lowercase identifiers retain their established keys. Identifiers
// containing punctuation, uppercase characters, or the reserved escape prefix
// use an injective UTF-8 encoding that remains safe in facet query parameters.
export function structuredDimensionKey(id: string): string {
  if (/^[a-z0-9_]+$/.test(id) && !id.startsWith("x0_")) return `bd_${id}`;
  return `bd_x0_${utf8Hex(id)}`;
}

function dimensionValue(
  label: string,
  rawValue: string,
  valueLabels?: Readonly<Record<string, string>>,
): string {
  const explicit = valueLabels && Object.hasOwn(valueLabels, rawValue)
    ? valueLabels[rawValue]
    : undefined;
  if (explicit) return explicit;
  if (label.toLowerCase() === "age band") {
    const range = /^(\d+)_(\d+)$/.exec(rawValue);
    if (range) return `${range[1]}–${range[2]}`;
    const openEnded = /^(\d+)_plus$/.exec(rawValue);
    if (openEnded) return `${openEnded[1]}+`;
  }
  return titleCase(rawValue);
}

export function readStructuredDimensions(
  values: JsonObject,
  definitions: Record<string, StructuredDimensionDefinition>,
): StructuredDimensions {
  let geography: string | null = null;
  let geographyId: string | null = null;
  let geographyDimensionId: string | null = null;
  let geographyRank: number | null = null;
  let level: string | null = null;
  const dimensions: StructuredTargetDimension[] = [];

  for (const [id, raw] of Object.entries(values)) {
    if (!id) continue;
    const rawValue = stringValue(raw);
    if (!rawValue) continue;
    const definition = definitions[id];
    const label = definition?.label ?? dimensionLabel(id);
    const value = dimensionValue(label, rawValue, definition?.values);
    const rankOrder = definition?.order ??
      (definition?.values ? Object.keys(definition.values) : undefined);
    const rank = rankOrder?.indexOf(rawValue) ?? -1;
    if (definition?.role === "geography") {
      if (geography != null) {
        throw new Error(
          "Structured target dimensions must contain at most one populated geography-role dimension.",
        );
      }
      geography = value;
      geographyId = rawValue;
      geographyDimensionId = id;
      geographyRank = rank >= 0 ? rank : null;
      level = definition.level ?? "region";
      continue;
    }
    dimensions.push({
      key: structuredDimensionKey(id),
      label,
      value,
      source_key: id,
      raw_value: rawValue,
      ...(rank >= 0 ? { rank } : {}),
    });
  }

  return {
    geography,
    geographyId,
    geographyDimensionId,
    geographyRank,
    level,
    dimensions,
  };
}
