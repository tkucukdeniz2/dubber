// Dubber service worker. It owns the one WebSocket to the orchestrator (`uv run dubber serve`)
// and relays messages between it and the YouTube tabs. The content script of each tab holds a
// long-lived Port to this worker.
importScripts("lib/protocol.js", "lib/router.js");

const { hello, stop, TabRouter, reconnectDelay } = globalThis.dubberLib;

const SERVER_URL = "ws://127.0.0.1:8765";
// Chrome ends a worker after 30 s without events; socket traffic counts, and the orchestrator
// answers each hello with a status.
const HELLO_EVERY_MS = 20000;
const VERSION = chrome.runtime.getManifest().version;

const ports = new Map(); // tab id -> the Port of that tab's content script
const router = new TabRouter();
let socket = null; // the WebSocket while it connects or is open
let connected = false; // whether the socket is open
let failures = 0; // connection attempts that failed in a row
let retryTimer = null;
let helloTimer = null;

function post(port, msg) {
  try {
    port.postMessage(msg);
  } catch (error) {
    // The tab is going away; its onDisconnect cleans up.
  }
}

function setConnected(value) {
  if (connected === value) return;
  connected = value;
  for (const port of ports.values()) post(port, { cmd: "socket", connected });
}

function send(msg) {
  if (connected) socket.send(JSON.stringify(msg));
}

// Opens the socket, unless it is open or connecting, a reconnect is pending, or no tab needs it.
function openSocket() {
  if (socket || retryTimer || ports.size === 0) return;
  const ws = new WebSocket(SERVER_URL);
  socket = ws;
  ws.onopen = () => {
    failures = 0;
    setConnected(true);
    send(hello(VERSION));
    helloTimer = setInterval(() => send(hello(VERSION)), HELLO_EVERY_MS);
  };
  ws.onmessage = (event) => deliver(event.data);
  ws.onclose = () => {
    if (socket !== ws) return; // closed on purpose by closeSocket
    socket = null;
    clearInterval(helloTimer);
    helloTimer = null;
    // The orchestrator's session ended with the socket. A tab that was dubbing starts over
    // when the socket is back, so no tab is kept as the owner meanwhile.
    router.reset();
    setConnected(false);
    if (ports.size === 0) return;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      openSocket();
    }, reconnectDelay(failures++));
  };
}

// Closes the socket once no YouTube tab is left, so that the worker can sleep.
function closeSocket() {
  clearTimeout(retryTimer);
  retryTimer = null;
  clearInterval(helloTimer);
  helloTimer = null;
  failures = 0;
  const ws = socket;
  socket = null;
  router.reset();
  setConnected(false);
  if (ws) ws.close();
}

// Passes a message of the orchestrator to the tab or tabs it is for.
function deliver(text) {
  let msg;
  try {
    msg = JSON.parse(text);
  } catch (error) {
    return;
  }
  if (!msg || typeof msg.type !== "string") return;
  for (const tab of router.recipients(msg, ports.keys())) {
    const port = ports.get(tab);
    if (port) post(port, msg);
  }
}

function fromTab(tab, port, msg) {
  if (!msg) return;
  if (msg.cmd === "socket") {
    post(port, { cmd: "socket", connected });
    return;
  }
  if (typeof msg.type !== "string") return;
  // Without the socket nothing can be sent, and a start must not make the tab the owner of a
  // session the orchestrator never heard of: the tab starts over when the socket is up.
  if (!connected) return;
  const verdict = router.fromTab(tab, msg);
  if (verdict === "busy") post(port, { cmd: "busy" });
  else if (verdict === "send") send(msg);
}

chrome.runtime.onConnect.addListener((port) => {
  const tab = port.sender && port.sender.tab ? port.sender.tab.id : undefined;
  if (port.name !== "dubber" || tab === undefined) {
    port.disconnect();
    return;
  }
  ports.set(tab, port);
  port.onMessage.addListener((msg) => fromTab(tab, port, msg));
  port.onDisconnect.addListener(() => {
    // The tab was closed or reloaded, or left YouTube.
    if (ports.get(tab) === port) ports.delete(tab);
    if (router.closed(tab)) send(stop());
    if (ports.size === 0) closeSocket();
  });
  openSocket();
});

function toggle(tab) {
  const port = tab ? ports.get(tab.id) : undefined;
  if (port) post(port, { cmd: "toggle" });
  else console.info("Dubber: this tab has no YouTube page with Dubber loaded; reload the tab if it is one.");
}

chrome.action.onClicked.addListener(toggle);
chrome.commands.onCommand.addListener((command, tab) => {
  if (command === "toggle-dub") toggle(tab);
});
