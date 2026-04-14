"""
backend/app/processing/embeddings/sentence_transformer.py

Wrapper de SentenceTransformer para embeddings semanticos
Integrado con el sistema de procesamiento de documentos
"""

from __future__ import annotations

import logging
import os
import json
import hashlib
import threading
from typing import List, Union, Optional
import numpy as np
import redis

from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


class EmbeddingGenerator:
    """
    Generador de embeddings usando SentenceTransformer.
    
    Caracteristicas:
    - Normalizacion automatica para cosine similarity
    - Batch processing eficiente
    - Cache de modelo en memoria
    - Forzado a CPU (sin CUDA) para evitar errores en contenedor
    """

    def __init__(
        self,
        model_name: str = "paraphrase-multilingual-MiniLM-L12-v2",
        device: Optional[str] = None,
        normalize: bool = True,
        batch_size: int = 32,
    ):
        """
        Args:
            model_name: Nombre del modelo de SentenceTransformers
            device: ignorado, siempre usamos "cpu" en este backend
            normalize: Si True, normaliza vectores para cosine similarity
            batch_size: Tamano de batch para encoding
        """
        # IMPORTANTE: para el backend forzamos siempre CPU.
        # Nos quitamos de historias con versiones de CUDA / GPU en el contenedor.
        self.model_name = model_name
        self.device = "cpu"
        self.normalize = normalize
        self.batch_size = batch_size

        logger.info(f"Cargando SentenceTransformer: {model_name} (device=cpu)")

        # Forzamos el modelo a CPU
        self.model = SentenceTransformer(model_name, device="cpu")
        try:
            # Por si acaso, nos aseguramos otra vez
            self.model = self.model.to("cpu")  # type: ignore[attr-defined]
        except Exception:
            # Algunos wrappers no tienen .to(); si falla, no pasa nada
            pass

        self.vector_dim = self.model.get_sentence_embedding_dimension()

        # Configurar Redis cache
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        try:
            self.redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
            self.redis_client.ping()
            self.use_cache = True
            logger.info(f"✅ Caché Redis conectada en {redis_url} para embeddings")
        except Exception as e:
            self.use_cache = False
            self.redis_client = None
            logger.warning(f"⚠️ No se pudo conectar a Redis en {redis_url}, caché deshabilitada: {e}")

        logger.info(f"✅ Modelo cargado (dim={self.vector_dim})")

    def encode(
        self,
        texts: Union[str, List[str]],
        show_progress: bool = False,
    ) -> np.ndarray:
        """
        Genera embeddings para uno o mas textos.

        Args:
            texts: Texto o lista de textos
            show_progress: Mostrar barra de progreso

        Returns:
            Array numpy (N, dim) con vectores normalizados
        """
        if isinstance(texts, str):
            texts = [texts]

        if not texts:
            return np.array([])
            
        vectors = []
        indices_to_compute = []
        
        if self.use_cache:
            for idx, text in enumerate(texts):
                cache_key = f"emb:{self.model_name}:{hashlib.md5(text.encode('utf-8')).hexdigest()}"
                cached_vec = None
                try:
                    raw = self.redis_client.get(cache_key)
                    if raw:
                        cached_vec = json.loads(raw)
                except Exception as redis_err:
                    logger.debug(f"Redis get fallado (bypass silencioso): {redis_err}")

                if cached_vec is not None:
                    vectors.append(cached_vec)
                else:
                    vectors.append(None)  # Placeholder
                    indices_to_compute.append((idx, text, cache_key))
        else:
            indices_to_compute = [(idx, text, None) for idx, text in enumerate(texts)]
            vectors = [None] * len(texts)
            
        if indices_to_compute:
            texts_to_compute = [t for _, t, _ in indices_to_compute]
            new_vectors = self.model.encode(
                texts_to_compute,
                batch_size=self.batch_size,
                show_progress_bar=show_progress,
                convert_to_numpy=True,
                normalize_embeddings=self.normalize,
                device="cpu",  # reforzamos CPU tambien aqui
            )
            
            for i, (orig_idx, _, cache_key) in enumerate(indices_to_compute):
                vec = new_vectors[i].tolist()
                vectors[orig_idx] = vec
                if self.use_cache and cache_key:
                    try:
                        # Guardar en caché por 24 horas (86400 segundos)
                        self.redis_client.setex(cache_key, 86400, json.dumps(vec))
                    except Exception as redis_err:
                        logger.debug(f"Redis setex fallado (bypass silencioso): {redis_err}")
                    
        return np.array(vectors)

    def encode_batch(
        self,
        texts: List[str],
        batch_size: Optional[int] = None,
    ) -> List[List[float]]:
        """
        Encoding batch con control de tamano.
        Retorna lista de listas (compatible con Qdrant).
        """
        vectors = self.encode(
            texts,
            show_progress=len(texts) > 100,
        )
        return vectors.tolist()

    def similarity(
        self,
        text1: Union[str, List[float]],
        text2: Union[str, List[float]],
    ) -> float:
        """
        Calcula similaridad coseno entre dos textos o vectores.

        Args:
            text1: Texto o vector
            text2: Texto o vector

        Returns:
            Score de similaridad [0, 1]
        """
        if isinstance(text1, str):
            vec1 = self.encode([text1])[0]
        else:
            vec1 = np.array(text1)

        if isinstance(text2, str):
            vec2 = self.encode([text2])[0]
        else:
            vec2 = np.array(text2)

        # Cosine similarity (ya normalizados)
        if self.normalize:
            return float(np.dot(vec1, vec2))
        else:
            return float(
                np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2))
            )

    @property
    def dimension(self) -> int:
        """Dimension de los vectores generados"""
        return self.vector_dim


# ============================================
# Instancia global (singleton thread-safe)
# ============================================

_global_embedder: Optional[EmbeddingGenerator] = None
_embedder_lock = threading.Lock()

_global_sparse_embedder = None
_sparse_embedder_unavailable = False
_sparse_embedder_lock = threading.Lock()


def get_embedder(
    model_name: Optional[str] = None,
    force_reload: bool = False,
) -> EmbeddingGenerator:
    """
    Obtiene instancia global del embedder (singleton thread-safe).

    Args:
        model_name: Nombre del modelo (si None, usa default)
        force_reload: Fuerza recarga del modelo

    Returns:
        EmbeddingGenerator configurado
    """
    global _global_embedder

    if _global_embedder is None or force_reload:
        with _embedder_lock:
            # Double-checked locking: re-verificar dentro del lock
            if _global_embedder is None or force_reload:
                model = model_name or os.getenv(
                    "EMBEDDING_MODEL",
                    "paraphrase-multilingual-MiniLM-L12-v2"
                )
                _global_embedder = EmbeddingGenerator(model_name=model)

    return _global_embedder


def get_sparse_embedder():
    """Obtiene instancia global del embedder disperso BM25 (thread-safe)."""
    global _global_sparse_embedder, _sparse_embedder_unavailable

    if _global_sparse_embedder is None and not _sparse_embedder_unavailable:
        with _sparse_embedder_lock:
            # Double-checked locking
            if _global_sparse_embedder is None and not _sparse_embedder_unavailable:
                try:
                    from fastembed import SparseTextEmbedding
                except ImportError:
                    logger.warning(
                        "fastembed no esta instalado. BM25 sparse deshabilitado; se usara dense-only."
                    )
                    _sparse_embedder_unavailable = True
                    return None

                logger.info("Cargando BM25 Sparse Embedder...")
                _global_sparse_embedder = SparseTextEmbedding("Qdrant/bm25")

    return _global_sparse_embedder


# ============================================
# API simplificada
# ============================================

def embed_text(text: str) -> List[float]:
    """Genera embedding para un texto (API simple)"""
    embedder = get_embedder()
    return embedder.encode([text])[0].tolist()


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Genera embeddings para multiples textos (API simple)"""
    embedder = get_embedder()
    return embedder.encode_batch(texts)


def text_similarity(text1: str, text2: str) -> float:
    """Calcula similaridad entre dos textos (API simple)"""
    embedder = get_embedder()
    return embedder.similarity(text1, text2)
