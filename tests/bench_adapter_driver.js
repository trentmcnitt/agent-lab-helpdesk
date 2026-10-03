// Feeds one recorded replay through the browser adapter, the same way adapter.py's
// convert_replay does, and prints the bench events as JSON. Used by test_bench_kit.py.
const fs = require('fs');
const { Adapter } = require('../app/bench/adapter.js');
const [path, tail] = process.argv.slice(2);
const rec = JSON.parse(fs.readFileSync(path, 'utf8'));
let src = rec.pre.slice(), final = rec.final;
if (rec.paused && tail && tail !== 'none') { src = src.concat(rec.tails[tail].events); final = rec.tails[tail].final; }
const a = new Adapter('replay', 'redacted');
let out = [];
for (const ev of src) out = out.concat(a.feed(ev));
out = out.concat(a.finish(null, 'ok', final));
process.stdout.write(JSON.stringify(out));
