const test = require("node:test");
const assert = require("node:assert");
const { statusLine } = require("../lib/overlay.js");

const PLAYING = {
  type: "status", mode: "captions", state: "playing", buffered_ahead: 23.456, lag: 0,
  engine: "identity+say:Samantha", message: "",
};

test("without a socket the line says what to do and what happens then, whatever the last status said", () => {
  assert.equal(
    statusLine({ connected: false, busy: false, status: PLAYING }),
    "Dubber · the server is not running (uv run dubber serve): dubbing starts when it is"
  );
});

test("another tab is dubbing", () => {
  assert.equal(statusLine({ connected: true, busy: true, status: null }), "Dubber · another tab is dubbing");
});

test("starting until the first status arrives", () => {
  assert.equal(statusLine({ connected: true, busy: false, status: null }), "Dubber · starting");
});

test("mode, state, engine and seconds ahead with one decimal", () => {
  assert.equal(
    statusLine({ connected: true, busy: false, status: PLAYING }),
    "Dubber · captions · playing · identity+say:Samantha · 23.5 s ahead"
  );
});

test("the status message and the notes come last", () => {
  const status = { ...PLAYING, state: "idle", buffered_ahead: 0, message: "no usable captions" };
  assert.equal(
    statusLine({ connected: true, busy: false, status, notes: ["captions: timed out", ""] }),
    "Dubber · captions · idle · identity+say:Samantha · 0.0 s ahead · no usable captions · captions: timed out"
  );
});

const { subtitleAt } = require("../lib/overlay.js");

test("the subtitle whose [start, end) holds the time is shown", () => {
  const subtitles = [{ text: "one", start: 1, end: 2 }, { text: "two", start: 3, end: 4.5 }];
  assert.equal(subtitleAt(subtitles, 1), "one");
  assert.equal(subtitleAt(subtitles, 1.99), "one");
  assert.equal(subtitleAt(subtitles, 2), "");
  assert.equal(subtitleAt(subtitles, 4), "two");
  assert.equal(subtitleAt(subtitles, 0.5), "");
  assert.equal(subtitleAt(subtitles, 5), "");
  assert.equal(subtitleAt([], 1), "");
});

test("where subtitles overlap, the one received last wins", () => {
  // After a seek the orchestrator voices a segment again and sends its subtitle anew.
  const subtitles = [{ text: "old", start: 10, end: 14 }, { text: "new", start: 11, end: 13 }];
  assert.equal(subtitleAt(subtitles, 12), "new");
  assert.equal(subtitleAt(subtitles, 13.5), "old");
});
