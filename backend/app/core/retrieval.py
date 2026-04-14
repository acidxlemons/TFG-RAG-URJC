# backend/app/core/retrieval.py
"""
Hybrid Retrieval System para JARVIS RAG

Este módulo implementa un sistema de búsqueda híbrida que combina:
1. Dense embeddings (búsqueda semántica tradicional)
2. Sparse embeddings BM25 (búsqueda por palabras clave)
3. Reranking con cross-encoder (refinamiento final)

El flujo de búsqueda es:
Query → Dense Search + Sparse Search → Fusion (RRF) → Reranking → Top-K Results

Ventajas:
- Mejora precisión 30-50% vs solo dense embeddings
- Combina ventajas de keyword matching y búsqueda semántica
- Reranking mejora drásticamente la relevancia de los top-5 resultados
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass
from collections import defaultdict

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Filter,
    FieldCondition,
    MatchValue,
    ScoredPoint,
    NamedVector,
    SparseVector,
)

try:
    from qdrant_client.models import Prefetch, FusionQuery, Fusion
    HYBRID_QUERY_AVAILABLE = True
except ImportError:
    Prefetch = None  # type: ignore[assignment]
    FusionQuery = None  # type: ignore[assignment]
    Fusion = None  # type: ignore[assignment]
    HYBRID_QUERY_AVAILABLE = False
from sentence_transformers import CrossEncoder
import numpy as np
from app.processing.embeddings.sentence_transformer import get_sparse_embedder

logger = logging.getLogger(__name__)

DEFAULT_RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


@dataclass
class SearchResult:
    """
    Resultado de búsqueda con metadata enriquecida
    
    Attributes:
        id: ID único del chunk
        text: Contenido del chunk
        score: Score final (después de reranking si aplica)
        metadata: Metadata del documento (filename, page, section, etc.)
        dense_score: Score de búsqueda densa (opcional)
        sparse_score: Score de búsqueda sparse (opcional)
        rerank_score: Score de reranking (opcional)
    """
    id: str
    text: str
    score: float
    metadata: Dict[str, Any]
    dense_score: Optional[float] = None
    sparse_score: Optional[float] = None
    rerank_score: Optional[float] = None
    
    def to_dict(self) -> Dict:
        """Convierte a diccionario para serialización"""
        return {
            "id": self.id,
            "text": self.text,
            "score": self.score,
            "metadata": self.metadata,
            "scores": {
                "dense": self.dense_score,
                "sparse": self.sparse_score,
                "rerank": self.rerank_score
            }
        }


class HybridRetriever:
    """
    Retriever híbrido que combina múltiples estrategias de búsqueda
    
    Estrategias disponibles:
    1. Dense-only: Solo embeddings densos (compatibilidad con anterior)
    2. Sparse-only: Solo BM25 (útil para queries con términos específicos)
    3. Hybrid: Combinación de dense + sparse con RRF
    4. Hybrid + Rerank: Lo anterior + cross-encoder reranking (recomendado)
    
    Parámetros importantes:
    - alpha: Peso entre dense (alpha) y sparse (1-alpha). Default 0.7
    - rerank: Si usar reranking. Mejora calidad pero añade latencia (~100-300ms)
    - top_k: Número de resultados finales
    - fetch_k: Número de resultados a obtener antes de reranking (debe ser > top_k)
    """
    
    def __init__(
        self,
        qdrant_client: QdrantClient,
        embedding_model: Any,
        reranker_model_name: str = "cross-encoder/ms-marco-MiniLM-L-12-v2",
        enable_reranking: bool = True,
        default_alpha: float = 0.7,
    ):
        """
        Inicializa el retriever híbrido
        
        Args:
            qdrant_client: Cliente de Qdrant ya configurado
            embedding_model: Modelo de embeddings (ej: SentenceTransformer)
            reranker_model_name: Nombre del modelo cross-encoder para reranking
            enable_reranking: Si habilitar reranking por defecto
            default_alpha: Peso por defecto entre dense y sparse (0-1)
        """
        self.qdrant = qdrant_client
        self.encoder = embedding_model
        self.enable_reranking = enable_reranking
        self.default_alpha = default_alpha
        self.sparse_embedder = get_sparse_embedder()
        self.reranker_model_name = self._resolve_reranker_model_name(reranker_model_name)
        self.reranker_device = (os.getenv("RERANKER_DEVICE", "cpu") or "cpu").strip().lower()
        
        # Inicializar reranker si está habilitado
        self.reranker = None
        if enable_reranking:
            try:
                logger.info(f"Cargando modelo de reranking: {self.reranker_model_name}")
                self.reranker = self._load_reranker(self.reranker_model_name, self.reranker_device)
                logger.info("✓ Reranker cargado correctamente")
            except Exception as e:
                logger.warning(f"No se pudo cargar reranker: {e}. Reranking deshabilitado.")
                self.enable_reranking = False
        
        logger.info(
            f"HybridRetriever inicializado (alpha={default_alpha}, "
            f"reranking={'ON' if self.enable_reranking else 'OFF'})"
        )

    @staticmethod
    def _resolve_reranker_model_name(model_name: str | None) -> str:
        configured = (model_name or "").strip()
        if not configured:
            return DEFAULT_RERANKER_MODEL

        looks_like_path = any(
            marker in configured for marker in (os.sep, "/", "\\")
        ) or configured.startswith(".")
        if looks_like_path and not os.path.exists(configured):
            logger.warning(
                "RERANKER_MODEL apunta a '%s' pero no existe. Se usara '%s'.",
                configured,
                DEFAULT_RERANKER_MODEL,
            )
            return DEFAULT_RERANKER_MODEL

        return configured

    @staticmethod
    def _normalize_text(text: str) -> str:
        value = unicodedata.normalize("NFKD", (text or "").strip().lower())
        return "".join(ch for ch in value if not unicodedata.combining(ch))

    def _dedupe_results(self, results: List[SearchResult]) -> List[SearchResult]:
        deduped: List[SearchResult] = []
        seen = set()
        for result in results:
            metadata = result.metadata or {}
            key = (
                self._normalize_text(str(metadata.get("filename", ""))),
                int(metadata.get("page") or 0),
                self._normalize_text(result.text),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(result)
        return deduped

    def _load_reranker(self, model_name: str, device: str):
        logger.info(f"Inicializando CrossEncoder en device={device}")
        try:
            return CrossEncoder(model_name, max_length=512, device=device)
        except TypeError:
            logger.warning("CrossEncoder no soporta parametro 'device'; se usa la configuracion por defecto.")
            self.reranker_device = "default"
            return CrossEncoder(model_name, max_length=512)

    def _maybe_fallback_reranker_to_cpu(self, error: Exception) -> bool:
        if self.reranker_device in {"cpu", "default"}:
            return False

        error_text = str(error).lower()
        if "cuda" not in error_text and "cublas" not in error_text and "device-side" not in error_text:
            return False

        try:
            logger.warning("Fallo CUDA en HybridRetriever; reintentando reranker en CPU.")
            self.reranker = self._load_reranker(self.reranker_model_name, "cpu")
            self.reranker_device = "cpu"
            return True
        except Exception as fallback_error:
            logger.error(f"No se pudo mover el reranker a CPU: {fallback_error}")
            self.reranker = None
            self.enable_reranking = False
            return False
    
    def search(
        self,
        query: str,
        collection_name: str,
        top_k: int = 10,
        tenant_id: Optional[str] = None,
        strategy: str = "hybrid",
        alpha: Optional[float] = None,
        use_reranking: Optional[bool] = None,
        filters: Optional[Dict] = None,
    ) -> List[SearchResult]:
        """
        Búsqueda principal con estrategia configurable
        
        Args:
            query: Texto de búsqueda
            collection_name: Nombre de la colección en Qdrant
            top_k: Número de resultados a retornar
            tenant_id: ID de tenant para filtrado multi-tenant
            strategy: Estrategia de búsqueda ("dense", "sparse", "hybrid")
            alpha: Peso dense vs sparse (solo para hybrid). Si None, usa default_alpha
            use_reranking: Override para habilitar/deshabilitar reranking
            filters: Filtros adicionales (ej: fecha, tipo de documento)
        
        Returns:
            Lista de SearchResult ordenados por relevancia
        """
        # Validar inputs
        if top_k <= 0:
            raise ValueError("top_k debe ser > 0")
        
        # Determinar parámetros
        alpha = alpha if alpha is not None else self.default_alpha
        use_reranking = use_reranking if use_reranking is not None else self.enable_reranking
        query_ids = self._extract_query_ids(query)
        
        # Para reranking, obtener más resultados inicialmente
        fetch_k = top_k * 3 if use_reranking else top_k
        if query_ids:
            fetch_k = max(fetch_k, top_k * 10)
        
        logger.info(
            f"Búsqueda: strategy={strategy}, top_k={top_k}, "
            f"tenant={tenant_id}, rerank={use_reranking}"
        )
        
        # Ejecutar estrategia correspondiente
        if strategy == "dense":
            results = self._dense_search(
                query, collection_name, fetch_k, tenant_id, filters
            )
        elif strategy == "sparse":
            results = self._sparse_search(
                query, collection_name, fetch_k, tenant_id, filters
            )
        elif strategy == "hybrid":
            results = self._hybrid_search(
                query, collection_name, fetch_k, tenant_id, alpha, filters
            )
        else:
            raise ValueError(f"Estrategia desconocida: {strategy}")

        results = self._dedupe_results(results)
        
        # Reranking si está habilitado
        if use_reranking and self.reranker and len(results) > 1:
            results = self._rerank(query, results)

        # Boost para IDs exactos (ej: M-003, CR-277).
        if query_ids:
            # 1. Recuperar proactivamente de Qdrant si no están en los Top-K
            exact_matches = self._fetch_exact_id_matches(query_ids, collection_name, tenant_id, filters)
            
            # Unir evitando duplicados por doc_id
            existing_ids = {r.id for r in results}
            for e in exact_matches:
                if e.id not in existing_ids:
                    results.append(e)
                    existing_ids.add(e.id)

            results = self._dedupe_results(results)
            
            # Boosters de score
            results = self._boost_exact_id_matches(results, query_ids)

        
        # Retornar top_k finales
        return results[:top_k]
    
    def _dense_search(
        self,
        query: str,
        collection_name: str,
        limit: int,
        tenant_id: Optional[str],
        filters: Optional[Dict],
    ) -> List[SearchResult]:
        """
        Búsqueda usando solo embeddings densos (semántica)
        
        Esta es la estrategia tradicional de RAG. Funciona bien para:
        - Queries conceptuales ("documentos sobre calidad")
        - Búsqueda por significado vs palabras exactas
        - Encontrar contenido similar semánticamente
        """
        try:
            # DEBUG: Inspeccionar objeto qdrant
            logger.info(f"DEBUG: qdrant type: {type(self.qdrant)}")
            logger.info(f"DEBUG: qdrant dir: {dir(self.qdrant)}")

            from qdrant_client.models import SearchRequest
            
            # Generar embedding de la query
            # encoder.encode() returns (1, dim) for single string, so take [0]
            query_vector = self.encoder.encode(query)[0].tolist()
            
            # Construir filtro
            search_filter = self._build_filter(tenant_id, filters)
            
            # B?squeda en Qdrant (named vector si existe)
            try:
                search_results = self.qdrant.query_points(
                    collection_name=collection_name,
                    query=NamedVector(name="dense", vector=query_vector),
                    query_filter=search_filter,
                    limit=limit,
                    with_payload=True,
                ).points
            except Exception:
                # Fallback a colecci?n con vector ?nico
                search_results = self.qdrant.query_points(
                    collection_name=collection_name,
                    query=query_vector,
                    query_filter=search_filter,
                    limit=limit,
                    with_payload=True,
                ).points

            # Convertir a SearchResult
            results = []
            for point in search_results:
                # Build metadata dict from payload (fields are at root level, not nested)
                payload = point.payload
                metadata = {
                    "filename": payload.get("filename", "unknown"),
                    "page": payload.get("page"),
                    "chunk_index": payload.get("chunk_index", 0),
                    "from_ocr": payload.get("from_ocr", False),
                    "source": payload.get("source", ""),
                    "tenant_id": payload.get("tenant_id"),
                }
                
                results.append(SearchResult(
                    id=str(point.id),
                    text=payload.get("text", ""),
                    score=point.score,
                    metadata=metadata,
                    dense_score=point.score,
                ))
            
            logger.debug(f"Dense search: {len(results)} resultados")
            return results
            
        except Exception as e:
            logger.error(f"Error en dense search: {e}")
            return []
    
    def _sparse_search(
        self,
        query: str,
        collection_name: str,
        limit: int,
        tenant_id: Optional[str],
        filters: Optional[Dict],
    ) -> List[SearchResult]:
        """
        B?squeda BM25 usando sparse vectors

        BM25 es excelente para:
        - B?squeda por keywords exactas ("ISO 9001", "GDPR")
        - T?rminos t?cnicos espec?ficos
        - Nombres propios, c?digos, referencias

        Nota: Requiere que Qdrant tenga sparse vectors habilitados
        y que los documentos hayan sido indexados con BM25
        """
        try:
            sparse_gen = list(self.sparse_embedder.embed([query]))[0]
            sparse_query = SparseVector(
                indices=sparse_gen.indices.tolist(),
                values=sparse_gen.values.tolist(),
            )

            search_filter = self._build_filter(tenant_id, filters)

            search_results = self.qdrant.query_points(
                collection_name=collection_name,
                query=sparse_query,
                using="sparse",
                query_filter=search_filter,
                limit=limit,
                with_payload=True,
            ).points

            results = []
            for point in search_results:
                payload = point.payload
                metadata = {
                    "filename": payload.get("filename", "unknown"),
                    "page": payload.get("page"),
                    "chunk_index": payload.get("chunk_index", 0),
                    "from_ocr": payload.get("from_ocr", False),
                    "source": payload.get("source", ""),
                    "tenant_id": payload.get("tenant_id"),
                }

                results.append(SearchResult(
                    id=str(point.id),
                    text=payload.get("text", ""),
                    score=point.score,
                    metadata=metadata,
                    sparse_score=point.score,
                ))

            logger.debug(f"Sparse search: {len(results)} resultados")
            return results

        except Exception as e:
            logger.error(f"Error en sparse search: {e}")
            return []

    def _hybrid_search(
        self,
        query: str,
        collection_name: str,
        limit: int,
        tenant_id: Optional[str],
        alpha: float,
        filters: Optional[Dict],
    ) -> List[SearchResult]:
        """
        B?squeda h?brida combinando dense + sparse con RRF

        Usa Prefetch + FusionQuery(RRF) de Qdrant para fusionar rankings.
        """
        if self.sparse_embedder is None:
            logger.warning("Hybrid search sin sparse embedder. Fallback a dense-only.")
            return self._dense_search(query, collection_name, limit, tenant_id, filters)

        try:
            if not HYBRID_QUERY_AVAILABLE:
                dense_results = self._dense_search(query, collection_name, limit, tenant_id, filters)
                sparse_results = self._sparse_search(query, collection_name, limit, tenant_id, filters)
                fused = self._reciprocal_rank_fusion(
                    [dense_results, sparse_results],
                    weights=[alpha, 1.0 - alpha],
                )
                logger.debug(f"Hybrid search: {len(fused)} resultados (manual RRF fallback)")
                return fused[:limit]

            query_dense = self.encoder.encode(query)[0].tolist()
            sparse_gen = list(self.sparse_embedder.embed([query]))[0]
            sparse_query = SparseVector(
                indices=sparse_gen.indices.tolist(),
                values=sparse_gen.values.tolist(),
            )
            search_filter = self._build_filter(tenant_id, filters)

            prefetch_requests = [
                Prefetch(
                    query=query_dense,
                    using="dense",
                    limit=int(limit),
                    filter=search_filter,
                ),
                Prefetch(
                    query=sparse_query,
                    using="sparse",
                    limit=int(limit),
                    filter=search_filter,
                ),
            ]

            search_results = self.qdrant.query_points(
                collection_name=collection_name,
                prefetch=prefetch_requests,
                query=FusionQuery(fusion=Fusion.RRF),
                limit=int(limit),
                with_payload=True,
            ).points

            results = []
            for point in search_results:
                payload = point.payload
                metadata = {
                    "filename": payload.get("filename", "unknown"),
                    "page": payload.get("page"),
                    "chunk_index": payload.get("chunk_index", 0),
                    "from_ocr": payload.get("from_ocr", False),
                    "source": payload.get("source", ""),
                    "tenant_id": payload.get("tenant_id"),
                }

                results.append(SearchResult(
                    id=str(point.id),
                    text=payload.get("text", ""),
                    score=point.score,
                    metadata=metadata,
                ))

            logger.debug(f"Hybrid search: {len(results)} resultados (Qdrant RRF)")
            return results
        except Exception as e:
            logger.warning(f"Hybrid search fallback a dense-only: {e}")
            return self._dense_search(query, collection_name, limit, tenant_id, filters)

    def _reciprocal_rank_fusion(
        self,
        result_lists: List[List[SearchResult]],
        weights: List[float],
        k: int = 60,
    ) -> List[SearchResult]:
        """
        Implementación de Reciprocal Rank Fusion
        
        Args:
            result_lists: Lista de listas de resultados (ej: [dense, sparse])
            weights: Pesos para cada lista (deben sumar aprox. 1.0)
            k: Constante de RRF (típicamente 60)
        
        Returns:
            Lista fusionada y ordenada por RRF score
        """
        # Diccionario para acumular scores
        # {doc_id: {"result": SearchResult, "rrf_score": float}}
        doc_scores: Dict[str, Dict] = {}
        
        # Procesar cada lista de resultados
        for results, weight in zip(result_lists, weights):
            for rank, result in enumerate(results, start=1):
                # Score RRF ponderado
                rrf_score = weight * (1.0 / (k + rank))
                
                if result.id not in doc_scores:
                    # Primera vez que vemos este documento
                    doc_scores[result.id] = {
                        "result": result,
                        "rrf_score": rrf_score
                    }
                else:
                    # Acumular score (el documento apareció en múltiples rankings)
                    doc_scores[result.id]["rrf_score"] += rrf_score
        
        # Ordenar por RRF score descendente
        sorted_docs = sorted(
            doc_scores.values(),
            key=lambda x: x["rrf_score"],
            reverse=True
        )
        
        # Crear lista de SearchResult con scores actualizados
        fused_results = []
        for item in sorted_docs:
            result = item["result"]
            # Actualizar score con el RRF score
            result.score = item["rrf_score"]
            fused_results.append(result)
        
        return fused_results
    
    def _rerank(
        self,
        query: str,
        candidates: List[SearchResult],
    ) -> List[SearchResult]:
        """
        Reranking usando cross-encoder
        
        El cross-encoder es más preciso que bi-encoder (embeddings) porque:
        - Procesa query + documento juntos (atención cruzada)
        - Puede capturar interacciones más complejas
        - Mejora especialmente el top-5 de resultados
        
        Trade-off:
        - Más preciso pero más lento (no escalable a millones de docs)
        - Por eso se usa solo para reranker, no para retrieval inicial
        
        Args:
            query: Query original
            candidates: Lista de candidatos a reranker
        
        Returns:
            Lista reordenada por rerank score
        """
        if not candidates:
            return []
        
        try:
            # Crear pares [query, documento]
            pairs = [[query, c.text] for c in candidates]
            
            # Predecir scores
            # El cross-encoder retorna scores de relevancia directamente
            rerank_scores = self.reranker.predict(pairs)
            
            # Actualizar scores en los resultados
            for i, candidate in enumerate(candidates):
                candidate.rerank_score = float(rerank_scores[i])
                # Usar rerank score como score principal
                candidate.score = candidate.rerank_score
            
            # Reordenar por rerank score
            reranked = sorted(
                candidates,
                key=lambda x: x.rerank_score,
                reverse=True
            )
            
            logger.debug(f"Reranked {len(reranked)} resultados")
            return reranked
            
        except Exception as e:
            if self._maybe_fallback_reranker_to_cpu(e) and self.reranker is not None:
                try:
                    pairs = [[query, c.text] for c in candidates]
                    rerank_scores = self.reranker.predict(pairs)

                    for i, candidate in enumerate(candidates):
                        candidate.rerank_score = float(rerank_scores[i])
                        candidate.score = candidate.rerank_score

                    return sorted(candidates, key=lambda x: x.rerank_score, reverse=True)
                except Exception as retry_error:
                    logger.error(f"Error en reranking tras fallback a CPU: {retry_error}")
                    return candidates

            logger.error(f"Error en reranking: {e}")
            # En caso de error, retornar candidatos sin reranker
            return candidates
    

    def _extract_query_ids(self, query: str) -> List[str]:
        tokens = re.findall(r"\b[A-Z]{1,8}-\d{2,8}\b", (query or "").upper())
        return list(dict.fromkeys(tokens))

    def _fetch_exact_id_matches(
        self,
        query_ids: List[str],
        collection_name: str,
        tenant_id: Optional[str],
        filters: Optional[Dict],
    ) -> List[SearchResult]:
        """
        Busca proactivamente los documentos que contengan exactamente el ID solicitado
        para asegurar que entren en el 'candidate pool' del RAG antes del re-ranker.
        """
        from qdrant_client.models import Filter, FieldCondition, MatchText
        
        results = []
        base_must = []
        if tenant_id:
            base_must.append(FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)))
        
        if filters:
            # TODO: mezclar 'must' adicionales de additional_filters si los hubiera
            pass
            
        for qid in query_ids:
            query_filter = Filter(
                must=base_must + [
                    FieldCondition(
                        key="filename",
                        match=MatchText(text=qid)
                    )
                ]
            )
            
            try:
                # Recuperar hasta 10 fragmentos con ese ID exacto
                points = self.qdrant.scroll(
                    collection_name=collection_name,
                    scroll_filter=query_filter,
                    limit=10,
                    with_payload=True,
                    with_vectors=False
                )[0]
                
                logger.debug(f"_fetch_exact_id_matches for '{qid}': found {len(points)} points")
                
                for point in points:
                    payload = point.payload
                    metadata = {
                        "filename": payload.get("filename", "unknown"),
                        "page": payload.get("page"),
                        "chunk_index": payload.get("chunk_index", 0),
                        "from_ocr": payload.get("from_ocr", False),
                        "source": payload.get("source", ""),
                        "tenant_id": payload.get("tenant_id"),
                    }
                    
                    # Le asignamos un score arbitrario alto temporal
                    # porque luego _boost_exact_id_matches lo multiplicará
                    results.append(SearchResult(
                        id=str(point.id),
                        text=payload.get("text", ""),
                        score=1.0, 
                        metadata=metadata,
                        dense_score=1.0,
                    ))
            except Exception as e:
                logger.warning(f"Error forzando fetch de ID exacto '{qid}': {e}")
                
        return results

    def _has_exact_id(self, text: str, token: str) -> bool:
        if not text:
            return False
        pattern = rf"(?<![A-Z0-9]){re.escape(token.upper())}(?![A-Z0-9])"
        return re.search(pattern, text.upper()) is not None

    def _boost_exact_id_matches(self, results: List[SearchResult], query_ids: List[str]) -> List[SearchResult]:
        def _priority(r: SearchResult):
            meta = r.metadata or {}
            filename = str(meta.get("filename") or "")
            source = str(meta.get("source") or "")
            text = str(r.text or "")[:500]

            fname_hits = sum(1 for t in query_ids if self._has_exact_id(filename, t))
            source_hits = sum(1 for t in query_ids if self._has_exact_id(source, t))
            text_hits = sum(1 for t in query_ids if self._has_exact_id(text, t))

            # filename > source > text > score base
            priority = (fname_hits * 100) + (source_hits * 10) + text_hits
            return (priority, float(r.score or 0.0))

        return sorted(results, key=_priority, reverse=True)

    def _build_filter(
        self,
        tenant_id: Optional[str],
        additional_filters: Optional[Dict],
    ) -> Optional[Filter]:
        """
        Construye filtro de Qdrant para multi-tenant y otros criterios
        
        Filtros comunes:
        - tenant_id: Aislamiento multi-tenant (CRÍTICO para seguridad)
        - document_type: Filtrar por tipo (pdf, docx, etc.)
        - date_range: Documentos en rango de fechas
        - department: Filtrar por departamento
        
        Args:
            tenant_id: ID del tenant (para multi-tenancy)
            additional_filters: Filtros adicionales como dict
        
        Returns:
            Objeto Filter de Qdrant o None
        """
        conditions = []
        
        # Filtro de tenant (SIEMPRE aplicar si está presente)
        if tenant_id:
            conditions.append(
                FieldCondition(
                    key="tenant_id",
                    match=MatchValue(value=tenant_id)
                )
            )
        
        # Filtros adicionales (date_range, source_type, filename, from_ocr, source)
        if additional_filters:
            # Filtro por tipo de fuente: sharepoint, upload, scrape, web
            source_type = additional_filters.get("source_type")
            if source_type:
                if isinstance(source_type, list):
                    # OR: varios tipos permitidos (Qdrant no soporta OR en must directo)
                    conditions.append(
                        FieldCondition(key="source_type", match=MatchValue(value=source_type[0]))
                    )
                else:
                    conditions.append(
                        FieldCondition(key="source_type", match=MatchValue(value=source_type))
                    )

            # Filtro por nombre de archivo (texto parcial)
            filename = additional_filters.get("filename")
            if filename:
                from qdrant_client.models import MatchText
                conditions.append(
                    FieldCondition(key="filename", match=MatchText(text=filename))
                )

            # Filtro por rango de fechas (epoch seconds: {"gte": 1700000000, "lte": 1800000000})
            date_range = additional_filters.get("date_range")
            if date_range and isinstance(date_range, dict):
                from qdrant_client.models import Range
                range_kwargs = {}
                if "gte" in date_range:
                    range_kwargs["gte"] = float(date_range["gte"])
                if "lte" in date_range:
                    range_kwargs["lte"] = float(date_range["lte"])
                if range_kwargs:
                    conditions.append(
                        FieldCondition(key="ingested_at_ts", range=Range(**range_kwargs))
                    )

            # Excluir o incluir documentos OCR
            from_ocr = additional_filters.get("from_ocr")
            if from_ocr is not None:
                conditions.append(
                    FieldCondition(key="from_ocr", match=MatchValue(value=bool(from_ocr)))
                )

            # Filtro por colección de origen
            source = additional_filters.get("source")
            if source:
                conditions.append(
                    FieldCondition(key="source", match=MatchValue(value=source))
                )

        if not conditions:
            return None

        return Filter(must=conditions)
    
    def search_multi_collection(
        self,
        query: str,
        collection_names: List[str],
        top_k: int = 10,
        strategy: str = "hybrid",
        alpha: Optional[float] = None,
        use_reranking: Optional[bool] = None,
        filters: Optional[Dict] = None,
    ) -> List[SearchResult]:
        """
        Búsqueda en múltiples colecciones simultáneamente.
        
        Útil para usuarios con acceso a múltiples departamentos.
        Fusiona resultados de todas las colecciones y los ordena por relevancia.
        
        Args:
            query: Texto de búsqueda
            collection_names: Lista de colecciones a buscar (ej: ["documents_RRHH", "documents_Calidad"])
            top_k: Número de resultados finales
            strategy: Estrategia de búsqueda ("dense", "sparse", "hybrid")
            alpha: Peso dense vs sparse
            use_reranking: Si usar reranking en los resultados fusionados
            filters: Filtros adicionales
        
        Returns:
            Lista de SearchResult con metadata de origen (source_collection)
        """
        if not collection_names:
            logger.warning("search_multi_collection llamado sin colecciones")
            return []
        
        logger.info(f"Multi-collection search: {len(collection_names)} colecciones, query='{query[:50]}...'")
        
        # Determinar parámetros
        alpha = alpha if alpha is not None else self.default_alpha
        use_reranking = use_reranking if use_reranking is not None else self.enable_reranking
        query_ids = self._extract_query_ids(query)
        
        # Obtener más resultados por colección si vamos a reranker
        per_collection_k = max(top_k, 5) if use_reranking else top_k
        
        all_results: List[SearchResult] = []
        
        # Buscar en cada colección
        for collection_name in collection_names:
            try:
                # Verificar si la colección existe
                try:
                    collection_info = self.qdrant.get_collection(collection_name)
                    if collection_info.points_count == 0:
                        logger.debug(f"Colección {collection_name} vacía, saltando")
                        continue
                except Exception:
                    logger.warning(f"Colección {collection_name} no existe, saltando")
                    continue
                
                # Buscar en esta colección (sin tenant_id, ya que el nombre de colección ES el tenant)
                results = self.search(
                    query=query,
                    collection_name=collection_name,
                    top_k=per_collection_k,
                    tenant_id=None,  # No filtrar por tenant, la colección YA es el filtro
                    strategy=strategy,
                    alpha=alpha,
                    use_reranking=False,  # Reranker al final sobre todos los resultados
                    filters=filters,
                )
                
                # Añadir metadata de colección de origen
                for result in results:
                    result.metadata["source_collection"] = collection_name
                
                all_results.extend(results)
                logger.debug(f"  {collection_name}: {len(results)} resultados")
                
            except Exception as e:
                logger.error(f"Error buscando en {collection_name}: {e}")
        
        if not all_results:
            logger.info("Multi-collection search: sin resultados")
            return []
        
        # Ordenar por score (antes de reranking)
        all_results.sort(key=lambda x: x.score, reverse=True)
        
        # Reranking sobre todos los resultados combinados
        if use_reranking and self.reranker and len(all_results) > 1:
            # Tomar top candidatos para reranking (por rendimiento)
            candidates = all_results[:min(len(all_results), top_k * 3)]
            all_results = self._rerank(query, candidates)
        
        # Retornar top_k finales
        final_results = all_results[:top_k]
        
        logger.info(
            f"Multi-collection search completado: {len(final_results)} resultados "
            f"de {len(collection_names)} colecciones"
        )
        
        return final_results


# ============================================
# UTILIDADES
# ============================================

def calculate_ndcg(
    results: List[SearchResult],
    relevance_labels: List[int],
    k: int = 10
) -> float:
    """
    Calcula NDCG@k (Normalized Discounted Cumulative Gain)
    
    NDCG es una métrica estándar para evaluar ranking.
    Penaliza documentos relevantes que aparecen en posiciones bajas.
    
    Args:
        results: Resultados del retriever
        relevance_labels: Etiquetas de relevancia (0=irrelevante, 1+=relevante)
        k: Número de resultados a considerar
    
    Returns:
        NDCG score entre 0 y 1 (1 = perfecto)
    """
    # DCG = Σ (relevance / log2(position + 1))
    dcg = 0.0
    for i, (result, label) in enumerate(zip(results[:k], relevance_labels[:k]), start=1):
        dcg += label / np.log2(i + 1)
    
    # IDCG = DCG del ranking ideal (ordenado por relevancia)
    ideal_labels = sorted(relevance_labels[:k], reverse=True)
    idcg = 0.0
    for i, label in enumerate(ideal_labels, start=1):
        idcg += label / np.log2(i + 1)
    
    # NDCG = DCG / IDCG
    if idcg == 0:
        return 0.0
    
    return dcg / idcg


# ============================================
# EJEMPLO DE USO
# ============================================

if __name__ == "__main__":
    """
    Ejemplo de uso del HybridRetriever
    """
    from qdrant_client import QdrantClient
    from sentence_transformers import SentenceTransformer
    
    # Inicializar componentes
    qdrant = QdrantClient(url="http://localhost:6333")
    encoder = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
    
    # Crear retriever
    retriever = HybridRetriever(
        qdrant_client=qdrant,
        embedding_model=encoder,
        enable_reranking=True,
        default_alpha=0.7
    )
    
    # Búsqueda hybrid con reranking
    results = retriever.search(
        query="¿Cuáles son los requisitos de auditoría interna?",
        collection_name="documents",
        top_k=5,
        tenant_id="empresa-demo",
        strategy="hybrid",
        use_reranking=True
    )
    
    # Mostrar resultados
    print(f"\n🔍 Encontrados {len(results)} resultados:\n")
    for i, result in enumerate(results, 1):
        print(f"{i}. Score: {result.score:.4f}")
        print(f"   Archivo: {result.metadata.get('filename', 'N/A')}")
        print(f"   Texto: {result.text[:100]}...")
        print(f"   Scores: dense={result.dense_score:.4f}, rerank={result.rerank_score:.4f}\n")
