// Validates one Orrery graph document with the pinned @axiom-foundation/orrery,
// accepting exactly the graph file contents microcosm-runs' publication
// accepts (lib/service.ts at PolicyEngine/microcosm-runs
// 204aa72247fd1589d3ee3a07fc4fe453f3692d93). Its checks on the publication
// itself (file sizes and names) are the Orrery worker's.
//
// 1. the bytes decode as strict UTF-8 and parse as JSON ("invalid_json");
// 2. Orrery's parseGraphDocument accepts the document ("invalid_graph");
// 3. checkSources: every `url` is either an https:// URL without a user name
//    or password, or names, before any `#`, a file published with the
//    document ("invalid_source").
//
//   node validate-orrery-document.mjs DOCUMENT.json [PUBLISHED_FILE_NAME ...]
//
// It prints one JSON line. Exit 0: {"valid": true, "orrery_version": ...}.
// Exit 2, the document is refused: {"valid": false, "orrery_version": ...,
// "code": ..., "message": ..., "issues": [{"path", "message"}, ...]}, with at
// most 100 issues of at most 500 characters each, all encodable as UTF-8. A
// refusal says where a
// link is, never what it holds, since a link may carry a credential. Exit 1:
// the validator could not run (a wrong argument, an unreadable file, an
// unexpected error), with the reason on standard error.
import { readFileSync } from "node:fs";

import { GraphValidationError, parseGraphDocument } from "@axiom-foundation/orrery";

// The package's exports do not include its package.json, so it is read from
// the installed copy.
const ORRERY_VERSION = JSON.parse(
  readFileSync(new URL("./node_modules/@axiom-foundation/orrery/package.json", import.meta.url), "utf8"),
).version;
// Keeps the answer small for a document with many problems, or with long
// identities, which Orrery repeats in its messages.
const MAX_ISSUES = 100;
const MAX_ISSUE_CHARACTERS = 500;

// Counts and keeps whole characters (code points, as Python counts them), and
// replaces any unpaired surrogate with U+FFFD: a document's JSON can escape
// one ("\ud83d"), Orrery repeats identities in its issues, and UTF-8 cannot
// encode it.
function shortened(text) {
  const characters = Array.from(text.toWellFormed());
  return characters.length > MAX_ISSUE_CHARACTERS
    ? `${characters.slice(0, MAX_ISSUE_CHARACTERS - 1).join("")}…`
    : characters.join("");
}

class DocumentRefusal extends Error {
  constructor(code, message, issues = []) {
    super(message);
    this.code = code;
    this.issues = issues;
  }
}

// A JSON path such as $.nodes[3].url, for a key of an array or an object.
function childPath(path, parent, key) {
  return Array.isArray(parent) ? `${path}[${key}]` : `${path}.${key}`;
}

function sourceRefusal(path, message) {
  return new DocumentRefusal("invalid_source", message, [{ path: shortened(path), message }]);
}

// microcosm-runs' checkSources, visiting the same values, except that an
// https:// value that is not a URL is refused here, where microcosm-runs
// answers 503 for it.
function checkSources(value, names) {
  const pending = [[value, "$"]];
  while (pending.length) {
    const [current, path] = pending.pop();
    if (!current || typeof current !== "object") continue;
    for (const [key, item] of Object.entries(current)) {
      const itemPath = childPath(path, current, key);
      if (key === "url" && typeof item === "string") {
        if (item.startsWith("https://")) {
          let url;
          try {
            url = new URL(item);
          } catch {
            throw sourceRefusal(itemPath, "A link is not a URL.");
          }
          if (url.username || url.password) {
            throw sourceRefusal(itemPath, "A link carries a user name or password.");
          }
        } else if (!names.has(item.split("#", 1)[0])) {
          throw sourceRefusal(itemPath, "A link is neither an https:// URL nor a file published with the document.");
        }
      }
      if (item && typeof item === "object") pending.push([item, itemPath]);
    }
  }
}

function validate(bytes, names) {
  let document;
  try {
    document = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw new DocumentRefusal("invalid_json", "The document is not UTF-8 JSON.");
  }
  // microcosm-runs refuses the document whatever parseGraphDocument throws.
  try {
    parseGraphDocument(document);
  } catch (error) {
    const issues = error instanceof GraphValidationError ? error.issues : [];
    throw new DocumentRefusal(
      "invalid_graph",
      "Orrery refused the document.",
      issues.slice(0, MAX_ISSUES).map(({ path, message }) => ({
        path: shortened(String(path)),
        message: shortened(String(message)),
      })),
    );
  }
  checkSources(document, names);
}

function main(argv) {
  if (argv.length < 1) {
    console.error("Usage: node validate-orrery-document.mjs DOCUMENT.json [PUBLISHED_FILE_NAME ...]");
    return 1;
  }
  const [documentPath, ...names] = argv;
  let bytes;
  try {
    bytes = readFileSync(documentPath);
  } catch (error) {
    console.error(`${documentPath} could not be read: ${error.message}`);
    return 1;
  }
  try {
    validate(bytes, new Set(names));
  } catch (error) {
    if (!(error instanceof DocumentRefusal)) {
      console.error(error instanceof Error ? error.stack ?? error.message : String(error));
      return 1;
    }
    console.log(
      JSON.stringify({
        valid: false,
        orrery_version: ORRERY_VERSION,
        code: error.code,
        message: error.message,
        issues: error.issues,
      }),
    );
    return 2;
  }
  console.log(JSON.stringify({ valid: true, orrery_version: ORRERY_VERSION }));
  return 0;
}

process.exitCode = main(process.argv.slice(2));
