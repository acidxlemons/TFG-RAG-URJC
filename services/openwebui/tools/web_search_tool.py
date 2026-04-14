"""
title: Web Search Tool
author: JARVIS Team
author_url: https://github.com/enterprise-rag
funding_url: https://github.com/enterprise-rag
version: 0.1.0
"""

import os
import requests
import json
from pydantic import BaseModel, Field
from typing import Optional

class Tools:
    class Valves(BaseModel):
        BACKEND_URL: str = Field(
            default="http://rag-backend:8000",
            description="URL base del backend RAG"
        )
        API_KEY: str = Field(
            default="",
            description="API Key si es necesaria (opcional)"
        )

    def __init__(self):
        self.valves = self.Valves()

    def search_web(self, query: str) -> str:
        """
        Busca información general en internet usando DuckDuckGo.
        Usa esta herramienta cuando el usuario pregunte por información actual, noticias, o datos que no estén en los documentos internos.
        Ejemplos: "Busca quién es el CEO de Microsoft", "Noticias sobre IA hoy", "Precio del bitcoin".
        
        :param query: La consulta de búsqueda (ej: "CEO de Microsoft")
        :return: Un resumen de los resultados encontrados.
        """
        print(f"Web Search Query: {query}")
        
        try:
            endpoint = f"{self.valves.BACKEND_URL}/web-search"
            payload = {"q": query}
            
            headers = {}
            if self.valves.API_KEY:
                headers["Authorization"] = f"Bearer {self.valves.API_KEY}"

            # GET request con query params
            response = requests.get(endpoint, params=payload, headers=headers, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            results = data.get("results", [])
            
            if not results:
                return "No se encontraron resultados en la web."
            
            # Formatear resultados para el LLM
            output = f"Resultados de búsqueda web para '{query}':\n\n"
            for i, res in enumerate(results, 1):
                output += f"Result {i}: {res.get('title')}\n"
                output += f"Link: {res.get('link')}\n"
                output += f"Snippet: {res.get('snippet')}\n\n"
                
            return output

        except Exception as e:
            return f"❌ Error al buscar en la web: {str(e)}"
