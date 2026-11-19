// extension/tests/volume.test.js
const test = require("node:test"); const assert = require("node:assert");
const { VolumeDuck } = require("../lib/volume.js");

test("duck, reapply and restore", () => {
  let v = 0.8; const d = new VolumeDuck(() => v, (x) => { v = x; });
  d.duck(0.2); assert.equal(v, 0.2); assert.equal(d.isDucked, true);
  v = 1.0; d.reapply(v); assert.equal(v, 0.2);
  d.restore(); assert.equal(v, 0.8); assert.equal(d.isDucked, false);
});

// Beyond the brief: the bookkeeping the content script relies on.

// A volume control that records every value set.
function control(start) {
  const state = { volume: start, sets: [] };
  const duck = new VolumeDuck(() => state.volume, (x) => { state.volume = x; state.sets.push(x); });
  return { state, duck };
}

test("a second duck keeps the first original volume", () => {
  const { state, duck } = control(0.7);
  duck.duck(0.2);
  duck.duck(0.15);
  assert.equal(state.volume, 0.15);
  duck.restore();
  assert.equal(state.volume, 0.7);
});

test("reapply sets nothing when the volume is the ducked level or nothing is ducked", () => {
  const { state, duck } = control(0.7);
  duck.reapply(0.5);
  assert.deepEqual(state.sets, []);
  duck.duck(0.2);
  duck.reapply(0.2);
  assert.deepEqual(state.sets, [0.2]);
});

test("restore sets nothing when nothing is ducked", () => {
  const { state, duck } = control(0.7);
  duck.restore();
  duck.duck(0.2);
  duck.restore();
  duck.restore();
  assert.deepEqual(state.sets, [0.2, 0.7]);
  assert.equal(duck.isDucked, false);
});

test("a duck after a restore remembers the volume of that moment", () => {
  const { state, duck } = control(0.7);
  duck.duck(0.2);
  duck.restore();
  state.volume = 0.4; // the owner changed it while nothing was ducked
  duck.duck(0.2);
  duck.restore();
  assert.equal(state.volume, 0.4);
});
