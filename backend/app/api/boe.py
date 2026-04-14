from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional
from app.integrations.boe_connector import BoeConnector

router = APIRouter(prefix="/boe")
client = BoeConnector()

@router.get("/summary", summary="Obtiene el sumario del BOE")
async def get_summary(date: Optional[str] = Query(None, description="Fecha en formato YYYYMMDD")):
    """
    Obtiene el listado de disposiciones del BOE para una fecha dada.
    """
    return client.get_summary(date)

@router.get("/search", summary="Busca legislación en el BOE")
async def search_legislation(
    q: str = Query(..., description="Términos de búsqueda"),
    days: int = Query(7, description="Días hacia atrás para buscar")
):
    """
    Busca legislación en los sumarios del BOE de los últimos días.
    """
    results = client.search_legislation(q, days_back=days)
    if not results:
        return {"message": "No se encontraron resultados", "results": []}
    return {"results": results}

@router.get("/tenders", summary="Busca licitaciones en el BOE")
async def search_tenders(
    q: str = Query(..., description="Términos de búsqueda"),
    days: int = Query(7, description="Días hacia atrás para buscar")
):
    """
    Busca licitaciones (Sección V) en los sumarios del BOE de los últimos días.
    """
    results = client.search_tenders(q, days_back=days)
    if not results:
        return {"message": "No se encontraron licitaciones", "results": []}
    return {"results": results}

@router.get("/law/{law_id}", summary="Obtiene texto completo de una ley")
async def get_law_text(law_id: str):
    """
    Obtiene el texto consolidado de una norma por su ID (ej: BOE-A-2018-16673).
    """
    result = client.get_law_text(law_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result

@router.get("/resolve/{name}", summary="Resuelve nombre común de ley a ID")
async def resolve_law(name: str):
    """
    Resuelve nombres como 'LOPD', 'Estatuto Trabajadores' a su ID BOE.
    """
    law_id = client.resolve_law_id(name)
    if not law_id:
        raise HTTPException(status_code=404, detail=f"No se encontró ley con nombre '{name}'")
    return {"name": name, "law_id": law_id, "link": f"https://www.boe.es/buscar/act.php?id={law_id}"}
