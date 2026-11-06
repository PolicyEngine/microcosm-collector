// Country registry for the Microcosm dashboard: the one place a country is
// registered. Adding a country is one entry here; everything else (selectors,
// navigation, repository resolution, national geography, link behaviour) reads
// the registration or the release artifact's typed `country` block.
//
// Isomorphic: imported by client components and API routes alike, so it must
// not import server-only modules or read `process.env`. Repository resolvers
// accept an explicit environment object from server-side callers.

export const COUNTRY_CAPABILITIES = [
  "calibration",
  "targets",
  "compare",
  "cross_dataset",
  "staging",
  "model_coverage",
  "pipeline",
  "variables",
  "external_checks",
] as const;
export type CountryCapability = (typeof COUNTRY_CAPABILITIES)[number];

export type RepositoryVisibility = "public" | "private";

export type RepositoryEnvironment = Readonly<
  Record<string, string | undefined>
>;

export interface ResolvedRepository {
  repo: string;
  revision: string;
}

export interface CountryRegistration {
  // Default HF dataset repository and revision (the server may override both
  // through the deployment variables named in repo_env / revision_env).
  repo: string;
  revision: string;
  repo_env?: string;
  revision_env?: string;
  // The repository-root pointer naming the country's current release:
  // `latest.json` unless the country promotes its default line through a
  // per-line pointer (`latest-<line>.json`, microcosm#823) whose payload also
  // names the immutable tag the release is read at (`cut_tag`). Read only
  // while the dataset repository has no `releases.json`; a repository with
  // one is read from it and its build folders alone, whatever the
  // registration says (release-record.ts).
  pointer_path?: string;
  // Country display name ("United States").
  label: string;
  // Sidebar dataset line ("Microcosm US").
  dataset_label: string;
  // National geography label and id (null when the id is unknown).
  geography: string;
  geography_id: string | null;
  visibility: RepositoryVisibility;
  capabilities: readonly CountryCapability[];
  // Optional staging telemetry repository. A staging-capable country must
  // declare this instead of inheriting another country's repository.
  staging?: {
    repo: string;
    revision: string;
    repo_env?: string;
    revision_env?: string;
    token_env?: string;
    // Staging pipelines, by the `pipeline.id` their run manifests record,
    // whose candidates are local-area builds. The Staging page compares those
    // candidates with the newest published local-area release instead of the
    // current (national) release.
    local_area_pipelines?: readonly string[];
  };
  // Other jurisdiction codes a cross-dataset bundle may use for this country.
  jurisdiction_aliases?: readonly string[];
  // Conformance-only registration: a valid country that is never listed in
  // selectors or alert allowlists.
  fixture?: true;
}

const ALL_CAPABILITIES: readonly CountryCapability[] = COUNTRY_CAPABILITIES;

const CALIBRATION_CAPABILITIES: readonly CountryCapability[] = [
  "calibration",
  "targets",
  "compare",
  "cross_dataset",
];

// Deprecated upstream identifiers: Microcosm's published HF repositories and
// deployment variables still use the former Populace names.
export const COUNTRY_REGISTRY = {
  us: {
    repo: "policyengine/populace-us",
    revision: "main",
    repo_env: "POPULACE_HF_REPO",
    label: "United States",
    dataset_label: "Microcosm US",
    geography: "United States",
    geography_id: "0100000US",
    visibility: "public",
    capabilities: ALL_CAPABILITIES,
    staging: {
      // Deprecated upstream identifier: the US staging publisher still uses
      // the former Populace repository and deployment-variable names.
      repo: "policyengine/populace-us-staging",
      revision: "main",
      repo_env: "POPULACE_STAGING_HF_REPO",
      revision_env: "POPULACE_STAGING_HF_REVISION",
    },
  },
  uk: {
    repo: "policyengine/populace-uk-private",
    revision: "main",
    repo_env: "POPULACE_UK_HF_REPO",
    revision_env: "POPULACE_UK_HF_REVISION",
    label: "United Kingdom",
    dataset_label: "Microcosm UK",
    geography: "United Kingdom",
    geography_id: null,
    visibility: "private",
    capabilities: [...CALIBRATION_CAPABILITIES, "staging"],
    // microcosm#823: the UK default line is the 2024-25 national dataset,
    // promoted through its own pointer; `latest.json` stays on the June 2023
    // release and is never moved. Unread once the repository has
    // releases.json: Microcosm's switch writes it and deletes the pointer
    // files in one commit.
    pointer_path: "latest-national.json",
    staging: {
      repo: "policyengine/populace-uk-staging",
      revision: "main",
      repo_env: "POPULACE_UK_STAGING_HF_REPO",
      revision_env: "POPULACE_UK_STAGING_HF_REVISION",
      token_env: "POPULACE_UK_STAGING_HF_TOKEN",
      // Microcosm's rowwise local-area candidates (published as the dense
      // release). National attempts run the uk-frs-calibration pipeline.
      local_area_pipelines: ["uk-local-candidate"],
    },
    jurisdiction_aliases: ["GB"],
  },
  be: {
    repo: "policyengine/populace-be-private",
    revision: "main",
    repo_env: "POPULACE_BE_HF_REPO",
    revision_env: "POPULACE_BE_HF_REVISION",
    label: "Belgium",
    dataset_label: "Microcosm Belgium",
    geography: "Belgium",
    geography_id: null,
    visibility: "private",
    capabilities: CALIBRATION_CAPABILITIES,
  },
  // Synthetic repository used only by the third-country conformance fixture.
  zz: {
    repo: "policyengine/microcosm-zz-fixture",
    revision: "main",
    label: "Zedland",
    dataset_label: "Microcosm ZZ",
    geography: "Zedland",
    geography_id: null,
    visibility: "private",
    capabilities: ["calibration", "targets", "compare"],
    fixture: true,
  },
  // Synthetic non-US staging registration. It verifies that staging artifact
  // resolution is country-scoped without exposing an unfinished country in
  // selectors or treating its fixture repository as a published dataset.
  am: {
    repo: "policyengine/microcosm-am-fixture",
    revision: "main",
    label: "Armenia",
    dataset_label: "Microcosm Armenia",
    geography: "Armenia",
    geography_id: null,
    visibility: "private",
    capabilities: ["calibration", "targets", "compare", "staging"],
    staging: {
      repo: "policyengine/microcosm-am-staging-fixture",
      revision: "main",
    },
    fixture: true,
  },
} satisfies Record<string, CountryRegistration>;

export type MicrocosmCountry = keyof typeof COUNTRY_REGISTRY;

export const DEFAULT_COUNTRY: MicrocosmCountry = "us";

const COUNTRY_CODES = Object.keys(COUNTRY_REGISTRY) as MicrocosmCountry[];

export function isCountry(value: string | null | undefined): value is MicrocosmCountry {
  return value != null && Object.hasOwn(COUNTRY_REGISTRY, value);
}

export function countryRegistration(country: MicrocosmCountry): CountryRegistration {
  return COUNTRY_REGISTRY[country];
}

// A registered country the dashboard offers: every registration except the
// conformance fixtures.
export function isSelectableCountry(
  value: string | null | undefined,
): value is MicrocosmCountry {
  return isCountry(value) && !countryRegistration(value).fixture;
}

// What a request's `country` parameter names. Absent (null or "") selects the
// registry default. A present value must equal a selectable registration key
// exactly; anything else (an unregistered code such as "nz", another spelling
// such as "US", or a conformance fixture) is not configured. It never resolves
// to a different country, so one country's release is never served under
// another country's code.
export type CountryParam =
  | { configured: true; country: MicrocosmCountry; explicit: boolean }
  | { configured: false; requested: string };

export function countryParam(value: string | null | undefined): CountryParam {
  if (value == null || value === "") {
    return { configured: true, country: DEFAULT_COUNTRY, explicit: false };
  }
  return isSelectableCountry(value)
    ? { configured: true, country: value, explicit: true }
    : { configured: false, requested: value };
}

// "unknown_country": the dashboard has no such country. "no_published_release":
// the country is registered but the dashboard manifest names no release for it
// yet (a newly registered country before its first publication).
export type CountryNotConfiguredReason = "unknown_country" | "no_published_release";

export const COUNTRY_NOT_CONFIGURED = "country_not_configured";

export interface CountryNotConfiguredBody {
  error: typeof COUNTRY_NOT_CONFIGURED;
  requested_country: string;
  reason: CountryNotConfiguredReason;
  detail: string;
}

export class CountryNotConfiguredError extends Error {
  readonly requested: string;
  readonly reason: CountryNotConfiguredReason;

  constructor(requested: string, reason?: CountryNotConfiguredReason) {
    const resolvedReason =
      reason ?? (isSelectableCountry(requested) ? "no_published_release" : "unknown_country");
    super(countryNotConfiguredDetail(requested, resolvedReason));
    this.name = "CountryNotConfiguredError";
    this.requested = requested;
    this.reason = resolvedReason;
  }

  body(): CountryNotConfiguredBody {
    return {
      error: COUNTRY_NOT_CONFIGURED,
      requested_country: this.requested,
      reason: this.reason,
      detail: this.message,
    };
  }
}

function countryNotConfiguredDetail(
  requested: string,
  reason: CountryNotConfiguredReason,
): string {
  if (reason === "no_published_release" && isCountry(requested)) {
    return `No Microcosm calibration release has been published for ${countryRegistration(requested).label} yet.`;
  }
  return `No Microcosm calibration release is configured for country "${requested}".`;
}

export function isCountryNotConfiguredBody(value: unknown): value is CountryNotConfiguredBody {
  return (
    value != null &&
    typeof value === "object" &&
    (value as { error?: unknown }).error === COUNTRY_NOT_CONFIGURED &&
    typeof (value as { requested_country?: unknown }).requested_country === "string"
  );
}

export const DEFAULT_POINTER_PATH = "latest.json";

// The repository-root pointer a country's current release is resolved through.
export function countryPointerPath(country: MicrocosmCountry): string {
  return countryRegistration(country).pointer_path ?? DEFAULT_POINTER_PATH;
}

// Countries offered in selectors, in registry order; fixtures are excluded.
export function selectableCountries(): MicrocosmCountry[] {
  return COUNTRY_CODES.filter(isSelectableCountry);
}

export function countryCapabilities(country: MicrocosmCountry): readonly CountryCapability[] {
  return COUNTRY_REGISTRY[country].capabilities;
}

// Staging pipeline ids whose candidates are local-area builds; empty for
// countries that register none.
export function stagingLocalAreaPipelines(country: MicrocosmCountry): readonly string[] {
  return countryRegistration(country).staging?.local_area_pipelines ?? [];
}

export function hasCapability(country: MicrocosmCountry, capability: CountryCapability): boolean {
  return countryCapabilities(country).includes(capability);
}

function configuredValue(
  environment: RepositoryEnvironment,
  variableName: string | undefined,
  fallback: string,
): string {
  return (variableName ? environment[variableName] : undefined) ?? fallback;
}

export function resolveRegisteredRepository(
  country: MicrocosmCountry,
  environment: RepositoryEnvironment,
): ResolvedRepository {
  const registration = countryRegistration(country);
  return {
    repo: configuredValue(
      environment,
      registration.repo_env,
      registration.repo,
    ),
    revision: configuredValue(
      environment,
      registration.revision_env,
      registration.revision,
    ),
  };
}

export function resolveRegisteredStagingRepository(
  country: MicrocosmCountry,
  environment: RepositoryEnvironment,
): ResolvedRepository | null {
  const registration = countryRegistration(country);
  if (!hasCapability(country, "staging") || !registration.staging) return null;
  return {
    repo: configuredValue(
      environment,
      registration.staging.repo_env,
      registration.staging.repo,
    ),
    revision: configuredValue(
      environment,
      registration.staging.revision_env,
      registration.staging.revision,
    ),
  };
}

export function isCountryCapability(value: unknown): value is CountryCapability {
  return typeof value === "string" && (COUNTRY_CAPABILITIES as readonly string[]).includes(value);
}
