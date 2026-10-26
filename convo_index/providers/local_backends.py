"""In-process embeddings through `fastembed` (ONNX, CPU): no server to install or keep
running. The model is fetched from Hugging Face on first use into `[embeddings].cache_dir`
and never leaves the machine afterwards. A core dependency; the import guard only covers a
broken or partial install."""
from __future__ import annotations

import importlib.util
import math
import threading
from pathlib import Path

from convo_index import config as C
from convo_index.providers.base import EmbedResult

# One loaded model per process: loading takes a second or two and the UI embeds a query per
# keystroke, so `make_embedder()` must stay cheap and `embed()` reuse the session.
_MODELS: dict[tuple[str, str], object] = {}
_LOCK = threading.Lock()


def installed():
    return importlib.util.find_spec("fastembed") is not None


class FastEmbed:
    name = "fastembed"

    def __init__(self, spec):
        self.spec = spec
        self.model = spec.get("model") or C.FASTEMBED_MODEL
        self.cache_dir = str(Path(spec.get("cache_dir") or C.EMBED_CACHE_DIR).expanduser())

    @staticmethod
    def available():
        return installed()

    @property
    def fingerprint(self):
        # "unit+task": the vectors are L2-normalised and the inputs carry the model's task
        # prefix. Either change would need a reembed, so both are part of the fingerprint.
        return f"{self.name}|{self.model}|dim={self.spec.get('dim')}|trunc={self.spec.get('max_chars')}|unit+task"

    # nomic-embed models are trained with a task prefix and fastembed does not add one; without
    # it queries and documents land in the wrong region of the space.
    PREFIXES = {"document": "search_document: ", "query": "search_query: "}

    def _prefix(self, purpose):
        return self.PREFIXES[purpose] if "nomic-embed" in self.model else ""

    def model_cached(self):
        """Is the model already on disk? Never downloads."""
        if not installed():
            return False
        from fastembed import TextEmbedding
        from loguru import logger   # fastembed logs a cache miss as ERROR; here it is the answer, not a fault
        logger.disable("fastembed")
        try:
            TextEmbedding(self.model, cache_dir=self.cache_dir, local_files_only=True, lazy_load=True)
            return True
        except Exception:
            return False
        finally:
            logger.enable("fastembed")

    def _load(self):
        key = (self.model, self.cache_dir)
        with _LOCK:
            if key not in _MODELS:
                from fastembed import TextEmbedding   # downloads on first use
                _MODELS[key] = TextEmbedding(self.model, cache_dir=self.cache_dir)
            return _MODELS[key]

    # ONNX pads every batch to its longest text, so attention memory is batch x longest^2: 32
    # transcript-sized messages at once took 13 GB and crawled. Texts are embedded longest
    # first in batches capped by total characters, so a transcript-sized one goes alone (2.4 GB peak)
    # and short ones still fill a batch.
    BATCH_CHARS = 8_000

    def _batches(self, texts, batch_size):
        order = sorted(range(len(texts)), key=lambda i: -len(texts[i]))
        batch, chars = [], 0
        for i in order:
            if batch and (len(batch) >= batch_size or chars + len(texts[i]) > self.BATCH_CHARS):
                yield batch
                batch, chars = [], 0
            batch.append(i); chars += len(texts[i])
        if batch:
            yield batch

    def embed(self, texts, batch_size=32, purpose="document") -> EmbedResult:
        """`purpose` is "document" for indexed text and "query" for a search string."""
        max_chars = int(self.spec.get("max_chars") or 8000)
        dim = int(self.spec.get("dim") or 0)
        prefix = self._prefix(purpose)
        texts = [prefix + (t or " ")[:max_chars] for t in texts]
        vectors: list = [None] * len(texts)
        try:
            model = self._load()
            for idx in self._batches(texts, batch_size):
                out = list(model.embed([texts[i] for i in idx], batch_size=len(idx)))
                if len(out) != len(idx):
                    raise RuntimeError(f"got {len(out)} vectors for {len(idx)} inputs")
                for i, v in zip(idx, out, strict=False):
                    vectors[i] = _unit(v.tolist())   # the vec0 tables use L2 distance: unit length
        except Exception as e:
            return EmbedResult(vectors=[None] * len(texts), fingerprint=self.fingerprint,
                               error="provider", detail=f"{type(e).__name__}: {e}"[:500])
        if dim and vectors and vectors[0] is not None and len(vectors[0]) != dim:
            return EmbedResult(vectors=[None] * len(texts), fingerprint=self.fingerprint,
                               error="config", detail=f"model returned {len(vectors[0])} dims, config says {dim}")
        return EmbedResult(vectors=vectors, fingerprint=self.fingerprint)


def _unit(v):
    """L2-normalised copy, or None for a zero or non-finite vector (nothing to rank by)."""
    n = math.sqrt(sum(x * x for x in v))
    if not math.isfinite(n) or n == 0.0:
        return None
    return [x / n for x in v]
