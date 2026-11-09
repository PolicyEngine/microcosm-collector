export type ChronicleMetadata = Record<string, unknown>;

const CURRENT_PREFIX = "chronicle_";
const LEGACY_PREFIX = "ledger_";

function asMetadata(value: unknown): ChronicleMetadata {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as ChronicleMetadata)
    : {};
}

function hasValue(value: unknown): boolean {
  return typeof value === "string" ? value.trim().length > 0 : value != null;
}

/**
 * Canonicalize artifact metadata after Ledger's rename to Chronicle.
 *
 * Published releases can contain either namespace. Preserve the original
 * fields for diagnostics, while exposing every legacy `ledger_*` field under
 * its canonical `chronicle_*` name. A populated Chronicle value always wins
 * when both forms are present.
 *
 * This intentionally aliases arbitrary suffixes rather than maintaining a
 * field allowlist, so newly published metadata remains backward-compatible.
 */
export function normalizeChronicleMetadata(
  value: unknown,
): ChronicleMetadata {
  const metadata = asMetadata(value);
  const normalized = { ...metadata };

  for (const [key, legacyValue] of Object.entries(metadata)) {
    if (!key.startsWith(LEGACY_PREFIX) || !hasValue(legacyValue)) continue;
    const canonicalKey = `${CURRENT_PREFIX}${key.slice(LEGACY_PREFIX.length)}`;
    if (!hasValue(normalized[canonicalKey])) {
      normalized[canonicalKey] = legacyValue;
    }
  }

  return normalized;
}
