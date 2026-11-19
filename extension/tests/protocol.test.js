// The messages the extension sends, checked against the fields of dubber/protocol.py: the
// orchestrator rejects a frame with a missing or an extra key.
const test = require("node:test");
const assert = require("node:assert");
const { hello, start, stop, state, videoIdOf } = require("../lib/protocol.js");

// A <video> element as the state report reads it.
const PLAYING = { currentTime: 61.5, playbackRate: 1.25, paused: false, seeking: false, readyState: 4, volume: 0.2 };

test("hello names the chrome client and the extension version", () => {
  assert.equal(JSON.stringify(hello("0.1.0")), '{"type":"hello","client":"chrome","version":"0.1.0"}');
});

test("start always carries mode null", () => {
  assert.equal(JSON.stringify(start()), '{"type":"start","mode":null}');
});

test("stop has nothing but its type", () => {
  assert.equal(JSON.stringify(stop()), '{"type":"stop"}');
});

test("state reports the video element", () => {
  assert.deepEqual(state(PLAYING, "XP5k0bHmhqc", false), {
    type: "state", video_id: "XP5k0bHmhqc", current_time: 61.5, rate: 1.25, paused: false, live: false, volume: 0.2,
  });
});

test("a seeking or buffering video counts as paused", () => {
  assert.equal(state({ ...PLAYING, paused: true }, "v", false).paused, true);
  assert.equal(state({ ...PLAYING, seeking: true }, "v", false).paused, true);
  assert.equal(state({ ...PLAYING, readyState: 2 }, "v", false).paused, true);
  assert.equal(state({ ...PLAYING, readyState: 3 }, "v", false).paused, false);
});

test("the live flag is a boolean", () => {
  assert.equal(state(PLAYING, "v", 1).live, true);
  assert.equal(state(PLAYING, "v", null).live, false);
});

test("the video id is the v query parameter", () => {
  assert.equal(videoIdOf("?v=XP5k0bHmhqc&t=10s"), "XP5k0bHmhqc");
  assert.equal(videoIdOf("?list=PL1&v=-abc_123XYZ"), "-abc_123XYZ");
  assert.equal(videoIdOf("?list=PL1"), null);
  assert.equal(videoIdOf(""), null);
});

const { captionsMessage } = require("../lib/protocol.js");

const EMPTY = { type: "captions", video_id: "v", lang: "en", kind: "asr", json3: { events: [] } };

test("captions message from the page script's reply", () => {
  const json3 = { wireMagic: "pb3", events: [{ tStartMs: 61880, segs: [{ utf8: "The" }] }] };
  assert.deepEqual(captionsMessage("v", { source: "dubber-page", id: "1", captions: { lang: "en", kind: "asr", json3 } }), {
    type: "captions", video_id: "v", lang: "en", kind: "asr", json3,
  });
});

test("a failed or missing reply gives the empty asr track", () => {
  assert.equal(JSON.stringify(captionsMessage("v", null)), '{"type":"captions","video_id":"v","lang":"en","kind":"asr","json3":{"events":[]}}');
  assert.deepEqual(captionsMessage("v", { error: "the video has no asr-en caption track" }), EMPTY);
});

test("a reply the orchestrator would refuse gives the empty asr track", () => {
  const json3 = { events: [] };
  assert.deepEqual(captionsMessage("v", { captions: { lang: "en", kind: "auto", json3 } }), EMPTY);
  assert.deepEqual(captionsMessage("v", { captions: { lang: 5, kind: "asr", json3 } }), EMPTY);
  assert.deepEqual(captionsMessage("v", { captions: { lang: "en", kind: "asr", json3: [] } }), EMPTY);
  assert.deepEqual(captionsMessage("v", { captions: { lang: "en", kind: "asr", json3: null } }), EMPTY);
});

// A button that works whenever it is pressed (2026-11-14): pressed while the server was not
// running, the start was lost, and the tab showed "starting" until the button was pressed twice more.
const { onSocket } = require("../lib/protocol.js");

test("a tab that is dubbing starts over when the socket comes up, and lets go when it drops", () => {
  assert.equal(onSocket(false, true, true), "restart"); // the server was started after the button, or came back
  assert.equal(onSocket(true, false, true), "release"); // the orchestrator's session ended with the socket
});

test("a tab that is not dubbing does nothing when the socket changes, and no change means nothing", () => {
  assert.equal(onSocket(false, true, false), null);
  assert.equal(onSocket(true, false, false), null);
  assert.equal(onSocket(true, true, true), null);
  assert.equal(onSocket(false, false, true), null);
});
