# backend/app/schemas/chat.py
"""
Esquemas Pydantic para el endpoint /chat y /chat/stream.

Estos modelos definen la estructura de las peticiones y respuestas del sistema
de chat del sistema JARVIS RAG. Se usan tanto en el endpoint síncrono (/chat)
como en el endpoint de streaming (/chat/stream).
"""

from typing import List, Optional, Union
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """
    Petición de chat.

    Campos:
    - message: Texto del mensaje del usuario (máximo 4000 caracteres).
    - conversation_id: ID de la conversación (para mantener contexto entre mensajes).
    - user_id: ID numérico del usuario (legacy, de la base de datos).
    - azure_id: ID de Azure AD del usuario (usado para SSO corporativo).
    - email: Email del usuario.
    - name: Nombre del usuario.
    - mode: Modo de operación:
        - "chat": Conversación normal sin búsqueda en documentos.
        - "rag": Activa búsqueda en documentos corporativos vía Qdrant.
        - "ocr": Placeholder para procesamiento OCR (actualmente similar a chat).

    El sistema también detecta automáticamente el modo basándose en la query:
    - Si contiene una URL → modo scraping (scrapea y resume la web).
    - Si contiene "busca en internet" → modo web search.
    - Si contiene "busca en documentos" → modo RAG (prioridad sobre el campo mode).
    """
    message: str = Field(default="", min_length=0, max_length=4000)
    conversation_id: Optional[Union[str, int]] = None
    user_id: Optional[int] = None
    azure_id: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    mode: str = "chat"  # "chat" | "rag" | "ocr"


class ChatResponse(BaseModel):
    """
    Respuesta de chat.

    Campos:
    - content: Texto de la respuesta generada por el LLM.
    - sources: Lista de fuentes usadas (documentos RAG, webs, etc.).
    - conversation_id: ID de la conversación asociada.
    - has_citations: True si la respuesta incluye citas documentales.
    - tokens_used: Número total de tokens consumidos por el LLM.
    - processing_time: Tiempo de procesamiento en segundos.
    """
    content: str
    sources: List[dict]
    conversation_id: Union[str, int]
    has_citations: bool
    tokens_used: int
    processing_time: float
