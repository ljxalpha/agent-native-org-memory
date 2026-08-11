"""On-disk cache for embedding vectors (DISTILL-001, §7.2).

Two properties define it.

**Only vectors are stored.** The record text is hashed into the key and then
dropped. Keeping it would create a second place leaf memories land on disk --
one the output scanner's write-time gate does not cover, and one that outlives
the run that produced it.

**The backend and the model are part of the key.** The test stand-in produces
three-dimensional word counts and ``fastembed`` produces real semantic vectors;
a cache that let the first answer a lookup made by the second would cluster
meaningless vectors with DBSCAN parameters tuned for meaning, and every metric
about that run would look ordinary.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path

LOGGER = logging.getLogger(__name__)


class EmbeddingCache:
    """A content-addressed store of vectors, keyed by backend, model and text.

    Deliberately not an LRU and not size-bounded: entries are tiny, immutable
    and safe to delete by hand. A cache that evicts has to decide *what* to
    evict, and getting that wrong silently costs the thing the cache exists to
    avoid.
    """

    def __init__(self, root: Path, *, backend: str, model_name: str) -> None:
        self.root = root
        self.backend = backend
        self.model_name = model_name

    def key(self, text: str) -> str:
        """``sha256(backend, model, sha256(text))`` -- the text never survives.

        The inner digest is what makes this a *content* address; the outer one
        binds it to the policy that produced the vector, so changing either the
        backend or the model necessarily misses rather than returning a vector
        from a different vector space.
        """
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        material = f"{self.backend}\x00{self.model_name}\x00{digest}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def get(self, text: str) -> list[float] | None:
        """The cached vector, or ``None`` for a miss *or* a damaged entry.

        A corrupt entry is a miss, not an error: the vector can always be
        recomputed, and refusing to run because a cache file was truncated would
        turn an optimisation into a new way for the run to fail.
        """
        path = self._path(text)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        vector = payload.get("vector") if isinstance(payload, dict) else None
        if not isinstance(vector, list) or not vector:
            return None
        result: list[float] = []
        for item in vector:
            if isinstance(item, bool) or not isinstance(item, int | float):
                return None
            result.append(float(item))
        return result

    def put(self, text: str, vector: list[float]) -> None:
        """Store one vector. A cache that cannot be written is not fatal."""
        path = self._path(text)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"vector": [float(value) for value in vector]}),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - depends on the filesystem
            LOGGER.warning("embedding cache write failed: %s", type(exc).__name__)

    def resolve(
        self,
        texts: list[str],
        compute: Callable[[list[str]], list[list[float]]],
    ) -> list[list[float]]:
        """Vectors for ``texts``, computing only the ones not already cached.

        ``compute`` is called once, with the misses in order, so a backend that
        batches keeps batching. It is not called at all when everything hits,
        which is the case that matters: loading a real embedding model is the
        expensive part.
        """
        cached: list[list[float] | None] = [self.get(text) for text in texts]
        missing = [text for text, hit in zip(texts, cached, strict=True) if hit is None]
        if not missing:
            return [vector for vector in cached if vector is not None]
        computed = compute(missing)
        if len(computed) != len(missing):  # pragma: no cover - defensive
            raise ValueError("embedding backend returned the wrong number of vectors")
        for text, vector in zip(missing, computed, strict=True):
            self.put(text, vector)
        fresh = iter(computed)
        return [vector if vector is not None else next(fresh) for vector in cached]

    def _path(self, text: str) -> Path:
        key = self.key(text)
        # Two levels of fan-out: a flat directory of tens of thousands of files
        # is slow to list on every filesystem this runs on.
        return self.root / key[:2] / f"{key}.json"
