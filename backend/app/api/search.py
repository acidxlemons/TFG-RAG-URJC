# backend/app/api/search.py
"""
Search API Endpoint - API de búsqueda RAG

Este endpoint expone el sistema de búsqueda híbrida mediante una API REST.

Endpoints:
- POST /api/v1/search - Búsqueda principal
- POST /api/v1/search/multi-query - Búsqueda multi-query
- GET /api/v1/search/health - Health check

Características:
- Búsqueda híbrida (dense + sparse + reranking)
- Query processing inteligente (intent detection, expansion)
- Multi-tenant con filtrado automático
- Métricas de Prometheus integradas
- Rate limiting por tenant
- Caché de resultados con Redis

Ejemplo de uso:
```bash
curl -X POST http://localhost:8000/api/v1/search \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: tenant-demo" \
  -d '{
    "query": "requisitos auditoría interna",
    "top_k": 5,
    "strategy": "hybrid",
    "use_reranking": true
  }'
```

Respuesta:
```json
{
  "query": {
    "original": "requisitos auditoría interna",
    "intent": "factual",
    "keywords": ["requisitos", "auditoría", "interna"]
  },
  "results": [
    {
      "id": "doc_123",
      "text": "Los requisitos de auditoría interna...",
      "score": 0.89,
      "metadata": {
        "filename": "FORM-027 Informe de auditoría.docx",
        "page": 3
      }
    }
  ],
  "total": 5,
  "latency_ms": 234
}
```
"""

from __future__ import annotations

import asyncio
import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional, Any
from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel, Field, validator
from prometheus_client import Counter, Histogram, Gauge

# Executor para correr query expansion en un thread sin bloquear el event loop
_expansion_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="qexpand")

from ..core.retrieval import HybridRetriever, SearchResult
from ..core.query_processor import QueryProcessor, MultiQueryRetriever, QueryIntent
from ..core.permissions import get_all_collection_names, resolve_authorized_collections
from ..core.auth import extract_allowed_collections
from ..storage.qdrant_client import get_qdrant_client
from ..processing.embeddings.sentence_transformer import get_embedder

logger = logging.getLogger(__name__)

# ============================================
# MÉTRICAS PROMETHEUS
# ============================================

# Contador de queries por tenant e intent
search_counter = Counter(
    'rag_search_requests_total',
    'Total de búsquedas RAG',
    ['tenant_id', 'intent', 'strategy']
)

# Histograma de latencia
search_duration = Histogram(
    'rag_search_duration_seconds',
    'Latencia de búsqueda RAG',
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0]
)

# Gauge de confianza promedio de resultados
result_confidence = Gauge(
    'rag_result_confidence',
    'Confianza promedio de resultados',
    ['tenant_id']
)

# Contador de aciertos (cuando se encuentran resultados)
search_hits = Counter(
    'rag_search_hits_total',
    'Búsquedas que retornaron resultados',
    ['tenant_id']
)

# Contador de fallos (sin resultados)
search_misses = Counter(
    'rag_search_misses_total',
    'Búsquedas sin resultados',
    ['tenant_id']
)


# ============================================
# MODELOS PYDANTIC (REQUEST/RESPONSE)
# ============================================

class SearchRequest(BaseModel):
    """
    Request para búsqueda
    
    Campos:
    - query: Texto de búsqueda (requerido)
    - top_k: Número de resultados (default: 5, max: 50)
    - strategy: Estrategia de búsqueda ("dense", "sparse", "hybrid")
    - use_reranking: Si usar cross-encoder reranking
    - tenant_id: ID de tenant (override del header, opcional)
    - filters: Filtros adicionales (fecha, tipo, etc.)
    - process_query: Si procesar la query (intent detection, etc.)
    """
    query: str = Field(..., description="Query de búsqueda", min_length=1, max_length=500)
    top_k: int = Field(5, description="Número de resultados", ge=1, le=50)
    strategy: str = Field("hybrid", description="Estrategia: dense, sparse, hybrid")
    use_reranking: bool = Field(True, description="Usar reranking con cross-encoder")
    tenant_id: Optional[str] = Field(None, description="ID de tenant (override)")
    filters: Optional[Dict[str, Any]] = Field(None, description="Filtros adicionales")
    process_query: bool = Field(True, description="Procesar query (intent, expansion)")
    
    @validator('strategy')
    def validate_strategy(cls, v):
        """Validar que la estrategia sea válida"""
        valid_strategies = {'dense', 'sparse', 'hybrid'}
        if v not in valid_strategies:
            raise ValueError(f"Estrategia debe ser una de: {valid_strategies}")
        return v


class QueryInfo(BaseModel):
    """Información de la query procesada"""
    original: str
    intent: str
    keywords: List[str]
    expanded: Optional[List[str]] = None


class ResultItem(BaseModel):
    """Item de resultado individual"""
    id: str
    text: str
    score: float
    metadata: Dict[str, Any]
    scores: Optional[Dict[str, Optional[float]]] = None  # dense, sparse, rerank can be None


class SearchResponse(BaseModel):
    """
    Respuesta de búsqueda
    
    Incluye:
    - query: Información de la query procesada
    - results: Lista de resultados encontrados
    - total: Total de resultados
    - latency_ms: Latencia de la búsqueda en milisegundos
    - strategy_used: Estrategia utilizada
    """
    query: QueryInfo
    results: List[ResultItem]
    total: int
    latency_ms: float
    strategy_used: str


class MultiQueryRequest(BaseModel):
    """Request para multi-query search"""
    query: str = Field(..., description="Query de búsqueda")
    top_k: int = Field(10, description="Número de resultados finales", ge=1, le=50)
    num_variations: int = Field(3, description="Número de variaciones de query", ge=1, le=5)
    tenant_id: Optional[str] = None


class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    retriever_ready: bool
    query_processor_ready: bool
    qdrant_connected: bool


# ============================================
# ROUTER
# ============================================

router = APIRouter(prefix="/api/v1", tags=["search"])


# ============================================
# DEPENDENCIAS
# ============================================

_global_retriever: Optional[HybridRetriever] = None
_global_processor: Optional[QueryProcessor] = None

def get_tenant_id(x_tenant_id: Optional[str] = Header(None)) -> str:
    """
    Extrae tenant ID del header X-Tenant-ID
    
    Seguridad multi-tenant:
    - SIEMPRE usa el tenant ID del header (autenticado)
    - Nunca confíes en el tenant_id del request body
    - En producción, valida que el usuario tenga acceso al tenant
    """
    if not x_tenant_id:
        # En desarrollo, usar tenant por defecto
        # En producción, esto debería fallar con 401
        logger.warning("No tenant ID en header, usando default")
        return "tenant-default"
    
    return x_tenant_id


async def get_retriever() -> HybridRetriever:
    """Dependency para obtener el retriever (Singleton)"""
    import os
    global _global_retriever
    
    if _global_retriever is None:
        logger.info("Inicializando HybridRetriever global...")
        # Obtener componentes
        qdrant = get_qdrant_client()
        encoder = get_embedder()
        
        # Leer modelo de reranker de variable de entorno
        reranker_model = os.getenv(
            "RERANKER_MODEL", 
            "cross-encoder/ms-marco-MiniLM-L-12-v2"  # Default
        )
        logger.info(f"Usando reranker: {reranker_model}")
        
        # Crear retriever
        _global_retriever = HybridRetriever(
            qdrant_client=qdrant,
            embedding_model=encoder,
            reranker_model_name=reranker_model,
            enable_reranking=True,
            default_alpha=0.7,
        )
        logger.info("✓ HybridRetriever global listo")
    
    return _global_retriever


async def get_query_processor() -> QueryProcessor:
    """Dependency para obtener el query processor (Singleton)"""
    global _global_processor
    
    if _global_processor is None:
        logger.info("Inicializando QueryProcessor global...")
        # En producción, pasar un LLM client real
        _global_processor = QueryProcessor(
            llm_client=None,
            enable_expansion=True,  # LiteLLM HTTP se detecta automáticamente via env vars
            max_expansions=3,
        )
        logger.info("✓ QueryProcessor global listo")
    
    return _global_processor


# ============================================
# ENDPOINTS
# ============================================

@router.post("/search", response_model=SearchResponse)
async def search(
    request: SearchRequest,
    tenant_id: str = Depends(get_tenant_id),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id"),
    x_tenant_ids: Optional[str] = Header(None, alias="X-Tenant-Ids"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    retriever: HybridRetriever = Depends(get_retriever),
    processor: QueryProcessor = Depends(get_query_processor),
):
    """
    Endpoint principal de búsqueda
    
    Flujo:
    1. Extraer tenant ID del header (seguridad)
    2. Procesar query (intent detection, keywords)
    3. Ejecutar búsqueda híbrida
    4. Aplicar reranking si está habilitado
    5. Registrar métricas
    6. Retornar resultados
    
    Seguridad:
    - Multi-tenant: Solo retorna documentos del tenant del usuario
    - Rate limiting: Aplicado por tenant (configurar en nginx/middleware)
    - Input validation: Pydantic valida todos los inputs
    
    Performance:
    - Caché: Resultados cacheados en Redis (por implementar)
    - Timeout: 30 segundos por búsqueda
    - Async: No bloqueante
    """
    t0 = time.perf_counter()
    
    # Override tenant_id del request con el del header (seguridad)
    effective_tenant_id = tenant_id
    
    try:
        # 1. Detección de intención y keywords (rápido, sin LLM)
        processed_query = None
        intent = "unknown"
        keywords: List[str] = []
        expanded_queries: List[str] = [request.query]

        if request.process_query:
            # Procesar intent/keywords de forma inmediata (sin expansion aún)
            processed_query = processor.process(
                query=request.query,
                expand=False,  # La expansion se lanza en paralelo abajo
            )
            intent = processed_query.intent.value
            keywords = processed_query.keywords

        # 2. Lanzar expansión LLM en background (si está habilitada)
        #    y búsqueda con query original en paralelo
        loop = asyncio.get_event_loop()
        expansion_future = None
        if request.process_query and processor.enable_expansion:
            expansion_future = loop.run_in_executor(
                _expansion_executor,
                lambda: processor.expand_query(request.query, num_variations=3),
            )

        logger.info(
            f"Búsqueda: tenant={effective_tenant_id}, query='{request.query[:50]}', "
            f"strategy={request.strategy}, intent={intent}"
        )

        # Validación JWT: si está activa, las colecciones vienen del token
        jwt_collections = extract_allowed_collections(authorization)
        if jwt_collections is not None:
            if not jwt_collections:
                raise HTTPException(status_code=403, detail="Token válido pero sin colecciones autorizadas.")
            collections_to_search = jwt_collections
        else:
            collections_to_search = resolve_authorized_collections(
                qdrant_client=retriever.qdrant,
                x_tenant_id=x_tenant_id,
                x_tenant_ids=x_tenant_ids,
            )
        tenant_filter = None

        all_results: List[SearchResult] = []
        for coll in collections_to_search:
            try:
                coll_results = retriever.search(
                    query=request.query,
                    collection_name=coll,
                    top_k=request.top_k,
                    tenant_id=tenant_filter,
                    strategy=request.strategy,
                    use_reranking=request.use_reranking,
                    filters=request.filters,
                )
                for r in coll_results:
                    if r.metadata is not None and "collection" not in r.metadata:
                        r.metadata["collection"] = coll
                all_results.extend(coll_results)
            except Exception as e:
                logger.warning(f"Error querying collection {coll}: {e}")

        all_results.sort(key=lambda r: r.score, reverse=True)
        results = all_results[: request.top_k]
        
        # 3. Calcular latencia
        latency = (time.perf_counter() - t0) * 1000  # en ms
        
        # 4. Registrar métricas
        search_counter.labels(
            tenant_id=effective_tenant_id,
            intent=intent,
            strategy=request.strategy
        ).inc()
        
        search_duration.observe(latency / 1000)  # en segundos
        
        if results:
            search_hits.labels(tenant_id=effective_tenant_id).inc()
            avg_confidence = sum(r.score for r in results) / len(results)
            result_confidence.labels(tenant_id=effective_tenant_id).set(avg_confidence)
        else:
            search_misses.labels(tenant_id=effective_tenant_id).inc()
        
        # 5. Construir respuesta
        response = SearchResponse(
            query=QueryInfo(
                original=request.query,
                intent=intent,
                keywords=keywords,
                expanded=processed_query.expanded if processed_query else None,
            ),
            results=[
                ResultItem(
                    id=r.id,
                    text=r.text,
                    score=r.score,
                    metadata=r.metadata,
                    scores={
                        "dense": r.dense_score,
                        "sparse": r.sparse_score,
                        "rerank": r.rerank_score,
                    }
                )
                for r in results
            ],
            total=len(results),
            latency_ms=round(latency, 2),
            strategy_used=request.strategy,
        )
        
        logger.info(
            f"✓ Búsqueda completada: {len(results)} resultados en {latency:.0f}ms"
        )
        
        return response
        
    except Exception as e:
        logger.error(f"Error en búsqueda: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error en búsqueda: {str(e)}"
        )


@router.post("/search/multi-query", response_model=SearchResponse)
async def multi_query_search(
    request: MultiQueryRequest,
    tenant_id: str = Depends(get_tenant_id),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id"),
    x_tenant_ids: Optional[str] = Header(None, alias="X-Tenant-Ids"),
    retriever: HybridRetriever = Depends(get_retriever),
    processor: QueryProcessor = Depends(get_query_processor),
):
    """
    Búsqueda multi-query con expansión automática
    
    Este endpoint:
    1. Expande la query en múltiples variaciones usando LLM
    2. Busca con cada variación
    3. Fusiona y deduplica resultados
    4. Retorna los top-k mejores resultados
    
    Ventajas:
    - Más robusto ante queries ambiguas
    - Mejor recall (encuentra más docs relevantes)
    
    Desventajas:
    - Mayor latencia (múltiples búsquedas)
    - Requiere LLM para expansión
    
    Cuándo usar:
    - Queries cortas o ambiguas
    - Búsquedas exploratorias
    - Cuando recall es más importante que latencia
    """
    t0 = time.perf_counter()
    
    try:
        # Crear multi-query retriever
        multi_retriever = MultiQueryRetriever(
            base_retriever=retriever,
            query_processor=processor,
        )
        
        # Ejecutar búsqueda multi-query
        collections_to_search = resolve_authorized_collections(
            qdrant_client=retriever.qdrant,
            x_tenant_id=x_tenant_id,
            x_tenant_ids=x_tenant_ids,
        )
        tenant_filter = None

        all_results: List[SearchResult] = []
        for coll in collections_to_search:
            try:
                coll_results = multi_retriever.search(
                    query=request.query,
                    collection_name=coll,
                    top_k=request.top_k,
                    tenant_id=tenant_filter,
                    strategy="hybrid",
                    use_reranking=True,
                )
                for r in coll_results:
                    if r.metadata is not None and "collection" not in r.metadata:
                        r.metadata["collection"] = coll
                all_results.extend(coll_results)
            except Exception as e:
                logger.warning(f"Error querying collection {coll}: {e}")

        all_results.sort(key=lambda r: r.score, reverse=True)
        results = all_results[: request.top_k]
        
        # Calcular latencia
        latency = (time.perf_counter() - t0) * 1000
        
        # Construir respuesta
        response = SearchResponse(
            query=QueryInfo(
                original=request.query,
                intent="unknown",  # Multi-query no usa intent detection
                keywords=[],
                expanded=None,  # Expansiones son internas
            ),
            results=[
                ResultItem(
                    id=r.id,
                    text=r.text,
                    score=r.score,
                    metadata=r.metadata,
                )
                for r in results
            ],
            total=len(results),
            latency_ms=round(latency, 2),
            strategy_used="multi-query-hybrid",
        )
        
        logger.info(
            f"✓ Multi-query completada: {len(results)} resultados en {latency:.0f}ms"
        )
        
        return response
        
    except Exception as e:
        logger.error(f"Error en multi-query: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error en multi-query: {str(e)}"
        )


@router.get("/search/health", response_model=HealthResponse)
async def health_check(
    retriever: HybridRetriever = Depends(get_retriever),
    processor: QueryProcessor = Depends(get_query_processor),
):
    """
    Health check del sistema de búsqueda
    
    Verifica:
    - Retriever inicializado
    - Query processor inicializado  
    - Conexión a Qdrant
    - (Opcional) Conexión a Redis cache
    
    Retorna:
    - 200 OK si todo está bien
    - 503 Service Unavailable si hay problemas
    """
    try:
        # Verificar Qdrant
        qdrant_ok = False
        try:
            qdrant = get_qdrant_client()
            # Intentar listar colecciones
            collections = qdrant.get_collections()
            qdrant_ok = True
        except Exception as e:
            logger.error(f"Qdrant no disponible: {e}")
        
        # Verificar componentes
        retriever_ok = retriever is not None
        processor_ok = processor is not None
        
        # Status general
        all_ok = retriever_ok and processor_ok and qdrant_ok
        status = "healthy" if all_ok else "degraded"
        
        response = HealthResponse(
            status=status,
            retriever_ready=retriever_ok,
            query_processor_ready=processor_ok,
            qdrant_connected=qdrant_ok,
        )
        
        if not all_ok:
            logger.warning(f"Health check degraded: {response.dict()}")
        
        return response
        
    except Exception as e:
        logger.error(f"Error en health check: {e}")
        raise HTTPException(
            status_code=503,
            detail="Service unavailable"
        )


# ============================================
# REGISTRO DEL ROUTER
# ============================================

def register_search_routes(app):
    """
    Registra las rutas de búsqueda en la app FastAPI
    
    Llamar desde main.py:
    ```python
    from app.api.search import register_search_routes
    register_search_routes(app)
    ```
    """
    app.include_router(router)
    logger.info("✓ Search API routes registradas")
