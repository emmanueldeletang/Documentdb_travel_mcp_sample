from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient
from pymongo.collection import Collection


def _normalize_question(question: str) -> str:
    return " ".join(question.strip().lower().split())


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class QuestionCache:
    def __init__(
        self,
        documentdb_uri: str,
        db_name: str = "traveldb",
        collection_name: str = "qa_history",
        similarity_threshold: float = 0.97,
        vector_candidates: int = 200,
    ):
        self._uri = documentdb_uri
        self._db_name = db_name
        self._collection_name = collection_name
        self._similarity_threshold = similarity_threshold
        self._vector_candidates = vector_candidates
        self._client: MongoClient | None = None
        self._collection: Collection | None = None

    def _get_collection(self) -> Collection:
        if self._collection is not None:
            return self._collection
        self._client = MongoClient(self._uri, serverSelectionTimeoutMS=5000)
        col = self._client[self._db_name][self._collection_name]
        col.create_index("normalized_question")
        col.create_index("created_at")
        # Create DiskANN vector index on question_vector for fast ANN search.
        # Wrapped in try/except so a pre-existing index or an older DocumentDB
        # version that doesn't yet support DiskANN won't break the application.
        try:
            col.database.command({
                "createIndexes": self._collection_name,
                "indexes": [{
                    "key": {"question_vector": "vector"},
                    "name": "qa_history_vector_diskann_idx",
                    "vectorOptions": {
                        "type": "diskann",
                        "dimensions": 1536,
                        "similarity": "cosine",
                    },
                }],
            })
        except Exception:
            pass  # index may already exist or DiskANN not supported on this cluster
        self._collection = col
        return col

    def find_match(
        self,
        question: str,
        question_vector: list[float] | None = None,
        similarity_threshold: float | None = None,
    ) -> dict[str, Any] | None:
        col = self._get_collection()
        normalized = _normalize_question(question)
        threshold = self._similarity_threshold if similarity_threshold is None else similarity_threshold

        exact = col.find_one({"normalized_question": normalized})
        if exact:
            return {
                "doc": exact,
                "match_type": "exact",
                "similarity": 1.0,
            }

        if not question_vector:
            return None

        cursor = col.find(
            {
                "question_vector": {"$type": "array"},
                "vector_dim": len(question_vector),
            },
            {
                "answer": 1,
                "trace": 1,
                "mode": 1,
                "llm_total_tokens": 1,
                "question": 1,
                "question_vector": 1,
            },
        ).sort("created_at", -1).limit(self._vector_candidates)

        best_doc: dict[str, Any] | None = None
        best_score = -1.0
        for doc in cursor:
            vec = doc.get("question_vector")
            if not isinstance(vec, list):
                continue
            score = _cosine_similarity(question_vector, vec)
            if score > best_score:
                best_score = score
                best_doc = doc

        if best_doc is None or best_score < threshold:
            return None
        return {
            "doc": best_doc,
            "match_type": "vector",
            "similarity": float(best_score),
        }

    def mark_cache_hit(self, doc_id: Any) -> None:
        col = self._get_collection()
        col.update_one(
            {"_id": doc_id},
            {
                "$set": {"last_hit_at": datetime.now(timezone.utc)},
                "$inc": {"cache_hits": 1},
            },
        )

    def store(
        self,
        question: str,
        answer: str,
        trace: list[dict[str, Any]],
        mode: str,
        question_vector: list[float] | None,
        llm_usage: dict[str, int],
        execution_ms: int,
    ) -> None:
        col = self._get_collection()
        normalized = _normalize_question(question)
        now = datetime.now(timezone.utc)
        total = int(llm_usage.get("total_tokens", 0))

        doc = {
            "question": question,
            "normalized_question": normalized,
            "answer": answer,
            "trace": trace,
            "mode": mode,
            "question_vector": question_vector or [],
            "vector_dim": len(question_vector or []),
            "llm_usage": {
                "prompt_tokens": int(llm_usage.get("prompt_tokens", 0)),
                "completion_tokens": int(llm_usage.get("completion_tokens", 0)),
                "total_tokens": total,
            },
            "llm_total_tokens": total,
            "execution_ms": int(execution_ms),
            "created_at": now,
            "last_hit_at": now,
            "cache_hits": 0,
        }

        col.update_one(
            {"normalized_question": normalized},
            {
                "$set": doc,
            },
            upsert=True,
        )

    def clear_all(self) -> dict[str, int]:
        col = self._get_collection()
        result = col.delete_many({})
        return {"deleted_count": int(result.deleted_count or 0)}
