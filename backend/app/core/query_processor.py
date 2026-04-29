"""
backend/app/core/query_processor.py

Procesamiento inteligente de queries para el pipeline RAG.

1. Intent Detection — clasifica la query en FACTUAL / PROCEDURAL / ANALYTICAL / CONVERSATIONAL
   y sugiere top_k y estrategia de búsqueda adaptados a cada tipo.

2. Query Expansion — genera variaciones de la query via LLM para mejorar el recall.
   Ejemplo: "normas escaleras" → +2 reformulaciones con sinónimos técnicos.

3. Smart Model Routing — elige el modelo LLM adecuado para generar la respuesta final:
   - JARVIS  (rag-qwen-ft:latest): consultas simples/factuales — rápido, formato de citas
   - qwen2.5-32b: consultas analíticas/complejas — mayor contexto y razonamiento

4. Multi-Query Retrieval — combina resultados de todas las variaciones y deduplica.
"""

from __future__ import annotations

import re
import os
import logging
from typing import List, Dict, Optional, Set, Any, Tuple
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class QueryIntent(str, Enum):
    """
    Intención de la query.

    FACTUAL       → hecho puntual;         top_k=5,  strategy=dense,  model=JARVIS
    PROCEDURAL    → cómo hacer algo;        top_k=8,  strategy=hybrid, model=JARVIS
    ANALYTICAL    → análisis/comparación;   top_k=12, strategy=hybrid, model=qwen2.5-32b
    CONVERSATIONAL → saludo/chat general;   top_k=0,  strategy=none,   model=JARVIS
    """
    FACTUAL = "factual"
    PROCEDURAL = "procedural"
    ANALYTICAL = "analytical"
    CONVERSATIONAL = "conversational"


@dataclass
class ProcessedQuery:
    original: str
    expanded: List[str]
    keywords: List[str]
    intent: QueryIntent
    suggested_top_k: int
    suggested_strategy: str
    suggested_model: str          # alias LiteLLM a usar para generar la respuesta

    def to_dict(self) -> Dict:
        return {
            "original": self.original,
            "expanded": self.expanded,
            "keywords": self.keywords,
            "intent": self.intent.value,
            "suggested_top_k": self.suggested_top_k,
            "suggested_strategy": self.suggested_strategy,
            "suggested_model": self.suggested_model,
        }


class QueryProcessor:
    """
    Procesador inteligente de queries.

    Parámetros clave:
      enable_expansion   — activar expansión LLM (+latencia, mejor recall)
      max_expansions     — variaciones adicionales a generar (default 2)
      primary_model      — alias LiteLLM para queries simples  (default: env LLM_MODEL → JARVIS)
      analytical_model   — alias LiteLLM para queries analíticas (default: qwen2.5-32b)
    """

    SPANISH_STOPWORDS: Set[str] = {
        'el', 'la', 'de', 'que', 'y', 'a', 'en', 'un', 'ser', 'se', 'no',
        'haber', 'por', 'con', 'su', 'para', 'como', 'estar', 'tener',
        'le', 'lo', 'todo', 'pero', 'más', 'hacer', 'o', 'poder', 'decir',
        'este', 'ir', 'otro', 'ese', 'si', 'me', 'ya', 'ver', 'porque',
        'dar', 'cuando', 'él', 'muy', 'sin', 'vez', 'mucho', 'saber',
        'qué', 'sobre', 'mi', 'alguno', 'mismo', 'yo', 'también', 'hasta',
        'año', 'dos', 'querer', 'entre', 'así', 'primero', 'desde', 'grande',
        'eso', 'ni', 'nos', 'llegar', 'pasar', 'tiempo', 'ella', 'les',
        'tal', 'uno', 'es', 'son', 'del', 'los', 'las', 'al', 'una', 'unos', 'unas',
    }

    def __init__(
        self,
        enable_expansion: bool = True,
        max_expansions: int = 2,
        min_query_length: int = 3,
        litellm_base_url: Optional[str] = None,
        litellm_api_key: Optional[str] = None,
        primary_model: Optional[str] = None,
        analytical_model: Optional[str] = None,
    ):
        self._litellm_url = (litellm_base_url or os.getenv("LITELLM_URL", "http://litellm:4000")).rstrip("/")
        self._litellm_key = (
            litellm_api_key
            or os.getenv("LITELLM_API_KEY")
            or os.getenv("LITELLM_MASTER_KEY", "sk-1234")
        )
        self._primary_model = primary_model or os.getenv("LLM_MODEL", "JARVIS")
        self._analytical_model = analytical_model or os.getenv("ANALYTICAL_MODEL", "qwen2.5-32b")

        self.enable_expansion = enable_expansion and bool(self._litellm_url and self._litellm_key)
        self.max_expansions = max_expansions
        self.min_query_length = min_query_length

        if self.enable_expansion:
            logger.info(
                f"✓ QueryProcessor listo "
                f"(primary={self._primary_model}, analytical={self._analytical_model}, "
                f"expansion=ON, max_expansions={max_expansions})"
            )
        else:
            logger.info(f"✓ QueryProcessor listo (expansion=OFF, primary={self._primary_model})")

    # ── API pública ───────────────────────────────────────────

    def process(self, query: str, expand: Optional[bool] = None) -> ProcessedQuery:
        """
        Procesa una query completamente:
          1. Detecta intención
          2. Extrae keywords
          3. Sugiere parámetros de búsqueda y modelo LLM
          4. Expande la query si está habilitado
        """
        query = query.strip()
        intent = self.detect_intent(query)
        keywords = self.extract_keywords(query)
        suggested_top_k, suggested_strategy = self._suggest_search_params(intent)
        suggested_model = self._suggest_model(intent)

        should_expand = (expand if expand is not None else self.enable_expansion)
        if should_expand and intent != QueryIntent.CONVERSATIONAL and len(query) >= self.min_query_length:
            expanded = self.expand_query(query)
        else:
            expanded = [query]

        logger.info(
            f"QueryProcessor: intent={intent.value}, model={suggested_model}, "
            f"top_k={suggested_top_k}, expansions={len(expanded)}"
        )
        return ProcessedQuery(
            original=query,
            expanded=expanded,
            keywords=keywords,
            intent=intent,
            suggested_top_k=suggested_top_k,
            suggested_strategy=suggested_strategy,
            suggested_model=suggested_model,
        )

    def detect_intent(self, query: str) -> QueryIntent:
        """Clasifica la intención de la query usando patrones regex."""
        q = query.lower()

        conversational = [
            r'^hola\b', r'^buenos d[ií]as\b', r'^buenas tardes\b',
            r'\bc[oó]mo est[aá]s\b', r'\bgracias\b', r'\badi[oó]s\b', r'\bhasta luego\b',
        ]
        analytical = [
            r'\bcompara\b', r'\bcomparaci[oó]n\b', r'\bdiferencia\b',
            r'\bventajas\b', r'\bdesventajas\b', r'\ban[aá]lisis\b',
            r'\bevaluaci[oó]n\b', r'\bversus\b', r'\bvs\b', r'\bentre .+ y\b',
            r'\bresume\b', r'\bresumen\b', r'\bexplica\b', r'\bdetalla\b',
        ]
        procedural = [
            r'\bc[oó]mo\b', r'\bpasos para\b', r'\bproceso de\b',
            r'\bprocedimiento\b', r'\bgu[ií]a\b', r'\binstrucciones\b',
            r'\brealizar\b', r'\bhacer\b',
        ]
        factual = [
            r'\bqu[eé] es\b', r'\bqu[eé] significa\b', r'\bcu[aá]ndo\b',
            r'\bcu[aá]nto\b', r'\bqui[eé]n\b', r'\bd[oó]nde\b',
            r'\bdefinici[oó]n de\b', r'\bdefine\b',
        ]

        if any(re.search(p, q) for p in conversational):
            return QueryIntent.CONVERSATIONAL
        if any(re.search(p, q) for p in analytical):
            return QueryIntent.ANALYTICAL
        if any(re.search(p, q) for p in procedural):
            return QueryIntent.PROCEDURAL
        if any(re.search(p, q) for p in factual):
            return QueryIntent.FACTUAL
        return QueryIntent.FACTUAL if '?' in query else QueryIntent.ANALYTICAL

    def extract_keywords(self, query: str, min_length: int = 3, max_keywords: int = 10) -> List[str]:
        """Extrae términos clave filtrando stopwords."""
        words = re.findall(r'\b\w+\b', query.lower())
        keywords = [w for w in words if w not in self.SPANISH_STOPWORDS and len(w) >= min_length]
        seen: Set[str] = set()
        unique: List[str] = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                unique.append(kw)
        unique.sort(key=len, reverse=True)
        return unique[:max_keywords]

    def expand_query(self, query: str) -> List[str]:
        """
        Genera variaciones de la query via LiteLLM para ampliar recall.

        Usa JARVIS (modelo primario) porque es rápido y suficiente para reformular.
        Timeout corto (12 s) para no bloquear el pipeline si LiteLLM está ocupado.
        """
        try:
            import requests as _req
            prompt = (
                f"Genera {self.max_expansions} reformulaciones breves (máx. 15 palabras) "
                f"de la siguiente pregunta para buscar en documentos empresariales.\n"
                f"Usa sinónimos y términos técnicos relacionados. Una por línea. Sin numeración.\n\n"
                f"Pregunta: {query}\n\nReformulaciones:"
            )
            resp = _req.post(
                f"{self._litellm_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._litellm_key}", "Content-Type": "application/json"},
                json={
                    "model": self._primary_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 150,
                    "temperature": 0.7,
                },
                timeout=12,
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"]
            lines: List[str] = []
            for line in raw.split("\n"):
                line = re.sub(r'^[\d]+[.)]\s*', '', line.strip())
                line = re.sub(r'^[-*•]\s*', '', line).strip()
                if line and len(line) > 8 and line.lower() != query.lower():
                    lines.append(line)
            variations = lines[:self.max_expansions]
            result = [query] + variations
            logger.info(f"Query expandida: '{query[:50]}' → {len(result)} variaciones")
            return result
        except Exception as e:
            logger.warning(f"Query expansion falló (usando solo original): {e}")
            return [query]

    # ── Helpers privados ──────────────────────────────────────

    def _suggest_search_params(self, intent: QueryIntent) -> Tuple[int, str]:
        return {
            QueryIntent.FACTUAL:        (5,  "dense"),
            QueryIntent.PROCEDURAL:     (8,  "hybrid"),
            QueryIntent.ANALYTICAL:     (12, "hybrid"),
            QueryIntent.CONVERSATIONAL: (0,  "none"),
        }.get(intent, (8, "hybrid"))

    def _suggest_model(self, intent: QueryIntent) -> str:
        """
        Smart model routing:
        - ANALYTICAL → qwen2.5-32b (32 K contexto, razonamiento superior)
        - resto       → JARVIS      (fine-tuned, rápido, formato de citas correcto)
        """
        if intent == QueryIntent.ANALYTICAL:
            return self._analytical_model
        return self._primary_model


class MultiQueryRetriever:
    """
    Wrapper que expande una query via QueryProcessor y fusiona resultados de múltiples variaciones.

    Encapsula la lógica multi-query para el endpoint /search/multi-query:
      1. Expande la query con QueryProcessor.expand_query()
      2. Ejecuta búsqueda con cada variación en el retriever base
      3. Deduplica por ID y retorna los top-k mejores por score
    """

    def __init__(self, base_retriever: Any, query_processor: QueryProcessor):
        self.retriever = base_retriever
        self.processor = query_processor

    def search(
        self,
        query: str,
        collection_name: str,
        top_k: int = 5,
        tenant_id: Optional[str] = None,
        strategy: str = "hybrid",
        use_reranking: bool = True,
    ) -> List[Any]:
        """Busca con query + variaciones expandidas y devuelve top-k deduplicados."""
        queries = self.processor.expand_query(query)
        seen_ids: Set[str] = set()
        all_results: List[Any] = []

        for q in queries:
            try:
                results = self.retriever.search(
                    query=q,
                    collection_name=collection_name,
                    top_k=top_k,
                    tenant_id=tenant_id,
                    strategy=strategy,
                    use_reranking=use_reranking,
                )
                for r in results:
                    rid = str(r.id)
                    if rid not in seen_ids:
                        seen_ids.add(rid)
                        all_results.append(r)
            except Exception as e:
                logger.warning(f"MultiQueryRetriever: variación '{q[:40]}' falló: {e}")

        all_results.sort(key=lambda r: r.score, reverse=True)
        return all_results[:top_k]
