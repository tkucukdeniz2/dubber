# Dubber extension

The Chrome side of `dubber`. It reads the YouTube player's state and the video's English
captions, sends them to the orchestrator (`uv run dubber serve`) on this Mac, lowers the video's
volume while the dub plays, and shows the orchestrator's status and subtitles on the player.

## Load it

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose this `extension` folder.
4. Optional: click the puzzle-piece icon in the toolbar and pin **Dubber**, so its button stays visible.
5. Reload every YouTube tab that was already open. Chrome adds the extension only to pages
   loaded after it, and the same goes after each **Reload** of the extension on `chrome://extensions`.

## Use it

1. Start the orchestrator first, in the repository folder:

   ```
   uv run dubber serve
   ```

   It prints `listening on ws://127.0.0.1:8765` and logs each message it gets.
2. Open a YouTube video (a `youtube.com/watch?v=...` page).
3. Click the Dubber button in the toolbar, or press **Option+Shift+D**. You can change the
   shortcut at `chrome://extensions/shortcuts`.

What happens then:

1. The player pauses, and the video's volume drops to 0.2. While dubbing is on, the volume stays
   there: if you drag YouTube's volume slider, it is pushed back.
2. The extension makes YouTube's player load the video's English auto-generated captions and
   sends them to the orchestrator. YouTube's captions may flash on for a moment; afterwards they
   are on or off just as before.
3. Once enough of the dub is ready, the orchestrator plays the video again and the dub follows
   it. It may pause the video again when it falls behind, and it holds the speed at 1.25x at
   most while dubbing.

A small bar sits at the top of the player. It never takes a click, so the player's controls work
as usual. Its first line shows:

- the mode, the orchestrator's state (`preparing`, `holding`, `playing`, `paused` or `idle`),
  the engine and how many seconds are ready ahead, then the orchestrator's message, if any;
- `the server is not running (uv run dubber serve): dubbing starts when it is` when the
  orchestrator cannot be reached;
- `another tab is dubbing` when another YouTube tab has dubbing on; only one tab dubs at a time;
- at the end, the extension's own notes: `captions: ...` says why the captions could not be
  fetched (the orchestrator then says `no usable captions` and lets the video play on; YouTube
  is given up to 16 seconds, and a track that was fetched is kept for the next start), and
  `play failed: ...` says Chrome refused to play the video; press play yourself.

The second line shows the subtitle of what is being said now.

## Stop it

Click the button again, or press the shortcut. The bar disappears, the volume goes back to
exactly what it was before, the speed goes back to what it was when you started (if the
orchestrator changed it), and a video the orchestrator had paused plays again.

Dubbing also stops by itself when you go to another video, leave YouTube, reload the tab or close
it. The volume and speed come back then too, and the video is left as YouTube plays it.

## When the orchestrator is not running

The bar says that the server is not running. You can press the button first and start
`uv run dubber serve` afterwards: the tab stays in dubbing, and dubbing starts by itself when
the server is up. If the server goes away while dubbing, the video gets its volume and speed
back at once, and plays again if the orchestrator had paused it. The extension keeps trying to
connect: after 1 second, then 2, 4 and 8, and every 8 seconds after that. Once the server is
running again, the tab starts over within about 10 seconds, from where the video is then,
with no need to reload the page or to press the button. Press the button to leave dubbing
instead.

While the orchestrator is down, Chrome may list "WebSocket connection ... failed" under the
extension's **Errors** button. They are the retries and do no harm.

## Local Network Access

Chrome's Local Network Access rules (since Chrome 142) make a web page ask for permission before
it connects to this computer (`localhost`, `127.0.0.1`) or to the local network. Since Chrome 145
that permission is split in two; the one for this computer is about reaching apps and services
on this device. WebSocket connections fall under the same rules.

Dubber does not need that permission, and Chrome should not ask for it:

- The connection to `ws://127.0.0.1:8765` is opened by the extension's service worker, not by
  the YouTube page.
- Chrome counts the extension's own pages and its service worker as belonging to this computer
  (the "loopback" address space): see `DetermineAddressSpaceFromURL` in Chromium's
  `chrome_content_browser_client.cc`, and the fixes for extension service workers in Chrome 140
  (crbug.com/435246545) and Chrome 144 (Chromium change 7106704).
- Local Network Access only checks a connection that goes from a more public address space to a
  more private one. A connection from this computer to this computer is not checked.

So the manifest asks for no extra host permission. One would change nothing here: the exemption
comes from the extension's own origin, and Chrome's match patterns cannot name `ws://` addresses
anyway (they take `http`, `https` and `file`).

The socket must stay in the service worker. Opened from the content script, it would come from
the YouTube page, so Chrome would ask whether youtube.com may reach apps on this device, and
the orchestrator would refuse it, because it accepts only the extension's origin.

If Chrome ever does show a permission prompt when you click the button, allow it, and write down
here the Chrome version and the prompt's words.

## Tests

The helpers in `lib/` have tests that run with Node 22:

```
node --test extension/tests/
```
