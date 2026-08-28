// kit/arena_ui/tests/reduce.test.js
//
// COLOSSEUM — plain-script tests for core/decode.js + core/reduce.js.
// No test framework (vanilla JS, no npm, no build step — see
// core/theme.js's header): a tiny local assert helper, a list of test
// functions, run top to bottom, PASS/FAIL printed per test, and a nonzero
// `process.exitCode` if anything failed. Run directly with
// `node kit/arena_ui/tests/reduce.test.js`, or via
// `kit/arena_ui/tests/run_js_tests.py`.

import { parseLine, normalizeEvent, resolveRef } from '../core/decode.js';
import { reduce, createInitialState, drainChoreography } from '../core/reduce.js';

// ---------------------------------------------------------------------------
// tiny assert helper
// ---------------------------------------------------------------------------

let failures = 0;
let passed = 0;

function assert(cond, msg) {
  if (!cond) throw new Error(msg || 'assertion failed');
}

function assertEqual(actual, expected, msg) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    throw new Error(`${msg || 'values differ'}\n  actual:   ${a}\n  expected: ${e}`);
  }
}

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  ok   - ${name}`);
  } catch (err) {
    failures += 1;
    console.log(`  FAIL - ${name}`);
    console.log(`         ${err && err.stack ? err.stack.split('\n').join('\n         ') : err}`);
  }
}

// ---------------------------------------------------------------------------
// fixture builder — a valid, fully-populated envelope with overrides
// ---------------------------------------------------------------------------

function envelope(overrides) {
  return {
    v: 1,
    layer: 1,
    seq: 0,
    t: 0,
    run_id: 'run_test',
    duel_id: 'd03',
    exchange_id: 'd03-r06-A',
    round: 6,
    side: 'A',
    producer: 'arena',
    type: 'command',
    p: {},
    ...overrides,
  };
}

// =============================================================================
// decode.js — parseLine / normalizeEvent
// =============================================================================

test('parseLine: unknown v is skipped, not fatal', () => {
  const line = JSON.stringify(envelope({ v: 2, type: 'command' }));
  assertEqual(parseLine(line), null, 'v=2 should decode to null');
  assertEqual(normalizeEvent(JSON.parse(line)), null, 'normalizeEvent should agree');
});

test('parseLine: unknown type is skipped, not fatal', () => {
  const line = JSON.stringify(envelope({ type: 'quantum_entangle' }));
  assertEqual(parseLine(line), null, 'an unrecognised type should decode to null');
});

test('parseLine: a partial trailing line returns null, not an error', () => {
  const truncated = JSON.stringify(envelope({ type: 'command' })).slice(0, 20); // deliberately cut mid-object
  assertEqual(parseLine(truncated), null, 'truncated JSON should decode to null');
  assertEqual(parseLine(''), null, 'an empty line should decode to null');
  assertEqual(parseLine('\n'), null, 'a bare newline should decode to null');
  assertEqual(parseLine('   \n'), null, 'a whitespace-only line should decode to null');
  // and it must not throw either, for any of the above:
  let threw = false;
  try {
    parseLine(truncated);
    parseLine('{"not even close');
  } catch {
    threw = true;
  }
  assert(!threw, 'parseLine must never throw on malformed input');
});

test('parseLine: a well-formed line round-trips, newline-terminated or not', () => {
  const raw = envelope({ seq: 5, t: 1.23, type: 'command', p: { server: 'slides', tool: 'query' } });
  const withNewline = JSON.stringify(raw) + '\n';
  const withoutNewline = JSON.stringify(raw);
  const a = parseLine(withNewline);
  const b = parseLine(withoutNewline);
  assert(a !== null && b !== null, 'a valid line must decode');
  assertEqual(a.seq, 5, 'seq preserved');
  assertEqual(a.t, 1.23, 't preserved');
  assertEqual(a, b, 'trailing newline must not change the decoded event');
});

test('normalizeEvent: missing/mistyped context fields degrade to null, not rejection', () => {
  const raw = envelope({ type: 'hp', side: 'not-a-side', run_id: 42, p: { A: 78, B: 61 } });
  const evt = normalizeEvent(raw);
  assert(evt !== null, 'a bad side/run_id must not reject the whole event');
  assertEqual(evt.side, null, 'an invalid side normalizes to null');
  assertEqual(evt.run_id, null, 'a non-string run_id normalizes to null');
});

test('normalizeEvent: a structurally malformed envelope (bad layer) is skipped', () => {
  assertEqual(normalizeEvent(envelope({ layer: 9 })), null, 'layer out of 1..4 must be skipped');
  assertEqual(normalizeEvent(envelope({ seq: -1 })), null, 'negative seq must be skipped');
  assertEqual(normalizeEvent(envelope({ p: 'not-an-object' })), null, 'non-object p must be skipped');
  assertEqual(normalizeEvent('not an object at all'), null, 'a bare string must be skipped');
  assertEqual(normalizeEvent(null), null, 'null must be skipped');
});

test('resolveRef: locates a blob indirection without fetching it', () => {
  const evt = normalizeEvent(
    envelope({
      type: 'model_turn',
      p: { iteration: 3, action_raw: { ref: 'blobs/0123456789abcdef' }, finish_reason: 'stop' },
    })
  );
  const refs = resolveRef(evt);
  assertEqual(refs.length, 1, 'exactly one ref should be found');
  assertEqual(refs[0].path, ['p', 'action_raw'], 'the path should point at the exact field');
  assertEqual(refs[0].sha16, '0123456789abcdef', 'the sha16 should be extracted from the ref string');
});

test('resolveRef: with a blobs map, splices a resolved value into a NEW event', () => {
  const evt = normalizeEvent(
    envelope({ type: 'model_turn', p: { action_raw: { ref: 'blobs/0123456789abcdef' } } })
  );
  const spliced = resolveRef(evt, { '0123456789abcdef': 'the full raw action line' });
  assertEqual(spliced.p.action_raw, 'the full raw action line', 'the ref should be replaced by its content');
  assertEqual(evt.p.action_raw, { ref: 'blobs/0123456789abcdef' }, 'the original event must be untouched');
  assert(spliced !== evt, 'a spliced event is a new object, not the original');
});

test('resolveRef: an event with no refs returns an empty array, no crash', () => {
  const evt = normalizeEvent(envelope({ type: 'command', p: { server: 'slides', tool: 'query' } }));
  assertEqual(resolveRef(evt), [], 'no refs to find');
});

// =============================================================================
// reduce.js — the pure fold
// =============================================================================

test('reduce: unknown v is a silent no-op, same reference returned', () => {
  const state = createInitialState();
  const evt = envelope({ v: 3, type: 'command' });
  const next = reduce(state, evt);
  assert(next === state, 'an unknown v must not touch state at all');
});

test('reduce: unknown type is a silent no-op, even fed directly (bypassing decode.js)', () => {
  const state = createInitialState();
  const evt = envelope({ type: 'a_type_from_the_future' });
  const next = reduce(state, evt);
  assert(next === state, 'an unknown type must not touch state at all');
});

test('reduce: never mutates its inputs', () => {
  const state = createInitialState();
  const stateCopy = JSON.parse(JSON.stringify(state));
  const evt = normalizeEvent(envelope({ type: 'hp', p: { A: 70, B: 55 } }));
  const evtCopy = JSON.parse(JSON.stringify(evt));
  reduce(state, evt);
  assertEqual(state, stateCopy, 'the input state must be unchanged after reduce()');
  assertEqual(evt, evtCopy, 'the input event must be unchanged after reduce()');
});

test('reduce: hp is rendered exactly as given, never derived', () => {
  let state = createInitialState();
  state = reduce(state, normalizeEvent(envelope({ type: 'hp', round: 6, p: { A: 78, B: 61 } })));
  assertEqual(state.sides.A.hp, 78, 'A hp taken verbatim');
  assertEqual(state.sides.B.hp, 61, 'B hp taken verbatim');
  assertEqual(state.round, 6, 'round refreshed from the envelope');
  assertEqual(state.roundScale, 1.25, 'round 6 falls in the ×1.25 band (r4-7)');
});

test('reduce: tool_call ticks the credit bar from credits_left (frozen render mapping)', () => {
  let state = createInitialState();
  state = reduce(
    state,
    normalizeEvent(envelope({ type: 'tool_call', side: 'A', p: { server: 'slides', tool: 'query', cost: 6, credits_left: 43 } }))
  );
  assertEqual(state.sides.A.credits, 43, 'credits_left should tick side A credits');
  assertEqual(state.sides.B.credits, 100, 'side B untouched');
});

test('reduce: credits (L3) handles both a dual-side and a single-side payload shape', () => {
  let dual = createInitialState();
  dual = reduce(dual, normalizeEvent(envelope({ type: 'credits', side: null, p: { A: 37, B: 52 } })));
  assertEqual(dual.sides.A.credits, 37);
  assertEqual(dual.sides.B.credits, 52);

  let single = createInitialState();
  single = reduce(single, normalizeEvent(envelope({ type: 'credits', side: 'B', p: { credits: 22 } })));
  assertEqual(single.sides.B.credits, 22);
  assertEqual(single.sides.A.credits, 100, 'side A untouched by a single-side credits event');
});

test('reduce: latent_violation increments a pure per-side counter', () => {
  let state = createInitialState();
  state = reduce(state, normalizeEvent(envelope({ type: 'latent_violation', side: 'A', p: { cls: 'wasteful' } })));
  state = reduce(state, normalizeEvent(envelope({ type: 'latent_violation', side: 'A', p: { cls: 'stale_read' } })));
  state = reduce(state, normalizeEvent(envelope({ type: 'latent_violation', side: 'B', p: { cls: 'wasteful' } })));
  assertEqual(state.sides.A.latent, 2);
  assertEqual(state.sides.B.latent, 1);
});

test('reduce: the combat log is bounded to the last 200 lines, oldest dropped first', () => {
  let state = createInitialState();
  const N = 250;
  for (let i = 0; i < N; i++) {
    state = reduce(state, normalizeEvent(envelope({ type: 'command', side: 'A', seq: i, p: { server: 'slides', tool: 'query' } })));
  }
  assertEqual(state.sides.A.log.length, 200, 'log must be capped at 200 entries');
  assertEqual(state.sides.A.log[0].seq, 50, 'the oldest surviving entry should be seq 50 (0..49 dropped)');
  assertEqual(state.sides.A.log[199].seq, 249, 'the newest entry should be the last one pushed');
  assertEqual(state.sides.B.log.length, 0, 'side B must be untouched by side-A-only events');
});

test('reduce: L4 choreography events queue without touching hp/credits/claims, and drain cleanly', () => {
  let state = createInitialState();
  state = reduce(state, normalizeEvent(envelope({ type: 'reveal', side: 'A', p: { card_id: 'atk_07' } })));
  state = reduce(state, normalizeEvent(envelope({ type: 'shake', side: 'A', p: { magnitude: 6 } })));
  state = reduce(
    state,
    normalizeEvent(envelope({ type: 'ticker', side: null, p: { text: 'Table A: d02 in progress', featured: { duelId: 'd02' } } }))
  );
  assertEqual(state.choreography.length, 3, 'three L4 events queued');
  assertEqual(state.featured, { duelId: 'd02' }, 'ticker.p.featured is mirrored onto state.featured');
  assertEqual(state.sides.A.hp, 100, 'L4 events never touch hp');
  assertEqual(state.sides.A.credits, 100, 'L4 events never touch credits');

  const { events, state: drained } = drainChoreography(state);
  assertEqual(events.length, 3, 'drain returns everything that was queued');
  assertEqual(drained.choreography.length, 0, 'drain empties the queue');
  assertEqual(state.choreography.length, 3, 'draining must not mutate the state it was given');
});

// ---------------------------------------------------------------------------
// a full exchange ending in a KO
// ---------------------------------------------------------------------------

function koFixtureEvents() {
  const base = { run_id: 'run_ko', duel_id: 'd03', exchange_id: 'd03-r06-A', round: 6 };
  return [
    envelope({ ...base, seq: 0, side: 'A', type: 'exchange_start', p: { attacker: 'team-07', defender: 'team-03', card_id: 'atk_07', world_id: 'w1', ask: { type: 'which_day_covers' } } }),
    envelope({ ...base, seq: 1, side: 'A', type: 'model_turn', p: { iteration: 1, prompt_tokens: 400, completion_tokens: 60, action_raw: 'call slides.query(...)', finish_reason: 'stop' } }),
    envelope({ ...base, seq: 2, side: 'A', type: 'command', p: { cmd_id: 'cmd:0007', kind: 'mcp', raw: 'slides.query', server: 'slides', tool: 'query', args: {}, fields: ['title'], headers: {}, lease_id: null, call_index: 0 } }),
    envelope({ ...base, seq: 3, side: 'A', type: 'decision', p: { verdict: 'forward', reason: null, latency_ms: 12, valid: true } }),
    envelope({ ...base, seq: 4, side: 'A', type: 'enforced', p: { verdict_applied: 'forward', charged: 6, reason: null } }),
    envelope({ ...base, seq: 5, side: 'A', type: 'tool_call', p: { server: 'slides', tool: 'query', mask: ['title'], cost: 6, credits_left: 94, replica: 'w', lease_used: null } }),
    envelope({ ...base, seq: 6, side: 'A', type: 'tool_result', p: { ok: true, error_code: null, n_rows: 3, partial: false, anchors: ['Frame:3f2a9c11/w/041'], bytes: 512, etag: 'sha256:16hex' } }),
    envelope({ ...base, seq: 7, side: 'A', type: 'mutation', p: { class: 'identity', target: 'a2a:curriculum-analyst', op: 'replace_act', applied: true, trigger_matched: true } }),
    envelope({ ...base, seq: 8, side: 'A', type: 'answer', p: { text: 'Day 26 covers streamable HTTP.', cited_anchors: ['Frame:3f2a9c11/w/041'], spans: [0] } }),
    envelope({ ...base, seq: 9, side: 'B', type: 'claim_filed', p: { cls: 'authority_exceeded', evidence: ['evt:0004'], expected: 'gateway.denied', observed: 'enforced.verdict_applied=forward', argument: 'no act check before forwarding' } }),
    envelope({ ...base, seq: 10, side: 'B', type: 'claim_outcome', p: { cls: 'authority_exceeded', evidence: ['evt:0004'], outcome: 'verified', weight: 10, scaled: 12, reasoning: 'ctx.act mismatch confirmed' } }),
    envelope({ ...base, seq: 11, side: null, type: 'hp', p: { A: 0, B: 88, round: 6 } }),
    envelope({ ...base, seq: 12, side: null, type: 'duel_end', p: { reason: 'ko', winner: 'B' } }),
  ];
}

test('reduce: a full exchange ending in a KO — hp verbatim, phase transitions, claim merged', () => {
  let state = createInitialState();
  for (const raw of koFixtureEvents()) {
    const evt = normalizeEvent(raw);
    assert(evt !== null, `fixture event type=${raw.type} must decode`);
    state = reduce(state, evt);
  }

  assertEqual(state.runId, 'run_ko');
  assertEqual(state.duelId, 'd03');
  assertEqual(state.exchangeId, 'd03-r06-A');
  assertEqual(state.round, 6);

  assertEqual(state.sides.A.team, 'team-03', 'A is the defender in exchange_start -> team A');
  assertEqual(state.sides.B.team, 'team-07', 'B is the attacker -> team B');

  assertEqual(state.sides.A.credits, 94, 'credits ticked from tool_call.credits_left');

  assertEqual(state.sides.B.claims.length, 1, 'claim_filed then claim_outcome merge into ONE claim entry');
  const claim = state.sides.B.claims[0];
  assertEqual(claim.cls, 'authority_exceeded');
  assertEqual(claim.outcome, 'verified');
  assertEqual(claim.weight, 10);
  assertEqual(claim.scaled, 12);
  assertEqual(claim.argument, 'no act check before forwarding', 'the argument from claim_filed is preserved onto the resolved claim');

  assertEqual(state.sides.A.hp, 0, 'A hp taken verbatim from the hp event');
  assertEqual(state.sides.B.hp, 88, 'B hp taken verbatim from the hp event');

  assertEqual(state.phase, 'duel_end', 'duel_end is the final, authoritative word on phase (overrides the ko phase hp alone would set)');

  // Side A logged 9 of its own events (exchange_start, model_turn, command,
  // decision, enforced, tool_call, tool_result, mutation, answer) plus the
  // shared duel_end = 10. Side B logged none of its own (claim_filed /
  // claim_outcome update `claims`, not `log`) plus the shared duel_end = 1.
  assertEqual(state.sides.A.log.length, 10, 'A: 9 side-scoped combat events + 1 shared duel_end');
  assertEqual(state.sides.B.log.length, 1, 'B: only the shared duel_end (claim events do not log)');
});

test('reduce: replay is free — folding the same fixture twice gives byte-identical state', () => {
  const events = koFixtureEvents().map(normalizeEvent);

  let stateA = createInitialState();
  for (const evt of events) stateA = reduce(stateA, evt);

  let stateB = createInitialState();
  for (const evt of events) stateB = reduce(stateB, evt);

  assertEqual(stateA, stateB, 'two independent folds of the same events must produce identical state');

  // And the decode.js -> reduce.js pipeline end to end, from JSONL TEXT this
  // time (not pre-built objects), must agree with the direct-object fold too
  // — this is the actual shape a live poll or a replay scrubber uses.
  let stateC = createInitialState();
  for (const raw of koFixtureEvents()) {
    const evt = parseLine(JSON.stringify(raw) + '\n');
    stateC = reduce(stateC, evt);
  }
  assertEqual(stateA, stateC, 'folding via parseLine(JSONL text) must match folding via normalizeEvent(object) exactly');
});

// =============================================================================
// summary
// =============================================================================

console.log(`\n${passed} passed, ${failures} failed`);
if (failures > 0) {
  process.exitCode = 1;
}
