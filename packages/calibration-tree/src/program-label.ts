// These are exact, domain-approved tokens—not guesses based on word length.
const CANONICAL_ACRONYM_LABELS: Readonly<Record<string, string>> = {
  aca: "ACA",
  actc: "ACTC",
  agi: "AGI",
  amt: "AMT",
  cdcc: "CDCC",
  ctc: "CTC",
  eitc: "EITC",
  fica: "FICA",
  fsa: "FSA",
  hsa: "HSA",
  ira: "IRA",
  magi: "MAGI",
  oasdi: "OASDI",
  ptc: "PTC",
  qbi: "QBI",
  salt: "SALT",
  se: "SE",
  snap: "SNAP",
  ssi: "SSI",
  tanf: "TANF",
  ui: "UI",
  w2: "W2",
  z1: "Z1",
};

function canonicalLabelKey(identifier: string): string {
  return identifier
    .trim()
    .toLowerCase()
    .replace(/[_\s-]+/g, "_");
}

/**
 * Resolve a canonical identifier's presentation label without changing its ID.
 * An upstream label wins when release artifacts begin supplying one; otherwise
 * exact domain overrides precede a conservative sentence-case fallback.
 */
export function canonicalLabel(
  identifier: string | null | undefined,
  explicitLabel?: string | null,
): string {
  const suppliedLabel = explicitLabel?.trim();
  if (suppliedLabel) return suppliedLabel;

  if (!identifier?.trim()) return "";
  const key = canonicalLabelKey(identifier);
  return key
    .split("_")
    .map((token, index) => {
      const acronym = CANONICAL_ACRONYM_LABELS[token];
      if (acronym) return acronym;
      return index === 0 ? token[0].toUpperCase() + token.slice(1) : token;
    })
    .join(" ");
}

export function programLabel(
  identifier: string | null | undefined,
  explicitLabel?: string | null,
): string {
  return canonicalLabel(identifier, explicitLabel);
}
