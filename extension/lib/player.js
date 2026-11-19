// Applies the orchestrator's duck and control messages to the <video> element, and gives the
// player back when dubbing ends.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module !== null && typeof exports === "object" && module.exports === exports) {
    module.exports = api;
  }
  root.dubberLib = Object.assign(root.dubberLib || {}, api);
})(globalThis, function () {
  "use strict";

  /**
   * One dubbing run on `video`. `volume` is a VolumeDuck bound to the video's volume. The rate
   * when dubbing starts is remembered here; the volume before the duck, by the VolumeDuck.
   */
  class PlayerControl {
    constructor(video, volume) {
      this._video = video;
      this._volume = volume;
      this._startRate = video.playbackRate;
      this._rateChanged = false; // a control message set the rate
      this._held = false; // the orchestrator paused the player and has not played it since
    }

    /** A duck message: duck the volume to `level`, or give it back for null. */
    duck(level) {
      if (level === null) this._volume.restore();
      else this._volume.duck(level);
    }

    /** The video's volume changed: while ducked, set the level again. */
    volumeChanged() {
      this._volume.reapply(this._video.volume);
    }

    /** A control message. Returns a promise, rejected when the browser refuses to play. */
    control(action, value) {
      const video = this._video;
      if (action === "pause") {
        this._held = true;
        video.pause();
      } else if (action === "play") {
        this._held = false;
        return video.play();
      } else if (action === "rate" && Number.isFinite(value) && value > 0) {
        this._rateChanged = true;
        video.playbackRate = value;
      }
      return Promise.resolve();
    }

    /**
     * Give the player back: the volume from before the duck and, if a control message changed
     * it, the rate from when dubbing started. With `play`, a player the orchestrator holds
     * paused plays again. Returns the promise of that play, if any.
     */
    restore({ play }) {
      this._volume.restore();
      if (this._rateChanged) {
        this._rateChanged = false;
        this._video.playbackRate = this._startRate;
      }
      const resume = play && this._held && this._video.paused;
      this._held = false;
      return resume ? this._video.play() : Promise.resolve();
    }
  }

  return { PlayerControl };
});
