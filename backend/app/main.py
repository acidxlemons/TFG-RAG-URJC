import os
import sys
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

from qdrant_client import QdrantClient
from app.storage.qdrant_client import get_qdrant_client

# Core and State
from app.core.state import app_state, limiter
from app.core.rag.retriever import RAGRetriever
from app.core.rag.reranker import Reranker
from app.core.memory.manager import MemoryManager
from app.core.agent.base import RAGAgent
from app.processing.ocr.paddle_ocr import OCRPipeline
from app.processing.chunking.smart_chunker import SmartChunker
from app.integrations.sharepoint.client import SharePointClient

# Routers
from app.api import scrape as scrape_api
from app.api import search as search_api
from app.api import web_search
from app.api.routers.boe import router as boe_router
from app.api import chat
from app.api import documents
from app.api import base_search
from app.api import webhooks
from app.api import health
from app.api import query as query_api


# ======================================================
# LOGGING
# ======================================================

log_format = os.getenv("LOG_FORMAT", "text")
log_level = os.getenv("LOG_LEVEL", "INFO")

if log_format == "json":
    from pythonjsonlogger import jsonlogger

    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    handler.setFormatter(formatter)
    logging.basicConfig(handlers=[handler], level=log_level)
else:
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
logger = logging.getLogger(__name__)


# ======================================================
# HELPERS
# ======================================================

def _bool_env(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}


def _default_cors_origins() -> list[str]:
    configured = os.getenv("CORS_ALLOW_ORIGINS")
    if configured is not None:
        return [origin.strip() for origin in configured.split(",") if origin.strip()] or ["http://localhost"]

    origins: list[str] = []
    app_url = (os.getenv("APP_URL") or "").strip()
    if app_url:
        origins.append(app_url.rstrip("/"))

    origins.extend(
        [
            "http://localhost",
            "https://localhost",
            "http://127.0.0.1",
            "https://127.0.0.1",
        ]
    )
    return list(dict.fromkeys(origins))


# ======================================================
# LIFECYCLE
# ======================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicialización y limpieza de la aplicación RAG"""
    logger.info("🚀 Iniciando aplicación RAG...")

    # ===== Qdrant
    qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
    # Use shared Qdrant client with collection bootstrap
    app_state.qdrant = get_qdrant_client()
    logger.info(f"✓ Qdrant conectado: {qdrant_url}")

    # ===== Retriever
    default_tenant_id = os.getenv("DEFAULT_TENANT_ID")
    reranker = Reranker()
    app_state.retriever = RAGRetriever(
        qdrant_client=app_state.qdrant,
        collection_name=os.getenv("QDRANT_COLLECTION", "documents"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"),
        top_k=int(os.getenv("RAG_TOP_K", "5")),
        score_threshold=float(os.getenv("RAG_SCORE_THRESHOLD", "0.7")),
        tenant_id=default_tenant_id,
        reranker=reranker,
    )
    logger.info("✓ RAG Retriever inicializado")

    # ===== Memory Manager
    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        logger.warning("POSTGRES_URL no establecido. MemoryManager intentará conectarse con valor vacío.")
    app_state.memory = MemoryManager(database_url=postgres_url)
    logger.info("✓ Memory Manager inicializado")

    # ===== Agente clásico
    app_state.agent = RAGAgent(
        retriever=app_state.retriever,
        memory_manager=app_state.memory,
        llm_base_url=os.getenv("LITELLM_URL", "http://localhost:4000"),
        llm_api_key=os.getenv("LITELLM_API_KEY") or os.getenv("LITELLM_MASTER_KEY", "sk-1234"),
        model_name=os.getenv("LLM_MODEL", "JARVIS"),
        default_tenant_id=default_tenant_id,
    )
    logger.info("✓ RAG Agent inicializado")

    # ===== OCR Pipeline
    app_state.ocr_pipeline = OCRPipeline(
        num_workers=int(os.getenv("OCR_NUM_WORKERS", "2")),
        use_gpu=_bool_env("OCR_USE_GPU", True),
    )
    logger.info("✓ OCR Pipeline inicializado")

    # ===== Chunker
    app_state.chunker = SmartChunker(
        chunk_size=int(os.getenv("CHUNK_SIZE", "500")),
        overlap=int(os.getenv("CHUNK_OVERLAP", "50")),
    )
    logger.info("✓ Smart Chunker inicializado")

    # ===== SharePoint (opcional)
    app_state.sharepoint = None
    if os.getenv("SHAREPOINT_TENANT_ID"):
        app_state.sharepoint = SharePointClient(
            tenant_id=os.getenv("SHAREPOINT_TENANT_ID"),
            client_id=os.getenv("SHAREPOINT_CLIENT_ID"),
            client_secret=os.getenv("SHAREPOINT_CLIENT_SECRET"),
            site_id=os.getenv("SHAREPOINT_SITE_ID"),
            folder_path=os.getenv("SHAREPOINT_FOLDER_PATH", "Documents"),
        )
        logger.info("✓ SharePoint Client inicializado")

    logger.info("✅ Aplicación lista (Refactorizada y Modular)")
    yield


# ======================================================
# APP FASTAPI
# ======================================================

app = FastAPI(
    title="JARVIS RAG System API",
    version="2.0.0",
    description="API RAG modular y refactorizada con soporte híbrido",
    lifespan=lifespan,
)

# Rate Limiting Global Middlewares
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
allow_origins = _default_cors_origins()
allow_credentials = "*" not in allow_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ======================================================
# ROUTER IMPORTS
# ======================================================

# Router de Web Scraping
app.include_router(scrape_api.router)

# Enrutador heredado (Legacy raw Qdrant search)
app.include_router(base_search.router)

# Nuevo Endpoint RAG Híbrido Avanzado
app.include_router(search_api.router, tags=["Search V2"])

# DuckDuckGo Búsqueda Genérica
app.include_router(web_search.router, tags=["Web Search"])

# Legislación BOE
app.include_router(boe_router, prefix="/external/boe", tags=["BOE"])

# Chat RAG Principal
app.include_router(chat.router)

# Almacenamiento, indexación y listado de documentos Qdrant
app.include_router(documents.router)

# Webhooks de ingestión pasiva (SharePoint)
app.include_router(webhooks.router)

# Health Checks
app.include_router(health.router)

# Consultas unificadas RAG + SQL
app.include_router(query_api.router)

@app.get("/", tags=["System"])
async def root():
    """Root endpoint"""
    return {
        "name": "JARVIS RAG System API",
        "version": "2.0.0",
        "docs": "/docs",
    }
