# backend/app/api/scrape.py
"""
Endpoints /scrape para web scraping con Playwright + trafilatura

Endpoints:
- POST /scrape - Scrapea e indexa en background
- POST /scrape/analyze - Scrapea y devuelve contenido sin indexar
"""

from __future__ import annotations

import logging
from typing import List, Optional
from enum import Enum

from fastapi import APIRouter, HTTPException, BackgroundTasks, Header
from pydantic import BaseModel, HttpUrl, Field

from app.integrations.scraper.playwright_scraper import WebScraper
from app.core.permissions import can_write_without_tenant_header, resolve_authorized_collections

logger = logging.getLogger(__name__)

# ============================================
# PROMETHEUS METRICS
# ============================================
from prometheus_client import Counter, Histogram, Gauge

# Contador de scrapes por status
web_scrapes_total = Counter(
    'web_scrapes_total',
    'Total de URLs scrapeadas',
    ['status', 'mode']  # success/error, index/analyze
)

# Duración del scraping
web_scrapes_duration_seconds = Histogram(
    'web_scrapes_duration_seconds',
    'Tiempo de scraping por URL',
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
)

# Errores de scraping por tipo
scrape_errors_total = Counter(
    'scrape_errors_total',
    'Errores de scraping',
    ['error_type']  # http_error, timeout, content_empty, etc.
)

router = APIRouter(prefix="/scrape", tags=["Scraping"])

# Instancia global del scraper (singleton)
_scraper: Optional[WebScraper] = None


def get_scraper() -> WebScraper:
    global _scraper
    if _scraper is None:
        _scraper = WebScraper(
            respect_robots=False,  # Desactivado para más flexibilidad
            timeout=30000,
            max_retries=3,
        )
    return _scraper


class ScrapeMode(str, Enum):
    INDEX = "index"      # Scrapea e indexa
    ANALYZE = "analyze"  # Solo devuelve contenido


class ScrapeRequest(BaseModel):
    url: HttpUrl
    tenant_id: Optional[str] = None
    mode: ScrapeMode = Field(default=ScrapeMode.INDEX, description="'index' para indexar, 'analyze' para solo devolver contenido")


class ScrapeResponse(BaseModel):
    status: str
    url: str
    title: Optional[str] = None
    content: Optional[str] = None  # Solo en modo analyze
    word_count: int = 0
    char_count: int = 0
    extraction_method: Optional[str] = None
    message: str


class AnalyzeResponse(BaseModel):
    """Respuesta detallada para modo analyze."""
    status: str
    url: str
    title: Optional[str] = None
    author: Optional[str] = None
    date: Optional[str] = None
    description: Optional[str] = None
    content: str
    word_count: int
    char_count: int
    extraction_method: str
    scraped_at: str


def _resolve_lookup_collections(
    x_tenant_id: Optional[str],
    x_tenant_ids: Optional[str],
    request_tenant_id: Optional[str],
) -> List[str]:
    from app.main import app_state

    return resolve_authorized_collections(
        qdrant_client=app_state.qdrant,
        x_tenant_id=x_tenant_id or request_tenant_id,
        x_tenant_ids=x_tenant_ids,
    )


def _scroll_url_points(collection: str, url: str, limit: int = 100, max_points: int = 1000):
    from app.main import app_state
    from qdrant_client.http import models

    points = []
    next_page = None

    while True:
        batch, next_page = app_state.qdrant.scroll(
            collection_name=collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="source",
                        match=models.MatchValue(value=url),
                    )
                ]
            ),
            limit=limit,
            offset=next_page,
            with_payload=True,
            with_vectors=False,
        )
        points.extend(batch)
        if not next_page or len(points) >= max_points:
            break

    return points


@router.post("", response_model=ScrapeResponse)
async def scrape_and_index(
    request: ScrapeRequest,
    background_tasks: BackgroundTasks,
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id"),
):
    """
    Scrapea una URL y la indexa en background.
    
    - Renderiza JavaScript con Chromium headless
    - Extrae contenido limpio con múltiples estrategias
    - Procesa e indexa como documento
    """
    url = str(request.url)
    tenant_id = x_tenant_id or request.tenant_id
    mode = request.mode

    if mode == ScrapeMode.INDEX and not x_tenant_id and not can_write_without_tenant_header():
        raise HTTPException(status_code=403, detail="X-Tenant-Id es obligatorio para indexar contenido web")
    
    import time
    start_time = time.time()
    
    try:
        scraper = get_scraper()
        
        # Scrape asíncrono
        result = await scraper.scrape(url)
        
        # Record duration
        duration = time.time() - start_time
        web_scrapes_duration_seconds.observe(duration)
        
        if not result:
            scrape_errors_total.labels(error_type="content_empty").inc()
            raise HTTPException(
                status_code=400,
                detail="No se pudo extraer contenido de la URL (bloqueado, error HTTP, o contenido vacío)"
            )
        
        content = result.get("content", "")
        title = result.get("title", "scraped_content")
        
        if len(content) < 50:
            scrape_errors_total.labels(error_type="content_insufficient").inc()
            raise HTTPException(
                status_code=400,
                detail="Contenido extraído insuficiente (menos de 50 caracteres)"
            )
        
        # Si modo es ANALYZE, devolver contenido directamente sin indexar
        if mode == ScrapeMode.ANALYZE:
            web_scrapes_total.labels(status="success", mode="analyze").inc()
            return ScrapeResponse(
                status="success",
                url=url,
                title=title,
                content=content,
                word_count=result.get("word_count", len(content.split())),
                char_count=result.get("char_count", len(content)),
                extraction_method=result.get("extraction_method"),
                message="Contenido extraído exitosamente",
            )
        
        # Modo INDEX: Indexar en background
        web_scrapes_total.labels(status="success", mode="index").inc()
        background_tasks.add_task(
            _index_scraped_content,
            url=url,
            title=title,
            content=content,
            metadata={
                "author": result.get("author"),
                "date": result.get("date"),
                "scraped_at": result.get("scraped_at"),
                "source": "web_scrape",
                "extraction_method": result.get("extraction_method"),
            },
            tenant_id=tenant_id,
        )
        
        return ScrapeResponse(
            status="processing",
            url=url,
            title=title,
            content=None,  # No devolver contenido en modo index
            word_count=result.get("word_count", 0),
            char_count=result.get("char_count", 0),
            extraction_method=result.get("extraction_method"),
            message="Contenido extraído y en cola para indexación",
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error scrapeando {url}: {e}")
        # scrape_errors_total.labels(error_type="exception").inc() # Optional
        raise HTTPException(status_code=500, detail=str(e))


class RecursiveScrapeRequest(BaseModel):
    url: HttpUrl
    # Límites defensivos: el crawler corre dentro del backend y un fan-out agresivo
    # puede saturar el contenedor con procesos de navegador.
    max_depth: int = Field(default=1, ge=1, le=3, description="Profundidad máxima de navegación (default: 1, max: 3)")
    max_pages: int = Field(default=5, ge=1, le=15, description="Máximo de páginas a scrapear por sitio (default: 5, max: 15)")
    tenant_id: Optional[str] = None


class RecursiveScrapeResponse(BaseModel):
    status: str
    base_url: str
    pages_initiated: int
    message: str


@router.post("/recursive", response_model=RecursiveScrapeResponse)
async def recursive_scrape(
    request: RecursiveScrapeRequest,
    background_tasks: BackgroundTasks,
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id"),
):
    """
    Crawling Recursivo: Scrapea la URL base y sigue enlaces internos.
    
    - Limitado por max_depth y max_pages
    - Solo sigue enlaces del mismo dominio
    - Indexa cada página encontrada individualmente
    """
    from app.integrations.scraper.recursive_scraper import RecursiveWebScraper
    
    url = str(request.url)
    tenant_id = x_tenant_id or request.tenant_id

    if not x_tenant_id and not can_write_without_tenant_header():
        raise HTTPException(status_code=403, detail="X-Tenant-Id es obligatorio para iniciar crawling indexado")
    
    # Iniciar crawling en background para no bloquear
    background_tasks.add_task(
        _run_recursive_crawl,
        url=url,
        max_depth=request.max_depth,
        max_pages=request.max_pages,
        tenant_id=tenant_id
    )
    
    return RecursiveScrapeResponse(
        status="processing",
        base_url=url,
        pages_initiated=request.max_pages, # Estimado/Límite
        message="Crawling iniciado en background"
    )

async def _run_recursive_crawl(url: str, max_depth: int, max_pages: int, tenant_id: Optional[str]):
    """Ejecuta el crawler y encola la indexación de cada resultado."""
    from app.integrations.scraper.recursive_scraper import RecursiveWebScraper
    
    try:
        crawler = RecursiveWebScraper(
            start_url=url,
            max_depth=max_depth,
            max_pages=max_pages
        )
        
        logger.info(f"🚀 Iniciando job de crawling para: {url}")
        results = await crawler.crawl()
        
        logger.info(f"✅ Crawl finalizado. Indexando {len(results)} páginas...")
        
        for res in results:
            try:
                content = res.get("content", "")
                if len(content) < 50:
                    continue
                    
                await _index_scraped_content(
                    url=res["url"],
                    title=res.get("title", "unknown"),
                    content=content,
                    metadata={
                        "author": res.get("author"),
                        "date": res.get("date"),
                        "scraped_at": res.get("scraped_at"),
                        "source": "recursive_crawl",
                        "depth": "unknown", # TODO: Crawler podría devolver depth
                        "extraction_method": res.get("extraction_method"),
                    },
                    tenant_id=tenant_id
                )
            except Exception as e:
                logger.error(f"Error indexando página crawleada {res.get('url')}: {e}")
                
    except Exception as e:
        logger.error(f"Error fatal en job de crawling recursivo para {url}: {e}")



@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze_url(
    request: ScrapeRequest,
):
    """
    Scrapea una URL y devuelve el contenido SIN indexar.
    
    Útil para:
    - Previsualizar contenido antes de indexar
    - Análisis rápido de páginas web
    - Resumir contenido directamente
    """
    url = str(request.url)
    
    try:
        scraper = get_scraper()
        result = await scraper.scrape(url)
        
        if not result:
            raise HTTPException(
                status_code=400,
                detail="No se pudo extraer contenido de la URL"
            )
        
        content = result.get("content", "")
        if len(content) < 50:
            raise HTTPException(
                status_code=400,
                detail="Contenido extraído insuficiente"
            )
        
        return AnalyzeResponse(
            status="success",
            url=url,
            title=result.get("title"),
            author=result.get("author"),
            date=result.get("date"),
            description=result.get("description"),
            content=content,
            word_count=result.get("word_count", len(content.split())),
            char_count=result.get("char_count", len(content)),
            extraction_method=result.get("extraction_method", "unknown"),
            scraped_at=result.get("scraped_at", ""),
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error analizando {url}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class CheckResponse(BaseModel):
    exists: bool
    url: str
    title: Optional[str] = None
    scraped_at: Optional[str] = None
    chunk_count: int = 0
    message: str

@router.post("/check", response_model=CheckResponse)
async def check_url_in_rag(
    request: ScrapeRequest,
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id"),
    x_tenant_ids: Optional[str] = Header(None, alias="X-Tenant-Ids"),
):
    """
    Verifica si una URL ya existe en las colecciones RAG autorizadas.
    Retorna metadatos si existe.
    """
    url = str(request.url)

    try:
        collections = _resolve_lookup_collections(
            x_tenant_id=x_tenant_id,
            x_tenant_ids=x_tenant_ids,
            request_tenant_id=request.tenant_id,
        )
        if not collections:
            return CheckResponse(
                exists=False,
                url=url,
                message="URL no encontrada en colecciones autorizadas",
            )

        for collection in collections:
            try:
                points = _scroll_url_points(collection=collection, url=url, limit=1, max_points=1)
            except Exception as e:
                logger.warning(f"Error checking URL {url} in collection {collection}: {e}")
                continue

            if not points:
                continue

            payload = points[0].payload
            return CheckResponse(
                exists=True,
                url=url,
                title=payload.get("filename") or payload.get("title"),
                scraped_at=payload.get("ingested_at") or payload.get("scraped_at"),
                message="URL encontrada en RAG",
            )

        return CheckResponse(
            exists=False,
            url=url,
            message="URL no encontrada en RAG",
        )

    except Exception as e:
        logger.error(f"Error checking URL {url}: {e}")
        return CheckResponse(exists=False, url=url, message=f"Error checking: {str(e)}")


class RetrieveResponse(BaseModel):
    status: str
    url: str
    title: Optional[str] = None
    content: str
    chunks_retrieved: int

@router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve_url_content(
    request: ScrapeRequest,
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id"),
    x_tenant_ids: Optional[str] = Header(None, alias="X-Tenant-Ids"),
):
    """
    Recupera y reconstruye el contenido de una URL desde las colecciones autorizadas.
    """
    url = str(request.url)

    try:
        collections = _resolve_lookup_collections(
            x_tenant_id=x_tenant_id,
            x_tenant_ids=x_tenant_ids,
            request_tenant_id=request.tenant_id,
        )
        if not collections:
            raise HTTPException(status_code=404, detail="URL no encontrada en colecciones autorizadas")

        for collection in collections:
            try:
                points = _scroll_url_points(collection=collection, url=url)
            except Exception as e:
                logger.warning(f"Error retrieving URL {url} from collection {collection}: {e}")
                continue

            if not points:
                continue

            sorted_points = sorted(points, key=lambda p: p.payload.get("chunk_index", 0))
            full_text = "\n\n".join([p.payload.get("text", "") for p in sorted_points])
            first_payload = sorted_points[0].payload

            return RetrieveResponse(
                status="success",
                url=url,
                title=first_payload.get("filename") or first_payload.get("title"),
                content=full_text,
                chunks_retrieved=len(points),
            )

        raise HTTPException(status_code=404, detail="URL no encontrada en documentos")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving content for {url}: {e}")
        raise HTTPException(status_code=500, detail=str(e))



async def _index_scraped_content(
    url: str,
    title: str,
    content: str,
    metadata: dict,
    tenant_id: Optional[str],
):
    """
    Indexa contenido scrapeado como si fuera un documento.
    """
    from datetime import datetime
    from sentence_transformers import SentenceTransformer
    import uuid
    import os
    from qdrant_client.models import PointStruct
    
    # Importar app_state
    from app.main import app_state
    
    logger.info(f"Indexando contenido scrapeado: {url}")
    
    try:
        # Chunking simple por párrafos
        chunks = []
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        
        chunk_size = int(os.getenv("CHUNK_SIZE", "500"))
        current = ""
        chunk_idx = 0
        
        for para in paragraphs:
            if len(current) + len(para) <= chunk_size:
                current += para + "\n\n"
            else:
                if current:
                    chunks.append({
                        "text": current.strip(),
                        "chunk_index": chunk_idx,
                    })
                    chunk_idx += 1
                current = para + "\n\n"
        
        if current:
            chunks.append({
                "text": current.strip(),
                "chunk_index": chunk_idx,
            })
        
        if not chunks:
            logger.warning(f"Sin chunks generados para {url}")
            return
        
        # Vectorizar
        # Vectorizar
        model_name = os.getenv("EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
        # Usar el embedder compartido que fuerza CPU y evita errores de CUDA
        from app.processing.embeddings.sentence_transformer import get_embedder, get_sparse_embedder
        embedder = get_embedder(model_name=model_name)

        texts = [c["text"] for c in chunks]
        # El wrapper ya devuelve numpy array normalizado
        embeddings = embedder.encode(texts)

        collection = (tenant_id or "webs").strip() or "webs"
        hybrid_ok = True
        exists = False
        try:
            collections = app_state.qdrant.get_collections().collections
            exists = any(c.name == collection for c in collections)
        except Exception as e:
            logger.warning(f"No se pudo listar colecciones Qdrant: {e}")

        if exists:
            try:
                info = app_state.qdrant.get_collection(collection)
                vectors = getattr(info.config.params, "vectors", None)
                sparse_vectors_cfg = getattr(info.config.params, "sparse_vectors", None)
                has_named_dense = isinstance(vectors, dict) and "dense" in vectors
                has_sparse = bool(sparse_vectors_cfg)
                if not has_named_dense or not has_sparse:
                    hybrid_ok = False
                    logger.warning(
                        "Colecci?n '%s' no est? configurada para b?squeda h?brida. Indexando solo dense.",
                        collection,
                    )
            except Exception:
                hybrid_ok = False
                logger.warning(
                    "No se pudo inspeccionar la colecci?n '%s'. Indexando dense-only.",
                    collection,
                )
        else:
            logger.info(f"Creando nueva colecci?n h?brida: {collection}")
            from qdrant_client.models import VectorParams, Distance, SparseVectorParams, Modifier
            app_state.qdrant.create_collection(
                collection_name=collection,
                vectors_config={"dense": VectorParams(size=len(embeddings[0]) if len(embeddings) > 0 else 384, distance=Distance.COSINE)},
                sparse_vectors_config={"sparse": SparseVectorParams(modifier=Modifier.IDF)},
            )
            hybrid_ok = True

        sparse_vectors = None
        if hybrid_ok:
            sparse_embedder = get_sparse_embedder()
            if sparse_embedder is None:
                logger.warning("Sparse embedder no disponible. Scrape se indexara en dense-only.")
                hybrid_ok = False
            else:
                sparse_gen = list(sparse_embedder.embed(texts))
                sparse_vectors = [{"indices": v.indices.tolist(), "values": v.values.tolist()} for v in sparse_gen]

        now = datetime.utcnow()
        points = []

        for idx, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            # Ajustar metadata para evitar sobreescribir 'source'
            safe_metadata = metadata.copy()
            if "source" in safe_metadata:
                safe_metadata["ingest_source"] = safe_metadata.pop("source")

            # ID determinista para evitar duplicados al reindexar la misma URL
            # Usamos UUID5 con namespace DNS y la combinaci?n URL+Index
            point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{url}_{chunk['chunk_index']}"))

            if hybrid_ok and sparse_vectors is not None:
                vector_payload = {"dense": embedding.tolist(), "sparse": sparse_vectors[idx]}
            else:
                vector_payload = embedding.tolist()

            points.append(
                PointStruct(
                    id=point_id,
                    vector=vector_payload,
                    payload={
                        "text": chunk["text"],
                        "filename": title or "scraped_content",
                        "source": url,  # URL real como source
                        "page": None,
                        "chunk_index": chunk["chunk_index"],
                        "from_ocr": False,
                        "ingested_at": now.isoformat() + "Z",
                        "ingested_at_ts": int(now.timestamp()),
                        "tenant_id": tenant_id or collection,
                        **safe_metadata,
                    }
                )
            )

        app_state.qdrant.upsert(
            collection_name=collection,
            points=points,
        )

        logger.info(
            f"Contenido indexado: {url} ({len(points)} chunks en '{collection}')"
        )

    except Exception as e:
        logger.error(f"Error indexando scrape de {url}: {e}")
