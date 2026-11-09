// Shared presentation names for source authorities. Keys remain stable data
// identifiers; these labels are display-only and never participate in filters
// or joins.
const SOURCE_LABELS: Record<string, string> = {
  bea: "BEA",
  cbo: "CBO",
  census_acs: "Census · American Community Survey",
  census_pep: "Census · Population Estimates Program",
  census_population: "Census population",
  census_population_projections: "Census · Population Projections",
  census_stc: "Census · State Tax Collections",
  cms_aca: "CMS · ACA marketplace",
  cms_medicaid: "CMS · Medicaid / CHIP",
  cms_medicare: "CMS · Medicare",
  cms_nhe: "CMS · National Health Expenditure Accounts",
  ed_fsa: "ED · Federal Student Aid",
  eurostat: "Eurostat",
  federal_reserve: "Federal Reserve",
  hhs_acf_liheap: "HHS · LIHEAP",
  hhs_acf_tanf: "HHS · TANF",
  ici: "Investment Company Institute",
  irs_soi: "IRS Statistics of Income",
  jct: "JCT",
  jrc: "JRC",
  kff: "KFF",
  ssa: "SSA",
  statbel: "Statbel",
  onss: "ONSS",
  sfpd: "SFPD",
  nasa: "NASA",
  state_income_tax: "State income tax",
  usda_snap: "USDA · SNAP",
};

const SOURCE_ACRONYMS = new Set([
  "aca",
  "acf",
  "acs",
  "api",
  "bea",
  "cbo",
  "cms",
  "hhs",
  "ici",
  "irs",
  "jct",
  "jrc",
  "kff",
  "liheap",
  "nhe",
  "pep",
  "soi",
  "ssa",
  "onss",
  "sfpd",
  "stc",
  "tanf",
  "usda",
]);

export function sourceAuthorityLabel(source: string): string {
  if (Object.hasOwn(SOURCE_LABELS, source)) return SOURCE_LABELS[source];
  return source
    .split("_")
    .map((word) =>
      SOURCE_ACRONYMS.has(word.toLowerCase())
        ? word.toUpperCase()
        : word.charAt(0).toUpperCase() + word.slice(1),
    )
    .join(" ");
}
