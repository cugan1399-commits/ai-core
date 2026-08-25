"""
Обёртка над локальной моделью эмбеддингов.

Не знает ничего про Bitrix, каталоги или конкретные фичи — просто превращает
текст в вектор. Используется и seller-модулем (RAG по каталогу/КБ), и потенциально
будущими не-Bitrix модулями, которым тоже нужен смысловой поиск по тексту.

Модель грузится один раз (лениво, при первом вызове) и держится в памяти процесса —
она нужна не всем модулям, поэтому не загружается на старте main.py/воркера.
"""
from __future__ import annotations

from functools import lru_cache

from config import EMBEDDING_MODEL_NAME


@lru_cache(maxsize=1)
def _get_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBEDDING_MODEL_NAME)


def embed_text(text: str) -> list[float]:
    """Возвращает эмбеддинг одного текста как список float (для записи в pgvector)."""
    model = _get_model()
    vector = model.encode(text, normalize_embeddings=True)
    return vector.tolist()


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Батч-версия — дешевле по CPU, чем вызывать embed_text() в цикле."""
    if not texts:
        return []
    model = _get_model()
    vectors = model.encode(texts, normalize_embeddings=True)
    return vectors.tolist()
