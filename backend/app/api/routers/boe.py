from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from app.integrations.boe_connector import BoeConnector
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

# Instancia global del conector
boe = BoeConnector()


# ======================================================
# Pydantic Models
# ======================================================

class BoeResult(BaseModel):
    title: str
    link: str
    summary: Optional[str] = None
    date: Optional[str] = None
    source: Optional[str] = None

class BoeResponse(BaseModel):
    query: str
    results: List[Dict[str, Any]]
    count: int

class BoeSearchPayload(BaseModel):
    """Payload para búsqueda BOE desde Jarvis (POST)."""
    mode: str = "search"  # "search" | "summary"
    query: Optional[str] = ""
    days_back: int = 30

class BoeLawPayload(BaseModel):
    """Payload para obtener texto de ley desde Jarvis (POST)."""
    law_name: str
    article: Optional[str] = None

class BoeLawAnalysisPayload(BaseModel):
    """Payload para análisis jurídico desde Jarvis (POST)."""
    law_name: str


# ======================================================
# GET endpoints (API directa / Swagger)
# ======================================================

@router.get("/search", response_model=BoeResponse)
async def search_legislation(
    q: str = Query(..., description="Keywords to search"),
    days: int = Query(30, description="Days to look back")
):
    """
    Busca legislación en los sumarios del BOE de los últimos días.
    """
    try:
        results = boe.search_legislation(q, days)
        return {
            "query": q,
            "results": results,
            "count": len(results)
        }
    except Exception as e:
        logger.error(f"Error searching BOE: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/tenders", response_model=BoeResponse)
async def search_tenders(
    q: str = Query(..., description="Keywords to search in tenders"),
    days: int = Query(30, description="Days to look back")
):
    """
    Busca licitaciones (Sección V) en los sumarios del BOE.
    """
    try:
        results = boe.search_tenders(q, days)
        return {
            "query": q,
            "results": results,
            "count": len(results)
        }
    except Exception as e:
        logger.error(f"Error searching BOE tenders: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ======================================================
# POST endpoints (llamados por Jarvis pipeline)
# ======================================================

@router.post("")
@router.post("/")
async def boe_search_post(payload: BoeSearchPayload):
    """
    POST endpoint para Jarvis. Soporta mode=search y mode=summary.
    Jarvis llama a POST /external/boe con JSON {"mode": "search|summary", "query": "..."}
    """
    try:
        if payload.mode == "summary":
            items = boe.get_summary()
            return {
                "results": items,
                "count": len(items),
                "mode": "summary"
            }
        elif payload.mode == "tenders":
            results = boe.search_tenders(payload.query or "", payload.days_back)
            return {
                "query": payload.query,
                "results": results,
                "count": len(results),
                "mode": "tenders"
            }
        else:
            results = boe.search_legislation(payload.query or "", payload.days_back)
            return {
                "query": payload.query,
                "results": results,
                "count": len(results),
                "mode": "search"
            }
    except Exception as e:
        logger.error(f"Error in BOE search POST: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/law")
async def get_law_post(payload: BoeLawPayload):
    """
    POST endpoint para Jarvis. Obtiene texto de una ley por nombre.
    Jarvis llama a POST /external/boe/law con JSON {"law_name": "LOPD", "article": "5"}
    """
    try:
        law_id = boe.resolve_law_id(payload.law_name)
        if not law_id:
            raise HTTPException(
                status_code=404, 
                detail=f"No se encontró ley con nombre '{payload.law_name}'"
            )
        
        result = boe.get_law_text(law_id)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        
        # Si pide artículo específico, extraer solo esa parte
        if payload.article and result.get("text"):
            article_text = _extract_article(result["text"], payload.article)
            if article_text:
                result["article_number"] = payload.article
                result["article_text"] = article_text
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting law: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/law/analysis")
async def law_analysis_post(payload: BoeLawAnalysisPayload):
    """
    POST endpoint para Jarvis. Análisis jurídico de una ley.
    Jarvis llama a POST /external/boe/law/analysis con JSON {"law_name": "LOPD"}
    """
    try:
        law_id = boe.resolve_law_id(payload.law_name)
        if not law_id:
            raise HTTPException(
                status_code=404, 
                detail=f"No se encontró ley con nombre '{payload.law_name}'"
            )
        
        result = boe.get_law_analysis(law_id)
        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])
        
        # Añadir metadatos de la ley
        metadata = boe.get_law_metadata(law_id)
        result["title"] = metadata.get("title", payload.law_name)
        result["link"] = metadata.get("link", "")
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in law analysis: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ======================================================
# Helpers
# ======================================================

def _extract_article(full_text: str, article_number: str) -> Optional[str]:
    """Extrae un artículo específico del texto completo de una ley."""
    import re
    
    # Buscar "Artículo X." o "Artículo X " 
    pattern = rf"(?:Artículo|ARTÍCULO|Art\.?)\s*{re.escape(article_number)}[\.\s]"
    match = re.search(pattern, full_text, re.IGNORECASE)
    
    if not match:
        return None
    
    start = match.start()
    
    # Buscar el siguiente artículo para delimitar el final
    next_pattern = rf"(?:Artículo|ARTÍCULO|Art\.?)\s*\d+[\.\s]"
    next_match = re.search(next_pattern, full_text[match.end():], re.IGNORECASE)
    
    if next_match:
        end = match.end() + next_match.start()
    else:
        # Tomar hasta 2000 caracteres si no hay siguiente artículo
        end = min(start + 2000, len(full_text))
    
    return full_text[start:end].strip()
