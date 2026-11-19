// Dubber content script. The toolbar button starts or stops dubbing the video in this tab.
// While dubbing, the script reports the player's state to the orchestrator through the service
// worker, sends it the video's captions, applies its duck and control messages to the video,
// and shows its status and subtitles in an overlay on the player.
(() => {
  "use strict";

  const lib = globalThis.dubberLib;

  const REPORT_EVERY_MS = 250;
  const RECONNECT_PORT_MS = 1000;
  // The page script takes up to 16 s (its BUDGET_MS): a slow or empty answer from YouTube is waited out.
  const CAPTIONS_TIMEOUT_MS = 20000;
  // A state report goes out on each of these events, besides every REPORT_EVERY_MS.
  const STATE_EVENTS = ["play", "pause", "seeking", "seeked", "ratechange", "waiting", "playing", "stalled"];

  let port = null; // the Port to the service worker
  let connected = false; // whether the service worker has its socket to the orchestrator
  let busy = false; // another tab is dubbing
  let status = null; // the orchestrator's last status message
  let session = null; // while dubbing: see startDubbing
  let overlay = null;

  function post(msg) {
    if (!port) return;
    try {
      port.postMessage(msg);
    } catch (error) {
      // The worker went away; onDisconnect reconnects.
    }
  }

  function findVideo() {
    return document.querySelector("#movie_player video") || document.querySelector("video.html5-main-video");
  }

  function report() {
    if (!session) return;
    const player = document.getElementById("movie_player");
    const live = Boolean(player && player.querySelector(".ytp-live"));
    post(lib.state(session.video, session.videoId, live));
  }

  function onVideoEvent(event) {
    if (event.type === "playing" && session && session.notes.play) {
      session.notes.play = ""; // the video plays after all
      render();
    }
    report();
  }

  function onVolumeChange() {
    if (session) session.player.volumeChanged();
  }

  function setText(element, text) {
    if (element.textContent !== text) element.textContent = text;
  }

  function render() {
    if (!session) return;
    const player = document.getElementById("movie_player");
    if (!player) return;
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "dubber-overlay";
      for (const name of ["dubber-status", "dubber-subtitle"]) {
        const line = document.createElement("div");
        line.className = name;
        overlay.append(line);
      }
    }
    if (overlay.parentNode !== player) player.append(overlay);
    const { notes, subtitles, video } = session;
    setText(overlay.firstChild, lib.statusLine({ connected, busy, status, notes: [notes.captions, notes.play] }));
    setText(overlay.lastChild, lib.subtitleAt(subtitles, video.currentTime));
  }

  function giveBack(player, play) {
    player.restore({ play }).catch((error) => console.warn("[dubber] could not play the video again:", error));
  }

  function startDubbing() {
    const video = findVideo();
    const videoId = lib.videoIdOf(location.search);
    if (!video || !videoId) {
      console.info("[dubber] open a YouTube video to dub it");
      return;
    }
    const volume = new lib.VolumeDuck(() => video.volume, (level) => {
      video.volume = level;
    });
    session = {
      video,
      videoId,
      player: new lib.PlayerControl(video, volume),
      subtitles: [],
      notes: { captions: "", play: "" },
      captions: null, // the pending captions request: { id, timer }
      timer: setInterval(tick, REPORT_EVERY_MS),
    };
    status = null;
    busy = false;
    for (const type of STATE_EVENTS) video.addEventListener(type, onVideoEvent);
    video.addEventListener("volumechange", onVolumeChange);
    video.addEventListener("timeupdate", render);
    post(lib.start());
    report();
    requestCaptions();
    render();
  }

  // Starts the orchestrator's session for a tab that is dubbing already: the button was
  // pressed before the server ran, or the server went away and came back. It knows of no
  // session then, so the tab sends what a first start sends.
  function restartDubbing() {
    if (session.captions) clearTimeout(session.captions.timer);
    session.captions = null;
    session.subtitles = [];
    session.notes = { captions: "", play: "" };
    status = null;
    busy = false;
    post(lib.start());
    report();
    requestCaptions();
  }

  // Ends dubbing in this tab and gives the player back. With `play`, a player the orchestrator
  // holds paused plays again; leaving the video or the page does not.
  function stopDubbing({ play }) {
    if (!session) return;
    post(lib.stop());
    const { video, player, timer, captions } = session;
    clearInterval(timer);
    if (captions) clearTimeout(captions.timer);
    for (const type of STATE_EVENTS) video.removeEventListener(type, onVideoEvent);
    video.removeEventListener("volumechange", onVolumeChange);
    video.removeEventListener("timeupdate", render);
    session = null;
    status = null;
    busy = false;
    if (overlay) overlay.remove();
    giveBack(player, play);
  }

  function tick() {
    if (!session) return;
    if (lib.videoIdOf(location.search) !== session.videoId) {
      stopDubbing({ play: false }); // YouTube moved on to another video in this tab
      return;
    }
    report();
    render();
  }

  // The page script fetches the captions; one captions message always follows a start.
  function requestCaptions() {
    const id = crypto.randomUUID();
    session.captions = { id, timer: setTimeout(() => sendCaptions(id, null), CAPTIONS_TIMEOUT_MS) };
    const request = { source: "dubber", id, cmd: "captions", want: "en", videoId: session.videoId };
    window.postMessage(request, window.location.origin);
  }

  function onPageMessage(event) {
    if (event.source !== window) return;
    const reply = event.data;
    if (!reply || reply.source !== "dubber-page") return;
    sendCaptions(reply.id, reply);
  }

  // Sends the captions of the page script's `reply`, or an empty track when it failed or did
  // not come (null), so that the orchestrator never holds the player for captions.
  function sendCaptions(id, reply) {
    if (!session || !session.captions || session.captions.id !== id) return;
    clearTimeout(session.captions.timer);
    session.captions = null;
    const failure = reply ? reply.error : `no answer from the page script within ${CAPTIONS_TIMEOUT_MS / 1000} s`;
    if (failure) {
      session.notes.captions = `captions: ${failure}`;
      console.warn("[dubber] no captions:", failure);
    }
    post(lib.captionsMessage(session.videoId, reply));
    render();
  }

  function onControl(msg) {
    const notes = session.notes;
    session.player.control(msg.action, msg.value).catch((error) => {
      if (error && error.name === "AbortError") return; // a pause came before the play began
      notes.play = `play failed: ${(error && error.message) || error}`;
      render();
    });
  }

  function onOrchestratorMessage(msg) {
    if (msg.type === "status") {
      status = msg;
      busy = false;
      render();
    } else if (msg.type === "subtitle") {
      session.subtitles.push(msg);
      render();
    } else if (msg.type === "duck") {
      session.player.duck(msg.volume);
    } else if (msg.type === "control") {
      onControl(msg);
    }
  }

  function onWorkerMessage(msg) {
    if (msg.cmd === "toggle") {
      if (session) stopDubbing({ play: true });
      else startDubbing();
    } else if (msg.cmd === "socket") {
      const was = connected;
      connected = Boolean(msg.connected);
      if (!connected) status = null;
      const what = lib.onSocket(was, connected, Boolean(session));
      if (what === "release") {
        // The orchestrator's session ended with the socket: give the player back now. The tab
        // stays in dubbing, and starts over when the socket is back.
        session.subtitles = [];
        giveBack(session.player, true);
      } else if (what === "restart") {
        restartDubbing();
      }
      render();
    } else if (msg.cmd === "busy") {
      busy = true;
      render();
    } else if (session) {
      onOrchestratorMessage(msg);
    }
  }

  // Leaving the video (another video, another page, a reload or closing the tab) ends dubbing.
  function onLeave() {
    stopDubbing({ play: false });
  }

  // The extension was reloaded or removed: this copy of the script can never reach it again.
  function retire() {
    stopDubbing({ play: true });
    document.removeEventListener("yt-navigate-start", onLeave);
    window.removeEventListener("pagehide", onLeave);
    window.removeEventListener("message", onPageMessage);
  }

  function connect() {
    if (!chrome.runtime || !chrome.runtime.id) {
      retire();
      return;
    }
    try {
      port = chrome.runtime.connect({ name: "dubber" });
    } catch (error) {
      retire();
      return;
    }
    port.onMessage.addListener(onWorkerMessage);
    port.onDisconnect.addListener(() => {
      void (chrome.runtime && chrome.runtime.lastError); // read, so Chrome does not log it as unchecked
      port = null;
      onWorkerMessage({ cmd: "socket", connected: false });
      // The worker was stopped (it sleeps when idle) or the extension was reloaded.
      setTimeout(connect, RECONNECT_PORT_MS);
    });
    post({ cmd: "socket" });
  }

  document.addEventListener("yt-navigate-start", onLeave);
  window.addEventListener("pagehide", onLeave);
  window.addEventListener("message", onPageMessage);
  connect();
})();
