import { describe, expect, test } from "bun:test";

import { canonicalLabel, programLabel } from "./program-label";

describe("programLabel", () => {
  test("uses exact, curated labels for known program identifiers", () => {
    expect(programLabel("eitc")).toBe("EITC");
    expect(programLabel("cdcc")).toBe("CDCC");
    expect(programLabel("ctc")).toBe("CTC");
    expect(programLabel("agi")).toBe("AGI");
  });

  test("preserves curated acronyms inside multiword program identifiers", () => {
    expect(programLabel("ira distributions")).toBe("IRA distributions");
    expect(programLabel("salt deduction")).toBe("SALT deduction");
    expect(programLabel("assigned aca ptc")).toBe("Assigned ACA PTC");
    expect(programLabel("refundable ctc")).toBe("Refundable CTC");
  });

  test("sentence-cases ordinary canonical identifiers", () => {
    expect(programLabel("taxable_interest_income")).toBe(
      "Taxable interest income",
    );
    expect(programLabel("adjusted gross income")).toBe(
      "Adjusted gross income",
    );
    expect(programLabel("ordinary-dividend-income")).toBe(
      "Ordinary dividend income",
    );
  });

  test("does not guess that an unknown short identifier is an acronym", () => {
    expect(programLabel("xyz")).toBe("Xyz");
  });

  test("prefers an explicit artifact label when one becomes available", () => {
    expect(programLabel("eitc", "Earned Income Tax Credit")).toBe(
      "Earned Income Tax Credit",
    );
  });
});

describe("canonicalLabel", () => {
  test("formats dimension values with sentence case and curated acronyms", () => {
    expect(canonicalLabel("traditional ira deduction")).toBe(
      "Traditional IRA deduction",
    );
    expect(canonicalLabel("refundable cdcc amount")).toBe(
      "Refundable CDCC amount",
    );
    expect(canonicalLabel("self_employed_pension_contribution_deduction")).toBe(
      "Self employed pension contribution deduction",
    );
    expect(canonicalLabel("SSI Category")).toBe("SSI category");
    expect(canonicalLabel("Form W2 Item")).toBe("Form W2 item");
    expect(canonicalLabel("Z1 Series Code")).toBe("Z1 series code");
  });
});
