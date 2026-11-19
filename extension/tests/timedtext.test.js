// extension/tests/timedtext.test.js
const test = require("node:test"); const assert = require("node:assert");
const { rewriteTimedtextUrl, pickTimedtextUrl, chooseTrack } = require("../lib/timedtext.js");

test("rewrite sets fmt and tlang", () => {
  const u = "https://www.youtube.com/api/timedtext?v=x&fmt=srv3&lang=en&pot=abc";
  assert.equal(rewriteTimedtextUrl(u, { fmt: "json3", tlang: "tr" }), "https://www.youtube.com/api/timedtext?v=x&fmt=json3&lang=en&pot=abc&tlang=tr");
  assert.equal(rewriteTimedtextUrl(u + "&tlang=tr", { fmt: "json3", tlang: null }), "https://www.youtube.com/api/timedtext?v=x&fmt=json3&lang=en&pot=abc");
});

test("pick last timedtext resource", () => {
  assert.equal(pickTimedtextUrl(["a", "https://www.youtube.com/api/timedtext?1", "b", "https://www.youtube.com/api/timedtext?2"]), "https://www.youtube.com/api/timedtext?2");
  assert.equal(pickTimedtextUrl(["a"]), null);
});

test("choose track by preference", () => {
  const tracks = [{ languageCode: "en", kind: "asr" }, { languageCode: "en" }, { languageCode: "tr" }];
  assert.deepEqual(chooseTrack(tracks, "manual-tr"), { languageCode: "tr" });
  assert.deepEqual(chooseTrack(tracks, "asr-en"), { languageCode: "en", kind: "asr" });
  assert.equal(chooseTrack([{ languageCode: "de" }], "manual-en"), null);
});

// Beyond the brief: the cases the page script relies on.

const BASE = "https://www.youtube.com/api/timedtext";

test("rewrite keeps the signed values byte for byte", () => {
  // URLSearchParams would turn %20 into + and ~ into %7E.
  const u = `${BASE}?v=x&pot=a~b%20c%2Fd&signature=E1F2.%3D%3D&fmt=srv3`;
  assert.equal(rewriteTimedtextUrl(u, { fmt: "json3", tlang: null }), `${BASE}?v=x&pot=a~b%20c%2Fd&signature=E1F2.%3D%3D&fmt=json3`);
});

test("rewrite adds fmt when the url has none and replaces tlang in place", () => {
  assert.equal(rewriteTimedtextUrl(`${BASE}?v=x`, { fmt: "json3" }), `${BASE}?v=x&fmt=json3`);
  assert.equal(rewriteTimedtextUrl(`${BASE}?v=x&tlang=de&lang=en`, { tlang: "tr" }), `${BASE}?v=x&tlang=tr&lang=en`);
});

test("rewrite leaves a parameter it is not given", () => {
  assert.equal(rewriteTimedtextUrl(`${BASE}?v=x&tlang=de`, { fmt: "json3" }), `${BASE}?v=x&tlang=de&fmt=json3`);
  assert.equal(rewriteTimedtextUrl(`${BASE}?v=x&fmt=srv3`, { tlang: "tr" }), `${BASE}?v=x&fmt=srv3&tlang=tr`);
});

test("rewrite removes tlang wherever it sits", () => {
  assert.equal(rewriteTimedtextUrl(`${BASE}?tlang=tr&v=x`, { tlang: null }), `${BASE}?v=x`);
  assert.equal(rewriteTimedtextUrl(`${BASE}?v=x&tlang=tr&lang=en`, { tlang: null }), `${BASE}?v=x&lang=en`);
  assert.equal(rewriteTimedtextUrl(`${BASE}?v=x&lang=en`, { tlang: null }), `${BASE}?v=x&lang=en`);
});

test("rewrite does not touch a parameter whose name only ends the same", () => {
  assert.equal(rewriteTimedtextUrl(`${BASE}?v=x&xfmt=1&xtlang=2`, { fmt: "json3", tlang: null }), `${BASE}?v=x&xfmt=1&xtlang=2&fmt=json3`);
});

test("pick returns null for no resources", () => {
  assert.equal(pickTimedtextUrl([]), null);
});

test("choose track matches regional codes and keeps manual and asr apart", () => {
  const tracks = [{ languageCode: "en", kind: "asr" }, { languageCode: "en-GB" }, { languageCode: "tr-TR" }];
  assert.deepEqual(chooseTrack(tracks, "manual-en"), { languageCode: "en-GB" });
  assert.deepEqual(chooseTrack(tracks, "manual-tr"), { languageCode: "tr-TR" });
  assert.equal(chooseTrack([{ languageCode: "en" }], "asr-en"), null);
  assert.equal(chooseTrack([{ languageCode: "en", kind: "asr" }], "manual-en"), null);
});

test("choose track takes the first match", () => {
  const tracks = [{ languageCode: "en", name: "one" }, { languageCode: "en", name: "two" }];
  assert.deepEqual(chooseTrack(tracks, "manual-en"), { languageCode: "en", name: "one" });
});

test("choose track refuses an unknown preference", () => {
  assert.throws(() => chooseTrack([{ languageCode: "en" }], "manual-de"), /unknown caption preference/);
});

const { timedtextParam, findTimedtextUrl, sourceTrack, describeCaptions } = require("../lib/timedtext.js");

test("a timedtext parameter is read as text and decoded", () => {
  const u = `${BASE}?v=XP5k0bHmhqc&kind=asr&lang=en&name=My%20track`;
  assert.equal(timedtextParam(u, "kind"), "asr");
  assert.equal(timedtextParam(u, "name"), "My track");
  assert.equal(timedtextParam(u, "tlang"), null);
  assert.equal(timedtextParam(`${BASE}?xlang=de`, "lang"), null);
});

test("find takes the newest timedtext request made since the player was asked", () => {
  const entries = [
    { name: `${BASE}?v=V&lang=en&n=old`, startTime: 100 },
    { name: "https://i.ytimg.com/vi/V/hq.jpg", startTime: 600 },
    { name: `${BASE}?v=V&lang=en&n=1`, startTime: 500 },
    { name: `${BASE}?v=V&lang=en-GB&n=2`, startTime: 700 },
    { name: "https://www.youtube.com/youtubei/v1/log", startTime: 800 },
  ];
  assert.equal(findTimedtextUrl(entries, { since: 500, videoId: "V", language: "en" }), `${BASE}?v=V&lang=en-GB&n=2`);
  assert.equal(findTimedtextUrl(entries, { since: 900, videoId: "V", language: "en" }), null);
});

test("find skips requests for another video or language", () => {
  const entries = [
    { name: `${BASE}?v=V&lang=en&n=1`, startTime: 500 },
    { name: `${BASE}?v=OTHER&lang=en`, startTime: 600 },
    { name: `${BASE}?v=V&lang=de`, startTime: 700 },
  ];
  assert.equal(findTimedtextUrl(entries, { since: 0, videoId: "V", language: "en" }), `${BASE}?v=V&lang=en&n=1`);
  assert.equal(findTimedtextUrl([{ name: `${BASE}?pot=x`, startTime: 1 }], { since: 0, videoId: "V", language: "en" }), `${BASE}?pot=x`);
});

test("the source track for a translation is the English one", () => {
  const asr = { languageCode: "en", kind: "asr" };
  const manual = { languageCode: "en" };
  assert.deepEqual(sourceTrack([manual, asr], "translated-tr"), asr);
  assert.deepEqual(sourceTrack([manual], "translated-tr"), manual);
  assert.equal(sourceTrack([{ languageCode: "de" }], "translated-tr"), null);
  assert.deepEqual(sourceTrack([manual, asr], "manual-en"), manual);
});

test("the captions are described by the request the player really made", () => {
  const track = { languageCode: "en", kind: "asr" };
  assert.deepEqual(describeCaptions(`${BASE}?v=V&kind=asr&lang=en`, "asr-en", track), { lang: "en", kind: "asr" });
  // Asked for the auto-captions, the player loaded the human track.
  assert.deepEqual(describeCaptions(`${BASE}?v=V&lang=en-GB`, "asr-en", track), { lang: "en-GB", kind: "manual" });
  assert.deepEqual(describeCaptions(`${BASE}?v=V&pot=x`, "manual-en", { languageCode: "en" }), { lang: "en", kind: "manual" });
  assert.deepEqual(describeCaptions(`${BASE}?v=V&kind=asr&lang=en`, "translated-tr", track), { lang: "tr", kind: "translated" });
});

test("English captions prefer the human track and fall back to the automatic one", () => {
  const asr = { languageCode: "en", kind: "asr" };
  const manual = { languageCode: "en-GB" };
  assert.deepEqual(sourceTrack([asr, manual], "en"), manual); // human subtitles first, wherever listed
  assert.deepEqual(sourceTrack([asr], "en"), asr); // only automatic captions
  assert.deepEqual(sourceTrack([{ languageCode: "tr" }, manual], "en"), manual);
  assert.equal(sourceTrack([{ languageCode: "de" }, { languageCode: "tr", kind: "asr" }], "en"), null);
  assert.deepEqual(describeCaptions(`${BASE}?v=V&lang=en-GB`, "en", manual), { lang: "en-GB", kind: "manual" });
  assert.deepEqual(describeCaptions(`${BASE}?v=V&kind=asr&lang=en`, "en", asr), { lang: "en", kind: "asr" });
});

// Captions on every Start (2026-11-14). Seen in Chrome: in the first 2.5 s of a page the player
// asks without its token and YouTube answers 200 with an empty body; and the first request for a
// track can take more than 5 s, after which the old code cut it off and gave up.
const { unusableTimedtext, awaitTimedtextUrl, CaptionStore } = require("../lib/timedtext.js");

const filled = (startTime, extra = "") => ({ name: `${BASE}?v=x&lang=en&pot=p${startTime}${extra}`, startTime, responseStatus: 200, encodedBodySize: 2000, decodedBodySize: 9000 });
const empty = (startTime) => ({ name: `${BASE}?v=x&lang=en`, startTime, responseStatus: 200, encodedBodySize: 0, decodedBodySize: 0 });
const cutOff = (startTime) => ({ name: `${BASE}?v=x&lang=en&pot=p${startTime}`, startTime, responseStatus: 0, encodedBodySize: 0, decodedBodySize: 0 });
const ABOUT = { videoId: "x", language: "en" };

test("find skips a request that came back empty or was cut off", () => {
  const entries = [filled(10), empty(20), cutOff(30)];
  assert.equal(findTimedtextUrl(entries, { since: 0, ...ABOUT }), entries[0].name);
  assert.equal(findTimedtextUrl(entries, { since: 15, ...ABOUT }), null);
});

test("find without a since takes an earlier request, of the wanted kind when one is named", () => {
  const entries = [filled(10, "&kind=asr"), filled(20)];
  assert.equal(findTimedtextUrl(entries, ABOUT), entries[1].name);
  assert.equal(findTimedtextUrl(entries, { ...ABOUT, asr: true }), entries[0].name);
  assert.equal(findTimedtextUrl(entries, { ...ABOUT, asr: false }), entries[1].name);
  assert.equal(findTimedtextUrl([filled(10)], { ...ABOUT, asr: true }), null);
});

test("a request is unusable when it came back empty or was cut off, for this video and language, since the ask", () => {
  assert.equal(unusableTimedtext([empty(20)], { since: 10, ...ABOUT }), true);
  assert.equal(unusableTimedtext([cutOff(20)], { since: 10, ...ABOUT }), true);
  assert.equal(unusableTimedtext([empty(5)], { since: 10, ...ABOUT }), false); // before the ask
  assert.equal(unusableTimedtext([filled(20)], { since: 10, ...ABOUT }), false);
  assert.equal(unusableTimedtext([empty(20)], { since: 10, videoId: "y", language: "en" }), false);
  assert.equal(unusableTimedtext([{ name: `${BASE}?v=x&lang=en`, startTime: 20 }], { since: 10, ...ABOUT }), false); // no sizes: not known to be empty
});

// A clock that moves only when the code under test waits.
function clockAndPlayer(entriesAt) {
  const state = { t: 0, asks: [] };
  return {
    state,
    options: {
      ask: () => state.asks.push(state.t),
      entries: () => entriesAt(state.t, state.asks),
      now: () => state.t,
      wait: async (ms) => { state.t += ms; },
      ...ABOUT,
      budgetMs: 16000,
    },
  };
}

test("a slow first request is waited for, not asked for again", async () => {
  const { state, options } = clockAndPlayer((t) => (t >= 6000 ? [filled(1)] : []));
  assert.equal(await awaitTimedtextUrl(options), filled(1).name);
  assert.deepEqual(state.asks, [0]);
  assert.ok(state.t >= 6000 && state.t < 6200);
});

test("an empty reply is asked for again after a pause, until the track comes", async () => {
  // Asked at 0 and at about 900 ms: empty both times (no token yet). The third ask is answered.
  const { state, options } = clockAndPlayer((t, asks) => asks.map((at, i) => (i < 2 ? empty(at + 1) : filled(at + 1))).filter((e) => e.startTime + 100 <= t));
  assert.equal(await awaitTimedtextUrl(options), filled(state.asks[2] + 1).name);
  assert.equal(state.asks.length, 3);
  assert.ok(state.asks[1] - state.asks[0] >= 700 && state.asks[2] - state.asks[1] >= 700);
});

test("a player that never finishes a request is given up on when the time is spent", async () => {
  const { state, options } = clockAndPlayer(() => []);
  await assert.rejects(awaitTimedtextUrl(options), /YouTube did not answer the player's caption request within 16 s/);
  assert.deepEqual(state.asks, [0]);
  assert.ok(state.t >= 16000 && state.t < 16200);
});

test("a track that keeps coming back empty is asked for four times and no more", async () => {
  const { state, options } = clockAndPlayer((t, asks) => asks.map((at) => empty(at + 1)).filter((e) => e.startTime + 100 <= t));
  await assert.rejects(awaitTimedtextUrl(options), /YouTube sent no caption track 4 times/);
  assert.equal(state.asks.length, 4);
  assert.ok(state.t < 5000); // it does not wait out the 16 s once the asks are spent
});

test("the number of asks can be set, and the time still ends the wait", async () => {
  const always = (t, asks) => asks.map((at) => empty(at + 1)).filter((e) => e.startTime + 100 <= t);
  const two = clockAndPlayer(always);
  await assert.rejects(awaitTimedtextUrl({ ...two.options, maxAsks: 2 }), /YouTube sent no caption track 2 times/);
  assert.equal(two.state.asks.length, 2);
  const short = clockAndPlayer(always);
  await assert.rejects(awaitTimedtextUrl({ ...short.options, budgetMs: 1500, maxAsks: 50 }), /YouTube sent no caption track \d+ times/);
  assert.ok(short.state.t >= 1500 && short.state.t < 2500);
});

test("the caption store answers for the videos it holds and forgets the oldest", () => {
  const store = new CaptionStore(2);
  assert.equal(store.get("a", "en"), null);
  store.put("a", "en", { lang: "en", kind: "asr", json3: { events: [1] } });
  store.put("b", "en", { lang: "en", kind: "manual", json3: { events: [2] } });
  assert.deepEqual(store.get("a", "en").json3, { events: [1] });
  assert.equal(store.get("a", "translated-tr"), null); // another kind of request
  store.put("c", "en", { lang: "en", kind: "asr", json3: { events: [3] } });
  assert.equal(store.get("a", "en"), null); // the oldest went
  assert.ok(store.get("b", "en") && store.get("c", "en"));
});

// YouTube clears the resource timing buffer itself (seen 2 s and 3 s after a page loads), so a
// request can be gone before the buffer is looked at. A PerformanceObserver is told of each
// request as it ends, and this keeps what it is told.
const { TimedtextLog } = require("../lib/timedtext.js");

test("the log keeps the timedtext requests it is told of, as plain copies", () => {
  const log = new TimedtextLog();
  const entry = { name: `${BASE}?v=x&lang=en`, startTime: 20, responseStatus: 200, encodedBodySize: 0, decodedBodySize: 0, toJSON() {} };
  log.add([{ name: "https://i.ytimg.com/vi/x/hq.jpg", startTime: 10 }, entry]);
  log.add([filled(30)]);
  assert.deepEqual(log.all(), [
    { name: entry.name, startTime: 20, responseStatus: 200, encodedBodySize: 0, decodedBodySize: 0 },
    filled(30),
  ]);
  assert.equal(findTimedtextUrl(log.all(), { since: 0, ...ABOUT }), filled(30).name);
  assert.equal(unusableTimedtext(log.all(), { since: 0, ...ABOUT }), true);
});

test("the log forgets its oldest requests past its limit", () => {
  const log = new TimedtextLog(3);
  log.add([filled(1), filled(2)]);
  log.add([filled(3), filled(4), filled(5)]);
  assert.deepEqual(log.all().map((entry) => entry.startTime), [3, 4, 5]);
});

// The final review (2026-11-14): a download that failed was tried again at once and without end,
// 62 asks and 62 downloads in 16 s against a YouTube that answered 429.
const { fetchTrack } = require("../lib/timedtext.js");

const refused = (startTime) => ({ name: `${BASE}?v=x&lang=en&pot=p${startTime}`, startTime, responseStatus: 429, encodedBodySize: 300, decodedBodySize: 300 });

test("a request that YouTube refused is unusable, whatever its body", () => {
  assert.equal(unusableTimedtext([refused(20)], { since: 10, ...ABOUT }), true);
  assert.equal(findTimedtextUrl([filled(10), refused(20)], { since: 0, ...ABOUT }), filled(10).name);
  assert.equal(findTimedtextUrl([refused(20)], { since: 0, ...ABOUT }), null);
});

// The player answers every ask with what `answer(askNumber, at)` gives, 100 ms later.
function fetching(answer, download) {
  const state = { t: 0, asks: [], downloads: [] };
  const wait = async (ms) => { state.t += ms; };
  return {
    state,
    options: {
      ask: () => state.asks.push(state.t),
      entries: () => state.asks.map((at, i) => answer(i + 1, at + 1)).filter((e) => e && e.startTime + 100 <= state.t),
      download: async (url, timeoutMs) => {
        state.downloads.push({ url, timeoutMs, at: state.t });
        return download(url, timeoutMs, wait);
      },
      now: () => state.t,
      wait,
      ...ABOUT,
      budgetMs: 16000,
    },
  };
}

test("the player's request is downloaded and comes back with its url", async () => {
  const { state, options } = fetching((n, at) => filled(at), async () => ({ events: [1, 2] }));
  assert.deepEqual(await fetchTrack(options), { url: filled(1).name, json3: { events: [1, 2] } });
  assert.equal(state.asks.length, 1);
  assert.deepEqual(state.downloads.map((d) => d.url), [filled(1).name]);
});

test("an earlier request of the player is downloaded and the player left alone", async () => {
  const { state, options } = fetching(() => null, async () => ({ events: [1] }));
  assert.deepEqual(await fetchTrack({ ...options, earlierUrl: `${BASE}?v=x&lang=en&pot=old` }), { url: `${BASE}?v=x&lang=en&pot=old`, json3: { events: [1] } });
  assert.deepEqual(state.asks, []);
});

test("an earlier request that hangs is given 4 s, and then the player is asked", async () => {
  const old = `${BASE}?v=x&lang=en&pot=old`;
  const { state, options } = fetching((n, at) => filled(at), async (url, timeoutMs, wait) => {
    if (url !== old) return { events: [1] };
    await wait(timeoutMs);
    throw new Error("YouTube did not send the caption track in time");
  });
  const got = await fetchTrack({ ...options, earlierUrl: old });
  assert.equal(got.url, filled(state.asks[0] + 1).name);
  assert.equal(state.downloads[0].timeoutMs, 4000);
  assert.deepEqual(state.asks, [4000]);
});

test("a download that fails is tried once more after a pause, and no more", async () => {
  const { state, options } = fetching((n, at) => filled(at), async () => { throw new Error("YouTube answered HTTP 429"); });
  await assert.rejects(fetchTrack(options), /YouTube answered HTTP 429/);
  assert.equal(state.asks.length, 2);
  assert.equal(state.downloads.length, 2);
  assert.ok(state.asks[1] - state.downloads[0].at >= 1000); // the pause before the second try
  assert.ok(state.t < 5000);
});

test("a YouTube that refuses the player is asked five times in all", async () => {
  const { state, options } = fetching((n, at) => refused(at), async () => ({ events: [1] }));
  await assert.rejects(fetchTrack(options), /YouTube sent no caption track/);
  assert.equal(state.asks.length, 5);
  assert.deepEqual(state.downloads, []);
});

test("an empty answer early in a page, then the track: one download", async () => {
  const { state, options } = fetching((n, at) => (n < 3 ? empty(at) : filled(at)), async () => ({ events: [1] }));
  const got = await fetchTrack(options);
  assert.equal(got.url, filled(state.asks[2] + 1).name);
  assert.equal(state.asks.length, 3);
  assert.equal(state.downloads.length, 1);
});
