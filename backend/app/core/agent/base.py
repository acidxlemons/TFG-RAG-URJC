# backend/app/core/agent/base.py
"""
Agente RAG con LangChain
Sistema de citas obligatorio y memoria conversacional
"""

from __future__ import annotations

from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime
from contextvars import ContextVar
from zoneinfo import ZoneInfo

# LangChain (compatible con LiteLLM/OpenAI)
# Nota: en tu requirements usas langchain-openai==0.0.5
from langchain_openai import ChatOpenAI
from langchain.agents import AgentExecutor, create_openai_functions_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.schema import HumanMessage, AIMessage, SystemMessage
from langchain.tools import Tool

# Componentes locales
from ..rag.retriever import RAGRetriever, RetrievalResult
from ..memory.manager import MemoryManager, ConversationContext
from ..permissions import get_all_collection_names
from ...integrations.boe_client import BoeClient

logger = logging.getLogger(__name__)

# Acepta extensiones comunes en mayúsculas/minúsculas y permite espacios
CITATION_REGEX_VISIBLE = r'\[([^\]]+\.(?:pdf|docx|pptx|xlsx|txt|csv|jpg|jpeg|png))\s*(?:p\.(\d+))?\]'
TENANT_CLAIM = "tenant_id"
CURRENT_TENANT: ContextVar[Optional[str]] = ContextVar("rag_agent_current_tenant", default=None)
CURRENT_COLLECTIONS: ContextVar[Tuple[str, ...]] = ContextVar("rag_agent_current_collections", default=())


@dataclass
class AgentResponse:
    """Respuesta del agente con trazabilidad"""
    content: str
    sources: List[Dict]  # Documentos citados
    conversation_id: int
    tokens_used: int
    processing_time: float
    has_citations: bool


class RAGAgent:
    """
    Agente RAG Empresarial

    Características:
    1. RAG con búsqueda semántica en Qdrant
    2. Memoria conversacional por usuario (Postgres)
    3. Sistema de citas OBLIGATORIO
    4. Tools extensibles (web scraping, etc.)
    5. Resúmenes automáticos de conversaciones largas
    """

    # ==================== PROMPT SYSTEM ====================

    SYSTEM_PROMPT = """Eres un asistente de IA empresarial especializado en análisis de documentos.

REGLAS CRÍTICAS QUE DEBES SEGUIR SIEMPRE:

1. CITAS OBLIGATORIAS:
   - TODA información extraída de documentos DEBE incluir cita en formato: [nombre_archivo.ext p.X]
   - Ejemplo: "El contrato tiene duración de 12 meses [contrato_2024.pdf p.3]"
   - Si la información proviene de múltiples documentos, cita todos
   - NUNCA inventes citas ni nombres de archivos

2. TRANSPARENCIA:
   - Si no encuentras información en los documentos, dilo claramente
   - Indica cuando necesitas más contexto o documentos específicos
   - Si la confianza en la información es baja, menciónalo

3. CONTEXTO CONVERSACIONAL:
   - Ten en cuenta la conversación anterior del usuario
   - Si el usuario se refiere a “el otro contrato”, intenta identificarlo por el contexto previo

4. ESTRUCTURA DE RESPUESTAS:
   - Respuestas concisas pero completas
   - Usa viñetas para listas
   - Destaca información crítica
   - Sugiere próximos pasos cuando sea útil

5. DOCUMENTOS OCR:
   - Algunos documentos fueron procesados con OCR y pueden contener errores menores
   - Si algo parece extraño, adviértelo brevemente

IMPORTANTE:
- NO agregues información que no esté en el contexto recuperado.
- Si no hay evidencia suficiente, di “no encontrado en las fuentes”.
"""

    def __init__(
        self,
        retriever: RAGRetriever,
        memory_manager: MemoryManager,
        llm_base_url: Optional[str],
        llm_api_key: Optional[str],
        model_name: str = "JARVIS",
        temperature: float = 0.1,
        max_context_docs: int = 5,
        default_tenant_id: Optional[str] = None,
        **kwargs
    ):
        """
        Args:
            retriever: RAG retriever configurado
            memory_manager: Gestor de memoria conversacional
            llm_base_url: URL de LiteLLM/OpenAI-compatible
            llm_api_key: API key
            model_name: Nombre del modelo (en LiteLLM)
            temperature: Temperatura del modelo
            max_context_docs: Máximo de documentos en contexto
            default_tenant_id: tenant por defecto (si aplica multi-tenant)
        """
        self.retriever = retriever
        self.memory = memory_manager
        self.max_context_docs = max_context_docs
        self.default_tenant_id = default_tenant_id

        # Mantenemos las herramientas extra inyectadas
        self._extra_tools = kwargs.get("mcp_tools", [])

        # Inicializar LLM
        logger.info(f"Inicializando LLM: {model_name} via {llm_base_url}")
        self.llm = ChatOpenAI(
            base_url=llm_base_url or os.getenv("LITELLM_URL") or os.getenv("OPENAI_BASE_URL"),
            api_key=llm_api_key or os.getenv("LITELLM_API_KEY") or os.getenv("OPENAI_API_KEY"),
            model=model_name,
            temperature=temperature,
            streaming=False,  
        )

        # Inicializar cliente BOE
        self.boe_client = BoeClient()

        # Crear agente con tools
        self.agent = self._create_agent()
        logger.info("RAG Agent inicializado")

    # =============== Creación del agente y tools ===============

    def _create_agent(self) -> AgentExecutor:
        """
        Configura el agente con una tool principal de búsqueda RAG.
        """

        search_tool = Tool(
            name="search_documents",
            description=(
                "Busca información en documentos de la empresa (RAG sobre Qdrant). "
                "Úsala cuando el usuario pregunte sobre contratos, políticas, informes, etc. "
                "Entrada: consulta en lenguaje natural. Salida: fragmentos relevantes con citas."
            ),
            func=self._search_documents_tool,
        )

        boe_tool = Tool(
            name="search_legislation",
            description=(
                "Busca legislación española en el BOE (Boletín Oficial del Estado). "
                "Úsala cuando el usuario pregunte sobre leyes, reales decretos, normas o regulaciones públicas. "
                "Entrada: keywords de búsqueda (ej: 'protección datos', 'teletrabajo'). "
                "Salida: lista de normas encontradas con su resumen y enlace."
            ),
            func=self._search_legislation_tool,
        )

        tools = [search_tool, boe_tool] + self._extra_tools

        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", self.SYSTEM_PROMPT),
                MessagesPlaceholder(variable_name="chat_history", optional=True),
                ("human", "{input}"),
                MessagesPlaceholder(variable_name="agent_scratchpad"),
            ]
        )

        agent = create_openai_functions_agent(
            llm=self.llm,
            tools=tools,
            prompt=prompt,
        )

        return AgentExecutor(
            agent=agent,
            tools=tools,
            verbose=False,
            max_iterations=3,
            handle_parsing_errors=True,
            return_intermediate_steps=False,
        )

    def _get_all_doc_collections(self) -> List[str]:
        return get_all_collection_names(self.retriever.client)

    def _resolve_target_collections(self) -> List[str]:
        requested = [c for c in CURRENT_COLLECTIONS.get() if isinstance(c, str) and c.strip()]
        if requested:
            return list(dict.fromkeys(requested))

        tenant = CURRENT_TENANT.get() or self.default_tenant_id
        search_all = os.getenv("RAG_SEARCH_ALL_COLLECTIONS", "false").lower() in {"1", "true", "yes", "y"}
        allow_wildcard = os.getenv("RAG_ALLOW_WILDCARD_COLLECTIONS", "false").lower() in {"1", "true", "yes", "y"}
        allow_global_no_headers = os.getenv("RAG_ALLOW_GLOBAL_WITHOUT_HEADERS", "false").lower() in {"1", "true", "yes", "y"}

        if tenant and tenant not in {"*", "all"}:
            return [tenant]
        if tenant in {"*", "all"}:
            return self._get_all_doc_collections() if allow_wildcard else []
        if search_all and allow_global_no_headers:
            return self._get_all_doc_collections()
        return []

    def _retrieve_grounding_results(self, query: str, top_k: int) -> List[RetrievalResult]:
        target_collections = self._resolve_target_collections()
        if not target_collections:
            return []
        if len(target_collections) == 1:
            return self.retriever.retrieve(
                query,
                top_k=top_k,
                tenant_id="",
                collection_name=target_collections[0],
            )
        return self.retriever.retrieve_multi_collection(
            query,
            collections=target_collections,
            top_k=top_k,
            tenant_id="",
        )

    # =============== Tool: búsqueda en documentos ===============

    def _search_documents_tool(self, query: str) -> str:
        """
        Ejecuta el retriever y devuelve texto con fragmentos + citas.
        Respeta el alcance de colecciones de la llamada actual.
        """
        try:
            target_collections = self._resolve_target_collections()
            if not target_collections:
                return "No tienes acceso a ninguna coleccion autorizada."

            results = self._retrieve_grounding_results(query, top_k=self.max_context_docs)
            
            if not results:
                return "No se encontraron documentos relevantes para esta consulta."

            return self._format_retrieval_snippets(results)

        except Exception as e:
            logger.error(f"Error en search_documents_tool: {e}")
            return f"Error buscando documentos: {str(e)}"

    def _search_legislation_tool(self, query: str) -> str:
        """
        Herramienta para buscar en el BOE.
        """
        try:
            results = self.boe_client.search_legislation(query, days_back=30)
            if not results:
                # Intentar resolver si es un nombre conocido
                resolved = self.boe_client.resolve_law_id(query)
                if resolved:
                    law_data = self.boe_client.get_law_text(resolved)
                    if "error" not in law_data:
                        return f"Encontrada ley específica: {law_data['title']}.\nEnlace: {law_data['link']}\nResumen del inicio: {law_data['text'][:500]}..."
                
                return f"No se encontró legislación reciente sobre '{query}' en el BOE (últimos 30 días)."
            
            # Formatear resultados
            response_parts = [f"Resultados BOE para '{query}':"]
            for r in results[:5]: # Top 5
                response_parts.append(f"- {r['title']} ({r['summary']})\n  Enlace: {r['link']}")
            
            return "\n".join(response_parts)
            
        except Exception as e:
            logger.error(f"Error en search_legislation_tool: {e}")
            return f"Error consultando el BOE: {str(e)}"

    @staticmethod
    def _format_retrieval_snippets(results: List[RetrievalResult]) -> str:
        parts = []
        for i, r in enumerate(results, 1):
            parts.append(
                f"{i}. {r.text}\n"
                f"   Fuente: {r.citation}\n"
                f"   Relevancia: {r.score:.2%}"
            )
        return "\n\n".join(parts)

    @staticmethod
    def _normalize_filename(value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").strip().lower())

    @staticmethod
    def _normalize_text(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", (value or "").strip().lower())
        return "".join(ch for ch in normalized if not unicodedata.combining(ch))

    @staticmethod
    def _extract_filename_mentions(text: str) -> List[str]:
        if not text:
            return []

        def _clean_candidate(candidate: str) -> Optional[str]:
            value = re.sub(r"\s+", " ", (candidate or "").strip().strip("\"'"))
            value = re.sub(r"[\s\]\[(){}.,;:!?]+$", "", value)
            if not re.search(r"\.(?:pdf|doc|docx|ppt|pptx|xlsx|csv|txt|jpg|jpeg|png)$", value, flags=re.IGNORECASE):
                return None

            tokens = value.split()
            extension_pattern = r"\.(?:pdf|doc|docx|ppt|pptx|xlsx|csv|txt|jpg|jpeg|png)$"
            end_idx = None
            for idx in range(len(tokens) - 1, -1, -1):
                if re.search(extension_pattern, tokens[idx], flags=re.IGNORECASE):
                    end_idx = idx
                    break

            if end_idx is not None:
                connectors = {"de", "del", "la", "las", "el", "los", "y", "e"}
                start_idx = end_idx
                while start_idx > 0:
                    prev = tokens[start_idx - 1].strip("\"'([{")
                    prev_norm = re.sub(r"[^\w-]+", "", prev, flags=re.UNICODE).lower()
                    if not prev_norm:
                        start_idx -= 1
                        continue
                    if prev_norm in connectors:
                        start_idx -= 1
                        continue
                    if re.match(r"^[A-Z0-9]", prev) or re.search(r"[_-]", prev):
                        start_idx -= 1
                        continue
                    break

                while start_idx < end_idx:
                    current_norm = re.sub(r"[^\w-]+", "", tokens[start_idx], flags=re.UNICODE).lower()
                    if current_norm in connectors:
                        start_idx += 1
                        continue
                    break

                suffix = " ".join(tokens[start_idx : end_idx + 1]).strip("\"'")
                suffix = re.sub(r"[\s\]\[(){}.,;:!?]+$", "", suffix)
                if re.search(extension_pattern, suffix, flags=re.IGNORECASE):
                    return suffix

            return value

        patterns = [
            r"[\"']([^\"']+\.(?:pdf|doc|docx|ppt|pptx|xlsx|csv|txt|jpg|jpeg|png))[\"']",
            r"\b([^\n\r]+?\.(?:pdf|doc|docx|ppt|pptx|xlsx|csv|txt|jpg|jpeg|png))\b",
        ]
        found: List[str] = []
        for pattern in patterns:
            for match in re.findall(pattern, text, flags=re.IGNORECASE):
                candidate = _clean_candidate(match)
                if candidate:
                    found.append(candidate)
        return list(dict.fromkeys(found))

    @staticmethod
    def _extract_collection_from_source(source: str) -> Optional[str]:
        match = re.match(r"^\[([^\]]+)\]\s+", (source or "").strip())
        if not match:
            return None
        collection = (match.group(1) or "").strip()
        return collection or None

    @staticmethod
    def _looks_like_tool_call_output(text: str) -> bool:
        payload = (text or "").strip()
        if not payload.startswith("{"):
            return False
        try:
            data = json.loads(payload)
        except Exception:
            return False
        name = str(data.get("name") or "").strip()
        return name in {"search_documents", "search_legislation"}

    def _build_grounding_query(self, message: str, context: ConversationContext) -> str:
        query = (message or "").strip()
        if not query:
            return query

        followup_markers = [
            "cada uno", "cada una", "ambos", "ambas", "el otro", "la otra",
            "ese documento", "esa norma", "esa ley", "esa coleccion", "esa colección",
            "en que coleccion", "en qué colección", "donde esta", "dónde está",
        ]
        query_norm = query.lower()
        needs_history = len(query.split()) <= 10 or any(marker in query_norm for marker in followup_markers)
        if not needs_history:
            return query

        recent_user_messages = [
            (msg.get("content") or "").strip()
            for msg in context.messages
            if msg.get("role") == "user" and (msg.get("content") or "").strip()
        ]
        prior_messages = recent_user_messages[:-1] if recent_user_messages else []
        if not prior_messages:
            return query

        combined = prior_messages[-2:] + [query]
        return "\n".join(part for part in combined if part)

    def _build_sources_payload(self, retrieval_results: List[RetrievalResult]) -> List[Dict]:
        payload: List[Dict] = []
        for result in retrieval_results:
            item = {
                "filename": result.filename,
                "page": result.page,
                "citation": result.citation,
                "score": result.score,
                "source": result.source,
            }
            collection = self._extract_collection_from_source(result.source)
            if collection:
                item["collection"] = collection
            payload.append(item)
        return payload

    def _build_filename_resolution_answer(
        self,
        message: str,
        context: ConversationContext,
        retrieval_results: List[RetrievalResult],
    ) -> str:
        history_text = "\n".join((msg.get("content") or "") for msg in context.messages[-6:])
        filenames = self._extract_filename_mentions(message) or self._extract_filename_mentions(history_text)
        if not filenames:
            return ""

        by_filename: Dict[str, RetrievalResult] = {}
        for result in retrieval_results:
            by_filename.setdefault(self._normalize_filename(result.filename), result)

        message_norm = (message or "").lower()
        asks_collection = any(token in message_norm for token in ["coleccion", "colección", "departamento", "site"])
        asks_existence = any(
            token in message_norm
            for token in ["existe", "existen", "esta", "está", "encontrado", "localizado", "disponible"]
        )
        if not asks_collection and not asks_existence:
            return ""

        lines: List[str] = []
        found_count = 0
        for filename in filenames:
            result = by_filename.get(self._normalize_filename(filename))
            if not result:
                lines.append(f"- {filename}: no encontrado en las fuentes disponibles.")
                continue

            found_count += 1
            collection = self._extract_collection_from_source(result.source)
            if asks_collection and collection:
                lines.append(f"- {filename}: colección {collection} {result.citation}")
            else:
                lines.append(f"- {filename}: encontrado {result.citation}")

        if not lines:
            return ""

        if asks_collection:
            intro = "He localizado estos documentos en las colecciones activas:"
        elif found_count == len(filenames):
            intro = "Sí, he localizado los documentos solicitados:"
        elif found_count:
            intro = "He localizado parte de los documentos solicitados:"
        else:
            intro = "No he localizado los documentos solicitados en las fuentes disponibles:"

        return "\n".join([intro, *lines]).strip()

    @staticmethod
    def _stringify_llm_content(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        parts.append(str(text))
                elif item:
                    parts.append(str(item))
            return "\n".join(parts).strip()
        return str(content or "").strip()

    def _generate_grounded_answer(
        self,
        message: str,
        context: ConversationContext,
        retrieval_results: List[RetrievalResult],
    ) -> str:
        if not retrieval_results:
            return ""

        history_lines = []
        for msg in context.messages[-6:]:
            role = msg.get("role")
            content = (msg.get("content") or "").strip()
            if role and content:
                history_lines.append(f"{role}: {content}")
        history_text = "\n".join(history_lines) or "(sin historial relevante)"

        snippets = []
        for idx, result in enumerate(retrieval_results[: self.max_context_docs], start=1):
            collection = self._extract_collection_from_source(result.source)
            header = f"Fragmento {idx}: {result.citation}"
            if collection:
                header += f" | colección={collection}"
            snippets.append(f"{header}\n{result.text}")

        prompt = (
            "Responde usando solo los fragmentos proporcionados.\n"
            "Si afirmas algo, cita la evidencia con el formato exacto disponible.\n"
            "Si preguntan por la colección, usa el campo colección cuando exista.\n"
            "Si no hay evidencia suficiente, di 'no encontrado en las fuentes'."
        )
        user_content = (
            f"Historial reciente:\n{history_text}\n\n"
            f"Pregunta actual:\n{message}\n\n"
            f"Fragmentos recuperados:\n{chr(10).join(snippets)}"
        )

        llm_response = self.llm.invoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=user_content),
            ]
        )
        return self._stringify_llm_content(getattr(llm_response, "content", llm_response))

    def _build_grounded_fallback_response(
        self,
        message: str,
        context: ConversationContext,
    ) -> Tuple[str, List[Dict]]:
        grounding_query = self._build_grounding_query(message, context)
        retrieved = self._retrieve_grounding_results(
            grounding_query,
            top_k=min(self.max_context_docs, 4),
        )
        if not retrieved:
            return "", []

        response_text = self._build_filename_resolution_answer(message, context, retrieved)
        if not response_text:
            try:
                response_text = self._generate_grounded_answer(message, context, retrieved)
            except Exception as e:
                logger.warning(f"Fallo sintetizando respuesta grounded: {e}")

        if not response_text:
            response_text = "He encontrado evidencia relevante, pero no he podido sintetizar una respuesta fiable."

        if not self._extract_citations(response_text):
            response_text = self._append_sources_section(response_text, retrieved)

        return response_text, self._build_sources_payload(retrieved)

    @staticmethod
    def _direct_datetime_response(message: str) -> Optional[str]:
        msg = RAGAgent._normalize_text(message)
        now = datetime.now(ZoneInfo("Europe/Madrid"))
        weekdays = [
            "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"
        ]
        weekdays = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
        months = [
            "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
        ]

        time_patterns = [
            "que hora es", "qué hora es", "hora actual", "ahora mismo",
            "que dia y hora es", "qué día y hora es", "fecha y hora", "dia y hora", "día y hora",
        ]
        date_patterns = [
            "que fecha es", "qué fecha es", "que dia es", "qué día es",
            "fecha de hoy", "hoy es", "que dia y hora es", "qué día y hora es",
            "fecha y hora", "dia y hora", "día y hora",
        ]

        time_patterns = [
            "que hora es", "hora actual", "ahora mismo",
            "que dia y hora es", "fecha y hora", "dia y hora",
        ]
        date_patterns = [
            "que fecha es", "que dia es",
            "fecha de hoy", "hoy es", "que dia y hora es",
            "fecha y hora", "dia y hora",
        ]
        formatted_date = f"{weekdays[now.weekday()]} {now.day:02d} de {months[now.month - 1]} de {now.year}"
        has_time = any(pattern in msg for pattern in time_patterns)
        has_date = any(pattern in msg for pattern in date_patterns)

        if has_time and has_date:
            return f"Hoy es {formatted_date} y la hora actual en Madrid es {now.strftime('%H:%M')}."

        if has_time:
            return f"La hora actual en Madrid es {now.strftime('%H:%M')} del {now.strftime('%d/%m/%Y')}."

        if has_date:
            return f"Hoy es {formatted_date} en Madrid."

        return None

    # ==================== MÉTODO PRINCIPAL ====================

    def chat(
        self,
        user_id: Optional[int],
        conversation_id: Optional[int],
        message: str,
        *,
        tenant_id: Optional[str] = None,
        authorized_collections: Optional[List[str]] = None,
        azure_id: Optional[str] = None,
        email: Optional[str] = None,
        name: Optional[str] = None,
    ) -> AgentResponse:
        
    # Procesa el mensaje del usuario con memoria + agente RAG.
       
        start_time = time.time()
        logger.info(f"Procesando mensaje: '{(message or '')[:80]}'")

        # Asegurar usuario
        if azure_id:
            user_id = self.memory.get_or_create_user(azure_id, email, name)
        elif user_id:
            user_id = self.memory.ensure_user_reference(user_id, email=email, name=name)

        # Crear conversación si no existe
        if not conversation_id:
            if not user_id:
                raise ValueError("user_id es obligatorio si no se proporciona conversation_id")
            conversation_id = self.memory.create_conversation(user_id)

        # Guardar mensaje del usuario (sin métricas)
        self.memory.add_message(
            conversation_id=conversation_id,
            role="user",
            content=message,
        )

        # Historial
        context = self.memory.get_conversation_context(conversation_id)
        chat_history = self._format_chat_history(context)

        tenant_token = CURRENT_TENANT.set(tenant_id or self.default_tenant_id)
        collections_token = CURRENT_COLLECTIONS.set(
            tuple(
                c.strip()
                for c in (authorized_collections or [])
                if isinstance(c, str) and c.strip()
            )
        )

        try:
            direct_datetime = self._direct_datetime_response(message)
            if direct_datetime:
                processing_time = time.time() - start_time
                processing_time_ms = int(processing_time * 1000)
                tokens_used = self._estimate_tokens(message, direct_datetime)
                active_tenant = CURRENT_TENANT.get()

                self.memory.add_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=direct_datetime,
                    metadata={
                        "model": "direct-datetime",
                        "tenant_id": active_tenant,
                    },
                    tokens_used=tokens_used,
                    processing_time_ms=processing_time_ms,
                )

                return AgentResponse(
                    content=direct_datetime,
                    sources=[],
                    conversation_id=conversation_id,
                    tokens_used=tokens_used,
                    processing_time=processing_time,
                    has_citations=False,
                )

            response_text = ""
            sources: List[Dict] = []
            has_citations = False
            agent_input = {"input": message, "chat_history": chat_history}
            try:
                result = self.agent.invoke(agent_input)
                response_text = (result.get("output") or "").strip()
            except Exception as agent_error:
                logger.warning(f"Agent invoke failed, usando fallback grounded: {agent_error}")

            # Asegurar citas (guardrail): si faltan, añadimos sección de fuentes recuperadas
            if not response_text or self._looks_like_tool_call_output(response_text):
                response_text, sources = self._build_grounded_fallback_response(message, context)
                has_citations = len(sources) > 0
            else:
                sources = self._extract_citations(response_text)
                has_citations = len(sources) > 0

            if not has_citations:
                # Intento de grounding mínimo: recuperar y anexar fuentes respetando las colecciones activas.
                response_text, sources = self._build_grounded_fallback_response(message, context)
                has_citations = len(sources) > 0
            if not response_text:
                    # Sin documentos: usar respuesta del agente si es válida
                    if not response_text:
                        response_text = (
                            "No he encontrado evidencia en las fuentes disponibles para responder con garantías. "
                            "Si puedes indicar el documento o aportar más contexto, lo reviso al momento."
                        )
                    # else: mantener la respuesta del agente tal cual

            # Métricas
            processing_time = time.time() - start_time
            tokens_used = self._estimate_tokens(message, response_text)
            processing_time_ms = int(processing_time * 1000)

            # Guardar respuesta del asistente con métricas
            self.memory.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=response_text,
                sources_used=sources,
                metadata={
                    "model": getattr(self.llm, "model", None) or getattr(self.llm, "model_name", None),
                    "temperature": getattr(self.llm, "temperature", None),
                    "tenant_id": CURRENT_TENANT.get(),
                },
                tokens_used=tokens_used,
                processing_time_ms=processing_time_ms,
            )

            return AgentResponse(
                content=response_text,
                sources=sources,
                conversation_id=conversation_id,
                tokens_used=tokens_used,
                processing_time=processing_time,
                has_citations=has_citations,
            )

        except Exception as e:
            logger.exception(f"Error en chat: {e}")
            error_msg = (
                "Disculpa, encontré un error procesando tu consulta. "
                "Por favor, intenta reformular tu pregunta o contacta al administrador."
            )

            # Guardamos el error como mensaje del asistente (sin métricas)
            self.memory.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=error_msg,
                metadata={"error": str(e), "tenant_id": CURRENT_TENANT.get()},
            )

            return AgentResponse(
                content=error_msg,
                sources=[],
                conversation_id=conversation_id,
                tokens_used=0,
                processing_time=time.time() - start_time,
                has_citations=False,
            )
        finally:
            CURRENT_TENANT.reset(tenant_token)
            CURRENT_COLLECTIONS.reset(collections_token)


    # ==================== MÉTODOS AUXILIARES ====================

    def _format_chat_history(self, context: ConversationContext) -> List:
        """
        Formatea historial de conversación para LangChain (incluye resumen si existe).
        """
        history: List = []
        if getattr(context, "summary", None):
            history.append(SystemMessage(content=f"Resumen de conversación previa:\n{context.summary}"))

        for msg in context.messages:
            role = msg.get("role")
            if role == "user":
                history.append(HumanMessage(content=msg.get("content", "")))
            elif role == "assistant":
                history.append(AIMessage(content=msg.get("content", "")))
        return history

    def _extract_citations(self, text: str) -> List[Dict]:
        """
        Extrae citas del texto de respuesta. Formato esperado: [archivo.ext p.N]
        """
        matches = re.findall(CITATION_REGEX_VISIBLE, text, re.IGNORECASE)
        sources: List[Dict] = []
        seen: set[str] = set()

        for filename, page in matches:
            key = f"{filename}_{page or ''}"
            if key in seen:
                continue
            sources.append(
                {
                    "filename": filename,
                    "page": int(page) if page else None,
                    "citation": f"[{filename}{' p.' + page if page else ''}]",
                }
            )
            seen.add(key)
        return sources

    @staticmethod
    def _append_sources_section(text: str, retrieval_results: List[RetrievalResult]) -> str:
        lines = ["\n\n---", "Fuentes consultadas:"]
        for r in retrieval_results:
            lines.append(f"- {r.citation}")
        return (text + ("\n" if text else "") + "\n".join(lines)).strip()

    @staticmethod
    def _estimate_tokens(input_text: str, output_text: str) -> int:
        """
        Estimación simple de tokens (≈ 4 chars/token).
        """
        total_chars = len(input_text or "") + len(output_text or "")
        return max(1, total_chars // 4)


# ============================================
# Validador ligero de citas (opcional)
# ============================================

class CitationValidator:
    """
    Valida que la respuesta incluya citas y que el formato sea correcto.
    """

    @staticmethod
    def validate_response(response: str, sources: List[Dict]) -> Dict[str, Any]:
        citations_in_text = re.findall(r'\[([^\]]+)\]', response or "")
        sources_cited = set()
        for cit in citations_in_text:
            for s in sources:
                if s.get("filename") and s["filename"] in cit:
                    sources_cited.add(s["filename"])

        missing = sorted(set(s["filename"] for s in sources if s.get("filename")) - sources_cited)
        invalid = []
        valid_pattern = re.compile(
            r'\[.+\.(?:pdf|docx|pptx|xlsx|txt|csv|jpg|jpeg|png)(?:\s+p\.\d+)?\]',
            re.IGNORECASE
        )
        for raw in citations_in_text:
            full = f"[{raw}]"
            if not valid_pattern.match(full):
                invalid.append(full)

        return {
            "has_citations": len(citations_in_text) > 0,
            "total_citations": len(citations_in_text),
            "sources_used": len(sources),
            "sources_cited": len(sources_cited),
            "missing_citations": missing,
            "invalid_format": invalid,
            "is_valid": len(missing) == 0 and len(invalid) == 0,
        }

