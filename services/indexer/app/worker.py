# services/indexer/app/worker.py
"""
Worker local de indexaciÃ³n (OCR â†’ Chunking â†’ Embeddings â†’ Qdrant)

Escenario de uso:
- Alternativa al flujo que envÃ­a archivos al backend /documents/upload.
- Procesa los archivos *localmente* y sube directamente a Qdrant.
- Puede llamarse desde otros mÃ³dulos con `LocalRAGProcessor.process_file(...)`
  o ejecutarse como script para indexar una ruta.

CaracterÃ­sticas:
- DetecciÃ³n automÃ¡tica: texto nativo vs. OCR (PDF/imagen)
- OCR con PaddleOCR (opcional GPU si estÃ¡ disponible)
- Chunking semÃ¡ntico simple con solapamiento
- Embeddings con sentence-transformers (normalizados = coseno)
- Upsert por lotes a Qdrant
- Reintentos y logs claros
- Metadatos compatibles con backend (source/filename/page/chunk_index/from_ocr/ingested_at/ingested_at_ts/tenant_id)

Requisitos (aÃ±ade en requirements.txt del indexer):
- paddleocr
- pdfplumber
- pdf2image
- pillow
- numpy
- sentence-transformers
- qdrant-client
- python-docx (para .docx)

Variables de entorno importantes:
- QDRANT_URL           (default: http://qdrant:6333)
- QDRANT_COLLECTION    (default: documents)
- EMBEDDING_MODEL      (default: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2)
- OCR_USE_GPU          (default: true)
- CHUNK_SIZE           (default: 500)
- CHUNK_OVERLAP        (default: 50)
- OCR_DPI              (default: 200)
- BATCH_UPSERT         (default: 128)
- INDEX_TENANT_ID      (opcional; aÃ±ade tenant_id a payloads)
- DEFAULT_TENANT_ID    (fallback si no hay INDEX_TENANT_ID)
"""

from __future__ import annotations

import os
import io
import uuid
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

# ---- OCR / PDF ----
import pdfplumber
from pdf2image import convert_from_path
from paddleocr import PaddleOCR

# ---- Embeddings / Vector DB ----
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# =========================
# Config
# =========================

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "documents")
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
OCR_USE_GPU = os.getenv("OCR_USE_GPU", "true").lower() in {"1", "true", "yes"}
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "50"))
OCR_DPI = int(os.getenv("OCR_DPI", "200"))
BATCH_UPSERT = int(os.getenv("BATCH_UPSERT", "128"))

TENANT_ID = os.getenv("INDEX_TENANT_ID") or os.getenv("DEFAULT_TENANT_ID")

SUPPORTED_EXTS = {".pdf", ".txt", ".docx", ".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp"}

# =========================
# Helpers de texto
# =========================

def _split_paragraphs(text: str) -> List[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]

def re_split_sentences(text: str) -> List[str]:
    """
    Split simple por oraciones. No dependemos de nltk/spacy.
    """
    import re
    return re.split(r'(?<=[\.\!\?])\s+', text)

def _split_long(text: str, max_len: int) -> List[str]:
    """Divide texto muy largo por oraciones/palabras respetando max_len."""
    parts: List[str] = []
    current = ""
    sentences = [s.strip() for s in re_split_sentences(text)]
    for s in sentences:
        if len(current) + len(s) + 1 <= max_len:
            current += (s + " ")
        else:
            if current:
                parts.append(current.strip())
            if len(s) <= max_len:
                current = s + " "
            else:
                # Corte forzado por palabras
                words = s.split()
                tmp = ""
                for w in words:
                    if len(tmp) + len(w) + 1 <= max_len:
                        tmp += (w + " ")
                    else:
                        parts.append(tmp.strip())
                        tmp = w + " "
                current = tmp
    if current:
        parts.append(current.strip())
    return parts

def smart_chunk(text: str, chunk_size: int, overlap: int) -> List[str]:
    """
    Chunking simple: agrupa por pÃ¡rrafos y corta lo que sobrepase en oraciones/palabras.
    """
    chunks: List[str] = []
    current = ""

    for para in _split_paragraphs(text):
        if len(para) <= chunk_size:
            if len(current) + len(para) + 2 <= chunk_size:
                current += (para + "\n\n")
            else:
                if current:
                    chunks.append(current.strip())
                # solapamiento: coger cola del anterior
                if chunks:
                    tail = chunks[-1][-overlap:] if overlap > 0 else ""
                    current = (tail + "\n\n" + para + "\n\n").strip()
                else:
                    current = para + "\n\n"
        else:
            # pÃ¡rrafo largo: dividir por oraciones/palabras
            parts = _split_long(para, chunk_size)
            for part in parts:
                if len(part) <= chunk_size:
                    if len(current) + len(part) + 2 <= chunk_size:
                        current += (part + "\n\n")
                    else:
                        if current:
                            chunks.append(current.strip())
                        if chunks:
                            tail = chunks[-1][-overlap:] if overlap > 0 else ""
                            current = (tail + "\n\n" + part + "\n\n").strip()
                        else:
                            current = part + "\n\n"
                else:
                    # muy raro que pase; por si acaso, split duro
                    for forced in [part[i:i+chunk_size] for i in range(0, len(part), chunk_size)]:
                        if current:
                            chunks.append(current.strip())
                            current = ""
                        chunks.append(forced.strip())

    if current:
        chunks.append(current.strip())

    # Filtrar vacÃ­os
    return [c for c in chunks if c and len(c.strip()) > 0]

# =========================
# OCR & Extractors
# =========================

class SimpleOCR:
    def __init__(self, lang: str = "es", use_gpu: bool = OCR_USE_GPU):
        logger.info(f"Inicializando PaddleOCR (lang={lang}, use_gpu={use_gpu})â€¦")
        self.ocr = PaddleOCR(
            use_angle_cls=True,
            lang=lang,
            use_gpu=use_gpu,
            show_log=False,
        )

    def image_to_text(self, img: Image.Image) -> Tuple[str, float]:
        arr = np.array(img)
        result = self.ocr.ocr(arr, cls=True)
        if not result or not result[0]:
            return "", 0.0
        lines, confs = [], []
        for line in result[0]:
            lines.append(line[1][0])
            confs.append(line[1][1])
        text = "\n".join(lines)
        avg = sum(confs) / len(confs) if confs else 0.0
        return text, avg

    def pdf_to_text(self, pdf_path: str, dpi: int = OCR_DPI) -> Tuple[str, float, int]:
        images = convert_from_path(pdf_path, dpi=dpi, fmt="jpeg", grayscale=True)
        if not images:
            return "", 0.0, 0
        page_texts, confs = [], []
        for img in images:
            t, c = self.image_to_text(img)
            page_texts.append(t)
            confs.append(c)
        full_text = "\n\n=== PÃGINA ===\n\n".join(page_texts)
        avg = sum(confs) / len(confs) if confs else 0.0
        return full_text, avg, len(images)

def extract_text_native_pdf(pdf_path: str) -> Tuple[str, int]:
    pages_text: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            pages_text.append(txt)
    full = "\n\n=== PÃGINA ===\n\n".join(pages_text)
    return full, len(pages_text)

def detect_needs_ocr(file_path: str) -> bool:
    ext = Path(file_path).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp"}:
        return True
    if ext == ".pdf":
        try:
            txt, _ = extract_text_native_pdf(file_path)
            return len((txt or "").strip()) < 100
        except Exception:
            return True
    return False

# =========================
# Embeddings + Qdrant
# =========================

class VectorStore:
    def __init__(self, model_name: str = EMBEDDING_MODEL, qdrant_url: str = QDRANT_URL, collection: str = QDRANT_COLLECTION):
        self.client = QdrantClient(url=qdrant_url)
        logger.info(f"Cargando modelo de embeddings: {model_name}")
        self.embedder = SentenceTransformer(model_name)
        
        logger.info("Cargando modelo sparse: Qdrant/bm25")
        from fastembed import SparseTextEmbedding
        self.sparse_embedder = SparseTextEmbedding("Qdrant/bm25")
        
        self.collection = collection
        self.hybrid_ok = True
        self._ensure_collection()

    def _ensure_collection(self):
        # Inferir dimension con un ejemplo
        dim = len(self.embedder.encode(["dimension probe"])[0])

        exists = False
        try:
            collections = self.client.get_collections().collections
            exists = any(c.name == self.collection for c in collections)
        except Exception as e:
            logger.warning(f"No se pudo listar colecciones Qdrant: {e}")

        if exists:
            try:
                info = self.client.get_collection(self.collection)
                vectors = getattr(info.config.params, "vectors", None)
                sparse_vectors_cfg = getattr(info.config.params, "sparse_vectors", None)
                self.hybrid_ok = isinstance(vectors, dict) and "dense" in vectors and bool(sparse_vectors_cfg)
                if not self.hybrid_ok:
                    logger.warning(
                        f"Colecci?n '{self.collection}' no es h?brida. Indexando dense-only."
                    )
            except Exception as e:
                self.hybrid_ok = False
                logger.warning(f"No se pudo inspeccionar la colecci?n '{self.collection}': {e}")

            logger.info(f"Qdrant collection '{self.collection}' disponible")
            return

        logger.info(f"Creando colecci?n Qdrant '{self.collection}' (dim={dim}) con ?ndices h?bridos")
        from qdrant_client.models import SparseVectorParams, Modifier

        self.client.create_collection(
            collection_name=self.collection,
            vectors_config={"dense": VectorParams(size=dim, distance=Distance.COSINE)},
            sparse_vectors_config={"sparse": SparseVectorParams(modifier=Modifier.IDF)},
        )
        self.hybrid_ok = True

    def encode(self, texts: List[str]) -> Tuple[List[List[float]], List[Dict]]:
        # Dense
        vecs = self.embedder.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        dense_vectors = [v.astype(np.float32).tolist() for v in vecs]

        if not self.hybrid_ok:
            return dense_vectors, [None] * len(texts)

        # Sparse
        sparse_gen = self.sparse_embedder.embed(texts)
        sparse_vectors = []
        for v in sparse_gen:
            sparse_vectors.append({
                "indices": v.indices.tolist(),
                "values": v.values.tolist()
            })

        return dense_vectors, sparse_vectors

    def upsert_chunks(self, filename: str, chunks: List[Dict], dense_vectors: List[List[float]], sparse_vectors: List[Dict], base_metadata: Dict):
        assert len(chunks) == len(dense_vectors) == len(sparse_vectors)
        points: List[PointStruct] = []
        for ch, d_vec, s_vec in zip(chunks, dense_vectors, sparse_vectors):
            payload = {
                "text": ch["text"],
                "filename": filename,
                "source": base_metadata.get("source") or filename,  # alineado con backend
                "page": ch["metadata"].get("page"),
                "chunk_index": ch["metadata"].get("chunk_index"),
                "from_ocr": base_metadata.get("from_ocr", False),
                "ingested_at": base_metadata.get("ingested_at"),
                "ingested_at_ts": base_metadata.get("ingested_at_ts"),
                "tenant_id": base_metadata.get("tenant_id"),
            }
            # aÃ±adir cualquier otra metainformaciÃ³n extra pasada por el caller
            for k, v in base_metadata.items():
                if k not in payload:
                    payload[k] = v

            if s_vec is None:
                vector_payload = d_vec
            else:
                vector_payload = {
                    "dense": d_vec,
                    "sparse": s_vec
                }
            points.append(PointStruct(id=str(uuid.uuid4()), vector=vector_payload, payload=payload))

            # Upsert por lotes
            if len(points) >= BATCH_UPSERT:
                self.client.upsert(collection_name=self.collection, points=points)
                points.clear()
        if points:
            self.client.upsert(collection_name=self.collection, points=points)

# =========================
# Processor
# =========================

@dataclass
class ProcessStats:
    filename: str
    pages: int
    chunks: int
    chars: int
    ocr_used: bool
    ocr_conf: Optional[float]
    seconds: float

class LocalRAGProcessor:
    """
    Procesador local: extrae texto (OCR si aplica), genera chunks, crea embeddings,
    y sube a Qdrant. Mantiene el formato de metadata compatible con el backend.
    """

    def __init__(
        self,
        vectorstore: Optional[VectorStore] = None,
        ocr: Optional[SimpleOCR] = None,
        chunk_size: int = CHUNK_SIZE,
        overlap: int = CHUNK_OVERLAP,
    ):
        self.vs = vectorstore or VectorStore()
        self.ocr = ocr or SimpleOCR()
        self.chunk_size = chunk_size
        self.overlap = overlap

    def _chunk_by_pages(self, text: str) -> List[Tuple[int, str]]:
        """Devuelve [(page_num|None, page_text)], conservando separadores si existen."""
        marker = "\n\n=== PÃGINA ===\n\n"
        if "=== PÃGINA ===" in text:
            parts = text.split(marker)
            return [(i, t) for i, t in enumerate(parts, 1)]
        else:
            return [(None, text)]

    def _make_chunks(self, text: str, filename: str) -> List[Dict]:
        all_chunks: List[Dict] = []
        by_pages = self._chunk_by_pages(text)
        for page_num, page_text in by_pages:
            if len(page_text) <= self.chunk_size:
                all_chunks.append({
                    "text": page_text.strip(),
                    "metadata": {"filename": filename, "page": page_num}
                })
            else:
                for c in smart_chunk(page_text, self.chunk_size, self.overlap):
                    all_chunks.append({
                        "text": c,
                        "metadata": {"filename": filename, "page": page_num}
                    })

        # asignar Ã­ndices
        for i, ch in enumerate(all_chunks):
            ch["metadata"]["chunk_index"] = i
        return all_chunks

    def process_file(self, file_path: str, filename: Optional[str] = None, extra_metadata: Optional[Dict] = None) -> ProcessStats:
        """
        Procesa un archivo de ruta local y lo sube a Qdrant.

        Args:
            file_path: ruta al archivo
            filename: nombre a usar en metadata (default: basename de file_path)
            extra_metadata: dict adicional para payloads

        Returns:
            ProcessStats
        """
        t0 = time.time()
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(file_path)
        filename = filename or path.name

        # 1) Extraer texto
        needs_ocr = detect_needs_ocr(str(path))
        ocr_conf = None
        pages = 1
        text = ""

        ext = path.suffix.lower()
        if ext == ".pdf":
            if needs_ocr:
                logger.info(f"OCR PDF: {filename}")
                text, ocr_conf, pages = self.ocr.pdf_to_text(str(path), dpi=OCR_DPI)
            else:
                text, pages = extract_text_native_pdf(str(path))
        elif ext in {".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp"}:
            logger.info(f"OCR Image: {filename}")
            img = Image.open(str(path))
            text, ocr_conf = self.ocr.image_to_text(img)
            pages = 1
        elif ext == ".txt":
            text = path.read_text(encoding="utf-8", errors="ignore")
            pages = text.count("\n\n=== PÃGINA ===\n\n") + 1 if "=== PÃGINA ===" in text else 1
        elif ext == ".docx":
            try:
                from docx import Document as DocxDocument
                doc = DocxDocument(str(path))
                text = "\n".join(p.text for p in doc.paragraphs)
                pages = 1
            except Exception as e:
                raise RuntimeError(f"Error leyendo DOCX: {e}")
        else:
            raise ValueError(f"Formato no soportado: {path.suffix}")

        if not text or len(text.strip()) < 10:
            raise RuntimeError("Texto insuficiente extraÃ­do del documento")

        # 2) Chunking
        chunks = self._make_chunks(text, filename)
        total_chars = sum(len(c["text"]) for c in chunks)

        # 3) Embeddings (normalizados)
        dense_vectors, sparse_vectors = self.vs.encode([c["text"] for c in chunks])

        # 4) Upsert Qdrant
        now_ts = int(time.time())
        base_meta = {
            "from_ocr": bool(needs_ocr),
            "ocr_confidence": float(ocr_conf) if ocr_conf is not None else None,
            "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
            "ingested_at_ts": now_ts,
            "tenant_id": TENANT_ID,
        }
        if extra_metadata:
            base_meta.update({k: v for k, v in extra_metadata.items() if v is not None})

        self.vs.upsert_chunks(filename, chunks, dense_vectors, sparse_vectors, base_meta)

        dt = time.time() - t0
        stats = ProcessStats(
            filename=filename,
            pages=pages,
            chunks=len(chunks),
            chars=total_chars,
            ocr_used=needs_ocr,
            ocr_conf=ocr_conf,
            seconds=dt,
        )
        logger.info(
            f"âœ“ Indexado local: {filename} | pages={pages} chunks={len(chunks)} "
            f"ocr={needs_ocr} conf={ocr_conf if ocr_conf is not None else '-'} time={dt:.2f}s"
        )
        return stats

# =========================
# CLI simple
# =========================

if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Indexador local â†’ Qdrant")
    parser.add_argument("path", help="Archivo o carpeta a indexar")
    parser.add_argument("--metadata", help='JSON con metadata extra {"source":"local"}', default=None)
    args = parser.parse_args()

    extra = {}
    if args.metadata:
        try:
            extra = json.loads(args.metadata)
        except Exception:
            logger.warning("Metadata invÃ¡lida, ignorando")

    proc = LocalRAGProcessor()
    p = Path(args.path)

    if p.is_file():
        proc.process_file(str(p), extra_metadata=extra)
    elif p.is_dir():
        for f in p.rglob("*"):
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS:
                try:
                    proc.process_file(str(f), extra_metadata=extra)
                except Exception as e:
                    logger.error(f"Error indexando {f}: {e}")
    else:
        logger.error("Ruta no vÃ¡lida")
