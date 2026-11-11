// A target's geography is identified by its level and code, but the dashboard
// shows, filters and groups targets by the geography's label. Microcosm's UK
// local-area builds calibrate parliamentary constituencies and local
// authorities side by side, and many of them share a name (Ashfield,
// Colchester, Midlothian, ...). A label used at more than one geography level
// therefore names its level as well, "Ashfield (constituency)" and "Ashfield
// (local authority)", so the explorer's tiles and filters, the targets page
// and the comparison maps keep the two areas apart. A label that only one
// level uses is left exactly as published, and so is the country's own
// national geography label: a target with no resolvable geography falls back
// to it at the national level, and it still means the same, whole country as
// a target that names it at the country level.

interface GeographyLabelledRow {
  geography?: unknown;
  level?: unknown;
}

function trimmedText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function levelPhrase(level: string): string {
  return level.replace(/[_-]+/g, " ");
}

/**
 * Returns the rows with every geography label that more than one level uses,
 * other than the national geography label, qualified by its level. Returns
 * the same array when no label is shared.
 */
export function levelQualifiedGeographyLabels<Row extends GeographyLabelledRow>(
  rows: Row[],
  nationalGeography: string,
): Row[] {
  const nationalLabel = nationalGeography.trim();
  const levelsByLabel = new Map<string, Set<string>>();
  for (const row of rows) {
    const label = trimmedText(row.geography);
    const level = trimmedText(row.level);
    if (!label || !level || label === nationalLabel) continue;
    const levels = levelsByLabel.get(label) ?? new Set<string>();
    levels.add(level);
    levelsByLabel.set(label, levels);
  }
  const sharedLabels = new Set(
    [...levelsByLabel]
      .filter(([, levels]) => levels.size > 1)
      .map(([label]) => label),
  );
  if (sharedLabels.size === 0) return rows;
  return rows.map((row) => {
    const label = trimmedText(row.geography);
    const level = trimmedText(row.level);
    return sharedLabels.has(label) && level
      ? { ...row, geography: `${label} (${levelPhrase(level)})` }
      : row;
  });
}
