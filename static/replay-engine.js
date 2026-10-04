/* Recorded-run replay, in the browser. A port of ReplayLibrary in
 * app/demo/engine.py, so a static page can play the same recordings with no
 * server: the recorded events with their original pacing (gaps capped), the
 * pause at the approval card, then the approved or denied ending. Approving
 * with the digest the card showed opens the recorded ticket; anything else
 * plays the denied ending. No model is called; everything here is a recording.
 *
 * A recording made by scripts/regen_demo.py also carries `bench`: Agent Lab's events of the same
 * run. They play on the same clock, interleaved with the app's own events, to `subscribeBench`
 * listeners (static-adapter.js hands them to the bench beside the page). The app's own stream
 * is exactly what it was without them.
 *
 * Works in a browser (window.ReplayEngine) and in Node (module.exports), so
 * tests/test_static_parity.py can check it against the Python replay. */
(function (root) {
  'use strict';

  function ReplayEngine(data, opts) {
    opts = opts || {};
    this.replays = data.replays;          // scenario id -> recording
    this.scenarios = data.scenarios;       // [{id, preview, expected_category, manual_estimate}]
    this.model = data.model;               // the model the recordings were made with
    this.maxGap = opts.maxGap == null ? 1.2 : opts.maxGap;
    this.sleep = opts.sleep || function (s) { return new Promise(function (r) { setTimeout(r, s * 1000); }); };
    this.now = opts.now || function () { return Date.now() / 1000; };
    this.randomHex = opts.randomHex || function (n) {
      var s = ''; while (s.length < n) s += Math.floor(Math.random() * 16).toString(16); return s;
    };
    this.listeners = [];
    this.benchListeners = [];
    this.board = [];                        // newest first, like the server's board
    this.threads = {};                      // thread_ts -> [message]
    this.runs = {};                         // run_id -> paused replay
    this.clocks = {};                       // run_id -> {vt, pausedAt}: the run's own clock (see _emit)
  }

  ReplayEngine.prototype.subscribe = function (fn) { this.listeners.push(fn); };
  ReplayEngine.prototype.publish = function (ev) { this.listeners.forEach(function (fn) { fn(ev); }); };
  ReplayEngine.prototype.subscribeBench = function (fn) { this.benchListeners.push(fn); };
  ReplayEngine.prototype.publishBench = function (ev) { this.benchListeners.forEach(function (fn) { fn(ev); }); };

  /* The app's events and the bench's, in time order (both are on the recording's clock); on a tie
     the bench's go first, so a step opens before what happened in it. */
  function merge(events, bench) {
    var out = events.map(function (ev, i) { return { ev: ev, bench: false, i: i }; })
      .concat((bench || []).map(function (ev, i) { return { ev: ev, bench: true, i: i }; }));
    return out.sort(function (a, b) {
      return (a.ev.ts - b.ev.ts) || (a.bench === b.bench ? a.i - b.i : (a.bench ? -1 : 1));
    });
  }
  ReplayEngine.prototype.meta = function (runId, phase, extra) {
    var data = { phase: phase };
    for (var k in (extra || {})) data[k] = extra[k];
    var c = this.clocks[runId];
    this.publish({ run_id: runId, node: '_meta', event_type: 'phase', data: data, ts: c ? c.vt : this.now() });
  };

  ReplayEngine.prototype.post = function (user, text, threadTs) {
    var ts = this.now().toFixed(6);
    while (this.threads[ts] && !threadTs) ts = (parseFloat(ts) + 0.000001).toFixed(6);
    var msg = { type: 'message', channel: 'helpdesk-requests', user: user, text: text, ts: ts, thread_ts: threadTs || ts };
    (this.threads[msg.thread_ts] = this.threads[msg.thread_ts] || []).push(msg);
    return msg;
  };

  ReplayEngine.prototype.start = function (scenarioId) {
    var rec = this.replays[scenarioId];
    if (!rec) return null;
    var posted = this.post(rec.requester_name, rec.message);
    var runId = 'run-replay-' + this.randomHex(10);
    var done = this._playPre(rec, runId, posted.ts);
    return { run_id: runId, thread_ts: posted.ts, channel: 'helpdesk-requests', mode: 'replay', done: done };
  };

  /* Plays with the recorded gaps capped at maxGap, so a visitor doesn't wait out every model
     call, but stamps each event on the run's own clock, which advances by the recorded gaps
     (uncapped) and by the visitor's real wait at the approval card. So the times the bench shows
     are the recording's (a 4 s model call reads 4 s) plus the real human wait, the same numbers
     the bench's own replay of this recording shows. */
  ReplayEngine.prototype._emit = async function (events, runId, bench) {
    var prev = null, c = this.clocks[runId] || (this.clocks[runId] = { vt: this.now() });
    var items = merge(events, bench);
    for (var i = 0; i < items.length; i++) {
      var ev = items[i].ev;
      if (prev !== null) {
        await this.sleep(Math.min(Math.max(ev.ts - prev, 0), this.maxGap));
        c.vt += Math.max(ev.ts - prev, 0);
      }
      prev = ev.ts;
      var out = {}; for (var k in ev) out[k] = ev[k];
      if (items[i].bench) {
        // The recording's run id becomes this run's, in its step ids too.
        ['step_id', 'parent_step_id'].forEach(function (f) {
          if (typeof out[f] === 'string' && out[f].indexOf(ev.run_id + ':') === 0) out[f] = runId + out[f].slice(ev.run_id.length);
        });
        out.run_id = runId; out.ts = c.vt;
        this.publishBench(out);
      } else {
        out.run_id = runId; out.ts = c.vt;
        this.publish(out);
      }
    }
  };

  ReplayEngine.prototype._playPre = async function (rec, runId, threadTs) {
    this.clocks[runId] = { vt: this.now() };
    this.meta(runId, 'started', { origin: 'replay', channel: 'helpdesk-requests', thread_ts: threadTs, requester: rec.requester_name });
    var bench = rec.bench || {};
    await this._emit(rec.pre, runId, rec.paused ? bench.pre : bench.events);
    if (rec.paused) {
      this.runs[runId] = { rec: rec, thread_ts: threadTs };
      this.clocks[runId].pausedAt = this.now();
      this.meta(runId, 'awaiting_approval');
      return;
    }
    this._finish(runId, threadTs, rec.final);
  };

  /* Returns false for an unknown or already-resolved run (the server answers 404). */
  ReplayEngine.prototype.resume = function (runId, approved, digest) {
    var entry = this.runs[runId];
    if (!entry || entry.resolved) return false;
    entry.resolved = true;
    var c = this.clocks[runId];
    if (c && c.pausedAt != null) { c.vt += Math.max(0, this.now() - c.pausedAt); c.pausedAt = null; }   // the real wait
    var rec = entry.rec;
    // Same rule as a live run: an approval must carry the digest of the action shown.
    var tail = approved && digest === rec.action_digest ? 'approved' : 'denied';
    return this._playTail(rec, runId, entry.thread_ts, tail);
  };

  ReplayEngine.prototype._playTail = async function (rec, runId, threadTs, tail) {
    // Open the recorded tickets first, then swap the recorded ids for the new ones
    // everywhere they appear, so the reply and the board agree.
    var ids = {};
    var self = this;
    (rec.tails[tail].tickets || []).forEach(function (t) {
      var ticket = {
        id: 'REQ-' + self.randomHex(6), title: t.title, description: t.description || '',
        status: t.assignee ? 'assigned' : 'open', assignee: t.assignee || null,
        labels: t.labels || '', created_by: t.created_by || '', created_at: self.now(),
      };
      self.board.unshift(ticket);
      ids[t.id] = ticket.id;
    });
    var d = new Date(this.now() * 1000);  // built by hand: toLocaleTimeString puts a narrow no-break space before AM/PM
    var now = ((d.getHours() % 12) || 12) + ':' + String(d.getMinutes()).padStart(2, '0') + ' ' + (d.getHours() < 12 ? 'AM' : 'PM');
    function swap(text) {
      Object.keys(ids).forEach(function (old) { text = text.split(old).join(ids[old]); });
      // The recording's approval time becomes the visitor's.
      return text.replace(/(Approved by [^.\n]*? at )\d{1,2}:\d{2} [AP]M/g, function (_, head) { return head + now; });
    }
    var events = JSON.parse(swap(JSON.stringify(rec.tails[tail].events)));
    var bench = rec.bench && rec.bench.tails ? JSON.parse(swap(JSON.stringify(rec.bench.tails[tail] || []))) : [];
    await this._emit(events, runId, bench);
    delete this.runs[runId];
    this._finish(runId, threadTs, swap(rec.tails[tail].final));
  };

  ReplayEngine.prototype._finish = function (runId, threadTs, finalText) {
    this.post('helpdesk-agent', finalText, threadTs);
    this.meta(runId, 'done', { final_response: finalText, replay: true });
    delete this.clocks[runId];
  };

  ReplayEngine.prototype.thread = function (threadTs) { return (this.threads[threadTs] || []).slice(); };

  if (typeof module !== 'undefined' && module.exports) module.exports = ReplayEngine;
  else root.ReplayEngine = ReplayEngine;
})(typeof window !== 'undefined' ? window : this);
