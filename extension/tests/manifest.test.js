// The manifest's content scripts: each world must get every file it needs, once.
//
// Chrome injects a file listed in two content_scripts entries into one world only
// (https://issues.chromium.org/issues/324096753). When lib/timedtext.js was listed for both the
// isolated world and the page world, the page world never got it: page.js stopped at its first
// line and every captions request ran into the content script's 10 s timeout.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifest.json"), "utf8"));
const entries = manifest.content_scripts;
const pageEntry = entries.find((entry) => entry.world === "MAIN");
const isolatedEntry = entries.find((entry) => (entry.world || "ISOLATED") === "ISOLATED");

test("no script is listed in two content-script entries", () => {
  const seen = new Map();
  entries.forEach((entry, index) => {
    for (const file of entry.js || []) {
      assert.ok(!seen.has(file), `${file} is listed in entries ${seen.get(file)} and ${index}`);
      seen.set(file, index);
    }
  });
});

test("the page-world scripts run on their own and listen for requests", () => {
  const listened = [];
  const sandbox = {
    addEventListener: (type) => listened.push(type),
    postMessage: () => {},
    location: { origin: "https://www.youtube.com" },
    performance: { getEntriesByType: () => [], now: () => 0, setResourceTimingBufferSize: () => {} },
    setTimeout,
    console,
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  for (const file of pageEntry.js) {
    vm.runInContext(fs.readFileSync(path.join(root, file), "utf8"), context, { filename: file });
  }
  assert.deepStrictEqual(listened, ["message"]);
});

test("every helper content.js uses comes from a file of its own entry", () => {
  const provided = new Set();
  for (const file of isolatedEntry.js.filter((name) => name.startsWith("lib/"))) {
    Object.keys(require(path.join(root, file))).forEach((name) => provided.add(name));
  }
  const used = new Set(fs.readFileSync(path.join(root, "content.js"), "utf8").match(/\blib\.(\w+)/g).map((m) => m.slice(4)));
  const missing = [...used].filter((name) => !provided.has(name));
  assert.deepStrictEqual(missing, []);
});
