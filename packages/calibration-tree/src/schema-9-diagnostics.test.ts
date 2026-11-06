import { describe, expect, test } from "bun:test";

import recordedGoldens from "./fixtures/calibration-tree-golden.json";
import { buildCalibration } from "./latest-artifact";
import {
  goldenReleaseInputs,
  goldenReleaseRecordFromDocument,
  type CalibrationTreeGoldenRecord,
} from "./test-support/calibration-tree-golden-cases";
import { schema9Diagnostics } from "./test-support/schema-9-diagnostics";

type JsonObject = Record<string, unknown>;

const recorded = recordedGoldens as CalibrationTreeGoldenRecord;
const BUILD_TIMEOUT_MS = 300_000;
const RELEASE_ID = "microcosm-uk-schema-9-test";

function ukSchema8(): JsonObject {
  return JSON.parse(goldenReleaseInputs()["uk-release"].diagnosticsText) as JsonObject;
}

// Two US-style rows with two dimensions each, in producer order, so the
// expansion must keep each row's dimension order and look values up under
// their own dimension: both dimensions have a value `all`.
function usSchema8(): JsonObject {
  const row = (
    name: string,
    band: { id: string; label: string },
    status: { id: string; label: string },
  ) => ({
    name: `${name}@2024`,
    target_name: name,
    source: "IRS SOI citation",
    target: 100,
    initial_estimate: 90,
    final_estimate: 101,
    hierarchy: {
      provider: { id: "irs_soi", label: "IRS Statistics of Income" },
      category: { id: "irs_soi.eitc", label: "Earned income tax credit", provider_id: "irs_soi" },
      geography: { id: "0100000US", label: "United States", level: "country" },
      dimensions: [
        {
          id: "us:statutes/26/62#adjusted_gross_income",
          label: "Adjusted gross income",
          value_id: band.id,
          value_label: band.label,
        },
        { id: "filing_status", label: "Filing status", value_id: status.id, value_label: status.label },
      ],
      target: { id: name, label: `EITC, ${band.label}, ${status.label}` },
    },
  });
  return {
    schema_version: 8,
    targets: [
      row(
        "irs_soi.eitc.under_1.all",
        { id: "under_1", label: "No adjusted gross income" },
        { id: "all", label: "All filing statuses" },
      ),
      row(
        "irs_soi.eitc.all.joint",
        { id: "all", label: "All returns" },
        { id: "joint", label: "Married filing jointly" },
      ),
    ],
  };
}

function calibrationOf(document: JsonObject) {
  return buildCalibration(document, RELEASE_ID, null, {}, {}, {}, "uk");
}

function catalogs(document: JsonObject): Record<string, JsonObject[]> {
  return document.hierarchy_catalogs as Record<string, JsonObject[]>;
}

function rowHierarchy(document: JsonObject, index: number): JsonObject {
  return (document.targets as JsonObject[])[index].hierarchy as JsonObject;
}

function refusal(mutate: (document: JsonObject) => void): string {
  const document = schema9Diagnostics(ukSchema8());
  mutate(document);
  try {
    calibrationOf(document);
  } catch (error) {
    return error instanceof Error ? error.message : String(error);
  }
  throw new Error("The schema 9 document was accepted.");
}

describe("schema 9 calibration diagnostics", () => {
  test("a schema 9 file reads into the rows of the equivalent schema 8 file", () => {
    for (const schema8 of [ukSchema8(), usSchema8()]) {
      const fromSchema8 = calibrationOf(schema8);
      const fromSchema9 = calibrationOf(schema9Diagnostics(schema8));

      expect(fromSchema9.rows).toEqual(fromSchema8.rows);
      expect(fromSchema9.hierarchy_label_variants).toEqual([]);
      expect(fromSchema9.schema_version).toBe(9);
      expect(fromSchema9.target_schema).toEqual({
        diagnostics_schema_version: 9,
        structured_dimensions: false,
        target_representation: "hierarchy",
      });
    }
  });

  test(
    "a schema 9 file builds the recorded UK release tree byte for byte",
    async () => {
      // Compiled under the recorded schema 8 file's identity (its source
      // digest and so its build ID); every file and reader response must
      // match the recorded schema 8 tree.
      expect(
        await goldenReleaseRecordFromDocument("uk-release", schema9Diagnostics(ukSchema8())),
      ).toEqual(recorded.sources["uk-release"]);
    },
    BUILD_TIMEOUT_MS,
  );

  test("a document without hierarchy catalogs is refused", () => {
    expect(refusal((document) => {
      delete document.hierarchy_catalogs;
    })).toBe("Schema 9 diagnostics must have a hierarchy_catalogs object.");
    expect(refusal((document) => {
      delete catalogs(document).geographies;
    })).toBe("Schema 9 hierarchy_catalogs.geographies must be an array.");
  });

  test("a catalog that lists one ID twice is refused", () => {
    expect(refusal((document) => {
      const geographies = catalogs(document).geographies;
      geographies.push({ ...geographies[0], label: "UK" });
    })).toBe("Schema 9 hierarchy_catalogs.geographies lists K02000001 more than once.");
    expect(refusal((document) => {
      catalogs(document).providers.push({ id: "obr", label: "OBR" });
    })).toBe("Schema 9 hierarchy_catalogs.providers lists obr more than once.");
    expect(refusal((document) => {
      const categories = catalogs(document).categories;
      categories.push({ ...categories[0] });
    })).toBe("Schema 9 hierarchy_catalogs.categories lists obr.efo_receipts more than once.");
    expect(refusal((document) => {
      const dimensions = catalogs(document).dimensions;
      dimensions.push({ ...dimensions[0] });
    })).toBe(
      "Schema 9 hierarchy_catalogs.dimensions lists obr.efo_receipts_line more than once.",
    );
    expect(refusal((document) => {
      const values = catalogs(document).dimensions[0].values as JsonObject[];
      values.push({ id: "income_tax", label: "Income tax" });
    })).toBe(
      "Schema 9 hierarchy_catalogs.dimensions entry obr.efo_receipts_line lists value " +
        "income_tax more than once.",
    );
  });

  test("a category whose provider the catalog does not list is refused", () => {
    expect(refusal((document) => {
      catalogs(document).categories[0].provider_id = "hmrc";
    })).toBe(
      "Schema 9 category obr.efo_receipts refers to provider hmrc, which " +
        "hierarchy_catalogs.providers does not list.",
    );
  });

  test("a row that refers to an unknown category, geography or dimension is refused", () => {
    expect(refusal((document) => {
      rowHierarchy(document, 0).category_id = "obr.efo_spending";
    })).toBe(
      "Schema 9 target obr.income_tax.K02000001@2025 refers to category obr.efo_spending, " +
        "which hierarchy_catalogs.categories does not list.",
    );
    expect(refusal((document) => {
      rowHierarchy(document, 0).geography_id = "W92000004";
    })).toBe(
      "Schema 9 target obr.income_tax.K02000001@2025 refers to geography W92000004, " +
        "which hierarchy_catalogs.geographies does not list.",
    );
    expect(refusal((document) => {
      (rowHierarchy(document, 0).dimensions as JsonObject[])[0].id = "obr.efo_line";
    })).toBe(
      "Schema 9 target obr.income_tax.K02000001@2025 refers to dimension obr.efo_line, " +
        "which hierarchy_catalogs.dimensions does not list.",
    );
  });

  test("a row that refers to a value under the wrong dimension is refused", () => {
    // `under_1` is listed, but under the income-band dimension, not under
    // filing status.
    const document = schema9Diagnostics(usSchema8());
    (rowHierarchy(document, 0).dimensions as JsonObject[])[1].value_id = "under_1";
    expect(() => calibrationOf(document)).toThrow(
      "Schema 9 target irs_soi.eitc.under_1.all@2024 refers to value under_1 of dimension " +
        "filing_status, which that dimension does not list.",
    );
  });

  test("a row that lists one dimension twice is refused", () => {
    const document = schema9Diagnostics(usSchema8());
    const dimensions = rowHierarchy(document, 0).dimensions as JsonObject[];
    dimensions[1] = { ...dimensions[0] };
    expect(() => calibrationOf(document)).toThrow(
      "Schema 9 target irs_soi.eitc.under_1.all@2024 lists dimension " +
        "us:statutes/26/62#adjusted_gross_income more than once.",
    );
  });

  test("a row in the schema 8 layout is refused under schema version 9", () => {
    const schema8 = ukSchema8();
    expect(() => calibrationOf({
      ...schema9Diagnostics(schema8),
      targets: schema8.targets,
    })).toThrow(
      "Schema 9 target obr.income_tax.K02000001@2025 hierarchy.category_id must be a " +
        "non-empty string.",
    );
  });

  test("a row that also carries schema 8 labels is refused", () => {
    // Microcosm's schema 9 model and JSON Schema forbid undeclared fields, so
    // an inline label beside a catalog reference can only disagree with the
    // catalog; the reader must not silently prefer one of them.
    expect(refusal((document) => {
      rowHierarchy(document, 0).geography = {
        id: "K02000001",
        label: "UK",
        level: "country",
      };
    })).toBe(
      "Schema 9 target obr.income_tax.K02000001@2025 hierarchy has field geography, " +
        "which schema 9 does not define.",
    );
    expect(refusal((document) => {
      const dimensions = rowHierarchy(document, 0).dimensions as JsonObject[];
      dimensions[0].value_label = "Income tax receipts";
    })).toBe(
      "Schema 9 target obr.income_tax.K02000001@2025 hierarchy.dimensions[0] has field " +
        "value_label, which schema 9 does not define.",
    );
  });

  test("a row whose hierarchy target is another target is refused", () => {
    expect(refusal((document) => {
      (rowHierarchy(document, 0).target as JsonObject).id = "obr.national_insurance.K02000001";
    })).toBe(
      "Schema 9 target obr.income_tax.K02000001@2025 hierarchy.target.id " +
        "obr.national_insurance.K02000001 does not match target_name obr.income_tax.K02000001.",
    );
  });
});
