// The service worker's routing between YouTube tabs and the orchestrator's one socket.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module !== null && typeof exports === "object" && module.exports === exports) {
    module.exports = api;
  }
  root.dubberLib = Object.assign(root.dubberLib || {}, api);
})(globalThis, function () {
  "use strict";

  /**
   * One tab at a time dubs. The session belongs to the tab that sent start until that tab sends
   * stop or closes; meanwhile the other tabs' messages are dropped, and the orchestrator's
   * messages go to that tab alone. Afterwards the answers to its stop still go to it alone, but
   * status messages go to every tab, so that a tab told another tab was dubbing learns it ended.
   */
  class TabRouter {
    constructor() {
      this.owner = null; // the tab that sent the last start
      this.active = false; // whether that tab is dubbing
    }

    /**
     * What to do with `msg` from tab `tab`: "send" it to the orchestrator, or drop it and tell
     * the tab another tab is dubbing ("busy"), or just "drop" it (another tab's stop).
     */
    fromTab(tab, msg) {
      if (this.active && tab !== this.owner) return msg.type === "stop" ? "drop" : "busy";
      if (msg.type === "start") {
        this.owner = tab;
        this.active = true;
      } else if (msg.type === "stop") {
        this.active = false;
      }
      return "send";
    }

    /** The socket closed and the orchestrator's session went with it: no tab owns one now. */
    reset() {
      this.owner = null;
      this.active = false;
    }

    /** Tab `tab` went away; true when it was dubbing, so that its session must be stopped. */
    closed(tab) {
      if (tab !== this.owner) return false;
      const wasDubbing = this.active;
      this.owner = null;
      this.active = false;
      return wasDubbing;
    }

    /** The tabs, of the connected `tabs`, that an orchestrator message goes to. */
    recipients(msg, tabs) {
      if (this.active) return [this.owner];
      if (msg.type === "status") return [...tabs];
      return this.owner === null ? [] : [this.owner];
    }
  }

  /** Milliseconds to wait before reconnecting after `failures` failed attempts in a row. */
  function reconnectDelay(failures) {
    return Math.min(1000 * 2 ** failures, 8000);
  }

  return { TabRouter, reconnectDelay };
});
