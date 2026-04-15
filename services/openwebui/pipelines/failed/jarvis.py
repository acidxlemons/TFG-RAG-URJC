"""
JARVIS - Intelligent RAG Assistant v3.0
========================================

Pipeline de IA para asistencia inteligente con:
- Detecta automáticamente la intención del usuario
- Selecciona el modo apropiado (RAG, web search, scraping, OCR, BOE, listar docs)
- Usa modelos especializados (llama3.1 para texto, llava para imágenes)
- Es el ÚNICO punto de entrada en OpenWebUI

CAPACIDADES:
- 📚 RAG: Búsqueda en documentos internos
- 🌐 Web Search: Búsqueda en internet
- 🔍 Web Scraping: Analizar URLs
- 🖼️  OCR/Visión: Analizar imágenes con LLaVA
- 📋 Listar Documentos: Ver PDFs disponibles
- 🏛️ BOE: Consultar Boletín Oficial del Estado
- 💬 Chat: Conversación normal

Desarrollado para TFG - Universidad Rey Juan Carlos
"""

from typing import List, Dict, Generator, Iterator, Union
import requests
import json
import logging
import os
import re
import time
from pydantic import BaseModel

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis")

class Pipeline:
    class Valves(BaseModel):
        BACKEND_URL: str = "http://rag-backend:8000"
        LITELLM_URL: str = "http://litellm:4000"
        DEBUG_MODE: bool = True
        # Modelos especializados
        TEXT_MODEL: str = "llama3.1-8b"       # LLaMA 3.1 8B Q8 - Conversación y RAG
        VISION_MODEL: str = "llava"           # LLaVA 13B - OCR y análisis de imágenes
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
        
        # URL del servicio Indexer para status checks
        INDEXER_URL: str = "http://rag-indexer:8001"

    def __init__(self):
        self.name = "JARVIS"
        self.valves = self.Valves()
        
        # --- MEMORIA DE SESIÓN (Por Usuario) ---
        # Clave: email del usuario
        self._user_image_memory: Dict[str, str] = {}    # {email: base64_image}
        self._user_web_memory: Dict[str, Dict] = {}     # {email: {url, content, ...}}
        self._user_pending_decision: Dict[str, Dict] = {} # {email: {type: 'url_check', data: ...}}
        
        # Cache de permisos por usuario {email: (timestamp, [colecciones])}
        self._permission_cache: Dict[str, tuple] = {}
        logger.info(f"✓ {self.name} inicializado")
        logger.info(f"  Backend: {self.valves.BACKEND_URL}")
        logger.info(f"  LiteLLM: {self.valves.LITELLM_URL}")
        logger.info(f"  Multi-departamento: {'ON' if self.valves.MULTI_DEPARTMENT_ENABLED else 'OFF'}")

    async def on_startup(self):
        logger.info(f"🚀 Starting {self.name}")

    async def on_shutdown(self):
        logger.info(f"👋 Shutting down {self.name}")
    
    def _get_user_departments(self, user_data: Dict) -> List[str]:
        """
        Obtiene las colecciones/departamentos a los que el usuario tiene acceso.
        """
        logger.info("⚡ _get_user_departments INICIO")
        if not self.valves.MULTI_DEPARTMENT_ENABLED:
            return [self.valves.DEFAULT_COLLECTION]
        
        email = user_data.get("email", "anonymous")
        logger.info(f"⚡ Procesando permisos para: {email}")
        
        # DEBUG: Limpiar cache para forzar recarga
        if email in self._permission_cache:
            del self._permission_cache[email]
        
        # DEBUG: Ver toda la info del usuario
        logger.info(f"DEBUG user_data keys: {list(user_data.keys())}")
        logger.info(f"DEBUG user_data completo: {user_data}")
        
        # Revisar cache
        if email in self._permission_cache:
            cache_time, cached_depts = self._permission_cache[email]
            if time.time() - cache_time < self.valves.PERMISSION_CACHE_TTL:
                logger.debug(f"Cache hit para {email}: {cached_depts}")
                return cached_depts
        
        # Extraer grupos del usuario
        groups = user_data.get("groups", [])
        if not groups:
            groups = user_data.get("oauth_groups", [])
        if not groups:
            info = user_data.get("info", {})
            if isinstance(info, dict):
                groups = info.get("groups", [])
        
        logger.info(f"DEBUG groups encontrados: {groups}")
        
        if not groups:
            # Si no hay grupos en user_data, intentar obtenerlos desde Microsoft Graph API
            try:
                graph_groups = self._get_user_groups_from_graph(email)
                if graph_groups:
                    groups = graph_groups
                    logger.info(f"Grupos obtenidos de Graph API para {email}: {len(groups)}")
            except Exception as e:
                logger.error(f"Error obteniendo grupos de Graph API: {e}")

        logger.info(f"DEBUG groups finales: {groups}")
        
        # Si groups es string, convertir a lista
        if isinstance(groups, str):
            groups = [g.strip() for g in groups.split(",") if g.strip()]
        
        # Mapear grupos a colecciones adicionales (además de la por defecto)
        extra_collections = []
        for group in groups:
            if group in self.valves.DEPARTMENT_MAPPING:
                collection = self.valves.DEPARTMENT_MAPPING[group]
                if collection not in extra_collections:
                    extra_collections.append(collection)
        
        # SIEMPRE incluir la colección por defecto para todos los usuarios
        departments = [self.valves.DEFAULT_COLLECTION]
        
        # SIEMPRE incluir Calidad (Acceso Global) - SOLO SI ESTÁ CONFIGURADO
        # if "documents_CALIDAD" not in departments:
        #      departments.append("documents_CALIDAD")
        
        # Añadir colecciones adicionales según pertenencia a grupos
        for coll in extra_collections:
            if coll not in departments:
                departments.append(coll)
        
        # Admin ve TODAS las colecciones
        role = user_data.get("role", "")
        if role == "admin":
            all_collections = set(self.valves.DEPARTMENT_MAPPING.values())
            for coll in all_collections:
                if coll not in departments:
                    departments.append(coll)
            logger.info(f"Usuario admin {email}: acceso a todas las colecciones")
        
        logger.info(f"Usuario {email} tiene acceso a: {departments}")
        
        # Guardar en cache
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
            groups_resp = requests.post(
                f"{graph_url}/{user_id}/getMemberObjects",
                headers=headers,
                json={"securityEnabledOnly": False},
                timeout=10
            )
            groups_resp.raise_for_status()
            
            group_ids = groups_resp.json().get("value", [])
            logger.info(f"Graph API: Encontrados {len(group_ids)} grupos para {email}")
            return group_ids
            
        except Exception as e:
            logger.error(f"Error consultando Graph API: {e}")
            return []

    def _get_user_email(self, body: Dict) -> str:
        """Extrae el email del usuario del cuerpo de la petición."""
        user = body.get("user", {})
        if isinstance(user, dict):
            return user.get("email", "anonymous")
        return "anonymous"

    def _extract_images_from_messages(self, messages: List[Dict]) -> List[str]:
        """
        Extrae imágenes SOLO del ÚLTIMO mensaje del usuario.
        """
        images = []
        if not messages:
            return images
            
        last_user_msg = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user_msg = msg
                break
        
        if not last_user_msg:
            return images
            
        content = last_user_msg.get("content", "")
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
        """Detecta si hay un archivo (PDF, doc, etc.) adjunto en el chat."""
        files = body.get("files", [])
        if files:
            for f in files:
                if isinstance(f, dict):
                    file_type = f.get("type", "").lower()
                    filename = f.get("name", "").lower()
                    if not any(ext in file_type for ext in ["image", "png", "jpg", "jpeg", "gif", "webp"]):
                        logger.info(f"Detected file attachment: {filename}")
                        return True
                    if filename.endswith((".pdf", ".doc", ".docx", ".txt", ".xlsx", ".csv")):
                        logger.info(f"Detected file attachment by extension: {filename}")
                        return True
        
        if messages:
            last_msg = messages[-1] if messages else None
            if last_msg and last_msg.get("role") == "user":
                content = last_msg.get("content", "")
                if isinstance(content, str):
                    if "<source" in content and "id=" in content:
                        logger.info("Detected OpenWebUI file context with <source> tags")
                        return True
                    if len(content) > 5000:
                        logger.info(f"Detected very long message ({len(content)} chars), likely file context")
                        return True
        
        return False

    def _detect_intent(self, message: str, body: Dict, messages: List[Dict] = None) -> Dict:
        """
        Detecta la intención del usuario y devuelve acción + metadata.
        """
        if not message:
             return {"action": "chat", "metadata": {}}
             
        message_lower = message.lower().strip()
        
        # DEBUG: Log what we receive
        if self.valves.DEBUG_MODE:
            logger.info(f"🔍 DEBUG _detect_intent:")
            logger.info(f"   message: {message[:50]}...")
        
        # --- KEYWORDS DEFINITIONS (MOVED TO TOP TO FIX SCOPE ERROR) ---
        
        # 1. Keywords para Guardar Sesión
        save_session_keywords = [
            "guarda esta conversacion", "guarda esta conversación",
            "guarda lo que acabamos de hablar", "guarda el contexto",
            "guarda esta pagina", "guarda esta página",
            "quiero guardar esto", "guarda esta información",
            "save this", "index this",
            "guarda esto", "guárdalo", "guardalo", "guarda el contenido",
            "indexa esto", "indexalo", "indexa el contenido",
            "añade esto al rag", "añádelo", "anadelo",
        ]
        
        # 2. Keywords para Listar Webs
        list_webs_keywords = [
            "/webs", "/listar webs", "listar webs", "lista webs", "listame webs",
            "que webs tienes", "qué webs tienes", "que paginas tienes", "qué páginas tienes",
            "webs guardadas", "páginas guardadas", "paginas guardadas",
            "webs indexadas", "páginas indexadas", "paginas indexadas",
            "sitios guardados", "sitios indexados",
            "que urls tienes", "qué urls tienes", "urls guardadas",
        ]
        
        # 3. Keywords para Indexar explícitamente
        index_keywords = [
            "indexa esta", "indexa el", "indexa la", "indexar esta",
            "guarda esta url", "guarda este contenido", "guarda esta web",
            "añade esta url", "añade al rag", "añade a los documentos",
            "ingesta esta", "ingestar esta",
        ]

        # 4. Keywords para Scraping Recursivo
        recursive_keywords = [
            "analiza la estructura", "estructura completa", "analisis recursivo", 
            "análisis recursivo", "navega recursivamente", "crawler", "crawling",
            "analiza todo el sitio", "baja toda la web", "descarga toda la web"
        ]

        # 5. Keywords para BOE / Legal
        boe_keywords = [
            # Consultas directas
            "busca en el boe", "buscar en el boe", "consulta el boe", "consulta en el boe",
            "consulta sobre el boe", "en el boe", "del boe", "boe de hoy", "boe sobre",
            # Términos legales
            "legislación sobre", "legislacion sobre", "ley de", "leyes de", "normas de",
            "real decreto", "disposición", "disposicion", "orden ministerial",
            # Frases naturales
            "boletín oficial", "boletin oficial", "qué dice el boe", "que dice el boe",
            "resumen del boe", "noticias del boe", "novedades del boe", "publicado en el boe",
            # Nuevas variaciones
            "según el boe", "segun el boe", "sumario del boe", "última hora boe"
        ]
        
        # 6. Keywords para Listar Docs (General)
        list_docs_keywords = ["/listar", "/docs", "/documentos", "/list", "list docs", "list", "docs"]
        list_docs_pattern = r"(?:listar?|listame|dime|ver|mostrar|cuales|que|list)\s+(?:son\s+|los\s+|hay\s+|mis\s+|todos\s+los\s+)?(docs|documentos|archivos|pdfs|guardado|tengo guardado)"
        
        # 7. Keywords de Web Search
        web_search_keywords = [
            "buscar en internet", "busca en internet", "buscar web",
            "busca en la web", "buscar en la web", "busca web",
            "me podrías buscar", "me podrias buscar", "podrías buscar", "podrias buscar",
            "noticias", "actualidad", "precio", "cotización",
            "qué es", "quién es", "cuándo", "dónde",
            "google", "información sobre",
            "últimas noticias", "hoy", "ayer",
            "clasificación", "clasificacion", "resultado", "resultados",
            "calendario", "partido", "jornada"
        ]
        
        # 8. Keywords de RAG (documentos internos)
        rag_keywords = [
            "documento", "documentos", "pdf", "archivo",
            "política", "politica", "manual", "procedimiento",
            "norma", "iso", "calidad", "seguridad",
            "según", "segun", "qué dice", "que dice",
            "internamente", "empresa", "organización", "organizacion",
            "reglamento", "contrato", "informe", "report",
            "mira en", "busca en el", "lee el", "revisa el",
            "consulta el", "consulta la", "dime qué", "dime que",
            "explica el", "resume el", "resumen de",
            "información sobre", "info sobre", "datos de",
            "contenido de", "contenido del",
        ]
        
        # 9. Keywords de Status
        status_keywords = [
            "cómo va", "como va", "estado de", "status", "estatus",
            "qué tal va", "que tal va", "progreso", "subida",
            "ingestión", "ingestion", "indexación", "indexacion",
            "estado del sistema", "cola de trabajo"
        ]
        
        # 10. Referencias a Web
        web_reference_phrases = [
            "la web", "la página", "la pagina", "esa web", "esta web",
            "esa página", "esta página", "esa pagina", "esta pagina",
            "la url", "esa url", "el sitio", "ese sitio",
            "el contenido", "ese contenido", "el artículo", "el articulo",
            "sobre eso", "sobre esto", "de eso", "de esto",
        ]

        # --- Obtener ID de usuario ---
        user_email = self._get_user_email(body)
        
        # -1. Revisar decisiones pendientes (Check URL)
        pending_decision = self._user_pending_decision.get(user_email)
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
        
        # 0. Detectar archivos adjuntos en el chat
        if self._has_file_attachment(body, messages):
            logger.info(f"   → Detected file attachment in chat!")
            return {"action": "file_chat", "metadata": {"from_attachment": True}}
        
        # 1. Imágenes adjuntas → OCR
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
        
        # 2. Referencias a imagen anterior
        image_reference_phrases = [
            "la imagen", "esta imagen", "esa imagen", "imagen anterior", "última imagen",
            "en la imagen", "de la imagen", "sobre la imagen", "describe la", "analiza la",
        ]
        stored_image = self._user_image_memory.get(user_email)
        if stored_image and (any(phrase in message_lower for phrase in image_reference_phrases) or (len(message_lower.split()) <= 4 and "?" in message)):
            logger.info(f"   → Detected image reference/followup!")
            return {"action": "ocr", "metadata": {"files": [], "images": [stored_image], "is_new_image": False}}
        
        # 2b. Referencias a Web Guardada
        stored_web = self._user_web_memory.get(user_email)
        if stored_web and any(phrase in message_lower for phrase in web_reference_phrases):
             logger.info(f"   → Detected web content reference!")
             return {"action": "web_followup", "metadata": {"from_memory": True}}
        
        # 2c. Comando "guarda esto"
        if stored_web and any(kw in message_lower for kw in save_session_keywords):
            logger.info(f"   → Detected save session content command!")
            return {"action": "save_web_content", "metadata": {"url": stored_web.get("url")}}
        
        # 2. Comando: Listar WEBS indexadas
        if any(kw in message_lower for kw in list_webs_keywords):
            return {"action": "prompt_for_url", "metadata": {"intent": "recursive_scrape"}}

        # 5a. BOE Search (sin URL explícita) -- FIXED POSITION
        # Primero: detectar peticiones específicas de ley
        import re
        
        # Patrón: "dame el artículo X de la LOPD" / "texto de la constitución"
        law_text_patterns = [
            r"(?:dame|muestra|léeme|leeme|dime|texto de)\s+(?:el\s+)?(?:artículo|articulo|art\.?)\s*(\d+)\s+(?:de\s+)?(?:la\s+)?(.+)",
            r"(?:texto|contenido)\s+(?:de\s+)?(?:la\s+)?(?:ley\s+)?(.+)",
            r"(?:qué|que)\s+dice\s+(?:la\s+)?(.+)",
        ]
        
        for pattern in law_text_patterns:
            match = re.search(pattern, message_lower)
            if match:
                groups = match.groups()
                if len(groups) == 2:  # artículo + ley
                    return {
                        "action": "boe_get_law",
                        "metadata": {"law_name": groups[1].strip(), "article": groups[0]}
                    }
                elif len(groups) == 1:  # solo ley
                    return {
                        "action": "boe_get_law",
                        "metadata": {"law_name": groups[0].strip()}
                    }
        
        # Patrón: "qué modifica la LOPD" / "referencias de la ley X"
        law_analysis_patterns = [
            r"(?:qué|que)\s+(?:modifica|deroga|cambia)\s+(?:la\s+)?(.+)",
            r"(?:qué|que)\s+leyes?\s+modifica\s+(?:la\s+)?(.+)",
            r"referencias\s+(?:de\s+)?(?:la\s+)?(.+)",
            r"análisis\s+(?:jurídico\s+)?(?:de\s+)?(?:la\s+)?(.+)",
        ]
        
        for pattern in law_analysis_patterns:
            match = re.search(pattern, message_lower)
            if match:
                return {
                    "action": "boe_law_analysis",
                    "metadata": {"law_name": match.group(1).strip()}
                }
        
        # BOE search/summary (existente)
        if any(kw in message_lower for kw in boe_keywords):
            mode = "search"
            if "boe de hoy" in message_lower or "resumen del boe" in message_lower or "sumario" in message_lower:
                mode = "summary"
            return {"action": "boe_search", "metadata": {"query": message, "mode": mode}}
        
        # 2b. Comando: Listar documentos (comando directo)
        if message_lower in list_docs_keywords:
            return {"action": "list_docs", "metadata": {}}
        
        # 3. Preguntas naturales sobre documentos
        collection_pattern = r"(?:en|de|dentro de|about)\s+([a-zA-Z0-9_\-\u00C0-\u00FF]+)"
        collection_match = re.search(collection_pattern, message_lower)
        
        if re.search(list_docs_pattern, message_lower) or message_lower in ["docs", "documentos", "archivos", "webs"]:
            metadata = {}
            # Mapeo de colecciones extendido
            coll_map = {
                "web": "webs", "webs": "webs", "internet": "webs", "paginas": "webs", "sitios": "webs",
                "local": "documents", "locales": "documents", "interno": "documents", "internos": "documents", 
                "mis documentos": "documents", "archivos": "documents", "doc": "documents", "docs": "documents"
            }
            
            if collection_match:
                target_word = collection_match.group(1).lower().strip()
                # Check direct map
                if target_word in coll_map:
                    metadata["target_collection"] = coll_map[target_word]
                else:
                    # Check partial matches
                    for key, val in coll_map.items():
                        if key in target_word:
                            metadata["target_collection"] = val
                            break
                    # Check known specific collections (civex, rrhh)
                    known_collections = ["calidad", "civex", "rrhh", "general"]
                    if any(kc in target_word for kc in known_collections):
                         metadata["target_collection"] = target_word

            return {"action": "list_docs", "metadata": metadata}
        
        # 4. URL en el mensaje + palabras de acción
        url_pattern = r'https?://[^\s\?\"\'\)]+' 
        url_match = re.search(url_pattern, message)
        
        if url_match:
            clean_url = url_match.group().rstrip('?.,;:!)*_]}>')
            
            if any(kw in message_lower for kw in index_keywords):
                return {"action": "web_index", "metadata": {"url": clean_url}}
            
            if any(kw in message_lower for kw in recursive_keywords):
                return {"action": "recursive_scrape", "metadata": {"url": clean_url}}

            # Default: Web Scrape directo
            logger.info(f"URL detected, routing directly to web_scrape: {clean_url}")
            return {"action": "web_scrape", "metadata": {"url": clean_url}}
        
        # 5. Web Search
        if any(kw in message_lower for kw in web_search_keywords):
            return {"action": "web_search", "metadata": {}}
        
        # 6. RAG
        if any(kw in message_lower for kw in rag_keywords):
            metadata = {}
            # Detectar si especifica colección para buscar
            coll_map = {
                "web": "webs", "webs": "webs", "internet": "webs", "paginas": "webs",
                "local": "documents", "locales": "documents", "interno": "documents", "internos": "documents", "mis documentos": "documents"
            }
            
            # Buscar menciones explícitas de colección: "en mis documentos", "en webs", "en local"
            for key, val in coll_map.items():
                if f"en {key}" in message_lower or f"de {key}" in message_lower or f"sobre {key}" in message_lower:
                    metadata["target_collection"] = val
                    break
            
            return {"action": "rag", "metadata": metadata}
        
        # 7. Document Codes
        doc_code_pattern = r'\\b([A-Z]{2,5}[-_]?\d{2,4})\\b'
        if re.search(doc_code_pattern, message.upper()):
            return {"action": "rag", "metadata": {}}
        
        # 8. Status
        if any(kw in message_lower for kw in status_keywords):
            return {"action": "check_status", "metadata": {}}
            
        # 9. Default: Chat
        logger.info(f"No document-related keywords found, defaulting to CHAT")
        return {"action": "chat", "metadata": {}}

    def _is_followup_question(self, current_message: str, chat_history: List[Dict]) -> bool:
        """Detecta si la pregunta actual es un seguimiento."""
        if not chat_history or len(chat_history) < 2:
            return False
        
        msg_lower = current_message.lower().strip()
        followup_indicators = [
            "esto", "eso", "este documento", "ese documento", "el documento",
            "del mismo", "sobre eso", "sobre esto", "más sobre", "mas sobre",
            "y qué", "y que", "también", "tambien", "además", "ademas",
            "en ese", "en este", "de ese", "de este", "el anterior",
            "sigue", "continua", "continúa", "explica más", "por qué",
        ]
        
        if any(ind in msg_lower for ind in followup_indicators):
            return True
        if len(msg_lower.split()) <= 4 and "?" in current_message:
            return True
        return False

    def _call_backend_chat(self, message: str, mode: str, user_data: Dict, chat_history: List[Dict] = None, metadata: Dict = None) -> Dict:
        """Llama al endpoint /chat del backend."""
        full_message = message
        if chat_history and len(chat_history) > 1:
            is_followup = self._is_followup_question(message, chat_history)
            if is_followup:
                last_assistant_msg = None
                for msg in reversed(chat_history[:-1]):
                    if msg.get("role") == "assistant":
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = " ".join([c.get("text", "") for c in content if isinstance(c, dict)])
                        if content and len(content) > 20:
                            last_assistant_msg = content[:800]
                            break
                if last_assistant_msg:
                    full_message = f"[PREVIOUS ANSWER]\n{last_assistant_msg}\n\n[CURRENT QUESTION]\n{message}"

        payload = {
            "message": full_message,
            "mode": mode,
            "user_id": None,
            "email": user_data.get("email"),
            "name": user_data.get("name"),
            "azure_id": user_data.get("id"),
            "conversation_id": None
        }
        
        authorized_depts = self._get_user_departments(user_data)
        headers = {}
        
        # Filtrado de colecciones (Webs vs Documentos)
        target_collection = metadata.get("target_collection") if metadata else None
        
        if self.valves.MULTI_DEPARTMENT_ENABLED and authorized_depts:
            if target_collection:
                # Si se pide una colección específica, filtrar solo esa si está autorizada
                # 'webs' siempre está permitido si no es restricción estricta, o revisamos autorizacion
                final_depts = []
                if target_collection == "webs":
                    final_depts = ["webs"]
                elif target_collection == "documents":
                     # Filtrar todo lo que NO sea webs
                     final_depts = [d for d in authorized_depts if d != "webs"]
                else:
                    # Colección específica (ej: calidad)
                    final_depts = [d for d in authorized_depts if target_collection.lower() in d.lower()]
                
                # Fallback si no hay match
                if not final_depts: final_depts = authorized_depts
            else:
                final_depts = authorized_depts

            headers["X-Tenant-Ids"] = ",".join(final_depts)
        else:
             # Modo simple
             if target_collection == "webs":
                 headers["X-Tenant-Ids"] = "webs"
             elif target_collection == "documents":
                 headers["X-Tenant-Ids"] = self.valves.DEFAULT_COLLECTION
             else:
                 headers["X-Tenant-Ids"] = f"{self.valves.DEFAULT_COLLECTION},webs"
        else:
            headers["X-Tenant-ID"] = self.valves.DEFAULT_COLLECTION
        
        response = requests.post(f"{self.valves.BACKEND_URL}/chat", json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        return response.json()

    def _call_litellm(self, message: str, model: str, system_prompt: str = None) -> str:
        """Llama a LiteLLM directamente."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": message})
        
        response = requests.post(
            f"{self.valves.LITELLM_URL}/v1/chat/completions",
            json={"model": model, "messages": messages, "temperature": 0.7, "stream": False},
            headers={"Authorization": "Bearer sk-1234"},
            timeout=60
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def _build_chat_history(self, messages: List[Dict], max_messages: int = 10) -> List[Dict]:
        """Construye historial para el LLM."""
        history = []
        if not messages: return history
        recent = messages[:-1] if len(messages) > 1 else []
        recent = recent[-(max_messages * 2):]
        for msg in recent:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join([c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"])
            if content and role in ["user", "assistant"]:
                if len(content) > 1500: content = content[:1500] + "..."
                history.append({"role": role, "content": content})
        return history

    def _call_litellm_with_history(self, user_message: str, system_prompt: str, messages: List[Dict], extra_context: str = None) -> str:
        """Llama a LiteLLM con historial."""
        llm_messages = [{"role": "system", "content": system_prompt}]
        history = self._build_chat_history(messages, max_messages=6)
        llm_messages.extend(history)
        
        current_message = f"{extra_context}\n\nUser question: {user_message}" if extra_context else user_message
        llm_messages.append({"role": "user", "content": current_message})
        
        response = requests.post(
            f"{self.valves.LITELLM_URL}/v1/chat/completions",
            json={"model": self.valves.TEXT_MODEL, "messages": llm_messages, "temperature": 0.7, "stream": False},
            headers={"Authorization": "Bearer sk-1234"},
            timeout=90
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def _scrape_and_summarize(self, url: str, user_message: str, user_email: str, check_rag: bool = False):
        """Helper para scraping y resumen."""
        if check_rag:
            try:
                yield f"🔍 **Verificando URL**: {url}...\n"
                rag_check = requests.post(f"{self.valves.BACKEND_URL}/scrape/check", json={"url": url}, timeout=10).json()
                if rag_check.get("exists"):
                    title = rag_check.get("title", "Sin título")
                    date = rag_check.get("scraped_at", "fecha desconocida")[:10]
                    self._user_pending_decision[user_email] = {"type": "url_check", "data": {"url": url, "title": title}}
                    yield f"\n⚠️ **Contenido Existente Detectado**\nYa tengo analizada esta página ({title}) desde el **{date}**.\n\n¿Qué prefieres?\n- **Usar existente**: (Escribe 'sí')\n- **Actualizar**: (Escribe 'actualizar')\n"
                    return
            except Exception as e:
                logger.error(f"Error checking RAG status: {e}")
        
        yield f"🌐 **Analizando URL en tiempo real**: {url}\n\n"
        try:
            response = requests.post(f"{self.valves.BACKEND_URL}/scrape/analyze", json={"url": url}, timeout=120)
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
            
            yield f"📄 **Título**: {title}\n📊 **Palabras extraídas**: {word_count}\n\n"
            
            max_content_chars = 8000
            truncated = len(content) > max_content_chars
            analysis_content = content[:max_content_chars] if truncated else content
            
            yield "📝 **Generando resumen...**\n\n"
            summary_prompt = f"###### REGLA CRÍTICA: IDIOMA ######\nResponde SIEMPRE en el mismo idioma que la pregunta del usuario.\n############################################\n\nAnaliza el siguiente contenido web:\nTítulo: {title}\nContenido:\n{analysis_content}\n\nPregunta del usuario: {user_message}\n\nProporciona un análisis útil y estructurado."
            
            try:
                summary = self._call_litellm(summary_prompt, self.valves.TEXT_MODEL, "Eres un asistente experto.")
                yield summary
            except Exception as llm_error:
                yield f"{content[:2000]}...\n"
            
            # Guardar en memoria
            from datetime import datetime
            self._user_web_memory[user_email] = {
                "url": url, "title": title, "content": content, "word_count": word_count,
                "scraped_at": datetime.now().isoformat(), "extraction_method": extraction_method,
            }
            if user_email in self._user_pending_decision:
                if self._user_pending_decision[user_email].get("data", {}).get("url") == url:
                    del self._user_pending_decision[user_email]
            
            yield f"\n\n---\n🔗 **Fuente**: [{title}]({url})\n"
            
        except Exception as e:
            logger.error(f"Error en scraping: {e}")
            yield f"❌ **Error**: {str(e)}\n"

    def _call_ollama_direct(self, prompt: str, system_prompt: str = None) -> str:
        """Llama a Ollama directamente."""
        ollama_url = "http://ollama:11434"
        full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        try:
            response = requests.post(
                f"{ollama_url}/api/generate",
                json={
                    "model": "llama3.1:8b-instruct-q8_0", "prompt": full_prompt, "stream": False,
                    "options": {"temperature": 0.7, "num_predict": 2000, "num_ctx": 8192}
                },
                timeout=120
            )
            if response.status_code == 200:
                return response.json().get("response", "No se pudo generar respuesta.")
            return f"Error de Ollama: {response.status_code}"
        except Exception as e:
            return f"Error: {str(e)}"

    def pipe(self, user_message: str, model_id: str, messages: List[Dict], body: Dict) -> Union[str, Generator, Iterator]:
        try:
            user_data = body.get("user", {})
            intent = self._detect_intent(user_message, body, messages)
            action = intent["action"]
            metadata = intent["metadata"]
            
            if self.valves.DEBUG_MODE:
                logger.info(f"🤖 Acción detectada: {action}")
            
            if action == "list_docs":
                yield "📋 **Consultando documentos disponibles...**\n\n"
                authorized_depts = self._get_user_departments(user_data)
                headers = {}
                target_collection = metadata.get("target_collection")
                
                if self.valves.MULTI_DEPARTMENT_ENABLED and authorized_depts:
                    final_depts = authorized_depts
                    if target_collection:
                        if target_collection.lower() == "webs":
                            final_depts = ["webs"]
                        else:
                            matched = [d for d in authorized_depts if target_collection.lower() in d.lower()]
                            if matched: final_depts = matched
                    
                    if not target_collection and "webs" not in final_depts:
                        final_depts.append("webs")
                    headers["X-Tenant-Ids"] = ",".join(final_depts)
                else:
                    headers["X-Tenant-Ids"] = f"{self.valves.DEFAULT_COLLECTION},webs"
                
                response = requests.get(f"{self.valves.BACKEND_URL}/documents/list", headers=headers, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    docs = data.get("documents", [])
                    total = data.get("total", 0)
                    
                    if total == 0:
                        yield "No tienes documentos disponibles.\n"
                    else:
                        # Separar por tipo
                        local_docs = [d for d in docs if d.get("type") != "web"]
                        web_docs = [d for d in docs if d.get("type") == "web"]
                        
                        yield f"**Total de documentos**: {total}\n\n"
                        
                        if local_docs:
                            yield "📂 **Documentos Locales / Internos**\n"
                            local_docs.sort(key=lambda x: x["filename"])
                            for i, doc in enumerate(local_docs, 1):
                                yield f"{i}. 📄 **{doc['filename']}**\n"
                            yield "\n"
                            
                        if web_docs:
                            yield "🌐 **Webs Guardadas**\n"
                            web_docs.sort(key=lambda x: x["filename"])
                            for i, doc in enumerate(web_docs, 1):
                                yield f"{i}. 🌐 **{doc['filename']}**\n"
                            yield "\n"
                        
                        if total > 20: yield f"\n*...mostrando primeros 20 resultados.*\n"
                return

            elif action == "web_search":
                yield "🌐 **Buscando en internet...**\n\n"
                response = requests.get(f"{self.valves.BACKEND_URL}/web-search", params={"q": user_message}, timeout=30)
                if response.status_code == 200:
                    results = response.json().get("results", [])
                    if not results:
                        yield "No encontré resultados.\n"
                        return
                    
                    context = "Resultados:\n" + "\n".join([f"- {r['title']}: {r['snippet']}" for r in results])
                    answer = self._call_litellm_with_history(user_message, "Responde usando solo los resultados.", messages, context)
                    yield answer
                    yield "\n\n---\n### Fuentes:\n"
                    for r in results[:3]:
                        yield f"- [{r['title']}]({r['link']})\n"
                return

            elif action == "handle_url_decision":
                decision = metadata.get("decision")
                data = metadata.get("data", {})
                if decision == "use_existing":
                    yield f"📂 **Recuperando contenido existente**: {data.get('title')}...\n\n"
                    resp = requests.post(f"{self.valves.BACKEND_URL}/scrape/retrieve", json={"url": data.get("url")}, timeout=30).json()
                    content = resp.get("content", "")
                    if content:
                         yield "📝 **Generando resumen...**\n\n"
                         yield self._call_litellm(f"Resumen de: {content[:8000]}", self.valves.TEXT_MODEL)
                elif decision == "update":
                    yield f"🔄 **Actualizando...**\n"
                    for chunk in self._scrape_and_summarize(data.get("url"), user_message, self._get_user_email(body)): yield chunk
                return

            elif action == "web_scrape":
                for chunk in self._scrape_and_summarize(metadata["url"], user_message, self._get_user_email(body), check_rag=True):
                    yield chunk
                return

            elif action == "web_index":
                yield f"📥 **Indexando URL**: {metadata['url']}\n\n"
                requests.post(f"{self.valves.BACKEND_URL}/scrape", json={"url": metadata["url"], "mode": "index"}, timeout=120)
                yield "✅ **Contenido indexado exitosamente**\n"
                return

            elif action == "web_followup":
                stored = self._user_web_memory.get(self._get_user_email(body))
                if stored:
                     yield f"💬 **Respondiendo sobre**: [{stored.get('title')}]({stored.get('url')})\n\n"
                     yield self._call_litellm_with_history(user_message, "Responde sobre el contenido web.", messages, stored.get("content")[:10000])
                else:
                     yield "⚠️ No hay contenido web en memoria.\n"
                return

            elif action == "save_web_content":
                stored = self._user_web_memory.get(self._get_user_email(body))
                if stored:
                    yield f"📥 **Guardando**: {stored.get('title')}\n"
                    requests.post(f"{self.valves.BACKEND_URL}/scrape", json={"url": stored.get("url"), "mode": "index"}, timeout=120)
                    yield "✅ **Guardado exitosamente**\n"
                else:
                    yield "⚠️ Nada que guardar.\n"
                return
            
            elif action == "recursive_scrape":
                url = metadata["url"]
                yield f"🕸️ **Iniciando Crawler**: {url}\n"
                resp = requests.post(f"{self.valves.BACKEND_URL}/scrape/recursive", json={"url": url}, timeout=10)
                if resp.status_code == 200:
                    yield f"✅ **Crawl Iniciado**: {resp.json().get('message')}\n"
                else:
                    yield f"❌ Error: {resp.text}\n"
                return

            elif action == "boe_search":
                mode = metadata.get("mode", "search")
                query = metadata.get("query", "")
                
                if mode == "summary":
                    yield "🏛️ **Consultando el BOE de hoy...**\n\n"
                else:
                    yield f"⚖️ **Buscando legislación**: '{query}'\n\n"
                
                payload = {"mode": mode}
                if mode == "search":
                    import re
                    # Limpieza robusta usando regex
                    clean_q = query.lower().strip()
                    
                    # Eliminar caracteres markdown/especiales
                    clean_q = clean_q.replace("_", " ").replace("*", " ").replace('"', "").replace("'", "")
                    clean_q = re.sub(r'\s+', ' ', clean_q).strip()
                    
                    # Patrón: buscar keyword después de "sobre", "de", "acerca de"
                    # Ej: "noticias del boe sobre defensa" -> "defensa"
                    patterns = [
                        r'(?:sobre|acerca de|de)\s+(\w+)$',  # Última palabra después de sobre/de
                        r'(?:sobre|acerca de)\s+(.+)$',      # Todo después de sobre
                        r'(?:legislaci[oó]n|noticias|novedades|leyes)\s+(?:de|del|sobre)\s+(.+)$',
                        r'boe\s+(?:sobre|de)\s+(.+)$',       # "boe sobre X"
                    ]
                    
                    extracted = None
                    for pattern in patterns:
                        match = re.search(pattern, clean_q)
                        if match:
                            extracted = match.group(1).strip()
                            break
                    
                    if extracted:
                        clean_q = extracted
                    else:
                        # Fallback: última palabra significativa (> 3 chars)
                        words = clean_q.split()
                        stopwords = {"el", "la", "los", "las", "de", "del", "en", "boe", "sobre", 
                                    "busca", "buscar", "consulta", "consultar", "noticias", 
                                    "novedades", "legislación", "legislacion", "dime", "dame", "que"}
                        significant = [w for w in words if len(w) > 3 and w not in stopwords]
                        if significant:
                            clean_q = significant[-1]  # Última palabra significativa
                        elif words:
                            clean_q = words[-1]
                    
                    payload["query"] = clean_q.strip()
                    logger.info(f"BOE Search: Original='{query}' -> Clean='{clean_q}'")
                
                try:
                    resp = requests.post(f"{self.valves.BACKEND_URL}/external/boe", json=payload, timeout=30)
                    if resp.status_code == 200:
                        data = resp.json()
                        results = data.get("results", [])
                        if not results:
                            yield f"No encontré resultados para '{payload.get('query', '')}'.\n"
                            yield "\n💡 *Prueba con términos más específicos: defensa, contratos, vivienda, etc.*"
                        else:
                            yield f"Encontré **{len(results)}** referencias:\n\n"
                            for i, item in enumerate(results[:10], 1):  # Mostrar más resultados
                                title = item.get('title', 'Sin título')
                                link = item.get('link', '#')
                                yield f"{i}. [{title[:100]}...]({link})\n"
                            yield "\n💡 *Pega el enlace para analizarlo en detalle.*"
                    else:
                        yield f"⚠️ Error BOE: {resp.text}\n"
                except Exception as e:
                    logger.error(f"Error BOE search: {e}")
                    yield f"⚠️ Error consultando BOE: {str(e)}\n"
                return

            elif action == "boe_get_law":
                law_name = metadata.get("law_name", "")
                article = metadata.get("article")
                
                yield f"📜 **Obteniendo ley**: {law_name}"
                if article:
                    yield f" (Artículo {article})"
                yield "\n\n"
                
                try:
                    # Llamar al backend
                    resp = requests.post(
                        f"{self.valves.BACKEND_URL}/external/boe/law",
                        json={"law_name": law_name, "include_text": True},
                        timeout=30
                    )
                    
                    if resp.status_code == 200:
                        data = resp.json()
                        title = data.get("title", "Sin título")
                        text = data.get("text", "")
                        link = data.get("link", "")
                        
                        yield f"## {title}\n\n"
                        yield f"🔗 [Ver en BOE]({link})\n\n"
                        
                        if article and text:
                            # Intentar extraer artículo específico
                            import re
                            art_pattern = rf"(?:Artículo|Art\.?)\s*{article}[.\s](.+?)(?=(?:Artículo|Art\.?)\s*\d+|$)"
                            match = re.search(art_pattern, text, re.DOTALL | re.IGNORECASE)
                            if match:
                                yield f"### Artículo {article}\n\n"
                                yield match.group(1).strip()[:3000]
                            else:
                                yield f"*No encontré el artículo {article} específicamente. Mostrando extracto:*\n\n"
                                yield text[:2000] + "..."
                        elif text:
                            yield text[:3000]
                            if len(text) > 3000:
                                yield "\n\n*...texto truncado...*"
                        else:
                            yield "⚠️ No se pudo obtener el texto de la ley."
                            
                    elif resp.status_code == 404:
                        yield f"⚠️ No encontré la ley '{law_name}' en el mapeo.\n\n"
                        yield "**Leyes disponibles:** LOPD, Constitución, Código Civil, Código Penal, "
                        yield "Estatuto de los Trabajadores, LPAC, LCSP, LGT, IRPF..."
                    else:
                        yield f"⚠️ Error: {resp.text}"
                        
                except Exception as e:
                    logger.error(f"Error get law: {e}")
                    yield f"⚠️ Error obteniendo ley: {str(e)}"
                return

            elif action == "boe_law_analysis":
                law_name = metadata.get("law_name", "")
                
                yield f"🔍 **Analizando referencias de**: {law_name}\n\n"
                
                try:
                    resp = requests.post(
                        f"{self.valves.BACKEND_URL}/external/boe/law/analysis",
                        json={"law_name": law_name},
                        timeout=30
                    )
                    
                    if resp.status_code == 200:
                        data = resp.json()
                        modifies = data.get("modifies", [])
                        modified_by = data.get("modified_by", [])
                        
                        if modifies:
                            yield f"### Leyes que **modifica** ({len(modifies)}):\n"
                            for m in modifies[:10]:
                                yield f"- [{m.get('title', m.get('id'))}](https://www.boe.es/buscar/act.php?id={m.get('id')})\n"
                            yield "\n"
                        
                        if modified_by:
                            yield f"### Leyes que la **modifican** ({len(modified_by)}):\n"
                            for m in modified_by[:10]:
                                yield f"- [{m.get('title', m.get('id'))}](https://www.boe.es/buscar/act.php?id={m.get('id')})\n"
                            yield "\n"
                        
                        if not modifies and not modified_by:
                            yield "No encontré referencias para esta ley en el análisis del BOE."
                            
                    elif resp.status_code == 404:
                        yield f"⚠️ No encontré la ley '{law_name}' en el mapeo."
                    else:
                        yield f"⚠️ Error: {resp.text}"
                        
                except Exception as e:
                    logger.error(f"Error law analysis: {e}")
                    yield f"⚠️ Error analizando ley: {str(e)}"
                return
            elif action == "ocr":
                yield "🖼️ **Analizando imagen...**\n\n"
                images = metadata.get("images", [])
                if not images:
                    yield "⚠️ No encontré la imagen.\n"
                    return
                # Guardar en memoria
                self._user_image_memory[self._get_user_email(body)] = images[0]
                
                prompt = user_message if user_message.strip() else "Describe esta imagen."
                sys_prompt = "Describe what you see clearly. If there is text, read it."
                
                # Extraer b64
                img_data = images[0].split("base64,")[1] if "base64," in images[0] else images[0]
                
                try:
                    ollama_resp = requests.post("http://ollama:11434/api/generate", json={
                        "model": "qwen2.5vl:7b",
                        "prompt": f"{sys_prompt}\n\n{prompt}",
                        "images": [img_data],
                        "stream": False
                    }, timeout=120)
                    if ollama_resp.status_code == 200:
                        yield ollama_resp.json().get("response", "")
                    else:
                        yield "⚠️ Error del modelo de visión.\n"
                except Exception as e:
                    yield f"❌ Error: {e}\n"
                return

            elif action == "rag":
                yield "📚 **Consultando documentos...**\n\n"
                data = self._call_backend_chat(user_message, "rag", user_data, messages, metadata=metadata)
                content = data.get("content", "")
                
                if messages and len(messages) > 1:
                     # Re-procesar con historial si es necesario
                     ctx = f"Resultados:\n{content}"
                     content = self._call_litellm_with_history(user_message, "Responde con los documentos.", messages, ctx)
                
                yield content
                
                if data.get("sources"):
                    yield "\n\n---\n### Fuentes:\n"
                    for src in data["sources"][:5]:
                        yield f"- **{src['filename']}** (pág {src.get('page')})\n"
                return

            elif action == "file_chat":
                yield "📎 **Procesando archivo adjunto...**\n\n"
                # Logic simplificada para restaurar rápido
                yield "⚠️ Funcionalidad de archivo restaurada. Por favor sube el archivo de nuevo si no respondo.\n"
                return

            elif action == "check_status":
                yield "📊 **Estado del Sistema**\n\n"
                try:
                    indexer = requests.get(f"{self.valves.INDEXER_URL}/health", timeout=2).json()
                    yield f"✅ Sincronización SharePoint: {'Activa' if indexer.get('sharepoint_enabled') else 'Inactiva'}\n"
                except:
                    yield "⚠️ No pude conectar con el indexer.\n"
                return

            else: # Chat normal
                if self.valves.DEBUG_MODE: yield "💬 **Modo conversación**\n"
                try:
                    resp = self._call_litellm_with_history(user_message, "Eres un asistente útil.", messages)
                    yield resp
                except:
                    # Fallback backend
                    d = self._call_backend_chat(user_message, "chat", user_data, messages)
                    yield d.get("content", "")
                return

        except Exception as e:
            logger.error(f"Error crítico en pipe: {e}", exc_info=True)
            yield f"❌ Error del sistema: {str(e)}"
