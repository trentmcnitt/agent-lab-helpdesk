/* The Slack Helpdesk Agent's story for the bench: the panels the old peek view drew by hand, now registered
   with the map instead of built into the page. Each draws on top of the bench's generic view;
   the panel's "raw" toggle still shows every field. */
BenchStory.register('slack-helpdesk', {
  panels: {
    // The handbook sections the model was given for this request, best match first. Each one
    // opens to its full text: exactly what went into the prompt. Ranked by the fused score
    // (BM25 keyword match + embeddings, reciprocal rank fusion); BM25 alone beside it.
    retrieval: function (events, ctx) {
      var h = ctx.h, hits = events[events.length - 1].data.hits || [];
      if (!hits.length) return '<span class="muted">no sections matched</span>';
      return '<div class="note" style="margin:0 0 6px">The model sees only these sections of the handbook, not the whole thing. Open one to read it as the model did.</div>' +
        hits.map(function (x, i) {
          var fused = x.bm25 !== undefined && x.score !== x.bm25;
          return '<details class="hit"><summary class="row"><span class="tag">' + (i + 1) + ' · ' + h.esc(x.id) + '</span> ' + h.esc(x.title) +
            '<span class="right mono">' + (fused ? 'fused ' + Number(x.score).toFixed(4) + ' · bm25 ' + Number(x.bm25).toFixed(1)
                                                : 'bm25 ' + Number(x.bm25 != null ? x.bm25 : x.score).toFixed(1)) + '</span></summary>' +
            (x.text ? '<pre class="io" style="margin:4px 0 8px">' + h.esc(x.text) + '</pre>' : '<div class="note">(section text not sent)</div>') +
            '</details>';
        }).join('');
    },

    classify: function (events, ctx) {
      var h = ctx.h, d = events[events.length - 1].data;
      var out = h.kv('category', d.category);
      if (d.original_category && d.original_category !== d.category) out += h.kv('model said', d.original_category);
      if (d.confidence != null) out += h.kv('confidence', Number(d.confidence).toFixed(2));
      if (d.rationale) out += '<div class="note">' + h.esc(d.rationale) + '</div>';
      if ((d.rationale_cites_unretrieved || []).length)
        out += '<div class="note" style="color:var(--warn)">cites section ' + h.esc(d.rationale_cites_unretrieved.join(', ')) + ', which retrieval didn\'t return</div>';
      return out;
    },

    grounding: function (events, ctx) {
      var h = ctx.h, d = events[events.length - 1].data;
      return '<div style="color:' + (d.grounded ? 'var(--good)' : 'var(--bad)') + '">' + (d.grounded ? '✓ grounded' : '✕ not grounded: handed to a human') + '</div>' +
        (d.overlap != null ? h.kv('overlap with the cited section', Number(d.overlap).toFixed(2)) : '') +
        (d.cited ? h.kv('cited', d.cited.join(', ')) : '') + (d.reason && d.reason !== 'ok' ? '<div class="note">' + h.esc(d.reason) + '</div>' : '');
    },

    permission: function (events, ctx) {
      var h = ctx.h, d = events[events.length - 1].data;
      return h.kv('action', d.action_type) + h.kv('allowed', d.allowed) + h.kv('forbidden', d.forbidden) +
        (d.requires_approval != null ? h.kv('needs a human', d.requires_approval) : '') + (d.reason ? '<div class="note">' + h.esc(d.reason) + '</div>' : '');
    },

    // The raw arguments that will execute, not just the model's prose, and the digest the
    // approval must carry back. The decision itself is made in the app.
    gate: function (events, ctx) {
      var h = ctx.h;
      return events.map(function (ev) {
        var d = ev.data;
        if (ev.event_type === 'gate_waiting') {
          var a = d.proposed || {};
          var args = ['action_type', 'target_system', 'target_tier', 'assignee', 'title'].filter(function (k) { return a[k] != null; })
            .map(function (k) { return h.kv(k, a[k]); }).join('');
          return '<div class="gate waiting">⏸ waiting for a human, in the app</div>' + args +
            (a.description ? '<div class="note">model\'s description: ' + h.esc(a.description) + '</div>' : '') +
            (d.digest ? h.kv('action sha256', String(d.digest).slice(0, 12)) : '');
        }
        return '<div class="gate ' + (d.approved ? 'ok' : 'bad') + '">' + (d.approved ? '✓ approved' : '✕ denied') +
          (d.by ? ' by ' + h.esc(d.by) : '') + (d.via ? ' via ' + h.esc(d.via) : '') + '</div>' +
          (d.digest_match === false ? '<div class="err">digest mismatch: the approval was for a different action</div>' : '');
      }).join('<div style="height:6px"></div>');
    }
  }
});
