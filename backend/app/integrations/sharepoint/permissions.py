"""
backend/app/integrations/sharepoint/permissions.py

Sincronización automática de permisos SharePoint → RAG.

Al arrancar, usa la Graph API para obtener el grupo M365 asociado a cada sitio
de Teams/SharePoint mediante su mailNickname (extraído del webUrl del sitio).
No requiere configurar nombres de grupos manualmente.

Flujo para sitios normales:
  1. Para cada sitio en sharepoint_sites.json con site_id (y sin global_access)
  2. GET /sites/{siteId}?$select=webUrl  → obtener URL del sitio
  3. Extraer mailNickname del path: /teams/Departamento → "Departamento"
  4. GET /groups?$filter=mailNickname eq 'Departamento' → UUID del grupo M365
  5. Guardar UUID en azure_group_ids (caché en disco)
  6. Construir mapeo {uuid: colección_qdrant} para validación JWT

Sitios con global_access: true:
  - No se sincronizan grupos (toda la empresa tiene acceso)
  - Su collection_name se devuelve en get_global_collections()

Al añadir un sitio nuevo en sharepoint_sites.json solo necesitas:
  - name, site_id, collection_name, enabled
  - azure_groups ya NO es necesario (se obtiene automáticamente)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Ruta al fichero de configuración de sitios.
# Dentro del contenedor: /workspace/config/sharepoint_sites.json (volumen montado)
_CONFIG_PATH = Path(
    os.getenv(
        "SHAREPOINT_SITES_CONFIG",
        "/workspace/config/sharepoint_sites.json",
    )
)


def _load_config(path: Optional[Path] = None) -> dict:
    p = path or _CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_config(config: dict, path: Optional[Path] = None):
    p = path or _CONFIG_PATH
    with open(p, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
    logger.info(f"sharepoint_sites.json actualizado en {p}")


def build_group_map(config: dict) -> Dict[str, str]:
    """
    Construye el mapeo {group_uuid: collection_name} a partir de los
    azure_group_ids almacenados en sharepoint_sites.json.

    Solo incluye sitios sin global_access (los globales no necesitan mapeo JWT).
    Usado por auth.py para validar tokens JWT.
    """
    group_map: Dict[str, str] = {}
    for site in config.get("sites", []):
        if not site.get("enabled", True):
            continue
        if site.get("global_access"):
            continue  # colecciones globales se gestionan aparte
        collection = site.get("collection_name")
        if not collection:
            continue
        for uuid in site.get("azure_group_ids", {}).values():
            if uuid:
                group_map[uuid] = collection
    return group_map


def get_global_collections(config_path: Optional[Path] = None) -> List[str]:
    """
    Devuelve la lista de colecciones con acceso global (global_access: true).
    Estas colecciones son accesibles para todos los usuarios autenticados,
    independientemente de los grupos JWT del usuario.
    """
    try:
        config = _load_config(config_path)
    except Exception as e:
        logger.warning(f"No se pudo leer config para colecciones globales: {e}")
        return []

    return [
        site["collection_name"]
        for site in config.get("sites", [])
        if site.get("enabled", True)
        and site.get("global_access")
        and site.get("collection_name")
    ]


def _extract_mail_nickname(web_url: str) -> Optional[str]:
    """
    Extrae el mailNickname del grupo M365 a partir del webUrl del sitio.

    Para sitios de Teams el path tiene forma /teams/<Nickname> o /sites/<Nickname>.
    Ejemplos:
      https://miempresa.sharepoint.com/teams/Departamento  → "Departamento"
      https://miempresa.sharepoint.com/sites/Calidad       → "Calidad"
    """
    path = urlparse(web_url).path.strip("/")
    # path = "teams/Departamento" o "sites/Calidad" etc.
    parts = path.split("/")
    if len(parts) >= 2:
        return parts[-1]  # último segmento
    if parts:
        return parts[0]
    return None


def sync_permissions(sharepoint_client, config_path: Optional[Path] = None) -> Dict[str, str]:
    """
    Para cada sitio habilitado sin global_access en sharepoint_sites.json:
      1. Obtiene el webUrl del sitio via Graph API
      2. Extrae el mailNickname del grupo M365 del path del webUrl
      3. Busca el UUID del grupo M365 por mailNickname (Group.Read.All)
      4. Guarda el UUID en azure_group_ids (caché en disco)
      5. Retorna el mapeo completo {uuid: collection} para validación JWT

    Los sitios con global_access: true se omiten (toda la empresa tiene acceso).
    Los UUIDs se cachean en disco. Si el grupo no cambia, no se vuelve a llamar
    a Graph API en reinicios.

    Args:
        sharepoint_client: instancia de SharePointClient autenticado
        config_path: ruta alternativa al JSON (opcional, para tests)

    Returns:
        {group_uuid: collection_name}
    """
    try:
        config = _load_config(config_path)
    except FileNotFoundError:
        logger.warning("sharepoint_sites.json no encontrado, omitiendo sync de permisos")
        return {}

    changed = False

    for site in config.get("sites", []):
        if not site.get("enabled", True):
            continue

        site_name = site.get("name", "?")

        # Sitios con acceso global: no se sincronizan grupos
        if site.get("global_access"):
            logger.info(f"[{site_name}] Acceso global — colección disponible para todos los usuarios autenticados")
            continue

        site_id = site.get("site_id")
        if not site_id:
            logger.warning(f"[{site_name}] Sin site_id, saltando")
            continue

        collection = site.get("collection_name")
        if not collection:
            logger.warning(f"[{site_name}] Sin collection_name, saltando")
            continue

        # ── Paso 1: Obtener webUrl del sitio ──────────────────────────────
        logger.info(f"[{site_name}] Obteniendo URL del sitio...")
        site_info = sharepoint_client.get_site_info(site_id)

        if not site_info or not site_info.get("webUrl"):
            logger.warning(f"[{site_name}] No se pudo obtener webUrl, saltando")
            continue

        web_url = site_info["webUrl"]
        display_name = site_info.get("displayName", "")

        # ── Paso 2: Extraer mailNickname y buscar grupo M365 ──────────────
        nickname = _extract_mail_nickname(web_url)
        group = None

        if nickname:
            logger.info(f"[{site_name}] Buscando grupo M365 con mailNickname='{nickname}'...")
            group = sharepoint_client.find_group_by_mail_nickname(nickname)

        if not group and display_name:
            # Fallback: buscar por displayName del sitio
            logger.info(f"[{site_name}] Intentando por displayName='{display_name}'...")
            group = sharepoint_client.find_group_by_name(display_name)

        new_ids: Dict[str, str] = {}
        if group:
            new_ids = {group["displayName"]: group["id"]}
            logger.info(f"  ✓ [{site_name}] '{group['displayName']}' → {group['id']}")
        else:
            logger.warning(
                f"[{site_name}] No se encontró grupo M365 "
                f"(mailNickname='{nickname}', displayName='{display_name}'). "
                f"Nadie podrá acceder a '{collection}' hasta que se resuelva."
            )

        old_ids = site.get("azure_group_ids", {})
        if new_ids != old_ids:
            site["azure_group_ids"] = new_ids
            changed = True
        else:
            logger.info(f"[{site_name}] Permisos sin cambios ({len(new_ids)} grupos)")

    if changed:
        _save_config(config, config_path)

    group_map = build_group_map(config)
    collections = sorted(set(group_map.values()))
    logger.info(
        f"Permisos sincronizados: {len(group_map)} grupos en {len(collections)} colecciones: "
        + (", ".join(collections) if collections else "(ninguna)")
    )
    return group_map


def get_cached_group_map(config_path: Optional[Path] = None) -> Dict[str, str]:
    """
    Devuelve el group_map desde los UUIDs ya cacheados en disco,
    SIN hacer llamadas a Graph API.

    Útil como fallback si Graph API no está disponible al arrancar.
    """
    try:
        config = _load_config(config_path)
        return build_group_map(config)
    except Exception as e:
        logger.warning(f"No se pudo leer group map cacheado: {e}")
        return {}
