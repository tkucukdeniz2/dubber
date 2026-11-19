const test = require("node:test");
const assert = require("node:assert");
const { TabRouter, reconnectDelay } = require("../lib/router.js");

const START = { type: "start", mode: null };
const STOP = { type: "stop" };
const STATE = { type: "state" };
const A = 1;
const B = 2;

test("the tab that starts owns the session; other tabs are told it is busy", () => {
  const router = new TabRouter();
  assert.equal(router.fromTab(A, START), "send");
  assert.equal(router.fromTab(A, STATE), "send");
  assert.equal(router.fromTab(B, START), "busy");
  assert.equal(router.fromTab(B, STATE), "busy");
  assert.equal(router.fromTab(B, { type: "captions" }), "busy");
});

test("another tab's stop never ends the owner's session", () => {
  const router = new TabRouter();
  router.fromTab(A, START);
  assert.equal(router.fromTab(B, STOP), "drop");
  assert.equal(router.fromTab(B, START), "busy");
});

test("after the owner stops, another tab may start", () => {
  const router = new TabRouter();
  router.fromTab(A, START);
  assert.equal(router.fromTab(A, STOP), "send");
  assert.equal(router.fromTab(B, START), "send");
  assert.equal(router.fromTab(A, START), "busy");
});

test("messages pass while nobody dubs", () => {
  const router = new TabRouter();
  assert.equal(router.fromTab(B, STATE), "send");
  assert.equal(router.fromTab(B, STOP), "send");
});

test("closing the owner's tab while it dubs asks for a stop", () => {
  const router = new TabRouter();
  router.fromTab(A, START);
  assert.equal(router.closed(B), false);
  assert.equal(router.closed(A), true);
  assert.equal(router.fromTab(B, START), "send");
});

test("closing a tab that is not dubbing asks for nothing", () => {
  const router = new TabRouter();
  router.fromTab(A, START);
  router.fromTab(A, STOP);
  assert.equal(router.closed(A), false);
});

test("orchestrator messages go to the session's tab, also the answers to its stop", () => {
  const router = new TabRouter();
  router.fromTab(A, START);
  assert.deepEqual(router.recipients({ type: "duck" }, [A, B]), [A]);
  router.fromTab(A, STOP);
  assert.deepEqual(router.recipients({ type: "control" }, [A, B]), [A]);
});

test("once the owner stops, status messages go to every tab", () => {
  // A tab still showing "another tab is dubbing" learns that the session ended.
  const router = new TabRouter();
  router.fromTab(A, START);
  assert.deepEqual(router.recipients({ type: "status" }, [A, B]), [A]);
  router.fromTab(A, STOP);
  assert.deepEqual(router.recipients({ type: "status" }, [A, B]), [A, B]);
});

test("without a session tab only status messages go out, to every tab", () => {
  const router = new TabRouter();
  assert.deepEqual(router.recipients({ type: "status" }, [A, B]), [A, B]);
  assert.deepEqual(router.recipients({ type: "duck" }, [A, B]), []);
  router.fromTab(A, START);
  router.closed(A);
  assert.deepEqual(router.recipients({ type: "control" }, [B]), []);
});

test("reconnect backoff is 1, 2, 4 and then 8 seconds", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 10].map(reconnectDelay), [1000, 2000, 4000, 8000, 8000, 8000]);
});

// The socket closed: the orchestrator's session went with it, so no tab owns one any more.
test("a reset forgets the owner, and the next tab to start owns the session", () => {
  const router = new TabRouter();
  router.fromTab(A, START);
  router.reset();
  assert.equal(router.owner, null);
  assert.equal(router.active, false);
  assert.equal(router.fromTab(B, START), "send");
  assert.equal(router.fromTab(A, START), "busy");
  assert.deepEqual(router.recipients({ type: "subtitle" }, [A, B]), [B]);
});

test("after a reset a status goes to every tab and nothing else goes anywhere", () => {
  const router = new TabRouter();
  router.fromTab(A, START);
  router.reset();
  assert.deepEqual(router.recipients({ type: "status" }, [A, B]), [A, B]);
  assert.deepEqual(router.recipients({ type: "subtitle" }, [A, B]), []);
});
