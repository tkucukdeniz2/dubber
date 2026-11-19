// What the overlay on the player shows.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module !== null && typeof exports === "object" && module.exports === exports) {
    module.exports = api;
  }
  root.dubberLib = Object.assign(root.dubberLib || {}, api);
})(globalThis, function () {
  "use strict";

  /**
   * The overlay's status line. `connected` says whether the service worker has the socket,
   * `busy` that another tab is dubbing, `status` is the last status message (null before the
   * first), and `notes` are the content script's own remarks; empty notes are left out.
   */
  function statusLine({ connected, busy, status, notes = [] }) {
    const parts = ["Dubber"];
    if (!connected) {
      // The tab stays in dubbing and starts by itself once the worker has its socket.
      parts.push("the server is not running (uv run dubber serve): dubbing starts when it is");
    } else if (busy) {
      parts.push("another tab is dubbing");
    } else if (!status) {
      parts.push("starting");
    } else {
      parts.push(status.mode, status.state, status.engine, `${Number(status.buffered_ahead).toFixed(1)} s ahead`);
      if (status.message) parts.push(status.message);
    }
    if (connected && !busy) parts.push(...notes.filter(Boolean));
    return parts.join(" · ");
  }

  /**
   * The text of the subtitle whose [start, end) holds the video time `t`, or "". Where
   * subtitles overlap, the one received last wins: after a seek the orchestrator may voice a
   * segment again and send its subtitle anew.
   */
  function subtitleAt(subtitles, t) {
    for (let i = subtitles.length - 1; i >= 0; i--) {
      const { text, start, end } = subtitles[i];
      if (start <= t && t < end) return text;
    }
    return "";
  }

  return { statusLine, subtitleAt };
});
