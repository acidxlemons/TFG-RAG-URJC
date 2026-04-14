"""
Conector para la API de Datos Abiertos del BOE (Boletín Oficial del Estado).
Doc: https://www.boe.es/datosabiertos/api/api.php

Conector unificado para backend y servidor MCP.
"""

import requests
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional
import logging
import time
import re
import unicodedata
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class BoeConnector:
    """
    Conector para buscar legislación en el BOE.
    Utiliza la API de sumarios y búsqueda del BOE.
    """
    
    # URLs base - usar siempre www.boe.es (consistente con documentación oficial)
    SUMARIO_URL = "https://www.boe.es/datosabiertos/api/boe/sumario/{date}"
    LEGISLACION_URL = "https://www.boe.es/datosabiertos/api/legislacion-consolidada"
    MATERIAS_URL = "https://www.boe.es/datosabiertos/api/datos-auxiliares/materias"
    
    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Enterprise-RAG-System/2.0; +https://github.com/enterprise-rag)",
        "Accept": "application/xml",
        "Accept-Language": "es-ES,es;q=0.9",
    }
    
    # Mapeo de nombres comunes a IDs del BOE
    LAW_MAPPINGS = {
        # Protección de Datos
        "lopd": "BOE-A-2018-16673",
        "lopdgdd": "BOE-A-2018-16673",
        "proteccion datos": "BOE-A-2018-16673",
        "protección datos": "BOE-A-2018-16673",
        "proteccion de datos": "BOE-A-2018-16673",
        "protección de datos": "BOE-A-2018-16673",
        "ley protección de datos": "BOE-A-2018-16673",
        "ley proteccion de datos": "BOE-A-2018-16673",
        "ley de protección de datos": "BOE-A-2018-16673",
        "ley de proteccion de datos": "BOE-A-2018-16673",
        "rgpd": "BOE-A-2018-16673",
        "datos personales": "BOE-A-2018-16673",
        # Constitución
        "constitución": "BOE-A-1978-31229",
        "constitucion": "BOE-A-1978-31229",
        "constitución española": "BOE-A-1978-31229",
        "constitucion española": "BOE-A-1978-31229",
        "ce": "BOE-A-1978-31229",
        # Laboral
        "estatuto de los trabajadores": "BOE-A-2015-11430",
        "estatuto trabajadores": "BOE-A-2015-11430",
        "et": "BOE-A-2015-11430",
        # Administrativo
        "lpac": "BOE-A-2015-10565",
        "procedimiento administrativo": "BOE-A-2015-10565",
        "ley procedimiento administrativo": "BOE-A-2015-10565",
        "lrjsp": "BOE-A-2015-10566",
        "régimen jurídico sector público": "BOE-A-2015-10566",
        # Contratos
        "lcsp": "BOE-A-2017-12902",
        "ley de contratos": "BOE-A-2017-12902",
        "contratos sector público": "BOE-A-2017-12902",
        "contratos publicos": "BOE-A-2017-12902",
        # Códigos
        "código civil": "BOE-A-1889-4763",
        "codigo civil": "BOE-A-1889-4763",
        "código penal": "BOE-A-1995-25444",
        "codigo penal": "BOE-A-1995-25444",
        # Tributario
        "lgt": "BOE-A-2003-23186",
        "ley general tributaria": "BOE-A-2003-23186",
        "irpf": "BOE-A-2006-20764",
        "ley irpf": "BOE-A-2006-20764",
        # Servicios de la Sociedad de la Información
        "lssi": "BOE-A-2002-13758",
        "lssice": "BOE-A-2002-13758",
        # Transparencia
        "ley de transparencia": "BOE-A-2013-12887",
        "transparencia": "BOE-A-2013-12887",
        # Seguridad Social
        "ley general seguridad social": "BOE-A-2015-11724",
        "lgss": "BOE-A-2015-11724",
        # Propiedad intelectual
        "ley propiedad intelectual": "BOE-A-1996-8930",
        "lpi": "BOE-A-1996-8930",
    }
    
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(self.DEFAULT_HEADERS)
    
    def _request_with_retry(self, url: str, method: str = "GET", 
                             max_retries: int = 3, timeout: int = 15, 
                             **kwargs) -> Optional[requests.Response]:
        """
        Realiza una petición HTTP con retry y backoff exponencial.
        Devuelve None si todos los intentos fallan.
        """
        for attempt in range(max_retries):
            try:
                if method.upper() == "GET":
                    response = self._session.get(url, timeout=timeout, **kwargs)
                else:
                    response = self._session.post(url, timeout=timeout, **kwargs)
                
                if response.status_code == 200:
                    return response
                elif response.status_code == 400:
                    # 400 podría ser problema temporal del BOE o fecha inválida
                    logger.warning(f"BOE API 400 en {url} (intento {attempt+1}/{max_retries})")
                    if attempt < max_retries - 1:
                        time.sleep(1 * (attempt + 1))
                    continue
                elif response.status_code == 404:
                    logger.warning(f"BOE API 404: recurso no encontrado en {url}")
                    return response  # 404 es definitivo, no reintentar
                elif response.status_code >= 500:
                    logger.warning(f"BOE API {response.status_code} en {url} (intento {attempt+1})")
                    if attempt < max_retries - 1:
                        time.sleep(2 * (attempt + 1))
                    continue
                else:
                    return response
                    
            except requests.Timeout:
                logger.warning(f"Timeout en BOE API {url} (intento {attempt+1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(1 * (attempt + 1))
            except requests.ConnectionError:
                logger.warning(f"Connection error a BOE API {url} (intento {attempt+1})")
                if attempt < max_retries - 1:
                    time.sleep(2 * (attempt + 1))
            except Exception as e:
                logger.error(f"Error inesperado en BOE API {url}: {e}")
                return None
        
        logger.error(f"BOE API: todos los intentos fallaron para {url}")
        return None

    def get_summary(self, date: Optional[str] = None) -> List[Dict]:
        """
        Obtiene el sumario del BOE para una fecha dada (YYYYMMDD).
        Si no se da fecha, usa la de hoy. Si hoy falla (festivo/domingo),
        intenta con los días anteriores.
        """
        if date:
            # Fecha específica
            return self._fetch_summary_for_date(date)
        
        # Sin fecha: intentar hoy y días anteriores (festivos no tienen BOE)
        for days_ago in range(4):
            target = datetime.now() - timedelta(days=days_ago)
            date_str = target.strftime("%Y%m%d")
            items = self._fetch_summary_for_date(date_str)
            if items:
                return items
            logger.info(f"No hay sumario BOE para {date_str}, intentando día anterior...")
        
        logger.warning("No se encontró sumario BOE en los últimos 4 días")
        return []
    
    def _fetch_summary_for_date(self, date: str) -> List[Dict]:
        """Obtiene el sumario de una fecha específica."""
        url = self.SUMARIO_URL.format(date=date)
        
        response = self._request_with_retry(url, timeout=10)
        if not response or response.status_code != 200:
            return []
        
        try:
            root = ET.fromstring(response.content)
            items = []
            
            valid_sections = ["I", "II", "III", "V"]
            
            for seccion in root.findall(".//seccion"):
                sec_name = seccion.get("nombre", "")
                if not any(sec_name.startswith(p) for p in valid_sections):
                    continue
                    
                for dep in seccion.findall("departamento"):
                    dep_name = dep.get("nombre", "")
                    
                    for item in dep.findall("item"):
                        ident = item.get("id", "")
                        titulo = item.find("titulo").text if item.find("titulo") is not None else ""
                        url_pdf = item.find("url_pdf").text if item.find("url_pdf") is not None else ""
                        url_html = item.find("url_html").text if item.find("url_html") is not None else ""
                        
                        items.append({
                            "id": ident,
                            "title": titulo,
                            "department": dep_name,
                            "section": sec_name,
                            "link": f"https://www.boe.es{url_html}" if url_html.startswith("/") else url_html,
                            "pdf": f"https://www.boe.es{url_pdf}" if url_pdf.startswith("/") else url_pdf,
                            "date": date
                        })
            
            return items
        except ET.ParseError as e:
            logger.error(f"Error parseando XML sumario {date}: {e}")
            return []

    def search_legislation(self, query: str, days_back: int = 30) -> List[Dict]:
        """
        Busca legislación. Primero intenta resolver por nombre conocido,
        luego busca keywords en los sumarios de los últimos días.
        """
        return self._legacy_search_legislation(query, days_back)

    def search_tenders(self, query: str, days_back: int = 30) -> List[Dict]:
        """
        Busca licitaciones (Sección V) buscando keywords en los sumarios.
        """
        return self._legacy_search_tenders(query, days_back)

    @staticmethod
    def _normalize_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKD", (text or "").strip().lower())
        return "".join(ch for ch in normalized if not unicodedata.combining(ch))

    def _query_keywords(self, query: str) -> List[str]:
        stopwords = {
            "boe", "del", "de", "la", "el", "los", "las", "que", "sobre", "hoy", "ayer",
            "busca", "buscar", "consulta", "consultar",
        }
        tokens = re.findall(r"[a-z0-9]{3,}", self._normalize_text(query))
        return [token for token in tokens if token not in stopwords]

    def _score_result(self, query: str, result: Dict, *, tender_mode: bool = False) -> float:
        query_norm = self._normalize_text(query)
        keywords = self._query_keywords(query)
        title_norm = self._normalize_text(result.get("title", ""))
        summary_norm = self._normalize_text(result.get("summary", ""))
        department_norm = self._normalize_text(result.get("department", ""))
        section_norm = self._normalize_text(result.get("section", ""))
        combined = " ".join([title_norm, summary_norm, department_norm, section_norm])

        score = 0.0
        if query_norm and query_norm in combined:
            score += 8.0

        if keywords:
            title_hits = sum(1 for kw in keywords if kw in title_norm)
            summary_hits = sum(1 for kw in keywords if kw in summary_norm)
            department_hits = sum(1 for kw in keywords if kw in department_norm)
            score += (title_hits * 4.0) + (summary_hits * 2.0) + (department_hits * 2.0)
            if title_hits == len(keywords):
                score += 5.0
        else:
            score += 1.0

        if tender_mode and section_norm.startswith("v"):
            score += 4.0
        if not tender_mode and section_norm.startswith("v"):
            score -= 10.0

        date_value = str(result.get("date", "")).replace("-", "")
        if date_value.isdigit():
            score += int(date_value[-2:]) / 100.0

        return score

    def _rank_results(self, query: str, results: List[Dict], *, tender_mode: bool = False, limit: int = 20) -> List[Dict]:
        scored = []
        seen = set()

        for index, result in enumerate(results):
            key = result.get("link") or result.get("title")
            if not key or key in seen:
                continue
            seen.add(key)
            score = result.get("_score")
            if score is None:
                score = self._score_result(query, result, tender_mode=tender_mode)
            scored.append((float(score), -index, result))

        scored.sort(reverse=True)
        return [result for _, _, result in scored[:limit]]

    def _legacy_search_legislation(self, query: str, days_back: int = 30) -> List[Dict]:
        """
        Busca legislacion sin mezclar resultados de la seccion V (licitaciones).
        """
        found: List[Dict] = []
        query_lower = self._normalize_text(query).strip()
        keywords = self._query_keywords(query)

        law_id = self.resolve_law_id(query)
        if law_id:
            logger.info(f"Identificada ley conocida: '{query}' -> {law_id}")
            metadata = self.get_law_metadata(law_id)
            if "error" not in metadata:
                found.append(
                    {
                        "title": metadata.get("title", f"Ley {law_id}"),
                        "link": metadata.get("link", f"https://www.boe.es/buscar/act.php?id={law_id}"),
                        "summary": (
                            f"Legislacion consolidada. Estado: {metadata.get('estado', 'Vigente')}. "
                            f"Publicado: {metadata.get('fecha_publicacion', 'Desconocido')}"
                        ),
                        "source": "BOE Legislacion Consolidada",
                        "date": metadata.get("fecha_publicacion", ""),
                        "section": "Legislacion consolidada",
                        "_score": 100.0,
                    }
                )

        start_date = datetime.now()
        logger.info(
            f"Buscando '{query}' (keywords: {keywords}) en sumarios de los ultimos {days_back} dias..."
        )

        for i in range(days_back):
            date = start_date - timedelta(days=i)
            date_str = date.strftime("%Y%m%d")

            url = self.SUMARIO_URL.format(date=date_str)
            response = self._request_with_retry(url, timeout=5, max_retries=1)
            if not response or response.status_code != 200:
                continue

            try:
                root = ET.fromstring(response.content)
                for seccion in root.findall(".//seccion"):
                    sec_name = seccion.get("nombre", "")
                    if sec_name.startswith("V."):
                        continue

                    containers = seccion.findall("departamento") or [seccion]
                    for container in containers:
                        dep_name = container.get("nombre", "") if container is not seccion else ""
                        for item in container.findall(".//item"):
                            titulo_el = item.find("titulo")
                            titulo = titulo_el.text if titulo_el is not None and titulo_el.text else ""
                            combined = " ".join(
                                [
                                    self._normalize_text(titulo),
                                    self._normalize_text(dep_name),
                                    self._normalize_text(sec_name),
                                ]
                            )

                            full_match = bool(query_lower and query_lower in combined)
                            keyword_matches = sum(1 for kw in keywords if kw in combined) if keywords else 0
                            min_hits = 1 if len(keywords) <= 2 else 2
                            if not (full_match or keyword_matches >= min_hits):
                                continue

                            url_html = item.find("url_html")
                            url_html_text = url_html.text if url_html is not None and url_html.text else ""
                            formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
                            summary_parts = [f"Publicado el {formatted_date}"]
                            if dep_name:
                                summary_parts.append(f"Departamento: {dep_name}")
                            if sec_name:
                                summary_parts.append(f"Seccion: {sec_name}")

                            found.append(
                                {
                                    "title": titulo,
                                    "link": f"https://www.boe.es{url_html_text}" if url_html_text.startswith("/") else url_html_text,
                                    "summary": ". ".join(summary_parts),
                                    "source": "BOE Sumario",
                                    "date": formatted_date,
                                    "department": dep_name,
                                    "section": sec_name,
                                }
                            )
            except Exception as e:
                logger.warning(f"Error procesando sumario {date_str}: {e}")
                continue

        ranked = self._rank_results(query, found, tender_mode=False, limit=20)
        logger.info(f"Encontrados {len(ranked)} resultados para '{query}'")
        return ranked

    def _legacy_search_tenders(self, query: str, days_back: int = 30) -> List[Dict]:
        """
        Busca licitaciones en la seccion V y las ordena por relevancia.
        """
        found: List[Dict] = []
        query_lower = self._normalize_text(query)
        keywords = self._query_keywords(query)
        start_date = datetime.now()

        logger.info(f"Buscando licitaciones '{query}' en ultimos {days_back} dias...")

        for i in range(days_back):
            date = start_date - timedelta(days=i)
            date_str = date.strftime("%Y%m%d")

            url = self.SUMARIO_URL.format(date=date_str)
            response = self._request_with_retry(url, timeout=5, max_retries=1)
            if not response or response.status_code != 200:
                continue

            try:
                root = ET.fromstring(response.content)
                for seccion in root.findall(".//seccion"):
                    sec_name = seccion.get("nombre", "")
                    if not sec_name.startswith("V."):
                        continue

                    containers = seccion.findall("departamento") or [seccion]
                    for container in containers:
                        dep_name = container.get("nombre", "") if container is not seccion else ""
                        for item in container.findall(".//item"):
                            titulo_el = item.find("titulo")
                            titulo = titulo_el.text if titulo_el is not None and titulo_el.text else ""
                            combined = " ".join(
                                [
                                    self._normalize_text(titulo),
                                    self._normalize_text(dep_name),
                                    self._normalize_text(sec_name),
                                ]
                            )

                            full_match = bool(query_lower and query_lower in combined)
                            keyword_matches = sum(1 for kw in keywords if kw in combined)
                            min_hits = 1 if len(keywords) <= 2 else 2
                            allow_generic = not keywords
                            if not (allow_generic or full_match or keyword_matches >= min_hits):
                                continue

                            url_html = item.find("url_html")
                            url_text = url_html.text if url_html is not None and url_html.text else ""
                            formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
                            summary_parts = [f"Anuncio publicado el {formatted_date}"]
                            if dep_name:
                                summary_parts.append(f"Organismo: {dep_name}")

                            found.append(
                                {
                                    "title": titulo,
                                    "link": f"https://www.boe.es{url_text}" if url_text.startswith("/") else url_text,
                                    "summary": ". ".join(summary_parts),
                                    "source": "BOE Licitaciones",
                                    "date": formatted_date,
                                    "section": sec_name,
                                    "department": dep_name,
                                }
                            )
            except Exception as e:
                logger.warning(f"Error procesando sumario licitaciones {date_str}: {e}")
                continue

        ranked = self._rank_results(query, found, tender_mode=True, limit=20)
        logger.info(f"Encontradas {len(ranked)} licitaciones para '{query}'")
        return ranked

    def resolve_law_id(self, law_name: str) -> Optional[str]:
        """Resuelve nombre común a ID BOE usando coincidencia difusa y limpieza."""
        
        # 1. Limpieza básica
        query = law_name.lower().strip()
        
        # 2. Quitar stop words comunes para normalizar
        stop_words = ["la ", "el ", "los ", "las ", " de ", " del ", " por ", " para ", " sobre ",
                       "busca ", "buscar ", "consulta ", "consultar ", "dime ", "dame ",
                       "en el boe ", "boe ", "legislación ", "legislacion "]
        clean_query = query
        for sw in stop_words:
            clean_query = clean_query.replace(sw, " ")
        
        clean_query = " ".join(clean_query.split())  # Quitar espacios extra
        
        # 3. Búsqueda directa en mappings (clean y original)
        if clean_query in self.LAW_MAPPINGS:
            return self.LAW_MAPPINGS[clean_query]
            
        if query in self.LAW_MAPPINGS:
            return self.LAW_MAPPINGS[query]

        # 4. Búsqueda por subcadenas (flexible)
        # Priorizamos match más largo para evitar falsos positivos
        best_match = None
        max_len = 0
        
        for key, boe_id in self.LAW_MAPPINGS.items():
            if key in query or key in clean_query:
                if len(key) > max_len:
                    max_len = len(key)
                    best_match = boe_id
            # También buscar la clave dentro del query limpio
            if query in key or clean_query in key:
                if len(key) > max_len:
                    max_len = len(key)
                    best_match = boe_id
        
        return best_match

    def get_law_text(self, law_id: str) -> Dict:
        """Obtiene texto completo de una ley consolidada."""
        url = f"{self.LEGISLACION_URL}/id/{law_id}/texto"
        
        response = self._request_with_retry(url, timeout=30)
        
        if not response:
            return {"error": "No se pudo conectar con la API del BOE", "law_id": law_id}
        
        if response.status_code == 404:
            return {"error": "Ley no encontrada", "law_id": law_id}
        
        if response.status_code != 200:
            return {"error": f"Error API BOE: HTTP {response.status_code}", "law_id": law_id}
        
        try:
            root = ET.fromstring(response.content)
            
            # Obtener título de metadatos
            titulo = law_id
            try:
                meta = self.get_law_metadata(law_id)
                if "error" not in meta and meta.get("title"):
                    titulo = meta["title"]
            except Exception:
                pass
            
            # Extraer texto
            texto_parts = []
            for p in root.findall(".//p"):
                if p.text:
                    texto_parts.append(p.text.strip())
                full_text = ET.tostring(p, encoding='unicode', method='text')
                if full_text and full_text.strip() and full_text.strip() not in texto_parts:
                    texto_parts.append(full_text.strip())
            
            if not texto_parts:
                for bloque in root.findall(".//bloque"):
                    bloque_text = ET.tostring(bloque, encoding='unicode', method='text')
                    if bloque_text and bloque_text.strip():
                        texto_parts.append(bloque_text.strip())
            
            texto_completo = "\n\n".join(texto_parts)
            
            return {
                "law_id": law_id,
                "title": titulo,
                "text": texto_completo,
                "word_count": len(texto_completo.split()),
                "link": f"https://www.boe.es/buscar/act.php?id={law_id}"
            }
        except ET.ParseError as e:
            logger.error(f"Error parseando XML ley {law_id}: {e}")
            return {"error": f"Error parseando respuesta: {e}", "law_id": law_id}

    def get_law_metadata(self, law_id: str) -> Dict:
        """Obtiene metadatos de una ley."""
        url = f"{self.LEGISLACION_URL}/id/{law_id}/metadatos"
        
        response = self._request_with_retry(url, timeout=15)
        
        if not response or response.status_code != 200:
            # Fallback: devolver info básica sin error
            return {
                "law_id": law_id,
                "title": f"Ley {law_id}",
                "rango": None,
                "fecha_publicacion": None,
                "estado": "Vigente",
                "link": f"https://www.boe.es/buscar/act.php?id={law_id}"
            }
        
        try:
            root = ET.fromstring(response.content)
            
            def get_text(xpath):
                el = root.find(xpath)
                return el.text if el is not None else None
            
            return {
                "law_id": law_id,
                "title": get_text(".//titulo"),
                "rango": get_text(".//rango"),
                "fecha_publicacion": get_text(".//fecha_publicacion"),
                "estado": get_text(".//estado_consolidacion"),
                "link": f"https://www.boe.es/buscar/act.php?id={law_id}"
            }
        except ET.ParseError as e:
            logger.error(f"Error parseando metadatos {law_id}: {e}")
            return {
                "law_id": law_id,
                "title": f"Ley {law_id}",
                "link": f"https://www.boe.es/buscar/act.php?id={law_id}"
            }

    def get_subjects(self) -> List[Dict]:
        """Obtiene lista de materias/categorías."""
        response = self._request_with_retry(self.MATERIAS_URL, timeout=15)
        
        if not response or response.status_code != 200:
            return []
        
        try:
            root = ET.fromstring(response.content)
            subjects = []
            for i in root.findall(".//item"):
                codigo = i.find("codigo")
                nombre = i.find("nombre")
                if codigo is not None and codigo.text and nombre is not None and nombre.text:
                    subjects.append({"code": codigo.text, "name": nombre.text})
            return subjects
        except Exception as e:
            logger.error(f"Error obteniendo materias: {e}")
            return []

    def get_law_analysis(self, law_id: str) -> Dict:
        """Obtiene análisis jurídico: qué modifica y qué la modifica."""
        url = f"{self.LEGISLACION_URL}/id/{law_id}/analisis"
        
        response = self._request_with_retry(url, timeout=15)
        
        if not response or response.status_code != 200:
            return {"error": "No se pudo obtener análisis", "law_id": law_id}
        
        try:
            root = ET.fromstring(response.content)
            
            def parse_relations(parent_tag):
                rels = []
                for r in root.findall(f".//{parent_tag}"):
                    id_norma = r.find("id_norma")
                    relacion = r.find("relacion")
                    texto = r.find("texto")
                    
                    if id_norma is not None and id_norma.text:
                        rels.append({
                            "id": id_norma.text,
                            "relation": relacion.text if relacion is not None else None,
                            "title": texto.text if texto is not None else None,
                            "description": texto.text if texto is not None else None
                        })
                return rels
            
            return {
                "law_id": law_id,
                "modifies": parse_relations("anterior"),
                "modified_by": parse_relations("posterior")
            }
        except Exception as e:
            logger.error(f"Error get_law_analysis: {e}")
            return {"error": str(e), "law_id": law_id}
