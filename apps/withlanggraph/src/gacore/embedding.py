"""Sentence embedding for memory-maintenance vector retrieval (阶段二).

Wraps a local, offline bilingual embedding model (bge-small-zh by default) so the
memory trigger can recall related portrait lines by *semantic* similarity — not just
keyword overlap. A later message like "朝阳那边的新家" that shares no literal keyword
with the portrait line "家住朝阳区" still matches because their vectors are close.

Design constraints:

- **Lazy, cached model**: model loading is expensive (millions of params). The first
  ``encode`` call builds and caches the SentenceTransformer on a thread-safe lock; the
  judge/trigger code never pays for it unless a vector trigger is actually configured.
- **Offline after first fetch**: the model weights are downloaded once from HuggingFace
  (bge-small-zh, ~100 MB) then served from the local cache. No network at runtime.
- **Optional dependency**: ``sentence-transformers`` lives in the ``vector`` extra. If it
  is not installed we degrade to a clear ``EmbeddingUnavailable`` error rather than an
  import crash, so core non-vector code paths keep working.
"""

from __future__ import annotations

import threading
from typing import Final

from gacore.config import Config
from gacore.jsonl_logger import get_logger

_logger = get_logger("embedding")

# Chinese-capable bilingual model, small enough for local CPU inference and strong for
# sentence-level semantic matching. bge family uses cosine similarity, aligning with our
# pgvector `<=>` operator. Points at a LOCAL copy of the weights (downloaded once from
# hf-mirror, kept at a fixed path) so first load needs no network. Loading a local dir
# also avoids the heavy HF hub/registry startup that hung on the office network.
DEFAULT_MODEL_NAME: Final = r"D:\models\bge-small-zh-v1.5"

_THREAD_LOCK = threading.Lock()
_model = None
_model_name_loaded: str | None = None


class EmbeddingUnavailable(RuntimeError):
    """Raised when sentence-transformers is missing or the model cannot be loaded."""


def _load_model(model_name: str):
    global _model, _model_name_loaded
    # re-import here so the dependency is truly optional (module import must not fail
    # when vector extra is absent)
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover - hard to trigger under test controls
        raise EmbeddingUnavailable(
            "sentence-transformers is not installed; add the gacore 'vector' extra "
            "to enable semantic memory retrieval"
        ) from exc
    with _THREAD_LOCK:
        if _model is None or _model_name_loaded != model_name:
            _logger.info("loading embedding model", model=model_name)
            _model = SentenceTransformer(model_name)
            _model_name_loaded = model_name
    return _model


def encode(text: str, cfg: Config | None = None, model_name: str = DEFAULT_MODEL_NAME) -> list[float]:
    """Return a normalized (L2) sentence-embedding vector for ``text``.

    A short/blank text yields the empty vector — the caller decides how to treat it.
    Errors during load/run are surfaced as ``EmbeddingUnavailable`` so the vector trigger
    can degrade to keyword mode instead of crashing the turn.
    """
    text = (text or "").strip()
    if not text:
        return []
    try:
        model = _load_model(model_name)
        # np.float32 -> native list of floats for pgvector / JSON serialization
        emb = model.encode([text], normalize_embeddings=True)[0]
        return [float(x) for x in emb]
    except EmbeddingUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — mirror the load error to the caller
        _logger.error("embed encode failed", error_type=type(exc).__name__, error=str(exc))
        raise EmbeddingUnavailable(str(exc)) from exc


def batch_encode(texts: list[str], cfg: Config | None = None, model_name: str = DEFAULT_MODEL_NAME) -> list[list[float]]:
    """Return L2-normalized vectors for a batch of texts (model invoked once)."""
    clean = [t.strip() for t in texts]
    if not any(clean):
        return []
    try:
        model = _load_model(model_name)
        embs = model.encode(clean, normalize_embeddings=True)
        return [[float(x) for x in row] for row in embs]
    except Exception as exc:  # noqa: BLE001
        _logger.error("embed batch failed", error_type=type(exc).__name__, error=str(exc))
        raise EmbeddingUnavailable(str(exc)) from exc


def dimension(model_name: str = DEFAULT_MODEL_NAME) -> int:
    """Return the embedding dimension of ``model_name`` (bge-small-zh-v1.5: 512)."""
    try:
        model = _load_model(model_name)
        return model.get_sentence_embedding_dimension()
    except Exception:  # noqa: BLE001
        # fall back to the known bge-small dimension; do not crash dimension checks
        return 512


__all__ = ("DEFAULT_MODEL_NAME", "EmbeddingUnavailable", "batch_encode", "dimension", "encode")