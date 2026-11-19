// Helpers for YouTube's /api/timedtext caption requests.
//
// The player's own request URL carries the signed `pot` and `signature` parameters YouTube
// requires, so these helpers edit it as text: URLSearchParams would re-encode those values.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module !== null && typeof exports === "object" && module.exports === exports) {
    module.exports = api;
  }
  root.dubberLib = Object.assign(root.dubberLib || {}, api);
})(globalThis, function () {
  "use strict";

  // Matches parameter `name` with its leading ? or &; `more` is the & after it, if any.
  const paramPattern = (name) => new RegExp(`([?&])${name}=[^&]*(&?)`);

  function setParam(url, name, value) {
    const pattern = paramPattern(name);
    if (pattern.test(url)) return url.replace(pattern, (_, lead, more) => `${lead}${name}=${value}${more}`);
    return `${url}${url.includes("?") ? "&" : "?"}${name}=${value}`;
  }

  function dropParam(url, name) {
    return url.replace(paramPattern(name), (_, lead, more) => (more ? lead : ""));
  }

  /**
   * The timedtext `url` with `fmt` set to opts.fmt and `tlang` set to opts.tlang, or removed
   * when opts.tlang is null. A parameter the options leave out stays as it is.
   */
  function rewriteTimedtextUrl(url, opts = {}) {
    let rewritten = url;
    if (opts.fmt !== undefined) rewritten = setParam(rewritten, "fmt", opts.fmt);
    if (opts.tlang === null) rewritten = dropParam(rewritten, "tlang");
    else if (opts.tlang !== undefined) rewritten = setParam(rewritten, "tlang", opts.tlang);
    return rewritten;
  }

  /** The last of `resourceNames` that is an /api/timedtext request, or null. */
  function pickTimedtextUrl(resourceNames) {
    for (let i = resourceNames.length - 1; i >= 0; i--) {
      if (resourceNames[i].includes("/api/timedtext")) return resourceNames[i];
    }
    return null;
  }

  // "en-GB" -> "en"
  const baseLanguage = (code) => String(code || "").split("-")[0];

  const PREFERENCES = {
    "manual-tr": { language: "tr", asr: false },
    "manual-en": { language: "en", asr: false },
    "asr-en": { language: "en", asr: true },
  };

  /**
   * The first caption track of `tracks` (YouTube's captionTracks) that `want` describes, or
   * null: "manual-tr" and "manual-en" are human tracks, "asr-en" the English auto-captions.
   */
  function chooseTrack(tracks, want) {
    const preference = PREFERENCES[want];
    if (!preference) throw new Error(`unknown caption preference ${want}`);
    const found = tracks.find(
      (track) => baseLanguage(track.languageCode) === preference.language && (track.kind === "asr") === preference.asr
    );
    return found || null;
  }

  /** The decoded value of parameter `name` in `url`, read as text, or null. */
  function timedtextParam(url, name) {
    const found = url.match(new RegExp(`[?&]${name}=([^&]*)`));
    if (!found) return null;
    try {
      return decodeURIComponent(found[1]);
    } catch (error) {
      return found[1];
    }
  }

  /**
   * Whether the resource timing `entry` is an /api/timedtext request that started at or after
   * `since` (any time when it is left out) for video `videoId` and language `language`; a
   * request that does not name its video or language passes. With `asr` given, the request
   * must be for the auto-captions (true) or for a human track (false).
   */
  function isRequestFor(entry, { since = -Infinity, videoId, language, asr }) {
    if (entry.startTime < since || !entry.name.includes("/api/timedtext")) return false;
    const video = timedtextParam(entry.name, "v");
    const lang = timedtextParam(entry.name, "lang");
    if (video !== null && video !== videoId) return false;
    if (lang !== null && baseLanguage(lang) !== baseLanguage(language)) return false;
    return asr === undefined || (timedtextParam(entry.name, "kind") === "asr") === asr;
  }

  /**
   * Whether a finished request brought no track: it was cut off before an answer (status 0),
   * YouTube refused it (400 or more, whatever the body), or the answer was empty. YouTube
   * answers 200 with an empty body when the player asks before it has its token, in the first
   * seconds of a page. An entry without sizes is not known to be empty.
   */
  function cameBackEmpty(entry) {
    if (entry.responseStatus === 0 || entry.responseStatus >= 400) return true;
    return entry.encodedBodySize === 0 && entry.decodedBodySize === 0;
  }

  /**
   * The newest /api/timedtext request among the resource timing `entries` that `about`
   * describes (see isRequestFor) and that brought a track. Null when there is none.
   */
  function findTimedtextUrl(entries, about) {
    return pickTimedtextUrl(entries.filter((entry) => isRequestFor(entry, about) && !cameBackEmpty(entry)).map((entry) => entry.name));
  }

  /** Whether a request that `about` describes came back empty or was cut off. */
  function unusableTimedtext(entries, about) {
    return entries.some((entry) => isRequestFor(entry, about) && cameBackEmpty(entry));
  }

  /**
   * Makes the player load a track and waits for its request to finish; resolves with the
   * request's URL. `ask()` makes the player request the track, `entries()` gives the resource
   * timing entries, and `now()` and `wait(ms)` are the clock.
   *
   * A request that is slow is waited for: the first one for a track can take YouTube more than
   * 5 s, and asking again would only cut it off. A request that came back without a track is
   * asked for again after `retryPauseMs`, `maxAsks` times in all: a YouTube that refuses must
   * not be asked over and over. Rejects when the asks are spent or `budgetMs` have passed,
   * saying which it was.
   */
  async function awaitTimedtextUrl({ ask, entries, now, wait, videoId, language, budgetMs, pollMs = 100, retryPauseMs = 700, maxAsks = 4 }) {
    const deadline = now() + budgetMs;
    let asks = 1;
    let empties = 0;
    let since = now();
    ask();
    for (;;) {
      await wait(pollMs);
      const seen = entries();
      const url = findTimedtextUrl(seen, { since, videoId, language });
      if (url) return url;
      const unusable = unusableTimedtext(seen, { since, videoId, language });
      if (unusable) empties += 1;
      if ((unusable && asks >= maxAsks) || now() >= deadline) {
        throw new Error(
          empties
            ? `YouTube sent no caption track ${empties} times (empty, or refused)`
            : `YouTube did not answer the player's caption request within ${Math.round(budgetMs / 1000)} s`
        );
      }
      if (unusable) {
        await wait(retryPauseMs);
        asks += 1;
        since = now();
        ask();
      }
    }
  }

  /**
   * Gets a caption track: resolves with {url, json3}, the player's request and what
   * `download(url, timeoutMs)` made of it.
   *
   * `earlierUrl`, a request the player made before for this track, is downloaded first, with
   * `earlierMs` at most, and the player is then left alone. Otherwise the player is asked
   * (awaitTimedtextUrl) and its request downloaded. A download that fails is tried once more
   * after `retryPauseMs`: `tries` downloads at most, and `maxAsks` asks of the player in all,
   * so that a YouTube that refuses is not asked dozens of times. Everything together takes
   * `budgetMs` at most. Rejects with the last failure.
   */
  async function fetchTrack({
    earlierUrl = null, ask, entries, download, now, wait, videoId, language, budgetMs,
    pollMs = 100, tries = 2, maxAsks = 5, earlierMs = 4000, retryPauseMs = 1000,
  }) {
    const deadline = now() + budgetMs;
    const left = () => deadline - now();
    if (earlierUrl) {
      try {
        return { url: earlierUrl, json3: await download(earlierUrl, Math.min(earlierMs, left())) };
      } catch (error) {
        // Its URL may have expired, or YouTube is slow: ask the player.
      }
    }
    let failure = new Error("no time was left to fetch the captions");
    let asked = 0;
    const counted = () => {
      asked += 1;
      ask();
    };
    for (let attempt = 1; attempt <= tries && asked < maxAsks && left() > pollMs; attempt++) {
      if (attempt > 1) await wait(Math.min(retryPauseMs, left()));
      try {
        const url = await awaitTimedtextUrl({
          ask: counted, entries, now, wait, videoId, language, budgetMs: left(), pollMs, maxAsks: maxAsks - asked,
        });
        return { url, json3: await download(url, Math.max(left(), 1000)) };
      } catch (error) {
        failure = error;
      }
    }
    throw failure;
  }

  /**
   * The player's /api/timedtext requests, as a PerformanceObserver reports them when they end.
   * YouTube clears the browser's own list of requests now and then (2 s and 3 s after a page
   * loads, for one), so a request can be gone before that list is read; what an observer was
   * told stays here. It holds plain copies of the newest `limit` requests.
   */
  class TimedtextLog {
    constructor(limit = 100) {
      this._limit = limit;
      this._entries = [];
    }

    add(entries) {
      for (const entry of entries) {
        if (!entry.name.includes("/api/timedtext")) continue;
        const { name, startTime, responseStatus, encodedBodySize, decodedBodySize } = entry;
        this._entries.push({ name, startTime, responseStatus, encodedBodySize, decodedBodySize });
      }
      if (this._entries.length > this._limit) this._entries.splice(0, this._entries.length - this._limit);
    }

    all() {
      return this._entries.slice();
    }
  }

  /**
   * The captions fetched in this page, by video and kind of request, so that a second Start
   * on a video needs nothing from the player. It holds the newest `limit` of them.
   */
  class CaptionStore {
    constructor(limit = 3) {
      this._limit = limit;
      this._kept = new Map();
    }

    get(videoId, want) {
      return this._kept.get(`${videoId} ${want}`) || null;
    }

    put(videoId, want, captions) {
      const key = `${videoId} ${want}`;
      this._kept.delete(key);
      this._kept.set(key, captions);
      while (this._kept.size > this._limit) this._kept.delete(this._kept.keys().next().value);
    }
  }

  /**
   * The track to make the player load for `want`: chooseTrack's choice; for "en" the English
   * track to dub (the human subtitles, else the auto-captions); for "translated-tr" the English
   * track that YouTube translates (the auto-captions, else the human track).
   */
  function sourceTrack(tracks, want) {
    if (want === "translated-tr") return chooseTrack(tracks, "asr-en") || chooseTrack(tracks, "manual-en");
    // English to dub: human subtitles are punctuated and cut at sentences, so they come first.
    if (want === "en") return chooseTrack(tracks, "manual-en") || chooseTrack(tracks, "asr-en");
    return chooseTrack(tracks, want);
  }

  /**
   * The lang and kind of the captions fetched for `want` from the player's request `url`. The
   * request says which track the player really loaded, which may differ from `track`, the one
   * asked for.
   */
  function describeCaptions(url, want, track) {
    if (want === "translated-tr") return { lang: "tr", kind: "translated" };
    return {
      lang: timedtextParam(url, "lang") || track.languageCode,
      kind: timedtextParam(url, "kind") === "asr" ? "asr" : "manual",
    };
  }

  return {
    rewriteTimedtextUrl,
    pickTimedtextUrl,
    chooseTrack,
    timedtextParam,
    findTimedtextUrl,
    unusableTimedtext,
    awaitTimedtextUrl,
    fetchTrack,
    CaptionStore,
    TimedtextLog,
    sourceTrack,
    describeCaptions,
  };
});
