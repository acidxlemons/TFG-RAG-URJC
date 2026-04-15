"""
JARVIS Pipeline v3.0 - Agente Inteligente Unificado
========================================

Este pipeline actúa como un AGENTE COMPLETO que:
- Detecta automáticamente la intención del usuario
- Selecciona el modo apropiado (RAG, web search, scraping, OCR, listar docs)
- Usa modelos especializados (llama3.1 para texto, llava para imágenes)
- Es el ÚNICO punto de entrada en OpenWebUI

CAPACIDADES:
- 📚 RAG: Búsqueda en documentos internos
- 🌐 Web Search: Búsqueda en internet
- 🔍 Web Scraping: Analizar URLs
- 🖼️  OCR/Visión: Analizar imágenes con LLaVA
- 📋 Listar Documentos: Ver PDFs disponibles
- 💬 Chat: Conversación normal
"""

from typing import List, Dict, Generator, Iterator, Union, Optional
import html as html_lib
import requests
import json
import logging
import os
import re
import time
import unicodedata
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from pydantic import BaseModel

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Pipeline:
    class Valves(BaseModel):
        BACKEND_URL: str = "http://rag-backend:8000"
        BACKEND_CHAT_TIMEOUT: int = 300
        LITELLM_URL: str = "http://litellm:4000"
        LITELLM_API_KEY: str = os.getenv("LITELLM_MASTER_KEY", "sk-1234")
        LITELLM_TIMEOUT: int = 300
        OLLAMA_URL: str = "http://ollama:11434"
        SHAREPOINT_SITES_CONFIG: str = os.getenv("SHAREPOINT_SITES_CONFIG", "/app/config/sharepoint_sites.json")
        DEBUG_MODE: bool = True
        BOE_CONTEXT_RESULTS: int = 5
        BOE_CONTEXT_SUMMARY_CHARS: int = 280
        BOE_FOCUS_SUMMARY_CHARS: int = 420
        # Modelos especializados
        TEXT_MODEL: str = "qwen2.5-32b"       # Qwen 2.5 32B Q4_K_M - Conversación y RAG
        VISION_MODEL: str = "qwen2.5vl"       # Qwen 2.5 VL 7B - OCR y análisis de imágenes
        TEXT_MODEL: str = "JARVIS"
        DEPARTMENT_MAPPING: Dict[str, str] = {
            # Mapeo nombre/UUID de grupo Azure AD → colección Qdrant
            # Ejemplo: "NombreGrupo": "documents_NombreGrupo"
            # o bien con UUID: "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX": "documents_NombreGrupo"
            # Configura aquí los grupos de tu organización según sharepoint_sites.json

            # Nota: la colección "documents" es global (acceso a todos los usuarios)
            # Añadir más grupos aquí cuando se configuren en Azure AD
        }
        # Colección por defecto si no hay grupos o multi-tenant deshabilitado
        DEFAULT_COLLECTION: str = "documents"
        # Habilitar multi-departamento
        MULTI_DEPARTMENT_ENABLED: bool = True
        # Tiempo de cache de permisos (segundos)
        PERMISSION_CACHE_TTL: int = 300
        FILE_CONTEXT_TTL: int = 7200
        RAG_CONTEXT_TTL: int = 7200
        FILE_CHUNK_SIZE: int = 1200
        FILE_CHUNK_OVERLAP: int = 200
        FILE_TOP_K: int = 5
        GLOBAL_READ_COLLECTIONS: List[str] = ["documents", "webs"]
        
        # URL del servicio Indexer para status checks
        INDEXER_URL: str = "http://rag-indexer:8001"

    def __init__(self):
        self.name = "JARVIS"
        self.id = "jarvis"
        self.valves = self.Valves()
        
        # --- MEMORIA DE SESIÓN (Por Usuario) ---
        # Clave: email del usuario
        self._user_image_memory: Dict[str, str] = {}    # {conversation_key: base64_image}
        self._user_web_memory: Dict[str, Dict] = {}     # {conversation_key: {url, content, ...}}
        self._user_boe_memory: Dict[str, Dict] = {}     # {conversation_key: {type, query, results, focus_result, fetched_at}}
        self._user_file_memory: Dict[str, Dict] = {}    # {conversation_key: {filename, chunks, fetched_at, ...}}
        self._user_rag_memory: Dict[str, Dict] = {}     # {conversation_key: {query, answer, sources, primary_filename, fetched_at}}
        self._user_pending_decision: Dict[str, Dict] = {} # {conversation_key: {type: 'url_check', data: ...}}
        self._agent_conversation_ids: Dict[str, int] = {}  # {conversation_key: backend conversation_id}
        
        # Cache de permisos por usuario {email: (timestamp, [colecciones])}
        self._permission_cache: Dict[str, tuple] = {}
        self._department_mapping: Dict[str, str] = {}
        self._normalized_department_mapping: Dict[str, str] = {}
        self._configured_collections: List[str] = []
        self._load_department_mapping()
        logger.info(f"✓ {self.name} inicializado")
        logger.info(f"  Backend: {self.valves.BACKEND_URL}")
        logger.info(f"  LiteLLM: {self.valves.LITELLM_URL}")
        logger.info(f"  Multi-departamento: {'ON' if self.valves.MULTI_DEPARTMENT_ENABLED else 'OFF'}")

    async def on_startup(self):
        logger.info(f"🚀 Starting {self.name}")

    async def on_shutdown(self):
        logger.info(f"👋 Shutting down {self.name}")
    
    def _load_department_mapping(self) -> None:
        mapping = dict(self.valves.DEPARTMENT_MAPPING or {})
        collections = set(mapping.values())
        config_path = Path(self.valves.SHAREPOINT_SITES_CONFIG)

        def register_collection_aliases(collection_name: str) -> None:
            collection = str(collection_name or "").strip()
            if not collection:
                return

            collections.add(collection)
            mapping[collection] = collection

            collection_lower = collection.lower()
            if collection_lower.startswith("documents_"):
                suffix = collection[len("documents_"):].strip()
                if suffix:
                    mapping[suffix] = collection
                    mapping[f"docs_{suffix.lower()}"] = collection
            elif collection_lower.startswith("docs_"):
                suffix = collection[len("docs_"):].strip()
                if suffix:
                    mapping[suffix] = collection
                    mapping[f"documents_{suffix.upper()}"] = collection

        try:
            if config_path.exists():
                config = json.loads(config_path.read_text(encoding="utf-8"))
                for site in config.get("sites", []):
                    if site.get("enabled", True) is False:
                        continue
                    collection = str(site.get("collection_name", "") or "").strip()
                    if not collection:
                        continue
                    register_collection_aliases(collection)
                    site_name = str(site.get("name", "") or "").strip()
                    if site_name:
                        mapping[site_name] = collection
                    for group in site.get("azure_groups", []) or []:
                        if isinstance(group, str) and group.strip():
                            mapping[group.strip()] = collection

                for key, value in (config.get("permission_mapping", {}).get("mappings", {}) or {}).items():
                    if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip():
                        mapping[key.strip()] = value.strip()
                        register_collection_aliases(value.strip())
        except Exception as e:
            logger.warning(f"No se pudo cargar SHAREPOINT_SITES_CONFIG: {e}")

        for builtin_collection in [self.valves.DEFAULT_COLLECTION, *self.valves.GLOBAL_READ_COLLECTIONS]:
            register_collection_aliases(builtin_collection)

        # Alias adicionales de colecciones para resolución por nombre natural
        # Añadir aquí aliases específicos de los departamentos configurados
        # Ejemplo: mapping["nombre_departamento"] = "documents_NombreDepartamento"
        register_collection_aliases("webs")

        self._department_mapping = mapping
        self._normalized_department_mapping = {
            self._normalize_text(key): value for key, value in mapping.items() if key and value
        }
        self._configured_collections = sorted(collections)

    @staticmethod
    def _coerce_claim_list(value) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("[") and text.endswith("]"):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]
                except Exception:
                    pass
            return [part.strip() for part in re.split(r"[,;]", text) if part.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    def _iter_claim_dicts(self, user_data: Dict) -> List[Dict]:
        claim_dicts: List[Dict] = []
        for candidate in [
            user_data,
            user_data.get("info"),
            user_data.get("claims"),
            user_data.get("oauth_user_info"),
            user_data.get("metadata"),
        ]:
            if isinstance(candidate, dict):
                claim_dicts.append(candidate)
        return claim_dicts

    def _resolve_collection_name(self, value: str) -> Optional[str]:
        candidate = str(value or "").strip()
        if not candidate:
            return None
        if candidate in self._department_mapping:
            return self._department_mapping[candidate]
        normalized = self._normalize_text(candidate)
        if normalized in self._normalized_department_mapping:
            return self._normalized_department_mapping[normalized]
        if candidate in self._configured_collections:
            return candidate
        return None

    def _extract_explicit_collections(self, user_data: Dict) -> List[str]:
        collections: List[str] = []
        claim_keys = [
            "tenant_ids",
            "tenantIds",
            "allowed_collections",
            "allowedCollections",
            "collections",
            "collection_access",
        ]

        for claim_dict in self._iter_claim_dicts(user_data):
            for key in claim_keys:
                for raw_value in self._coerce_claim_list(claim_dict.get(key)):
                    resolved = self._resolve_collection_name(raw_value)
                    if resolved and resolved not in collections:
                        collections.append(resolved)
        return collections

    def _extract_user_groups(self, user_data: Dict) -> List[str]:
        groups: List[str] = []
        for claim_dict in self._iter_claim_dicts(user_data):
            for key in ("groups", "oauth_groups", "azure_groups"):
                for raw_value in self._coerce_claim_list(claim_dict.get(key)):
                    if raw_value not in groups:
                        groups.append(raw_value)
        return groups

    def _user_can_access_collection(self, user_data: Dict, collection_name: str) -> bool:
        return collection_name in self._get_user_departments(user_data)

    def _get_global_read_collections(self) -> List[str]:
        collections: List[str] = []
        for collection in self.valves.GLOBAL_READ_COLLECTIONS:
            resolved = self._resolve_collection_name(collection) or collection
            if resolved and resolved not in collections:
                collections.append(resolved)
        return collections

    def _get_private_web_collection_for_email(self, email: str) -> Optional[str]:
        clean_email = (email or "").strip().lower()
        if not clean_email or clean_email == "anonymous":
            return None
        digest = hashlib.sha1(clean_email.encode("utf-8")).hexdigest()[:12]
        return f"webs_user_{digest}"

    def _get_private_web_collection(self, user_data: Dict) -> Optional[str]:
        if not isinstance(user_data, dict):
            return None

        identity_candidates = [
            user_data.get("email"),
            user_data.get("id"),
            user_data.get("username"),
            user_data.get("name"),
        ]
        for candidate in identity_candidates:
            value = str(candidate or "").strip().lower()
            if not value or value == "anonymous":
                continue
            digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
            return f"webs_user_{digest}"
        return None

    def _build_web_lookup_headers(self, user_email: str) -> Dict[str, str]:
        collections = ["webs"]
        private_collection = self._get_private_web_collection_for_email(user_email)
        if private_collection and private_collection not in collections:
            collections.insert(0, private_collection)
        return {"X-Tenant-Ids": ",".join(collections)}

    def _build_private_web_write_headers(self, user_data: Dict) -> Dict[str, str]:
        target_collection = self._get_private_web_collection(user_data) or "webs"
        return {"X-Tenant-Id": target_collection}

    def _litellm_headers(self) -> Dict[str, str]:
        api_key = (self.valves.LITELLM_API_KEY or "").strip()
        if not api_key:
            return {}
        return {"Authorization": f"Bearer {api_key}"}

    @staticmethod
    def _normalize_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKD", (text or "").strip().lower())
        return "".join(ch for ch in normalized if not unicodedata.combining(ch))

    @staticmethod
    def _extract_message_content(message: Dict) -> str:
        content = (message or {}).get("content", "")
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if text:
                        text_parts.append(text)
            return " ".join(text_parts)
        return str(content or "")

    def _get_last_assistant_message(self, messages: List[Dict]) -> str:
        for msg in reversed(messages or []):
            if msg.get("role") == "assistant":
                content = self._extract_message_content(msg)
                if content:
                    return content
        return ""

    def _is_generic_followup(self, message: str) -> bool:
        message_norm = self._normalize_text(message)
        generic_phrases = [
            "hablame mas",
            "puedes hablarme mas",
            "mas de eso",
            "mas detalles",
            "dame mas detalle",
            "explica mas",
            "cuentame mas",
            "profundiza",
            "amplia eso",
            "amplia",
            "y eso",
            "y de eso",
            "que mas",
        ]
        if any(phrase in message_norm for phrase in generic_phrases):
            return True
        words = [w for w in re.findall(r"[a-z0-9]{2,}", message_norm)]
        return len(words) <= 6 and any(
            token in message_norm for token in ["eso", "esto", "esa", "ese", "ello", "mismo", "anterior"]
        )

    def _looks_like_help_response(self, text: str) -> bool:
        text_norm = self._normalize_text(text)
        help_markers = [
            "guia rapida de jarvis",
            "ayuda ampliada de jarvis",
            "jarvis puede ayudarte con esto",
            "si quieres la version detallada",
        ]
        return any(marker in text_norm for marker in help_markers)

    def _is_help_followup(self, message: str, messages: List[Dict]) -> bool:
        last_assistant = self._get_last_assistant_message(messages or [])
        if not last_assistant or not self._looks_like_help_response(last_assistant):
            return False

        message_norm = self._normalize_text(message)
        explicit_followups = {
            "mas",
            "mas ayuda",
            "mas detalle",
            "mas detalles",
            "mas ejemplos",
            "detalle",
            "detallalo",
            "detallamelo",
            "amplialo",
            "sigue",
            "continua",
            "continuar",
        }
        if message_norm in explicit_followups:
            return True
        return self._is_generic_followup(message)

    def _match_help_request(self, message: str, messages: Optional[List[Dict]] = None) -> Optional[Dict]:
        message_norm = self._normalize_text(message)
        help_keywords = [
            "ayuda",
            "help",
            "que puedes hacer",
            "capacidades",
            "instrucciones",
            "manual",
            "como funcionas",
            "que sabes hacer",
            "funciones disponibles",
            "comandos",
            "funcionalidades",
            "manual de uso",
            "quien eres",
        ]
        direct_detailed_keywords = {
            "mas ayuda",
            "ayuda detallada",
            "ayuda completa",
            "manual detallado",
            "manual completo",
            "help detailed",
        }

        if self._is_help_followup(message, messages or []):
            return {"action": "help", "metadata": {"detailed": True}}

        if message_norm in direct_detailed_keywords:
            return {"action": "help", "metadata": {"detailed": True}}

        if message_norm in help_keywords or any(message_norm.startswith(f"dime {kw}") for kw in help_keywords):
            return {"action": "help", "metadata": {"detailed": False}}

        return None

    def _extract_boe_keywords(self, query: str) -> List[str]:
        stopwords = {
            "boe", "del", "de", "la", "el", "los", "las", "que", "dice", "sobre", "hoy", "ayer",
            "mas", "eso", "esto", "esa", "ese", "una", "unas", "unos", "para", "por",
            "con", "sin", "en", "al", "lo", "me", "puedes", "hablarme", "cuentame", "dime",
            "noticias", "sumario", "resumen",
        }
        tokens = re.findall(r"[a-z0-9]{3,}", self._normalize_text(query))
        return [token for token in tokens if token not in stopwords]

    def _rank_boe_results(self, query: str, results: List[Dict], focus_result: Optional[Dict] = None) -> List[Dict]:
        if not results:
            return []

        keywords = self._extract_boe_keywords(query)
        query_norm = self._normalize_text(query)
        is_generic = self._is_generic_followup(query)
        scored_results = []

        for index, result in enumerate(results):
            raw_title = str(result.get("title", "") or "")
            title_norm = self._normalize_text(result.get("title", ""))
            summary_norm = self._normalize_text(result.get("summary", ""))
            section_norm = self._normalize_text(result.get("section", ""))
            combined = f"{title_norm} {summary_norm} {section_norm}"
            score = 0.0
            word_count = len(re.findall(r"[a-z0-9]+", title_norm))
            is_short_title = word_count <= 2 or len(title_norm) <= 18
            is_uppercase_short = raw_title.isupper() and word_count <= 3
            is_legislative_title = title_norm.startswith("ley ") or title_norm.startswith("real decreto") or title_norm.startswith("decreto-ley")
            is_correction = title_norm.startswith("correccion de errores")

            if keywords:
                title_hits = sum(1 for kw in keywords if kw in title_norm)
                summary_hits = sum(1 for kw in keywords if kw in summary_norm)
                score += (title_hits * 3.0) + summary_hits
                if query_norm and query_norm in combined:
                    score += 6.0
                if title_hits == len(keywords):
                    score += 4.0
                meaningful_law_keywords = [
                    kw for kw in keywords
                    if kw not in {"ley", "decreto", "real", "reglamento", "normativa", "legislacion", "articulo", "disposicion", "resolucion"}
                ]
                if is_legislative_title:
                    score += 3.0
                    if meaningful_law_keywords and all(kw in title_norm for kw in meaningful_law_keywords):
                        score += 5.0
                if is_correction and any(kw in keywords for kw in {"ley", "decreto", "reglamento", "normativa"}):
                    score -= 6.0
            else:
                score += max(0.0, 5.0 - index * 0.1)
                if word_count >= 6:
                    score += 3.0
                elif word_count >= 4:
                    score += 1.5
                if not is_short_title:
                    score += 1.5
                if any(token in title_norm for token in ["anuncio de", "real decreto", "orden ", "resolucion", "formalizacion"]):
                    score += 3.0
                if "contratacion del sector publico" in section_norm:
                    score += 2.0
                if is_uppercase_short:
                    score -= 5.0
                elif is_short_title:
                    score -= 2.0

            if focus_result and result.get("link") == focus_result.get("link"):
                score += 8.0 if is_generic else 3.0

            if result.get("date"):
                score += 0.01

            scored_results.append((score, index, result))

        scored_results.sort(key=lambda item: (item[0], -item[1]), reverse=True)

        deduped = []
        seen_links = set()
        for _, _, result in scored_results:
            link = result.get("link") or result.get("title")
            if link in seen_links:
                continue
            seen_links.add(link)
            deduped.append(result)
        return deduped

    def _store_boe_memory(self, conversation_key: str, boe_type: str, query: str, results: List[Dict]) -> None:
        ranked = self._rank_boe_results(query, results)
        self._user_boe_memory[conversation_key] = {
            "type": boe_type,
            "query": query,
            "results": ranked[:10],
            "focus_result": ranked[0] if ranked else None,
            "fetched_at": time.time(),
        }

    def _is_boe_focus_summary_request(self, message: str) -> bool:
        message_norm = self._normalize_text(message)
        summary_markers = [
            "resume",
            "resumen",
            "resumeme",
            "de que trata",
            "que dice",
            "que establece",
            "explica",
            "detalla",
            "mas detalle",
            "mas informacion",
            "hablame mas",
            "cuentame mas",
            "amplia",
        ]
        reference_markers = [
            "esta ley",
            "esa ley",
            "la ley",
            "este anuncio",
            "ese anuncio",
            "el anuncio",
            "la primera",
            "el primero",
            "primer resultado",
            "segundo resultado",
            "segunda",
            "segunda ley",
            "tercera",
            "ultima",
            "ultimo",
            "esto",
            "eso",
        ]
        return any(marker in message_norm for marker in summary_markers) and (
            any(marker in message_norm for marker in reference_markers)
            or self._is_generic_followup(message)
            or "ley" in message_norm
            or "anuncio" in message_norm
        )

    def _contains_boe_reference(self, message: str) -> bool:
        message_norm = self._normalize_text(message)
        return bool(
            re.search(r"\bboe\b", message_norm)
            or "boe.es" in message_norm
            or "boletin oficial del estado" in message_norm
            or "boletin oficial" in message_norm
        )

    def _is_boe_summary_request(self, message: str) -> bool:
        message_norm = self._normalize_text(message)
        summary_markers = [
            "noticias",
            "publicaciones",
            "publicado",
            "sumario",
            "resumen",
            "novedades",
            "ha salido",
            "ha publicado",
            "salio hoy",
            "sale hoy",
        ]
        return any(marker in message_norm for marker in summary_markers)

    def _select_boe_focus_result(self, message: str, results: List[Dict], fallback: Optional[Dict] = None) -> Optional[Dict]:
        if not results and not fallback:
            return None

        message_norm = self._normalize_text(message)
        ranked_results = results or ([fallback] if fallback else [])
        direct_reference_markers = [
            "esta ley",
            "esa ley",
            "la ley",
            "este anuncio",
            "ese anuncio",
            "el anuncio",
            "esto",
            "eso",
            "ese",
            "esa",
        ]
        ordinal_map = {
            0: ["primera", "primero", "1", "primer resultado"],
            1: ["segunda", "segundo", "2", "segundo resultado"],
            2: ["tercera", "tercero", "3", "tercer resultado"],
        }
        for index, markers in ordinal_map.items():
            if index < len(ranked_results) and any(marker in message_norm for marker in markers):
                return ranked_results[index]

        if ranked_results and any(marker in message_norm for marker in ["ultima", "ultimo", "ultimo resultado"]):
            return ranked_results[-1]

        if fallback and (self._is_generic_followup(message) or any(marker in message_norm for marker in direct_reference_markers)):
            return fallback

        if ranked_results:
            meaningful_keywords = [
                token for token in self._extract_boe_keywords(message)
                if token not in {"resume", "resumen", "resumeme", "ley", "anuncio", "esta", "esa", "esto", "eso"}
            ]
            if meaningful_keywords:
                rescored = self._rank_boe_results(" ".join(meaningful_keywords), ranked_results, focus_result=fallback)
                if rescored:
                    top_candidate = rescored[0]
                    if any(token in self._normalize_text(top_candidate.get("title", "")) for token in meaningful_keywords):
                        return top_candidate

        return fallback or (ranked_results[0] if ranked_results else None)

    def _format_boe_focus_result_response(self, result: Dict) -> str:
        title = result.get("title", "Sin titulo")
        date = result.get("date", "N/D")
        summary = str(result.get("summary", "") or "").strip()
        section = result.get("section") or result.get("source") or ""
        link = result.get("link", "")

        lines = [f"**Resultado principal**: {title}", f"**Fecha**: {date}"]
        if section:
            lines.append(f"**Tipo/Fuente**: {section}")

        if summary and self._normalize_text(summary) not in {"sin resumen", "n/d", "sin titulo"}:
            lines.append(f"**Resumen disponible**: {summary}")
        else:
            lines.append(
                "**Resumen disponible**: No dispongo de un resumen oficial más amplio en este resultado; "
                "con este contexto puedo confirmar el título, la fecha y el enlace oficial."
            )

        if link:
            lines.append(f"**Enlace oficial**: {link}")
        return "\n".join(lines)

    def _is_boe_followup(self, conversation_key: str, message: str, messages: List[Dict]) -> bool:
        stored = self._user_boe_memory.get(conversation_key)
        if not stored:
            return False

        message_norm = self._normalize_text(message)
        if re.search(r"https?://", message or "", flags=re.IGNORECASE):
            return False
        if "boe" in message_norm:
            return True
        if self._is_generic_followup(message):
            return True
        if any(
            phrase in message_norm
            for phrase in [
                "que dice sobre",
                "que mas dice",
                "de que trata",
                "que menciona",
                "menciona algo sobre",
                "resume esta ley",
                "resume la ley",
                "resume este anuncio",
            ]
        ):
            return True

        last_assistant = self._normalize_text(self._get_last_assistant_message(messages))
        return bool(
            last_assistant
            and "fuentes boe" in last_assistant
            and (len(message_norm.split()) <= 8 or self._is_boe_focus_summary_request(message))
        )

    def _is_rag_followup(self, message: str, messages: List[Dict]) -> bool:
        if not messages or len(messages) < 2:
            return False

        message_norm = self._normalize_text(message)
        if not message_norm:
            return False

        if re.search(r"https?://", message or "", flags=re.IGNORECASE):
            return False

        last_assistant = self._normalize_text(self._get_last_assistant_message(messages))
        if not last_assistant:
            return False

        rag_context_markers = [
            "fuentes citadas",
            "consultando documentos internos",
            "relevancia:",
            "pag.",
            "pág.",
        ]
        if not any(marker in last_assistant for marker in rag_context_markers):
            return False

        direct_markers = [
            "documento",
            "ese documento",
            "este documento",
            "que dice el documento",
            "que hace",
            "que mas dice",
            "que más dice",
            "dime mas",
            "dime más",
            "explica mas",
            "explica más",
            "resumelo",
            "resúmelo",
            "amplia",
            "amplía",
            "detalla",
            "necesito escribir un correo",
        ]
        if any(marker in message_norm for marker in direct_markers):
            return True

        return self._is_generic_followup(message)

    def _infer_boe_type(self, message: str, fallback_type: Optional[str] = None) -> str:
        message_norm = self._normalize_text(message)

        summary_keywords = [
            "noticias de hoy del boe",
            "noticias de hoy en el boe",
            "noticias del boe",
            "publicaciones del boe",
            "publicaciones de hoy del boe",
            "publicaciones de hoy en el boe",
            "publicaciones hoy del boe",
            "sumario de hoy del boe",
            "sumario del boe",
            "resumen del boe",
            "boe de hoy",
            "que ha salido en el boe",
            "que se ha publicado en el boe",
            "novedades del boe",
        ]
        tender_keywords = [
            "licitacion", "licitaciones", "concurso publico", "concurso", "adjudicacion",
            "formalizacion de contratos", "formalizacion", "expediente", "contratacion publica",
        ]
        legislation_keywords = [
            "ley", "real decreto", "decreto", "reglamento", "normativa", "legislacion",
            "articulo", "disposicion", "resolucion",
        ]

        if any(keyword in message_norm for keyword in summary_keywords):
            return "summary"
        if self._contains_boe_reference(message_norm) and (
            self._is_boe_summary_request(message_norm)
            or ("hoy" in message_norm and "boe" in message_norm)
        ):
            return "summary"
        if any(keyword in message_norm for keyword in legislation_keywords):
            return "legislation"
        if any(keyword in message_norm for keyword in tender_keywords):
            return "tenders"
        if fallback_type:
            return fallback_type
        return "legislation"

    def _get_user_departments(self, user_data: Dict) -> List[str]:
        """
        Obtiene las colecciones/departamentos a los que el usuario tiene acceso.
        Solo expone colecciones explicitamente autorizadas por claims/grupos.
        """
        logger.info("Permission resolution start")
        if not self.valves.MULTI_DEPARTMENT_ENABLED:
            return [self.valves.DEFAULT_COLLECTION]

        email = user_data.get("email", "anonymous")
        logger.info(f"Resolving permissions for: {email}")

        if self.valves.DEBUG_MODE:
            logger.debug(f"Permission payload keys for {email}: {list(user_data.keys())}")

        if email in self._permission_cache:
            cache_time, cached_depts = self._permission_cache[email]
            if time.time() - cache_time < self.valves.PERMISSION_CACHE_TTL:
                logger.debug(f"Cache hit para {email}: {cached_depts}")
                return cached_depts

        departments = self._get_global_read_collections()
        private_web_collection = self._get_private_web_collection(user_data)
        if private_web_collection and private_web_collection not in departments:
            departments.append(private_web_collection)

        for collection in self._extract_explicit_collections(user_data):
            if collection not in departments:
                departments.append(collection)
        groups = self._extract_user_groups(user_data)
        logger.info(f"DEBUG groups encontrados: {groups}")

        if not groups:
            try:
                graph_groups = self._get_user_groups_from_graph(email)
                if graph_groups:
                    groups = graph_groups
                    logger.info(f"Grupos obtenidos de Graph API para {email}: {len(groups)}")
            except Exception as e:
                logger.error(f"Error obteniendo grupos de Graph API: {e}")

        logger.info(f"DEBUG groups finales: {groups}")

        for group in groups:
            collection = self._resolve_collection_name(group)
            if collection and collection not in departments:
                departments.append(collection)

        role = user_data.get("role", "")
        if role == "admin":
            for coll in self._configured_collections:
                if coll not in departments:
                    departments.append(coll)
            logger.info(f"Usuario admin {email}: acceso a todas las colecciones configuradas")

        logger.info(f"Usuario {email} tiene acceso a: {departments}")
        self._permission_cache[email] = (time.time(), departments)
        return departments

    def _get_user_groups_from_graph(self, email: str) -> List[str]:
        """
        Consulta los grupos del usuario directamente a Microsoft Graph API.
        Requiere AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET.
        """
        try:
            tenant_id = os.getenv("AZURE_TENANT_ID")
            client_id = os.getenv("AZURE_CLIENT_ID")
            client_secret = os.getenv("AZURE_CLIENT_SECRET")
            
            if not (tenant_id and client_id and client_secret):
                logger.warning("Credenciales Azure no configuradas en entorno, saltando Graph API")
                return []
                
            # 1. Obtener token
            token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
            token_data = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default"
            }
            token_resp = requests.post(token_url, data=token_data, timeout=10)
            token_resp.raise_for_status()
            token = token_resp.json().get("access_token")
            
            # 2. Obtener usuario por email para sacar su ID
            # (Usamos filter porque el email puede ser UPN o mail)
            graph_url = "https://graph.microsoft.com/v1.0/users"
            headers = {"Authorization": f"Bearer {token}"}
            
            # Intentar buscar por mail o userPrincipalName
            user_resp = requests.get(
                f"{graph_url}?$filter=mail eq '{email}' or userPrincipalName eq '{email}'&$select=id",
                headers=headers,
                timeout=10
            )
            
            if user_resp.status_code != 200:
                logger.warning(f"No se encontró usuario {email} en Graph: {user_resp.text}")
                return []
                
            users = user_resp.json().get("value", [])
            if not users:
                logger.warning(f"Usuario {email} no encontrado en Azure AD")
                return []
                
            user_id = users[0]["id"]
            
            # 3. Obtener grupos transitivos (memberOf)
            # Solo nos interesan los IDs y nombres
            groups_resp = requests.post(
                f"{graph_url}/{user_id}/getMemberObjects",
                headers=headers,
                json={"securityEnabledOnly": False},
                timeout=10
            )
            groups_resp.raise_for_status()
            
            # getMemberObjects devuelve solo IDs de string
            group_ids = groups_resp.json().get("value", [])
            
            logger.info(f"Graph API: Encontrados {len(group_ids)} grupos para {email}")
            logger.info(f"DEBUG Graph Groups IDs: {group_ids}")
            return group_ids
            
        except Exception as e:
            logger.error(f"Error consultando Graph API: {e}")
            return []

    def _get_user_email(self, body: Dict) -> str:
        """Extrae un identificador estable del usuario del cuerpo de la petición."""
        user = body.get("user", {})
        if isinstance(user, dict):
            identity_candidates = [
                user.get("email"),
                user.get("id"),
                user.get("username"),
                user.get("name"),
            ]
            for candidate in identity_candidates:
                value = str(candidate or "").strip()
                if value and value.lower() != "anonymous":
                    return value
        return "anonymous"

    def _get_conversation_scope_key(self, body: Dict, messages: Optional[List[Dict]] = None) -> str:
        user_key = self._get_user_email(body)
        candidates: List[str] = []

        def add_candidate(value) -> None:
            text = str(value or "").strip()
            if text and text.lower() not in {"none", "null", "0"}:
                candidates.append(text)

        for key in ("conversation_id", "conversationId", "chat_id", "chatId", "id"):
            add_candidate(body.get(key))

        for container_key in ("chat", "conversation", "metadata"):
            container = body.get(container_key)
            if isinstance(container, dict):
                for key in ("id", "conversation_id", "conversationId", "chat_id", "chatId"):
                    add_candidate(container.get(key))

        user = body.get("user", {})
        if isinstance(user, dict):
            metadata = user.get("metadata")
            if isinstance(metadata, dict):
                for key in ("id", "conversation_id", "conversationId", "chat_id", "chatId"):
                    add_candidate(metadata.get(key))

        for candidate in candidates:
            return f"{user_key}::{candidate}"

        first_user_text = ""
        for msg in messages or []:
            if msg.get("role") != "user":
                continue
            first_user_text = self._extract_message_content(msg).strip()
            if first_user_text:
                break

        if not first_user_text:
            return f"{user_key}::default"

        fingerprint = hashlib.sha1(first_user_text[:4000].encode("utf-8")).hexdigest()[:16]
        return f"{user_key}::{fingerprint}"

    def _get_recent_file_memory(self, conversation_key: str) -> Optional[Dict]:
        stored = self._user_file_memory.get(conversation_key)
        if not stored:
            return None
        if time.time() - float(stored.get("fetched_at", 0)) > self.valves.FILE_CONTEXT_TTL:
            self._user_file_memory.pop(conversation_key, None)
            return None
        return stored

    def _get_recent_rag_memory(self, conversation_key: str) -> Optional[Dict]:
        stored = self._user_rag_memory.get(conversation_key)
        if not stored:
            return None
        if time.time() - float(stored.get("fetched_at", 0)) > self.valves.RAG_CONTEXT_TTL:
            self._user_rag_memory.pop(conversation_key, None)
            return None
        return stored

    def _store_rag_memory(self, conversation_key: str, query: str, answer: str, sources: List[Dict]) -> Dict:
        primary_filename = ""
        if sources:
            primary_filename = str(sources[0].get("filename", "") or "").strip()
        stored = {
            "query": query,
            "answer": answer,
            "sources": list(sources or []),
            "primary_filename": primary_filename,
            "fetched_at": time.time(),
        }
        self._user_rag_memory[conversation_key] = stored
        return stored

    def _extract_file_context_from_messages(self, messages: List[Dict], user_message: str) -> Dict:
        last_msg = messages[-1] if messages else {}
        content = (last_msg or {}).get("content", "")
        if isinstance(content, list):
            content = self._extract_message_content(last_msg)

        filename = "documento"
        user_query = (user_message or "").strip() or "Resume el documento."
        sources: List[Dict[str, str]] = []
        file_context = ""

        if isinstance(content, str):
            context_match = re.search(r"<context>(.*?)</context>", content, re.DOTALL | re.IGNORECASE)
            query_match = re.search(r"<user_query>(.*?)</user_query>", content, re.DOTALL | re.IGNORECASE)
            if context_match:
                file_context = (context_match.group(1) or "").strip()
            if query_match and query_match.group(1).strip():
                user_query = query_match.group(1).strip()

            source_matches = re.findall(r'<source[^>]*name="([^"]*)"[^>]*>(.*?)</source>', content, re.DOTALL | re.IGNORECASE)
            for source_name, source_text in source_matches:
                clean_name = (source_name or "documento").strip() or "documento"
                clean_text = (source_text or "").strip()
                if clean_text:
                    sources.append({"name": clean_name, "text": clean_text})

            if sources:
                filename = sources[0]["name"]
                if not file_context:
                    file_context = "\n\n".join(source["text"] for source in sources)
            elif len(content) > 2000:
                file_context = content.strip()

            lines = [line.strip() for line in content.splitlines()]
            for line in reversed(lines):
                if line and len(line) < 300 and not line.startswith("<"):
                    user_query = line
                    break

        if file_context and not sources:
            sources = [{"name": filename, "text": file_context}]

        return {
            "filename": filename,
            "user_query": user_query,
            "file_context": file_context,
            "sources": sources,
        }

    def _chunk_file_source(self, text: str, source_name: str) -> List[Dict]:
        clean_text = (text or "").strip()
        if not clean_text:
            return []

        chunk_size = max(400, int(self.valves.FILE_CHUNK_SIZE))
        overlap = max(0, min(int(self.valves.FILE_CHUNK_OVERLAP), chunk_size // 3))
        chunks: List[Dict] = []
        start = 0
        chunk_index = 1

        while start < len(clean_text):
            end = min(len(clean_text), start + chunk_size)
            if end < len(clean_text):
                paragraph_end = clean_text.rfind("\n\n", start, end)
                sentence_end = clean_text.rfind(". ", start, end)
                best_end = max(paragraph_end, sentence_end)
                if best_end > start + (chunk_size // 2):
                    end = best_end + 1
            snippet = clean_text[start:end].strip()
            if snippet:
                chunks.append({"id": chunk_index, "source_name": source_name, "text": snippet})
                chunk_index += 1
            if end >= len(clean_text):
                break
            start = max(end - overlap, start + 1)

        return chunks

    def _store_file_memory(self, conversation_key: str, parsed: Dict) -> Dict:
        sources = parsed.get("sources", []) or []
        chunks: List[Dict] = []
        for source in sources:
            chunks.extend(
                self._chunk_file_source(
                    source.get("text", ""),
                    source.get("name", parsed.get("filename", "documento")),
                )
            )

        stored = {
            "filename": parsed.get("filename", "documento"),
            "chunks": chunks,
            "sources": [source.get("name", "documento") for source in sources],
            "fetched_at": time.time(),
        }
        self._user_file_memory[conversation_key] = stored
        return stored

    def _score_file_chunk(self, query: str, chunk_text: str, chunk_id: int) -> float:
        query_norm = self._normalize_text(query)
        chunk_norm = self._normalize_text(chunk_text)
        query_tokens = [token for token in re.findall(r"[a-z0-9]{3,}", query_norm)]
        score = 0.0

        if query_tokens:
            overlap = sum(1 for token in query_tokens if token in chunk_norm)
            score += overlap * 3.0
            if query_norm and query_norm in chunk_norm:
                score += 6.0
        elif self._is_generic_followup(query):
            score += 4.0

        if any(keyword in query_norm for keyword in ["resumen", "resume", "summary", "de que trata", "de que va"]):
            score += max(0.0, 5.0 - (chunk_id - 1))

        return score

    def _retrieve_file_chunks(self, query: str, stored: Dict) -> List[Dict]:
        chunks = stored.get("chunks", []) or []
        if not chunks:
            return []
        ranked = sorted(
            chunks,
            key=lambda chunk: self._score_file_chunk(query, chunk.get("text", ""), int(chunk.get("id", 0) or 0)),
            reverse=True,
        )
        top_chunks = [chunk for chunk in ranked[: self.valves.FILE_TOP_K] if chunk.get("text")]
        return top_chunks or chunks[: self.valves.FILE_TOP_K]

    @staticmethod
    def _extract_file_sentences(text: str) -> List[str]:
        sentences = re.split(r"(?<=[.!?])\s+|\n+", text or "")
        return [sentence.strip() for sentence in sentences if len(sentence.strip()) > 20]

    def _is_refusal_text(self, text: str) -> bool:
        text_norm = self._normalize_text(text)
        refusal_markers = [
            "lo siento", "no puedo", "cannot", "cant", "i cannot",
            "no puedo responder", "no puedo cumplir", "no puedo continuar",
        ]
        return any(marker in text_norm for marker in refusal_markers)

    def _build_file_fallback_answer(self, query: str, chunks: List[Dict]) -> str:
        if not chunks:
            return "No he podido extraer informacion suficiente del archivo."

        query_norm = self._normalize_text(query)
        frequency_map = [
            ("anual", "anualmente, es decir, una vez al ano"),
            ("mensual", "mensualmente, es decir, una vez al mes"),
            ("semanal", "semanalmente, es decir, una vez por semana"),
            ("diari", "a diario"),
            ("trimestral", "trimestralmente"),
            ("semestral", "semestralmente"),
        ]

        if any(token in query_norm for token in ["cada cuanto", "cuanto", "cada cuánto", "frecuencia", "when", "how often"]):
            for chunk in chunks:
                chunk_norm = self._normalize_text(chunk.get("text", ""))
                for token, answer in frequency_map:
                    if token in chunk_norm:
                        return f"Deben hacerse {answer} [bloque {chunk.get('id')}]."

        if any(token in query_norm for token in ["resumen", "resume", "summary", "de que trata", "de que va"]):
            parts = []
            for chunk in chunks[:2]:
                sentences = self._extract_file_sentences(chunk.get("text", ""))
                if sentences:
                    parts.append(f"{sentences[0]} [bloque {chunk.get('id')}]")
            if parts:
                return "Resumen: " + " ".join(parts)

        best_chunk = chunks[0]
        sentences = self._extract_file_sentences(best_chunk.get("text", ""))
        best_sentence = sentences[0] if sentences else (best_chunk.get("text", "") or "").strip()
        if not best_sentence:
            best_sentence = "No he podido identificar una frase util en el fragmento."

        if any(token in query_norm for token in ["que exige", "que dice", "que indica", "que establece", "what does"]):
            return f"El documento indica: {best_sentence} [bloque {best_chunk.get('id')}]."

        return f"Segun el documento: {best_sentence} [bloque {best_chunk.get('id')}]."

    def _build_rag_followup_fallback(self, user_message: str, stored: Dict) -> str:
        answer = str(stored.get("answer", "") or "").strip()
        primary_filename = stored.get("primary_filename") or "el documento anterior"
        sentences = self._extract_file_sentences(answer)
        base_summary = " ".join(sentences[:2]).strip() if sentences else answer[:600].strip()
        query_norm = self._normalize_text(user_message)

        if not base_summary:
            return f"No tengo mas detalle fiable sobre {primary_filename} en este momento."

        if any(token in query_norm for token in ["correo", "email", "mail"]):
            return (
                f"Asunto: Resumen de {primary_filename}\n\n"
                f"Hola,\n\n"
                f"Te resumo lo que indica {primary_filename}: {base_summary}\n\n"
                "Si quieres, puedo convertirlo en un correo mas formal o mas tecnico."
            )

        if any(token in query_norm for token in ["dime mas", "dime más", "mas detalle", "más detalle", "explica", "amplia", "amplía"]):
            return f"Ampliando sobre {primary_filename}: {base_summary}"

        return f"Segun {primary_filename}: {base_summary}"

    def _handle_rag_followup(
        self,
        conversation_key: str,
        user_message: str,
        messages: List[Dict],
    ) -> Generator:
        stored = self._get_recent_rag_memory(conversation_key)
        if not stored:
            return

        yield "\U0001f4da **Consultando documentos internos...**\n\n"

        primary_filename = stored.get("primary_filename") or "documento anterior"
        prior_answer = str(stored.get("answer", "") or "").strip()
        source_blocks = []
        for src in (stored.get("sources") or [])[:4]:
            filename = src.get("filename", "documento")
            page = src.get("page")
            snippet = str(src.get("snippet", "") or "").strip()
            if not snippet:
                continue
            header = filename
            if page:
                header += f" (pag. {page})"
            source_blocks.append(f"[{header}]\n{snippet}")

        context_parts = [f"Documento principal: {primary_filename}"]
        if prior_answer:
            context_parts.append(f"Respuesta anterior:\n{prior_answer}")
        if source_blocks:
            context_parts.append("Fragmentos relevantes:\n" + "\n\n".join(source_blocks))
        context_text = "\n\n".join(context_parts)

        prompt = (
            "Responde usando SOLO el contexto del mismo documento ya consultado.\n"
            "Mantén el foco en ese documento y no cambies a otros.\n"
            "Si el usuario pide mas detalle, amplia la explicacion.\n"
            "Si el usuario pide ayuda para redactar un correo, prepara un borrador breve y profesional.\n"
            "No rechaces la peticion salvo que realmente falte informacion en el contexto.\n\n"
            f"{context_text}\n\n"
            f"Nueva peticion del usuario: {user_message}"
        )

        try:
            answer = self._call_litellm(
                message=prompt,
                model=self.valves.TEXT_MODEL,
                system_prompt=(
                    "Eres un asistente documental de empresa. "
                    "Siempre respondes en el idioma del usuario y mantienes el foco del documento previo."
                ),
            )
            if self._is_refusal_text(answer):
                answer = self._build_rag_followup_fallback(user_message, stored)
        except Exception as e:
            logger.warning(f"Error en RAG follow-up contextual: {e}")
            answer = self._build_rag_followup_fallback(user_message, stored)

        yield answer

        sources = stored.get("sources", []) or []
        if sources:
            yield "\n\n---\n### \U0001f4da Fuentes Citadas:\n\n"
            seen = set()
            for i, src in enumerate(sources, 1):
                filename = src.get("filename", "Doc")
                page = src.get("page")
                key = f"{filename}_{page}" if page else filename
                if key in seen:
                    continue
                seen.add(key)
                citation = f"**[{i}]** \U0001f4c4 {filename}"
                if page:
                    citation += f" (p\u00e1g. {page})"
                score = src.get("score")
                if score is not None:
                    citation += f" - *relevancia: {float(score):.2f}*"
                yield f"{citation}\n"

        self._store_rag_memory(conversation_key, stored.get("query", user_message), answer, sources)

    def _build_web_fallback_answer(self, content: str, title: str = "contenido web") -> str:
        clean_content = (content or "").strip()
        if not clean_content:
            return f"No he encontrado contenido suficiente sobre {title}."

        paragraphs = [
            paragraph.strip()
            for paragraph in re.split(r"\n\s*\n+", clean_content)
            if len(paragraph.strip()) > 40
        ]
        if not paragraphs:
            paragraphs = self._extract_file_sentences(clean_content)

        parts = []
        for paragraph in paragraphs[:3]:
            snippet = " ".join(paragraph.split())
            if len(snippet) > 320:
                snippet = snippet[:317].rstrip() + "..."
            parts.append(snippet)

        if not parts:
            snippet = " ".join(clean_content.split())
            if len(snippet) > 320:
                snippet = snippet[:317].rstrip() + "..."
            parts.append(snippet)

        return f"Resumen de {title}: " + " ".join(parts)

    def _clean_html_fragment(self, text: str) -> str:
        cleaned = re.sub(r"<[^>]+>", " ", text or "")
        cleaned = html_lib.unescape(cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    def _apply_web_search_aliases(self, text: str) -> str:
        updated = text or ""
        alias_map = {}
        for pattern, replacement in alias_map.items():
            updated = re.sub(pattern, replacement, updated, flags=re.IGNORECASE)
        return updated.strip()

    def _extract_weather_request(self, message: str) -> Optional[Dict[str, str]]:
        text = (message or "").strip()
        text_norm = self._normalize_text(text)
        if not text_norm:
            return None

        weather_tokens = [
            "tiempo", "clima", "temperatura", "meteorologia", "meteorologia",
            "llueve", "llovera", "sol", "frio", "calor",
        ]
        if not any(token in text_norm for token in weather_tokens):
            return None

        when = "today"
        if "manana" in text_norm:
            when = "tomorrow"

        location = None
        location_patterns = [
            r"\b(?:en|por)\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'-]*(?:\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'-]*){0,3})",
            r"\bpara\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'-]*(?:\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'-]*){0,3})",
            r"\bde\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ .'-]{1,40})$",
        ]
        blocked_locations = {
            "internet", "web", "google", "hoy", "manana", "ahora", "mismo",
            "tiempo", "clima", "meteorologia", "meteorología", "hace",
        }
        for pattern in location_patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                candidate = match.group(1).strip(" .,:;!?")
                candidate = re.sub(
                    r"\s+(?:hoy|ahora(?:\s+mismo)?|manana|esta\s+tarde|esta\s+noche)$",
                    "",
                    candidate,
                    flags=re.IGNORECASE,
                ).strip(" .,:;!?")
                candidate_norm = self._normalize_text(candidate)
                candidate_tokens = set(re.findall(r"[a-z0-9]+", candidate_norm))
                if not candidate_norm or candidate_tokens.intersection(blocked_locations):
                    continue
                location = candidate

        if not location:
            return None

        return {"location": location, "when": when}

    def _weather_code_label(self, code: Optional[int]) -> str:
        mapping = {
            0: "despejado",
            1: "principalmente despejado",
            2: "parcialmente nuboso",
            3: "cubierto",
            45: "niebla",
            48: "niebla con escarcha",
            51: "llovizna ligera",
            53: "llovizna moderada",
            55: "llovizna intensa",
            61: "lluvia ligera",
            63: "lluvia moderada",
            65: "lluvia intensa",
            71: "nieve ligera",
            73: "nieve moderada",
            75: "nieve intensa",
            80: "chubascos ligeros",
            81: "chubascos moderados",
            82: "chubascos intensos",
            95: "tormenta",
            96: "tormenta con granizo ligero",
            99: "tormenta con granizo",
        }
        return mapping.get(int(code or 0), "condiciones variables")

    def _fetch_weather_summary(self, message: str) -> Optional[Dict[str, str]]:
        weather_request = self._extract_weather_request(message)
        if not weather_request:
            return None

        geo_response = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={
                "name": weather_request["location"],
                "count": 1,
                "language": "es",
                "format": "json",
            },
            timeout=20,
        )
        geo_response.raise_for_status()
        geo_data = geo_response.json()
        geo_results = geo_data.get("results") or []
        if not geo_results:
            return None

        place = geo_results[0]
        forecast_response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": 2,
            },
            timeout=20,
        )
        forecast_response.raise_for_status()
        forecast = forecast_response.json()

        daily = forecast.get("daily") or {}
        daily_times = daily.get("time") or []
        if not daily_times:
            return None

        index = 1 if weather_request["when"] == "tomorrow" and len(daily_times) > 1 else 0
        label = self._weather_code_label((daily.get("weather_code") or [None])[index])
        temp_max = (daily.get("temperature_2m_max") or [None])[index]
        temp_min = (daily.get("temperature_2m_min") or [None])[index]
        precip_max = (daily.get("precipitation_probability_max") or [None])[index]

        location_label = place.get("name", weather_request["location"])
        admin1 = place.get("admin1")
        country = place.get("country")
        if admin1 and admin1.lower() not in location_label.lower():
            location_label = f"{location_label}, {admin1}"
        elif country and country.lower() not in location_label.lower():
            location_label = f"{location_label}, {country}"

        if weather_request["when"] == "tomorrow":
            answer = f"Para manana en {location_label} se espera {label}."
        else:
            current = forecast.get("current") or {}
            current_temp = current.get("temperature_2m")
            current_label = self._weather_code_label(current.get("weather_code"))
            wind_speed = current.get("wind_speed_10m")
            answer = f"Ahora mismo en {location_label}: {current_temp} C, {current_label}"
            if wind_speed is not None:
                answer += f", viento {wind_speed} km/h"
            answer += "."

        daily_parts = []
        if temp_max is not None and temp_min is not None:
            daily_parts.append(f"Max {temp_max} C y min {temp_min} C")
        if precip_max is not None:
            daily_parts.append(f"probabilidad maxima de lluvia del {precip_max}%")
        if daily_parts:
            answer += " " + ", ".join(daily_parts) + "."

        return {
            "answer": answer,
            "source_title": f"Open-Meteo para {location_label}",
            "source_link": "https://open-meteo.com/",
        }

    def _fetch_local_activity_summary(self, message: str) -> Optional[Dict[str, object]]:
        if not self._is_local_activity_query(message):
            return None

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }
        source_defs = [
            ("https://www.esmadrid.com/agenda-madrid", "Agenda Madrid | Turismo Madrid"),
            ("https://www.timeout.es/madrid/es/que-hacer", "Que hacer en Madrid: los mejores planes de la ciudad"),
        ]

        sources = []
        timeout_titles = []
        for url, fallback_title in source_defs:
            response = requests.get(url, headers=headers, timeout=20)
            response.raise_for_status()
            html = response.text
            title_match = re.search(r"<title>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
            desc_match = re.search(
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
                html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            title = self._clean_html_fragment(title_match.group(1)) if title_match else fallback_title
            description = self._clean_html_fragment(desc_match.group(1)) if desc_match else ""
            sources.append({"title": title or fallback_title, "link": url, "snippet": description})

            if "timeout.es" in url:
                raw_titles = re.findall(r"<h3[^>]*>(.*?)</h3>", html, flags=re.IGNORECASE | re.DOTALL)
                seen_titles = set()
                for raw in raw_titles:
                    clean = self._clean_html_fragment(raw)
                    if len(clean) < 12 or clean.lower() in seen_titles:
                        continue
                    seen_titles.add(clean.lower())
                    timeout_titles.append(clean)
                    if len(timeout_titles) >= 3:
                        break

        message_norm = self._normalize_text(message)
        if "manana" in message_norm:
            when_label = "mañana"
        elif "fin de semana" in message_norm or "este finde" in message_norm:
            when_label = "este fin de semana"
        elif "esta noche" in message_norm:
            when_label = "esta noche"
        elif "esta tarde" in message_norm:
            when_label = "esta tarde"
        else:
            when_label = "hoy"
        answer_parts = [
            f"Para {when_label} por Madrid, las referencias más útiles ahora mismo son la agenda oficial de Turismo Madrid y la guía de Time Out Madrid."
        ]
        if sources and sources[0].get("snippet"):
            answer_parts.append(f"Turismo Madrid destaca: {sources[0]['snippet']}")
        if len(sources) > 1 and sources[1].get("snippet"):
            answer_parts.append(f"Time Out Madrid resume su selección así: {sources[1]['snippet']}")
        if timeout_titles:
            answer_parts.append("Algunos destacados visibles ahora mismo en portada son: " + "; ".join(timeout_titles) + ".")
        answer_parts.append("Si quieres, te lo afino por tipo: conciertos, teatro, exposiciones, gratis, niños o cena.")

        return {
            "answer": "\n\n".join(answer_parts),
            "sources": sources,
        }

    def _is_local_activity_query(self, message: str) -> bool:
        text_norm = self._normalize_text(message)
        if not text_norm:
            return False

        blocked_terms = [
            "plan de calidad",
            "plan de gestion",
            "plan de riesgos",
            "plan de continuidad",
            "plan de acogida",
            ".pdf",
            "documento",
        ]
        if any(term in text_norm for term in blocked_terms):
            return False

        leisure_patterns = [
            r"\bplanes?\s+para\b",
            r"\bque\s+planes?\b",
            r"\bplanes?\b",
            r"\bque\s+hacer\b",
            r"\bcosas\s+que\s+hacer\b",
            r"\beventos?\b",
            r"\bagenda\b",
            r"\bocio\b",
        ]
        has_leisure_intent = any(re.search(pattern, text_norm) for pattern in leisure_patterns)
        has_context = (
            any(token in text_norm for token in ["hoy", "manana", "esta tarde", "esta noche", "fin de semana", "este finde"])
            or re.search(r"\b(?:en|por)\s+[a-záéíóúüñ][a-záéíóúüñ .'-]{2,40}\b", text_norm) is not None
        )
        return bool(has_leisure_intent and has_context)

    def _extract_named_web_source(self, message: str) -> Optional[Dict[str, str]]:
        text = (message or "").strip()
        if not text:
            return None

        match = re.match(
            r"^(?:busca(?:me)?|buscar|mira|consulta)\s+en\s+([A-Za-z0-9._-]+)\s+(.+)$",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None

        source = self._normalize_text(match.group(1))
        query = match.group(2).strip()
        blocked_sources = {
            "internet", "web", "google", "el", "la", "los", "las",
            "documento", "documentos", "documentacion", "boe", "sharepoint",
        }
        if source in blocked_sources:
            return None

        return {"source": source, "query": query}

    def _prepare_web_search_query(self, effective_message: str, user_message: str) -> str:
        search_query = effective_message
        prefix_pattern = r"^(?:busca(?:me)?(?:\s+en\s+(?:internet|la\s+web|web|google))?\s+)"
        search_query = re.sub(prefix_pattern, "", search_query, flags=re.IGNORECASE)
        search_query = re.sub(
            r"^(?:dime|cuentame|quiero saber|me puedes decir|podrias decirme|puedes decirme)\s+",
            "",
            search_query,
            flags=re.IGNORECASE,
        )
        suffix_pattern = r"\s+y\s+(?:hablame|cuentame|dime)\s+(?:de\s+)?(?:ella|el|ello|esto)\.?$"
        search_query = re.sub(suffix_pattern, "", search_query, flags=re.IGNORECASE).strip()
        if not search_query:
            search_query = effective_message

        source_request = self._extract_named_web_source(user_message) or self._extract_named_web_source(effective_message)
        if source_request:
            source = source_request["source"]
            source_query = self._apply_web_search_aliases(source_request["query"])
            source_domains = {
                "infodefensa": "infodefensa.com",
                "flashscore": "flashscore.es",
                "marca": "marca.com",
                "as": "as.com",
            }
            domain = source_domains.get(source)
            if not domain and "." in source:
                domain = source
            if domain:
                return f"site:{domain} {source_query}".strip()

        search_query = self._apply_web_search_aliases(search_query)
        query_norm = self._normalize_text(search_query)
        if self._is_local_activity_query(search_query):
            location_match = re.search(
                r"\b(?:en|por)\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ .'-]{1,40})",
                search_query,
                flags=re.IGNORECASE,
            )
            location = location_match.group(1).strip() if location_match else ""
            target_date = None
            if "manana" in query_norm:
                target_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            elif "hoy" in query_norm:
                target_date = datetime.now().strftime("%Y-%m-%d")

            parts = ["agenda", "eventos", "que hacer"]
            if location:
                parts.append(location)
            if target_date:
                parts.append(target_date)
            return " ".join(parts).strip()

        if "real madrid" in query_norm and any(token in query_norm for token in ["resultado", "marcador"]):
            return "site:flashscore.es real madrid resultado"

        return search_query

    def _search_backend_web(self, search_query: str) -> List[Dict]:
        response = requests.get(
            f"{self.valves.BACKEND_URL}/web-search",
            params={"q": search_query},
            timeout=30,
        )
        response.raise_for_status()
        search_data = response.json()
        return search_data.get("results", [])

    def _search_infodefensa_results(self, query: str) -> List[Dict]:
        # Fuente específica del despliegue corporativo original.
        # Se deshabilita en la variante TFG para evitar referencias de marca.
        return []

    def _search_real_madrid_live_results(self) -> List[Dict]:
        response = requests.get(
            "https://www.flashscore.es/equipo/real-madrid/W8mj7MDD/",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            },
            timeout=20,
        )
        response.raise_for_status()

        title_match = re.search(r"<title>(.*?)</title>", response.text, flags=re.IGNORECASE | re.DOTALL)
        description_match = re.search(
            r'<meta name="description" content="([^"]+)"',
            response.text,
            flags=re.IGNORECASE,
        )
        title = self._clean_html_fragment(title_match.group(1)) if title_match else "Real Madrid: marcadores en directo y resultados"
        snippet = self._clean_html_fragment(description_match.group(1)) if description_match else ""
        return [
            {
                "title": title,
                "link": str(response.url),
                "snippet": snippet,
            }
        ]

    def _results_contain_score(self, results: List[Dict]) -> bool:
        score_pattern = re.compile(r"\b\d+\s*[-–]\s*\d+\b")
        for result in results[:5]:
            haystack = f"{result.get('title', '')} {result.get('snippet', '')}"
            if score_pattern.search(haystack):
                return True
        return False

    def _build_web_search_results_fallback(self, search_query: str, results: List[Dict]) -> str:
        if not results:
            return "No he encontrado resultados web suficientes para responder con fiabilidad."

        query_norm = self._normalize_text(search_query)
        if any(token in query_norm for token in ["ceo", "consejero delegado"]):
            name_patterns = [
                r"ceo de [^,.;:]+,\s*([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑñ' -]{2,80})",
                r"consejero delegado(?: de [^,.;:]+)?,\s*([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑñ' -]{2,80})",
                r"nuevo ceo de [^,.;:]+[.:,]\s*([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑñ' -]{2,80})",
            ]
            for idx, result in enumerate(results, 1):
                haystack = f"{result.get('title', '')}. {result.get('snippet', '')}"
                for pattern in name_patterns:
                    match = re.search(pattern, haystack, flags=re.IGNORECASE)
                    if match:
                        name = " ".join(match.group(1).split()).strip(" .,;:")
                        return f"Según las fuentes web, el CEO es {name} [{idx}]."

        if any(token in query_norm for token in ["resultado", "marcador"]):
            for idx, result in enumerate(results, 1):
                title = " ".join(str(result.get("title", "") or "").split())
                snippet = " ".join(str(result.get("snippet", "") or "").split())
                haystack = f"{title} {snippet}".strip()
                if re.search(r"\b\d+\s*[-–]\s*\d+\b", haystack):
                    headline = title or snippet or haystack
                    if len(headline) > 220:
                        headline = headline[:217].rstrip() + "..."
                    return f"He encontrado este marcador en la web: {headline} [{idx}]."

            top_title = " ".join(str(results[0].get("title", "") or "").split())
            return f"No veo el marcador exacto en los snippets, pero la fuente más directa ahora mismo es {top_title} [1]."

        summary_parts = []
        for idx, result in enumerate(results[:2], 1):
            snippet = " ".join(str(result.get("snippet", "") or "").split())
            title = " ".join(str(result.get("title", "") or "").split())
            if snippet:
                summary_parts.append(f"{snippet} [{idx}]")
            elif title:
                summary_parts.append(f"{title} [{idx}]")

        if summary_parts:
            return "Resumen de las fuentes web: " + " ".join(summary_parts)

        return f"Resultado principal: {results[0].get('title', 'sin título')} [1]."

    def _match_collection_only_request(self, message: str) -> Optional[str]:
        message_norm = self._normalize_text(message)
        if not message_norm or len(message_norm.split()) > 2:
            return None

        explicit_collection_prefixes = ("documents_", "webs", "docs_")
        if not any(message_norm.startswith(prefix) for prefix in explicit_collection_prefixes):
            return None

        return self._resolve_collection_name(message_norm) or self._resolve_collection_name(message.strip())

    def _looks_like_list_docs_request(self, message: str) -> bool:
        message_lower = (message or "").lower().strip()
        if not message_lower:
            return False

        list_webs_keywords = [
            "/webs", "/listar webs", "listar webs", "lista webs", "listame webs",
            "que webs tienes", "qué webs tienes", "que paginas tienes", "qué páginas tienes",
            "webs guardadas", "páginas guardadas", "paginas guardadas",
            "webs indexadas", "páginas indexadas", "paginas indexadas",
            "sitios guardados", "sitios indexados",
            "que urls tienes", "qué urls tienes", "urls guardadas",
        ]
        if any(keyword in message_lower for keyword in list_webs_keywords):
            return True

        if message_lower in ["/listar", "/docs", "/documentos", "/list", "list docs", "list", "docs"]:
            return True

        short_docs_pattern = r"(?:^|\s)(docs|documentos|archivos|pdfs)\s+(?:de|en|del|dentro de)\s+([a-zA-Z0-9_\-\u00C0-\u00FF]+)"
        if re.search(short_docs_pattern, message_lower):
            return True

        list_docs_pattern = r"(?:listar?|listame|dime|ver|mostrar|cuales|que|list)\s+(?:son\s+|los\s+|hay\s+|mis\s+|todos\s+los\s+)?(docs|documentos|archivos|pdfs|guardado|tengo guardado)"
        if re.search(list_docs_pattern, message_lower) or message_lower in ["docs", "documentos", "archivos"]:
            return True

        return self._match_collection_only_request(message) is not None

    def _match_document_search_request(self, message: str) -> Optional[Dict]:
        message_raw = (message or "").strip()
        message_norm = self._normalize_text(message_raw)
        if not message_norm:
            return None

        collection_first = re.match(
            r"^(?:busca(?:me)?|buscar|encuentra|localiza|find)\s+en\s+([a-z0-9_\-\u00c0-\u00ff]+)\s+(?:(?:el|los|un|una)\s+)?(?:(?:documento|documentos|archivo|archivos|pdf|pdfs)\s+)?(.+)$",
            message_norm,
        )
        if collection_first:
            collection_token = collection_first.group(1).strip()
            resolved_collection = self._resolve_collection_name(collection_token)
            if resolved_collection:
                query = collection_first.group(2).strip()
                query = re.sub(r"^(?:documento|documentos|archivo|archivos|pdf|pdfs)\s+", "", query).strip()
                if query:
                    return {"query": query, "target_collection": resolved_collection}

        explicit_patterns = [
            r"^(?:busca(?:me)?|buscar|encuentra|localiza|find)\s+(?:(?:el|los|un|una)\s+)?(?:documento|documentos|archivo|archivos|pdf|pdfs)\s+(.+)$",
            r"^(?:busca(?:me)?|buscar|encuentra|localiza)\s+por\s+(?:codigo|nombre)\s+(.+)$",
        ]
        for pattern in explicit_patterns:
            match = re.match(pattern, message_norm)
            if not match:
                continue
            query = match.group(1).strip()
            if query:
                return {"query": query}

        return None

    def _looks_like_document_search_request(self, message: str) -> bool:
        return self._match_document_search_request(message) is not None

    def _is_file_followup(self, conversation_key: str, message: str, messages: List[Dict]) -> bool:
        stored = self._get_recent_file_memory(conversation_key)
        if not stored:
            return False

        message_norm = self._normalize_text(message)
        if re.search(r"https?://", message or "", flags=re.IGNORECASE):
            return False
        if self._looks_like_list_docs_request(message):
            return False
        if self._looks_like_document_search_request(message):
            return False

        explicit_markers = [
            "archivo", "documento adjunto", "documento que subi", "pdf", "word",
            "excel", "adjunto", "este documento", "ese documento",
        ]
        if any(marker in message_norm for marker in explicit_markers):
            return True

        filename_norm = self._normalize_text(stored.get("filename", ""))
        if filename_norm and filename_norm in message_norm:
            return True

        last_assistant = self._normalize_text(self._get_last_assistant_message(messages))
        if self._is_generic_followup(message) and last_assistant:
            return "archivo consultado en esta conversacion" in last_assistant or (
                filename_norm and filename_norm in last_assistant
            )

        if last_assistant and (
            "archivo consultado en esta conversacion" in last_assistant or
            (filename_norm and filename_norm in last_assistant)
        ):
            continuation_prefixes = [
                "y ", "cada cuanto", "cuando", "como", "por que", "por qué",
                "quien", "que ", "qué ", "cual", "cuál", "cuanto", "cuánto",
                "resum", "explica", "detalla", "amplia", "traduce",
            ]
            if any(message_norm.startswith(prefix) for prefix in continuation_prefixes):
                return True

        return False

    def _extract_images_from_messages(self, messages: List[Dict]) -> List[str]:
        """
        Extrae imágenes SOLO del ÚLTIMO mensaje del usuario.
        OpenWebUI envía imágenes en el formato:
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "..."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
            ]
        }
        """
        images = []
        
        # Solo revisar el último mensaje del usuario, no todo el historial
        if not messages:
            return images
            
        # Buscar el último mensaje del usuario
        last_user_msg = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user_msg = msg
                break
        
        if not last_user_msg:
            return images
            
        content = last_user_msg.get("content", "")
        # Si content es una lista, buscar imágenes
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "image_url":
                        image_url = item.get("image_url", {})
                        if isinstance(image_url, dict):
                            url = image_url.get("url", "")
                        else:
                            url = image_url
                        if url:
                            images.append(url)
                    elif item.get("type") == "image":
                        url = item.get("url", "")
                        if url:
                            images.append(url)
        return images

    def _has_file_attachment(self, body: Dict, messages: List[Dict] = None) -> bool:
        """
        Detecta si hay un archivo (PDF, doc, etc.) adjunto en el chat.
        
        OpenWebUI envía archivos como:
        - body.files con type != image
        - Contexto muy largo con <source> tags
        - messages con file_url o attachments
        
        Returns True si hay un archivo NO-imagen adjunto.
        """
        # Check body.files for non-image files
        files = body.get("files", [])
        if files:
            for f in files:
                if isinstance(f, dict):
                    file_type = f.get("type", "").lower()
                    filename = f.get("name", "").lower()
                    # Si no es imagen, es archivo
                    if not any(ext in file_type for ext in ["image", "png", "jpg", "jpeg", "gif", "webp"]):
                        logger.info(f"Detected file attachment: {filename}")
                        return True
                    if filename.endswith((".pdf", ".doc", ".docx", ".txt", ".xlsx", ".csv")):
                        logger.info(f"Detected file attachment by extension: {filename}")
                        return True
        
        # Check if there's a very long context in messages (OpenWebUI embeds file content)
        if messages:
            last_msg = messages[-1] if messages else None
            if last_msg and last_msg.get("role") == "user":
                content = last_msg.get("content", "")
                if isinstance(content, str):
                    # Si el mensaje tiene <source> tags, OpenWebUI está pasando contexto de archivo
                    if "<source" in content or "<context>" in content:
                        logger.info("Detected OpenWebUI file context with <source> tags")
                        return True
                    # Si el mensaje es muy largo (>5000 chars), probablemente tiene archivo embebido  
                    if len(content) > 5000:
                        logger.info(f"Detected very long message ({len(content)} chars), likely file context")
                        return True
        
        return False

    def _detect_intent(self, message: str, body: Dict, messages: List[Dict] = None) -> Dict:
        """
        Detecta la intención del usuario y devuelve acción + metadata.
        
        IMPORTANTE: El default es CHAT, NO RAG.
        RAG solo se activa cuando el usuario explícitamente menciona documentos.
        
        Returns:
            {
                "action": "list_docs"|"web_search"|"web_scrape"|"ocr"|"rag"|"chat"|"file_chat",
                "metadata": {...}
            }
        """
        if self._is_rag_followup(message, messages or []):
            logger.info("   Detected RAG follow-up from previous document answer")
            return {"action": "rag", "metadata": {"followup": True}}

        message_lower = message.lower().strip()
        message_norm = self._normalize_text(message)
        
        # DEBUG: Log what we receive
        if self.valves.DEBUG_MODE:
            logger.info(f"🔍 DEBUG _detect_intent:")
            logger.info(f"   message: {message[:50]}...")
            logger.info(f"   body.files: {body.get('files')}")
            logger.info(f"   body.images: {body.get('images')}")

        # OpenWebUI envía prompts internos para títulos, etiquetas y follow-up suggestions.
        # Deben bypassar el detector de intents para no contaminar la conversación con flujos BOE/RAG.
        if message_lower.startswith("### task:"):
            return {"action": "chat", "metadata": {"internal_task": True}}
        
        # --- Obtener ID de usuario para memoria aislada ---
        user_email = self._get_user_email(body)
        conversation_key = self._get_conversation_scope_key(body, messages)
        
        # -1. Revisar decisiones pendientes (Check URL)
        pending_decision = self._user_pending_decision.get(conversation_key)
        if pending_decision and pending_decision.get("type") == "url_check":
            decision_keywords = ["si", "sí", "usar", "existente", "ver", "guardada", "ok", "vale"]
            update_keywords = ["no", "actualizar", "scrapear", "nueva", "buscar"]
            
            if any(kw in message_lower for kw in decision_keywords):
                return {
                    "action": "handle_url_decision",
                    "metadata": {"decision": "use_existing", "data": pending_decision["data"]}
                }
            elif any(kw in message_lower for kw in update_keywords):
                return {
                    "action": "handle_url_decision",
                    "metadata": {"decision": "update", "data": pending_decision["data"]}
                }
            # Si no coincide (pregunta normal), limpiamos el estado pendiente o lo ignoramos?
            # Mejor dejarlo pendiente UN turno más? O limpiarlo si el usuario cambia de tema.
            # Por ahora, si no es respuesta clara, asumimos que ignora la pregunta y limpiamos.
            # self._user_pending_decision.pop(user_email, None) 
            # (No, mejor dejar que el handler decida o timeout)
        
        # -0.6. BOE: consulta directa y seguimientos sobre resultados anteriores.
        boe_keywords = [
            "busca en el boe", "mira en el boe", "consulta el boe",
            "legislación", "legislacion", "normativa", "ley de", "decreto", "real decreto",
            "licitaciones", "licitación", "concurso público", "concurso publico",
            "sumario del boe", "noticias del boe", "boe de hoy", "formalización de contratos",
            "formalizacion de contratos", "adjudicación", "adjudicacion", "expediente",
        ]
        boe_keywords.extend(
            [
                "noticias de hoy del boe",
                "noticias de hoy en el boe",
                "publicaciones del boe",
                "publicaciones de hoy del boe",
                "publicaciones de hoy en el boe",
                "que se ha publicado en el boe",
            ]
        )
        stored_boe = self._user_boe_memory.get(conversation_key)
        has_explicit_url = re.search(r'https?://[^\s\?\"\'\)]+' , message) is not None
        if self._is_boe_followup(conversation_key, message, messages):
            return {
                "action": "check_boe",
                "metadata": {
                    "type": self._infer_boe_type(message, stored_boe.get("type") if stored_boe else None),
                    "followup": True,
                },
            }
        if not has_explicit_url and (any(kw in message_lower for kw in boe_keywords) or self._contains_boe_reference(message)):
            return {
                "action": "check_boe",
                "metadata": {"type": self._infer_boe_type(message)},
            }
        help_request = self._match_help_request(message, messages)
        if help_request:
            return help_request

        collection_only_request = self._match_collection_only_request(message)
        if collection_only_request:
            return {"action": "list_docs", "metadata": {"target_collection": collection_only_request}}

        document_search_request = self._match_document_search_request(message)
        if document_search_request:
            return {"action": "search_docs", "metadata": document_search_request}

        # -0.5. Ayuda / Capacidades (NUEVO)
        help_keywords = [
            "ayuda", "help", "que puedes hacer", "qué puedes hacer",
            "capabilities", "capacidades", "instrucciones", "manual",
            "cómo funcionas", "como funcionas", "qué sabes hacer", "que sabes hacer",
            "funciones disponibles", "comandos"
        ]
        # Match exact phrase or if message is just "ayuda"
        if message_lower in help_keywords or any(f"dime {kw}" in message_lower for kw in help_keywords):
            return {"action": "help", "metadata": {}}

        # 0. Detectar archivos adjuntos en el chat (NO imágenes, sino PDFs/docs)
        # OpenWebUI envía estos como attachments o context
        # Si detectamos que hay un archivo adjunto Y el mensaje es corto, es file_chat
        if self._has_file_attachment(body, messages):
            logger.info(f"   → Detected file attachment in chat!")
            return {
                "action": "file_chat",
                "metadata": {"from_attachment": True}
            }
        
        # 1. Imágenes adjuntas → OCR (revisar primero, antes de mensaje vacío)
        # Buscar imágenes en body.files, body.images, o dentro de messages
        if self._is_file_followup(conversation_key, message, messages):
            logger.info(f"   â†’ Detected file follow-up for {user_email}")
            return {
                "action": "file_chat",
                "metadata": {"from_memory": True}
            }

        images_from_messages = self._extract_images_from_messages(messages or [])
        
        if body.get("images") or images_from_messages:
            logger.info(f"   → Detected OCR action with new image!")
            return {
                "action": "ocr",
                "metadata": {
                    "files": body.get("files", []),
                    "images": body.get("images", []) + images_from_messages,
                    "is_new_image": True
                }
            }
        
        # 2. Referencias a "la imagen" sin imagen adjunta → usar imagen guardada
        image_reference_phrases = [
            # Referencias directas
            "la imagen", "esta imagen", "esa imagen", "imagen anterior",
            "última imagen", "ultima imagen", "la foto",
            "the image", "this image", "that image",
            # Preguntas sobre contenido
            "en la imagen", "de la imagen", "sobre la imagen",
            "qué pone", "que pone", "qué hay", "que hay",
            "qué dice", "que dice", "qué muestra", "que muestra",
            "qué ves", "que ves", "qué texto", "que texto",
            # Acciones sobre imagen
            "describe la", "analiza la", "resume la", "traduce la",
            "transcribe la", "transcribe el", "transcribelo", "transcríbelo",
            "lee la imagen", "lee el texto",
            "contenido de la imagen", "texto de la imagen",
            # Follow-up cortos (traducciones, formatos)
            "en español", "en inglés", "en ingles", "tradúcelo", "traducelo",
            "en castellano", "resúmelo", "resumelo", "más detalle", "mas detalle",
            "explícalo", "explicalo", "otra vez", "repite", "repítelo",
        ]
        
        stored_image = self._user_image_memory.get(conversation_key)
        
        # 1. Referencia explícita en keywords
        has_image_reference = any(phrase in message_lower for phrase in image_reference_phrases)
        
        # 2. Mensajes MUY cortos (<=4 palabras)
        is_short_followup = len(message_lower.split()) <= 4
        
        # 3. Detección de intención semántica (verbos + objetos implícitos)
        # Ej: "podrías traducir este mensaje", "traduce el texto", "resumen de esto"
        action_verbs = ["traduc", "resum", "explic", "analiz", "descri", "dime", "lee", "copia"]
        target_objects = ["esto", "texto", "mensaje", "carta", "documento", "contenido", "lo de antes"]
        
        has_action_verb = any(v in message_lower for v in action_verbs)
        has_target_object = any(t in message_lower for t in target_objects)
        
        # Si tiene verbo de acción Y (objeto target O es corto), es probable follow-up
        is_semantic_followup = has_action_verb and (has_target_object or is_short_followup)
        
        if stored_image and (has_image_reference or is_short_followup or is_semantic_followup):
            logger.info(f"   → Detected image reference/followup, using stored image for {user_email}!")
            return {
                "action": "ocr",
                "metadata": {
                    "files": [],
                    "images": [stored_image],
                    "is_new_image": False
                }
            }
        
        # 2b. Referencias a "la web/página" sin URL nueva → usar contenido guardado
        web_reference_phrases = [
            # Referencias directas
            "la web", "la página", "la pagina", "esa web", "esta web",
            "esa página", "esta página", "esa pagina", "esta pagina",
            "la url", "esa url", "el sitio", "ese sitio",
            "el contenido", "ese contenido", "el artículo", "el articulo",
            # Preguntas de seguimiento
            "sobre eso", "sobre esto", "de eso", "de esto",
            "en la web", "de la web", "en la página", "de la página",
            "qué más dice", "que mas dice", "qué más hay", "que mas hay",
            "explica más", "explica mas", "cuéntame más", "cuentame mas",
            "profundiza", "amplía", "amplia", "detalla",
            # Preguntas específicas
            "qué dice sobre", "que dice sobre", 
            "qué menciona", "que menciona",
            "habla de", "menciona algo",
        ]
        
        stored_web = self._user_web_memory.get(conversation_key)
        if stored_web and any(phrase in message_lower for phrase in web_reference_phrases):
            logger.info(f"   → Detected web content reference, using stored content for {user_email}!")
            return {
                "action": "web_followup",
                "metadata": {"from_memory": True}
            }
        
        # 2c. Comando "guarda esto" / "indexa esto" → guardar contenido de sesión en RAG
        save_session_keywords = [
            "guarda esto", "guárdalo", "guardalo", "guarda el contenido",
            "indexa esto", "indexalo", "indexa el contenido",
            "añade esto al rag", "añádelo", "anadelo",
            "quiero guardar esto", "guarda esta información",
            "save this", "index this",
        ]
        
        if stored_web and any(kw in message_lower for kw in save_session_keywords):
            logger.info(f"   → Detected save session content command!")
            return {
                "action": "save_web_content",
                "metadata": {"url": stored_web.get("url")}
            }
        
        message_lower = message.lower().strip()
        
        # 1b. Comando: AYUDA / FUNCIONALIDADES
        help_keywords = [
            "que puedes hacer", "qué puedes hacer", "ayuda", "help", 
            "funcionalidades", "capacidades", "manual de uso", 
            "comandos", "que sabes hacer", "qué sabes hacer",
            "quien eres", "quié eres", "quién eres"
        ]
        if message_lower in help_keywords or any(message_lower.startswith(f"dime {kw}") for kw in help_keywords):
            return {"action": "help", "metadata": {}}

        # 2. Comando: Listar WEBS indexadas (nuevo - solo colección 'webs')
        list_webs_keywords = [
            "/webs", "/listar webs", "listar webs", "lista webs", "listame webs",
            "que webs tienes", "qué webs tienes", "que paginas tienes", "qué páginas tienes",
            "webs guardadas", "páginas guardadas", "paginas guardadas",
            "webs indexadas", "páginas indexadas", "paginas indexadas",
            "sitios guardados", "sitios indexados",
            "que urls tienes", "qué urls tienes", "urls guardadas",
        ]
        if any(kw in message_lower for kw in list_webs_keywords):
            return {"action": "list_docs", "metadata": {"target_collection": "webs"}}
        
        # 2b. Comando: Listar documentos (comando directo)
        if message_lower in ["/listar", "/docs", "/documentos", "/list", "list docs", "list", "docs"]:
            return {"action": "list_docs", "metadata": {}}

        # 2c. Comando corto: "docs de calidad", "documentos en civex2", etc.
        short_docs_pattern = r"(?:^|\s)(docs|documentos|archivos|pdfs)\s+(?:de|en|del|dentro de)\s+([a-zA-Z0-9_\-\u00C0-\u00FF]+)"
        short_docs_match = re.search(short_docs_pattern, message_lower)
        if short_docs_match:
            target_coll = short_docs_match.group(2).strip()
            if target_coll and target_coll not in ["el", "la", "los", "las", "mis", "tus", "estos", "aqui", "ahi", "todos"]:
                logger.info(f"   → Short docs command detected. target_collection={target_coll}")
                return {"action": "list_docs", "metadata": {"target_collection": target_coll}}
            return {"action": "list_docs", "metadata": {}}
        
        # 3. Preguntas naturales sobre documentos disponibles (Regex Robust)
        # Patrón para detectar:
        # - Verbos: listar, lista, listame, dime, mostrar, ver, cuales, que, list
        # - Conectores opcionales: son, los, hay, mis, todos
        # - Sustantivos: docs, documentos, archivos, pdfs
        list_docs_pattern = r"(?:listar?|listame|dime|ver|mostrar|cuales|que|list)\s+(?:son\s+|los\s+|hay\s+|mis\s+|todos\s+los\s+)?(docs|documentos|archivos|pdfs|guardado|tengo guardado)"
        
        # Intentar extraer colección específica
        # Intentar extraer colección específica
        # Ej: "que docs tengo en Proyecto1", "docs de proyecto1", "documentos en documents_Proyecto1"
        # Regex mejorada para soportar acentos y caracteres especiales básicos
        collection_pattern = r"(?:en|de|dentro de|about)\s+([a-zA-Z0-9_\-\u00C0-\u00FF]+)"
        collection_match = re.search(collection_pattern, message_lower)
        
        # Check regex match OR direct commands
        if re.search(list_docs_pattern, message_lower) or message_lower in ["docs", "documentos", "archivos"]:
            metadata = {}
            if collection_match:
                # Si encontramos un posible nombre de colección, lo guardamos
                target_coll = collection_match.group(1).strip()
                # Filtrar palabras comunes que podrían falsos positivos
                if target_coll not in ["el", "la", "los", "las", "mis", "tus", "estos", "estos", "aqui", "ahí", "todos"]:
                     
                     # IMPORTANTE: Si el usuario pide "docs de [tema]", debemos decidir si es 
                     # una COLECCIÓN (listar) o un TEMA (RAG search).
                     # Heurística: Si parece una colección conocida, listamos. Si no, buscamos.
                     
                     metadata["target_collection"] = target_coll
                     logger.info(f"   → Detected target collection: {target_coll}")

            return {"action": "list_docs", "metadata": metadata}
        
        # 4. URL en el mensaje + palabras de acción → Web Scraping
        # Solo scrapear si hay keywords que indican acción de scraping/análisis
        # 4. URL en el mensaje + palabras de acción → Web Scraping
        # Solo scrapear si hay keywords que indican acción de scraping/análisis
        scrape_action_keywords = [
            # Análisis y extracción
            "analiza el contenido", "analiza la", "analiza esto", "analiza esta",
            "analizar la", "analizar el", "analizar esta", "analiza http", "analiza https",
            "analiza", "analyze", "analizar", # Permitir "analiza" solo si hay URL
            "extrae de", "extrae el contenido", "extrae la",
            "scrapea", "scrape", "scrapear",
            # Lectura
            "lee esta url", "lee esta página", "lee esta pagina", "lee la web",
            "lee el contenido", "lee esta web", "leer la", "leer el", "read this",
            # Resumen
            "resume esta", "resume la web", "resume la página", "resume la pagina",
            "resumen de la web", "resumen de esta",
            "haz un resumen", "hazme un resumen", "summarize",
            # Ver/mirar
            "qué hay en http", "que hay en http", "qué dice esta", "que dice esta",
            "mira esta url", "mira esta página", "mira esta web",
            "mira el contenido", "revisa esta", "revisa la",
            # Explicación
            "explica esta página", "explica esta web", "explica el contenido",
            "cuéntame sobre esta", "cuentame sobre esta",
            "dime qué hay", "dime que hay",
            # Búsqueda en URL específica
            "busca en http", "busca en www", "buscar en http",
        ]
        
        # Keywords específicas para INDEXAR (guardar para futuras consultas)
        index_keywords = [
            "indexa esta", "indexa el", "indexa la", "indexar esta",
            "guarda esta url", "guarda este contenido", "guarda esta web",
            "añade esta url", "añade al rag", "añade a los documentos",
            "ingesta esta", "ingestar esta",
        ]
        
        # Keywords específicas para CRAWLER (Recursive)
        crawl_keywords = [
            "crawler", "crawl", "crawlea", "explora", "navega",
            "a fondo", "recursivo", "profundidad", "todo el dominio",
            "toda la web", "todos los enlaces"
        ]
        
        url_pattern = r'https?://[^\s\?\"\'\)]+' 
        url_match = re.search(url_pattern, message)
        
        if url_match:
            # Limpiar URL de caracteres de puntuación y markdown comunes al final
            clean_url = url_match.group().rstrip('?.,;:!)*_]}>')
            
            # 4a. Verificar Crawler/Recursive
            if any(kw in message_lower for kw in crawl_keywords):
                return {
                    "action": "web_crawl",
                    "metadata": {"url": clean_url}
                }
            
            # 4b. Verificar Indexar (Single page)
            if any(kw in message_lower for kw in index_keywords) or re.search(r"\b(indexa|indexar|ingesta|ingestar|guardar|guarda|anade|añade)\b", message_lower):
                return {
                    "action": "web_index",
                    "metadata": {"url": clean_url}
                }
            
            # 4c. Default a Web Scraping (Analyze/Read)
            logger.info(f"URL detected, routing directly to web_scrape: {clean_url}")
            return {
                "action": "web_scrape",
                "metadata": {"url": clean_url}
            }
        
        # 4d. Preguntas directas de fecha/hora -> AGENT
        agent_time_keywords = [
            "que hora es", "qué hora es", "hora actual",
            "que dia y hora es", "qué día y hora es", "dia y hora", "día y hora",
            "que dia es", "qué día es", "que fecha es", "qué fecha es",
            "fecha de hoy", "fecha y hora", "hoy es", "ahora mismo"
        ]
        if any(kw in message_lower for kw in agent_time_keywords):
            return {"action": "agent", "metadata": {}}

        explicit_internal_rag_keywords = [
            "documentos internos", "documentos de la empresa", "documentación interna",
            "según los documentos", "segun los documentos", "en los documentos",
            "en el documento", "en la documentación", "en la documentacion",
        ]
        if any(kw in message_lower for kw in explicit_internal_rag_keywords):
            return {"action": "rag", "metadata": {}}

        if self._extract_weather_request(message):
            return {"action": "web_search", "metadata": {"weather": True}}

        if self._is_local_activity_query(message):
            return {"action": "web_search", "metadata": {"local_plans": True}}

        sports_result_patterns = [
            r"\bresultado\b.*\breal madrid\b",
            r"\breal madrid\b.*\bresultado\b",
            r"\bmarcador\b.*\breal madrid\b",
            r"\breal madrid\b.*\bmarcador\b",
        ]
        if any(re.search(pattern, message_norm) for pattern in sports_result_patterns):
            return {"action": "web_search", "metadata": {"sports_result": True}}


        # 5. Keywords de Web Search
        web_search_keywords = [
            "buscar en internet", "busca en internet", "buscar web",
            "busca en la web", "buscar en la web", "busca web",
            "me podrías buscar", "me podrias buscar", "podrías buscar", "podrias buscar",
            "noticias", "actualidad", "precio", "cotización",
            "qué es", "quién es", "cuándo", "dónde",
            "google",
            "últimas noticias", "hoy", "ayer"
        ]
        web_search_keywords_norm = [self._normalize_text(kw) for kw in web_search_keywords]
        explicit_named_source_search = self._extract_named_web_source(message) is not None
        if explicit_named_source_search or any(kw in message_norm for kw in web_search_keywords_norm):
            # Excepción: Si pregunta la hora o la fecha, es AGENT para que use herramientas (es decir, MCP dummy time o web search)
            # Ej: "qué hora es", "qué día es hoy", "dime la fecha"
            time_questions = [
                "hora es", "dia es", "día es", "fecha de hoy", "fecha es", "qué día", "que dia",
                "dia y hora", "día y hora", "fecha y hora", "hoy es"
            ]
            time_questions_norm = [self._normalize_text(tq) for tq in time_questions]

            # Si pide explícitamente BUSCAR EN INTERNET, respetamos su deseo de buscar fuera
            explicit_web_search = explicit_named_source_search or any(
                kw in message_norm
                for kw in [self._normalize_text(item) for item in ["busca en internet", "buscar en internet", "busca en la web", "google"]]
            )

            if not explicit_web_search and any(tq in message_norm for tq in time_questions_norm) and not any(
                kw in message_norm
                for kw in [self._normalize_text(item) for item in ["noticias", "precio", "valor", "cotizacion", "cotización", "tiempo en", "clima"]]
            ):
                logger.info(f"Date/Time question detected, routing to AGENT to allow MCP tools")
                return {"action": "agent", "metadata": {}}

            return {"action": "web_search", "metadata": {}}
        
        # 6. Keywords de RAG (documentos internos)
        rag_keywords = [
            "documento", "documentos", "pdf", "archivo",
            "política", "politica", "manual", "procedimiento",
            "norma", "iso", "calidad", "seguridad",
            "según", "segun", "qué dice", "que dice",
            "internamente", "empresa", "organización", "organizacion",
            "reglamento", "contrato", "informe", "report",
            "proveedor", "proveedores", "criterio", "criterios",
            "búsqueda", "busqueda", "homologación", "homologacion",
            # Nuevos: acciones sobre documentos
            "mira en", "busca en el", "lee el", "revisa el",
            "consulta el", "consulta la", "dime qué", "dime que",
            "explica el", "resume el", "resumen de",
            "información sobre", "info sobre", "datos de",
            "contenido de", "contenido del",
        ]
        if any(kw in message_lower for kw in rag_keywords):
            return {"action": "rag", "metadata": {}}
        
        # 7. Detectar códigos de documentos (CR-277, MAP-003, ISO9001, etc.)
        doc_code_pattern = r'\b([A-Z]{2,5}[-_]?\d{2,4})\b'
        if re.search(doc_code_pattern, message.upper()):
            logger.info(f"Detected document code in query, routing to RAG")
            return {"action": "rag", "metadata": {}}
        
        # 8. Keywords de Status
        status_keywords = [
            "estado", "check status", "system status",
            "cómo va", "como va", "estado de", "status", "estatus",
            "qué tal va", "que tal va", "progreso", "subida",
            "ingestión", "ingestion", "indexación", "indexacion"
        ]
        if any(kw in message_lower for kw in status_keywords):
            return {"action": "check_status", "metadata": {}}

        # 9. Default: CHAT (Conversación directa con LiteLLM con historial)
        # Evitamos el agente RAG para mensajes genéricos que no requieren documentos.
        logger.info(f"No document-related keywords found, defaulting to CHAT for: {message[:50]}...")
        return {"action": "chat", "metadata": {}}

    def _get_current_time_context(self) -> str:
        """Devuelve el contexto de tiempo actual para inyectar en los prompts."""
        from datetime import datetime
        import locale
        
        # Intentar poner locale en español si está disponible, sino default
        try:
            locale.setlocale(locale.LC_TIME, 'es_ES.UTF-8')
        except:
            try:
                locale.setlocale(locale.LC_TIME, 'es_ES')
            except:
                pass # Fallback to default system locale
                
        now = datetime.now()
        date_str = now.strftime("%A, %d de %B de %Y")
        time_str = now.strftime("%H:%M")
        
        return f"Fecha actual: {date_str}. Hora: {time_str}."

    def _is_followup_question(self, current_message: str, chat_history: List[Dict]) -> bool:
        """
        Detecta si la pregunta actual es un seguimiento de la conversación anterior.
        
        Returns True si:
        - La pregunta usa pronombres demostrativos (esto, eso, el documento, etc.)
        - La pregunta es muy corta (probablemente referencia algo anterior)
        - NO menciona un documento/tema nuevo explícitamente
        """
        if not chat_history or len(chat_history) < 2:
            return False
        
        msg_lower = current_message.lower().strip()
        
        # Indicadores de pregunta de seguimiento (pronombres, referencias)
        followup_indicators = [
            # Español
            "esto", "eso", "este documento", "ese documento", "el documento",
            "del mismo", "sobre eso", "sobre esto", "más sobre", "mas sobre",
            "y qué", "y que", "también", "tambien", "además", "ademas",
            "en ese", "en este", "de ese", "de este",
            "el anterior", "lo anterior", "la anterior",
            "sigue", "continua", "continúa", "explica más", "explica mas",
            "por qué", "por que", "cómo", "como es que",
            # English
            "this", "that", "the document", "this document", "that document",
            "about it", "more about", "also", "furthermore",
            "the same", "what about", "and what", "why is",
        ]
        
        # Si contiene indicadores de seguimiento, es followup
        if any(ind in msg_lower for ind in followup_indicators):
            return True
        
        # Preguntas muy cortas suelen ser follow-ups
        if len(msg_lower.split()) <= 4 and "?" in current_message:
            return True
        
        # Detectar si menciona un documento NUEVO (código tipo CR-277, MAP-003, PCN, etc.)
        import re
        doc_pattern = r'\b([A-Z]{2,5}[-_]?\d{2,4})\b'
        
        # Documentos en el mensaje actual
        current_docs = set(re.findall(doc_pattern, current_message.upper()))
        
        # Si menciona un documento nuevo, NO es followup (es una nueva pregunta)
        if current_docs:
            # Verificar si el documento ya estaba en la conversación reciente
            recent_docs = set()
            for msg in chat_history[-4:]:
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join([c.get("text", "") for c in content if isinstance(c, dict)])
                recent_docs.update(re.findall(doc_pattern, str(content).upper()))
            
            # Si el documento actual NO estaba en la conversación, es pregunta nueva
            if current_docs and not current_docs.intersection(recent_docs):
                logger.info(f"New document detected: {current_docs}, not in recent: {recent_docs}")
                return False
        
        # Por defecto, si la conversación tiene historial y no detectamos tema nuevo, 
        # asumimos que podría ser follow-up pero no añadimos contexto completo
        return False

    def _call_backend_chat(
        self,
        message: str,
        mode: str,
        user_data: Dict,
        chat_history: List[Dict] = None,
        conversation_key: Optional[str] = None,
    ) -> Dict:
        """
        Llama al endpoint /chat del backend.
        
        IMPORTANTE: Para búsquedas RAG, enviamos el mensaje LIMPIO para no contaminar
        la búsqueda semántica. El contexto conversacional solo se añade cuando es
        una pregunta de seguimiento clara sobre el mismo tema.
        """
        
        # Por defecto, historial vacío
        history_context = None
        
        # Solo añadir contexto si es claramente una pregunta de seguimiento
        if chat_history and len(chat_history) > 1:
            is_followup = self._is_followup_question(message, chat_history)
            
            if is_followup:
                # Es follow-up: añadir contexto mínimo (solo el último intercambio)
                last_assistant_msg = None
                for msg in reversed(chat_history[:-1]):  # Excluir mensaje actual
                    if msg.get("role") == "assistant":
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = " ".join([c.get("text", "") for c in content if isinstance(c, dict)])
                        if content and len(content) > 20:
                            last_assistant_msg = content[:1500]  # Limitar longitud
                            break
                
                if last_assistant_msg:
                    history_context = last_assistant_msg
                    logger.info(f"Added follow-up context (1 previous message)")
                    
                    # === NOVEDAD: REESCRIBIR LA PREGUNTA PARA BÚSQUEDA VECTORIAL ===
                    # Si es followup, la pregunta original puede ser muy ambigua (ej. "dime más sobre eso")
                    # Para Qdrant necesitamos una query explícita. Usamos LiteLLM para reescribirla.
                    rewrite_prompt = (
                        "Dada la respuesta anterior del asistente y una nueva pregunta de seguimiento del usuario,\n"
                        "reescribe la pregunta del usuario para que sea una instrucción de búsqueda independiente y completa.\n"
                        "Debes extraer los nombres de documentos o conceptos clave de la respuesta anterior si a eso se refiere.\n"
                        "LIMITATE A DEVOLVER SOLO LA FRASE REESCRITA, sin introducciones ni comillas.\n\n"
                        f"Respuesta anterior del asistente:\n{history_context[:800]}"
                    )
                    try:
                        rewritten_message = self._call_litellm(
                            message=message,
                            model=self.valves.TEXT_MODEL,  # Usa el modelo configurado
                            system_prompt=rewrite_prompt
                        )
                        logger.info(f"Query reescrita: '{message}' -> '{rewritten_message}'")
                        message = rewritten_message.strip(' "\'')
                    except Exception as e:
                        logger.error(f"Error reescribiendo pregunta de seguimiento: {e}")
            else:
                logger.info(f"New topic detected, sending clean query without history")
        
        backend_conversation_id = None
        if mode == "agent" and conversation_key:
            stored_id = self._agent_conversation_ids.get(conversation_key)
            if isinstance(stored_id, int) and stored_id > 0:
                backend_conversation_id = stored_id

        payload = {
            "message": message,  # Send clean message for semantic search
            "mode": mode,
            "user_id": None,
            "email": user_data.get("email"),
            "name": user_data.get("name"),
            "azure_id": user_data.get("id"),
            "conversation_id": backend_conversation_id,
            "chat_history_context": history_context,  # Send context cleanly
            "stream": False
        }
        
        # Obtener departamentos autorizados del usuario
        authorized_depts = self._get_user_departments(user_data)
        
        if mode == "rag" and not authorized_depts:
            return {
                "content": "No tienes acceso a ninguna coleccion autorizada.",
                "sources": [],
                "conversation_id": 0,
                "has_citations": False,
                "tokens_used": 0,
                "processing_time": 0.0,
            }

        # Construir headers con colecciones autorizadas
        headers = {}
        if self.valves.MULTI_DEPARTMENT_ENABLED and authorized_depts:
            # Enviar lista de colecciones como CSV
            headers["X-Tenant-Ids"] = ",".join(authorized_depts)
            logger.info(f"RAG search en colecciones: {authorized_depts}")
        else:
            # Modo compatibilidad: una sola colección
            logger.info("No collection headers sent for this request")
        
        response = requests.post(
            f"{self.valves.BACKEND_URL}/chat",
            json=payload,
            headers=headers,
            timeout=self.valves.BACKEND_CHAT_TIMEOUT
        )
        response.raise_for_status()
        data = response.json()

        if mode == "agent" and conversation_key:
            raw_conversation_id = data.get("conversation_id")
            try:
                parsed_conversation_id = int(raw_conversation_id)
            except Exception:
                parsed_conversation_id = 0
            if parsed_conversation_id > 0:
                self._agent_conversation_ids[conversation_key] = parsed_conversation_id

        return data

    def _call_litellm(self, message: str, model: str, system_prompt: str = None, temperature: float = 0.7) -> str:
        """Llama a LiteLLM directamente para casos especiales"""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": message})
        
        response = requests.post(
            f"{self.valves.LITELLM_URL}/v1/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "stream": False
            },
            headers=self._litellm_headers(),
            timeout=self.valves.LITELLM_TIMEOUT
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def _build_chat_history(self, messages: List[Dict], max_messages: int = 10) -> List[Dict]:
        """
        Construye un historial de conversación para enviar al LLM.
        Extrae los últimos N mensajes del historial.
        
        Args:
            messages: Lista de mensajes de OpenWebUI
            max_messages: Número máximo de mensajes a incluir (default: 10)
        
        Returns:
            Lista de dicts con role y content listos para el LLM
        """
        history = []
        if not messages:
            return history
        
        # Tomar los últimos mensajes (excluyendo el mensaje actual)
        recent = messages[:-1] if len(messages) > 1 else []
        recent = recent[-(max_messages * 2):]  # max_messages de intercambios
        
        for msg in recent:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            # Manejar contenido multimodal (lista de componentes)
            if isinstance(content, list):
                text_parts = []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        text_parts.append(c.get("text", ""))
                content = " ".join(text_parts)
            
            # Solo incluir mensajes con contenido válido
            if content and role in ["user", "assistant"]:
                # Limitar longitud de cada mensaje para no exceder contexto
                if len(content) > 1500:
                    content = content[:1500] + "..."
                history.append({"role": role, "content": content})
        
        return history

    def _is_probably_english(self, text: str) -> bool:
        t = (text or "").lower()
        if not t:
            return False
        spanish_markers = [
            "que ", "como ", "por favor", "gracias", "puedes", "ayuda",
            "busca", "documento", "documentos", "estado", "licitacion",
        ]
        if any(m in t for m in spanish_markers):
            return False
        english_markers = [
            "what", "who", "how", "can you", "do you", "please", "help",
            "and what", "tell me", "information",
        ]
        if any(m in t for m in english_markers):
            return True
        if any(m in t for m in ["the ", "and ", "is ", "are ", "you "]):
            return True
        return False

    def _has_explicit_document_reference(self, text: str) -> bool:
        """
        Detecta si el usuario menciona explícitamente un documento concreto.
        Esto evita reescrituras de query que puedan degradar búsquedas por nombre/ID.
        """
        if not text:
            return False

        # IDs tipo AL-08, M-003, CR-277
        if re.search(r"\b[A-Z]{1,8}-\d{2,8}\b", text.upper()):
            return True

        # Referencias directas a ficheros
        if re.search(r"\.(pdf|doc|docx|txt|xlsx|csv|ppt|pptx)\b", text, re.IGNORECASE):
            return True

        return False

    def _call_litellm_with_history(self, user_message: str, system_prompt: str, 
                                    messages: List[Dict], extra_context: str = None) -> str:
        """
        Llama a LiteLLM con historial de conversación.
        
        Args:
            user_message: Mensaje actual del usuario
            system_prompt: System prompt a usar
            messages: Historial completo de OpenWebUI
            extra_context: Contexto adicional (ej: resultados de búsqueda, documentos RAG)
        
        Returns:
            Respuesta del LLM
        """
        llm_messages = [{"role": "system", "content": system_prompt}]
        
        # Añadir historial de conversación
        history = self._build_chat_history(messages, max_messages=6)
        llm_messages.extend(history)
        
        # Construir mensaje actual con contexto adicional si existe
        if extra_context:
            current_message = f"{extra_context}\n\nUser question: {user_message}"
        else:
            current_message = user_message
        
        llm_messages.append({"role": "user", "content": current_message})
        
        if self.valves.DEBUG_MODE:
            logger.info(f"LiteLLM call with {len(llm_messages)} messages (history: {len(history)})")
        
        response = requests.post(
            f"{self.valves.LITELLM_URL}/v1/chat/completions",
            json={
                "model": self.valves.TEXT_MODEL,
                "messages": llm_messages,
                "temperature": 0.7,
                "stream": False
            },
            headers=self._litellm_headers(),
            timeout=self.valves.LITELLM_TIMEOUT
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def _scrape_and_summarize(
        self,
        url: str,
        user_message: str,
        user_email: str,
        conversation_key: str,
        check_rag: bool = False,
    ):
        """
        Helper method to scrape, analyze and summarize a URL.
        Includes optional check for existing RAG content.
        """
        # 1. OPTIONAL: Check if exists in RAG
        if check_rag:
            try:
                yield f"🔍 **Verificando URL**: {url}...\n"
                lookup_headers = self._build_web_lookup_headers(user_email)
                rag_check = requests.post(
                    f"{self.valves.BACKEND_URL}/scrape/check",
                    json={"url": url},
                    headers=lookup_headers,
                    timeout=10
                ).json()
                
                if rag_check.get("exists"):
                    title = rag_check.get("title", "Sin título")
                    date = rag_check.get("scraped_at", "fecha desconocida")[:10]
                    
                    # Store pending decision
                    self._user_pending_decision[conversation_key] = {
                        "type": "url_check",
                        "data": {"url": url, "title": title}
                    }
                    
                    yield f"\n⚠️ **Contenido Existente Detectado**\n"
                    yield f"Ya tengo analizada esta página ({title}) desde el **{date}**.\n\n"
                    yield f"¿Qué prefieres?\n"
                    yield f"- **Usar existente**: Respondo rápido con lo que ya sé (Escribe 'sí' o 'usar').\n"
                    yield f"- **Actualizar**: Vuelvo a descargar la página (Escribe 'actualizar').\n"
                    return
            except Exception as e:
                logger.error(f"Error checking RAG status: {e}")
                # Continue to scrape if check fails
        
        # 2. Proceed to Scrape (Analyze mode)
        yield f"🌐 **Analizando URL en tiempo real**: {url}\n\n"
        
        try:
            # Llamar al endpoint /scrape/analyze para obtener contenido sin indexar
            response = requests.post(
                f"{self.valves.BACKEND_URL}/scrape/analyze",
                json={"url": url},
                timeout=120
            )
            response.raise_for_status()
            scrape_data = response.json()
            
            if scrape_data.get("status") != "success":
                yield f"⚠️ No se pudo extraer contenido de la URL.\n"
                return
            
            content = scrape_data.get("content", "")
            title = scrape_data.get("title", "Sin título")
            word_count = scrape_data.get("word_count", 0)
            extraction_method = scrape_data.get("extraction_method", "unknown")
            
            if not content or len(content) < 100:
                yield "⚠️ El contenido extraído es muy corto o está vacío.\n"
                return
            
            yield f"📄 **Título**: {title}\n"
            yield f"📊 **Palabras extraídas**: {word_count}\n\n"
            
            # Truncar contenido si es muy largo para el LLM
            max_content_chars = 8000
            truncated = len(content) > max_content_chars
            analysis_content = content[:max_content_chars] if truncated else content
            
            yield "📝 **Generando resumen...**\n\n"
            
            # Generar resumen con LLM
            summary_prompt = f"""###### REGLA CRÍTICA: IDIOMA ######
Responde SIEMPRE en el mismo idioma que la pregunta del usuario.
############################################

Analiza el siguiente contenido extraído de una página web y proporciona:
1. Un resumen conciso (2-3 párrafos)
2. Los puntos clave principales (lista con bullets)
3. Cualquier dato relevante destacado

CONTENIDO DE LA WEB ({title}):
---
{analysis_content}
---

{'[NOTA: El contenido fue truncado por longitud]' if truncated else ''}

Pregunta del usuario: {user_message}

Proporciona un análisis útil y estructurado."""

            try:
                summary = self._call_litellm(
                    message=summary_prompt,
                    model=self.valves.TEXT_MODEL,
                    system_prompt="Eres un asistente experto en análisis y resumen de contenido web. Respondes de forma clara, estructurada y concisa."
                )
                if self._is_refusal_text(summary):
                    summary = self._build_web_fallback_answer(content, title)
                yield summary
            except Exception as llm_error:
                logger.error(f"Error generando resumen: {llm_error}")
                yield "**Extracto del contenido:**\n\n"
                yield f"{content[:2000]}...\n" if len(content) > 2000 else content
            
            # ✨ GUARDAR EN MEMORIA DE SESIÓN (Por Usuario)
            from datetime import datetime
            self._user_web_memory[conversation_key] = {
                "url": url,
                "title": title,
                "content": content,  # Contenido completo
                "word_count": word_count,
                "scraped_at": datetime.now().isoformat(),
                "extraction_method": extraction_method,
            }
            logger.info(f"💾 Contenido web guardado en memoria de sesión para {user_email}")
            
            # Limpiar decisión pendiente si existía
            if conversation_key in self._user_pending_decision:
                if self._user_pending_decision[conversation_key].get("data", {}).get("url") == url:
                    del self._user_pending_decision[conversation_key]

            yield f"\n\n---\n🔗 **Fuente**: [{title}]({url})\n"
            yield f"🛠️ *Método de extracción: {extraction_method}*\n"
            
            yield f"\n💬 *Ahora puedes hacerme preguntas sobre este contenido. Por ejemplo:*\n"
            yield f"*- \"¿Qué dice sobre X?\"*\n"
            yield f"*- \"Guarda esto\" (para indexar en RAG)*\n"
            
        except requests.exceptions.Timeout:
            yield "⏱️ **Timeout**: La página tardó demasiado en cargar. Intenta con otra URL.\n"
        except requests.exceptions.RequestException as e:
            logger.error(f"Error en scraping request: {e}")
            yield f"❌ **Error de conexión**: No se pudo acceder a la URL.\n"
        except Exception as e:
            logger.error(f"Error inesperado en scraping: {e}")
            yield f"❌ **Error**: {str(e)}\n"

    def _call_ollama_direct(self, prompt: str, system_prompt: str = None) -> str:
        """
        Llama a Ollama directamente (bypassing LiteLLM) para archivos grandes.
        Usa el modelo llama3.1 local.
        """
        ollama_url = self.valves.OLLAMA_URL.rstrip("/")
        
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{prompt}"
        
        try:
            response = requests.post(
                f"{ollama_url}/api/generate",
                json={
                    "model": "llama3.1:8b-instruct-q8_0",
                    "prompt": full_prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.7,
                        "num_predict": 2000,
                        "num_ctx": 8192  # Context window más grande
                    }
                },
                timeout=120
            )
            
            if response.status_code == 200:
                result = response.json()
                return result.get("response", "No se pudo generar respuesta.")
            else:
                logger.error(f"Ollama error: {response.status_code} - {response.text}")
                return f"Error de Ollama: {response.status_code}"
                
        except requests.exceptions.Timeout:
            return "El procesamiento tardó demasiado. Intenta con un documento más corto."
        except Exception as e:
            logger.error(f"Error calling Ollama directly: {e}")
            return f"Error: {str(e)}"

    def _rewrite_query(self, user_message: str, messages: List[Dict]) -> str:
        """
        Reescribe la consulta del usuario usando el historial de chat para hacerla autónoma (Contextual Query Rewriting).
        Ideal para RAG y Web Search cuando hay pronombres o referencias implícitas.
        """
        if not messages or len(messages) < 2:
            return user_message
            
        # Construir historial ligero (últimos 4 mensajes)
        history_text = ""
        recent = messages[:-1][-4:] # Excluir el actual y tomar últimos 4
        for msg in recent:
            role = "User" if msg.get("role") == "user" else "Assistant"
            content = msg.get("content", "")
            if isinstance(content, list): # Handle multimodal
                 content = " ".join([c.get("text", "") for c in content if isinstance(c, dict) and c.get("text")])
            history_text += f"{role}: {content[:200]}\n" # Truncar para no saturar

        prompt = f"""###### TASK: QUERY REWRITING ######
Tu único trabajo es REESCRIBIR la última pregunta del usuario para que se entienda por sí sola, recuperando el contexto de la conversación anterior.
- Si el usuario dice "¿Y de Boeing?", reescribe a "Precio acciones Boeing".
- Si el usuario dice "explícamelo más", reescribe a "Explica más sobre [tema anterior]".
- Si la pregunta ya es clara, devuélvela EXACTAMENTE igual.
- NO respondas a la pregunta. SOLO devuelve la frase reescrita.
- MANTÉN el mismo idioma del usuario.

Conversation History:
{history_text}

Current Question: {user_message}

Rewritten Question:"""

        try:
            # Usar LiteLLM directamente (mucho más rápido que backend)
            rewritten = self._call_litellm(prompt, self.valves.TEXT_MODEL, temperature=0.0)
            rewritten = rewritten.strip().replace("Rewritten Question:", "").replace('"', '')
            
            logger.info(f"🔄 Rewrite: '{user_message}' -> '{rewritten}'")
            return rewritten
        except Exception as e:
            logger.error(f"Error rewriting query: {e}")
            return user_message

    def _handle_web_followup(self, body: Dict, user_message: str, messages: Optional[List[Dict]] = None) -> Generator:
        conversation_key = self._get_conversation_scope_key(body, messages)
        stored = self._user_web_memory.get(conversation_key)
        if not stored:
            yield "\u26a0\ufe0f No hay contenido web en memoria. Primero analiza una URL.\n"
            yield "\U0001f4a1 *Ejemplo: 'Analiza https://ejemplo.com'*\n"
            return
        title = stored.get("title", "Sin t\u00edtulo")
        url = stored.get("url", "")
        content = stored.get("content", "")
        yield f"\U0001f4ac **Respondiendo sobre**: [{title}]({url})\n\n"
        max_content_chars = 10000
        truncated = len(content) > max_content_chars
        context_content = content[:max_content_chars] if truncated else content
        followup_prompt = f"""###### REGLA CR\u00cdTICA: IDIOMA ######
Responde SIEMPRE en el mismo idioma que la pregunta del usuario.
############################################

Tienes acceso al siguiente contenido web previamente analizado:

FUENTE: {title}
URL: {url}
---
{context_content}
---
{'[NOTA: Contenido truncado por longitud]' if truncated else ''}

El usuario pregunta: {user_message}

Responde bas\u00e1ndote \u00daNCICAMENTE en el contenido proporcionado. Si la informaci\u00f3n solicitada no est\u00e1 en el texto, indica que no la encontraste en el contenido analizado."""
        try:
            answer = self._call_litellm(
                message=followup_prompt,
                model=self.valves.TEXT_MODEL,
                system_prompt="Eres un asistente que responde preguntas bas\u00e1ndose en contenido web previamente analizado. S\u00e9 preciso y cita partes relevantes del texto cuando sea \u00fatil."
            )
            if self._is_refusal_text(answer):
                answer = self._build_web_fallback_answer(context_content, title)
            yield answer
            yield f"\n\n---\n\U0001f517 *Fuente: [{title}]({url})*\n"
        except Exception as e:
            logger.error(f"Error en web followup: {e}")
            yield f"\u274c Error procesando la pregunta: {str(e)}\n"

    def _handle_save_web_content(self, body: Dict, user_data: Dict, messages: Optional[List[Dict]] = None) -> Generator:
        conversation_key = self._get_conversation_scope_key(body, messages)
        stored = self._user_web_memory.get(conversation_key)
        if not stored:
            yield "\u26a0\ufe0f No hay contenido web en memoria para guardar.\n"
            yield "\U0001f4a1 *Primero analiza una URL con: 'Analiza https://ejemplo.com'*\n"
            return
        url = stored.get("url", "")
        title = stored.get("title", "Sin t\u00edtulo")
        yield f"\U0001f4e5 **Indexando contenido de sesi\u00f3n**: {title}\n\n"
        try:
            write_headers = self._build_private_web_write_headers(user_data)
            target_collection = write_headers.get("X-Tenant-Id", "webs")
            response = requests.post(
                f"{self.valves.BACKEND_URL}/scrape",
                json={"url": url, "tenant_id": target_collection, "mode": "index"},
                headers=write_headers,
                timeout=120
            )
            response.raise_for_status()
            scrape_data = response.json()
            if scrape_data.get("status") == "processing":
                yield f"\u2705 **Contenido guardado exitosamente**\n\n"
                yield f"\U0001f4c4 **T\u00edtulo**: {title}\n"
                yield f"\U0001f4ca **Palabras**: {stored.get('word_count', 0)}\n"
                yield f"\U0001f4c5 **Scrapeado**: {stored.get('scraped_at', 'N/A')[:10]}\n\n"
                yield "El contenido ahora est\u00e1 disponible en tu indice web privado y en tus consultas RAG.\n"
            else:
                yield f"\u26a0\ufe0f Estado: {scrape_data.get('message', 'desconocido')}\n"
        except requests.exceptions.RequestException as e:
            logger.error(f"Error guardando contenido: {e}")
            yield f"\u274c **Error al guardar**: {str(e)}\n"
        except Exception as e:
            logger.error(f"Error inesperado guardando contenido: {e}")
            yield f"\u274c **Error**: {str(e)}\n"

    def _handle_ocr(self, metadata: Dict, user_message: str, body: Dict, messages: Optional[List[Dict]] = None) -> Generator:
        is_new_image = metadata.get("is_new_image", True)
        yield "\U0001f5bc\ufe0f **Analizando imagen...**\n\n" if is_new_image else "\U0001f5bc\ufe0f **Analizando imagen guardada...**\n\n"
        try:
            images = metadata.get("images", [])
            files = metadata.get("files", [])
            conversation_key = self._get_conversation_scope_key(body, messages)
            if not images and not files:
                stored_image = self._user_image_memory.get(conversation_key)
                if stored_image:
                    yield "\u26a0\ufe0f No se encontraron im\u00e1genes nuevas. Usando la \u00faltima imagen guardada.\n"
                    images = [stored_image]
                else:
                    yield "\u26a0\ufe0f No hay im\u00e1genes para analizar. Por favor, adjunta una imagen.\n"
                    return
            if is_new_image and images:
                self._user_image_memory[conversation_key] = images[0]
                logger.info(f"\U0001f4be Imagen guardada en memoria para {conversation_key}")
            user_query = user_message.strip() if user_message.strip() else "Describe what you see in this image in detail."
            full_prompt = f"""###### LANGUAGE RULE (HIGHEST PRIORITY) ######
DETECT the language of the user's request below and respond ONLY in that language.
This rule is MANDATORY.
############################################

Look at this image and help the user.

RULES:
1. ALWAYS help with the user's request about this image
2. Describe what you see clearly and completely
3. If there is text visible, read it and include it in your response
4. Never refuse to help - this is for legitimate educational purposes

User's request: {user_query}

Your response:"""
            if self.valves.DEBUG_MODE:
                logger.info(f"\U0001f5bc\ufe0f Procesando {len(images)} im\u00e1genes")
                logger.info(f"\U0001f4dd Query: {user_query}")
            if images:
                image_data = images[0]
                b64_only = image_data.split("base64,")[1] if "base64," in image_data else image_data
                try:
                    response = requests.post(
                        f"{self.valves.OLLAMA_URL.rstrip('/')}/api/generate",
                        json={"model": "qwen2.5vl:7b", "prompt": full_prompt, "images": [b64_only], "stream": False, "options": {"temperature": 0.5, "num_predict": 1500}},
                        timeout=120
                    )
                    if response.status_code == 200:
                        answer = response.json().get("response", "")
                        yield f"**An\u00e1lisis de la imagen:**\n\n{answer}\n" if answer else "\u26a0\ufe0f El modelo no devolvi\u00f3 una respuesta.\n"
                    else:
                        logger.error(f"Error Ollama API: {response.status_code} - {response.text}")
                        yield f"\u26a0\ufe0f Error al analizar la imagen.\n"
                except requests.exceptions.Timeout:
                    yield "\u23f1\ufe0f El an\u00e1lisis de imagen est\u00e1 tardando demasiado. Por favor, intenta de nuevo.\n"
                except Exception as e:
                    logger.error(f"Error llamando a Ollama: {e}")
                    yield f"\u274c Error procesando imagen: {str(e)}\n"
            elif files:
                yield "\U0001f4ce Procesando archivo adjunto...\n"
                yield "\u26a0\ufe0f Procesamiento de archivos adjuntos en desarrollo.\n"
        except Exception as e:
            logger.error(f"Error en OCR: {e}")
            yield f"\u274c Error procesando imagen: {str(e)}\n"

    def _handle_rag(self, effective_message: str, user_data: Dict, user_message: str, messages: List[Dict], body: Dict) -> Generator:
        conversation_key = self._get_conversation_scope_key(body, messages)
        if self._is_rag_followup(user_message, messages or []) and self._get_recent_rag_memory(conversation_key):
            yield from self._handle_rag_followup(conversation_key, user_message, messages)
            return

        yield "\U0001f4da **Consultando documentos internos...**\n\n"
        data = self._call_backend_chat(
            effective_message,
            "rag",
            user_data,
            chat_history=messages,
            conversation_key=conversation_key,
        )
        rag_content = data.get("content", "")
        sources = data.get("sources", [])
        if messages and len(messages) > 1:
            system_prompt = (
                "###### LANGUAGE RULE (HIGHEST PRIORITY) ######\n"
                "DETECT the language of the user's question and respond ONLY in that language.\n"
                "This rule is MANDATORY.\n"
                "############################################\n\n"
                "You are an expert enterprise assistant. Answer based on the document context provided.\n"
                f"{self._get_current_time_context()}\n"
                "You have access to the conversation history to understand follow-up questions.\n"
                "Be precise and cite sources when possible."
            )
            doc_context = f"Document search results:\n{rag_content}"
            try:
                content = self._call_litellm_with_history(
                    user_message=user_message, system_prompt=system_prompt,
                    messages=messages, extra_context=doc_context
                )
                if self._is_refusal_text(content):
                    content = rag_content
            except Exception as e:
                logger.warning(f"Error re-processing with history: {e}, using original response")
                content = rag_content
        else:
            content = rag_content
        yield content
        self._store_rag_memory(conversation_key, effective_message or user_message, content or rag_content, sources)
        if sources:
            yield "\n\n---\n### \U0001f4da Fuentes Citadas:\n\n"
            seen = set()
            for i, src in enumerate(sources, 1):
                filename = src.get("filename", "Doc")
                page = src.get("page")
                key = f"{filename}_{page}" if page else filename
                if key in seen:
                    continue
                seen.add(key)
                citation = f"**[{i}]** \U0001f4c4 {filename}"
                if page:
                    citation += f" (p\u00e1g. {page})"
                score = src.get("score")
                if score:
                    citation += f" - *relevancia: {score:.2f}*"
                yield f"{citation}\n"

    def _handle_file_chat(self, messages: List[Dict], user_message: str, body: Dict) -> Generator:
        parsed = self._extract_file_context_from_messages(messages, user_message)
        conversation_key = self._get_conversation_scope_key(body, messages)
        stored = self._get_recent_file_memory(conversation_key)

        if parsed.get("file_context"):
            yield "\U0001f4ce **Consultando documento adjunto en esta conversacion...**\n\n"
            stored = self._store_file_memory(conversation_key, parsed)
        else:
            yield "\U0001f4ce **Consultando documento adjunto ya cargado en esta conversacion...**\n\n"

        if not stored:
            yield "\u26a0\ufe0f No se encontro un documento adjunto disponible en esta conversacion.\n"
            yield "Sube el archivo de nuevo para poder consultarlo.\n"
            return

        try:
            filename = stored.get("filename", "documento")
            user_query = parsed.get("user_query") or user_message or f"Resume el documento {filename}."
            retrieved_chunks = self._retrieve_file_chunks(user_query, stored)

            if not retrieved_chunks:
                yield "\u26a0\ufe0f No se pudo extraer contenido suficiente del archivo.\n"
                return

            context_blocks = []
            citations = []
            for chunk in retrieved_chunks:
                block_id = chunk.get("id")
                source_name = chunk.get("source_name", filename)
                context_blocks.append(f"[bloque {block_id} | {source_name}]\n{chunk.get('text', '')}")
                citations.append(f"**[bloque {block_id}]** {source_name}")

            system_prompt = (
                "###### LANGUAGE RULE (HIGHEST PRIORITY) ######\n"
                "DETECT the language of the user's question and respond ONLY in that language.\n"
                "This rule is MANDATORY.\n"
                "############################################\n\n"
                "You answer questions using ONLY the provided document fragments.\n"
                "If the answer is clearly present, answer directly and do not refuse.\n"
                "You may restate obvious equivalents from the text, for example 'anual' means 'una vez al ano'.\n"
                "If the answer is not present, say so clearly.\n"
                "Cite the supporting fragment using the format [bloque X]."
            )
            prompt = (
                f"DOCUMENTO: {filename}\n\n"
                f"FRAGMENTOS RELEVANTES:\n---\n{chr(10).join(context_blocks)}\n---\n\n"
                f"PREGUNTA DEL USUARIO: {user_query}\n\n"
                "RESPUESTA:"
            )

            answer = self._call_litellm(
                message=prompt,
                model=self.valves.TEXT_MODEL,
                system_prompt=system_prompt,
                temperature=0.1,
            )
            if self._is_refusal_text(answer):
                answer = self._build_file_fallback_answer(user_query, retrieved_chunks)
            yield answer
            yield "\n\n---\n### Fragmentos usados:\n"
            for citation in citations:
                yield f"{citation}\n"
            yield f"\n*Archivo consultado en esta conversacion: {filename}*\n"
        except Exception as e:
            logger.error(f"Error en file_chat: {e}")
            yield f"\u274c Error procesando archivo: {str(e)}\n"

    def _handle_check_status(self) -> Generator:
        yield "\U0001f4ca **Estado del Sistema RAG Multi-Site**\n\n"
        try:
            yield "\U0001f504 **Sincronizaci\u00f3n SharePoint:**\n\n"
            indexer_resp = requests.get(f"{self.valves.INDEXER_URL}/health", timeout=5)
            if indexer_resp.status_code == 200:
                health_data = indexer_resp.json()
                multi_data = health_data.get("multi_site")
                if multi_data:
                    sites = multi_data.get("sites", [])
                    if not sites:
                        yield "\u26a0\ufe0f No hay sitios SharePoint configurados.\n"
                    else:
                        collection_names = [s.get("collection") for s in sites if s.get("collection")]
                        collection_status = {}
                        if collection_names:
                            try:
                                status_resp = requests.get(
                                    f"{self.valves.BACKEND_URL}/documents/collections/status",
                                    params={"collections": ",".join(collection_names)},
                                    timeout=10,
                                )
                                if status_resp.status_code == 200:
                                    status_data = status_resp.json()
                                    for row in status_data.get("collections", []):
                                        collection_status[row.get("requested_collection")] = row
                            except Exception as status_err:
                                logger.warning(f"No se pudo obtener estado real de colecciones: {status_err}")

                        yield "| Sitio | Colecci\u00f3n | Estado | Docs |\n"
                        yield "|---|---|---|---|\n"
                        for site in sites:
                            site_name = site.get("name")
                            requested_collection = site.get("collection")
                            enabled = bool(site.get("enabled"))
                            stats = collection_status.get(requested_collection, {})
                            exists = bool(stats.get("exists"))
                            docs_count = int(stats.get("display_documents", stats.get("documents", 0))) if stats else 0
                            resolved_collection = stats.get("resolved_collection") or requested_collection
                            fallback_alias = stats.get("fallback_alias")
                            fallback_docs = int(stats.get("fallback_documents", 0)) if stats else 0

                            if not enabled:
                                status_icon = "\u23f8\ufe0f"
                            elif exists and docs_count > 0:
                                status_icon = "\u2705"
                            elif exists:
                                status_icon = "\U0001f7e1"
                            else:
                                status_icon = "\u274c"

                            collection_label = f"`{requested_collection}`"
                            if resolved_collection and resolved_collection != requested_collection:
                                collection_label = f"`{requested_collection}` \u2192 `{resolved_collection}`"
                            elif fallback_alias and fallback_docs > 0:
                                collection_label = f"`{requested_collection}` \u2194 `{fallback_alias}`"

                            yield f"| {site_name} | {collection_label} | {status_icon} | {docs_count} |\n"
                        yield "\nLeyenda: ✅ indexada con documentos, 🟡 colección vacía, ❌ colección no existe, ⏸️ sitio deshabilitado.\n"
                else:
                    if health_data.get("sharepoint_enabled"):
                        yield "\u2705 SharePoint (Single Site) activo.\n"
                    else:
                        yield "\u26aa SharePoint desactivado.\n"
            else:
                yield f"\u26a0\ufe0f No se pudo conectar con el indexer ({indexer_resp.status_code}).\n"
        except Exception as e:
            logger.error(f"Error checking indexer status: {e}")
            yield f"\u274c Error conectando con servicio de indexaci\u00f3n: {str(e)}\n"
        yield "\n---\n"
        try:
            yield "\U0001f4c4 **Ingesti\u00f3n de Archivos (Reciente):**\n\n"
            response = requests.get(f"{self.valves.BACKEND_URL}/documents/ingestion-status", params={"limit": 10}, timeout=10)
            response.raise_for_status()
            items = response.json()
            if not items:
                yield "No hay actividad reciente de ingesti\u00f3n de archivos.\n"
            else:
                yield "| Archivo | Estado | Mensaje | Actualizado |\n"
                yield "|---|---|---|---|\n"
                status_icons = {"pending": "\u23f3", "processing": "\u2699\ufe0f", "completed": "\u2705", "failed": "\u274c", "deleted": "\U0001f5d1\ufe0f"}
                for item in items:
                    icon = status_icons.get(item['status'], "\u2753")
                    msg = item.get('message') or ""
                    if len(msg) > 30:
                        msg = msg[:27] + "..."
                    ts = item.get('updated_at', '').split('T')[1][:8] if 'T' in item.get('updated_at', '') else item.get('updated_at')
                    yield f"| {item['filename']} | {icon} {item['status']} | {msg} | {ts} |\n"
        except Exception as e:
            logger.error(f"Error checking backend status: {e}")
            yield f"\u274c Error consultando estado de archivos: {str(e)}\n"
        yield "\n\U0001f50e *Los archivos de SharePoint se sincronizan autom\u00e1ticamente cada 5 minutos.*\n"

    def _handle_agent(self, user_message: str, user_data: Dict, messages: List[Dict], body: Dict) -> Generator:
        if self.valves.DEBUG_MODE:
            yield "\U0001f9e0 *Delegando al Agente del Backend...*\n\n"
        conversation_key = self._get_conversation_scope_key(body, messages)
        data = self._call_backend_chat(
            user_message,
            "agent",
            user_data,
            messages,
            conversation_key=conversation_key,
        )
        content = data.get("content", "")
        sources = data.get("sources", [])
        yield content
        if sources:
            yield "\n\n---\n### \U0001f4da Fuentes o Herramientas Citadas:\n\n"
            seen = set()
            for i, src in enumerate(sources, 1):
                filename = src.get("filename", "Recurso")
                page = src.get("page")
                url = src.get("url")
                key = f"{filename}_{page}" if page else filename
                if key in seen:
                    continue
                seen.add(key)
                citation = f"**[{i}]** "
                if url:
                    citation += f"\U0001f310 [{filename}]({url})"
                else:
                    citation += f"\U0001f4c4 {filename}"
                    if page:
                        citation += f" (p\u00e1g. {page})"
                yield f"{citation}\n"

    def _handle_chat(self, user_message: str, user_data: Dict, messages: List[Dict]) -> Generator:
        if self.valves.DEBUG_MODE:
            yield "\U0001f4ac **Modo conversaci\u00f3n**\n\n"
        try:
            # Heuristic memory fallback for "what is my name?" follow-ups.
            message_norm = self._normalize_text(user_message)
            if re.search(r"(recuerdas|sabes).*(como me llamo|mi nombre)|como me llamo", message_norm):
                name = self._extract_name_from_history(messages or [])
                if name:
                    if self._is_probably_english(user_message):
                        yield f"Your name is {name}."
                    else:
                        yield f"Te llamas {name}."
                    return

            is_english = self._is_probably_english(user_message)
            if is_english:
                system_prompt = (
                    "###### LANGUAGE RULE ######\n"
                    "The user's message is in English. Respond ONLY in English.\n"
                    "This rule is MANDATORY.\n"
                    "############################################\n\n"
                    "You are JARVIS, an enterprise RAG assistant.\n"
                    "You help users search internal documents, web content, and official sources.\n\n"
                    f"{self._get_current_time_context()}\n\n"
                    "MANDATORY RULES:\n"
                    "1. NEVER say you are Claude, GPT, LLaMA, Qwen or any other AI model.\n"
                    "2. If asked who you are: respond in English. Example: 'I am JARVIS, an enterprise RAG assistant.'\n"
                    "3. Do not invent information. If you don't know, say so.\n"
                    "4. For internal company data, suggest using RAG.\n\n"
                    "Respond clearly, helpfully, and in English."
                )
            else:
                system_prompt = (
                    "###### REGLA DE IDIOMA ######\n"
                    "DETECTA el idioma del mensaje del usuario y responde SOLO en ese idioma.\n"
                    "- Mensaje en ingl\u00e9s \u2192 Responde en ingl\u00e9s\n"
                    "- Mensaje en espa\u00f1ol \u2192 Responde en espa\u00f1ol\n"
                    "Esta regla es OBLIGATORIA.\n"
                    "############################################\n\n"
                    "Eres JARVIS, un asistente RAG empresarial.\n"
                    "Ayudas a consultar documentos internos, contenido web y fuentes oficiales.\n\n"
                    f"{self._get_current_time_context()}\n\n"
                    "REGLAS OBLIGATORIAS:\n"
                    "1. NUNCA digas que eres Claude, GPT, LLaMA, Qwen o cualquier otro modelo de IA.\n"
                    "2. Si preguntan qui\u00e9n eres: responde en el idioma del usuario.\n"
                    "   ES: 'Soy JARVIS, un asistente RAG empresarial.'\n"
                    "   EN: 'I am JARVIS, an enterprise RAG assistant.'\n"
                    "3. NO inventes informaci\u00f3n. Si no sabes algo, adm\u00edtelo.\n"
                    "4. Para datos internos de la empresa, sugiere consultar documentos con RAG.\n\n"
                    "Responde de forma clara, \u00fatil y amigable."
                )
            llm_messages = [{"role": "system", "content": system_prompt}]
            if messages and len(messages) > 1:
                history = messages[:-1][-20:]
                for msg in history:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = " ".join([c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"])
                    if content and role in ["user", "assistant"]:
                        llm_messages.append({"role": role, "content": content})
            llm_messages.append({"role": "user", "content": user_message})
            if self.valves.DEBUG_MODE:
                logger.info(f"Chat: Enviando {len(llm_messages)} mensajes al LLM")
            response = requests.post(
                f"{self.valves.LITELLM_URL}/v1/chat/completions",
                json={"model": self.valves.TEXT_MODEL, "messages": llm_messages, "temperature": 0.7, "stream": False},
                headers=self._litellm_headers(),
                timeout=self.valves.LITELLM_TIMEOUT
            )
            response.raise_for_status()
            yield response.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Error en chat directo: {e}")
            data = self._call_backend_chat(user_message, "chat", user_data, messages)
            yield data.get("content", "")

    def _extract_name_from_history(self, messages: List[Dict]) -> str:
        """Extract user name from recent user messages."""
        if not messages:
            return ""
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                )
            text = (content or "").strip()
            if not text:
                continue
            m = re.search(r"\bme llamo\s+([A-Za-zÁÉÍÓÚáéíóúÑñ'\\-]{2,40})", text, re.IGNORECASE)
            if m:
                return m.group(1).strip(" .,;:!?")
        return ""

    def _handle_web_search(self, effective_message: str, user_message: str, messages: List[Dict]) -> Generator:
        yield "🌐 **Buscando en internet...**\n\n"
        search_query = self._prepare_web_search_query(effective_message, user_message)
        source_request = self._extract_named_web_source(user_message) or self._extract_named_web_source(effective_message)
        logger.info(f"Web search: '{user_message}' -> Rewritten: '{effective_message}' -> Clean: '{search_query}'")

        try:
            weather_summary = self._fetch_weather_summary(user_message) or self._fetch_weather_summary(search_query)
        except Exception as e:
            logger.warning(f"Weather lookup fallback to web search: {e}")
            weather_summary = None
        if weather_summary:
            yield weather_summary["answer"]
            yield "\n\n---\n### Fuente meteorologica:\n"
            yield f"**[1]** [{weather_summary['source_title']}]({weather_summary['source_link']})\n"
            return

        try:
            local_activity_summary = self._fetch_local_activity_summary(user_message) or self._fetch_local_activity_summary(search_query)
        except Exception as e:
            logger.warning(f"Local activity lookup fallback to web search: {e}")
            local_activity_summary = None
        if local_activity_summary:
            yield local_activity_summary["answer"]
            yield "\n\n---\n### Fuentes Web:\n"
            for idx, source in enumerate(local_activity_summary.get("sources", [])[:3], 1):
                yield f"**[{idx}]** [{source['title']}]({source['link']})\n"
            return

        results: List[Dict] = []
        search_query_norm = self._normalize_text(search_query)
        if (
            "real madrid" in search_query_norm
            and any(token in search_query_norm for token in ["resultado", "marcador"])
        ):
            try:
                results = self._search_real_madrid_live_results()
            except Exception as e:
                logger.warning(f"Direct Real Madrid lookup failed: {e}")

        if source_request and source_request["source"] == "infodefensa":
            try:
                results = self._search_infodefensa_results(source_request["query"])
            except Exception as e:
                logger.warning(f"Infodefensa direct search failed: {e}")

        if not results:
            results = self._search_backend_web(search_query)

        if (
            "real madrid" in search_query_norm
            and any(token in search_query_norm for token in ["resultado", "marcador"])
            and not results
            and not self._results_contain_score(results)
        ):
            try:
                fallback_results = self._search_backend_web("site:flashscore.es real madrid resultado")
                if fallback_results:
                    results = fallback_results
            except Exception as e:
                logger.warning(f"Sports fallback web search failed: {e}")

        if not results:
            yield "No se encontraron resultados en la búsqueda web.\n"
            return

        official_site_terms = [
            "web oficial", "sitio oficial", "página oficial", "pagina oficial",
            "official site", "official website",
        ]
        is_official_site_query = any(term in search_query.lower() or term in user_message.lower() for term in official_site_terms)
        if is_official_site_query:
            primary = results[0]
            title = primary.get("title", "Sitio oficial")
            link = primary.get("link", "")
            snippet = primary.get("snippet", "")

            answer_lines = [f"El sitio oficial es {link}."]
            if title:
                answer_lines.append(f"Referencia principal: {title}.")
            if snippet:
                answer_lines.append(snippet)

            yield "\n\n".join(answer_lines)
            yield "\n\n---\n### 🌐 Fuentes Web:\n"
            for i, res in enumerate(results[:3], 1):
                yield f"**[{i}]** [{res['title']}]({res['link']})\n"
            return
        
        context = "Resultados de búsqueda web:\n\n"
        for i, res in enumerate(results, 1):
            context += f"{i}. **{res['title']}**\n   {res['snippet']}\n   Fuente: {res['link']}\n\n"
        
        system_prompt = (
            "###### REGLA CRÍTICA: SOLO USA DATOS DE LAS FUENTES ######\n"
            "SOLO puedes usar información que aparezca en los resultados de búsqueda.\n"
            "PROHIBIDO inventar datos, fechas, cifras o hechos no presentes en las fuentes.\n"
            "Si las fuentes no contienen la información, di: 'No encontré información específica sobre eso.'\n"
            "############################################\n\n"
            "Eres JARVIS. Responde en el idioma del usuario.\n"
            f"{self._get_current_time_context()}\n"
            "Sintetiza la información de los resultados de búsqueda.\n"
            "Cita las fuentes cuando menciones datos específicos."
        )
        try:
            answer = self._call_litellm_with_history(
                user_message=user_message,
                system_prompt=system_prompt,
                messages=messages,
                extra_context=context
            )
            if self._is_refusal_text(answer):
                answer = self._build_web_search_results_fallback(search_query, results)
        except Exception as e:
            logger.warning(f"Web search LLM fallback activated: {e}")
            answer = self._build_web_search_results_fallback(search_query, results)
        yield answer
        yield "\n\n---\n### 🌐 Fuentes Web:\n"
        for i, res in enumerate(results[:3], 1):
            yield f"**[{i}]** [{res['title']}]({res['link']})\n"

    def _handle_url_decision(self, metadata: Dict, body: Dict, user_message: str, messages: Optional[List[Dict]] = None) -> Generator:
        decision = metadata.get("decision")
        data = metadata.get("data", {})
        url = data.get("url")
        title = data.get("title", "Sin título")
        user_email = self._get_user_email(body)
        conversation_key = self._get_conversation_scope_key(body, messages)
        if conversation_key in self._user_pending_decision:
            del self._user_pending_decision[conversation_key]

        if decision == "use_existing":
            yield f"📂 **Recuperando contenido existente**: {title}...\n\n"
            try:
                retrieve_resp = requests.post(
                    f"{self.valves.BACKEND_URL}/scrape/retrieve",
                    json={"url": url},
                    headers=self._build_web_lookup_headers(user_email),
                    timeout=30,
                ).json()
                content = retrieve_resp.get("content", "")
                if not content:
                    yield "⚠️ Error recuperando contenido. Actualizando en su lugar...\n"
                    yield from self._scrape_and_summarize(url, user_message, user_email, conversation_key, check_rag=False)
                    return
                from datetime import datetime
                self._user_web_memory[conversation_key] = {
                    "url": url, "title": title, "content": content,
                    "word_count": len(content.split()),
                    "scraped_at": datetime.now().isoformat(),
                    "extraction_method": "rag_retrieval",
                }
                analysis_content = content[:8000]
                summary_prompt = f"###### REGLA CRÍTICA: IDIOMA ######\nResponde SIEMPRE en el mismo idioma que la pregunta del usuario.\n############################################\n\nAnaliza el siguiente contenido RECUPERADO DE MEMORIA y proporciona:\n1. Un resumen conciso\n2. Puntos clave\n\nCONTENIDO ({title}):\n---\n{analysis_content}\n---\n\nEl usuario pregunta: {user_message}\n"
                yield "📝 **Generando resumen de memoria...**\n\n"
                summary = self._call_litellm(summary_prompt, self.valves.TEXT_MODEL, "Eres un asistente experto.")
                if self._is_refusal_text(summary):
                    summary = self._build_web_fallback_answer(content, title)
                yield summary
                yield f"\n\n---\n🔗 **Fuente**: [{title}]({url})\n"
                yield f"\n💬 *Puedes hacer preguntas de seguimiento sobre este contenido.*\n"
            except Exception as e:
                logger.error(f"Error retrieving: {e}")
                yield f"❌ Error recuperando contenido: {e}\n"
        elif decision == "update":
            yield f"🔄 **Actualizando contenido...**\n"
            yield from self._scrape_and_summarize(url, user_message, user_email, conversation_key, check_rag=False)

    def _handle_web_scrape(self, metadata: Dict, user_message: str, body: Dict, messages: Optional[List[Dict]] = None) -> Generator:
        url = metadata["url"]
        user_email = self._get_user_email(body)
        conversation_key = self._get_conversation_scope_key(body, messages)
        yield from self._scrape_and_summarize(url, user_message, user_email, conversation_key, check_rag=True)

    def _handle_web_index(self, metadata: Dict, user_data: Dict) -> Generator:
        url = metadata["url"]
        yield f"📥 **Indexando URL**: {url}\n\n"
        try:
            write_headers = self._build_private_web_write_headers(user_data)
            target_collection = write_headers.get("X-Tenant-Id", "webs")
            response = requests.post(
                f"{self.valves.BACKEND_URL}/scrape",
                json={"url": url, "tenant_id": target_collection, "mode": "index"},
                headers=write_headers,
                timeout=120
            )
            response.raise_for_status()
            scrape_data = response.json()
            if scrape_data.get("status") == "processing":
                title = scrape_data.get("title", "Sin título")
                word_count = scrape_data.get("word_count", 0)
                yield f"✅ **Contenido indexado exitosamente**\n\n"
                yield f"📄 **Título**: {title}\n"
                yield f"📊 **Palabras**: {word_count}\n\n"
                yield "El contenido ahora está disponible en tu índice web privado y en tus consultas RAG.\n"
                yield f"\n💡 *Prueba preguntando: \"¿Qué información hay sobre {title}?\"*\n"
            else:
                yield f"⚠️ Estado: {scrape_data.get('message', 'desconocido')}\n"
        except requests.exceptions.RequestException as e:
            logger.error(f"Error indexando {url}: {e}")
            yield f"❌ **Error al indexar**: No se pudo acceder a la URL.\n"
        except Exception as e:
            logger.error(f"Error inesperado indexando: {e}")
            yield f"❌ **Error**: {str(e)}\n"
    def _handle_search_docs(self, user_data: Dict, metadata: Dict) -> Generator:
        query = str(metadata.get("query", "") or "").strip()
        if not query:
            yield "🔎 Indica qué documento quieres buscar. Ejemplo: `buscar documento IT-045`.\n"
            return

        authorized_depts = self._get_user_departments(user_data)
        if not authorized_depts:
            yield "No tienes acceso a ninguna coleccion autorizada.\n"
            return

        final_depts = list(authorized_depts)
        target_collection = metadata.get("target_collection")
        if target_collection:
            resolved_target = self._resolve_collection_name(target_collection) or target_collection
            matched_depts = [d for d in authorized_depts if d == resolved_target or resolved_target.lower() in d.lower()]
            if matched_depts:
                final_depts = matched_depts
            else:
                yield f"No tienes acceso a la coleccion solicitada: {target_collection}.\n"
                return

        yield f"🔎 **Buscando documentos**: `{query}`\n\n"
        try:
            response = requests.get(
                f"{self.valves.BACKEND_URL}/documents/search",
                params={"q": query, "limit": 12},
                headers={"X-Tenant-Ids": ",".join(final_depts)},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            results = data.get("results", []) or []
            total = int(data.get("total", 0) or 0)

            if total == 0:
                yield "No he encontrado documentos que coincidan con esa busqueda.\n"
                yield "\n💡 Prueba con una referencia exacta (`IT-045`, `MAN-003`) o parte del nombre.\n"
                return

            yield f"**Resultados**: {total}\n\n"
            for index, result in enumerate(results, 1):
                icon = "🌐" if result.get("type") == "web" else "📄"
                filename = result.get("filename", "Sin nombre")
                collection = result.get("collection", "desconocida")
                chunks = result.get("chunks", 0)
                score = result.get("score", 0)
                yield f"{index}. {icon} **{filename}**\n"
                yield f"   Coleccion: `{collection}` | Chunks: {chunks} | Score: {score}\n"

            if total > len(results):
                yield f"\n*Mostrando {len(results)} de {total} coincidencias.*\n"

            yield "\n💡 Para consultar el contenido, pregunta por el documento en el chat RAG.\n"
            yield "Ejemplos: `que dice IT-045` o `busca en documentos internos MAN-003`.\n"
        except Exception as e:
            logger.error(f"Error searching documents: {e}")
            yield f"❌ Error buscando documentos: {e}\n"

    def _handle_list_docs(self, user_data: Dict, metadata: Dict, user_message: str) -> Generator:
        yield "\U0001F4CB **Consultando documentos disponibles...**\n\n"
        authorized_depts = self._get_user_departments(user_data)
        if not authorized_depts:
            yield "No tienes acceso a ninguna coleccion autorizada.\n"
            return

        final_depts = list(authorized_depts)
        target_collection = metadata.get("target_collection")
        if target_collection:
            resolved_target = self._resolve_collection_name(target_collection) or target_collection
            matched_depts = [d for d in authorized_depts if d == resolved_target or target_collection.lower() in d.lower()]
            if matched_depts:
                final_depts = matched_depts
                logger.info(f"Filtrando listado a coleccion solicitada: {final_depts}")
            else:
                yield f"No tienes acceso a la coleccion solicitada: {target_collection}.\n"
                return

        try:
            response = requests.get(
                f"{self.valves.BACKEND_URL}/documents/list",
                headers={"X-Tenant-Ids": ",".join(final_depts)},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            total = data.get("total", 0)
            documents = data.get("documents", [])

            if total == 0:
                yield "No tienes documentos disponibles para consultar.\n"
                yield f"\n*Acceso actual: {', '.join(final_depts)}*\n"
                return

            show_all = any(kw in user_message.lower() for kw in ["todos", "todo", "all", "completa", "entera"])
            max_docs_show = 20
            yield f"**Total de documentos**: {total}\n\n"

            documents.sort(key=lambda x: (0 if x.get("type") == "web" else 1, x["filename"]))
            if total > max_docs_show and not show_all:
                yield f"\u2B07\uFE0F *Mostrando primeros {max_docs_show} documentos (Webs primero)*:\n\n"
                docs_to_show = documents[:max_docs_show]
            else:
                yield "**Documentos disponibles para consulta**:\n\n"
                docs_to_show = documents

            for i, doc in enumerate(docs_to_show, 1):
                icon = "\U0001F310" if doc.get("type") == "web" else "\U0001F4C4"
                yield f"{i}. {icon} **{doc['filename']}**\n"

            if total > max_docs_show and not show_all:
                yield f"\n*... y {total - max_docs_show} más.*\n"
                yield "\n\U0001F4A1 *Para ver TODOS los documentos, escribe: 'listar todos los documentos'*\n"
                yield "\n\U0001F4A1 *Para ver documentos específicos, usa el buscador o filtra por colección.*\n"

            yield f"\n*Acceso a: {', '.join(final_depts)}*\n"
        except Exception as e:
            logger.error(f"Error listing documents: {e}")
            yield f"\u274C Error recuperando lista de documentos: {e}\n"

    def _handle_check_boe_legacy(
        self,
        metadata: Dict,
        effective_message: str,
        messages: List[Dict],
        user_message: str,
        user_data: Dict,
    ) -> Generator:
        boe_type = metadata.get("type", "legislation")
        if boe_type == "tenders":
            yield "🏛️ **Buscando licitaciones en el BOE...**\n\n"
            endpoint = "tenders"
            query = effective_message.lower()
            for kw in ["licitaciones", "licitacion", "concurso", "contrato", "boe", "busca", "en el"]:
                query = query.replace(kw, "")
            query = query.strip()
            if not query: query = "general"
        else:
            yield "⚖️ **Consultando legislación en el BOE...**\n\n"
            endpoint = "search"
            clean_query = effective_message.lower()
            prefixes = [
                "busca en el boe", "buscar en el boe", "consulta en el boe", 
                "dime del boe", "busca boe", "en el boe", "sobre el boe",
                "las ultimas", "los ultimos", "aviones caza"
            ]
            for p in ["busca en el boe", "buscar en el boe", "consulta en el boe", "dime del boe", "busca boe", "en el boe"]:
                clean_query = clean_query.replace(p, "")
            clean_query = clean_query.strip()
            if not clean_query: clean_query = effective_message
            query = clean_query

        try:
            boe_url = self.valves.BACKEND_URL
            logger.info(f"BOE Request URL: {boe_url}/external/boe/{endpoint} with params q={query}")
            response = requests.get(
                f"{boe_url}/external/boe/{endpoint}",
                params={"q": query, "days": 30},
                timeout=15
            )
            if response.status_code != 200:
                yield f"⚠️ Error consultando BOE: {response.status_code}\n"
                return

            data = response.json()
            results = data.get("results", [])
            if not results:
                yield f"No se encontraron resultados en el BOE para '{query}'.\n"
                return

            context = f"Resultados del BOE ({boe_type}):\n\n"
            for i, res in enumerate(results[:10], 1):
                 context += f"{i}. **{res['title']}**\n"
                 context += f"   Fecha: {res.get('date', 'N/A')}\n"
                 context += f"   Resumen: {res.get('summary', '')}\n"
                 context += f"   Enlace: {res['link']}\n\n"

            yield f"✅ **Encontrados {len(results)} resultados.** Analizando...\n\n"
            system_prompt = (
                "Eres un asistente legal experto. Responde basándote en los resultados del BOE proporcionados.\n"
                "Sé preciso con las fechas y referencias legales.\n"
                "Si hay licitaciones, resume los puntos clave (objeto, fecha).\n"
                "Proporciona los enlaces a los documentos oficiales."
            )
            answer = self._call_litellm_with_history(
                user_message=user_message,
                system_prompt=system_prompt,
                messages=messages,
                extra_context=context
            )
            yield answer
            yield "\n\n---\n### 🏛️ Fuentes BOE:\n"
            for i, res in enumerate(results[:5], 1):
                yield f"**[{i}]** [{res['title']}]({res['link']})\n"
        except Exception as e:
            logger.error(f"Error calling BOE API: {e}")
            yield f"❌ Error de conexión con el servicio BOE: {str(e)}\n"

    def _handle_check_boe(
        self,
        metadata: Dict,
        effective_message: str,
        messages: List[Dict],
        user_message: str,
        user_data: Dict,
        body: Dict,
    ) -> Generator:
        user_email = "anonymous"
        if isinstance(user_data, dict):
            user_email = user_data.get("email", "anonymous")
        conversation_key = self._get_conversation_scope_key(body, messages)

        boe_type = metadata.get("type", "legislation")
        followup = bool(metadata.get("followup"))
        stored = self._user_boe_memory.get(conversation_key) or {}
        generic_followup = followup and self._is_generic_followup(user_message)
        focus_summary_followup = followup and self._is_boe_focus_summary_request(user_message)
        boe_url = self.valves.BACKEND_URL.rstrip("/")

        def clean_query(raw_query: str) -> str:
            query = self._normalize_text(raw_query)
            noise_patterns = [
                r"\b(busca|buscar|consulta|consultar|dime|dame|mira|ver|explica)\b",
                r"\b(en|del|de|el|la|los|las|sobre)\b",
                r"\bboe\b",
                r"\bque dice\b",
                r"\bpuedes\b",
                r"\bhablarme\b",
                r"\bmas\b",
                r"\bde eso\b",
            ]
            for pattern in noise_patterns:
                query = re.sub(pattern, " ", query)
            query = " ".join(query.split())
            return query or raw_query.strip()

        def merge_results(primary: List[Dict], secondary: List[Dict]) -> List[Dict]:
            merged = []
            seen = set()
            for result in (primary or []) + (secondary or []):
                key = result.get("link") or result.get("title")
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(result)
            return merged

        def build_context(target_query: str, ranked_results: List[Dict], focus_result: Optional[Dict]) -> str:
            def shrink(value: str, max_chars: int) -> str:
                clean = " ".join(str(value or "").split())
                if len(clean) <= max_chars:
                    return clean
                return clean[: max_chars - 1].rstrip() + "..."

            context_limit = max(1, int(self.valves.BOE_CONTEXT_RESULTS))
            if boe_type == "summary":
                context_limit = min(context_limit, 4)

            lines = [f"Consulta objetivo: {target_query}", f"Tipo BOE: {boe_type}", ""]
            if focus_result:
                lines.extend(
                    [
                        "RESULTADO PRINCIPAL:",
                        f"Titulo: {shrink(focus_result.get('title', 'Sin titulo'), 180)}",
                        f"Fecha: {focus_result.get('date', 'N/D')}",
                        f"Resumen: {shrink(focus_result.get('summary', 'Sin resumen'), self.valves.BOE_FOCUS_SUMMARY_CHARS)}",
                        f"Enlace: {focus_result.get('link', '')}",
                        "",
                    ]
                )

            lines.append("RESULTADOS DISPONIBLES:")
            for index, result in enumerate(ranked_results[:context_limit], 1):
                lines.extend(
                    [
                        f"{index}. {shrink(result.get('title', 'Sin titulo'), 180)}",
                        f"Fecha: {result.get('date', 'N/D')}",
                        f"Resumen: {shrink(result.get('summary', 'Sin resumen'), self.valves.BOE_CONTEXT_SUMMARY_CHARS)}",
                        f"Enlace: {result.get('link', '')}",
                        "",
                    ]
                )
            return "\n".join(lines)

        query = clean_query(effective_message)
        if boe_type == "legislation" and (
            self._is_boe_summary_request(user_message)
            or (self._contains_boe_reference(user_message) and "hoy" in self._normalize_text(user_message) and not query)
            or query in {"hoy", "publicaciones", "publicaciones hoy", "noticias", "noticias hoy", "sumario"}
        ):
            boe_type = "summary"
        endpoint = "search"
        request_method = "GET"
        request_kwargs: Dict = {"timeout": 20}
        results: List[Dict] = []
        focus_result = stored.get("focus_result")
        status_message = "Consultando BOE..."

        if boe_type == "summary":
            status_message = "Consultando sumario reciente del BOE..."
            request_method = "POST"
            request_kwargs["json"] = {"mode": "summary", "query": query, "days_back": 30}
        elif boe_type == "tenders":
            endpoint = "tenders"
            status_message = "Buscando anuncios y licitaciones en el BOE..."
            request_kwargs["params"] = {"q": query or "general", "days": 30}
        else:
            endpoint = "search"
            status_message = "Consultando legislacion en el BOE..."
            request_kwargs["params"] = {"q": query, "days": 30}

        yield f"{status_message}\n\n"

        try:
            if (generic_followup or focus_summary_followup) and stored.get("results"):
                boe_type = stored.get("type", boe_type)
                query = stored.get("query", query)
                results = list(stored.get("results", []))
                focus_result = self._select_boe_focus_result(user_message, results, stored.get("focus_result"))
            else:
                if request_method == "POST":
                    response = requests.post(f"{boe_url}/external/boe", **request_kwargs)
                else:
                    response = requests.get(f"{boe_url}/external/boe/{endpoint}", **request_kwargs)

                if response.status_code != 200:
                    yield f"Error consultando BOE: HTTP {response.status_code}\n"
                    return

                data = response.json()
                results = data.get("results", []) or []

                if followup and stored.get("results"):
                    results = merge_results(results, stored.get("results", []))
                    if not focus_result:
                        focus_result = stored.get("focus_result")

                if not results and followup and stored.get("results"):
                    results = list(stored.get("results", []))
                    focus_result = stored.get("focus_result")

            ranked_results = self._rank_boe_results(
                user_message if followup else query,
                results,
                focus_result=focus_result,
            )

            if boe_type == "summary":
                filtered_summary = [
                    result for result in ranked_results
                    if len(re.findall(r"[a-z0-9]+", self._normalize_text(result.get("title", "")))) >= 3
                ]
                if filtered_summary:
                    ranked_results = filtered_summary

            if not ranked_results:
                yield f"No se encontraron resultados en el BOE para '{query}'.\n"
                return

            selected_focus_result = self._select_boe_focus_result(user_message, ranked_results, focus_result)
            focus_result = selected_focus_result or ranked_results[0]
            memory_query = query if not followup or generic_followup or focus_summary_followup else user_message
            self._store_boe_memory(conversation_key, boe_type, memory_query, ranked_results)
            self._user_boe_memory[conversation_key]["focus_result"] = focus_result

            context = build_context(memory_query, ranked_results, focus_result)
            yield f"Encontrados {len(ranked_results)} resultados relevantes. Analizando...\n\n"

            if boe_type != "summary" and focus_result and (followup or self._is_boe_focus_summary_request(user_message)):
                yield self._format_boe_focus_result_response(focus_result)
                yield "\n\n---\n### Fuentes BOE:\n"
                for index, result in enumerate(ranked_results[:5], 1):
                    yield f"**[{index}]** [{result.get('title', 'Sin titulo')}]({result.get('link', '')})\n"
                return

            if boe_type == "summary" and not followup:
                system_prompt = (
                    "Eres un asistente experto en BOE. "
                    "Resume entre 3 y 5 noticias o anuncios relevantes del contexto. "
                    "No te centres en un solo resultado. "
                    "Para cada punto indica de forma breve el titulo, la fecha exacta y el enlace oficial. "
                    "No inventes detalles que no aparezcan en el contexto."
                )
            else:
                system_prompt = (
                    "Eres un asistente experto en BOE y contratacion publica. "
                    "Responde solo con la informacion del contexto. "
                    "Prioriza el RESULTADO PRINCIPAL si la pregunta es de seguimiento. "
                    "Incluye fechas exactas, expediente si aparece, y el enlace oficial. "
                    "Si el contexto no basta para una afirmacion, dilo claramente."
                )
            answer = self._call_litellm(
                message=f"Pregunta del usuario:\n{user_message}\n\nContexto BOE:\n{context}",
                model=self.valves.TEXT_MODEL,
                system_prompt=system_prompt,
                temperature=0.2,
            )
            answer_norm = self._normalize_text(answer)
            if focus_result and any(
                marker in answer_norm
                for marker in [
                    "no puedo cumplir con esa solicitud",
                    "no puedo ayudar",
                    "lo siento",
                ]
            ):
                answer = self._format_boe_focus_result_response(focus_result)
            yield answer
            yield "\n\n---\n### Fuentes BOE:\n"
            for index, result in enumerate(ranked_results[:5], 1):
                yield f"**[{index}]** [{result.get('title', 'Sin titulo')}]({result.get('link', '')})\n"
        except Exception as e:
            logger.error(f"Error calling BOE API: {e}")
            yield f"Error de conexion con el servicio BOE: {str(e)}\n"

    def _handle_help(self, detailed: bool = False) -> Generator:
        if not detailed:
            yield "🧭 **Guia rapida de JARVIS**\n\n"
            yield "JARVIS puede ayudarte con esto:\n\n"
            yield "- 💬 **Chat general**: `hola`, `que puedes hacer`, `explicame esta duda`\n"
            yield "- 📚 **Documentos internos**: `que dice la politica de calidad`, `busca en documentos internos IT-045`\n"
            yield "- 🔎 **Buscar documentos**: `buscar documento IT-045`, `buscar en calidad homologacion proveedores`\n"
            yield "- 📋 **Listar documentos**: `docs`, `listar todos los documentos`\n"
            yield "- 🌐 **Internet**: `busca en internet la web oficial de Airbus`\n"
            yield "- 🏛️ **BOE y licitaciones**: `noticias de hoy del BOE`, `busca en el BOE la ley de contratos`\n"
            yield "- 🔎 **Analizar una web**: `analiza https://www.boe.es`\n"
            yield "- 💾 **Guardar una web en tu indice privado**: `indexa https://example.com`\n"
            yield "- 🕸️ **Explorar una web completa**: `explora a fondo https://example.com`\n"
            yield "- 📎 **Archivo subido al chat**: `que dice este documento`, `resumemelo`\n"
            yield "- 🖼️ **Imagenes / OCR**: `describe esta imagen`, `transcribe el texto`\n"
            yield "- 📊 **Estado del sistema**: `estado`, `check status`\n\n"
            yield "✨ **Diferencia clave en webs**\n"
            yield "- `analiza URL` = lee una pagina ahora\n"
            yield "- `indexa URL` = la guarda en tu indice privado\n"
            yield "- `explora a fondo URL` = hace crawler y sigue enlaces\n\n"
            yield "➡️ Si quieres la version detallada, escribe `más`, `mas`, `más ayuda`, `mas ayuda` o `detallalo`.\n"
            return

        yield "🧭 **Ayuda ampliada de JARVIS**\n\n"
        yield "Aqui tienes la guia completa, con ejemplos y diferencias entre funciones.\n\n"
        yield "## 💬 1. Chat general\n"
        yield "Usalo para preguntas normales o conversacion abierta.\n"
        yield "Ejemplos:\n"
        yield "- `hola`\n"
        yield "- `que puedes hacer`\n"
        yield "- `cuanto mide la torre Eiffel`\n\n"
        yield "## 📚 2. Documentos internos (RAG)\n"
        yield "Busca informacion en documentos indexados del sistema.\n"
        yield "Ejemplos:\n"
        yield "- `que dice la politica de calidad sobre los objetivos`\n"
        yield "- `busca en documentos internos IT-045 homologacion de proveedores`\n"
        yield "- `que informacion hay sobre el manual MAN-003`\n\n"
        yield "## 🔎 3. Buscar documentos por nombre o codigo\n"
        yield "Usalo cuando quieras localizar un archivo concreto sin hacer una pregunta RAG.\n"
        yield "Ejemplos:\n"
        yield "- `buscar documento IT-045`\n"
        yield "- `buscar documentos de calibracion`\n"
        yield "- `buscar en calidad homologacion proveedores`\n\n"
        yield "## 📋 4. Listar documentos\n"
        yield "Muestra los documentos que tienes disponibles para consulta.\n"
        yield "Ejemplos:\n"
        yield "- `docs`\n"
        yield "- `que documentos tienes`\n"
        yield "- `listar todos los documentos`\n"
        yield "- `documentos de calidad`\n\n"
        yield "## 🌐 5. Busqueda en internet\n"
        yield "Usalo cuando necesites informacion actualizada fuera del RAG.\n"
        yield "Ejemplos:\n"
        yield "- `busca en internet la web oficial de Airbus`\n"
        yield "- `busca en internet noticias de aviacion de hoy`\n"
        yield "- `busca en internet el sitio oficial de Qdrant`\n\n"
        yield "## 🏛️ 6. BOE y licitaciones\n"
        yield "Consulta normativa, anuncios y concursos publicos.\n"
        yield "Ejemplos:\n"
        yield "- `busca en el BOE la ley de contratos`\n"
        yield "- `noticias de hoy del BOE`\n"
        yield "- `busca licitaciones de seguridad en Madrid`\n"
        yield "- `hablame mas de la primera`\n\n"
        yield "## 🔎 7. Analizar una web\n"
        yield "Lee y resume una sola pagina sin indexarla globalmente.\n"
        yield "Ejemplos:\n"
        yield "- `analiza https://www.boe.es`\n"
        yield "- `resume esta web https://qdrant.tech`\n"
        yield "- `que dice esta pagina https://example.com`\n\n"
        yield "## 💾 8. Guardar una web en tu indice privado\n"
        yield "Guarda una URL para poder consultarla despues como parte de tus busquedas.\n"
        yield "Ejemplos:\n"
        yield "- `indexa https://example.com`\n"
        yield "- `guarda esta web https://example.com`\n"
        yield "- `anade esta url al rag https://example.com`\n\n"
        yield "## 🕸️ 9. Explorar una web completa\n"
        yield "Hace crawling recursivo y sigue enlaces internos de la web.\n"
        yield "Ejemplos:\n"
        yield "- `explora a fondo https://example.com`\n"
        yield "- `crawl https://example.com`\n"
        yield "- `recorre toda la web https://example.com`\n\n"
        yield "## ♻️ 10. Guardar una web ya analizada\n"
        yield "Si acabas de analizar una URL, puedes pedir que la guarde sin repetirla.\n"
        yield "Ejemplos:\n"
        yield "- `guarda esto`\n"
        yield "- `indexa esto`\n"
        yield "- `guarda el contenido`\n\n"
        yield "## 📎 11. Archivos subidos al chat\n"
        yield "Si subes un archivo, consultalo en ese mismo chat como si fuera un mini RAG privado del usuario.\n"
        yield "Como usarlo:\n"
        yield "1. Sube el archivo al chat.\n"
        yield "2. Pregunta en ese mismo chat: `que dice este documento`, `resumemelo`, `que exige este documento`.\n"
        yield "3. Haz preguntas de seguimiento: `y cada cuanto deben hacerse`, `y quien lo aprueba`, `y que excepciones hay`.\n"
        yield "Notas:\n"
        yield "- No se indexa globalmente.\n"
        yield "- No hace falta decir el nombre exacto del archivo si acabas de subirlo.\n"
        yield "- Si el chat pierde el contexto, vuelve a subir el archivo.\n\n"
        yield "## 🖼️ 12. Imagenes y OCR\n"
        yield "Analiza imagenes y extrae o traduce texto.\n"
        yield "Ejemplos:\n"
        yield "- `describe esta imagen`\n"
        yield "- `transcribe el texto`\n"
        yield "- `traduce lo que pone en la imagen`\n\n"
        yield "## 📊 13. Estado del sistema\n"
        yield "Comprueba salud del backend, SharePoint y la ingesta reciente.\n"
        yield "Ejemplos:\n"
        yield "- `estado`\n"
        yield "- `check status`\n"
        yield "- `estado del sistema`\n\n"
        yield "## 🗂️ 14. Donde se consulta cada cosa\n"
        yield "- Archivo subido al chat: en ese mismo chat, preguntando por `este documento` o haciendo follow-up.\n"
        yield "- Web analizada y no guardada: en ese mismo chat, preguntando por `esa web`, `esa pagina` o `que mas dice`.\n"
        yield "- Web privada indexada o crawleada: en JARVIS como parte de tus consultas RAG normales y tambien al listar documentos.\n"
        yield "Ejemplos para una web privada ya indexada:\n"
        yield "- `que informacion hay sobre Example Domains`\n"
        yield "- `busca en documentos internos Example Domains`\n"
        yield "- `que webs tienes`\n"
        yield "- `listar todos los documentos`\n\n"
        yield "## 🔐 15. Privacidad y permisos\n"
        yield "- Los archivos que subes al chat no se indexan globalmente y se consultan desde ese flujo de chat.\n"
        yield "- Las webs que indexas o crawleas se guardan en tu indice web privado.\n"
        yield "- Los documentos globales compartidos siguen estando disponibles para todos.\n\n"
        yield "## 🧠 16. Regla rapida para webs\n"
        yield "- `analiza URL` = leer ahora\n"
        yield "- `indexa URL` = guardar una pagina para ti\n"
        yield "- `explora a fondo URL` = recorrer varias paginas de ese sitio\n"


    def _handle_web_crawl(self, url: str, user_data: Dict) -> Generator:
        yield f"🕸️ **Iniciando Crawler en Profundidad**: {url}\n"
        yield "   *Modo recursivo: Navegando y siguiendo enlaces...*\n\n"
        try:
            write_headers = self._build_private_web_write_headers(user_data)
            target_collection = write_headers.get("X-Tenant-Id", "webs")
            response = requests.post(
                f"{self.valves.BACKEND_URL}/scrape/recursive",
                json={
                    "url": url, 
                    "max_depth": 2,
                    "max_pages": 5,
                    "tenant_id": target_collection
                },
                headers=write_headers,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            if data.get("status") in {"success", "processing"}:
                yield f"✅ **Crawler Iniciado Correctamente**\n"
                yield f"   🔗 **URL Base**: {data.get('base_url')}\n"
                yield f"   📄 **Páginas en cola**: {data.get('pages_initiated')} (aprox)\n\n"
                yield "⏳ El proceso se está ejecutando en segundo plano. Las páginas irán apareciendo en el índice RAG conforme se procesen.\n"
                yield "\n💡 *Puedes seguir usando el chat mientras tanto.*"
            else:
                yield f"⚠️ **Alerta**: {data.get('message')}\n"
        except Exception as e:
            logger.error(f"Error en crawler: {e}")
            yield f"❌ **Error al iniciar crawler**: {str(e)}\n"

    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: List[Dict],
        body: Dict
    ) -> Union[str, Generator, Iterator]:
        
        try:
            # Extraer datos del usuario
            user_data = body.get("user", {})
            
            # Detectar intención (pasamos messages para detectar imágenes)
            intent = self._detect_intent(user_message, body, messages)
            action = intent["action"]
            metadata = intent["metadata"]
            
            # --- CONTEXTUAL QUERY REWRITING ---
            # Solo reescribimos si es una acción de BÚSQUEDA (RAG, Web) y hay historial
            # Esto mejora drásticamente las preguntas de seguimiento ("¿Y de X?")
            effective_message = user_message
            if action in ["rag", "web_search", "web_crawl"] and messages and len(messages) > 1:
                # Verificar si parece una pregunta de seguimiento antes de gastar tokens
                # O simplemente reescribir siempre para asegurar (más robusto)
                if action == "rag" and self._has_explicit_document_reference(user_message):
                    logger.info("Skipping query rewrite due explicit document reference")
                else:
                    effective_message = self._rewrite_query(user_message, messages)
            
            if self.valves.DEBUG_MODE:
                logger.info(f"🎯 Usuario: {user_data.get('email', 'anonymous')}")
                logger.info(f"📨 Mensaje: {user_message[:100]}...")
                logger.info(f"🤖 Acción detectada: {action}")
            
            handlers = {
                "help": lambda: self._handle_help(bool(metadata.get("detailed"))),
                "web_crawl": lambda: self._handle_web_crawl(metadata["url"], user_data),
                "search_docs": lambda: self._handle_search_docs(user_data, metadata),
                "list_docs": lambda: self._handle_list_docs(user_data, metadata, user_message),
                "check_boe": lambda: self._handle_check_boe(metadata, effective_message, messages, user_message, user_data, body),
                "web_search": lambda: self._handle_web_search(effective_message, user_message, messages),
                "handle_url_decision": lambda: self._handle_url_decision(metadata, body, user_message, messages),
                "web_scrape": lambda: self._handle_web_scrape(metadata, user_message, body, messages),
                "web_index": lambda: self._handle_web_index(metadata, user_data),
                "web_followup": lambda: self._handle_web_followup(body, user_message, messages),
                "save_web_content": lambda: self._handle_save_web_content(body, user_data, messages),
                "ocr": lambda: self._handle_ocr(metadata, user_message, body, messages),
                "rag": lambda: self._handle_rag(effective_message, user_data, user_message, messages, body),
                "file_chat": lambda: self._handle_file_chat(messages, user_message, body),
                "check_status": lambda: self._handle_check_status(),
                "agent": lambda: self._handle_agent(user_message, user_data, messages, body),
                "chat": lambda: self._handle_chat(user_message, user_data, messages),
            }

            handler = handlers.get(action) or handlers["chat"]
            yield from handler()
            return

        except requests.exceptions.Timeout:
            logger.error("Timeout al comunicar con el backend")
            yield "\u23f1\ufe0f El sistema est\u00e1 tardando m\u00e1s de lo esperado. Por favor, intenta de nuevo."

        except requests.exceptions.HTTPError as e:
            logger.error(f"Error HTTP del backend: {e}")
            yield f"\u274c Error del servidor: {e.response.status_code}\n{e.response.text}"

        except Exception as e:
            logger.error(f"Error inesperado: {e}", exc_info=True)
            yield f"❌ Error inesperado: {str(e)}\n\nPor favor, contacta al administrador del sistema."
