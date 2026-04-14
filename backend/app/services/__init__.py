# backend/app/services/__init__.py
"""
Servicios de lógica de negocio del sistema JARVIS RAG.

Este paquete contiene la lógica de negocio separada de los endpoints HTTP:
- mode_detector.py: Detección inteligente del modo de operación del chat.
- chat_service.py: Lógica de negocio del chat (scraping, web search, RAG).
- cache.py: Caché Redis para resultados de búsqueda.

Separar servicios de endpoints permite:
1. Testear la lógica de negocio sin necesitar un servidor HTTP.
2. Reutilizar funciones entre diferentes endpoints.
3. Mantener los endpoints limpios y enfocados en HTTP.
"""
