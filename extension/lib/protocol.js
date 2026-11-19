// The messages the extension sends to the orchestrator, shaped exactly as dubber/protocol.py
// expects them: the orchestrator rejects a frame with a missing or an extra key.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module !== null && typeof exports === "object" && module.exports === exports) {
    module.exports = api;
  }
  root.dubberLib = Object.assign(root.dubberLib || {}, api);
})(globalThis, function () {
  "use strict";

  // HTMLMediaElement.HAVE_FUTURE_DATA: below it the video cannot play on.
  const HAVE_FUTURE_DATA = 3;

  function hello(version) {
    return { type: "hello", client: "chrome", version: String(version) };
  }

  /** Start dubbing; the orchestrator picks the mode, so `mode` is always null. */
  function start() {
    return { type: "start", mode: null };
  }

  function stop() {
    return { type: "stop" };
  }

  /**
   * The player's state, read from the <video> element. A video that is seeking or buffering
   * counts as paused, so that it never runs the orchestrator's clock.
   */
  function state(video, videoId, live) {
    return {
      type: "state",
      video_id: videoId,
      current_time: video.currentTime,
      rate: video.playbackRate,
      paused: video.paused || video.seeking || video.readyState < HAVE_FUTURE_DATA,
      live: Boolean(live),
      volume: video.volume,
    };
  }

  const KINDS = ["manual", "asr", "translated"];

  /**
   * The captions message for the page script's `reply` ({captions: {lang, kind, json3}} or
   * {error}). A failed, missing (null) or malformed reply gives an empty English auto-caption
   * track, so the orchestrator ends the start with "no usable captions" instead of holding the
   * player for captions that never come.
   */
  function captionsMessage(videoId, reply) {
    const captions = reply && reply.captions;
    const usable =
      captions &&
      typeof captions.lang === "string" &&
      KINDS.includes(captions.kind) &&
      captions.json3 !== null &&
      typeof captions.json3 === "object" &&
      !Array.isArray(captions.json3);
    const track = usable ? captions : { lang: "en", kind: "asr", json3: { events: [] } };
    return { type: "captions", video_id: videoId, lang: track.lang, kind: track.kind, json3: track.json3 };
  }

  /**
   * What a tab does when the worker's socket to the orchestrator changes from `wasConnected` to
   * `connected`, given whether the tab is `dubbing`: "restart" when the socket comes up (the
   * server was started after the button was pressed, or came back, and knows of no session),
   * "release" when it drops (the orchestrator's session ended with it), else null.
   */
  function onSocket(wasConnected, connected, dubbing) {
    if (!dubbing || wasConnected === connected) return null;
    return connected ? "restart" : "release";
  }

  /** The `v` parameter of a watch page's query string (location.search), or null. */
  function videoIdOf(search) {
    return new URLSearchParams(search).get("v") || null;
  }

  return { hello, start, stop, state, captionsMessage, onSocket, videoIdOf };
});
