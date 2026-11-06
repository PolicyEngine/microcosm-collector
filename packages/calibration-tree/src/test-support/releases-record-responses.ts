// Hugging Face's answers to a request for `releases.json`, for the synthetic
// repositories in tests. A dataset repository that has no `releases.json`
// still publishes through its tags and pointer files; Hugging Face then
// answers 404 with the error code `EntryNotFound`. A 404 whose error code
// says the repository or the revision is missing is a different answer: the
// readers must not take it to mean that the repository has no record.

/** The 404 for a file the revision does not have: no `releases.json`. */
export function missingReleasesRecord(): Response {
  return new Response("Entry not found", {
    status: 404,
    headers: { "X-Error-Code": "EntryNotFound" },
  });
}

/** The 404 for a revision the repository does not have. */
export function missingRevision(): Response {
  return new Response("Revision not found", {
    status: 404,
    headers: { "X-Error-Code": "RevisionNotFound" },
  });
}
