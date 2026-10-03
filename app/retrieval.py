"""Deterministic retrieval over the handbook, chunked by section: BM25 fused
with a small local embedding model (reciprocal rank fusion). Deterministic on
purpose -- only the classify/draft/propose nodes call an LLM; retrieval and
grounding are plain code (see ARCHITECTURE.md).

Why hybrid: plain BM25 missed the right section for 2 of the 23 labeled
requests (a JetBrains license request never reached "Software License
Requests"). Stopwords fixed one; the embedding model fixed the other.
RETRIEVAL_MODE=bm25 skips the embedding model (no download); RETRIEVAL_MODE=full
skips retrieval and sends the whole handbook (see the README's comparison)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rank_bm25 import BM25Okapi

from . import config

_WORD_RE = re.compile(r"[a-z0-9]+")
RRF_K = 60  # the usual constant; ranks are fused, not raw scores
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

STOPWORDS = frozenset("""a an the and or but if then than so to of in on at by for with from as is are was were be been
being it its this that these those you your yours i me my we our they them their he she his her not no do does did done
can could should would will may might must have has had there here what which who whom when where why how all any each
more most other some such only own same too very just also into about over under up down out off again further once both
few nor per via please thanks""".split())


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _terms(text: str) -> list[str]:
    return [_stem(w) for w in _tokenize(text) if w not in STOPWORDS]


@dataclass
class Chunk:
    chunk_id: str
    section: str
    text: str


class HandbookIndex:
    def __init__(self, handbook_path: Path | None = None, mode: str | None = None):
        path = handbook_path or config.HANDBOOK_PATH
        self.chunks = self._split_sections(path.read_text())
        self._bm25 = BM25Okapi([_terms(c.text) for c in self.chunks])
        self.mode = mode or config.RETRIEVAL_MODE
        self._embedder = None
        if self.mode == "hybrid":
            import numpy as np
            from fastembed import TextEmbedding

            self._embedder = TextEmbedding(EMBEDDING_MODEL)
            self._doc_vecs = np.array(list(self._embedder.embed([c.text for c in self.chunks])))

    @staticmethod
    def _split_sections(raw: str) -> list[Chunk]:
        parts = re.split(r"(?m)^## ", raw)
        chunks: list[Chunk] = []
        preamble = parts[0].strip()
        if preamble:
            chunks.append(Chunk(chunk_id="preamble", section="Preamble", text=preamble))
        for i, part in enumerate(parts[1:], start=1):
            heading, _, body = part.partition("\n")
            heading = heading.strip()
            body = body.split("\n---", 1)[0].strip()
            chunks.append(Chunk(chunk_id=f"sec-{i}", section=heading, text=f"## {heading}\n{body}"))
        return chunks

    def retrieve(self, query: str, k: int | None = None) -> list[dict]:
        if self.mode == "full":  # every section; the graph sends them as one cached system block
            return [{"chunk_id": c.chunk_id, "section": c.section, "text": c.text, "score": 0.0, "bm25": 0.0}
                    for c in self.chunks]
        k = k or config.RETRIEVAL_TOP_K
        n = len(self.chunks)
        bm25 = self._bm25.get_scores(_terms(query))
        bm25_rank = {i: r for r, i in enumerate(sorted(range(n), key=lambda i: bm25[i], reverse=True))}
        if self._embedder is None:
            fused = {i: float(bm25[i]) for i in range(n)}
        else:
            sims = self._doc_vecs @ next(iter(self._embedder.query_embed([query])))
            emb_rank = {i: r for r, i in enumerate(sorted(range(n), key=lambda i: sims[i], reverse=True))}
            fused = {i: 1 / (RRF_K + bm25_rank[i]) + 1 / (RRF_K + emb_rank[i]) for i in range(n)}
        ranked = sorted(range(n), key=lambda i: fused[i], reverse=True)[:k]
        return [
            {
                "chunk_id": self.chunks[i].chunk_id,
                "section": self.chunks[i].section,
                "text": self.chunks[i].text,
                "score": round(float(fused[i]), 4),
                "bm25": round(float(bm25[i]), 2),
            }
            for i in ranked
        ]

    def section_for_chunk(self, chunk_id: str) -> str | None:
        for c in self.chunks:
            if c.chunk_id == chunk_id:
                return c.section
        return None
