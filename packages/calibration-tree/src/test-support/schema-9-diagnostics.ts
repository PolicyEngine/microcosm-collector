// Test support: write a schema 8 calibration diagnostics document in the
// schema 9 layout, the way Microcosm's writer does. Provider, category,
// geography and dimension labels move into `hierarchy_catalogs`, declared
// once each, and every target row refers to them by ID. The target's own
// label stays on the row. Catalog entries are listed in the order the rows
// first use them. Like the writer, this refuses a document that labels one
// identifier more than one way, since a catalog holds one label.

type JsonObject = Record<string, unknown>;

interface Schema8Hierarchy {
  provider: { id: string; label: string };
  category: { id: string; label: string; provider_id: string };
  geography: { id: string; label: string; level: string };
  dimensions: Array<{ id: string; label: string; value_id: string; value_label: string }>;
  target: { id: string; label: string };
}

function recordOnce<Entry>(
  entries: Map<string, Entry>,
  id: string,
  entry: Entry,
  description: string,
): Entry {
  const known = entries.get(id);
  if (known === undefined) {
    entries.set(id, entry);
    return entry;
  }
  if (JSON.stringify(known) !== JSON.stringify(entry)) {
    throw new Error(
      `Cannot write ${description} ${id} in a catalog: rows give it ` +
        `${JSON.stringify(known)} and ${JSON.stringify(entry)}.`,
    );
  }
  return known;
}

export function schema9Diagnostics(schema8: JsonObject): JsonObject {
  if (schema8.schema_version !== 8) {
    throw new Error("Only a schema 8 document can be written as schema 9.");
  }
  const providers = new Map<string, { id: string; label: string }>();
  const categories = new Map<string, { id: string; label: string; provider_id: string }>();
  const geographies = new Map<string, { id: string; label: string; level: string }>();
  const dimensions = new Map<string, { id: string; label: string }>();
  const dimensionValues = new Map<string, Map<string, { id: string; label: string }>>();
  const targets = (schema8.targets as JsonObject[]).map((row) => {
    const hierarchy = row.hierarchy as Schema8Hierarchy;
    recordOnce(providers, hierarchy.provider.id, { ...hierarchy.provider }, "provider");
    recordOnce(categories, hierarchy.category.id, { ...hierarchy.category }, "category");
    recordOnce(geographies, hierarchy.geography.id, { ...hierarchy.geography }, "geography");
    for (const dimension of hierarchy.dimensions) {
      recordOnce(
        dimensions,
        dimension.id,
        { id: dimension.id, label: dimension.label },
        "dimension",
      );
      const values = dimensionValues.get(dimension.id) ?? new Map();
      dimensionValues.set(dimension.id, values);
      recordOnce(
        values,
        dimension.value_id,
        { id: dimension.value_id, label: dimension.value_label },
        `dimension ${dimension.id} value`,
      );
    }
    return {
      ...row,
      hierarchy: {
        category_id: hierarchy.category.id,
        geography_id: hierarchy.geography.id,
        dimensions: hierarchy.dimensions.map((dimension) => ({
          id: dimension.id,
          value_id: dimension.value_id,
        })),
        target: { ...hierarchy.target },
      },
    };
  });
  return {
    ...schema8,
    schema_version: 9,
    targets,
    hierarchy_catalogs: {
      providers: [...providers.values()],
      categories: [...categories.values()],
      geographies: [...geographies.values()],
      dimensions: [...dimensions.values()].map((dimension) => ({
        ...dimension,
        values: [...(dimensionValues.get(dimension.id)?.values() ?? [])],
      })),
    },
  };
}
