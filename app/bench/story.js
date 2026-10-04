/* The Slack Helpdesk Agent's story for Agent Lab: custom panels drawn over the bench's generic view
   (each panel's "raw" toggle still shows every field). The panels themselves (which events, which
   nodes, their titles) are declared beside the graph in app/agent_lab.py (lab.Story); the events
   come from the app's lab.* lines in app/graph.py:
   - retrieval: lab.retrieved in retrieve;
   - decision, check_result (confidence_check) and the app's own `triage` event in classify;
   - check_result (grounded) in grounding_check;
   - the app's own `permission_verdict` event in permission_check;
   - gate_waiting (derived from interrupt(): `proposed` is the interrupt's value) and gate_resolved
     (lab.gate_resolved) in approval_gate.
   ctx.mode: "presentation" adds to the bench's generic callout only what this story alone knows
   (the generic one already shows the path taken, the proposal, the checks and who decided), in
   plain words with no scores, confidence or ids, and returns "" when it has nothing to add;
   "engineering" (and a bench too old to send a mode) keeps every field. Words the map already has
   (a branch's plain label, a handbook section's title) are read from ctx.topo, never retyped here.
   This file names no node ids, so lab.Story's `reads` is empty. */
(function () {
  'use strict';
  // A branch out of `node`, in the map's own words (its plain_label), else its id in words.
  function pathWords(ctx, node, branch) {
    var ed = ((ctx.topo && ctx.topo.edges) || []).filter(function (e) { return e.from === node && e.from_branch === branch; })[0];
    return (ed && ed.plain_label) || String(branch).replace(/_/g, ' ');
  }
  // A handbook item by its title in the map's sources (what lab.corpus read from the index).
  function itemTitle(ctx, id) {
    var found = null;
    ((ctx.topo && ctx.topo.sources) || []).forEach(function (s) { (s.items || []).forEach(function (it) { if (it.id === id) found = it.title; }); });
    return found || id;
  }
  function latest(events, type) {
    for (var i = events.length - 1; i >= 0; i--) if (events[i].event_type === type) return events[i].data || {};
    return null;
  }

  BenchStory.register('slack-helpdesk', {
    panels: {
      // Engineering (the panel's audience): the handbook sections the model was given, best match
      // first, each with its fused score (BM25 keyword match + embeddings, reciprocal rank fusion)
      // and opening to its full text, exactly what went into the prompt.
      retrieval: function (events, ctx) {
        var h = ctx.h, hits = events[events.length - 1].data.hits || [];
        if (!hits.length) return '<span class="muted">no sections matched</span>';
        var lead = 'retrieve: ranked by the fused score (BM25 keyword match + embedding similarity, reciprocal rank fusion). The model sees only these sections of the handbook, not the whole thing. Open one to read it as the model did.';
        return '<div class="note" style="margin:0 0 6px">' + h.esc(lead) + '</div>' +
          hits.map(function (x, i) {
            var right = x.score == null ? '' : '<span class="right mono">' + (x.bm25 != null ? 'fused ' + Number(x.score).toFixed(4) + ' · bm25 ' + Number(x.bm25).toFixed(1)
                                                                                         : 'score ' + Number(x.score).toFixed(4)) + '</span>';
            return '<details class="hit"><summary class="row"><span class="tag">' + (i + 1) + ' · ' + h.esc(x.id) + '</span> ' +
              h.esc(x.title || x.id) + right + '</summary>' +
              (x.text ? '<pre class="io" style="margin:4px 0 8px">' + h.esc(x.text) + '</pre>' : '<div class="note">(section text not sent)</div>') +
              '</details>';
          }).join('');
      },

      // classify's decision (the model's rationale), its low-confidence backstop, and the app's
      // `triage` event: the path the model picked, the path the app routed it to, and any section
      // the rationale cites that the search never gave it.
      classify: function (events, ctx) {
        var h = ctx.h, d = latest(events, 'decision') || {}, t = latest(events, 'triage') || {}, unsure = null;
        events.forEach(function (ev) { if (ev.event_type === 'check_result' && ev.data && ev.data.name === 'confidence_check') unsure = ev.data; });
        var unseen = t.unseen || [];             // handbook item ids the reason cites that the search never gave it
        var routed = t.routed, picked = t.picked, node = events[0].node;
        if (ctx.mode === 'presentation') {
          // The path taken and the AI's reason are the callout's own; only what they can't say is here.
          var out = '';
          if (picked && routed && picked !== routed)
            out += '<div class="note">The AI picked “' + h.esc(pathWords(ctx, node, picked)) +
              '”, but it wasn\'t confident enough, so the request goes to a person.</div>';
          else if (routed && picked === null)
            out += '<div class="note">The AI\'s choice couldn\'t be read, so the request goes to a person.</div>';
          if (unseen.length) out += '<div class="note" style="color:var(--warn)">Its reason mentions ' +
            h.esc(unseen.map(function (id) { return itemTitle(ctx, id); }).join(', ')) + ', which the search didn\'t give it.</div>';
          return out;
        }
        var o = routed ? h.kv('category', routed) : '';
        if (picked && routed && picked !== routed) o += h.kv('model said', picked);
        if (d.confidence != null) o += h.kv('confidence', Number(d.confidence).toFixed(2) + ' (the model\'s own estimate, not measured accuracy)');
        if (unsure) o += h.kv('low-confidence check', unsure.passed ? 'passed' : 'triggered: overridden to escalate');
        if (d.cited && d.cited.length) o += h.kv('rationale cites', d.cited.join(', '));
        if (d.rationale) o += '<div class="note">' + h.esc(d.rationale) + '</div>';
        if (unseen.length)
          o += '<div class="note" style="color:var(--warn)">cites ' + h.esc(unseen.join(', ')) + ', which retrieval didn\'t return</div>';
        return o || '<span class="muted">no decision yet</span>';
      },

      // The grounding rule's verdict (lab.check "grounded"): evidence = the sections it relied on.
      grounding: function (events, ctx) {
        var h = ctx.h, d = events[events.length - 1].data || {}, evidence = d.evidence || [];
        if (ctx.mode === 'presentation') {
          // The verdict and its detail are the callout's; this adds what the check can't catch.
          return d.passed ? '<div class="note">This check compares wording; it can\'t tell whether an answer that uses the handbook\'s own words is still wrong.</div>' : '';
        }
        return '<div style="color:' + (d.passed ? 'var(--good)' : 'var(--bad)') + '">' + (d.passed ? '✓ grounded' : '✕ not grounded: handed to a human') + '</div>' +
          (evidence.length ? h.kv('evidence', evidence.join(', ')) : '') + (d.detail ? '<div class="note">' + h.esc(d.detail) + '</div>' : '');
      },

      permission: function (events, ctx) {
        var h = ctx.h, d = events[events.length - 1].data || {};
        if (ctx.mode === 'presentation') {
          // "allowed" and the reason are the permission check's (shown by the callout); this says
          // what was asked for (the app's own words for it) and when it's never allowed.
          var what = d.asked_for || String(d.action_type || 'something').replace(/_/g, ' ');
          return '<div>Asked for: <b>' + h.esc(what) + '</b></div>' +
            (d.forbidden ? '<div class="note">That is never allowed, whoever asks.</div>' : '');
        }
        return h.kv('action', d.action_type) + h.kv('allowed', d.allowed) + h.kv('forbidden', d.forbidden) +
          (d.requires_approval != null ? h.kv('needs a human', d.requires_approval) : '') + (d.reason ? '<div class="note">' + h.esc(d.reason) + '</div>' : '');
      },

      // Engineering (the panel's audience): the raw arguments that will execute, not just the
      // model's prose, and the digest the approval must carry back. The decision is made in the app.
      gate: function (events, ctx) {
        var h = ctx.h;
        // Once a person has decided, the ticket stays (what they decided on) but not the "waiting" line.
        var decided = events.some(function (ev) { return ev.event_type === 'gate_resolved'; });
        return events.map(function (ev) {
          var d = ev.data || {};
          if (ev.event_type === 'gate_waiting') {
            var p = d.proposed || {}, a = p.proposed_action || {};
            var args = ['action_type', 'target_system', 'target_tier', 'assignee', 'title'].filter(function (k) { return a[k] != null; })
              .map(function (k) { return h.kv(k, a[k]); }).join('');
            return (decided ? '' : '<div class="gate waiting">⏸ waiting for a human, in the app</div>') + args +
              (a.description ? '<div class="note">model\'s description: ' + h.esc(a.description) + '</div>' : '') +
              (p.action_digest ? h.kv('action sha256', String(p.action_digest).slice(0, 12)) : '');
          }
          var head = '<div class="gate ' + (d.approved ? 'ok' : 'bad') + '">' + (d.approved ? '✓ approved' : '✕ denied') +
            (d.by ? ' by ' + h.esc(d.by) : '') + '</div>';
          return head + (!d.approved && d.reason ? '<div class="note">' + h.esc(d.reason) + '</div>' : '');
        }).join('<div style="height:6px"></div>');
      }
    }
  });
})();
