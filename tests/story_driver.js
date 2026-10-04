// Renders every story panel at every event of every recording given, in both view modes, with a
// stub BenchStory and the bench's helper shapes, and prints one line per problem as JSON (an
// empty array is a pass). Used by test_agent_lab.py for its Presentation leak checks, beside the bench's own harness.
// As in the viewer: a panel whose audience is "engineering" isn't shown in Presentation, and in
// Presentation a panel adds to the generic callout, so "" (nothing to add for this step) is fine.
const fs = require('fs');
const vm = require('vm');
const files = process.argv.slice(2);

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}
function kv(k, v) { return '<div class="kv"><span>' + esc(k) + '</span><b>' + esc(v) + '</b></div>'; }
const h = { esc, kv, fmtMs: String, fmtUsd: String, fmtTok: String, short: String, fields: () => '', generic: () => '' };
// What Presentation must never show: scores, ids, digests, confidence.
const PRESENTATION_LEAKS = [/\bsec-\d+/, /bm25/i, /fused /, /sha256/i, /confidence/i, /\b0\.\d{2,}\b/];

const problems = [];
for (const file of files) {
  const lines = fs.readFileSync(file, 'utf8').split('\n').filter(Boolean).map(JSON.parse);
  const head = lines[0], events = lines.slice(1);
  let story = null;
  const BenchStory = { register: (id, s) => { story = s; } };
  vm.runInNewContext(head.story, { BenchStory });
  if (!story) { problems.push(file + ': story did not register'); continue; }
  const panels = head.topology.panels.filter(p => p.story);
  for (const mode of ['presentation', 'engineering']) {
    for (const p of panels) {
      if (mode === 'presentation' && p.audience === 'engineering') continue;
      const fn = story.panels[p.id];
      if (typeof fn !== 'function') { problems.push(`${file}: no function for panel ${p.id}`); continue; }
      let rendered = 0;
      for (let i = 0; i < events.length; i++) {
        const seen = events.slice(0, i + 1);
        const list = seen.filter(e => p.event_types.includes(e.event_type) && (!p.nodes || p.nodes.includes(e.node)));
        if (!list.length || list[list.length - 1] !== events[i]) continue;
        const where = `${file.split('/').pop()} ${mode} ${p.id} @${i}`;
        let out;
        try { out = fn(list, { run: { id: events[i].run_id }, h, all: seen, mode, topo: head.topology }); } catch (e) { problems.push(`${where}: threw ${e.message}`); continue; }
        rendered++;
        if (typeof out !== 'string') problems.push(`${where}: returned ${typeof out}`);
        else if (!out.trim()) { if (mode !== 'presentation') problems.push(`${where}: empty result`); }
        else {
          for (const bad of ['undefined', 'NaN', '[object Object]']) if (out.includes(bad)) problems.push(`${where}: contains ${bad}`);
          if (mode === 'presentation') for (const re of PRESENTATION_LEAKS) if (re.test(out)) problems.push(`${where}: shows ${re} in presentation`);
        }
      }
      if (!rendered && events.some(e => p.event_types.includes(e.event_type) && (!p.nodes || p.nodes.includes(e.node))))
        problems.push(`${file}: panel ${p.id} never rendered`);
    }
  }
}
process.stdout.write(JSON.stringify(problems));
