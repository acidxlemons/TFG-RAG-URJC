# backend/app/core/query_processor.py
"""
Query Processor - Procesamiento inteligente de queries

Este módulo mejora las queries del usuario antes de la búsqueda mediante:

1. Query Expansion (Expansión de Queries):
   - Genera variaciones de la query usando LLM
   - Mejora recall (encontrar más documentos relevantes)
   - Ejemplo: "ISO 9001" → "norma ISO 9001 certificación calidad"

2. Intent Detection (Detección de Intención):
   - Clasifica el tipo de pregunta del usuario
   - Permite adaptar la estrategia de búsqueda
   - Tipos: factual, procedural, analytical, conversational

3. Keyword Extraction (Extracción de Keywords):
   - Identifica términos clave en la query
   - Útil para filtrado y highlighting
   - Remueve stopwords automáticamente

4. Multi-Query Retrieval:
   - Busca con múltiples variaciones de la query
   - Fusiona y deduplica resultados
   - Mejora robustez ante queries ambiguas

Casos de uso:
- Query corta ("auditoría") → Expansión a "auditoría interna proceso requisitos"
- Query ambigua → Múltiples interpretaciones y búsqueda paralela
- Query técnica → Detección de intent para usar más keyword matching
"""

from __future__ import annotations

import re
import os
import logging
from typing import List, Dict, Optional, Set, Any
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class QueryIntent(str, Enum):
    """
    Tipos de intención de query
    
    Cada tipo sugiere una estrategia de búsqueda diferente:
    
    FACTUAL: Busca un hecho específico
        - Ejemplo: "¿Qué es ISO 9001?"
        - Estrategia: Priorizar dense search (semántica)
        - Top-k: Menor (3-5 resultados)
    
    PROCEDURAL: Busca cómo hacer algo
        - Ejemplo: "¿Cómo realizar una auditoría?"
        - Estrategia: Buscar documentos con pasos/procedimientos
        - Top-k: Mayor (5-10 resultados) para ver proceso completo
    
    ANALYTICAL: Requiere análisis/comparación
        - Ejemplo: "Diferencias entre ISO 9001 e ISO 14001"
        - Estrategia: Multi-query, buscar múltiples documentos
        - Top-k: Mayor (10-15) para análisis comprensivo
    
    CONVERSATIONAL: Chat general, no requiere RAG
        - Ejemplo: "Hola, ¿cómo estás?"
        - Estrategia: Responder directamente sin RAG
        - Top-k: 0 (sin búsqueda)
    """
    FACTUAL = "factual"
    PROCEDURAL = "procedural"
    ANALYTICAL = "analytical"
    CONVERSATIONAL = "conversational"


@dataclass
class ProcessedQuery:
    """
    Query procesada con información enriquecida
    
    Attributes:
        original: Query original del usuario
        expanded: Variaciones expandidas de la query
        keywords: Keywords extraídas
        intent: Intención detectada
        suggested_top_k: Top-k sugerido según intent
        suggested_strategy: Estrategia de búsqueda sugerida
    """
    original: str
    expanded: List[str]
    keywords: List[str]
    intent: QueryIntent
    suggested_top_k: int
    suggested_strategy: str
    
    def to_dict(self) -> Dict:
        """Convierte a diccionario"""
        return {
            "original": self.original,
            "expanded": self.expanded,
            "keywords": self.keywords,
            "intent": self.intent.value,
            "suggested_top_k": self.suggested_top_k,
            "suggested_strategy": self.suggested_strategy,
        }


class QueryProcessor:
    """
    Procesador inteligente de queries
    
    Mejora las queries del usuario antes de la búsqueda mediante:
    - Expansión con LLM
    - Detección de intención
    - Extracción de keywords
    
    Configuración:
    - enable_expansion: Activar expansión (requiere LLM, +latencia)
    - max_expansions: Máximo de variaciones a generar
    - min_query_length: Longitud mínima para procesar
    """
    
    # Stopwords en español (palabras a filtrar)
    SPANISH_STOPWORDS: Set[str] = {
        'el', 'la', 'de', 'que', 'y', 'a', 'en', 'un', 'ser', 'se', 'no',
        'haber', 'por', 'con', 'su', 'para', 'como', 'estar', 'tener',
        'le', 'lo', 'todo', 'pero', 'más', 'hacer', 'o', 'poder', 'decir',
        'este', 'ir', 'otro', 'ese', 'si', 'me', 'ya', 'ver', 'porque',
        'dar', 'cuando', 'él', 'muy', 'sin', 'vez', 'mucho', 'saber',
        'qué', 'sobre', 'mi', 'alguno', 'mismo', 'yo', 'también', 'hasta',
        'año', 'dos', 'querer', 'entre', 'así', 'primero', 'desde', 'grande',
        'eso', 'ni', 'nos', 'llegar', 'pasar', 'tiempo', 'ella', 'les',
        'tal', 'uno', 'es', 'son', 'del', 'los', 'las', 'al', 'una', 'unos', 'unas'
    }
    
    def __init__(
        self,
        llm_client: Optional[Any] = None,
        enable_expansion: bool = True,
        max_expansions: int = 3,
        min_query_length: int = 3,
        litellm_base_url: Optional[str] = None,
        litellm_api_key: Optional[str] = None,
        llm_model: Optional[str] = None,
    ):
        """
        Inicializa el query processor

        Args:
            llm_client: Cliente LLM para expansión (opcional, puede ser callable o dict)
            enable_expansion: Si habilitar query expansion
            max_expansions: Máximo número de expansiones a generar
            min_query_length: Longitud mínima de query para procesar
            litellm_base_url: URL de LiteLLM (fallback a LITELLM_URL env var)
            litellm_api_key: API key de LiteLLM (fallback a LITELLM_API_KEY env var)
            llm_model: Nombre del modelo (fallback a LLM_MODEL env var)
        """
        self.llm = llm_client
        self.max_expansions = max_expansions
        self.min_query_length = min_query_length

        # Configuración HTTP para LiteLLM (leída de env vars si no se pasan)
        self._litellm_url = (
            litellm_base_url
            or os.getenv("LITELLM_URL", "http://litellm:4000")
        ).rstrip("/")
        self._litellm_key = (
            litellm_api_key
            or os.getenv("LITELLM_API_KEY")
            or os.getenv("LITELLM_MASTER_KEY", "sk-1234")
        )
        self._llm_model = llm_model or os.getenv("LLM_MODEL", "JARVIS")

        # Activar expansión si hay cliente explícito O si LiteLLM está configurado
        litellm_available = bool(self._litellm_url and self._litellm_key)
        self.enable_expansion = enable_expansion and (llm_client is not None or litellm_available)
        self.max_expansions = max_expansions
        self.min_query_length = min_query_length

        if self.enable_expansion:
            logger.info(
                f"✓ Query expansion habilitado (model={self._llm_model}, url={self._litellm_url})"
            )
        else:
            logger.info("Query expansion deshabilitado (no LLM client)")
    
    def process(
        self,
        query: str,
        expand: Optional[bool] = None,
    ) -> ProcessedQuery:
        """
        Procesa una query completamente
        
        Args:
            query: Query del usuario
            expand: Override para habilitar/deshabilitar expansión
        
        Returns:
            ProcessedQuery con toda la información procesada
        """
        # Validar query
        query = query.strip()
        if len(query) < self.min_query_length:
            logger.warning(f"Query muy corta: '{query}'")
        
        # Detectar intención
        intent = self.detect_intent(query)
        
        # Extraer keywords
        keywords = self.extract_keywords(query)
        
        # Expandir query si está habilitado
        should_expand = expand if expand is not None else self.enable_expansion
        expanded = []
        if should_expand and intent != QueryIntent.CONVERSATIONAL:
            expanded = self.expand_query(query, num_variations=self.max_expansions)
        else:
            expanded = [query]  # Solo la query original
        
        # Sugerir parámetros de búsqueda según intent
        suggested_top_k, suggested_strategy = self._suggest_search_params(intent)
        
        result = ProcessedQuery(
            original=query,
            expanded=expanded,
            keywords=keywords,
            intent=intent,
            suggested_top_k=suggested_top_k,
            suggested_strategy=suggested_strategy,
        )
        
        logger.info(
            f"Query procesada: intent={intent.value}, "
            f"keywords={len(keywords)}, expansions={len(expanded)}"
        )
        
        return result
    
    def detect_intent(self, query: str) -> QueryIntent:
        """
        Detecta la intención de la query usando patrones regex
        
        Esta es una implementación basada en reglas (rule-based).
        Para mayor precisión, se podría usar un clasificador entrenado.
        
        Args:
            query: Query del usuario
        
        Returns:
            QueryIntent detectado
        """
        query_lower = query.lower()
        
        # Patrones para cada tipo de intent
        factual_patterns = [
            r'\bqu[eé] es\b',
            r'\bqu[eé] significa\b',
            r'\bcu[aá]ndo\b',
            r'\bcu[aá]nto\b',
            r'\bqui[eé]n\b',
            r'\bd[oó]nde\b',
            r'\bdefinici[oó]n de\b',
            r'\bdefine\b',
        ]
        
        procedural_patterns = [
            r'\bc[oó]mo\b',
            r'\bpasos para\b',
            r'\bproceso de\b',
            r'\bprocedimiento\b',
            r'\bgu[ií]a\b',
            r'\binstrucciones\b',
            r'\bmanual\b',
            r'\brealizar\b',
            r'\bhacer\b',
        ]
        
        analytical_patterns = [
            r'\bcompara\b',
            r'\bcomparaci[oó]n\b',
            r'\bdiferencia\b',
            r'\bventajas\b',
            r'\bdesventajas\b',
            r'\ban[aá]lisis\b',
            r'\bevaluaci[oó]n\b',
            r'\bversus\b',
            r'\bvs\b',
            r'\bentre .+ y\b',
        ]
        
        conversational_patterns = [
            r'^hola\b',
            r'^buenos d[ií]as\b',
            r'^buenas tardes\b',
            r'\bc[oó]mo est[aá]s\b',
            r'\bgracias\b',
            r'\badi[oó]s\b',
            r'\bhasta luego\b',
        ]
        
        # Evaluar patrones en orden de prioridad
        # (conversational primero para evitar false positives)
        if any(re.search(p, query_lower) for p in conversational_patterns):
            return QueryIntent.CONVERSATIONAL
        
        if any(re.search(p, query_lower) for p in analytical_patterns):
            return QueryIntent.ANALYTICAL
        
        if any(re.search(p, query_lower) for p in procedural_patterns):
            return QueryIntent.PROCEDURAL
        
        if any(re.search(p, query_lower) for p in factual_patterns):
            return QueryIntent.FACTUAL
        
        # Default: asumir factual si hay signos de interrogación, sino analytical
        if '?' in query:
            return QueryIntent.FACTUAL
        
        return QueryIntent.ANALYTICAL
    
    def extract_keywords(
        self,
        query: str,
        min_length: int = 3,
        max_keywords: int = 10,
    ) -> List[str]:
        """
        Extrae keywords relevantes de la query
        
        Proceso:
        1. Tokenización (separar palabras)
        2. Normalización (lowercase)
        3. Filtrado de stopwords
        4. Filtrado por longitud mínima
        5. Ordenar por relevancia (longitud, frecuencia)
        
        Args:
            query: Query del usuario
            min_length: Longitud mínima de keyword
            max_keywords: Máximo número de keywords a retornar
        
        Returns:
            Lista de keywords ordenadas por relevancia
        """
        # Tokenizar: extraer palabras (alfanuméricas)
        words = re.findall(r'\b\w+\b', query.lower())
        
        # Filtrar stopwords y palabras muy cortas
        keywords = [
            w for w in words
            if w not in self.SPANISH_STOPWORDS and len(w) >= min_length
        ]
        
        # Eliminar duplicados manteniendo orden
        seen = set()
        unique_keywords = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                unique_keywords.append(kw)
        
        # Ordenar por longitud (palabras más largas suelen ser más específicas)
        unique_keywords.sort(key=len, reverse=True)
        
        return unique_keywords[:max_keywords]
    
    def expand_query(
        self,
        query: str,
        num_variations: int = 3,
    ) -> List[str]:
        """
        Expande la query generando variaciones usando LLM
        
        La expansión ayuda a:
        - Mejorar recall (encontrar más documentos relevantes)
        - Manejar sinónimos y términos relacionados
        - Reformular queries ambiguas
        
        Ejemplo:
        Input: "requisitos ISO 9001"
        Output: [
            "requisitos ISO 9001",
            "norma ISO 9001 requisitos certificación",
            "documentación necesaria ISO 9001",
            "estándares calidad ISO 9001"
        ]
        
        Args:
            query: Query original
            num_variations: Número de variaciones a generar
        
        Returns:
            Lista de queries (original + variaciones)
        """
        # Verificar que haya alguna forma de llamar al LLM
        # (self.llm explícito O LiteLLM HTTP configurado)
        has_llm = self.llm is not None or bool(self._litellm_url and self._litellm_key)
        if not has_llm:
            logger.warning("No LLM disponible para expansión (ni cliente ni LiteLLM HTTP)")
            return [query]

        try:
            # Prompt para el LLM
            prompt = f"""Genera {num_variations} reformulaciones de la siguiente pregunta que ayuden a encontrar información relacionada en una base de datos de documentos empresariales.

Pregunta original: {query}

Instrucciones:
- Mantén el significado original
- Usa sinónimos y términos relacionados
- Sé conciso (máximo 15 palabras por reformulación)
- No numeres las reformulaciones
- Una reformulación por línea

Reformulaciones:"""

            # Llamar al LLM
            response = self._call_llm(prompt, max_tokens=200)

            if not response:
                logger.warning("LLM devolvió respuesta vacía para expansion")
                return [query]

            # Parsear respuesta — limpiar prefijos de lista ("1.", "-", "*", "•")
            import re as _re
            raw_lines = response.split('\n')
            variations = []
            for line in raw_lines:
                line = line.strip()
                # Quitar prefijos de numeración/lista
                line = _re.sub(r'^[\d]+[.)]\s*', '', line)
                line = _re.sub(r'^[-*•]\s*', '', line).strip()
                if line and len(line) > 10:
                    variations.append(line)

            # Filtrar variaciones válidas
            valid_variations = [
                v for v in variations
                if v.lower() != query.lower()  # No duplicar original
            ][:num_variations]

            # Incluir query original al principio
            result = [query] + valid_variations

            logger.info(
                f"Query expandida: '{query[:40]}' → {len(result)} variaciones"
            )
            return result

        except Exception as e:
            logger.error(f"Error expandiendo query: {e}")
            return [query]  # Fallback a query original
    
    def _call_llm(
        self,
        prompt: str,
        max_tokens: int = 200,
        temperature: float = 0.7,
    ) -> str:
        """
        Llama al LLM (wrapper genérico)
        
        Debes implementar esto según tu cliente LLM específico.
        Ejemplo para LiteLLM, OpenAI, etc.
        
        Args:
            prompt: Prompt para el LLM
            max_tokens: Tokens máximos a generar
            temperature: Temperatura (creatividad)
        
        Returns:
            Respuesta del LLM como string
        """
        # Opción 1: cliente LLM explícito pasado en el constructor
        if self.llm is not None:
            try:
                if callable(self.llm):
                    return self.llm(prompt)
                if hasattr(self.llm, "invoke"):
                    resp = self.llm.invoke(prompt)
                    return getattr(resp, "content", str(resp))
                if hasattr(self.llm, "completion"):
                    resp = self.llm.completion(
                        model=self._llm_model,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                    return resp.choices[0].message.content
            except Exception as e:
                logger.warning(f"Error usando llm_client explícito: {e}. Intentando LiteLLM HTTP.")

        # Opción 2: LiteLLM via HTTP
        try:
            import requests as _req
            response = _req.post(
                f"{self._litellm_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._litellm_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=15,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.warning(f"LiteLLM HTTP no disponible para query expansion: {e}")
            return ""
    
    def _suggest_search_params(
        self,
        intent: QueryIntent,
    ) -> tuple[int, str]:
        """
        Sugiere parámetros de búsqueda según el intent
        
        Diferentes intents requieren diferentes estrategias:
        
        FACTUAL: 
            - Top-k: Bajo (3-5) - Solo necesita la respuesta
            - Strategy: Dense - Búsqueda semántica precisa
        
        PROCEDURAL:
            - Top-k: Medio (5-10) - Necesita ver el proceso completo
            - Strategy: Hybrid - Combinar semántica + keywords
        
        ANALYTICAL:
            - Top-k: Alto (10-15) - Necesita múltiples documentos
            - Strategy: Hybrid - Máxima cobertura
        
        CONVERSATIONAL:
            - Top-k: 0 - No requiere búsqueda
            - Strategy: None - Responder directamente
        
        Args:
            intent: Intent detectado
        
        Returns:
            Tupla (top_k, strategy)
        """
        params_map = {
            QueryIntent.FACTUAL: (5, "dense"),
            QueryIntent.PROCEDURAL: (8, "hybrid"),
            QueryIntent.ANALYTICAL: (12, "hybrid"),
            QueryIntent.CONVERSATIONAL: (0, "none"),
        }
        
        return params_map.get(intent, (10, "hybrid"))


# ============================================
# MULTI-QUERY RETRIEVAL
# ============================================

class MultiQueryRetriever:
    """
    Retriever que usa múltiples variaciones de la query
    
    Proceso:
    1. Expandir query en variaciones
    2. Buscar con cada variación
    3. Fusionar resultados eliminando duplicados
    4. Reranker final
    
    Ventaja: Más robusto ante queries ambiguas
    Desventaja: Mayor latencia (múltiples búsquedas)
    """
    
    def __init__(
        self,
        base_retriever: Any,
        query_processor: QueryProcessor,
    ):
        """
        Args:
            base_retriever: HybridRetriever u otro retriever base
            query_processor: QueryProcessor para expansión
        """
        self.retriever = base_retriever
        self.processor = query_processor
    
    def search(
        self,
        query: str,
        collection_name: str,
        top_k: int = 10,
        **kwargs,
    ) -> List[Any]:
        """
        Búsqueda multi-query
        
        Args:
            query: Query original
            collection_name: Colección en Qdrant
            top_k: Resultados finales
            **kwargs: Argumentos adicionales para el retriever
        
        Returns:
            Lista de resultados deduplicados y fusionados
        """
        # Procesar query
        processed = self.processor.process(query, expand=True)
        
        # Si no hay expansiones, usar búsqueda normal
        if len(processed.expanded) == 1:
            return self.retriever.search(
                query=query,
                collection_name=collection_name,
                top_k=top_k,
                **kwargs
            )
        
        # Buscar con cada variación
        all_results = []
        for expanded_query in processed.expanded:
            results = self.retriever.search(
                query=expanded_query,
                collection_name=collection_name,
                top_k=top_k * 2,  # Obtener más para fusionar
                **kwargs
            )
            all_results.extend(results)
        
        # Deduplicar por ID
        seen_ids = set()
        unique_results = []
        for result in all_results:
            if result.id not in seen_ids:
                seen_ids.add(result.id)
                unique_results.append(result)
        
        # Reordenar por score y retornar top_k
        unique_results.sort(key=lambda x: x.score, reverse=True)
        
        logger.info(
            f"Multi-query: {len(processed.expanded)} queries → "
            f"{len(all_results)} resultados → {len(unique_results)} únicos"
        )
        
        return unique_results[:top_k]


# ============================================
# EJEMPLO DE USO
# ============================================

if __name__ == "__main__":
    """
    Ejemplos de uso del QueryProcessor
    """
    
    # Crear processor (sin LLM para el ejemplo)
    processor = QueryProcessor(
        llm_client=None,  # En producción, pasar cliente LLM real
        enable_expansion=False,
    )
    
    # Ejemplo 1: Query factual
    print("=== Ejemplo 1: Query Factual ===")
    result = processor.process("¿Qué es ISO 9001?")
    print(f"Intent: {result.intent.value}")
    print(f"Keywords: {result.keywords}")
    print(f"Sugerido: top_k={result.suggested_top_k}, strategy={result.suggested_strategy}\n")
    
    # Ejemplo 2: Query procedural
    print("=== Ejemplo 2: Query Procedural ===")
    result = processor.process("¿Cómo realizar una auditoría interna?")
    print(f"Intent: {result.intent.value}")
    print(f"Keywords: {result.keywords}")
    print(f"Sugerido: top_k={result.suggested_top_k}, strategy={result.suggested_strategy}\n")
    
    # Ejemplo 3: Query analytical
    print("=== Ejemplo 3: Query Analytical ===")
    result = processor.process("Diferencias entre ISO 9001 e ISO 14001")
    print(f"Intent: {result.intent.value}")
    print(f"Keywords: {result.keywords}")
    print(f"Sugerido: top_k={result.suggested_top_k}, strategy={result.suggested_strategy}\n")
    
    # Ejemplo 4: Conversational
    print("=== Ejemplo 4: Conversational ===")
    result = processor.process("Hola, ¿cómo estás?")
    print(f"Intent: {result.intent.value}")
    print(f"Sugerido: top_k={result.suggested_top_k}, strategy={result.suggested_strategy}\n")
