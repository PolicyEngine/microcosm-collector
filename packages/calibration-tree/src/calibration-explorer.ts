export const FIT_BANDS = [
  "0_5",
  "5_10",
  "10_20",
  "20_40",
  "40_plus",
  "unscored",
] as const;

export type FitBand = (typeof FIT_BANDS)[number];
export const COMPARISON_FITS = [
  "improved",
  "regressed",
  "unchanged",
  "not_applicable",
] as const;
export type ComparisonFit = (typeof COMPARISON_FITS)[number];
export type CalibrationStatus = "included" | "skipped";
export type ExplorerBreakdown = "program" | "geography";

export interface ExplorerDimensionSelection {
  key: string;
  label?: string;
  value: string;
}

export interface ExplorerPath {
  source?: string;
  program?: string;
  geography?: string;
  dimensions: ExplorerDimensionSelection[];
  target?: string;
}

export interface ExplorerFilters {
  geographyLevels: string[];
  geographies: string[];
  fitBands: FitBand[];
  comparisonFits: ComparisonFit[];
  calibrationStatuses: CalibrationStatus[];
}

export interface ExplorerState {
  breakdown: ExplorerBreakdown;
  path: ExplorerPath;
  filters: ExplorerFilters;
}

export type ExplorerNodeSelection =
  | { kind: "program"; source: string; value: string }
  | { kind: "geography"; value: string }
  | { kind: "dimension_value"; key: string; label: string; value: string }
  | { kind: "target"; value: string };

export type ExplorerAction =
  | { type: "select"; selection: ExplorerNodeSelection }
  | { type: "navigate"; path: ExplorerPath }
  | { type: "up" }
  | { type: "breakdown"; breakdown: ExplorerBreakdown }
  | { type: "filters"; filters: ExplorerFilters }
  | { type: "clear_target" };

export function calibrationExplorerSourceIdentity({
  country,
  release,
  stagingRunId,
}: {
  country: string;
  release?: string;
  stagingRunId?: string;
}): string {
  return JSON.stringify(
    stagingRunId
      ? [country, "staging", stagingRunId]
      : [country, "release", release ?? null],
  );
}

export function createExplorerFilters(): ExplorerFilters {
  return {
    geographyLevels: [],
    geographies: [],
    fitBands: [],
    comparisonFits: [],
    calibrationStatuses: [],
  };
}

export function createExplorerState(): ExplorerState {
  return {
    breakdown: "program",
    path: { dimensions: [] },
    filters: createExplorerFilters(),
  };
}

export function selectExplorerNode(
  state: ExplorerState,
  selection: ExplorerNodeSelection,
): ExplorerState {
  const path = { ...state.path, dimensions: [...state.path.dimensions] };
  delete path.target;
  if (selection.kind === "program") {
    if (state.breakdown === "geography" && !path.geography) return state;
    path.source = selection.source;
    path.program = selection.value;
    if (state.breakdown === "program") delete path.geography;
    path.dimensions = [];
  } else if (selection.kind === "geography") {
    if (state.breakdown === "program" && (!path.source || !path.program)) {
      return state;
    }
    if (state.breakdown === "geography") {
      delete path.source;
      delete path.program;
    }
    path.geography = selection.value;
    path.dimensions = [];
  } else if (selection.kind === "dimension_value") {
    if (!path.geography && (!path.source || !path.program)) return state;
    path.dimensions = [
      ...path.dimensions.filter((dimension) => dimension.key !== selection.key),
      {
        key: selection.key,
        label: selection.label,
        value: selection.value,
      },
    ];
  } else {
    if (!path.geography && (!path.source || !path.program)) return state;
    path.target = selection.value;
  }
  return { ...state, path };
}

export function nextLevelExplorerStates(
  state: ExplorerState,
  selections: ExplorerNodeSelection[],
): ExplorerState[] {
  return selections.flatMap((selection) =>
    selection.kind === "target"
      ? []
      : [selectExplorerNode(state, selection)],
  );
}

export function parentExplorerState(state: ExplorerState): ExplorerState {
  const path = { ...state.path, dimensions: [...state.path.dimensions] };
  delete path.target;
  if (path.dimensions.length) {
    path.dimensions.pop();
  } else if (path.geography && path.source && path.program) {
    if (state.breakdown === "program") {
      delete path.geography;
    } else {
      delete path.program;
      delete path.source;
    }
  } else if (path.geography) {
    delete path.geography;
  } else if (path.program || path.source) {
    delete path.program;
    delete path.source;
  }
  return { ...state, path };
}

export function explorerReducer(
  state: ExplorerState,
  action: ExplorerAction,
): ExplorerState {
  if (action.type === "select") {
    return selectExplorerNode(state, action.selection);
  }
  if (action.type === "navigate") {
    const path = {
      ...action.path,
      dimensions: [...action.path.dimensions],
    };
    delete path.target;
    return { ...state, path };
  }
  if (action.type === "up") return parentExplorerState(state);
  if (action.type === "breakdown") {
    return {
      ...state,
      breakdown: action.breakdown,
      path: { dimensions: [] },
    };
  }
  if (action.type === "filters") {
    return {
      ...state,
      filters: action.filters,
      path: { ...state.path, dimensions: [...state.path.dimensions], target: undefined },
    };
  }
  return {
    ...state,
    path: { ...state.path, dimensions: [...state.path.dimensions], target: undefined },
  };
}
