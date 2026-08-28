// kit/arena_ui/tests/layout.test.js
//
// COLOSSEUM — the one layout invariant that a screenshot caught and no test
// did: the prosecution cut-in must never draw over the five-stage strip.
//
// The strip is the whole point of the projector — QUESTION > ACTION > ANSWER >
// EVAL > REFEREE — and the cut-in announces a referee decision. Drawing the
// announcement on top of the decision is the worst possible collision, and it
// is invisible to every non-visual test: the canvas draws both, without error,
// in the wrong order.
//
// Run with `node kit/arena_ui/tests/layout.test.js`.

import { cutInRect } from '../core/widgets.js';

let failures = 0;
let passed = 0;

function assert(cond, msg) {
  if (!cond) throw new Error(msg || 'assertion failed');
}

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`PASS  ${name}`);
  } catch (err) {
    failures += 1;
    console.log(`FAIL  ${name}\n      ${err.message}`);
  }
}

// A featured pane laid out the way projector.src.html lays it out.
function pane({ h = 700, logH = 140, stripH = 76, pad = 12 } = {}) {
  const rect = { x: 320, y: 0, w: 1100, h };
  const stripTop = rect.y + h - pad - stripH;
  const logTop = stripTop - 12 - logH;
  return { rect, logTop, stripTop };
}

test('the cut-in ends above the five-stage strip', () => {
  const { rect, logTop, stripTop } = pane();
  const c = cutInRect(rect, logTop, stripTop);
  assert(c.y + c.h <= stripTop,
    `cut-in bottom ${c.y + c.h} overlaps the strip at ${stripTop}`);
});

test('it also stays above the combat log', () => {
  const { rect, logTop, stripTop } = pane();
  const c = cutInRect(rect, logTop, stripTop);
  assert(c.y + c.h <= logTop, `cut-in bottom ${c.y + c.h} overlaps the log at ${logTop}`);
});

test('a short pane still cannot push it onto the strip', () => {
  // The regression the naive `Math.max(...)` clamp would reintroduce: when
  // there is no room above the log, a fixed-height cut-in gets clamped to the
  // top of the pane and its BOTTOM slides back down over the strip.
  for (const h of [220, 260, 300, 360, 420, 540, 700, 1200]) {
    const { rect, logTop, stripTop } = pane({ h });
    const c = cutInRect(rect, logTop, stripTop);
    assert(c.y + c.h <= stripTop,
      `h=${h}: cut-in bottom ${c.y + c.h} overlaps the strip at ${stripTop}`);
    assert(c.h >= 24, `h=${h}: cut-in collapsed to ${c.h}px`);
    assert(c.y >= rect.y, `h=${h}: cut-in escaped the top of the pane`);
  }
});

test('it stays inside the pane horizontally', () => {
  const { rect, logTop, stripTop } = pane();
  const c = cutInRect(rect, logTop, stripTop);
  assert(c.x >= rect.x && c.x + c.w <= rect.x + rect.w, 'cut-in escaped the pane sideways');
});

test('garbage geometry does not produce a NaN rect', () => {
  const c = cutInRect({ x: 0, y: 0, w: 100, h: 100 }, undefined, null);
  for (const k of ['x', 'y', 'w', 'h']) {
    assert(Number.isFinite(c[k]), `${k} is ${c[k]}`);
  }
});

console.log(`\n${passed} passed, ${failures} failed`);
if (failures) process.exitCode = 1;
