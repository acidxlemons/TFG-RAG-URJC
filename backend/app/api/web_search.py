"""
Web Search API – Google (primary) + DuckDuckGo fallbacks.
Endpoint: GET /web-search?q=...
"""

import re
import logging
import requests
from datetime import datetime
from typing import Optional, List, Dict
from urllib.parse import unquote, urlparse, parse_qs
from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter()


# ======================================================
# Freshness Detection
# ======================================================

CURRENT_EVENT_KEYWORDS = [
    "hoy", "ayer", "esta semana", "este mes", "últimas noticias",
    "precio", "cotización", "tiempo", "clima", "resultado",
    "2025", "2026", "cuando juega", "próximo partido",
    "noticias", "última hora", "breaking",
]

SEARCH_PREFIX_PATTERN = r"^(?:busca(?:me)?(?:\s+en\s+(?:internet|la\s+web|web|google))?\s+)"
SUMMARY_SUFFIX_PATTERN = (
    r"\s+y\s+(?:resume|resumen|resúmelo|resumelo|cuéntame|cuentame|"
    r"háblame|hablame|dime)(?:\s+lo\s+importante|\s+más|\s+mas)?\.?$"
)
OFFICIAL_SITE_PATTERNS = [
    r"(?:la\s+|el\s+)?(?:web|sitio|p[aá]gina)\s+oficial\s+de\s+(.+)",
    r"official site of\s+(.+)",
    r"official website of\s+(.+)",
]
OFFICIAL_SITE_STOPWORDS = {
    "la", "el", "de", "del", "of", "official", "site", "website", "web", "sitio",
    "pagina", "página", "resume", "resumen", "importante", "lo", "y",
}
OFFICIAL_SITE_BAD_DOMAINS = {
    "wikipedia.org", "developer.mozilla.org", "sitios.cl", "whatsapp.com",
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "youtube.com",
}
OFFICIAL_SITE_TLDS = ["com", "io", "tech", "ai", "org", "es"]

def needs_fresh_results(query: str) -> bool:
    """Detecta si la query necesita resultados recientes."""
    query_lower = query.lower()
    return any(kw in query_lower for kw in CURRENT_EVENT_KEYWORDS)


def _clean_search_query(query: str) -> str:
    cleaned = re.sub(SEARCH_PREFIX_PATTERN, "", (query or "").strip(), flags=re.IGNORECASE).strip()
    cleaned = re.sub(SUMMARY_SUFFIX_PATTERN, "", cleaned, flags=re.IGNORECASE).strip(" .")
    return cleaned or (query or "").strip()


def _extract_official_site_entity(query: str) -> Optional[str]:
    if not query:
        return None

    for pattern in OFFICIAL_SITE_PATTERNS:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            entity = match.group(1).strip(" .")
            entity = re.sub(SUMMARY_SUFFIX_PATTERN, "", entity, flags=re.IGNORECASE).strip(" .")
            return entity or None
    return None


def _prepare_search_query(query: str) -> str:
    cleaned = _clean_search_query(query)
    official_entity = _extract_official_site_entity(cleaned)
    if official_entity:
        return f"{official_entity} official site"
    return cleaned


def _tokenize_official_entity(entity: str) -> List[str]:
    return [
        token for token in re.findall(r"[a-z0-9]+", (entity or "").lower())
        if token and token not in OFFICIAL_SITE_STOPWORDS
    ]


def _rerank_results(query: str, results: List[Dict]) -> List[Dict]:
    if not results:
        return results

    entity = _extract_official_site_entity(query) or _extract_official_site_entity(_clean_search_query(query))
    if not entity and not any(word in (query or "").lower() for word in ("oficial", "official")):
        return results

    tokens = _tokenize_official_entity(entity or query)
    if not tokens:
        return results

    def score(result: Dict) -> tuple[int, float]:
        title = str(result.get("title", "") or "").lower()
        link = extract_url_from_ddg_redirect(str(result.get("link", "") or ""))
        domain = urlparse(link).netloc.lower().removeprefix("www.")
        snippet = str(result.get("snippet", "") or "").lower()

        current = 0
        if any(token == domain.split(".")[0] for token in tokens):
            current += 14
        if any(token in domain for token in tokens):
            current += 10
        if any(token in title for token in tokens):
            current += 4
        if any(token in snippet for token in tokens):
            current += 2
        if domain.count(".") <= 1:
            current += 1
        if any(bad in domain for bad in OFFICIAL_SITE_BAD_DOMAINS):
            current -= 8

        return current, float(len(result.get("snippet", "") or ""))

    return sorted(results, key=score, reverse=True)


def _has_domain_match_for_entity(entity: str, results: List[Dict]) -> bool:
    tokens = _tokenize_official_entity(entity)
    if not tokens:
        return False

    for result in results[:5]:
        domain = urlparse(str(result.get("link", "") or "")).netloc.lower().removeprefix("www.")
        if any(token in domain for token in tokens):
            return True
    return False


def _filter_results_for_entity_domains(entity: str, results: List[Dict]) -> List[Dict]:
    tokens = _tokenize_official_entity(entity)
    if not tokens:
        return results

    filtered: List[Dict] = []
    for result in results:
        domain = urlparse(str(result.get("link", "") or "")).netloc.lower().removeprefix("www.")
        if any(token in domain for token in tokens):
            filtered.append(result)

    return filtered or results


def _extract_html_title(html: str) -> str:
    if not html:
        return ""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _extract_meta_description(html: str) -> str:
    if not html:
        return ""
    match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _guess_official_site(entity: str) -> Optional[Dict]:
    tokens = _tokenize_official_entity(entity)
    if not tokens:
        return None

    bases = []
    joined = "".join(tokens)
    hyphenated = "-".join(tokens)
    for base in [joined, hyphenated, tokens[0]]:
        if base and base not in bases:
            bases.append(base)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }
    parked_markers = ["buy this domain", "domain for sale", "sedo", "parking"]

    for base in bases:
        for tld in OFFICIAL_SITE_TLDS:
            for prefix in ("https://www.", "https://"):
                candidate = f"{prefix}{base}.{tld}/"
                try:
                    response = requests.get(candidate, headers=headers, timeout=5, allow_redirects=True)
                except Exception:
                    continue

                if response.status_code >= 400:
                    continue

                final_url = response.url
                final_domain = urlparse(final_url).netloc.lower().removeprefix("www.")
                if base not in final_domain:
                    continue

                page_title = _extract_html_title(response.text)
                meta_description = _extract_meta_description(response.text)
                page_title_lower = page_title.lower()
                response_text_lower = response.text[:4000].lower()
                if any(marker in response_text_lower for marker in parked_markers):
                    continue

                if page_title and not any(token in page_title_lower or token in final_domain for token in tokens):
                    continue

                title = page_title or f"{entity} - Sitio oficial"
                return {
                    "title": title,
                    "link": final_url,
                    "snippet": meta_description or f"Sitio oficial detectado por comprobación directa del dominio {final_domain}.",
                }

    return None


def _enrich_official_site_results(query: str, results: List[Dict]) -> List[Dict]:
    entity = _extract_official_site_entity(query) or _extract_official_site_entity(_clean_search_query(query))
    if not entity:
        return results

    if _has_domain_match_for_entity(entity, results):
        return _filter_results_for_entity_domains(entity, results)

    guessed = _guess_official_site(entity)
    if not guessed:
        return results

    deduped = [result for result in results if result.get("link") != guessed["link"]]
    return _filter_results_for_entity_domains(entity, [guessed] + deduped)


# ======================================================
# URL Utility
# ======================================================

def extract_url_from_ddg_redirect(ddg_url: str) -> str:
    """Extrae la URL real de un redirect de DuckDuckGo."""
    try:
        if "duckduckgo.com" in ddg_url:
            parsed = urlparse(ddg_url)
            params = parse_qs(parsed.query)
            if "uddg" in params:
                return unquote(params["uddg"][0])
        return ddg_url
    except Exception:
        return ddg_url


# ======================================================
# Result Quality Filtering
# ======================================================

JUNK_DOMAINS = {
    "dle.rae.es", "www.rae.es",
    "wordreference.com", "www.wordreference.com",
    "thefreedictionary.com", "www.thefreedictionary.com",
    "diccionario.reverso.net",
    "en.wiktionary.org", "es.wiktionary.org",
    "www.definicion.de", "definicion.de",
    "zhihu.com", "www.zhihu.com", "zhuanlan.zhihu.com",
    "baidu.com", "www.baidu.com", "tieba.baidu.com",
    "weibo.com", "www.weibo.com",
    "bilibili.com", "www.bilibili.com",
}

# Domains that are only junk when the query is NOT about definitions/language
DICTIONARY_DOMAINS = {
    "dle.rae.es", "www.rae.es",
    "wordreference.com", "www.wordreference.com",
    "es.wiktionary.org", "en.wiktionary.org",
    "definicion.de", "www.definicion.de",
}

DEFINITION_QUERY_MARKERS = [
    "significado", "definición", "definicion", "que significa",
    "qué significa", "meaning of", "definition of",
    "sinónimo", "sinonimo", "antónimo", "antonimo",
    "conjugación", "conjugacion", "conjugar",
]

NON_SPANISH_PATTERNS = re.compile(
    r"[\u4e00-\u9fff"     # CJK Unified Ideographs (Chinese)
    r"\u3040-\u309f"      # Hiragana (Japanese)
    r"\u30a0-\u30ff"      # Katakana (Japanese)
    r"\uac00-\ud7af"      # Hangul (Korean)
    r"\u0600-\u06ff"      # Arabic
    r"\u0e00-\u0e7f]"     # Thai
)


def _is_definition_query(query: str) -> bool:
    """Check if query is asking for word definitions/meanings."""
    query_lower = (query or "").lower()
    return any(marker in query_lower for marker in DEFINITION_QUERY_MARKERS)


def _has_keyword_overlap(query: str, text: str, min_overlap: int = 1) -> bool:
    """Check if there's meaningful keyword overlap between query and text."""
    stopwords = {
        "de", "la", "el", "en", "que", "es", "un", "una", "los", "las",
        "del", "al", "por", "con", "para", "como", "se", "su", "lo",
        "mas", "más", "pero", "sin", "sobre", "entre", "ser", "hay",
        "y", "a", "o", "no", "si", "sí", "mi", "te", "me", "le",
        "the", "a", "an", "of", "in", "to", "and", "is", "for", "on",
    }
    query_tokens = {
        w for w in re.findall(r"[a-záéíóúüñ]+", (query or "").lower())
        if len(w) >= 3 and w not in stopwords
    }
    text_lower = (text or "").lower()
    if not query_tokens:
        return True  # Can't judge, pass through
    overlap = sum(1 for token in query_tokens if token in text_lower)
    return overlap >= min_overlap


def _is_non_spanish_content(title: str, snippet: str) -> bool:
    """Detect if a result is in a non-Latin-script language (Chinese, Japanese, etc.)."""
    combined = f"{title or ''} {snippet or ''}"
    non_latin_chars = len(NON_SPANISH_PATTERNS.findall(combined))
    latin_chars = len(re.findall(r"[a-záéíóúüñA-ZÁÉÍÓÚÜÑ]", combined))
    total = non_latin_chars + latin_chars
    if total == 0:
        return False
    # If more than 30% of alphabetic characters are non-Latin, it's likely foreign
    return non_latin_chars / total > 0.3


def _filter_junk_results(query: str, results: List[Dict]) -> List[Dict]:
    """
    Filtra resultados irrelevantes:
    - Sitios de diccionario cuando la query no es sobre definiciones
    - Resultados en idiomas no-latinos (chino, japonés, etc.)
    - Resultados sin overlap de keywords con la query
    """
    if not results:
        return results

    is_def_query = _is_definition_query(query)
    filtered = []

    for result in results:
        link = str(result.get("link", "") or "")
        title = str(result.get("title", "") or "")
        snippet = str(result.get("snippet", "") or "")
        domain = urlparse(link).netloc.lower().removeprefix("www.")
        full_domain = urlparse(link).netloc.lower()

        # 1. Filter dictionary sites (unless query is about definitions)
        if not is_def_query and (domain in DICTIONARY_DOMAINS or full_domain in DICTIONARY_DOMAINS):
            logger.debug(f"Filtered dictionary result: {link}")
            continue

        # 2. Always filter known junk domains (zhihu, baidu, etc.)
        if domain in JUNK_DOMAINS or full_domain in JUNK_DOMAINS:
            logger.debug(f"Filtered junk domain: {link}")
            continue

        # 3. Filter non-Spanish content
        if _is_non_spanish_content(title, snippet):
            logger.debug(f"Filtered non-Spanish result: {title[:50]}")
            continue

        # 4. Filter results with zero keyword overlap (title + snippet must share words with query)
        combined_text = f"{title} {snippet}"
        if not _has_keyword_overlap(query, combined_text, min_overlap=1):
            logger.debug(f"Filtered no-overlap result: {title[:50]}")
            continue

        filtered.append(result)

    # If we filtered everything, return the original (better than nothing)
    if not filtered and results:
        logger.warning(f"All results filtered for '{query}', returning originals")
        return results

    return filtered


def _enrich_result_snippets(results: List[Dict], max_enrich: int = 3) -> List[Dict]:
    """
    Enriquece snippets vacíos o muy cortos descargando la meta description de la página.
    Solo enriquece los primeros `max_enrich` resultados para no ralentizar.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }
    enriched_count = 0

    for result in results:
        if enriched_count >= max_enrich:
            break

        snippet = str(result.get("snippet", "") or "").strip()
        if len(snippet) > 60:
            continue  # Already has a decent snippet

        link = result.get("link", "")
        if not link:
            continue

        try:
            resp = requests.get(link, headers=headers, timeout=5, allow_redirects=True)
            if resp.status_code >= 400:
                continue

            html = resp.text[:10000]  # Only parse first 10KB

            # Extract meta description
            meta_desc = _extract_meta_description(html)
            if meta_desc and len(meta_desc) > len(snippet):
                result["snippet"] = meta_desc
                enriched_count += 1
                logger.debug(f"Enriched snippet for: {link[:60]}")
                continue

            # Extract title if missing
            if not result.get("title") or result["title"] == "Sin título":
                page_title = _extract_html_title(html)
                if page_title:
                    result["title"] = page_title

            # Extract first paragraph as snippet fallback
            paragraph_match = re.search(
                r"<p[^>]*>([^<]{40,300})</p>",
                html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if paragraph_match:
                paragraph_text = re.sub(r"<[^>]+>", " ", paragraph_match.group(1))
                paragraph_text = re.sub(r"\s+", " ", paragraph_text).strip()
                if len(paragraph_text) > len(snippet):
                    result["snippet"] = paragraph_text
                    enriched_count += 1
                    logger.debug(f"Enriched snippet from <p> for: {link[:60]}")

        except Exception as e:
            logger.debug(f"Failed to enrich snippet for {link[:60]}: {e}")
            continue

    return results


# ======================================================
# Method 0: Google Search (PRIMARY - best quality)
# ======================================================

def search_with_google(query: str, max_results: int = 5) -> List[Dict]:
    """Búsqueda usando googlesearch-python (scraping de Google)."""
    try:
        from googlesearch import search as google_search

        urls = list(google_search(query, num_results=max_results, lang="es", sleep_interval=0))

        if not urls:
            return []

        results = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }

        for url in urls[:max_results]:
            title = ""
            snippet = ""
            try:
                resp = requests.get(url, headers=headers, timeout=5, allow_redirects=True)
                if resp.status_code < 400:
                    html = resp.text[:10000]
                    title = _extract_html_title(html)
                    snippet = _extract_meta_description(html)
                    if not snippet:
                        # Try first meaningful paragraph
                        p_match = re.search(
                            r"<p[^>]*>([^<]{40,300})</p>",
                            html,
                            flags=re.IGNORECASE | re.DOTALL,
                        )
                        if p_match:
                            snippet = re.sub(r"<[^>]+>", " ", p_match.group(1))
                            snippet = re.sub(r"\s+", " ", snippet).strip()
            except Exception:
                pass

            results.append({
                "title": title or url,
                "link": url,
                "snippet": snippet or "",
            })

        if results:
            logger.info(f"Google search: {len(results)} resultados para '{query}'")
        return results

    except ImportError:
        logger.warning("googlesearch-python library no instalada, usando fallback")
        return []
    except Exception as e:
        logger.warning(f"Google search error: {e}")
        return []


# ======================================================
# Method 1: duckduckgo-search library
# ======================================================

def search_with_ddgs(query: str, max_results: int = 5) -> List[Dict]:
    """Búsqueda usando la librería duckduckgo-search con región forzada a español."""
    try:
        from duckduckgo_search import DDGS

        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, region="es-es", max_results=max_results):
                url = r.get("href", r.get("link", ""))
                if url:
                    results.append({
                        "title": r.get("title", "Sin título"),
                        "link": url,
                        "snippet": r.get("body", r.get("snippet", "")),
                    })

        if results:
            logger.info(f"DDGS library: {len(results)} resultados para '{query}'")
        return results

    except ImportError:
        logger.warning("duckduckgo-search library no instalada, usando fallback")
        return []
    except Exception as e:
        logger.warning(f"DDGS library error: {e}")
        return []


# ======================================================
# Method 2: HTML Scraping (fallback)
# ======================================================

def search_with_html_scraping(query: str, max_results: int = 5) -> List[Dict]:
    """Búsqueda en DuckDuckGo usando scraping HTML directo."""
    try:
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Referer": "https://duckduckgo.com/",
        }
        
        params = {"q": query, "kl": "es-es"}
        
        response = requests.post(url, data=params, headers=headers, timeout=10)
        
        if response.status_code != 200:
            logger.warning(f"DDG HTML scraping status: {response.status_code}")
            return []
        
        results = []
        from html.parser import HTMLParser
        
        class DDGParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.results = []
                self.current = {}
                self.in_result = False
                self.in_title = False
                self.in_snippet = False
                self.depth = 0
            
            def handle_starttag(self, tag, attrs):
                attrs_dict = dict(attrs)
                
                if tag == "a" and attrs_dict.get("class") == "result__a":
                    self.in_title = True
                    href = attrs_dict.get("href", "")
                    real_url = extract_url_from_ddg_redirect(href)
                    self.current = {"title": "", "link": real_url, "snippet": ""}
                
                if tag == "a" and attrs_dict.get("class") == "result__snippet":
                    self.in_snippet = True
                    
            def handle_data(self, data):
                if self.in_title:
                    self.current["title"] += data
                elif self.in_snippet and self.current:
                    self.current["snippet"] += data
                    
            def handle_endtag(self, tag):
                if tag == "a" and self.in_title:
                    self.in_title = False
                elif tag == "a" and self.in_snippet:
                    self.in_snippet = False
                    if self.current.get("link") and self.current.get("title"):
                        self.results.append(self.current)
                    self.current = {}
        
        parser = DDGParser()
        parser.feed(response.text)
        results = parser.results[:max_results]
        
        # Fallback: regex parsing if HTMLParser finds nothing
        if not results:
            # Try extracting from raw HTML with regex
            link_pattern = r'class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>'
            snippet_pattern = r'class="result__snippet"[^>]*>([^<]+)'
            
            links = re.findall(link_pattern, response.text)
            snippets = re.findall(snippet_pattern, response.text)
            
            for i, (href, title) in enumerate(links[:max_results]):
                real_url = extract_url_from_ddg_redirect(href)
                snippet = snippets[i] if i < len(snippets) else ""
                if real_url and title.strip():
                    results.append({
                        "title": title.strip(),
                        "link": real_url,
                        "snippet": snippet.strip(),
                    })
        
        if results:
            logger.info(f"HTML scraping: {len(results)} resultados para '{query}'")
        return results
        
    except Exception as e:
        logger.error(f"Error en HTML scraping: {e}")
        return []


# ======================================================
# Method 3: DuckDuckGo Lite (second fallback)
# ======================================================

def search_with_ddg_lite(query: str, max_results: int = 5) -> List[Dict]:
    """Búsqueda usando DuckDuckGo Lite (más permisivo con scraping)."""
    try:
        url = "https://lite.duckduckgo.com/lite/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html",
            "Accept-Language": "es-ES,es;q=0.9",
        }
        
        response = requests.post(url, data={"q": query, "kl": "es-es"}, headers=headers, timeout=10)
        
        if response.status_code != 200:
            return []
        
        results = []
        # DDG Lite uses simple table-based layout
        link_pattern = r'<a[^>]+href="([^"]+)"[^>]*class="result-link"[^>]*>([^<]+)</a>'
        matches = re.findall(link_pattern, response.text)
        
        if not matches:
            # More generic pattern
            link_pattern = r'<a[^>]+rel="nofollow"[^>]+href="(https?://[^"]+)"[^>]*>([^<]+)</a>'
            matches = re.findall(link_pattern, response.text)
        
        for href, title in matches[:max_results]:
            real_url = extract_url_from_ddg_redirect(href)
            if real_url and title.strip() and "duckduckgo" not in real_url.lower():
                results.append({
                    "title": title.strip(),
                    "link": real_url,
                    "snippet": "",
                })
        
        if results:
            logger.info(f"DDG Lite: {len(results)} resultados para '{query}'")
        return results
        
    except Exception as e:
        logger.warning(f"DDG Lite error: {e}")
        return []


# ======================================================
# Main Endpoint
# ======================================================

@router.get("/web-search")
async def web_search(q: str = Query(..., description="Search query")):
    """
    Busca en internet.
    Intenta 4 métodos en orden: Google, DDGS library, HTML scraping, DDG Lite.
    Filtra resultados basura y enriquece snippets vacíos.
    """
    if not q or len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="La query debe tener al menos 2 caracteres")

    query = q.strip()
    search_query = _prepare_search_query(query)

    # Enriquecer query si necesita resultados frescos
    enhanced_query = search_query
    if needs_fresh_results(search_query):
        current_year = datetime.now().strftime("%Y")
        if current_year not in search_query:
            enhanced_query = f"{search_query} {current_year}"
            logger.info(f"Query enriquecida: '{search_query}' -> '{enhanced_query}'")

    # Intentar con los 4 métodos en orden de preferencia
    methods = [
        ("Google", search_with_google),
        ("DDGS library", search_with_ddgs),
        ("HTML scraping", search_with_html_scraping),
        ("DDG Lite", search_with_ddg_lite),
    ]

    for method_name, method_fn in methods:
        # Intentar primero con query enriquecida
        results = method_fn(enhanced_query)

        # Si no hay resultados y la query fue modificada, intentar con la original
        if not results and enhanced_query != search_query:
            logger.info(f"{method_name}: sin resultados con query enriquecida, intentando query preparada")
            results = method_fn(search_query)

        if not results and search_query != query:
            logger.info(f"{method_name}: sin resultados con query preparada, intentando original")
            results = method_fn(query)

        if results:
            # Post-processing pipeline
            results = _filter_junk_results(search_query, results)
            results = _rerank_results(query, results)
            results = _enrich_official_site_results(query, results)
            results = _enrich_result_snippets(results, max_enrich=3)
            logger.info(f"Web search exitosa via {method_name}: {len(results)} resultados")
            return {"query": query, "results": results}

    # Ningún método funcionó
    logger.warning(f"Web search: todos los métodos fallaron para '{query}'")
    return {
        "query": query,
        "results": [],
        "message": "No se encontraron resultados. Intente con otros términos de búsqueda."
    }
