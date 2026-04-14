# backend/app/core/rag/citations.py
"""
Citations utilities for the JARVIS RAG system.

Objetivos:
- Normalizar citas a una estructura estable: {source_id, filename, page, span, label, uri}
- Validar que la cita es consistente (p. ej., que la página existe cuando se conoce el total)
- Resolver URIs públicas (MinIO/SharePoint/etc.) para enlazar la fuente en el frontend
- Deduplicar, verificar grounding e insertar citas faltantes en una respuesta

No depende de Qdrant ni de clientes concretos. Usa callbacks/resolvers inyectables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Callable, Dict, List, Tuple, Iterable
from urllib.parse import quote
import logging
import re

logger = logging.getLogger(__name__)

__all__ = [
    "CitationRef",
    "MetadataLookup",
    "UriResolver",
    "make_label",
    "normalize",
    "dedupe_preserve_order",
    "validate_page_in_range",
    "enrich_uris",
    "assert_grounded",
    "from_retrieval_results",
    "minio_resolver_factory",
    "sharepoint_resolver_factory",
    "parse_labels_from_text",
    "ensure_citations_in_text",
    "build_sources_payload",
]

# -------------------------
# Tipos y modelos
# -------------------------

@dataclass(frozen=True, slots=True)
class CitationRef:
    """
    Representa una cita normalizada a un fragmento indexado.
    - source_id: identificador estable de la fuente (ruta completa, key MinIO o driveItemId)
    - filename: nombre de archivo mostrado al usuario
    - page: número de página (1-based) si aplica
    - span: texto exacto o rango dentro del chunk (opcional)
    - label: etiqueta visible que debe usarse en la respuesta (p. ej. "[archivo.pdf p.3]")
    - uri: URL pública o firma temporal para acceder a la fuente (puede ser None)
    """
    source_id: str
    filename: str
    page: Optional[int]
    span: Optional[str]
    label: str
    uri: Optional[str] = None


# Callbacks inyectables
MetadataLookup = Callable[[CitationRef], Dict]   # Debe devolver p. ej. {"total_pages": int | None}
UriResolver = Callable[[CitationRef], Optional[str]]

# -------------------------
# Helpers de formato
# -------------------------

_ALLOWED_EXTS = (".pdf", ".docx", ".xlsx", ".txt", ".jpg", ".jpeg", ".png", ".pptx", ".md")

def make_label(filename: str, page: Optional[int]) -> str:
    """Formatea la etiqueta visible de la cita."""
    fname = filename or "unknown"
    return f"[{fname} p.{page}]" if page is not None else f"[{fname}]"


def normalize(
    *,
    source_id: str,
    filename: str,
    page: Optional[int],
    span: Optional[str] = None,
    uri: Optional[str] = None,
) -> CitationRef:
    """Crea una CitationRef coherente y lista para usarse."""
    p = page if isinstance(page, int) and page >= 1 else None
    return CitationRef(
        source_id=source_id or filename or "unknown",
        filename=filename or "unknown",
        page=p,
        span=span,
        label=make_label(filename or "unknown", p),
        uri=uri,
    )


def dedupe_preserve_order(citations: List[CitationRef]) -> List[CitationRef]:
    """Elimina duplicados manteniendo el orden (duplicado = misma (source_id, page))."""
    seen: set[Tuple[str, Optional[int]]] = set()
    out: List[CitationRef] = []
    for c in citations:
        key = (c.source_id, c.page)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out

# -------------------------
# Validación y grounding
# -------------------------

def validate_page_in_range(cite: CitationRef, meta_lookup: Optional[MetadataLookup]) -> Tuple[bool, Optional[str]]:
    """
    Verifica que la página existe si conocemos total_pages.
    Si no hay metadata, se considera válido (fail-open controlado).
    """
    if cite.page is None or not meta_lookup:
        return True, None
    try:
        meta = meta_lookup(cite) or {}
        total = meta.get("total_pages")
        if isinstance(total, int) and total >= 1:
            if 1 <= cite.page <= total:
                return True, None
            return False, f"Página fuera de rango: {cite.page} > {total}"
        return True, None
    except Exception as e:
        logger.warning(f"validate_page_in_range: error consultando metadata: {e}")
        return True, None  # no bloqueamos por error de metadata


def enrich_uris(citations: List[CitationRef], resolver: Optional[UriResolver]) -> List[CitationRef]:
    """Devuelve nuevas CitationRef con `uri` rellenado si hay resolver."""
    if not resolver:
        return citations
    enriched: List[CitationRef] = []
    for c in citations:
        try:
            uri = resolver(c)
        except Exception as e:
            logger.warning(f"Resolver URI falló para {c.source_id}: {e}")
            uri = None
        enriched.append(CitationRef(**{**c.__dict__, "uri": uri}))
    return enriched


def assert_grounded(output_text: str, citations: List[CitationRef]) -> bool:
    """
    Heurística sencilla: el texto debe contener al menos una de las labels
    de las citas proporcionadas. (Usado como guardrail ligero).
    """
    if not output_text or not citations:
        return False
    labels = [c.label for c in citations]
    return any(lbl in output_text for lbl in labels)

# -------------------------
# Resolvers de ejemplo
# -------------------------

def minio_resolver_factory(
    *,
    endpoint: str,          # p. ej. "https://minio.example.com"
    bucket: str,            # p. ej. "documents"
    prefix: str = "",       # p. ej. "tenant-123/"
    signed_url_func: Optional[Callable[[str], str]] = None,
    page_anchor_param: Optional[str] = "page",  # añade ?page=N si aplica
) -> UriResolver:
    """
    Crea un resolver para objetos en MinIO/S3.
    - `source_id` se interpreta como la key (o se compone con prefix/filename).
    - Si pasas signed_url_func(key) → usará ese generador (presigned GET).
    - Si no, construye URL pública (útil si el bucket/objeto es público).
    """
    def _resolver(c: CitationRef) -> Optional[str]:
        key = c.source_id or f"{prefix}{c.filename}"
        if prefix and not key.startswith(prefix):
            key = f"{prefix.rstrip('/')}/{c.source_id or c.filename}"
        if signed_url_func:
            url = signed_url_func(key)
        else:
            url = f"{endpoint.rstrip('/')}/{quote(bucket)}/{quote(key)}"
        if c.page is not None and page_anchor_param:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{page_anchor_param}={c.page}"
        return url
    return _resolver


def sharepoint_resolver_factory(
    *,
    tenant_host: str,   # p. ej. "contoso.sharepoint.com"
    site_path: str,     # p. ej. "sites/Legal"
    file_link_builder: Optional[Callable[[CitationRef], str]] = None,
    page_anchor: Optional[str] = None,  # p. ej. "#page=" si tu visor soporta anclas
) -> UriResolver:
    """
    Crea un resolver de URLs de SharePoint (enlaza a la vista web).
    Para máxima flexibilidad, puedes inyectar `file_link_builder` que
    mapee CitationRef → URL. Si no, se compone una URL básica por ruta.
    """
    def _resolver(c: CitationRef) -> Optional[str]:
        if file_link_builder:
            url = file_link_builder(c)
        else:
            base = f"https://{tenant_host}/{site_path.strip('/')}"
            # Ejemplo más común: biblioteca "Shared Documents"
            url = f"{base}/Shared%20Documents/{quote(c.filename)}"
        if c.page is not None and page_anchor:
            url = f"{url}{page_anchor}{c.page}"
        return url
    return _resolver

# -------------------------
# Pipeline conveniente
# -------------------------

def from_retrieval_results(
    results: Iterable,  # espera objetos con attrs: filename, source, page, citation
    *,
    uri_resolver: Optional[UriResolver] = None,
    meta_lookup: Optional[MetadataLookup] = None,
) -> List[CitationRef]:
    """
    Convierte una lista de resultados del retriever en citas normalizadas,
    validando páginas cuando sea posible y resolviendo URIs.
    """
    citations: List[CitationRef] = []
    for r in results:
        source_id = getattr(r, "source", None) or getattr(r, "filename", None) or "unknown"
        filename = getattr(r, "filename", None) or "unknown"
        page = getattr(r, "page", None)
        label = getattr(r, "citation", None) or make_label(filename, page if isinstance(page, int) else None)

        c = CitationRef(
            source_id=source_id,
            filename=filename,
            page=page if isinstance(page, int) and page >= 1 else None,
            span=None,
            label=label,
            uri=None,
        )

        ok, reason = validate_page_in_range(c, meta_lookup)
        if not ok:
            logger.info(f"Cita descartada por página inválida: {c} ({reason})")
            continue

        citations.append(c)

    citations = dedupe_preserve_order(citations)
    citations = enrich_uris(citations, uri_resolver)
    return citations

# -------------------------
# Extra: parsing y asegurado de citas en texto
# -------------------------

# Regex flexible para [nombre.ext] o [nombre.ext p.N]
_LABEL_RE = re.compile(
    r"\[(?P<filename>[^\[\]\n]+?\.(?:pdf|docx|xlsx|pptx|txt|md|jpg|jpeg|png))(?:\s+p\.(?P<page>\d+))?\]",
    re.IGNORECASE,
)

def parse_labels_from_text(text: str) -> List[Tuple[str, Optional[int]]]:
    """
    Extrae pares (filename, page) de un texto de respuesta.
    No asegura que existan en tu índice, solo parsea etiquetas.
    """
    out: List[Tuple[str, Optional[int]]] = []
    if not text:
        return out
    for m in _LABEL_RE.finditer(text):
        fname = m.group("filename").strip()
        page = m.group("page")
        out.append((fname, int(page) if page else None))
    # dedupe conservando orden
    seen = set()
    uniq: List[Tuple[str, Optional[int]]] = []
    for t in out:
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    return uniq


def build_sources_payload(citations: List[CitationRef]) -> List[Dict]:
    """
    Convierte CitationRef a un payload listo para persistir junto a la respuesta:
    [{filename, page, citation, uri}]
    """
    return [
        {
            "filename": c.filename,
            "page": c.page,
            "citation": c.label,
            "uri": c.uri,
            "source_id": c.source_id,
        }
        for c in citations
    ]


def ensure_citations_in_text(
    response_text: str,
    retrieval_results: Optional[Iterable] = None,
    *,
    uri_resolver: Optional[UriResolver] = None,
    meta_lookup: Optional[MetadataLookup] = None,
    append_section_title: str = "Fuentes consultadas",
) -> Tuple[str, List[CitationRef]]:
    """
    Si la respuesta no incluye ninguna etiqueta de cita, intenta generarlas
    desde `retrieval_results` y las añade en un bloque al final.

    Devuelve: (texto_final, citations_normalizadas)
    """
    existing = parse_labels_from_text(response_text)
    if existing:
        # Ya hay citas; no añadimos nada, pero normalizamos a objetos básicos
        citations = [
            normalize(source_id=fname, filename=fname, page=page) for (fname, page) in existing
        ]
        citations = enrich_uris(citations, uri_resolver)
        return response_text, citations

    if not retrieval_results:
        # Sin resultados de RAG, no podemos inventar
        return response_text, []

    citations = from_retrieval_results(
        retrieval_results, uri_resolver=uri_resolver, meta_lookup=meta_lookup
    )
    if not citations:
        return response_text, []

    # Añadir sección al final
    lines = [response_text.rstrip(), "", "---", f"{append_section_title}:"]
    for c in citations:
        if c.uri:
            lines.append(f"- {c.label} → {c.uri}")
        else:
            lines.append(f"- {c.label}")
    final = "\n".join(lines)
    return final, citations
