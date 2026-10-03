/* Lets ui/index.html run as a static page. The page talks to its server
 * through fetch('/api/...') and one EventSource('/api/stream'); this file
 * answers those in the browser from ReplayEngine and recorded runs in
 * data.json, so no server is needed. The page's own code is unchanged, and it
 * says plainly that every run here is a recording.
 *
 * Load order in the exported index.html: replay-engine.js, this file, then the
 * page's script. data.json sits next to this file, so the page works under any
 * subpath (e.g. /agentlabs/). */
(function () {
  'use strict';
  var base = document.currentScript ? document.currentScript.src : location.href;
  var realFetch = window.fetch.bind(window);
  var engine = null, loaded = null;
  var ready = realFetch(new URL('data.json', base).href)
    .then(function (r) { return r.json(); })
    .then(function (data) { loaded = data; engine = new window.ReplayEngine(data); return engine; });

  var REASON = 'this is a static page, so every run is a recording';

  function json(body, status) {
    return new Response(JSON.stringify(body), { status: status || 200, headers: { 'Content-Type': 'application/json' } });
  }

  async function handle(path, init) {
    var e = await ready;
    var method = ((init && init.method) || 'GET').toUpperCase();
    var body = {};
    try { body = init && init.body ? JSON.parse(init.body) : {}; } catch (err) {}
    var m;
    if (path === '/api/status') {
      return json({ demo: true, mode: 'replay', mode_reason: REASON, free_text: false,
                    model: e.model || 'claude-sonnet-5', langfuse: 'off (static page)',
                    slack: 'mock', slack_channel: 'helpdesk-requests',
                    seed_requests: e.scenarios.map(function (s) { return s.id; }) });
    }
    if (path === '/api/seed_requests') return json(e.scenarios);
    if (path === '/api/board') return json(e.board);
    if ((m = path.match(/^\/api\/thread\/([^/]+)\/([^/]+)$/))) {
      if (decodeURIComponent(m[1]).replace(/^#/, '') !== 'helpdesk-requests') return json({ detail: 'only the helpdesk channel is readable here' }, 404);
      return json(e.thread(decodeURIComponent(m[2])));
    }
    if (method === 'POST' && (m = path.match(/^\/api\/requests\/seed\/([^/]+)$/))) {
      var started = e.start(decodeURIComponent(m[1]));
      if (!started) return json({ detail: 'unknown scenario' }, 404);
      return json({ run_id: started.run_id, thread_ts: started.thread_ts, channel: started.channel,
                    mode: 'replay', mode_reason: REASON });
    }
    if (method === 'POST' && (m = path.match(/^\/api\/approve\/([^/]+)$/))) {
      var ok = e.resume(decodeURIComponent(m[1]), !!body.approved, body.action_digest || null);
      return ok ? json({ ok: true }) : json({ detail: 'no paused run with that id on this page' }, 404);
    }
    if (path === '/api/requests/custom') return json({ detail: "free-text requests need the live demo; this page plays recordings" }, 404);
    return json({ detail: 'not found' }, 404);
  }

  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : input.url;
    if (url.indexOf('/api/') === 0) return handle(url.split('?')[0], init);
    return realFetch(input, init);
  };

  /* The page's single stream: engine events arrive as SSE-shaped messages. */
  function StaticEventSource(url) {
    var self = this;
    this.url = url; this.readyState = 1; this.onmessage = null;
    ready.then(function (e) {
      e.subscribe(function (ev) {
        if (typeof self.onmessage === 'function') self.onmessage({ data: JSON.stringify(ev) });
      });
    });
  }
  StaticEventSource.prototype.close = function () { this.readyState = 2; this.onmessage = null; };
  /* Inside the Agent Lab Bench shell (?bench_session=...), every run also goes to the bench
     beside this page, converted by bench-adapter.js and posted through the shell (the bench
     spec's section 3a). Outside the shell, none of this runs. */
  var params = new URLSearchParams(location.search);
  var benchSession = params.get('bench_session');
  if (benchSession && window.self !== window.top && window.Scenario1Bench) {
    var target = params.get('bench_origin') || (document.referrer ? new URL(document.referrer).origin : location.origin);
    var adapters = {};
    var post = function (msg) { window.parent.postMessage(msg, target); };
    ready.then(function (e) {
      var d = loaded || {};
      if (d.bench) post({ type: 'bench:register', app_id: d.bench.topology.app.id, topology: d.bench.topology, story: d.bench.story });
      e.subscribe(function (ev) {
        if (!ev || !ev.run_id) return;
        var a = adapters[ev.run_id] || (adapters[ev.run_id] = new window.Scenario1Bench.Adapter(benchSession, 'redacted'));
        var out = a.feed(ev);  // the engine stamps events as shown, so waits read as they happened
        if (out.length) post({ type: 'bench:events', events: out });
        if (a.finished) delete adapters[ev.run_id];
      });
    });
  }

  var RealEventSource = window.EventSource;
  window.EventSource = function (url, cfg) {
    return String(url).indexOf('/api/') === 0 ? new StaticEventSource(url) : new RealEventSource(url, cfg);
  };
})();
