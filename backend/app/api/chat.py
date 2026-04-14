from fastapi import APIRouter, Request, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import List, Optional
import time
import json
import logging
import re
import unicodedata
import uuid

from app.core.state import app_state, llm_client, LLM_MODEL, limiter
from app.core.permissions import resolve_authorized_collections

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])

def _extract_query_ids(text: str) -> List[str]:
    return list(dict.fromkeys(re.findall(r"\b[A-Z]{1,8}-\d{2,8}\b", (text or "").upper())))


def _has_exact_id(text: str, token: str) -> bool:
    if not text:
        return False
    pattern = rf"(?<![A-Z0-9]){re.escape(token.upper())}(?![A-Z0-9])"
    return re.search(pattern, text.upper()) is not None


def _extract_filename_mentions(text: str) -> List[str]:
    if not text:
        return []

    def _clean_candidate(candidate: str) -> Optional[str]:
        value = re.sub(r"\s+", " ", (candidate or "").strip().strip("\"'“”‘’"))
        value = re.sub(r"[\s\]\[(){}.,;:!?]+$", "", value)
        if not re.search(r"\.(?:pdf|doc|docx|txt|xlsx|csv|ppt|pptx)$", value, flags=re.IGNORECASE):
            return None

        tokens = value.split()
        for idx, token in enumerate(tokens):
            token_clean = token.strip("\"'“”‘’([{")
            if not token_clean:
                continue
            if re.match(r"^[A-ZÁÉÍÓÚÜÑ0-9]", token_clean):
                suffix = " ".join(tokens[idx:]).strip("\"'“”‘’")
                suffix = re.sub(r"[\s\]\[(){}.,;:!?]+$", "", suffix)
                if re.search(r"\.(?:pdf|doc|docx|txt|xlsx|csv|ppt|pptx)$", suffix, flags=re.IGNORECASE):
                    return suffix
        return value

    patterns = [
        r"[\"']([^\"']+\.(?:pdf|doc|docx|txt|xlsx|csv|ppt|pptx))[\"']",
        r"\b([^\n\r]+?\.(?:pdf|doc|docx|txt|xlsx|csv|ppt|pptx))\b",
    ]
    found: List[str] = []
    for pat in patterns:
        for m in re.findall(pat, text, flags=re.IGNORECASE):
            v = _clean_candidate(m)
            if v:
                found.append(v)
    return list(dict.fromkeys(found))


def _normalize_text(text: str) -> str:
    t = (text or "").strip().lower()
    t = unicodedata.normalize("NFKD", t)
    return "".join(ch for ch in t if not unicodedata.combining(ch))


def _interleave_results_by_filename(results: List, filename_norm_order: List[str]) -> List:
    if len(filename_norm_order) < 2:
        return results

    buckets = {name: [] for name in filename_norm_order}
    remainder = []

    for result in results:
        filename_norm = _normalize_text(getattr(result, "filename", ""))
        if filename_norm in buckets:
            buckets[filename_norm].append(result)
        else:
            remainder.append(result)

    ordered: List = []
    while True:
        advanced = False
        for name in filename_norm_order:
            if buckets[name]:
                ordered.append(buckets[name].pop(0))
                advanced = True
        if not advanced:
            break

    ordered.extend(remainder)
    for name in filename_norm_order:
        ordered.extend(buckets[name])
    return ordered


def _dedupe_results(results: List) -> List:
    deduped = []
    seen = set()
    for result in results:
        key = (
            _normalize_text(getattr(result, "filename", "")),
            int(getattr(result, "page", 0) or 0),
            (getattr(result, "text", "") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(result)
    return deduped


def _is_refusal_text(text: str) -> bool:
    text_norm = _normalize_text(text)
    refusal_markers = [
        "lo siento",
        "no puedo",
        "cannot",
        "cant",
        "i cannot",
        "i can't",
        "no puedo responder",
        "no puedo cumplir",
        "no puedo ayudar",
    ]
    return any(marker in text_norm for marker in refusal_markers)


def _clean_fallback_snippet(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    cleaned = cleaned.replace("\ufffd", "")
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    return cleaned


def _summarize_filename_subject(filename: str) -> str:
    subject = re.sub(r"\.(?:pdf|doc|docx|txt|xlsx|csv|ppt|pptx)$", "", filename or "", flags=re.IGNORECASE)
    subject = re.sub(r"\(fdo\.[^)]+\)", "", subject, flags=re.IGNORECASE)
    subject = re.sub(r"^[A-Z]?\d{4}-\d{4}\s+Ed\d+\s+", "", subject, flags=re.IGNORECASE)
    subject = re.sub(r"\s{2,}", " ", subject).strip(" -_()")
    return subject


def _build_rag_fallback_answer(query: str, results: List) -> str:
    if not results:
        return "He encontrado contexto relevante, pero no he podido generar una respuesta fiable."

    query_norm = _normalize_text(query)
    unique_files = []
    seen_files = set()
    for result in results:
        filename = str(getattr(result, "filename", "") or "").strip()
        if filename and filename not in seen_files:
            seen_files.add(filename)
            unique_files.append(filename)

    if len(unique_files) == 1:
        intro = f"He encontrado contenido relevante en el documento {unique_files[0]}."
    else:
        intro = "He encontrado contenido relevante en estos documentos: " + ", ".join(unique_files[:3]) + "."

    fragments = []
    for result in results[:6]:
        snippet = _clean_fallback_snippet(getattr(result, "text", "") or "")
        if not snippet:
            continue
        if len(snippet) > 260:
            snippet = snippet[:257].rstrip() + "..."
        alpha_chars = sum(ch.isalpha() for ch in snippet)
        if alpha_chars < 20:
            continue
        page = getattr(result, "page", None)
        citation = f"[{getattr(result, 'filename', 'Doc')}"
        if page:
            citation += f" p.{page}"
        citation += "]"
        fragments.append(f"{snippet} {citation}")
        if len(fragments) >= 2:
            break

    if len(unique_files) == 1 and any(
        token in query_norm for token in ["que dice", "resume", "de que trata", "de qué trata", "que pone"]
    ):
        filename = unique_files[0]
        subject = _summarize_filename_subject(filename)
        combined_text = " ".join(_clean_fallback_snippet(getattr(result, "text", "") or "") for result in results[:4])
        ref_match = re.search(r"\b[A-Z]?\d{4}-\d{4}\b", f"{filename} {combined_text}", flags=re.IGNORECASE)
        date_match = re.search(r"\b\d{2}/\d{2}/\d{4}\b", combined_text)
        aircraft_match = re.search(r"\bEC[- ]?\d{3}\b", combined_text, flags=re.IGNORECASE)
        mentions = []
        if ref_match:
            mentions.append(f"referencia {ref_match.group(0).upper()}")
        if date_match:
            mentions.append(f"fecha {date_match.group(0)}")
        if aircraft_match:
            mentions.append(f"menciona {aircraft_match.group(0).upper()}")
        if re.search(r"video\s+transmission\s+system", combined_text, flags=re.IGNORECASE):
            mentions.append("menciona el sistema de transmision de video")

        lead = f"El documento {filename} parece corresponder a '{subject or filename}'."
        if mentions:
            lead += " En la OCR recuperada se identifican " + ", ".join(mentions) + "."
        if fragments:
            return lead + f" Fragmento principal: {fragments[0]}"
        return lead

    if not fragments:
        if any(token in query_norm for token in ["que dice", "resume", "de que trata", "de qué trata", "que pone"]):
            return intro + " No puedo resumirlo mejor con fiabilidad, pero ya he localizado el PDF correcto en las fuentes citadas."
        return intro

    return intro + " Fragmentos relevantes: " + " ".join(fragments)


def _find_filenames_by_ids(collections: List[str], query_ids: List[str], max_scan: int = 15000) -> List[str]:
    """
    Busca filenames por ID de documento (AL-08, M-003, etc.) en payload de Qdrant.
    Se usa para anclar /chat al documento correcto cuando la búsqueda semántica deriva.
    """
    if not query_ids:
        return []

    out: List[str] = []
    seen = set()

    for coll in collections:
        try:
            offset = None
            scanned = 0
            while scanned < max_scan:
                points, next_offset = app_state.qdrant.scroll(
                    collection_name=coll,
                    offset=offset,
                    limit=512,
                    with_payload=True,
                    with_vectors=False,
                )
                if not points:
                    break
                scanned += len(points)
                for p in points:
                    payload = p.payload or {}
                    filename = str(payload.get("filename", "") or "").strip()
                    source = str(payload.get("source", "") or "")
                    if not filename:
                        continue
                    if any(_has_exact_id(filename, qid) or _has_exact_id(source, qid) for qid in query_ids):
                        if filename not in seen:
                            seen.add(filename)
                            out.append(filename)
                if not next_offset:
                    break
                offset = next_offset
        except Exception as e:
            logger.warning(f"Error scanning filenames in collection {coll}: {e}")
            continue

    return out

class ChatRequest(BaseModel):
    """Request para chat"""
    message: str = Field(..., min_length=1, max_length=4000)
    mode: str = "rag"
    stream: bool = False
    conversation_id: Optional[int] = None
    user_id: Optional[int] = None
    azure_id: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    chat_history_context: Optional[str] = None


class ChatResponse(BaseModel):
    """Response de chat"""
    content: str
    sources: List[dict]
    conversation_id: int
    has_citations: bool
    tokens_used: int
    processing_time: float


@router.post("/chat")
@limiter.limit("20/minute")
async def chat(
    request: Request,
    chat_request: ChatRequest,
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id"),
    x_tenant_ids: Optional[str] = Header(None, alias="X-Tenant-Ids"),
):
    """
    Chat RAG simple:
    - Recupera chunks relevantes desde Qdrant
    - Llama al LLM via LiteLLM
    - Responde usando SOLO ese contexto
    """
    start = time.time()

    try:
        use_stream = chat_request.stream
        mode = (chat_request.mode or "rag").lower()
        use_rag = mode == "rag"
        use_agent = mode == "agent"
        # Colecciones objetivo basadas en headers de permisos.
        tenant_collections = resolve_authorized_collections(
            qdrant_client=app_state.qdrant,
            x_tenant_id=x_tenant_id,
            x_tenant_ids=x_tenant_ids,
        )

        results = []
        sources = []
        context_text = ""

        if use_agent:
            tenant_for_agent = tenant_collections[0] if tenant_collections else None
            azure_id = chat_request.azure_id
            if not azure_id and chat_request.email:
                azure_id = f"email:{chat_request.email}"
            if not azure_id and not chat_request.user_id and not chat_request.conversation_id:
                azure_id = f"anonymous:{uuid.uuid4()}"

            agent_response = app_state.agent.chat(
                user_id=chat_request.user_id,
                conversation_id=chat_request.conversation_id,
                message=chat_request.message,
                tenant_id=tenant_for_agent,
                authorized_collections=tenant_collections or None,
                azure_id=azure_id,
                email=chat_request.email,
                name=chat_request.name,
            )

            if use_stream:
                async def agent_stream():
                    chunk_str = json.dumps({"content": agent_response.content})
                    yield f"data: {chunk_str}\n\n"
                    done_str = json.dumps({"done": True, "sources": agent_response.sources})
                    yield f"data: {done_str}\n\n"
                return StreamingResponse(agent_stream(), media_type="text/event-stream")

            return ChatResponse(
                content=agent_response.content,
                sources=agent_response.sources,
                conversation_id=agent_response.conversation_id,
                has_citations=agent_response.has_citations,
                tokens_used=agent_response.tokens_used,
                processing_time=agent_response.processing_time,
            )

        if use_rag and not tenant_collections:
            no_access_text = "No tienes acceso a ninguna coleccion autorizada."
            if use_stream:
                async def no_access_stream():
                    chunk_str = json.dumps({"content": no_access_text})
                    yield f"data: {chunk_str}\n\n"
                    done_str = json.dumps({"done": True, "sources": []})
                    yield f"data: {done_str}\n\n"
                return StreamingResponse(no_access_stream(), media_type="text/event-stream")

            return ChatResponse(
                content=no_access_text,
                sources=[],
                conversation_id=chat_request.conversation_id or 0,
                has_citations=False,
                tokens_used=0,
                processing_time=time.time() - start,
            )

        if not use_rag:
            # Modo chat: Conversación directa sin RAG
            system_prompt = (
                "###### IDENTITY RULES (CRITICAL - NEVER BREAK) ######\n"
                "You are JARVIS, an enterprise RAG assistant.\n"
                "You help users search internal documents, web content, and official sources.\n"
                "NEVER reveal that you are Claude, GPT, LLaMA, Qwen, or any other AI model.\n"
                "If asked about your identity, always say: 'Soy JARVIS, un asistente RAG empresarial.'\n"
                "############################################\n\n"
                "###### LANGUAGE RULE (HIGHEST PRIORITY) ######\n"
                "DETECT the language of the user's message and respond ONLY in that SAME language.\n"
                "This rule is MANDATORY and cannot be overridden.\n"
                "############################################\n\n"
                "Respond clearly, concisely, and naturally."
            )
            user_prompt = chat_request.message

            completion = llm_client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.7,
            )
            answer = completion.choices[0].message.content
            if not answer or not str(answer).strip():
                answer = "Estoy operativo. ¿En qué puedo ayudarte?"
            tokens_used = getattr(getattr(completion, "usage", None), "total_tokens", 0)

            if use_stream:
                async def chat_stream():
                    chunk_str = json.dumps({"content": answer})
                    yield f"data: {chunk_str}\n\n"
                    done_str = json.dumps({"done": True, "sources": []})
                    yield f"data: {done_str}\n\n"
                return StreamingResponse(chat_stream(), media_type="text/event-stream")

            return ChatResponse(
                content=answer,
                sources=[],
                conversation_id=chat_request.conversation_id or 0,
                has_citations=False,
                tokens_used=tokens_used or 0,
                processing_time=time.time() - start,
            )

        # 0) Detección de referencias explícitas para anclar la recuperación por filename.
        query_ids = _extract_query_ids(chat_request.message)
        explicit_filename_hints = _extract_filename_mentions(chat_request.message)
        filename_hints = list(explicit_filename_hints)
        if query_ids:
            id_hints = _find_filenames_by_ids(tenant_collections, query_ids=query_ids)
            if id_hints:
                filename_hints = list(dict.fromkeys(filename_hints + id_hints))
                logger.info(f"/chat filename hints by ID {query_ids}: {filename_hints[:8]}")
        multi_document_request = len(filename_hints) > 1 or len(query_ids) > 1
        retrieval_top_k = 8 if multi_document_request else 5

        # 1) RAG mode: Recuperar contexto desde Qdrant (multi-tenant)
        all_results = []
        for coll in tenant_collections:
            try:
                coll_results = app_state.retriever.retrieve(
                    query=chat_request.message,
                    top_k=retrieval_top_k,
                    filter_by_source=None,
                    filter_by_filenames=filename_hints or None,
                    exclude_ocr=False,
                    collection_name=coll,  # The multi-site architecture uses completely separate collections
                    tenant_id="",
                )
                # Si el filtro por filename dejó la colección sin resultados, fallback semántico.
                if not coll_results and filename_hints:
                    coll_results = app_state.retriever.retrieve(
                        query=chat_request.message,
                        top_k=retrieval_top_k,
                        filter_by_source=None,
                        filter_by_filenames=None,
                        exclude_ocr=False,
                        collection_name=coll,
                        tenant_id="",
                    )
                all_results.extend(coll_results)
            except Exception as e:
                logger.warning(f"Error querying collection {coll}: {e}")
        all_results = _dedupe_results(all_results)

        # Sort: priorizar IDs/documentos explicitamente solicitados, luego score.
        explicit_hint_norm = {_normalize_text(h) for h in explicit_filename_hints} if explicit_filename_hints else set()
        hint_norm = {_normalize_text(h) for h in filename_hints} if filename_hints else set()
        if query_ids:
            id_matching_results = [
                result
                for result in all_results
                if any(_has_exact_id(getattr(result, "filename", ""), qid) for qid in query_ids)
            ]
            if id_matching_results:
                all_results = id_matching_results
        if hint_norm:
            matching_results = [
                result
                for result in all_results
                if _normalize_text(getattr(result, "filename", "")) in hint_norm
            ]
            if matching_results:
                all_results = matching_results
        if all_results and (hint_norm or query_ids) and not multi_document_request:
            top_filename_norm = _normalize_text(getattr(all_results[0], "filename", ""))
            same_top_results = [
                result
                for result in all_results
                if _normalize_text(getattr(result, "filename", "")) == top_filename_norm
            ]
            if same_top_results:
                all_results = same_top_results

        if hint_norm:
            query_tokens = set(re.findall(r"[a-z0-9]{3,}", _normalize_text(chat_request.message)))

            def _hint_priority(filename: str) -> int:
                n = _normalize_text(filename)
                if n in explicit_hint_norm:
                    return 2
                if n in hint_norm:
                    return 1
                return 0

            def _overlap_score(filename: str) -> int:
                if not query_tokens:
                    return 0
                f_tokens = set(re.findall(r"[a-z0-9]{3,}", _normalize_text(filename)))
                return len(query_tokens.intersection(f_tokens))

            all_results.sort(
                key=lambda x: (
                    _hint_priority(getattr(x, "filename", "")),
                    _overlap_score(getattr(x, "filename", "")),
                    float(getattr(x, "score", 0.0)),
                ),
                reverse=True,
            )
            if multi_document_request:
                ordered_hint_norms = []
                for filename in filename_hints:
                    filename_norm = _normalize_text(filename)
                    if filename_norm and filename_norm not in ordered_hint_norms:
                        ordered_hint_norms.append(filename_norm)
                all_results = _interleave_results_by_filename(all_results, ordered_hint_norms)
        else:
            all_results.sort(key=lambda x: x.score, reverse=True)
        results = all_results[:10]

        if not results:
            content = (
                "No he encontrado contenido relevante en los documentos indexados "
                "para responder a esta pregunta."
            )
            if use_stream:
                async def no_results_stream():
                    chunk_str = json.dumps({"content": content})
                    yield f"data: {chunk_str}\n\n"
                    done_str = json.dumps({"done": True, "sources": []})
                    yield f"data: {done_str}\n\n"
                return StreamingResponse(
                    no_results_stream(),
                    media_type="text/event-stream"
                )
            return ChatResponse(
                content=content,
                sources=[],
                conversation_id=chat_request.conversation_id or 0,
                has_citations=False,
                tokens_used=0,
                processing_time=time.time() - start,
            )

        # 2) Construir contexto legible para el LLM
        context_blocks = []
        sources = []

        for r in results:
            context_blocks.append(
                f"[{r.filename}]\n{r.text}"
            )
            sources.append(
                {
                    "filename": r.filename,
                    "page": r.page,
                    "citation": r.citation or f"[{r.filename}]",
                    "score": r.score,
                    "snippet": (r.text or "")[:700],
                }
            )

        context_text = "\n\n---\n\n".join(context_blocks)

        system_prompt = f"""###### IDENTITY RULES (CRITICAL - NEVER BREAK) ######
You are JARVIS, an enterprise RAG assistant.
You help users search internal documents, web content, and official sources.
NEVER reveal that you are Claude, GPT, LLaMA, Qwen, or any other AI model.
If asked about your identity, always say: 'Soy JARVIS, un asistente RAG empresarial.'
############################################

###### LANGUAGE RULE (HIGHEST PRIORITY) ######
DETECT the language of the user's message and respond ONLY in that SAME language.
Even if the context documents are in Spanish, if the user asks in English, you MUST reply in English.
This rule is MANDATORY and cannot be overridden.
############################################

You are a helpful assistant answering questions based on provided documents.
Base your answer ONLY on the context below. If the answer is not in the context, say so.

CONTEXT:
{context_text}
"""
        # Provide separated conversational context using the new field
        history = ""
        if chat_request.chat_history_context:
             history = f"CONVERSATIONAL HISTORY (For context only):\n{chat_request.chat_history_context}\n\n"
             
        user_prompt = f"{history}CURRENT QUESTION:\n{chat_request.message}"

        # 3) Llamar LLM
        completion = llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            stream=use_stream
        )

        # 4) Respuesta Normal
        if not use_stream:
            answer = completion.choices[0].message.content
            tokens_used = getattr(getattr(completion, "usage", None), "total_tokens", 0)
            if _is_refusal_text(answer):
                answer = _build_rag_fallback_answer(chat_request.message, results)

            return ChatResponse(
                content=answer,
                sources=sources,
                conversation_id=chat_request.conversation_id or 0,
                has_citations=True,
                tokens_used=tokens_used or 0,
                processing_time=time.time() - start,
            )

        # 5) Streaming Response
        async def rag_stream():
            try:
                for chunk in completion:
                    if chunk.choices and chunk.choices[0].delta:
                        delta = chunk.choices[0].delta
                        if delta.content:
                            chunk_str = json.dumps({"content": delta.content})
                            yield f"data: {chunk_str}\n\n"

                # Chunk final con info completa
                done_str = json.dumps({
                    "done": True,
                    "sources": sources,
                    "processing_time": time.time() - start,
                })
                yield f"data: {done_str}\n\n"
            except Exception as e:
                logger.error(f"Error en stream RAG: {e}")
                err_str = json.dumps({"error": str(e)})
                yield f"data: {err_str}\n\n"

        return StreamingResponse(
            rag_stream(),
            media_type="text/event-stream"
        )

    except Exception as e:
        logger.error(f"Error procesando /chat: {str(e)}")
        import traceback
        traceback.print_exc()
        if request.app.debug:
            err_msg = str(e)
        else:
            err_msg = "Error interno procesando respuesta LLM"
        return ChatResponse(
            content=f"Lo siento, ocurrió un error: {err_msg}",
            sources=[],
            conversation_id=0,
            has_citations=False,
            tokens_used=0,
            processing_time=time.time() - start,
        )


