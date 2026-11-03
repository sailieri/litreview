"""
Embedding module: chunk text + compute vectors via external API.

Chunking strategy:
  - Target chunk size: ~500 tokens (~2000 chars)
  - Split on paragraph boundaries where possible
  - Each chunk has overlap of ~1 sentence for context continuity
"""

import math
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# 配置统一由 modules/config.py 读取（本模块不直接解析 yaml）
from modules.config import section as _cfg_section  # noqa: E402

_embed_cfg = _cfg_section("embedding")

EMBED_URL = str(_embed_cfg.get("base_url") or "").rstrip("/")
if EMBED_URL:
    EMBED_URL += "/api/embed"

# 候选链：env LITREVIEW_EMBED_URL > config.embedding.base_url > config.embedding.candidates。
# 命中过的端点会被缓存复用，单个端点不可达不至于让整库向量化失败。
EMBED_CANDIDATES: list[str] = []
_cands = [os.environ.get("LITREVIEW_EMBED_URL", "").rstrip("/"),
          (EMBED_URL or "").replace("/api/embed", "")]
_cands += [str(u).rstrip("/") for u in (_embed_cfg.get("candidates") or [])]
for _u in _cands:
    if _u and _u not in EMBED_CANDIDATES:
        EMBED_CANDIDATES.append(_u)
_working_url: str | None = None
EMBED_MODEL = _embed_cfg.get("model", "")
EMBED_DIM = _embed_cfg.get("dim", 1024)
EMBED_API_KEY = _embed_cfg.get("api_key", "")
CHUNK_TARGET_CHARS = 2000
CHUNK_OVERLAP_CHARS = 200


def chunk_text(text: str, target_chars: int = CHUNK_TARGET_CHARS,
               overlap_chars: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    """Split text into overlapping chunks on paragraph boundaries.

    Args:
        text: Input text to chunk.
        target_chars: Target size per chunk.
        overlap_chars: Overlap between consecutive chunks.

    Returns:
        List of chunk strings.
    """
    if len(text) <= target_chars:
        return [text]

    # Split on double newlines (paragraphs)
    paragraphs = re.split(r'\n\s*\n', text)
    chunks = []
    current = []
    current_len = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # If a single paragraph is longer than target, split it by sentences
        if len(para) > target_chars:
            # Flush current buffer first
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0

            # Split long paragraph by sentences
            sentences = re.split(r'(?<=[.!?])\s+', para)
            buf = []
            buf_len = 0
            for sent in sentences:
                if buf_len + len(sent) > target_chars and buf:
                    chunks.append(" ".join(buf))
                    # Keep last sentence for overlap
                    overlap = buf[-1] if buf else ""
                    buf = [overlap] if overlap else []
                    buf_len = len(overlap)
                buf.append(sent)
                buf_len += len(sent)
            if buf:
                chunks.append(" ".join(buf))
            continue

        if current_len + len(para) > target_chars and current:
            chunks.append("\n\n".join(current))
            # Keep last elements for overlap
            overlap_text = current[-1] if current else ""
            current = [overlap_text] if len(overlap_text) < overlap_chars * 3 else []
            current_len = sum(len(p) for p in current)

        current.append(para)
        current_len += len(para)

    if current:
        chunks.append("\n\n".join(current))

    return [c for c in chunks if c.strip()]


def compute_embedding(text: str) -> list[float] | None:
    """Compute embedding vector for a text string via the configured embedding endpoint.

    Returns:
        List of 1024 floats, or None on failure.
    """
    import httpx
    global _working_url
    order = ([_working_url] if _working_url else []) + [u for u in EMBED_CANDIDATES if u != _working_url]
    last_err = None
    for base in order:
        try:
            # trust_env=False：不读系统代理，避免请求被本地代理劫持
            with httpx.Client(timeout=30, trust_env=False) as cl:
                resp = cl.post(
                    f"{base}/api/embed",
                    json={"model": EMBED_MODEL, "input": [text]},
                    headers={"Authorization": f"Bearer {EMBED_API_KEY}"},
                )
                resp.raise_for_status()
                embeddings = resp.json().get("embeddings", [])
                if embeddings:
                    _working_url = base
                    return embeddings[0]
        except Exception as e:
            last_err = e
            continue
    if last_err is not None:
        print(f"  [embed] Error: 全部候选端点失败（{', '.join(order)}），最后错误：{last_err}", file=sys.stderr)
    return None


def vector_to_blob(vector: list[float]) -> bytes:
    """Convert float32 vector to BLOB for SQLite storage."""
    return struct.pack(f"{len(vector)}f", *vector)


def blob_to_vector(blob: bytes) -> list[float]:
    """Convert BLOB back to float32 vector."""
    return list(struct.unpack(f"{len(blob)//4}f", blob))


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = math.sqrt(sum(a * a for a in v1))
    n2 = math.sqrt(sum(b * b for b in v2))
    if n1 == 0 or n2 == 0:
        return 0.0
    return dot / (n1 * n2)


def embed_chunks_async(text: str, db_path: str, chunk_ids: list[int]):
    """Compute embeddings for chunks and store them. Runs as subprocess."""
    import sqlite3
    import json as _json

    chunks = chunk_text(text)

    conn = sqlite3.connect(db_path)
    for cid in chunk_ids:
        row = conn.execute("SELECT content FROM chunks WHERE id=?", (cid,)).fetchone()
        if not row:
            continue
        vec = compute_embedding(row[0])
        if vec:
            blob = vector_to_blob(vec)
            conn.execute("UPDATE chunks SET vector=? WHERE id=?", (blob, cid))
    conn.commit()
    conn.close()
