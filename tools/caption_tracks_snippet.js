// Caption-track snippet for the Phase 0 bake-off. tools/bakeoff_captions.py records its output.
//
// Use: open a youtube.com/watch page in the Chrome profile you are logged in with, open the
// DevTools console (Cmd+Opt+J), paste this whole file and press Enter. If Chrome refuses the
// paste, type "allow pasting", press Enter, and paste again. After a few seconds two files
// download (Chrome may ask once to allow multiple downloads; allow it):
//
//   <videoId>.en.<asr|manual>.json3          the English captions, auto-generated or human
//   <videoId>.tr.<translated|manual>.json3   YouTube's auto-translation into Turkish, or the
//                                            video's own human Turkish track when it has one
//
// Move them into bakeoff/captions/. The console then prints the video's caption tracks as one
// line of JSON, and the command that records them:
//
//   uv run python -m tools.bakeoff_captions --video=<videoId> --tracks '<that line>'
//
// (--video= has an equals sign because a video id can start with "-", and with a space the
// command line would read that id as an option.)
//
// The player is left as it was found: captions off again, or the old track selected again.
//
// How it works (verified in Chrome 156 on 2026-11-01): YouTube rate-limits and bot-checks
// requests made from outside the page, and a URL built from ytInitialPlayerResponse alone comes
// back with an empty body. The player's own /api/timedtext request carries the `pot` and
// `signature` parameters the server requires, so this makes the player send that request through
// its API and re-uses its URL, changing only fmt and tlang. The URL is edited as text, because
// URLSearchParams would re-encode the signed parameters.

(async () => {
  const TAG = '[dubber]';
  const STOP = Symbol('stop');
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const TRACK_MS = 1500; // the wait after selecting a track, as verified
  const REQUEST_MS = 2500; // the wait after the last option is set, before the timeline is read
  const log = (...parts) => console.log(TAG, ...parts);
  const language = (code) => String(code).split('-')[0];

  // Query parameters are read and edited as plain text.
  const param = (url, name) => {
    const found = url.match(new RegExp(`[?&]${name}=([^&]*)`));
    return found ? decodeURIComponent(found[1]) : null;
  };
  const setParam = (url, name, value) =>
    new RegExp(`[?&]${name}=`).test(url)
      ? url.replace(new RegExp(`([?&])${name}=[^&]*`), (_, lead) => `${lead}${name}=${value}`)
      : `${url}${url.includes('?') ? '&' : '?'}${name}=${value}`;
  const dropParam = (url, name) =>
    url.replace(new RegExp(`([?&])${name}=[^&]*(&?)`), (_, lead, more) => (more ? lead : ''));
  const asJson3 = (url) => setParam(url, 'fmt', 'json3');

  // One step of the run. If it fails, the message names the step and says why, and the run
  // stops. An optional step only prints the message, and the run goes on.
  const step = async (name, work, { optional = false } = {}) => {
    try {
      return await work();
    } catch (error) {
      console.error(TAG, `${name} failed: ${error && error.message ? error.message : error}`);
      if (!optional) throw STOP;
      return undefined;
    }
  };

  let player = null;
  let videoId = '';
  let tracks = []; // {lang, kind} of every caption track the player lists
  let english = null; // the English track we ask for: the human one if there is one
  let humanTurkish = null;
  let base = ''; // the player's own timedtext URL, which both files are made from
  let loaded = false; // this run loaded the captions module, so it has to put things back
  let original = null; // the caption track that was showing before, or null if captions were off
  let translated = false;
  const saved = [];

  // The player's newest /api/timedtext request, checked to be for this video and language.
  const newestRequest = (code) => {
    const urls = performance
      .getEntriesByType('resource')
      .map((entry) => entry.name)
      .filter((name) => name.includes('/api/timedtext'));
    if (!urls.length) {
      throw new Error(
        'the player has made no /api/timedtext request (an ad may be playing; reload the page and run again)'
      );
    }
    const url = urls[urls.length - 1];
    const forVideo = param(url, 'v');
    if (forVideo !== null && forVideo !== videoId) {
      throw new Error(
        `the newest /api/timedtext request is for video ${forVideo}, not ${videoId} (reload the page and run again)`
      );
    }
    const forLanguage = param(url, 'lang');
    if (forLanguage !== null && language(forLanguage) !== language(code)) {
      throw new Error(`the newest /api/timedtext request is for language "${forLanguage}", not "${code}"`);
    }
    return url;
  };

  const save = async (name, url) => {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const text = await response.text();
    if (!text.trim()) {
      throw new Error("YouTube sent an empty reply (it wants the player's own pot and signature)");
    }
    let data = null;
    try {
      data = JSON.parse(text);
    } catch (error) {
      throw new Error('the reply is not JSON');
    }
    if (!data || !Array.isArray(data.events) || !data.events.length) {
      throw new Error('the reply holds no caption events');
    }
    const blobUrl = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
    const link = document.createElement('a');
    link.href = blobUrl;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 60000);
    saved.push(name);
    log(`downloaded ${name} (${data.events.length} events)`);
  };

  const findPlayer = () => {
    player = document.getElementById('movie_player');
    if (!player || typeof player.getPlayerResponse !== 'function') {
      throw new Error('there is no YouTube player here; open a youtube.com/watch page');
    }
    const found = location.search.match(/[?&]v=([\w-]{11})(?:&|$)/);
    if (!found) throw new Error('there is no video id in the address bar; open a youtube.com/watch page');
    videoId = found[1];
  };

  const listTracks = () => {
    const response = player.getPlayerResponse() || {};
    const listed = ((response.captions || {}).playerCaptionsTracklistRenderer || {}).captionTracks || [];
    tracks = listed.map((track) => ({
      lang: track.languageCode,
      kind: track.kind === 'asr' ? 'asr' : 'manual',
    }));
    const find = (code, kind) =>
      tracks.find((track) => language(track.lang) === code && (!kind || track.kind === kind));
    english = find('en', 'manual') || find('en');
    humanTurkish = find('tr', 'manual') || null;
  };

  // Make the player ask for the captions, and keep its request.
  const requestCaptions = async () => {
    let current = null;
    try {
      current = player.getOption('captions', 'track');
    } catch (error) {
      current = null; // an error counts as captions being off
    }
    const wasOn = !!(current || {}).languageCode;
    original = wasOn ? current : null;
    loaded = true;
    player.loadModule('captions');
    player.setOption('captions', 'track', { languageCode: english.lang });
    await wait(TRACK_MS);
    if (!humanTurkish) player.setOption('captions', 'translationLanguage', { languageCode: 'tr' });
    await wait(REQUEST_MS);
    base = newestRequest(english.lang);
  };

  const saveEnglish = async () => {
    // The URL says which track the player really loaded; when it is silent, it is the one we asked for.
    const kind = param(base, 'kind') === 'asr' ? 'asr' : english.kind;
    if (kind !== english.kind) log('the player loaded the auto-generated English track, not the human one');
    await save(`${videoId}.en.${kind}.json3`, asJson3(dropParam(base, 'tlang')));
  };

  const saveTurkish = async () => {
    if (humanTurkish) {
      // The human track beats the translation: select it and take the request it makes.
      player.setOption('captions', 'track', { languageCode: humanTurkish.lang });
      await wait(TRACK_MS + REQUEST_MS);
      const url = newestRequest(humanTurkish.lang);
      if (param(url, 'kind') === 'asr') {
        throw new Error('the player loaded an auto-generated Turkish track, not the human one');
      }
      await save(`${videoId}.tr.manual.json3`, asJson3(dropParam(url, 'tlang')));
    } else {
      await save(`${videoId}.tr.translated.json3`, asJson3(setParam(base, 'tlang', 'tr')));
      translated = true;
    }
  };

  const report = () => {
    const listed = JSON.stringify(translated ? tracks.concat({ lang: 'tr', kind: 'translated' }) : tracks);
    if (saved.length) log('move the downloaded files into bakeoff/captions/');
    log(`the caption tracks of ${videoId}, for the recorder:`);
    console.log(listed);
    // --video= with an equals sign: an id can start with "-", which argparse would take for an option.
    log(`uv run python -m tools.bakeoff_captions --video=${videoId} --tracks '${listed}'`);
  };

  const putBack = () => {
    if (!loaded) return;
    try {
      if (original) player.setOption('captions', 'track', original);
      else player.unloadModule('captions');
    } catch (error) {
      console.warn(TAG, 'could not put the captions back as they were:', error);
    }
  };

  try {
    // Chrome keeps 250 resource timing entries by default and drops the rest, so on a page
    // that has been playing for a while the player's /api/timedtext request would be missing.
    performance.setResourceTimingBufferSize(5000);
    await step('find the player and the video', findPlayer);
    await step('list the caption tracks', listTracks);
    if (!english) {
      log('this video has no English caption track, so there is nothing to download');
    } else {
      await step('make the player request the captions', requestCaptions);
      await step('download the English captions', saveEnglish);
      await step('download the Turkish captions', saveTurkish, { optional: true });
    }
    report();
  } catch (error) {
    if (error !== STOP) console.error(TAG, 'unexpected error:', error);
  } finally {
    putBack();
  }
})();
