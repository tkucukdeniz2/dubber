// Ducking the video's volume while it is dubbed, and giving the exact volume back afterwards.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module !== null && typeof exports === "object" && module.exports === exports) {
    module.exports = api;
  }
  root.dubberLib = Object.assign(root.dubberLib || {}, api);
})(globalThis, function () {
  "use strict";

  /**
   * Holds a volume at a ducked level. The volume found by the first duck() is kept and set again
   * by restore(), however often the level was re-applied in between.
   */
  class VolumeDuck {
    constructor(getVolume, setVolume) {
      this._getVolume = getVolume;
      this._setVolume = setVolume;
      this._original = null; // the volume before the duck
      this._level = null; // the ducked level, null when not ducked
    }

    get isDucked() {
      return this._level !== null;
    }

    /** Set the volume to `level`, remembering the volume of the first duck. */
    duck(level) {
      if (!this.isDucked) this._original = this._getVolume();
      this._level = level;
      this._setVolume(level);
    }

    /** While ducked, set the level again if something else changed the volume to `current`. */
    reapply(current) {
      if (this.isDucked && current !== this._level) this._setVolume(this._level);
    }

    /** Give back the volume from before the duck; nothing happens when not ducked. */
    restore() {
      if (!this.isDucked) return;
      const original = this._original;
      this._original = null;
      this._level = null;
      this._setVolume(original);
    }
  }

  return { VolumeDuck };
});
