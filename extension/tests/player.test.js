const test = require("node:test");
const assert = require("node:assert");
const { PlayerControl } = require("../lib/player.js");
const { VolumeDuck } = require("../lib/volume.js");

// A <video> element that records the calls made on it.
function fakeVideo(over = {}) {
  return {
    volume: 0.8,
    playbackRate: 1,
    paused: false,
    calls: [],
    pause() {
      this.paused = true;
      this.calls.push("pause");
    },
    play() {
      this.paused = false;
      this.calls.push("play");
      return Promise.resolve();
    },
    ...over,
  };
}

function controlOf(video) {
  return new PlayerControl(video, new VolumeDuck(() => video.volume, (level) => { video.volume = level; }));
}

test("duck messages duck the volume and null gives it back", () => {
  const video = fakeVideo();
  const player = controlOf(video);
  player.duck(0.2);
  assert.equal(video.volume, 0.2);
  player.duck(null);
  assert.equal(video.volume, 0.8);
});

test("a volume change while ducked is pushed back", () => {
  const video = fakeVideo();
  const player = controlOf(video);
  player.volumeChanged(); // nothing is ducked yet: the owner's change stands
  player.duck(0.2);
  video.volume = 1; // the owner drags YouTube's slider
  player.volumeChanged();
  assert.equal(video.volume, 0.2);
});

test("control messages pause, play and set the rate", async () => {
  const video = fakeVideo();
  const player = controlOf(video);
  await player.control("pause", null);
  assert.equal(video.paused, true);
  await player.control("play", null);
  assert.equal(video.paused, false);
  await player.control("rate", 0.9);
  assert.equal(video.playbackRate, 0.9);
  await player.control("rate", null);
  assert.equal(video.playbackRate, 0.9);
  assert.deepEqual(video.calls, ["pause", "play"]);
});

test("a refused play is passed on, so that it can be shown", async () => {
  const blocked = Object.assign(new Error("play() failed because the user didn't interact"), { name: "NotAllowedError" });
  const player = controlOf(fakeVideo({ play: () => Promise.reject(blocked) }));
  await assert.rejects(player.control("play", null), /didn't interact/);
});

test("restore gives back the volume, the start rate and plays a player the orchestrator holds", async () => {
  const video = fakeVideo({ playbackRate: 1.5 });
  const player = controlOf(video);
  player.duck(0.2);
  await player.control("pause", null);
  await player.control("rate", 1.25);
  await player.restore({ play: true });
  assert.deepEqual([video.volume, video.playbackRate, video.paused], [0.8, 1.5, false]);
  assert.deepEqual(video.calls, ["pause", "play"]);
});

test("restore leaves alone a rate the orchestrator never set", async () => {
  const video = fakeVideo();
  const player = controlOf(video);
  video.playbackRate = 1.1; // the owner's choice while dubbing
  await player.restore({ play: true });
  assert.equal(video.playbackRate, 1.1);
});

test("restore does not play a player the owner paused", async () => {
  const video = fakeVideo();
  const player = controlOf(video);
  await player.control("pause", null);
  await player.control("play", null);
  video.pause(); // the owner pauses
  await player.restore({ play: true });
  assert.equal(video.paused, true);
  assert.deepEqual(video.calls, ["pause", "play", "pause"]);
});

test("restore without play leaves a held player paused", async () => {
  // Leaving the page or the video: the next video plays by itself, a hidden page should not.
  const video = fakeVideo();
  const player = controlOf(video);
  player.duck(0.2);
  await player.control("pause", null);
  await player.restore({ play: false });
  assert.deepEqual([video.volume, video.paused], [0.8, true]);
});

test("a second restore changes nothing", async () => {
  const video = fakeVideo({ playbackRate: 1.5 });
  const player = controlOf(video);
  player.duck(0.2);
  await player.control("pause", null);
  await player.control("rate", 1.25);
  await player.restore({ play: true });
  video.volume = 0.5; // the owner, after dubbing ended
  video.playbackRate = 2;
  video.pause();
  await player.restore({ play: true });
  assert.deepEqual([video.volume, video.playbackRate, video.paused], [0.5, 2, true]);
});
