// Dubber page script. It runs in the page's own JavaScript world, where YouTube's player API
// lives, and answers the requests the content script posts to the window.
//
// Captions come from the player's own /api/timedtext request: YouTube serves a URL built from
// the player response alone with an empty body, because only the player's request carries the
// signed `pot` and `signature` parameters. So this makes the player load the track through its
// API, takes the URL of the request it makes, and fetches it again as json3 (the method of
// tools/caption_tracks_snippet.js, verified in Chrome 156).
//
// The player's request is not always quick or good, and the browser's list of requests is not
// a safe place to look for it (seen in Chrome on 2026-11-14). YouTube clears that list itself,
// 2 s and 3 s after a page loads for one, so a request could be gone before it was looked for:
// a PerformanceObserver is told of every request as it ends, and those are kept here. In the
// first 2.5 s of a page the player asks without its token and gets an empty reply; and the
// first request for a track can take YouTube more than 5 s. So a slow request is waited for, an
// empty one is asked for again, and a track that was fetched is kept for the next Start.
(() => {
  "use strict";

  const { CaptionStore, describeCaptions, fetchTrack, findTimedtextUrl, rewriteTimedtextUrl, sourceTrack, TimedtextLog } =
    globalThis.dubberLib;

  // All the time one captions request may take, the waits and the downloads together. The
  // content script waits a little longer for the answer (CAPTIONS_TIMEOUT_MS).
  const BUDGET_MS = 16000;
  const POLL_MS = 100;

  const store = new CaptionStore(3);

  // Every /api/timedtext request of this page, from the ones already listed when this script
  // loads. The observer is told of them whatever the page does to the browser's own list.
  const requests = new TimedtextLog();
  try {
    new PerformanceObserver((list) => requests.add(list.getEntries())).observe({ type: "resource", buffered: true });
  } catch (error) {
    console.warn("[dubber] cannot watch the player's requests, so captions cannot be fetched:", error);
  }

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function reply(id, body) {
    window.postMessage({ source: "dubber-page", id, ...body }, window.location.origin);
  }

  // The caption track the player shows, or null when captions are off.
  function shownTrack(player) {
    try {
      const track = player.getOption("captions", "track");
      return track && track.languageCode ? track : null;
    } catch (error) {
      return null; // the captions module is not loaded: captions are off
    }
  }

  // Leaves the captions as they were: off again, or the old track selected again.
  function putBack(player, shown) {
    try {
      if (shown) player.setOption("captions", "track", shown);
      else player.unloadModule("captions");
    } catch (error) {
      console.warn("[dubber] could not put the captions back as they were:", error);
    }
  }

  // Makes the player request `track`. A player that holds the track already would not ask for
  // it again, so the captions module is unloaded first, whether or not it was loaded.
  function askPlayer(player, track) {
    try {
      player.unloadModule("captions");
    } catch (error) {
      // it was not loaded
    }
    player.loadModule("captions");
    player.setOption("captions", "track", { languageCode: track.languageCode });
  }

  // Fetches `url`; gives up after `timeoutMs`.
  async function download(url, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let text;
    try {
      const response = await fetch(url, { signal: controller.signal });
      if (!response.ok) throw new Error(`YouTube answered HTTP ${response.status}`);
      text = await response.text();
    } catch (error) {
      if (error && error.name === "AbortError") throw new Error("YouTube did not send the caption track in time");
      throw error;
    } finally {
      clearTimeout(timer);
    }
    if (!text.trim()) throw new Error("YouTube sent an empty reply");
    let json3;
    try {
      json3 = JSON.parse(text);
    } catch (error) {
      throw new Error("YouTube's reply is not JSON");
    }
    if (!json3 || !Array.isArray(json3.events) || json3.events.length === 0) {
      throw new Error("YouTube's reply holds no caption events");
    }
    return json3;
  }

  // Fetches the `want` track of video `videoId`: {lang, kind, json3}.
  async function fetchCaptions(want, videoId) {
    const kept = store.get(videoId, want);
    if (kept) return kept;
    const player = document.getElementById("movie_player");
    if (!player || typeof player.getPlayerResponse !== "function") {
      throw new Error("there is no YouTube player on this page");
    }
    const response = player.getPlayerResponse() || {};
    const loaded = (response.videoDetails || {}).videoId;
    if (loaded && loaded !== videoId) throw new Error(`the player holds video ${loaded}, not ${videoId}`);
    const tracks = ((response.captions || {}).playerCaptionsTracklistRenderer || {}).captionTracks || [];
    const track = sourceTrack(tracks, want);
    if (!track) {
      throw new Error(want === "en" ? "the video has no English captions, human or automatic" : `the video has no ${want} caption track`);
    }

    const about = { videoId, language: track.languageCode };
    const tlang = want === "translated-tr" ? "tr" : null;
    const shown = shownTrack(player);
    let asked = false;
    try {
      const { url, json3 } = await fetchTrack({
        // A request the player made earlier for this track, with captions shown, serves as well.
        earlierUrl: findTimedtextUrl(requests.all(), { ...about, asr: track.kind === "asr" }),
        ask: () => {
          asked = true;
          askPlayer(player, track);
        },
        entries: () => requests.all(),
        download: (requested, timeoutMs) => download(rewriteTimedtextUrl(requested, { fmt: "json3", tlang }), timeoutMs),
        now: () => performance.now(),
        wait,
        ...about,
        budgetMs: BUDGET_MS,
        pollMs: POLL_MS,
      });
      const captions = { ...describeCaptions(url, want, track), json3 };
      store.put(videoId, want, captions); // kept for the next Start on this video
      return captions;
    } finally {
      if (asked) putBack(player, shown); // a player that was not asked is as the owner left it
    }
  }

  // One captions request at a time, so that each finds the captions as the owner left them.
  let queue = Promise.resolve();

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const request = event.data;
    if (!request || request.source !== "dubber") return;
    if (request.cmd === "ping") {
      reply(request.id, { pong: true });
    } else if (request.cmd === "captions") {
      queue = queue
        .then(() => fetchCaptions(request.want, request.videoId))
        .then(
          (captions) => reply(request.id, { captions }),
          (error) => reply(request.id, { error: String((error && error.message) || error) })
        )
        .catch((error) => console.warn("[dubber] could not answer a captions request:", error));
    }
  });
})();
