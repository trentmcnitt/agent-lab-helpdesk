/* The browser twin of adapter.py: this app's event bus -> bench events (bench/0).
   The static demo uses it to drive the bench beside it (SPEC 3a: postMessage through the
   shell). Kept line-for-line with adapter.py; tests/test_bench_kit.py checks they agree on
   every recording. */
(function (root) {
  'use strict';
  var V = 'bench/0';
  var COST_BASIS = 'request-queue price table (app/config.py)';
  var LLM_KEYS = ['model', 'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'cost_usd',
                  'sampling', 'system', 'messages', 'output', 'params'];

  function r1(x) { return Math.round(x * 10) / 10; }
  function copy(o) { var c = {}; for (var k in o) c[k] = o[k]; return c; }

  function Adapter(sessionId, contentMode) {
    this.sessionId = sessionId || null;
    this.contentMode = contentMode || 'redacted';
    this.runId = null; this.seq = 0; this.started = false; this.finished = false;
    this.openNode = null; this.openStep = null; this.openTs = 0; this.openLast = 0;
    this.stepCounts = {}; this.lastTs = 0; this.sawError = false;
  }

  Adapter.prototype._ev = function (node, type, ts, data, stepId) {
    var ev = { v: V, run_id: this.runId, seq: this.seq, ts: ts, node: node, event_type: type,
               content_mode: this.contentMode, data: data || {} };
    if (this.sessionId) ev.session_id = this.sessionId;
    if (stepId) ev.step_id = stepId;
    this.seq++;
    return ev;
  };

  Adapter.prototype._close = function (ts, status) {
    if (this.openNode === null) return [];
    var out = [this._ev(this.openNode, 'step_finished', ts,
      { status: status || 'ok', latency_ms: r1(Math.max(0, ts - this.openTs) * 1000), latency_inferred: true }, this.openStep)];
    this.openNode = this.openStep = null;
    return out;
  };

  Adapter.prototype._open = function (node, ts, data) {
    var n = (this.stepCounts[node] || 0) + 1;
    this.stepCounts[node] = n;
    this.openNode = node; this.openStep = this.runId + ':' + node + ':' + n; this.openTs = ts; this.openLast = ts;
    return [this._ev(node, 'step_started', ts, data || null, this.openStep)];
  };

  Adapter.prototype._start = function (ts, data) {
    this.started = true;
    return [this._ev('_run', 'run_started', ts, data || {})];
  };

  Adapter.prototype.feed = function (src) {
    if (this.finished) return [];
    this.runId = this.runId || src.run_id;
    var node = src.node, et = src.event_type, data = copy(src.data || {}), ts = Number(src.ts);
    this.lastTs = Math.max(this.lastTs, ts);
    var out = [];

    if (node === '_meta') {
      var phase = data.phase;
      if (phase === 'started') {
        if (!this.started) out = out.concat(this._start(ts, 'origin' in data ? { origin: data.origin } : {}));
      } else if (phase === 'done') {
        out = out.concat(this.finish(ts, this.sawError ? 'error' : 'ok', data.final_response));
      } else if (phase === 'error') {
        this.sawError = true;
        out = out.concat(this._close(ts, 'error'));
        out.push(this._ev('_run', 'error', ts, { message: String(data.error != null ? data.error : 'run failed') }));
        out = out.concat(this.finish(ts, 'error'));
      }
      return out;
    }

    var input = (et === 'node_enter' && 'message' in data) ? { input: data.message } : null;
    if (!this.started) out = out.concat(this._start(ts, input || {}));

    if (node !== this.openNode) {
      // The previous step ends at its own last event; this node's work began then.
      var boundary = this.openNode !== null ? this.openLast : ts;
      out = out.concat(this._close(boundary));
      out = out.concat(this._open(node, boundary, input));
      this.openLast = ts;
      if (et === 'node_enter') return out;
    } else {
      this.openLast = ts;
      if (et === 'node_enter') return out;
    }

    var step = this.openStep, k;
    if (et === 'retrieval_hits') {
      var hits = (data.hits || []).map(function (h) {
        var hit = { id: String(h.chunk_id != null ? h.chunk_id : (h.id != null ? h.id : '?')), title: h.section || '' };
        if (h.score != null) hit.score = h.score;
        ['bm25', 'embed', 'text'].forEach(function (x) { if (x in h) hit[x] = h[x]; });
        return hit;
      });
      var rest = { hits: hits };
      for (k in data) if (k !== 'hits') rest[k] = data[k];
      out.push(this._ev(node, 'retrieval', ts, rest, step));
    } else if (et === 'decision') {
      // `branch` names the edge taken, matching the map's from_branch (Agent Spec branch_selected).
      if ('category' in data) { if (!('branch' in data)) data.branch = data.category; }
      else if ('grounded' in data) { if (!('branch' in data)) data.branch = data.grounded ? 'grounded' : 'not_grounded'; }
      out.push(this._ev(node, 'decision', ts, data, step));
    } else if (et === 'llm_call') {
      var llm = {};
      LLM_KEYS.forEach(function (x) { if (x in data && data[x] !== null && data[x] !== undefined) llm[x] = data[x]; });
      if (!('model' in llm)) llm.model = 'unknown';
      if (!('input_tokens' in llm)) llm.input_tokens = 0;
      if (!('output_tokens' in llm)) llm.output_tokens = 0;
      llm.provider = 'anthropic';
      if ('cost_usd' in llm) { llm.cost_source = 'estimated'; llm.cost_basis = COST_BASIS; }
      out.push(this._ev(node, 'llm_call', ts, llm, step));
    } else if (et === 'approval_requested') {
      var gw = {};
      if (data.proposed_action != null) gw.proposed = data.proposed_action;
      if (data.action_digest != null) gw.digest = data.action_digest;
      out.push(this._ev(node, 'gate_waiting', ts, gw, step));
    } else if (et === 'approval_result') {
      var gr = { approved: !!data.approved };
      [['approved_by', 'by'], ['approver_via', 'via'], ['digest_match', 'digest_match'], ['reason', 'reason'], ['mode', 'mode']]
        .forEach(function (p) { if (data[p[0]] != null) gr[p[1]] = data[p[0]]; });
      out.push(this._ev(node, 'gate_resolved', ts, gr, step));
    } else if (et === 'model_output_error') {
      out.push(this._ev(node, et, ts, data, step));
      out.push(this._ev(node, 'error', ts, { message: String(data.parsing_error != null ? data.parsing_error : 'model output did not parse'),
                                               type: 'model_output_error', retryable: true }, step));
    } else {
      // permission_verdict, tool_call, respond, handoff, and anything added later.
      out.push(this._ev(node, et, ts, data, step));
    }
    return out;
  };

  Adapter.prototype.finish = function (ts, status, output) {
    if (this.finished || this.runId === null) return [];
    ts = ts == null ? this.lastTs : ts;
    var out = this._close(ts);
    var d = { status: status || 'ok' };
    if (output != null) d.output = output;
    out.push(this._ev('_run', 'run_finished', ts, d));
    this.finished = true;
    return out;
  };

  var api = { Adapter: Adapter };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Scenario1Bench = api;
})(typeof window !== 'undefined' ? window : this);
