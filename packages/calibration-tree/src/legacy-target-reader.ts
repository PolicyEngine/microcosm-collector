type JsonObject = Record<string, unknown>;

export interface ParsedLegacyTarget {
  geography: string;
  level: string;
  source: string;
  variable: string;
  breakdown: string;
}

export interface LegacyFilterDecomposition {
  geography: string;
  level: string;
  dimensions: ReadonlyArray<{ value: string }>;
}

const FIPS_TO_ABBR: Record<string, string> = {
  "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
  "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
  "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
  "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
  "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
  "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
  "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
  "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
  "54": "WV", "55": "WI", "56": "WY",
};

const STATE_ABBRS = new Set([
  "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
  "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
  "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
  "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "US",
]);

// Compatibility labels for the country-level GSS identifiers in the current
// UK schema-6 artifact. Future artifacts should publish geography labels in
// structured dimension definitions or `ledger_geography_name`.
const LEGACY_GSS_GEOGRAPHIES: Readonly<Record<string, string>> = {
  K02000001: "United Kingdom",
  K03000001: "Great Britain",
  E92000001: "England",
  S92000003: "Scotland",
};

const MEASURES = new Set(["total", "count", "mean", "filers", "nonfilers"]);
const LEGACY_UNDERSCORE_PUBLISHERS = new Set([
  "statbel",
  "onss",
  "jrc",
  "sfpd",
  "nasa",
]);

function asObject(value: unknown): JsonObject {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readableToken(value: string | null): string | null {
  return value ? value.replace(/_/g, " ") : null;
}

export function stateFromGeoId(value: string | null): string | null {
  if (!value) return null;
  const match = /US(\d{2})$/.exec(value);
  return match ? FIPS_TO_ABBR[match[1]] ?? null : null;
}

export function districtFromGeoId(value: string | null): string | null {
  if (!value) return null;
  const match = /US(\d{2})(\d{2})$/.exec(value);
  if (!match) return null;
  const state = FIPS_TO_ABBR[match[1]];
  return state ? `${state}-${match[2]}` : null;
}

function geographyFromMetadata(
  metadata: JsonObject,
  nationalGeography: string,
): Pick<ParsedLegacyTarget, "geography" | "level"> | null {
  const publishedName = stringValue(metadata.ledger_geography_name)?.trim() ?? null;
  const geographyId = stringValue(metadata.ledger_geography_id);
  const geographyLevel = stringValue(metadata.ledger_geography_level);
  const gssGeography = geographyId ? LEGACY_GSS_GEOGRAPHIES[geographyId] : null;

  if (publishedName) {
    return {
      geography: publishedName,
      level:
        geographyLevel === "country" &&
          publishedName === nationalGeography &&
          !gssGeography
          ? "national"
          : geographyLevel ?? "",
    };
  }
  if (gssGeography) {
    return { geography: gssGeography, level: geographyLevel ?? "country" };
  }
  if (geographyLevel === "congressional_district") {
    const geography = districtFromGeoId(geographyId);
    return geography ? { geography, level: geographyLevel } : null;
  }
  if (geographyLevel === "state") {
    const geography = stateFromGeoId(geographyId) ?? stringValue(metadata.state);
    return geography ? { geography, level: geographyLevel } : null;
  }
  if (geographyLevel === "country") {
    return { geography: nationalGeography, level: "national" };
  }
  return null;
}

function variableFromMeasure(value: string | null): string | null {
  return value
    ? value.replace(/_(amount|returns|claims|count|total|collections|projected_amount)$/, "")
    : null;
}

function breakdownFromSourceMeasure(
  variable: string | null,
  measureId: string | null,
): string | null {
  if (!variable || !measureId) return null;
  const variablePrefix = variable.replace(/\s+/g, "_").toLowerCase();
  const measure = measureId.toLowerCase();
  if (!measure.startsWith(`${variablePrefix}_`)) return null;
  const detail = measure
    .slice(variablePrefix.length + 1)
    .replace(/_(amount|returns|claims|count|total|collections|projected_amount)$/, "");
  if (
    variablePrefix === "eitc" &&
    ["amount", "returns", "claims", "count", "total"].includes(detail)
  ) {
    return "all children";
  }
  if (
    !detail ||
    MEASURES.has(detail) ||
    ["amount", "returns", "claims", "count", "total", "collections", "projected_amount"]
      .includes(detail)
  ) {
    return null;
  }
  return readableToken(detail);
}

export function qualifyingChildrenFromRecordSet(value: string | null): string | null {
  if (!value) return null;
  const match = /\.eitc_by_agi_children\.([^.]+)$/.exec(value);
  if (!match) return null;
  const childGroup = match[1];
  if (childGroup === "no_qualifying_children") return "no qualifying children";
  if (childGroup === "one_qualifying_child") return "one qualifying child";
  if (childGroup === "two_qualifying_children") return "two qualifying children";
  if (childGroup === "three_or_more_qualifying_children") {
    return "three or more qualifying children";
  }
  return readableToken(childGroup);
}

export function chroniclePublisherFromMetadata(metadata: JsonObject): string | null {
  if (!Array.isArray(metadata.chronicle_record_ids)) return null;
  const first = metadata.chronicle_record_ids.find(
    (value): value is string => typeof value === "string" && Boolean(value.trim()),
  );
  return first?.trim().split(".", 1)[0] || null;
}

function legacyVariable(
  name: string,
  row: JsonObject,
  decomposition: LegacyFilterDecomposition | null,
): string | null {
  const metadata = asObject(row.metadata);
  const published =
    stringValue(metadata.variable) ??
    (typeof row.variable === "string" ? stringValue(row.variable) : null);
  if (published) return readableToken(published);

  if (decomposition && name.includes("_") && !name.includes("/")) {
    let identifier = name;
    const filterSuffix = stringValue(row.filter)?.replace(/^cell_/, "");
    if (filterSuffix && identifier.endsWith(`_${filterSuffix}`)) {
      identifier = identifier.slice(0, -(filterSuffix.length + 1));
    }
    const separator = identifier.indexOf("_");
    if (separator >= 0) identifier = identifier.slice(separator + 1);
    return readableToken(identifier);
  }

  const namePublisher = name.split("_", 1)[0];
  if (LEGACY_UNDERSCORE_PUBLISHERS.has(namePublisher)) {
    return readableToken(name.slice(namePublisher.length + 1));
  }
  const publisher = chroniclePublisherFromMetadata(metadata);
  if (publisher && !name.includes(".") && name.startsWith(`${publisher}_`)) {
    return readableToken(name.slice(publisher.length + 1));
  }
  return null;
}

function parseDottedTarget(
  name: string,
  row: JsonObject,
  nationalGeography: string,
): ParsedLegacyTarget | null {
  if (!name.includes(".")) return null;
  const metadata = asObject(row.metadata);
  const registry = asObject(row.registry);
  const parts = name.split(".");
  const source = stringValue(registry.family) ?? parts[0] ?? "";
  const measureId = stringValue(metadata.source_measure_id) ?? parts.at(-1) ?? "";
  const variable =
    readableToken(stringValue(metadata.variable)) ??
    readableToken(variableFromMeasure(measureId)) ??
    readableToken(parts.at(-2) ?? null) ??
    "";
  const childBreakdown = qualifyingChildrenFromRecordSet(
    stringValue(metadata.ledger_layout_record_set_id),
  );
  const breakdown = [
    readableToken(stringValue(metadata.ledger_layout_groupby_value_id)),
    childBreakdown ?? breakdownFromSourceMeasure(variable, measureId),
    readableToken(stringValue(metadata.filing_status)),
  ]
    .filter((value): value is string => Boolean(value && value !== variable))
    .join(" · ");

  const stateMarkerIndex = parts.findIndex((part) => part.startsWith("state_"));
  const state = stateMarkerIndex < 0
    ? null
    : parts
        .slice(stateMarkerIndex + 1, stateMarkerIndex + 3)
        .map((part) => part.toUpperCase())
        .find((part) => STATE_ABBRS.has(part) && part !== "US") ?? null;
  const national = parts.includes("us");

  return {
    geography: state ?? (national ? nationalGeography : ""),
    level: state ? "state" : national ? "national" : "",
    source,
    variable,
    breakdown,
  };
}

function parseSlashTarget(name: string, nationalGeography: string): ParsedLegacyTarget {
  const parts = name.split("/");
  const p0 = parts[0] ?? "";
  const fips = /^US(\d{2})$/.exec(p0);
  if (fips) {
    return {
      geography: FIPS_TO_ABBR[fips[1]] ?? p0,
      level: "state",
      source: "admin",
      variable: parts[1] ?? "",
      breakdown: parts.slice(2).join(" · "),
    };
  }
  if (p0 === "state") {
    const second = parts[1] ?? "";
    if (STATE_ABBRS.has(second) && second !== "US") {
      return {
        geography: second,
        level: "state",
        source: "state",
        variable: parts[2] ?? "",
        breakdown: parts.slice(3).join(" · "),
      };
    }
    const last = parts[parts.length - 1];
    if (parts.length >= 4 && STATE_ABBRS.has(last)) {
      return {
        geography: last,
        level: "state",
        source: parts[1] ?? "",
        variable: parts[2] ?? "",
        breakdown: parts.slice(3, -1).join(" · "),
      };
    }
    return {
      geography: "state",
      level: "state",
      source: parts[1] ?? "",
      variable: parts[2] ?? "",
      breakdown: parts.slice(3).join(" · "),
    };
  }
  if (p0 === "nation" || p0 === "national" || p0 === "us") {
    return {
      geography: nationalGeography,
      level: "national",
      source: parts[1] ?? "",
      variable: parts[2] ?? "",
      breakdown: parts.slice(3).join(" · "),
    };
  }
  return {
    geography: "",
    level: "",
    source: p0,
    variable: parts[1] ?? "",
    breakdown: parts.slice(2).join(" · "),
  };
}

export function readLegacyTarget(
  name: string,
  row: JsonObject,
  nationalGeography: string,
  decomposition: LegacyFilterDecomposition | null,
): ParsedLegacyTarget {
  const metadata = asObject(row.metadata);
  const parsed =
    parseDottedTarget(name, row, nationalGeography) ??
    parseSlashTarget(name, nationalGeography);
  const publisher = chroniclePublisherFromMetadata(metadata);
  const metadataGeography = geographyFromMetadata(metadata, nationalGeography);
  return {
    ...parsed,
    geography:
      decomposition?.geography ?? metadataGeography?.geography ?? parsed.geography,
    level: decomposition?.level ?? metadataGeography?.level ?? parsed.level,
    source: publisher ?? parsed.source,
    variable: legacyVariable(name, row, decomposition) ?? parsed.variable,
    breakdown: decomposition
      ? decomposition.dimensions.map((dimension) => dimension.value).join(" · ")
      : parsed.breakdown,
  };
}
