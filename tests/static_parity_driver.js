// Plays every scenario through the browser replay engine with no delays and
// prints what a page would see, for tests/test_static_parity.py to compare.
const fs = require('fs');
const path = require('path');
const ReplayEngine = require(path.join(__dirname, '..', 'static', 'replay-engine.js'));
const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

(async () => {
  const out = {};
  for (const s of data.scenarios) {
    for (const choice of ['approve', 'wrong-digest']) {
      const e = new ReplayEngine(data, { sleep: async () => {} });
      const events = [];
      e.subscribe(ev => events.push(ev));
      const r = e.start(s.id);
      await r.done;
      const rec = data.replays[s.id];
      if (rec.paused) {
        await e.resume(r.run_id, true, choice === 'approve' ? rec.action_digest : 'wrong');
      }
      out[`${s.id}/${choice}`] = { events, thread: e.thread(r.thread_ts), board: e.board };
    }
  }
  process.stdout.write(JSON.stringify(out));
})();
