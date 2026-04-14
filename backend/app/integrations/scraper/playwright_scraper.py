"""
backend/app/integrations/scraper/playwright_scraper.py
Web Scraper con Playwright y múltiples estrategias de extracción.

Features:
- Renderizado JavaScript con Chromium headless
- Retry con backoff exponencial
- Espera inteligente para SPAs
- Rotación de User-Agents
- Verificación de robots.txt (opcional)
"""

from __future__ import annotations

import logging
import random
import asyncio
import urllib.robotparser
from typing import Optional, Dict, List
from urllib.parse import urlparse
from datetime import datetime

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from .content_extractor import ContentExtractor

logger = logging.getLogger(__name__)

# User agents reales para evitar bloqueos
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]


class WebScraper:
    """
    Scraper web robusto con Playwright.

    Características:
    - Renderiza JavaScript (SPAs, React, Vue, etc.)
    - Extrae contenido limpio con múltiples estrategias
    - Retry automático con backoff exponencial
    - Rotación de user-agents
    """

    def __init__(
        self,
        user_agent: Optional[str] = None,
        respect_robots: bool = False,  # Desactivado por defecto para más flexibilidad
        timeout: int = 30000,  # ms
        max_retries: int = 3,
    ):
        self.user_agent = user_agent or random.choice(USER_AGENTS)
        self.respect_robots = respect_robots
        self.timeout = timeout
        self.max_retries = max_retries

        self.robots_cache: Dict[str, urllib.robotparser.RobotFileParser] = {}
        self.extractor = ContentExtractor()
        
        logger.info(f"WebScraper inicializado (timeout={timeout}ms, retries={max_retries})")

    async def scrape(self, url: str) -> Optional[Dict]:
        """
        Scrapea y extrae contenido limpio de una URL.
        
        Returns:
            Dict con: url, title, author, date, description, content, 
                      word_count, scraped_at, status_code, extraction_method
            o None si falla.
        """
        logger.info(f"🔍 Scrapeando: {url}")

        # Verificar robots.txt si está habilitado
        if self.respect_robots and not await self._can_fetch(url):
            logger.warning(f"robots.txt prohíbe scrapear: {url}")
            return None

        # Intentar con retry
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                result = await self._scrape_with_playwright(url, attempt)
                if result:
                    return result
            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    wait_time = 2 ** attempt  # Backoff exponencial: 2, 4, 8 segundos
                    logger.warning(f"Intento {attempt} falló: {e}. Reintentando en {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    # Rotar user-agent en cada reintento
                    self.user_agent = random.choice(USER_AGENTS)
                else:
                    logger.error(f"Scraping falló después de {self.max_retries} intentos: {e}")
        
        return None

    async def _scrape_with_playwright(self, url: str, attempt: int = 1) -> Optional[Dict]:
        """Ejecuta el scraping con Playwright."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )
            
            try:
                context = await browser.new_context(
                    user_agent=self.user_agent,
                    viewport={"width": 1920, "height": 1080},
                    locale="es-ES",
                    timezone_id="Europe/Madrid",
                )
                
                page = await context.new_page()
                
                # Bloquear recursos innecesarios para acelerar
                await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,ico}", lambda route: route.abort())
                await page.route("**/*google-analytics*/**", lambda route: route.abort())
                await page.route("**/*doubleclick*/**", lambda route: route.abort())
                await page.route("**/*facebook*/**", lambda route: route.abort())
                
                # Navegar a la URL
                response = await page.goto(
                    url, 
                    timeout=self.timeout, 
                    wait_until="domcontentloaded"
                )
                
                if not response:
                    logger.error(f"No response from {url}")
                    return None
                
                status_code = response.status
                
                # Manejar errores HTTP
                if status_code >= 400:
                    logger.error(f"HTTP {status_code} para {url}")
                    if status_code in [403, 429]:
                        # Posible bloqueo, lanzar para retry
                        raise Exception(f"HTTP {status_code} - posible bloqueo")
                    return None
                
                # Esperar a que la página esté lista (importante para SPAs)
                await self._wait_for_page_ready(page)
                
                # Obtener HTML renderizado
                html = await page.content()
                
                # Extraer contenido
                extracted = self.extractor.extract_from_html(html, url=url)
                
                if not extracted or not extracted.get("content"):
                    logger.warning(f"No se pudo extraer contenido de: {url}")
                    return None
                
                # Extraer enlaces internos (href) para el crawler recursivo
                _links = await page.evaluate("""
                    () => {
                        const links = Array.from(document.querySelectorAll('a[href]'));
                        return links.map(a => a.href);
                    }
                """)
                
                # Construir resultado
                result = {
                    "url": url,
                    "title": extracted.get("title") or await self._get_page_title(page),
                    "author": extracted.get("author"),
                    "date": extracted.get("date"),
                    "description": extracted.get("description"),
                    "content": extracted["content"],
                    "links": _links, # NUEVO: Enlaces encontrados
                    "word_count": extracted.get("word_count", len(extracted["content"].split())),
                    "char_count": extracted.get("char_count", len(extracted["content"])),
                    "scraped_at": datetime.utcnow().isoformat() + "Z",
                    "status_code": status_code,
                    "extraction_method": extracted.get("extraction_method", "unknown"),
                }
                
                logger.info(
                    f"✅ Scrapeado: {url} -> '{result['title'][:50] if result['title'] else 'sin título'}' "
                    f"({result['word_count']} palabras, {len(_links)} enlaces, método: {result['extraction_method']})"
                )
                
                return result

            except PlaywrightTimeout as e:
                logger.error(f"Timeout scrapeando {url}: {e}")
                raise
            except Exception as e:
                logger.error(f"Error scrapeando {url}: {e}")
                raise
            finally:
                await browser.close()

    async def _wait_for_page_ready(self, page) -> None:
        """
        Espera inteligente para que la página esté completamente cargada.
        Importante para SPAs (React, Vue, Angular, etc.)
        """
        try:
            # 1. Esperar a network idle (casi sin requests pendientes)
            await page.wait_for_load_state('networkidle', timeout=10000)
        except PlaywrightTimeout:
            pass  # Si timeout, continuar de todas formas
        
        try:
            # 2. Esperar un poco más para JavaScript dinámico
            await page.wait_for_timeout(500)
            
            # 3. Esperar a que el body tenga contenido
            await page.wait_for_function(
                "document.body && document.body.innerText.length > 100",
                timeout=5000
            )
        except PlaywrightTimeout:
            pass  # Continuar si timeout
        except Exception:
            pass
    
    async def _get_page_title(self, page) -> Optional[str]:
        """Obtiene el título de la página."""
        try:
            return await page.title()
        except Exception:
            return None

    async def _can_fetch(self, url: str) -> bool:
        """Verifica robots.txt para el user-agent configurado."""
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        if base_url in self.robots_cache:
            rp = self.robots_cache[base_url]
        else:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"{base_url}/robots.txt")
            try:
                rp.read()
                self.robots_cache[base_url] = rp
            except Exception as e:
                logger.debug(f"No se pudo leer robots.txt de {base_url}: {e}")
                return True  # Si no hay robots.txt, permitir

        allowed = rp.can_fetch(self.user_agent, url)
        if not allowed:
            logger.info(f"robots.txt prohíbe: {url}")
        return allowed

    def scrape_sync(self, url: str) -> Optional[Dict]:
        """Versión síncrona del scraper."""
        return asyncio.run(self.scrape(url))
