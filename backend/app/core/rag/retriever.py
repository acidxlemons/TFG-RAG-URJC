# backend/app/core/rag/retriever.py

"""
RAG Retriever con Qdrant
Implementa búsqueda semántica con sistema de citas obligatorio
"""

from typing import List, Dict, Optional, Tuple, Union
from dataclasses import dataclass
from datetime import datetime
import re

import logging
import numpy as np

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Filter,
    FieldCondition,
    MatchText,
    MatchValue,
    Range,
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

from app.processing.embeddings.sentence_transformer import get_embedder, get_sparse_embedder

logger = logging.getLogger(__name__)

TENANT_CLAIM = "tenant_id"


@dataclass
class RetrievalResult:
    """Resultado de búsqueda con metadata completo"""
    text: str
    score: float
    source: str
    filename: str
    page: Optional[int]
    chunk_index: int
    from_ocr: bool
    ingested_at: datetime
    citation: str  # Formato: [filename p.X]


class RAGRetriever:
    """
    Retriever principal del sistema RAG

    Características:
    - Búsqueda semántica en Qdrant
    - Filtros avanzados (por documento, fecha, tipo)
    - Sistema de citas obligatorio
    - Reranking opcional (MMR)
    - Fusión de contexto
    """

    def __init__(
        self,
        qdrant_client: QdrantClient,
        collection_name: str,
        embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2",
        top_k: int = 5,
        score_threshold: float = 0.7,
        tenant_id: Optional[str] = None,
        reranker=None,
    ):
        self.client = qdrant_client
        self.collection_name = collection_name
        self.top_k = top_k
        self.score_threshold = score_threshold
        self.tenant_id = tenant_id
        self.reranker = reranker

        # Embeddings compartidos (singleton)
        logger.info(f"Inicializando embedder: {embedding_model}")
        self.embedder = get_embedder(model_name=embedding_model)
        
        logger.info(f"Inicializando sparse embedder BM25")
        self.sparse_embedder = get_sparse_embedder()
        if self.sparse_embedder is None:
            logger.warning("Sparse embedder no disponible. RAGRetriever usara dense-only.")

        logger.info(f"RAG Retriever inicializado para colección: {collection_name}")

    # =============================== API principal ===============================

    def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        filter_by_source: Optional[str] = None,
        filter_by_filenames: Optional[List[str]] = None,
        filter_date_range: Optional[Tuple[datetime, datetime]] = None,
        exclude_ocr: bool = False,
        tenant_id: Optional[str] = None,
        collection_name: Optional[str] = None,
    ) -> List[RetrievalResult]:
        """
        Recupera documentos relevantes con sistema de citas

        Args:
            query: Consulta del usuario
            top_k: Número de resultados (None = usar default)
            filter_by_source: Filtrar por ruta completa
            filter_by_filenames: Lista de nombres de archivo
            filter_date_range: Tupla (desde, hasta)
            exclude_ocr: Excluir documentos procesados con OCR
            tenant_id: Forzar tenant (si no, usa el de la clase)
            collection_name: Colección específica a buscar (None = usar default)

        Returns:
            Lista de RetrievalResult con citas formateadas
        """
        k = int(top_k or self.top_k)
        
        # Usar colección específica o la por defecto
        target_collection = collection_name or self.collection_name

        # Construir filtros
        # LOGIC FIX: Allow passing "" (empty string) to force NO tenant filter, even if self.tenant_id is set
        eff_tenant = tenant_id if tenant_id is not None else self.tenant_id
        qdrant_filter = self._build_filter(
            source=filter_by_source,
            filenames=filter_by_filenames,
            date_range=filter_date_range,
            exclude_ocr=exclude_ocr,
            tenant_id=eff_tenant,
        )

        log_query = (query or "")[:200].replace("\n", " ")
        logger.info(f"Qdrant.search: '{log_query}…' (collection={target_collection}, k={k}, threshold={self.score_threshold:.2f}, tenant={eff_tenant})")

        # Vectorizar query (Dense)
        query_vec = self.embedder.encode(query)[0]  # np.ndarray (dim,)
        
        # Vectorizar query (Sparse)
        sparse_indices: List[int] = []
        sparse_values: List[float] = []
        if self.sparse_embedder is not None:
            sparse_gen = list(self.sparse_embedder.embed([query]))[0]
            sparse_indices = sparse_gen.indices.tolist()
            sparse_values = sparse_gen.values.tolist()

        # 1) Búsqueda con umbral (Fusión híbrida)
        hits = self._search_raw(
            query_dense=query_vec.tolist(),
            sparse_indices=sparse_indices,
            sparse_values=sparse_values,
            limit=k * 2,
            qdrant_filter=qdrant_filter,
            score_threshold=self.score_threshold,
            collection_name=target_collection,
        )

        # 2) Si no hay nada por umbral, reintento “suave” sin threshold
        if not hits:
            logger.info("Sin resultados por encima del score_threshold. Reintentando sin umbral…")
            hits = self._search_raw(
                query_dense=query_vec.tolist(),
                sparse_indices=sparse_indices,
                sparse_values=sparse_values,
                limit=k * 2,
                qdrant_filter=qdrant_filter,
                score_threshold=None,  # sin umbral
                collection_name=target_collection,
            )

        query_ids = self._extract_query_ids(query)

        # Transformar resultados de búsqueda vectorial.
        results = self._hits_to_results(hits)

        # Para consultas con IDs explícitos (AL-08, M-003, etc.),
        # recuperamos proactivamente chunks por coincidencia textual de payload.
        if query_ids:
            exact_matches = self._fetch_exact_id_matches(
                query_ids=query_ids,
                collection_name=target_collection,
                tenant_id=eff_tenant,
                source=filter_by_source,
                exclude_ocr=exclude_ocr,
            )
            if exact_matches:
                results.extend(exact_matches)

        results = self._dedupe_results(results)

        if getattr(self, "reranker", None) and getattr(self.reranker, "model", None):
            results = self.reranker.rerank(query, results, top_k=k)
        else:
            results = self.mmr_rerank(query, results, lambda_mult=0.5)[:k]

        # Boost determinista para consultas tipo ID (ej: M-003, CR-277).
        # Evita que MMR o similitud semántica escondan coincidencias exactas.
        if query_ids:
            results = self._boost_exact_id_matches(results, query_ids)[:k]

        logger.info(f"Recuperados {len(results)} documentos relevantes tras reranking")
        return results

    def retrieve_multi_collection(
        self,
        query: str,
        collections: List[str],
        top_k: Optional[int] = None,
        filter_by_filenames: Optional[List[str]] = None,
        tenant_id: Optional[str] = None,
    ) -> List[RetrievalResult]:
        """
        Recupera documentos de m?ltiples colecciones simult?neamente.

        Fusiona resultados de todas las colecciones y aplica MMR reranking global.
        """
        k = int(top_k or self.top_k)
        all_results: List[RetrievalResult] = []

        if not collections:
            collections = [self.collection_name]

        logger.info(f"Buscando en {len(collections)} colecciones: {collections}")

        # Buscar en cada colecci?n
        per_collection_limit = max(3, k // len(collections) + 2)

        for collection in collections:
            try:
                results = self.retrieve(
                    query=query,
                    top_k=per_collection_limit,
                    filter_by_filenames=filter_by_filenames,
                    tenant_id=tenant_id,
                    collection_name=collection,
                )

                # Agregar metadata de colecci?n para tracking
                for r in results:
                    if not r.source.startswith(f"[{collection}]"):
                        r.source = f"[{collection}] {r.source}"

                all_results.extend(results)
                logger.debug(f"  - {collection}: {len(results)} resultados")

            except Exception as e:
                logger.error(f"Error buscando en colecci?n {collection}: {e}")

        # Aplicar reranking global
        if len(all_results) > 1:
            if getattr(self, "reranker", None) and getattr(self.reranker, "model", None):
                all_results = self.reranker.rerank(query, all_results, top_k=k)
            else:
                all_results = self.mmr_rerank(query, all_results, lambda_mult=0.5)[:k]

        # Tomar top_k
        final_results = all_results[:k]

        logger.info(f"Multi-collection: {len(final_results)} resultados finales de {len(collections)} colecciones")
        return final_results

    # =============================== B?squeda cruda ===============================

    def _search_raw(
        self,
        *,
        query_dense: List[float],
        sparse_indices: List[int],
        sparse_values: List[float],
        limit: int,
        qdrant_filter: Optional[Filter],
        score_threshold: Optional[float],
        collection_name: Optional[str] = None,
    ):
        target_collection = collection_name or self.collection_name
        if not sparse_indices or not sparse_values:
            try:
                try:
                    fallback = self.client.query_points(
                        collection_name=target_collection,
                        query=query_dense,
                        using="dense",
                        query_filter=qdrant_filter,
                        limit=int(limit),
                        score_threshold=float(score_threshold) if score_threshold is not None else None,
                        with_payload=True,
                    )
                except Exception:
                    fallback = self.client.query_points(
                        collection_name=target_collection,
                        query=query_dense,
                        query_filter=qdrant_filter,
                        limit=int(limit),
                        score_threshold=float(score_threshold) if score_threshold is not None else None,
                        with_payload=True,
                    )
                hits = sorted(fallback.points, key=lambda h: h.score or 0.0, reverse=True)
                return hits
            except Exception as e:
                logger.exception(f"Dense-only search failed (collection={target_collection}): {e}")
                return []

        try:
            if not HYBRID_QUERY_AVAILABLE:
                raise RuntimeError("Hybrid query API not available in installed qdrant-client")

            prefetch_requests = [
                Prefetch(
                    query=query_dense,
                    using="dense",
                    limit=int(limit),
                    filter=qdrant_filter,
                    score_threshold=float(score_threshold) if score_threshold is not None else None,
                ),
                Prefetch(
                    query=SparseVector(indices=sparse_indices, values=sparse_values),
                    using="sparse",
                    limit=int(limit),
                    filter=qdrant_filter,
                ),
            ]

            search_results = self.client.query_points(
                collection_name=target_collection,
                prefetch=prefetch_requests,
                query=FusionQuery(fusion=Fusion.RRF),
                limit=int(limit),
                with_payload=True,
            )

            # Orden descendente por score (RRF scores)
            hits = sorted(search_results.points, key=lambda h: h.score or 0.0, reverse=True)
            return hits
        except Exception as e:
            logger.exception(f"Error en Qdrant.search (collection={target_collection}): {e}")
            # Fallback a b?squeda dense-only para colecciones sin h?brido
            try:
                try:
                    fallback = self.client.query_points(
                        collection_name=target_collection,
                        query=query_dense,
                        using="dense",
                        query_filter=qdrant_filter,
                        limit=int(limit),
                        score_threshold=float(score_threshold) if score_threshold is not None else None,
                        with_payload=True,
                    )
                except Exception:
                    fallback = self.client.query_points(
                        collection_name=target_collection,
                        query=query_dense,
                        query_filter=qdrant_filter,
                        limit=int(limit),
                        score_threshold=float(score_threshold) if score_threshold is not None else None,
                        with_payload=True,
                    )
                hits = sorted(fallback.points, key=lambda h: h.score or 0.0, reverse=True)
                return hits
            except Exception as e2:
                logger.exception(f"Fallback dense-only failed (collection={target_collection}): {e2}")
                return []

    # =============================== Filtros ===============================

    def _build_filter(
        self,
        source: Optional[str] = None,
        filenames: Optional[List[str]] = None,
        date_range: Optional[Tuple[datetime, datetime]] = None,
        exclude_ocr: bool = False,
        tenant_id: Optional[str] = None,
    ) -> Optional[Filter]:
        """Construye filtro complejo de Qdrant (must/should/must_not)"""

        must: List[FieldCondition] = []
        should: List[FieldCondition] = []
        must_not: List[FieldCondition] = []

        # Aislamiento por tenant
        if tenant_id:
            must.append(FieldCondition(key=TENANT_CLAIM, match=MatchValue(value=tenant_id)))

        # Filtro por origen/ruta completa
        if source:
            must.append(FieldCondition(key="source", match=MatchValue(value=source)))

        # Filtro por lista de filenames (OR lógico)
        if filenames:
            for fn in filenames:
                should.append(FieldCondition(key="filename", match=MatchValue(value=fn)))

        # Filtro por rango de fechas (epoch seconds)
        if date_range:
            start, end = date_range
            must.append(
                FieldCondition(
                    key="ingested_at_ts",
                    range=Range(gte=int(start.timestamp()), lte=int(end.timestamp())),
                )
            )

        # Excluir OCR
        if exclude_ocr:
            must.append(FieldCondition(key="from_ocr", match=MatchValue(value=False)))

        if not (must or should or must_not):
            return None

        clauses: Dict[str, List[FieldCondition]] = {}
        if must:
            clauses["must"] = must
        if should:
            clauses["should"] = should
        if must_not:
            clauses["must_not"] = must_not

        return Filter(**clauses)

    # =============================== Conversión de resultados ===============================

    def _hits_to_results(self, hits) -> List[RetrievalResult]:
        results: List[RetrievalResult] = []
        for hit in hits or []:
            payload = hit.payload or {}
            citation = self._format_citation(
                filename=payload.get("filename", "unknown"),
                page=payload.get("page"),
            )
            results.append(
                RetrievalResult(
                    text=payload.get("text", ""),
                    score=float(hit.score or 0.0),
                    source=payload.get("source", ""),
                    filename=payload.get("filename", "unknown"),
                    page=payload.get("page"),
                    chunk_index=payload.get("chunk_index", 0),
                    from_ocr=payload.get("from_ocr", False),
                    ingested_at=self._parse_ingested_at(
                        payload.get("ingested_at"),
                        payload.get("ingested_at_ts"),
                    ),
                    citation=citation,
                )
            )
        return results

    def _dedupe_results(self, results: List[RetrievalResult]) -> List[RetrievalResult]:
        """Elimina duplicados por (filename,page,chunk_index) preservando orden/score."""
        seen = set()
        out: List[RetrievalResult] = []
        for r in results:
            key = (r.filename, r.page, r.chunk_index)
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out

    def _extract_query_ids(self, query: str) -> List[str]:
        tokens = re.findall(r"\b[A-Z]{1,8}-\d{2,8}\b", (query or "").upper())
        return list(dict.fromkeys(tokens))

    def _fetch_exact_id_matches(
        self,
        query_ids: List[str],
        collection_name: str,
        tenant_id: Optional[str] = None,
        source: Optional[str] = None,
        exclude_ocr: bool = False,
    ) -> List[RetrievalResult]:
        """
        Recupera chunks cuyo payload (filename/source) contiene el ID solicitado.
        Esto evita falsos negativos cuando la búsqueda semántica no prioriza el doc correcto.
        """
        base_must: List[FieldCondition] = []
        if tenant_id:
            base_must.append(FieldCondition(key=TENANT_CLAIM, match=MatchValue(value=tenant_id)))
        if source:
            base_must.append(FieldCondition(key="source", match=MatchValue(value=source)))
        if exclude_ocr:
            base_must.append(FieldCondition(key="from_ocr", match=MatchValue(value=False)))

        exact_results: List[RetrievalResult] = []
        seen_keys = set()

        def _add_point(point) -> None:
            payload = point.payload or {}
            key = (
                payload.get("filename", "unknown"),
                payload.get("page"),
                payload.get("chunk_index", 0),
            )
            if key in seen_keys:
                return
            seen_keys.add(key)
            exact_results.append(
                RetrievalResult(
                    text=payload.get("text", ""),
                    score=1.0,
                    source=payload.get("source", ""),
                    filename=payload.get("filename", "unknown"),
                    page=payload.get("page"),
                    chunk_index=payload.get("chunk_index", 0),
                    from_ocr=payload.get("from_ocr", False),
                    ingested_at=self._parse_ingested_at(
                        payload.get("ingested_at"),
                        payload.get("ingested_at_ts"),
                    ),
                    citation=self._format_citation(
                        payload.get("filename", "unknown"),
                        payload.get("page"),
                    ),
                )
            )

        def _payload_matches_token(payload: Dict, token: str) -> bool:
            filename = str(payload.get("filename", "") or "")
            source_val = str(payload.get("source", "") or "")
            text_val = str(payload.get("text", "") or "")[:400]
            return (
                self._has_exact_id(filename, token)
                or self._has_exact_id(source_val, token)
                or self._has_exact_id(text_val, token)
            )

        for token in query_ids:
            points = []
            try:
                scroll_filter = Filter(
                    must=base_must,
                    should=[
                        FieldCondition(key="filename", match=MatchText(text=token)),
                        FieldCondition(key="source", match=MatchText(text=token)),
                    ],
                )
                points, _ = self.client.scroll(
                    collection_name=collection_name,
                    scroll_filter=scroll_filter,
                    limit=20,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception as e:
                logger.warning(f"Exact ID fetch failed for '{token}' in {collection_name}: {e}")
                points = []

            # Fallback robusto: escaneo acotado de payload si MatchText no devuelve nada.
            if not points:
                try:
                    offset = None
                    scanned = 0
                    max_scan = 15000
                    batch_size = 512
                    while scanned < max_scan:
                        batch, next_offset = self.client.scroll(
                            collection_name=collection_name,
                            offset=offset,
                            limit=batch_size,
                            with_payload=True,
                            with_vectors=False,
                        )
                        if not batch:
                            break
                        scanned += len(batch)
                        for point in batch:
                            payload = point.payload or {}
                            if tenant_id and payload.get(TENANT_CLAIM) != tenant_id:
                                continue
                            if source and payload.get("source") != source:
                                continue
                            if exclude_ocr and payload.get("from_ocr") is True:
                                continue
                            if _payload_matches_token(payload, token):
                                points.append(point)
                        if len(points) >= 20 or not next_offset:
                            break
                        offset = next_offset
                except Exception as e:
                    logger.warning(
                        f"Fallback exact ID scan failed for '{token}' in {collection_name}: {e}"
                    )

            for point in points:
                _add_point(point)

        if exact_results:
            logger.info(
                f"Exact ID fetch: {len(exact_results)} chunks para IDs {query_ids} "
                f"en colección {collection_name}"
            )

        return exact_results

    def _has_exact_id(self, text: str, token: str) -> bool:
        if not text:
            return False
        pattern = rf"(?<![A-Z0-9]){re.escape(token.upper())}(?![A-Z0-9])"
        return re.search(pattern, text.upper()) is not None

    def _boost_exact_id_matches(
        self,
        results: List[RetrievalResult],
        query_ids: List[str],
    ) -> List[RetrievalResult]:
        def _boost(r: RetrievalResult) -> Tuple[int, float]:
            filename = (r.filename or "")
            source = (r.source or "")
            text = (r.text or "")[:500]

            fname_hits = sum(1 for t in query_ids if self._has_exact_id(filename, t))
            source_hits = sum(1 for t in query_ids if self._has_exact_id(source, t))
            text_hits = sum(1 for t in query_ids if self._has_exact_id(text, t))

            # Prioridad: filename > source > text > score original.
            priority = (fname_hits * 100) + (source_hits * 10) + text_hits
            return priority, float(r.score or 0.0)

        return sorted(results, key=_boost, reverse=True)

    # =============================== Helpers ===============================

    def _format_citation(self, filename: str, page: Optional[int]) -> str:
        """
        Formato de cita obligatorio: [filename.ext p.X]

        Ejemplos:
            - [contrato_2024.pdf p.3]
            - [presentacion.docx p.12]
            - [imagen_scan.jpg]  # Sin página
        """
        if page is not None:
            return f"[{filename.replace('[', '').replace(']', '')} p.{page}]"
        return f"[{filename.replace('[', '').replace(']', '')}]"

    def _parse_ingested_at(
        self,
        val: Optional[Union[str, int, float, datetime]],
        ts: Optional[Union[int, float]],
    ) -> datetime:
        """Acepta datetime, ISO string o epoch seconds (preferencia por 'ts' si existe)."""
        if ts is not None:
            try:
                return datetime.fromtimestamp(float(ts))
            except Exception:
                pass
        if isinstance(val, datetime):
            return val
        if isinstance(val, (int, float)):
            return datetime.fromtimestamp(float(val))
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except Exception:
                pass
        return datetime.utcnow()

    # =============================== Reranking MMR ===============================

    def mmr_rerank(
        self,
        query: str,
        results: List[RetrievalResult],
        lambda_mult: float = 0.5,
    ) -> List[RetrievalResult]:
        """
        Maximal Marginal Relevance reranking (usando coseno)
        Reduce redundancia manteniendo relevancia

        Args:
            query: Query original
            results: Resultados a reordenar
            lambda_mult: Balance relevancia/diversidad (0-1)

        Returns:
            Resultados reordenados
        """
        if len(results) <= 1:
            return results

        # Vectorizar y normalizar (coseno)
        query_vec = np.asarray(self.embedder.encode(query)[0], dtype=np.float32)
        qn = float(np.linalg.norm(query_vec) or 1.0)
        query_vec = query_vec / qn

        doc_vecs = np.asarray(
            self.embedder.encode([r.text for r in results]),
            dtype=np.float32,
        )
        norms = np.linalg.norm(doc_vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        doc_vecs = doc_vecs / norms

        selected: List[int] = []
        remaining = list(range(len(results)))

        # Seleccionar el más relevante primero
        similarities = (doc_vecs @ query_vec).tolist()
        first_idx = int(np.argmax(similarities))
        selected.append(first_idx)
        remaining.remove(first_idx)

        # Seleccionar el resto balanceando relevancia y diversidad
        while remaining:
            best_idx = None
            best_score = -1e9
            for idx in remaining:
                relevance = float(similarities[idx])
                max_sim_to_selected = max(float(doc_vecs[idx] @ doc_vecs[j]) for j in selected)
                mmr = lambda_mult * relevance - (1.0 - lambda_mult) * max_sim_to_selected
                if mmr > best_score:
                    best_score = mmr
                    best_idx = idx
            selected.append(best_idx)  # type: ignore[arg-type]
            remaining.remove(best_idx)  # type: ignore[arg-type]

        return [results[i] for i in selected]

    # =============================== Stats ===============================

    def get_collection_stats(self) -> Dict:
        """Estadísticas de la colección"""
        try:
            info = self.client.get_collection(self.collection_name)
            total_vectors = getattr(info, "vectors_count", None)
            total_points = getattr(info, "points_count", None)
            segments = getattr(info, "segments_count", None)
            status = getattr(info, "status", None)

            stats = {
                "collection_name": self.collection_name,
                "status": str(status),
                "segments": segments,
                "total_vectors": total_vectors,
                "total_points": total_points,
            }
            return stats
        except Exception as e:
            logger.error(f"Error obteniendo stats de colección '{self.collection_name}': {e}")
            return {"collection_name": self.collection_name, "error": str(e)}
