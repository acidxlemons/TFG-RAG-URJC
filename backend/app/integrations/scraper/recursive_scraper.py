# backend/app/integrations/scraper/recursive_scraper.py
"""
Scraper Recursivo (Spider/Crawler)
Permite navegar y extraer contenido de múltiples páginas enlazadas dentro de un mismo dominio.
"""

from __future__ import annotations

import logging
import asyncio
from typing import Set, List, Dict, Optional
from urllib.parse import urlparse, urljoin
import re

# Reutilizamos el WebScraper existente para extraer cada página individual
from .playwright_scraper import WebScraper

logger = logging.getLogger(__name__)

class RecursiveWebScraper:
    """
    Crawler que navega recursivamente siguiendo enlaces internos.
    
    Características:
    - Respeta el dominio original (no sale del sitio).
    - Evita ciclos (mantiene registro de visitados).
    - Límite de profundidad (depth) y total de páginas (max_pages).
    - Extracción asíncrona.
    """

    def __init__(
        self,
        start_url: str,
        max_depth: int = 1,
        max_pages: int = 5,
        concurrency: int = 1
    ):
        self.start_url = start_url
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.concurrency = concurrency
        
        # Estado del crawler
        self.visited: Set[str] = set()
        self.results: List[Dict] = []
        self.base_domain = urlparse(start_url).netloc
        
        # Scraper individual (singleton-ish)
        self.scraper = WebScraper(
            timeout=30000, 
            respect_robots=False # Opcional: activar si se desea ser muy estricto
        )

    async def crawl(self) -> List[Dict]:
        """
        Inicia el proceso de crawling.
        Retorna la lista de resultados (páginas scrapeadas).
        """
        logger.info(f"🕸️ Iniciando crawl recursivo: {self.start_url} (depth={self.max_depth}, max={self.max_pages})")
        
        # Cola de trabajo: (url, profundidad_actual)
        queue = asyncio.Queue()
        queue.put_nowait((self.start_url, 0))
        self.visited.add(self.start_url)
        
        # Workers
        workers = [asyncio.create_task(self._worker(queue)) for _ in range(self.concurrency)]
        
        # Esperar a que la cola se vacíe
        await queue.join()
        
        # Cancelar workers
        for w in workers:
            w.cancel()
            
        logger.info(f"🕸️ Crawl finalizado. Páginas extraídas: {len(self.results)}")
        return self.results

    async def _worker(self, queue: asyncio.Queue):
        """Worker que procesa URLs de la cola."""
        while True:
            # Si ya alcanzamos el límite, vaciamos la cola pendiente y terminamos el worker.
            if len(self.results) >= self.max_pages:
                drained = 0
                while True:
                    try:
                        queue.get_nowait()
                        queue.task_done()
                        drained += 1
                    except asyncio.QueueEmpty:
                        break
                if drained:
                    logger.info(f"🧹 Worker drenó {drained} URLs pendientes al alcanzar max_pages={self.max_pages}")
                return

            try:
                url, depth = await queue.get()
            except asyncio.QueueEmpty:
                break
                
            try:
                if len(self.results) >= self.max_pages:
                    queue.task_done()
                    continue

                logger.info(f"🕷️ Procesando (d={depth}): {url}")
                
                # 1. Scrapear página
                page_data = await self.scraper.scrape(url)
                
                if page_data:
                    self.results.append(page_data)
                    
                    # 2. Si no alcanzamos profundidad máxima, buscar enlaces
                    if depth < self.max_depth:
                        links = self._extract_internal_links(page_data.get("content", ""), url)
                        # También podríamos extraer del HTML raw si el extractor lo guardara, 
                        # pero por simplicidad buscamos en el texto/markdown extraído o 
                        # idealmente el scraper debería devolver los hrefs.
                        
                        # NOTA: playwrigth_scraper.py devuelve 'content' limpio (texto). 
                        # Para un crawler real, necesitamos los hrefs del HTML original.
                        # Como workaround sin modificar mucho el scraper base:
                        # Vamos a asumir que el scraper NO devuelve los links, así que 
                        # tendremos que hacer una modificación ligera o inferir.
                        
                        # MEJORA: Para no romper el scraper actual, intentaremos 
                        # extraer URLs del texto si es Markdown, O BIEN (mejor opción),
                        # modificar el scraper base para que devuelva 'links'.
                        # Por ahora, usaremos una regex simple sobre el contenido si parece tener links,
                        # pero lo ideal es modificar el PlaywrightScraper. 
                        # Vamos a modificar PlaywrightScraper para devolver 'links' extraídos.
                        
                        found_links = page_data.get("links", []) 
                        
                        for link in found_links:
                            if link not in self.visited and self._is_internal(link):
                                if len(self.results) + queue.qsize() < self.max_pages: # Check aproximado
                                    self.visited.add(link)
                                    queue.put_nowait((link, depth + 1))

            except Exception as e:
                logger.error(f"Error en worker con {url}: {e}")
            finally:
                queue.task_done()

    def _is_internal(self, url: str) -> bool:
        """Verifica si la URL pertenece al mismo dominio base."""
        try:
            parsed = urlparse(url)
            # Aceptar dominios exactos o subdominios si se desea (aquí estricto al netloc)
            return parsed.netloc == self.base_domain
        except Exception:
            return False

    def _extract_internal_links(self, content: str, base_url: str) -> List[str]:
        """
        Helper legacy si el scraper no devolviera links.
        (Se espera que modifiquemos playwright_scraper para devolver 'links')
        """
        return []
