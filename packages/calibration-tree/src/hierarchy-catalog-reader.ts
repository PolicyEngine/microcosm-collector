// Diagnostics schema 9 declares each provider, category, geography and
// dimension label once, in top-level `hierarchy_catalogs`, and each target
// row refers to them by ID; only the target's own label stays on the row.
// One geography ID or one dimension value ID therefore has exactly one label
// in the file. This module expands a schema 9 document into the schema 8 row
// shape, so the schema 8 reader, the tree builder and every page read both
// versions through the same code:
//
//   hierarchy_catalogs: {
//     providers:   [{ id, label }],
//     categories:  [{ id, label, provider_id }],
//     geographies: [{ id, label, level }],
//     dimensions:  [{ id, label, values: [{ id, label }] }],
//   }
//   targets[].hierarchy: {
//     category_id, geography_id,
//     dimensions: [{ id, value_id }],   // in producer order
//     target: { id, label },
//   }
//
// The provider is the category's. A file that is structurally inconsistent
// (missing catalogs, an ID listed twice, a reference to an entry the catalogs
// do not list, a value looked up under a dimension that does not list it) is
// refused, as is a row hierarchy field schema 9 does not define (such as an
// inline schema 8 label). IDs are compared after trimming whitespace, as the
// schema 8 reader trims them.

type JsonObject = Record<string, unknown>;

export const HIERARCHY_CATALOG_SCHEMA_VERSION = 9;

function isPlainObject(value: unknown): value is JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function requiredObject(value: unknown, message: string): JsonObject {
  if (!isPlainObject(value)) throw new Error(message);
  return value;
}

function requiredString(value: unknown, message: string): string {
  if (typeof value !== "string" || !value.trim()) throw new Error(message);
  return value;
}

function catalogEntries(catalogs: JsonObject, name: string): JsonObject[] {
  const entries = catalogs[name];
  if (!Array.isArray(entries)) {
    throw new Error(`Schema 9 hierarchy_catalogs.${name} must be an array.`);
  }
  return entries.map((entry, index) =>
    requiredObject(entry, `Schema 9 hierarchy_catalogs.${name}[${index}] must be an object.`)
  );
}

/** Index entries by trimmed ID, refusing an ID listed twice. */
function byId<Entry extends { id: string }>(
  entries: Entry[],
  duplicateMessage: (id: string) => string,
): Map<string, Entry> {
  const indexed = new Map<string, Entry>();
  for (const entry of entries) {
    const key = entry.id.trim();
    if (indexed.has(key)) throw new Error(duplicateMessage(key));
    indexed.set(key, entry);
  }
  return indexed;
}

interface DimensionCatalogEntry {
  id: string;
  label: string;
  values: Map<string, { id: string; label: string }>;
}

interface HierarchyCatalogs {
  providers: Map<string, { id: string; label: string }>;
  categories: Map<string, { id: string; label: string; provider_id: string }>;
  geographies: Map<string, { id: string; label: string; level: string }>;
  dimensions: Map<string, DimensionCatalogEntry>;
}

function readCatalogs(document: JsonObject): HierarchyCatalogs {
  const catalogs = requiredObject(
    document.hierarchy_catalogs,
    "Schema 9 diagnostics must have a hierarchy_catalogs object.",
  );
  const field = (catalog: string, index: number, name: string) =>
    `Schema 9 hierarchy_catalogs.${catalog}[${index}].${name} must be a non-empty string.`;
  const listedTwice = (catalog: string) => (id: string) =>
    `Schema 9 hierarchy_catalogs.${catalog} lists ${id} more than once.`;

  const providers = byId(
    catalogEntries(catalogs, "providers").map((entry, index) => ({
      id: requiredString(entry.id, field("providers", index, "id")),
      label: requiredString(entry.label, field("providers", index, "label")),
    })),
    listedTwice("providers"),
  );
  const categories = byId(
    catalogEntries(catalogs, "categories").map((entry, index) => ({
      id: requiredString(entry.id, field("categories", index, "id")),
      label: requiredString(entry.label, field("categories", index, "label")),
      provider_id: requiredString(entry.provider_id, field("categories", index, "provider_id")),
    })),
    listedTwice("categories"),
  );
  for (const category of categories.values()) {
    if (!providers.has(category.provider_id.trim())) {
      throw new Error(
        `Schema 9 category ${category.id.trim()} refers to provider ` +
          `${category.provider_id.trim()}, which hierarchy_catalogs.providers does not list.`,
      );
    }
  }
  const geographies = byId(
    catalogEntries(catalogs, "geographies").map((entry, index) => ({
      id: requiredString(entry.id, field("geographies", index, "id")),
      label: requiredString(entry.label, field("geographies", index, "label")),
      level: requiredString(entry.level, field("geographies", index, "level")),
    })),
    listedTwice("geographies"),
  );
  const dimensions = byId(
    catalogEntries(catalogs, "dimensions").map((entry, index) => {
      const id = requiredString(entry.id, field("dimensions", index, "id"));
      if (!Array.isArray(entry.values)) {
        throw new Error(`Schema 9 hierarchy_catalogs.dimensions[${index}].values must be an array.`);
      }
      const values = byId(
        entry.values.map((value, valueIndex) => {
          const valueField = (name: string) =>
            `Schema 9 hierarchy_catalogs.dimensions[${index}].values[${valueIndex}].${name} ` +
            "must be a non-empty string.";
          const valueEntry = requiredObject(
            value,
            `Schema 9 hierarchy_catalogs.dimensions[${index}].values[${valueIndex}] must be an object.`,
          );
          return {
            id: requiredString(valueEntry.id, valueField("id")),
            label: requiredString(valueEntry.label, valueField("label")),
          };
        }),
        (valueId) =>
          `Schema 9 hierarchy_catalogs.dimensions entry ${id.trim()} lists value ` +
          `${valueId} more than once.`,
      );
      return {
        id,
        label: requiredString(entry.label, field("dimensions", index, "label")),
        values,
      };
    }),
    listedTwice("dimensions"),
  );
  return { providers, categories, geographies, dimensions };
}

const ROW_HIERARCHY_FIELDS = new Set(["category_id", "geography_id", "dimensions", "target"]);
const DIMENSION_REFERENCE_FIELDS = new Set(["id", "value_id"]);

/**
 * Refuse a field schema 9 does not define, as Microcosm's model and JSON
 * Schema do: an inline schema 8 label beside a catalog reference could only
 * disagree with the catalog.
 */
function refuseUndefinedFields(
  value: JsonObject,
  allowed: Set<string>,
  location: string,
): void {
  for (const field of Object.keys(value)) {
    if (!allowed.has(field)) {
      throw new Error(`${location} has field ${field}, which schema 9 does not define.`);
    }
  }
}

function rowName(row: JsonObject, index: number): string {
  for (const candidate of [row.name, row.target_name]) {
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  return `row ${index}`;
}

function expandRowHierarchy(
  row: JsonObject,
  index: number,
  catalogs: HierarchyCatalogs,
): JsonObject {
  const name = rowName(row, index);
  const reference = requiredObject(row.hierarchy, `Schema 9 target ${name} hierarchy must be an object.`);
  const referenceField = (field: string) =>
    `Schema 9 target ${name} hierarchy.${field} must be a non-empty string.`;
  const notListed = (kind: string, id: string, catalog: string) =>
    `Schema 9 target ${name} refers to ${kind} ${id}, which hierarchy_catalogs.${catalog} ` +
    "does not list.";

  const categoryId = requiredString(reference.category_id, referenceField("category_id")).trim();
  const category = catalogs.categories.get(categoryId);
  if (!category) throw new Error(notListed("category", categoryId, "categories"));
  // readCatalogs checked that the catalog lists every category's provider.
  const provider = catalogs.providers.get(category.provider_id.trim())!;

  const geographyId = requiredString(reference.geography_id, referenceField("geography_id")).trim();
  const geography = catalogs.geographies.get(geographyId);
  if (!geography) throw new Error(notListed("geography", geographyId, "geographies"));

  if (!Array.isArray(reference.dimensions)) {
    throw new Error(`Schema 9 target ${name} hierarchy.dimensions must be an array.`);
  }
  const seenDimensions = new Set<string>();
  const dimensions = reference.dimensions.map((value, rank) => {
    const dimensionReference = requiredObject(
      value,
      `Schema 9 target ${name} hierarchy.dimensions[${rank}] must be an object.`,
    );
    const dimensionId = requiredString(
      dimensionReference.id,
      referenceField(`dimensions[${rank}].id`),
    ).trim();
    if (seenDimensions.has(dimensionId)) {
      throw new Error(`Schema 9 target ${name} lists dimension ${dimensionId} more than once.`);
    }
    seenDimensions.add(dimensionId);
    const dimension = catalogs.dimensions.get(dimensionId);
    if (!dimension) throw new Error(notListed("dimension", dimensionId, "dimensions"));
    const valueId = requiredString(
      dimensionReference.value_id,
      referenceField(`dimensions[${rank}].value_id`),
    ).trim();
    const dimensionValue = dimension.values.get(valueId);
    if (!dimensionValue) {
      throw new Error(
        `Schema 9 target ${name} refers to value ${valueId} of dimension ${dimensionId}, ` +
          "which that dimension does not list.",
      );
    }
    refuseUndefinedFields(
      dimensionReference,
      DIMENSION_REFERENCE_FIELDS,
      `Schema 9 target ${name} hierarchy.dimensions[${rank}]`,
    );
    return {
      id: dimension.id,
      label: dimension.label,
      value_id: dimensionValue.id,
      value_label: dimensionValue.label,
    };
  });

  const target = requiredObject(reference.target, `Schema 9 target ${name} hierarchy.target must be an object.`);
  const targetId = requiredString(target.id, referenceField("target.id"));
  requiredString(target.label, referenceField("target.label"));
  const rowTargetId = typeof row.target_name === "string" ? row.target_name.trim() : "";
  if (rowTargetId && rowTargetId !== targetId.trim()) {
    throw new Error(
      `Schema 9 target ${name} hierarchy.target.id ${targetId.trim()} does not match ` +
        `target_name ${rowTargetId}.`,
    );
  }

  // Checked after the required fields, so a schema 8 row reports its missing
  // category_id first.
  refuseUndefinedFields(reference, ROW_HIERARCHY_FIELDS, `Schema 9 target ${name} hierarchy`);

  // The schema 8 hierarchy, with the producer's key order.
  return {
    provider: { id: provider.id, label: provider.label },
    category: { id: category.id, label: category.label, provider_id: category.provider_id },
    geography: { id: geography.id, label: geography.label, level: geography.level },
    dimensions,
    target: { id: target.id, label: target.label },
  };
}

/**
 * The document every reader below `buildCalibration` sees. A schema 9
 * document becomes a copy whose target rows carry the schema 8 hierarchy,
 * without `hierarchy_catalogs`; its `schema_version` stays 9. Any other
 * document is returned unchanged, as the same object.
 */
export function expandHierarchyCatalogs(document: JsonObject): JsonObject {
  if (document.schema_version !== HIERARCHY_CATALOG_SCHEMA_VERSION) return document;
  const catalogs = readCatalogs(document);
  if (!Array.isArray(document.targets)) {
    throw new Error("Schema 9 diagnostics must have a targets array.");
  }
  const targets = document.targets.map((value, index) => {
    const row = requiredObject(value, `Schema 9 target row ${index} must be an object.`);
    return { ...row, hierarchy: expandRowHierarchy(row, index, catalogs) };
  });
  const { hierarchy_catalogs: _catalogs, ...rest } = document;
  return { ...rest, targets };
}
