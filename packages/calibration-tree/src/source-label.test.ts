import { describe, expect, test } from "bun:test";

import { sourceLabel } from "./source-label";

describe("sourceLabel", () => {
  test("uses the same curated source names throughout Microcosm", () => {
    expect(sourceLabel("irs_soi")).toBe("IRS Statistics of Income");
    expect(sourceLabel("cms_medicaid")).toBe("CMS · Medicaid / CHIP");
    expect(sourceLabel("usda_snap")).toBe("USDA · SNAP");
  });

  test("formats an unknown source without changing its identifier", () => {
    const source = "new_data_source";

    expect(sourceLabel(source)).toBe("New Data Source");
    expect(source).toBe("new_data_source");
  });
});
