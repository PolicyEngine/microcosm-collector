// The golden harness changes process-wide state (repository environment
// variables and `globalThis.fetch`) while it builds a case, and
// `goldenComparisonRecord` builds both sides of a comparison under
// `Promise.all`. These tests run two scopes that overlap, finishing in the
// order that exposed each scope's state to the other.
import { afterEach, expect, test } from "bun:test";

import { COUNTRY_REGISTRY } from "@policyengine/calibration-tree/countries";
import {
  withHuggingFaceFiles,
  withRegisteredRepositories,
} from "./calibration-tree-golden-cases";

const originalFetch = globalThis.fetch;
const STAGING_REPOSITORY_VARIABLE = COUNTRY_REGISTRY.uk.staging.repo_env;
const originalStagingRepository = process.env[STAGING_REPOSITORY_VARIABLE];

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalStagingRepository == null) delete process.env[STAGING_REPOSITORY_VARIABLE];
  else process.env[STAGING_REPOSITORY_VARIABLE] = originalStagingRepository;
});

function signal(): { promise: Promise<void>; send: () => void } {
  let send = () => {};
  const promise = new Promise<void>((resolve) => {
    send = resolve;
  });
  return { promise, send };
}

test("overlapping repository scopes keep overrides cleared until the last one finishes", async () => {
  process.env[STAGING_REPOSITORY_VARIABLE] = "someone/staging-override";
  const firstScopeMayFinish = signal();
  const secondScopeMayFinish = signal();
  const secondScopeReadings: Array<string | undefined> = [];

  const firstScope = withRegisteredRepositories(async () => {
    await firstScopeMayFinish.promise;
  });
  const secondScope = withRegisteredRepositories(async () => {
    secondScopeReadings.push(process.env[STAGING_REPOSITORY_VARIABLE]);
    await secondScopeMayFinish.promise;
    secondScopeReadings.push(process.env[STAGING_REPOSITORY_VARIABLE]);
  });
  firstScopeMayFinish.send();
  await firstScope;
  secondScopeMayFinish.send();
  await secondScope;

  expect(secondScopeReadings).toEqual([undefined, undefined]);
  expect(process.env[STAGING_REPOSITORY_VARIABLE]).toBe("someone/staging-override");
});

test("overlapping Hugging Face file scopes each read only their own files", async () => {
  const fetchOutsideScopes = (async () => {
    throw new Error("Unexpected network request outside a Hugging Face file scope.");
  }) as unknown as typeof fetch;
  globalThis.fetch = fetchOutsideScopes;
  const firstUrl = "https://huggingface.co/datasets/first/resolve/main/first.json";
  const secondUrl = "https://huggingface.co/datasets/second/resolve/main/second.json";
  const firstScopeMayRead = signal();
  const secondScopeMayRead = signal();

  const firstScope = withHuggingFaceFiles(new Map([[firstUrl, '"first"']]), async () => {
    await firstScopeMayRead.promise;
    return (await fetch(firstUrl)).text();
  });
  const secondScope = withHuggingFaceFiles(new Map([[secondUrl, '"second"']]), async () => {
    await secondScopeMayRead.promise;
    return (await fetch(secondUrl)).text();
  });
  firstScopeMayRead.send();
  const firstBody = await firstScope;
  secondScopeMayRead.send();
  const secondBody = await secondScope;

  expect(firstBody).toBe('"first"');
  expect(secondBody).toBe('"second"');
  expect(globalThis.fetch).toBe(fetchOutsideScopes);
});
