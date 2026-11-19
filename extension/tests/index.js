// `node --test extension/tests/` hands this directory to Node as a single module (Node 22 does
// not search a directory named on the command line), so this file loads every test file in it.
const fs = require("node:fs");
const path = require("node:path");

for (const name of fs.readdirSync(__dirname).sort()) {
  if (name.endsWith(".test.js")) require(path.join(__dirname, name));
}
