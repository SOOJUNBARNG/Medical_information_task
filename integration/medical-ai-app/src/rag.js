// medical-rag-db 検索モジュール（Electron メインプロセス用）
//
// rag.db（事前埋め込み済み SQLite）を読み取り専用で開き、クエリだけを
// Ollama の nomic-embed-text で埋め込んでコサイン類似度で上位K件を返す。
// 実行時の通信はローカルの Ollama（127.0.0.1）のみ。
//
// メモリ対策: 埋め込みは正規化後に int8 量子化して保持する（115,000件 × 768次元で約88MB。
// Float32 のままだと約350MB）。量子化スコアで候補を絞り、上位候補だけ
// DB の Float32 BLOB で正確なコサイン類似度を再計算する。
'use strict';

const Database = require('better-sqlite3');

const CATEGORY_IDS = { icd10: 0, dpc: 1, pmda: 2, formulary: 3 };
const LOAD_PAGE = 5000;
const RERANK_MIN = 50;
const ICD_CODE = /\b([A-Z]\d{2}(?:\.\d{1,2}[a-z]?)?)\b/g;
const DPC_CODE = /\b(\d{6}[0-9x]{8})\b/g;

let state = null;

/**
 * rag.db を開いて検索インデックスを読み込む。アプリ起動時に1回呼ぶ。
 * 読み込み中もイベントループを止めないよう、ページ単位で setImmediate を挟む。
 *
 * @param {object} opts
 * @param {string} opts.dbPath      rag.db のパス
 * @param {string} [opts.ollamaBase] 例: 'http://127.0.0.1:11435'
 * @param {(text: string) => Promise<Float32Array|number[]>} [opts.embedQuery] テスト用の差し替え
 */
async function initRag({ dbPath, ollamaBase = 'http://127.0.0.1:11435', embedQuery = null } = {}) {
  const db = new Database(dbPath, { readonly: true, fileMustExist: true });
  const meta = Object.fromEntries(db.prepare('SELECT key, value FROM meta').all().map((r) => [r.key, r.value]));
  const dim = Number(meta.embed_dim || 768);
  const total = db.prepare('SELECT COUNT(*) AS n FROM chunks').get().n;

  const ids = new Int32Array(total);
  const cats = new Uint8Array(total);
  const scales = new Float32Array(total);
  const q8 = new Int8Array(total * dim);
  const page = db.prepare('SELECT id, category, embedding FROM chunks WHERE id > ? ORDER BY id LIMIT ?');

  let n = 0;
  let lastId = 0;
  for (;;) {
    const rows = page.all(lastId, LOAD_PAGE);
    if (rows.length === 0) break;
    for (const r of rows) {
      const v = blobToFloat32(r.embedding, dim);
      let norm = 0;
      for (let j = 0; j < dim; j++) norm += v[j] * v[j];
      norm = Math.sqrt(norm) || 1;
      let maxAbs = 0;
      for (let j = 0; j < dim; j++) {
        const a = Math.abs(v[j] / norm);
        if (a > maxAbs) maxAbs = a;
      }
      const s = maxAbs / 127 || 1;
      const off = n * dim;
      for (let j = 0; j < dim; j++) q8[off + j] = Math.round(v[j] / norm / s);
      scales[n] = s;
      ids[n] = r.id;
      cats[n] = CATEGORY_IDS[r.category] ?? 255;
      n++;
      lastId = r.id;
    }
    await new Promise(setImmediate);
  }

  // 院内医薬品集の薬品名・成分名・先発品名（長い語から照合する）
  let keywords = [];
  try {
    keywords = db.prepare('SELECT word, chunk_id AS id FROM keywords').all().sort((a, b) => b.word.length - a.word.length);
  } catch {
    // 古い rag.db には keywords テーブルが無い
  }

  state = {
    db,
    keywords,
    meta,
    dim,
    count: n,
    ids,
    cats,
    scales,
    q8,
    ollamaBase,
    embedQuery: embedQuery || ((text) => ollamaEmbed(ollamaBase, meta.embed_model || 'nomic-embed-text', (meta.query_prefix || '') + text)),
    stmtById: db.prepare('SELECT id, category, code, title, section, body, embedding FROM chunks WHERE id = ?'),
    stmtByCode: db.prepare('SELECT id, category, code, title, section, body FROM chunks WHERE code = ? LIMIT 3'),
    stmtMeta: db.prepare('SELECT id, category, code, title, section, body FROM chunks WHERE id = ?'),
  };
  return { count: n, dim, model: meta.embed_model, builtAt: meta.built_at };
}

function blobToFloat32(buf, dim) {
  // Buffer はプール上のオフセットを持つことがあるため、4バイト境界でなければコピーする
  if (buf.byteOffset % 4 === 0) return new Float32Array(buf.buffer, buf.byteOffset, dim);
  return new Float32Array(Uint8Array.from(buf).buffer, 0, dim);
}

async function ollamaEmbed(base, model, prompt) {
  const res = await fetch(`${base}/api/embeddings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model, prompt }),
    signal: AbortSignal.timeout(30000),
  });
  if (!res.ok) throw new Error(`埋め込みモデルの呼び出しに失敗しました (${res.status})`);
  const json = await res.json();
  if (!Array.isArray(json.embedding)) throw new Error(json.error || '埋め込み結果が不正です');
  return json.embedding;
}

/**
 * 返値: [{ id, category, code, title, section, body, score }]
 * @param {string} queryText
 * @param {{ topK?: number, category?: 'icd10'|'dpc'|'pmda'|null, minScore?: number }} [options]
 */
async function searchRag(queryText, options = {}) {
  if (!state) throw new Error('initRag() が呼ばれていません');
  const { topK = 5, category = null, minScore = 0 } = options;
  const qRaw = await state.embedQuery(queryText);
  if (qRaw.length !== state.dim) throw new Error(`クエリ埋め込みの次元が ${qRaw.length}（期待値 ${state.dim}）`);
  return searchByVector(Float32Array.from(qRaw), { topK, category, minScore, queryText });
}

/** 埋め込み済みベクトルで検索する（searchRag の後半。性能測定・テスト用に公開） */
function searchByVector(qvec, { topK = 5, category = null, minScore = 0, queryText = '' } = {}) {
  const { dim, count, q8, scales, cats, ids } = state;
  let norm = 0;
  for (let j = 0; j < dim; j++) norm += qvec[j] * qvec[j];
  norm = Math.sqrt(norm) || 1;
  const q = new Float32Array(dim);
  for (let j = 0; j < dim; j++) q[j] = qvec[j] / norm;

  const catId = category == null ? -1 : CATEGORY_IDS[category];
  if (category != null && catId === undefined) throw new Error(`不明なカテゴリ: ${category}`);

  // 1) int8 近似スコアで候補を絞る
  const nCand = Math.max(topK * 8, RERANK_MIN);
  const candIdx = new Int32Array(nCand).fill(-1);
  const candScore = new Float32Array(nCand).fill(-Infinity);
  let worst = 0; // candScore 内の最小値の位置
  const dim4 = dim - (dim % 4);
  for (let i = 0; i < count; i++) {
    if (catId >= 0 && cats[i] !== catId) continue;
    const off = i * dim;
    // 4本のアキュムレータに展開すると V8 で約2倍速い
    let a = 0, b = 0, c = 0, d = 0;
    for (let j = 0; j < dim4; j += 4) {
      a += q[j] * q8[off + j];
      b += q[j + 1] * q8[off + j + 1];
      c += q[j + 2] * q8[off + j + 2];
      d += q[j + 3] * q8[off + j + 3];
    }
    for (let j = dim4; j < dim; j++) a += q[j] * q8[off + j];
    const s = (a + b + c + d) * scales[i];
    if (s > candScore[worst]) {
      candScore[worst] = s;
      candIdx[worst] = i;
      for (let k = 0; k < nCand; k++) if (candScore[k] < candScore[worst]) worst = k;
    }
  }

  // 2) 候補を Float32 で正確に再スコア
  const results = [];
  for (let k = 0; k < nCand; k++) {
    if (candIdx[k] < 0) continue;
    const row = state.stmtById.get(ids[candIdx[k]]);
    const v = blobToFloat32(row.embedding, dim);
    let dot = 0;
    let vn = 0;
    for (let j = 0; j < dim; j++) {
      dot += q[j] * v[j];
      vn += v[j] * v[j];
    }
    const score = dot / (Math.sqrt(vn) || 1);
    if (score < minScore) continue;
    const { embedding, ...rest } = row;
    results.push({ ...rest, score });
  }
  results.sort((a, b) => b.score - a.score);

  // 3) 質問に薬品名・成分名・先発品名や ICD-10 / DPC コードがそのまま書かれていれば、そのチャンクを先頭に置く
  const cat = catId >= 0 ? category : null;
  const exact = [...keywordHits(queryText, cat), ...exactCodeHits(queryText, cat)];
  const seen = new Set(exact.map((r) => r.id));
  return [...exact, ...results.filter((r) => !seen.has(r.id))].slice(0, topK);
}

function keywordHits(text, category) {
  if (!text) return [];
  const hits = [];
  const seen = new Set();
  for (const { word, id } of state.keywords) {
    if (seen.has(id) || !text.includes(word)) continue;
    seen.add(id);
    const row = state.stmtMeta.get(id);
    if (row && (!category || row.category === category)) hits.push({ ...row, score: 1 });
  }
  return hits;
}

function exactCodeHits(text, category) {
  if (!text) return [];
  const codes = new Set([...text.matchAll(ICD_CODE)].map((m) => m[1]).concat([...text.matchAll(DPC_CODE)].map((m) => m[1])));
  const hits = [];
  for (const code of codes) {
    for (const row of state.stmtByCode.all(code)) {
      if (!category || row.category === category) hits.push({ ...row, score: 1 });
    }
  }
  return hits;
}

const CATEGORY_LABEL = { icd10: 'ICD-10', dpc: 'DPC', pmda: '添付文書', formulary: '院内医薬品集' };

// Ollamaプロンプトにコンテキストを挿入
function buildRagPrompt(userQuery, ragResults) {
  if (!ragResults || ragResults.length === 0) return userQuery;
  const context = ragResults
    .map((r) => {
      const label = [CATEGORY_LABEL[r.category] || r.category, r.code, r.title, r.section].filter(Boolean).join(' ');
      return `[参考: ${label}] ${r.body}`;
    })
    .join('\n\n');
  return (
    `以下の医療情報を参考にユーザーの質問に答えてください。` +
    `参考情報に書かれていない用量・禁忌・副作用などは推測で答えず、「院内の参考情報には記載がありません」と伝えてください。` +
    `\n\n${context}\n\n質問: ${userQuery}`
  );
}

/** 出典表示用（PMDA は帰属表示が必要） */
function getAttribution() {
  if (!state) return {};
  try {
    return JSON.parse(state.meta.attribution || '{}');
  } catch {
    return {};
  }
}

function closeRag() {
  if (state) state.db.close();
  state = null;
}

module.exports = { initRag, searchRag, searchByVector, buildRagPrompt, getAttribution, closeRag };
