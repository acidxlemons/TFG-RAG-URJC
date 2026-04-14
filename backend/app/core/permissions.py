import os
from typing import List, Optional


def get_all_collection_names(qdrant_client) -> List[str]:
    try:
        cols = qdrant_client.get_collections().collections
        names = [c.name for c in cols if getattr(c, "name", None)]
        return names or [os.getenv("QDRANT_COLLECTION", "documents")]
    except Exception:
        return [os.getenv("QDRANT_COLLECTION", "documents")]


def get_global_read_collection_names(qdrant_client) -> List[str]:
    configured_raw = os.getenv("RAG_GLOBAL_READ_COLLECTIONS", "")
    if not configured_raw.strip():
        return []
    configured = [item.strip() for item in configured_raw.split(",") if item.strip()]
    existing = set(get_all_collection_names(qdrant_client))
    return [collection for collection in configured if collection in existing]


def resolve_authorized_collections(
    qdrant_client,
    x_tenant_id: Optional[str],
    x_tenant_ids: Optional[str],
) -> List[str]:
    requested: List[str] = []
    if x_tenant_ids:
        requested = [t.strip() for t in x_tenant_ids.split(",") if t.strip()]
    elif x_tenant_id and x_tenant_id.strip():
        requested = [x_tenant_id.strip()]

    existing = set(get_all_collection_names(qdrant_client))

    if requested:
        if "*" in requested or "all" in requested:
            allow_wildcard = os.getenv("RAG_ALLOW_WILDCARD_COLLECTIONS", "false").lower() in {"1", "true", "yes", "y"}
            return sorted(existing) if allow_wildcard else []
        filtered = [c for c in requested if c in existing]
        return list(dict.fromkeys(filtered))

    global_read = get_global_read_collection_names(qdrant_client)
    if global_read:
        return global_read

    allow_global_no_headers = os.getenv("RAG_ALLOW_GLOBAL_WITHOUT_HEADERS", "false").lower() in {"1", "true", "yes", "y"}
    if allow_global_no_headers:
        return sorted(existing)
    return []


def can_write_without_tenant_header() -> bool:
    return os.getenv("RAG_ALLOW_GLOBAL_UPLOAD_WITHOUT_HEADERS", "false").lower() in {"1", "true", "yes", "y"}
