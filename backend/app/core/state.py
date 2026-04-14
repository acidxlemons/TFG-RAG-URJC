import os
from typing import Optional
from openai import OpenAI
from qdrant_client import QdrantClient
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.rag.retriever import RAGRetriever
from app.core.memory.manager import MemoryManager
from app.core.agent.base import RAGAgent
from app.processing.ocr.paddle_ocr import OCRPipeline
from app.processing.chunking.smart_chunker import SmartChunker
from app.integrations.sharepoint.client import SharePointClient

# ======================================================
# CONFIG & LIMITER
# ======================================================
rate_limit = os.getenv("RATE_LIMIT", "20/minute")
limiter = Limiter(key_func=get_remote_address, default_limits=[rate_limit])

# ======================================================
# CLIENTE LLM (via LiteLLM)
# ======================================================
LITELLM_BASE_URL = os.getenv("LITELLM_URL", "http://localhost:4000").rstrip("/")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY") or os.getenv("LITELLM_MASTER_KEY", "sk-1234")
LLM_MODEL = os.getenv("LLM_MODEL", "JARVIS")

llm_client = OpenAI(
    base_url=f"{LITELLM_BASE_URL}/v1",
    api_key=LITELLM_API_KEY,
)

# ======================================================
# ESTADO GLOBAL
# ======================================================
class AppState:
    """Estado compartido de la aplicación"""
    qdrant: QdrantClient
    retriever: RAGRetriever
    memory: MemoryManager
    agent: RAGAgent
    ocr_pipeline: OCRPipeline
    chunker: SmartChunker
    sharepoint: Optional[SharePointClient]

app_state = AppState()
