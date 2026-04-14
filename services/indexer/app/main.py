# services/indexer/app/main.py
"""
Servicio de Indexación Automática (modo backend o local)
- Modo "backend": envía archivos al backend /documents/upload
- Modo "local": procesa localmente (OCR→chunking→embeddings) y sube a Qdrant

También:
- Escaneo periódico de carpeta local (WATCH_FOLDER)
- Sincronización con SharePoint (descarga cambios y los procesa)
- MULTI-SITE: Sincronización de múltiples sitios SharePoint a colecciones separadas
- Webhook de SharePoint con validación (GET/POST validationtoken)
"""

from __future__ import annotations

import os
import base64
import logging
from pathlib import Path
from typing import Optional, Dict, List

import time
import requests
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Header
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler

# Prometheus metrics
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

# ======================================================
# PROMETHEUS METRICS for SharePoint Sync
# ======================================================
sharepoint_sync_total = Counter(
    'sharepoint_sync_total',
    'Total SharePoint sync operations',
    ['site', 'status']  # status: success, error
)

sharepoint_files_downloaded = Counter(
    'sharepoint_files_downloaded_total',
    'Total files downloaded from SharePoint',
    ['site']
)

sharepoint_files_indexed = Counter(
    'sharepoint_files_indexed_total',
    'Total files successfully indexed to Qdrant',
    ['site']
)

sharepoint_sync_in_progress = Gauge(
    'sharepoint_sync_in_progress',
    'Whether a sync is currently in progress (1) or not (0)',
    ['site']
)

sharepoint_last_sync_timestamp = Gauge(
    'sharepoint_last_sync_timestamp_seconds',
    'Timestamp of last successful sync',
    ['site']
)

sharepoint_sync_duration = Gauge(
    'sharepoint_sync_duration_seconds',
    'Duration of last sync in seconds',
    ['site']
)

# SharePoint (el que ya tienes en backend/app/integrations/sharepoint/client.py)
from app.integrations.sharepoint.client import SharePointClient, SharePointSynchronizer
# Multi-site synchronizer
from app.multi_site_sync import MultiSiteSynchronizer, SyncResult
# Procesador local
from app.worker import LocalRAGProcessor

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = FastAPI(title="RAG Indexer Service", version="2.0.0")

# =========================
# Configuración por entorno
# =========================
INDEX_MODE = os.getenv("INDEX_MODE", "backend").lower()  # backend | local
BACKEND_URL = os.getenv("BACKEND_URL", "http://rag-backend:8000")
INDEX_TENANT_ID = os.getenv("INDEX_TENANT_ID")  # si se define, se envía como X-Tenant-Id
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "300"))  # segundos
WATCH_FOLDER = Path(os.getenv("WATCH_FOLDER", "/app/watch"))

# SharePoint (opcionales - modo single site)
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
SHAREPOINT_SITE_ID = os.getenv("SHAREPOINT_SITE_ID")
SHAREPOINT_DRIVE_ID = os.getenv("SHAREPOINT_DRIVE_ID")  # Soporta drive_id
SHAREPOINT_FOLDER_PATH = os.getenv("SHAREPOINT_FOLDER_PATH", "Documents")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "changeme")
SYNC_ON_START = os.getenv("SYNC_ON_START", "false").lower() in {"1", "true", "yes"}

# Multi-site SharePoint (NUEVO)
SHAREPOINT_MULTI_SITE = os.getenv("SHAREPOINT_MULTI_SITE", "false").lower() in {"1", "true", "yes"}
SHAREPOINT_SITES_CONFIG = os.getenv("SHAREPOINT_SITES_CONFIG", "/app/config/sharepoint_sites.json")

SUPPORTED_EXTS = {
    ".pdf", ".doc", ".docx", ".txt",
    ".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp"
}

# =========================
# Estado global
# =========================
sp_client: Optional[SharePointClient] = None
sp_sync: Optional[SharePointSynchronizer] = None
multi_sync: Optional[MultiSiteSynchronizer] = None  # NUEVO: Multi-site sync
local_processor: Optional[LocalRAGProcessor] = None

if INDEX_MODE == "local":
    try:
        local_processor = LocalRAGProcessor()
        logger.info("✓ LocalRAGProcessor inicializado (modo local)")
    except Exception as e:
        logger.error(f"Error iniciando LocalRAGProcessor: {e}")
        raise

# NUEVO: Inicializar Multi-Site Synchronizer si está habilitado
if SHAREPOINT_MULTI_SITE and AZURE_TENANT_ID and AZURE_CLIENT_ID and AZURE_CLIENT_SECRET:
    config_path = Path(SHAREPOINT_SITES_CONFIG)
    if config_path.exists():
        try:
            multi_sync = MultiSiteSynchronizer(
                config_path=str(config_path),
                base_watch_folder=str(WATCH_FOLDER),
                azure_tenant_id=AZURE_TENANT_ID,
                azure_client_id=AZURE_CLIENT_ID,
                azure_client_secret=AZURE_CLIENT_SECRET,
            )
            logger.info(f"✓ MultiSiteSynchronizer habilitado ({len(multi_sync.sites)} sitios)")
        except Exception as e:
            logger.error(f"No se pudo inicializar MultiSiteSynchronizer: {e}")
            multi_sync = None
    else:
        logger.warning(f"Multi-site habilitado pero config no encontrada: {config_path}")

# Single-site SharePoint (legacy, si multi-site no está habilitado)
elif AZURE_TENANT_ID and AZURE_CLIENT_ID and AZURE_CLIENT_SECRET and (SHAREPOINT_SITE_ID or SHAREPOINT_DRIVE_ID):
    try:
        sp_client = SharePointClient(
            tenant_id=AZURE_TENANT_ID,
            client_id=AZURE_CLIENT_ID,
            client_secret=AZURE_CLIENT_SECRET,
            site_id=SHAREPOINT_SITE_ID or None,
            drive_id=SHAREPOINT_DRIVE_ID or None,
            folder_path=SHAREPOINT_FOLDER_PATH,
        )
        sp_sync = SharePointSynchronizer(
            client=sp_client,
            local_dir=str(WATCH_FOLDER),
            delta_token_file=str(WATCH_FOLDER / ".sharepoint_delta.txt"),
        )
        logger.info("✓ SharePointSynchronizer habilitado (modo single-site)")
    except Exception as e:
        logger.error(f"No se pudo inicializar SharePoint: {e}")
        sp_client = None
        sp_sync = None
class IndexRequest(BaseModel):
    file_path: str
    metadata: Optional[dict] = {}

class SharePointWebhookItem(BaseModel):
    subscriptionId: Optional[str] = None
    clientState: Optional[str] = None
    resource: Optional[str] = None
    changeType: Optional[str] = None

class SharePointWebhookEnvelope(BaseModel):
    value: List[SharePointWebhookItem] = []


# =========================
# Utilidades markers
# =========================
def _get_cache_dir(fp: Path) -> Path:
    # Crear carpeta .index_cache en el mismo directorio del archivo
    cache_dir = fp.parent / ".index_cache"
    if not cache_dir.exists():
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            # Ocultar carpeta en Windows si es necesario (opcional)
        except Exception:
            pass
    return cache_dir

def _marker_path(fp: Path) -> Path:
    return _get_cache_dir(fp) / f"{fp.name}.indexed"

def _marker_signature(fp: Path) -> str:
    st = fp.stat()
    return f"{st.st_size}|{int(st.st_mtime)}"

def _is_indexed_up_to_date(fp: Path) -> bool:
    mk = _marker_path(fp)
    if not mk.exists():
        # Retrocompatibilidad: buscar en la carpeta raíz
        old_mk = fp.parent / f".indexed_{fp.name}"
        if old_mk.exists():
            # Mover al nuevo lugar
            try:
                _get_cache_dir(fp) # asegurar que existe
                old_mk.rename(mk)
                return True # Asumimos que si existe el viejo, está ok (o se revalidará)
            except Exception:
                pass
        return False
        
    try:
        stored = mk.read_text(encoding="utf-8").strip()
        return stored == _marker_signature(fp)
    except Exception:
        return False

def _write_marker(fp: Path):
    mk = _marker_path(fp)
    try:
        mk.write_text(_marker_signature(fp), encoding="utf-8")
    except Exception as e:
        logger.warning(f"No se pudo escribir marker para {fp.name}: {e}")

# =========================
# Procesadores
# =========================
def _report_status(filename: str, status: str, message: str = ""):
    """Reporta estado al backend (best effort)"""
    if INDEX_MODE != "backend":
        return
    try:
        requests.post(
            f"{BACKEND_URL}/documents/status",
            json={"filename": filename, "status": status, "message": message},
            timeout=5
        )
    except Exception as e:
        logger.warning(f"No se pudo reportar status {status} para {filename}: {e}")


def _report_sharepoint_sync_state(
    *,
    site_id: str,
    folder_path: str,
    collection_name: Optional[str] = None,
    site_name: Optional[str] = None,
    delta_token: Optional[str] = None,
    status: str = "success",
    downloaded_files: int = 0,
    indexed_files: int = 0,
    errors: int = 0,
    message: str = "",
    is_active: bool = True,
) -> None:
    """Persistencia best-effort del estado SharePoint en el backend."""
    if INDEX_MODE != "backend":
        return
    try:
        requests.post(
            f"{BACKEND_URL}/documents/sharepoint-sync/report",
            json={
                "site_id": site_id,
                "folder_path": folder_path,
                "collection_name": collection_name,
                "site_name": site_name,
                "delta_token": delta_token,
                "last_sync": datetime.utcnow().isoformat() + "Z",
                "status": status,
                "downloaded_files": downloaded_files,
                "indexed_files": indexed_files,
                "errors": errors,
                "message": message,
                "is_active": is_active,
            },
            timeout=10,
        )
    except Exception as e:
        logger.warning("No se pudo reportar estado SharePoint para %s: %s", site_name or site_id, e)


def _ensure_backend_collections(collection_names: List[str]) -> None:
    """Pide al backend crear colecciones híbridas necesarias para multi-site."""
    if INDEX_MODE != "backend":
        return

    names = [c.strip() for c in collection_names if isinstance(c, str) and c.strip()]
    names = list(dict.fromkeys(names))
    if not names:
        return

    payload = {"collections": names}
    url = f"{BACKEND_URL}/documents/collections/ensure"
    attempts = 0
    last_error = None

    while attempts < 3:
        attempts += 1
        try:
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code >= 300:
                raise RuntimeError(f"{resp.status_code} {resp.text[:300]}")
            data = resp.json()
            logger.info(
                "✓ Colecciones aseguradas en backend: requested=%s created=%s hybrid_ok=%s status=%s",
                data.get("requested"),
                data.get("created"),
                data.get("hybrid_ok"),
                data.get("status"),
            )
            return
        except Exception as e:
            last_error = e
            logger.warning(
                "No se pudo asegurar colecciones en backend (intento %s/3): %s",
                attempts,
                e,
            )
            time.sleep(1.5 * attempts)

    logger.error("Fallo asegurando colecciones en backend tras reintentos: %s", last_error)

def _delete_from_backend(filename: str):
    """Solicita borrado al backend"""
    if INDEX_MODE != "backend":
        return
    try:
        logger.info(f"Solicitando borrado de {filename} al backend...")
        resp = requests.delete(
            f"{BACKEND_URL}/documents/delete",
            params={"filename": filename},
            headers={"X-Tenant-Id": INDEX_TENANT_ID} if INDEX_TENANT_ID else None,
            timeout=10
        )
        if resp.status_code == 200:
            logger.info(f"✓ Backend confirmó borrado de {filename}")
        else:
            logger.warning(f"Backend falló borrando {filename}: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Error solicitando borrado de {filename}: {e}")

def _delete_from_backend_with_tenant(filename: str, tenant_id: Optional[str] = None):
    """Solicita borrado al backend con tenant_id específico"""
    if INDEX_MODE != "backend":
        return
    try:
        effective_tenant = tenant_id or INDEX_TENANT_ID
        logger.info(f"Solicitando borrado de {filename} (tenant={effective_tenant})...")
        resp = requests.delete(
            f"{BACKEND_URL}/documents/delete",
            params={"filename": filename},
            headers={"X-Tenant-Id": effective_tenant} if effective_tenant else None,
            timeout=10
        )
        if resp.status_code == 200:
            logger.info(f"✓ Backend confirmó borrado de {filename}")
        else:
            logger.warning(f"Backend falló borrando {filename}: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Error solicitando borrado de {filename}: {e}")


def _upload_to_backend(file_path: str, filename: str, metadata: Dict, tenant_id: Optional[str] = None) -> None:
    """Sube archivo al backend con tenant_id opcional (para multi-site)"""
    effective_tenant = tenant_id or INDEX_TENANT_ID
    logger.info(f"[backend] Enviando '{filename}' al backend (tenant={effective_tenant})")

    with open(file_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("utf-8")

    headers = {}
    if effective_tenant:
        headers["X-Tenant-Id"] = effective_tenant

    payload_metadata = dict(metadata or {})
    payload_metadata.setdefault("source_path", file_path)
    payload_metadata["collection_name"] = effective_tenant or os.getenv("QDRANT_COLLECTION", "documents")

    # Retry suave (red transitoria)
    attempts = 0
    last_err = None
    while attempts < 3:
        try:
            resp = requests.post(
                f"{BACKEND_URL}/documents/upload",
                json={"filename": filename, "content_base64": content_b64, "metadata": payload_metadata},
                timeout=600,
                headers=headers or None,
            )
            if resp.status_code >= 300:
                raise RuntimeError(f"Backend respondió {resp.status_code}: {resp.text[:300]}")
            logger.info(f"✓ Encolado en backend: {filename} (tenant={effective_tenant})")
            return
        except Exception as e:
            last_err = e
            attempts += 1
            wait = 1.5 * attempts
            logger.warning(f"Fallo subiendo {filename} (intento {attempts}/3). Reintentando en {wait:.1f}s… Error: {e}")
            time.sleep(wait)
    
    # si llegó aquí, falló
    _report_status(filename, "failed", f"Error subiendo: {last_err}")
    raise RuntimeError(f"No se pudo subir '{filename}' al backend tras reintentos: {last_err}")

def process_path(file_path: str, metadata: Optional[Dict] = None, tenant_id: Optional[str] = None, create_marker: bool = True) -> None:
    """
    Decide según INDEX_MODE:
    - backend: POST al backend (con tenant_id para multi-site)
    - local: procesar y upsert directo a Qdrant
    
    Args:
        file_path: Ruta al archivo
        metadata: Metadata adicional
        tenant_id: ID de tenant/colección para multi-site (ej: 'documents_RRHH')
    """
    fp = Path(file_path)
    if not fp.exists():
        raise FileNotFoundError(f"Archivo no encontrado: {fp}")

    filename = fp.name
    
    # Saltar archivos internos/de control
    if filename.startswith(".") or filename.startswith("_"):
        logger.debug(f"Saltando archivo interno: {filename}")
        return
    
    base_meta = {"source": (metadata or {}).get("source"), "source_path": str(fp)}
    # Añadir site_name si viene en metadata
    if metadata and metadata.get("site"):
        base_meta["site"] = metadata["site"]
    base_meta = {k: v for k, v in base_meta.items() if v}

    try:
        if INDEX_MODE == "backend":
            _report_status(filename, "pending", f"Detectado por indexer, subiendo... (tenant={tenant_id})")
            _upload_to_backend(str(fp), filename, base_meta or {}, tenant_id=tenant_id)
            if create_marker:
                _write_marker(fp)
        elif INDEX_MODE == "local":
            assert local_processor is not None, "LocalRAGProcessor no inicializado"
            local_processor.process_file(str(fp), filename=filename, extra_metadata=base_meta)
            if create_marker:
                _write_marker(fp)
        else:
            raise ValueError(f"INDEX_MODE inválido: {INDEX_MODE}")
    except Exception as e:
        if INDEX_MODE == "backend":
            _report_status(filename, "failed", str(e))
        raise

def scan_folder(folder_path: Path):
    """
    Escanea carpeta y procesa archivos nuevos o modificados.
    Detecta también archivos eliminados y notifica al backend.
    """
    logger.info(f"Escaneando carpeta: {folder_path}")
    indexed = []
    
    # 1. Procesar nuevos/modificados
    for fp in folder_path.rglob("*"):
        if fp.is_file() and fp.suffix.lower() in SUPPORTED_EXTS:
            if not _is_indexed_up_to_date(fp):
                try:
                    process_path(str(fp), {"source": "local_watch"})
                    indexed.append(str(fp))
                except Exception as e:
                    logger.error(f"Error indexando {fp}: {e}")
    
    if indexed:
        logger.info(f"✓ Procesados {len(indexed)} archivo(s) nuevo(s)/actualizado(s)")
    else:
        logger.info("No hay archivos nuevos o modificados")

    # 2. Detección de borrados
    # Iterar sobre todos los markers .indexed en .index_cache
    for cache_dir in folder_path.rglob(".index_cache"):
        if not cache_dir.is_dir(): continue
        
        for marker in cache_dir.glob("*.indexed"):
            # marker name is filename.indexed
            original_name = marker.name.replace(".indexed", "")
            original_file = cache_dir.parent / original_name
            
            if not original_file.exists():
                logger.info(f"Detectado archivo borrado: {original_name}")
                if INDEX_MODE == "backend":
                    _delete_from_backend(original_name)
                    _report_status(original_name, "deleted", "Archivo eliminado de watch folder")
                
                # Borrar marker
                try:
                    marker.unlink()
                    logger.info(f"Marker eliminado para {original_name}")
                except Exception as e:
                    logger.error(f"Error borrando marker {marker}: {e}")

    return indexed

# =========================
# Scheduler
# =========================
scheduler = BackgroundScheduler()

def scheduled_job():
    """Tarea programada: escanea carpeta local y sincroniza SharePoint"""
    
    # 1. Escanear carpeta local (para archivos copiados manualmente)
    if WATCH_FOLDER.exists():
        scan_folder(WATCH_FOLDER)
    
    # 2. Multi-site SharePoint sync (NUEVO)
    if multi_sync:
        try:
            _ensure_backend_collections(multi_sync.get_collection_names())
        except Exception as e:
            logger.warning(f"No se pudieron asegurar colecciones antes del sync: {e}")

        logger.info("⏳ Sincronizando múltiples sitios SharePoint...")
        
        # Inicializar métricas para cada sitio
        for site in multi_sync.sites:
            sharepoint_sync_in_progress.labels(site=site.name).set(1)
        
        try:
            sync_start_time = time.time()
            results = multi_sync.sync_all()
            
            # Agrupar resultados por sitio
            site_stats = {}
            for result in results:
                site_name = result.site_name
                if site_name not in site_stats:
                    site_stats[site_name] = {"downloaded": 0, "indexed": 0, "errors": 0}
                
                if result.error:
                    logger.error(f"Error en {site_name}: {result.error}")
                    site_stats[site_name]["errors"] += 1
                    continue
                
                if result.deleted:
                    # Archivo eliminado en SharePoint → eliminar del RAG
                    logger.info(f"Eliminando {result.file_name} de {result.collection_name}")
                    _delete_from_backend_with_tenant(result.file_name, result.collection_name)
                    continue
                
                if result.local_path:
                    site_stats[site_name]["downloaded"] += 1
                    sharepoint_files_downloaded.labels(site=site_name).inc()
                    
                    try:
                        process_path(
                            result.local_path,
                            metadata={"source": "sharepoint", "site": site_name},
                            tenant_id=result.collection_name,
                            create_marker=False,  # No crear marker para evitar que scan_folder detecte falso borrado
                        )
                        site_stats[site_name]["indexed"] += 1
                        sharepoint_files_indexed.labels(site=site_name).inc()
                        
                        # Eliminar archivo local después de indexar exitosamente
                        local_file = Path(result.local_path)
                        if local_file.exists():
                            local_file.unlink()
                            logger.info(f"🗑️ Archivo local eliminado: {result.file_name}")
                    except Exception as e:
                        logger.error(f"Error procesando {result.file_name}: {e}")
                        site_stats[site_name]["errors"] += 1
            
            # Actualizar métricas finales para cada sitio
            sync_duration = time.time() - sync_start_time
            for site in multi_sync.sites:
                site_name = site.name
                stats = site_stats.get(site_name, {"downloaded": 0, "indexed": 0, "errors": 0})
                sync_bundle = multi_sync.synchronizers.get(site_name, {})
                sync_obj = sync_bundle.get("sync")
                delta_token = sync_obj._load_delta_token() if sync_obj else None
                
                # Determinar status del sync
                if stats["errors"] > 0 and stats["indexed"] == 0:
                    sync_status = "error"
                    sharepoint_sync_total.labels(site=site_name, status="error").inc()
                else:
                    sync_status = "success"
                    sharepoint_sync_total.labels(site=site_name, status="success").inc()
                
                sharepoint_sync_in_progress.labels(site=site_name).set(0)
                sharepoint_last_sync_timestamp.labels(site=site_name).set(time.time())
                sharepoint_sync_duration.labels(site=site_name).set(sync_duration)
                _report_sharepoint_sync_state(
                    site_id=site.site_id,
                    folder_path=site.folder_path,
                    collection_name=site.collection_name,
                    site_name=site_name,
                    delta_token=delta_token,
                    status=sync_status,
                    downloaded_files=stats["downloaded"],
                    indexed_files=stats["indexed"],
                    errors=stats["errors"],
                    message=f"Sync {sync_status}: downloaded={stats['downloaded']} indexed={stats['indexed']} errors={stats['errors']}",
                    is_active=site.enabled,
                )
                
                if stats["downloaded"] > 0 or stats["indexed"] > 0:
                    logger.info(f"📊 {site_name}: {stats['downloaded']} descargados, {stats['indexed']} indexados, {stats['errors']} errores")
                    
        except Exception as e:
            logger.error(f"Multi-site sync falló: {e}")
            # Marcar error en todas las métricas de sitios
            for site in multi_sync.sites:
                sharepoint_sync_total.labels(site=site.name, status="error").inc()
                sharepoint_sync_in_progress.labels(site=site.name).set(0)
                sync_bundle = multi_sync.synchronizers.get(site.name, {})
                sync_obj = sync_bundle.get("sync")
                delta_token = sync_obj._load_delta_token() if sync_obj else None
                _report_sharepoint_sync_state(
                    site_id=site.site_id,
                    folder_path=site.folder_path,
                    collection_name=site.collection_name,
                    site_name=site.name,
                    delta_token=delta_token,
                    status="error",
                    downloaded_files=0,
                    indexed_files=0,
                    errors=1,
                    message=str(e),
                    is_active=site.enabled,
                )
    
    # 3. Single-site SharePoint sync (legacy)
    elif sp_sync:
        logger.info("⏳ Tarea programada: sync SharePoint (single-site)")
        sharepoint_sync_in_progress.labels(site="legacy").set(1)
        try:
            sync_start_time = time.time()
            changes = sp_sync.sync()
            indexed_count = 0
            for ch in changes:
                sharepoint_files_downloaded.labels(site="legacy").inc()
                try:
                    process_path(ch["local_path"], {"source": "sharepoint"}, create_marker=False)
                    indexed_count += 1
                    sharepoint_files_indexed.labels(site="legacy").inc()
                except Exception as e:
                    logger.error(f"Error procesando cambio SharePoint {ch.get('name')}: {e}")
            
            sharepoint_sync_total.labels(site="legacy", status="success").inc()
            sharepoint_sync_in_progress.labels(site="legacy").set(0)
            sharepoint_last_sync_timestamp.labels(site="legacy").set(time.time())
            sharepoint_sync_duration.labels(site="legacy").set(time.time() - sync_start_time)
            _report_sharepoint_sync_state(
                site_id=SHAREPOINT_DRIVE_ID or SHAREPOINT_SITE_ID or "legacy",
                folder_path=SHAREPOINT_FOLDER_PATH,
                collection_name=INDEX_TENANT_ID or os.getenv("QDRANT_COLLECTION", "documents"),
                site_name="legacy",
                delta_token=sp_sync._load_delta_token(),
                status="success",
                downloaded_files=len(changes),
                indexed_files=indexed_count,
                errors=max(0, len(changes) - indexed_count),
                message=f"Legacy sync: downloaded={len(changes)} indexed={indexed_count}",
                is_active=True,
            )
        except Exception as e:
            logger.error(f"Delta sync SharePoint falló: {e}")
            sharepoint_sync_total.labels(site="legacy", status="error").inc()
            sharepoint_sync_in_progress.labels(site="legacy").set(0)
            _report_sharepoint_sync_state(
                site_id=SHAREPOINT_DRIVE_ID or SHAREPOINT_SITE_ID or "legacy",
                folder_path=SHAREPOINT_FOLDER_PATH,
                collection_name=INDEX_TENANT_ID or os.getenv("QDRANT_COLLECTION", "documents"),
                site_name="legacy",
                delta_token=sp_sync._load_delta_token(),
                status="error",
                downloaded_files=0,
                indexed_files=0,
                errors=1,
                message=str(e),
                is_active=True,
            )

# =========================
# Lifecycle
# =========================
@app.on_event("startup")
async def startup_event():
    logger.info(f"Iniciando Indexer (INDEX_MODE={INDEX_MODE})…")
    WATCH_FOLDER.mkdir(parents=True, exist_ok=True)
    scheduler.add_job(
        scheduled_job, 
        "interval", 
        seconds=SYNC_INTERVAL, 
        id="sync_job",
        replace_existing=True,  # Reemplaza job anterior si existe
        coalesce=True,          # Combina ejecuciones perdidas en una
        max_instances=1,        # Solo 1 instancia, pero coalesce las perdidas
        misfire_grace_time=60   # 60 seg de gracia para ejecuciones perdidas
    )
    scheduler.start()
    logger.info(f"✓ Scheduler iniciado (intervalo: {SYNC_INTERVAL}s)")

    # Multi-site: asegurar desde el inicio que todas las colecciones configuradas existen.
    # Así nuevos documentos se indexan automáticamente sin depender del primer upsert.
    if multi_sync:
        try:
            _ensure_backend_collections(multi_sync.get_collection_names())
        except Exception as e:
            logger.warning(f"No se pudieron asegurar colecciones multi-site al inicio: {e}")

    if SYNC_ON_START and sp_sync:
        logger.info("FULL sync SharePoint inicial…")
        try:
            downloaded = sp_sync.full_sync()
            for d in downloaded:
                try:
                    process_path(d["local_path"], {"source": "sharepoint"}, create_marker=False)
                except Exception as e:
                    logger.error(f"Error procesando archivo inicial {d.get('name')}: {e}")
        except Exception as e:
            logger.error(f"FULL sync falló: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    scheduler.shutdown(wait=False)
    logger.info("Indexer detenido")

# =========================
# Endpoints
# =========================

def _get_all_sites_from_config() -> dict:
    """Lee TODOS los sitios del config (habilitados o no) para el health check"""
    config_path = Path(SHAREPOINT_SITES_CONFIG)
    if not config_path.exists():
        return None
    
    try:
        import json
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        
        sites = []
        for site in config.get("sites", []):
            sites.append({
                "name": site.get("name", "unknown"),
                "collection": site.get("collection_name", ""),
                "enabled": site.get("enabled", False),
                "folder": site.get("folder_path", ""),
            })
        
        return {
            "total_sites": len(sites),
            "active_synchronizers": len([s for s in sites if s["enabled"]]),
            "sites": sites,
            "sync_interval": config.get("sync_settings", {}).get("interval_seconds", 300),
        }
    except Exception as e:
        logger.error(f"Error reading sites config: {e}")
        return None

@app.get("/scheduler")
async def get_scheduler_status():
    """Debug endpoint to check scheduler state"""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "next_run_time": str(job.next_run_time),
            "pending": job.pending
        })
    
    return {
        "running": scheduler.running,
        "state": scheduler.state,
        "jobs": jobs,
    }


@app.get("/health")
async def health():
    # Info de multi-site (priorizar multi_sync si existe, sino leer config)
    multi_site_info = None
    if multi_sync:
        multi_site_info = multi_sync.get_status()
    elif SHAREPOINT_MULTI_SITE:
        # Leer config directamente para mostrar todos los sitios
        multi_site_info = _get_all_sites_from_config()
    
    return {
        "status": "healthy",
        "version": "2.0.0",
        "index_mode": INDEX_MODE,
        "backend_url": BACKEND_URL if INDEX_MODE == "backend" else None,
        "watch_folder": str(WATCH_FOLDER),
        "sync_interval": SYNC_INTERVAL,
        "sharepoint_mode": "multi-site" if SHAREPOINT_MULTI_SITE else ("single-site" if sp_sync else "disabled"),
        "sharepoint_enabled": bool(multi_sync or sp_sync or SHAREPOINT_MULTI_SITE),
        "multi_site": multi_site_info,
        "tenant_header": bool(INDEX_TENANT_ID is not None),
    }


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint for SharePoint sync monitoring"""
    from starlette.responses import Response
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )

@app.post("/index")
async def index_endpoint(request: IndexRequest, background_tasks: BackgroundTasks):
    fp = Path(request.file_path)
    if not fp.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    background_tasks.add_task(process_path, str(fp), request.metadata or {})
    return {"status": "processing", "file": str(fp), "mode": INDEX_MODE}

@app.post("/scan")
async def scan_endpoint(background_tasks: BackgroundTasks):
    background_tasks.add_task(scan_folder, WATCH_FOLDER)
    return {"status": "scanning", "folder": str(WATCH_FOLDER)}

# --- Webhooks SharePoint ---
# Nota: Graph exige responder el validation token en **texto plano**.
@app.get("/webhooks/sharepoint", response_class=PlainTextResponse)
async def sp_webhook_validation(validationtoken: Optional[str] = None):
    if validationtoken:
        logger.info("Validación GET webhook SharePoint")
        return validationtoken
    raise HTTPException(status_code=400, detail="Missing validationtoken")

@app.post("/webhooks/sharepoint", response_class=PlainTextResponse)
async def sp_webhook_notify(
    request: Request,
    background_tasks: BackgroundTasks,
    validationtoken: Optional[str] = Header(None, alias="validationtoken"),
):
    if validationtoken:
        logger.info("Validación POST webhook SharePoint")
        return validationtoken

    try:
        payload = await request.json()
        items = payload.get("value", [])
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    accepted = 0
    for ev in items:
        if ev.get("clientState") != WEBHOOK_SECRET:
            logger.warning("Evento rechazado: clientState inválido")
            continue
        accepted += 1

    if accepted == 0:
        raise HTTPException(status_code=403, detail="No valid events")

    # Programamos una sync; el scan + sync manejará descargas e indexación
    background_tasks.add_task(scheduled_job)
    return "accepted"

@app.get("/")
async def root():
    return {"service": "RAG Indexer", "version": "1.3.0", "mode": INDEX_MODE}

@app.get("/stats")
async def get_stats():
    """Estadísticas del indexer"""
    watch_files = len(list(WATCH_FOLDER.rglob("*"))) if WATCH_FOLDER.exists() else 0
    indexed_markers = len(list(WATCH_FOLDER.rglob(".indexed_*"))) if WATCH_FOLDER.exists() else 0
    
    return {
        "mode": INDEX_MODE,
        "watch_folder": str(WATCH_FOLDER),
        "total_files": watch_files,
        "indexed_files": indexed_markers,
        "sharepoint_enabled": bool(sp_sync),
        "next_sync": "TODO",  # Puedes implementar esto con APScheduler
    }
