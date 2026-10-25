'use strict';
// rag.js の検証: 合成DBでの正しさと 115,000件規模での応答時間、実 rag.db での動作。
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const rag = require('../medical-ai-app/src/rag.js');

const DIM = 768;
const REAL_DB = path.join(__dirname, '..', '..', 'data', 'rag.db');

function randomVec(seed) {
  // 決定的な疑似乱数（xorshift）
  let x = seed * 2654435761 >>> 0 || 1;
  const v = new Float32Array(DIM);
  for (let j = 0; j < DIM; j++) {
    x ^= x << 13; x >>>= 0; x ^= x >>> 17; x ^= x << 5; x >>>= 0;
    v[j] = (x / 0xffffffff) * 2 - 1;
  }
  return v;
}

function makeDb(file, n) {
  const db = new Database(file);
  db.exec(`CREATE TABLE chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, code TEXT,
           title TEXT NOT NULL, section TEXT, body TEXT NOT NULL, embedding BLOB NOT NULL);
           CREATE INDEX idx_code ON chunks (code);
           CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);`);
  db.prepare('INSERT INTO meta VALUES (?, ?)').run('embed_dim', String(DIM));
  db.prepare('INSERT INTO meta VALUES (?, ?)').run('embed_model', 'fake');
  db.exec('CREATE TABLE keywords (chunk_id INTEGER NOT NULL, word TEXT NOT NULL)');
  db.prepare('INSERT INTO keywords VALUES (?, ?)').run(11, 'ワルファリン');
  db.prepare('INSERT INTO keywords VALUES (?, ?)').run(11, 'ワーファリン');
  const ins = db.prepare('INSERT INTO chunks (category, code, title, section, body, embedding) VALUES (?, ?, ?, ?, ?, ?)');
  const cats = ['icd10', 'dpc', 'formulary'];
  db.transaction(() => {
    for (let i = 1; i <= n; i++) {
      const v = randomVec(i);
      ins.run(cats[i % 3], i === 7 ? 'I21.0' : `C${i}`, `title${i}`, null, `body${i}`, Buffer.from(v.buffer));
    }
  })();
  db.close();
}

test('合成DB: 自分自身のベクトルが1位・カテゴリ絞り込み・コード完全一致', async () => {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'rag-')), 'rag.db');
  makeDb(file, 3000);
  const info = await rag.initRag({ dbPath: file, embedQuery: async () => randomVec(42) });
  assert.strictEqual(info.count, 3000);

  const hits = await rag.searchRag('なんでも', { topK: 5 });
  assert.strictEqual(hits[0].id, 42);
  assert.ok(Math.abs(hits[0].score - 1) < 1e-4);
  assert.ok(!('embedding' in hits[0]));

  const dpc = await rag.searchRag('なんでも', { topK: 5, category: 'dpc' });
  assert.ok(dpc.every((h) => h.category === 'dpc'));

  const coded = await rag.searchRag('I21.0 について', { topK: 3 });
  assert.strictEqual(coded[0].code, 'I21.0');

  const drug = await rag.searchRag('ワーファリンの用量は？', { topK: 3 });
  assert.strictEqual(drug[0].id, 11);
  assert.strictEqual(drug.filter((h) => h.id === 11).length, 1);

  const prompt = rag.buildRagPrompt('質問です', hits.slice(0, 2));
  assert.match(prompt, /^以下の医療情報を参考に/);
  assert.match(prompt, /推測で答えず/);
  assert.match(prompt, /\[参考: [^\]]*title42\] body42/);
  assert.match(prompt, /質問: 質問です$/);
  rag.closeRag();
});

test('115,000件で検索応答 < 200ms（クエリ埋め込みを除く）', async () => {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'rag-')), 'rag.db');
  makeDb(file, 115000);
  const t0 = performance.now();
  await rag.initRag({ dbPath: file, embedQuery: async () => randomVec(1) });
  const loadMs = performance.now() - t0;
  const times = [];
  for (let s = 0; s < 10; s++) {
    const t = performance.now();
    const hits = rag.searchByVector(randomVec(1000 + s), { topK: 5 });
    times.push(performance.now() - t);
    assert.strictEqual(hits[0].id, 1000 + s);
  }
  times.sort((a, b) => a - b);
  const median = times[5];
  console.log(`  load ${loadMs.toFixed(0)} ms / search median ${median.toFixed(1)} ms, max ${times[9].toFixed(1)} ms`);
  assert.ok(median < 200, `median ${median} ms`);
  rag.closeRag();
});

test('実 rag.db + Ollama（存在する場合のみ）', { skip: !fs.existsSync(REAL_DB) }, async (t) => {
  const base = process.env.OLLAMA_HOST_URL || 'http://127.0.0.1:11434';
  try {
    await fetch(`${base}/api/version`, { signal: AbortSignal.timeout(2000) });
  } catch {
    t.skip(`Ollama (${base}) に接続できません`);
    return;
  }
  const info = await rag.initRag({ dbPath: REAL_DB, ollamaBase: base });
  assert.ok(info.count > 0);
  const t0 = performance.now();
  const hits = await rag.searchRag('急性心筋梗塞', { topK: 5, category: 'icd10' });
  console.log(`  ${(performance.now() - t0).toFixed(0)} ms (埋め込み込み)`);
  for (const h of hits) console.log(`  ${h.score.toFixed(3)} ${h.code} ${h.title}`);
  assert.ok(hits.some((h) => (h.code || '').startsWith('I21')));
  rag.closeRag();
});
