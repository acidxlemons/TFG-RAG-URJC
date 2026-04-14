import logging
from typing import Optional
from fastapi import APIRouter, Header, Body, HTTPException
from pydantic import BaseModel
from app.core.state import app_state
from app.core.permissions import resolve_authorized_collections

router = APIRouter(tags=["Search"])
logger = logging.getLogger(__name__)

class SearchRequest(BaseModel):
    """Request para búsqueda directa"""
    query: str
    top_k: int = 5
    filter_by_filename: Optional[str] = None
    exclude_ocr: bool = False

@router.post("/search")
async def search_documents(
    payload: dict = Body(...),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-Id"),
    x_tenant_ids: Optional[str] = Header(None, alias="X-Tenant-Ids"),
):
    try:
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            raise HTTPException(400, "Field 'query' is required and must be a non-empty string")

        raw_top_k = payload.get("top_k", 5)
        try:
            top_k = int(raw_top_k)
        except Exception:
            top_k = 5

        filter_by_filename = payload.get("filter_by_filename")
        exclude_ocr = bool(payload.get("exclude_ocr", False))

        collections_to_search = resolve_authorized_collections(
            qdrant_client=app_state.qdrant,
            x_tenant_id=x_tenant_id,
            x_tenant_ids=x_tenant_ids,
        )

        tenant_filter = ""

        all_results = []
        for coll in collections_to_search:
            try:
                coll_results = app_state.retriever.retrieve(
                    query=query,
                    top_k=top_k,
                    filter_by_source=None,
                    filter_by_filenames=[filter_by_filename] if filter_by_filename else None,
                    exclude_ocr=exclude_ocr,
                    tenant_id=tenant_filter,
                    collection_name=coll,
                )
                all_results.extend(coll_results)
            except Exception as e:
                logger.warning(f"Error querying collection {coll}: {e}")

        all_results.sort(key=lambda r: r.score, reverse=True)
        results = all_results[:top_k]

        return {
            "query": query,
            "results": [
                {
                    "text": r.text,
                    "score": r.score,
                    "filename": r.filename,
                    "page": r.page,
                    "citation": r.citation,
                    "from_ocr": r.from_ocr,
                }
                for r in results
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))
