type JsonObject = Record<string, unknown>;

export interface HierarchyTargetDimension {
  key: string;
  label: string;
  value: string;
  value_id: string;
  source_key: string;
  raw_value: string;
  rank: number;
}

export interface HierarchyTargetIdentity {
  source: string;
  sourceLabel: string;
  variable: string;
  variableLabel: string;
  geography: string;
  geographyId: string;
  level: string;
  dimensions: HierarchyTargetDimension[];
  targetId: string;
  targetLabel: string;
  breakdown: string;
}

function isPlainObject(value: unknown): value is JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function requiredObject(value: unknown, path: string): JsonObject {
  if (!isPlainObject(value)) {
    throw new Error(`Schema 8 target ${path} must be an object.`);
  }
  return value;
}

function requiredString(value: unknown, path: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`Schema 8 target ${path} must be a non-empty string.`);
  }
  return value.trim();
}

function node(value: unknown, path: string): { id: string; label: string } {
  const object = requiredObject(value, path);
  return {
    id: requiredString(object.id, `${path}.id`),
    label: requiredString(object.label, `${path}.label`),
  };
}

/** Read the complete, producer-authored hierarchy on one schema-8 row. */
export function readHierarchyTarget(row: JsonObject): HierarchyTargetIdentity {
  const hierarchy = requiredObject(row.hierarchy, "hierarchy");
  const provider = node(hierarchy.provider, "hierarchy.provider");
  const categoryObject = requiredObject(
    hierarchy.category,
    "hierarchy.category",
  );
  const category = node(categoryObject, "hierarchy.category");
  const categoryProviderId = requiredString(
    categoryObject.provider_id,
    "hierarchy.category.provider_id",
  );
  if (categoryProviderId !== provider.id) {
    throw new Error(
      `Schema 8 category ${category.id} references provider ` +
        `${categoryProviderId}, expected ${provider.id}.`,
    );
  }

  const geographyObject = requiredObject(
    hierarchy.geography,
    "hierarchy.geography",
  );
  const geography = node(geographyObject, "hierarchy.geography");
  const geographyLevel = requiredString(
    geographyObject.level,
    "hierarchy.geography.level",
  );

  if (!Array.isArray(hierarchy.dimensions)) {
    throw new Error("Schema 8 target hierarchy.dimensions must be an array.");
  }
  const dimensionIds = new Set<string>();
  const dimensions = hierarchy.dimensions.map((value, rank) => {
    const object = requiredObject(value, `hierarchy.dimensions[${rank}]`);
    const id = requiredString(object.id, `hierarchy.dimensions[${rank}].id`);
    if (dimensionIds.has(id)) {
      throw new Error(`Schema 8 target contains duplicate dimension id ${id}.`);
    }
    dimensionIds.add(id);
    const valueId = requiredString(
      object.value_id,
      `hierarchy.dimensions[${rank}].value_id`,
    );
    return {
      key: id,
      label: requiredString(
        object.label,
        `hierarchy.dimensions[${rank}].label`,
      ),
      value: requiredString(
        object.value_label,
        `hierarchy.dimensions[${rank}].value_label`,
      ),
      value_id: valueId,
      source_key: id,
      raw_value: valueId,
      rank,
    };
  });

  const target = node(hierarchy.target, "hierarchy.target");
  const rowTargetId =
    typeof row.target_name === "string" && row.target_name.trim()
      ? row.target_name.trim()
      : typeof row.name === "string"
        ? row.name.replace(/@[^@]+$/, "").trim()
        : "";
  if (rowTargetId && rowTargetId !== target.id) {
    throw new Error(
      `Schema 8 hierarchy target id ${target.id} does not match row target ` +
        `${rowTargetId}.`,
    );
  }

  return {
    source: provider.id,
    sourceLabel: provider.label,
    variable: category.id,
    variableLabel: category.label,
    geography: geography.label,
    geographyId: geography.id,
    level: geographyLevel,
    dimensions,
    targetId: target.id,
    targetLabel: target.label,
    breakdown: dimensions.map((dimension) => dimension.value).join(" · "),
  };
}

export type HierarchyLabelKind =
  | "provider"
  | "category"
  | "geography"
  | "dimension"
  | "dimension value"
  | "target";

/** One identifier a file labels more than one way. */
export interface HierarchyLabelVariant {
  kind: HierarchyLabelKind;
  id: string;
  // Distinct spellings in file order; the first is the one the dashboard shows.
  labels: string[];
}

export interface HierarchyTargetValidation {
  label_variants: HierarchyLabelVariant[];
}

// Chronicle keeps each publisher's own text for a name (chronicle#267: HMRC
// writes "Hartlepool UA" where ONS writes "Hartlepool", DWP writes "Ynys Môn"
// where HMRC writes "Ynys Mon"), and Microcosm requires one spelling only
// within a target's own member facts. A file that spans publishers therefore
// carries several spellings of one identifier legitimately, so a differing
// label is reported, never refused: the first spelling seen is kept and the
// rest are listed for the reader. Capitalisation and whitespace differences
// (Unicode whitespace, as JS `\s` matches) are one spelling, not a variant.
function labelIdentity(label: string): string {
  return label.trim().replace(/\s+/g, " ").toLowerCase();
}

function recordLabel(labels: Map<string, string[]>, id: string, label: string): void {
  const spellings = labels.get(id);
  if (spellings == null) {
    labels.set(id, [label]);
    return;
  }
  const identity = labelIdentity(label);
  if (!spellings.some((known) => labelIdentity(known) === identity)) {
    spellings.push(label);
  }
}

function variantsOf(
  kind: HierarchyLabelKind,
  labels: Map<string, string[]>,
  displayId: (key: string) => string = (key) => key,
): HierarchyLabelVariant[] {
  return [...labels.entries()]
    .filter(([, spellings]) => spellings.length > 1)
    .map(([key, spellings]) => ({ kind, id: displayId(key), labels: [...spellings] }));
}

/**
 * Read every row's hierarchy, refuse structural inconsistencies (a category
 * under two providers), and report every identifier the file labels more
 * than one way.
 */
export function validateHierarchyTargets(rows: JsonObject[]): HierarchyTargetValidation {
  const providers = new Map<string, string[]>();
  const categories = new Map<string, string[]>();
  const categoryProviders = new Map<string, string>();
  const geographies = new Map<string, string[]>();
  const dimensions = new Map<string, string[]>();
  const dimensionValues = new Map<string, string[]>();
  const targets = new Map<string, string[]>();
  for (const row of rows) {
    const identity = readHierarchyTarget(row);
    recordLabel(providers, identity.source, identity.sourceLabel);
    recordLabel(categories, identity.variable, identity.variableLabel);
    const categoryProvider = categoryProviders.get(identity.variable);
    if (categoryProvider != null && categoryProvider !== identity.source) {
      throw new Error(
        `Schema 8 category ${identity.variable} references inconsistent ` +
          `providers: ${categoryProvider} and ${identity.source}.`,
      );
    }
    categoryProviders.set(identity.variable, identity.source);
    recordLabel(geographies, `${identity.level}\0${identity.geographyId}`, identity.geography);
    for (const dimension of identity.dimensions) {
      recordLabel(dimensions, dimension.key, dimension.label);
      recordLabel(dimensionValues, `${dimension.key}\0${dimension.value_id}`, dimension.value);
    }
    recordLabel(targets, identity.targetId, identity.targetLabel);
  }
  const joined = (separator: string) => (key: string) => key.split("\0").join(separator);
  return {
    label_variants: [
      ...variantsOf("provider", providers),
      ...variantsOf("category", categories),
      ...variantsOf("geography", geographies, joined(" ")),
      ...variantsOf("dimension", dimensions),
      ...variantsOf("dimension value", dimensionValues, joined("=")),
      ...variantsOf("target", targets),
    ],
  };
}
