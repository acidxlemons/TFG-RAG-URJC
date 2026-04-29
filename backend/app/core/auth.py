"""
backend/app/core/auth.py

Validación de tokens JWT de Azure AD para multi-tenant.

Extrae las colecciones permitidas a partir de las claims del token:
  - claim "groups" → IDs de grupos Azure AD → se mapean a colecciones Qdrant
  - claim "roles"  → roles de la app → se mapean a colecciones
  - claim "tenant_collections" → claim personalizado (si el frontend lo incluye)

Activación: variable de entorno AZURE_JWT_VALIDATION=true
Si está desactivada (por defecto), el sistema sigue usando los headers X-Tenant-Id
tal como hasta ahora (compatibilidad total con la instalación existente).
"""

from __future__ import annotations

import logging
import os
import time
from functools import lru_cache
from typing import List, Optional

import requests
from jwt import decode as jwt_decode, PyJWKClient, ExpiredSignatureError, InvalidTokenError

logger = logging.getLogger(__name__)

# ── Configuración ──────────────────────────────────────────

_AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "")
_AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "")
_JWT_ENABLED = os.getenv("AZURE_JWT_VALIDATION", "false").lower() in {"1", "true", "yes"}

# Mapeo de group IDs de Azure AD a nombres de colección Qdrant.
# Se construye automáticamente desde sharepoint_sites.json al arrancar
# (ver main.py → sync_permissions). Puede complementarse con AZURE_GROUP_MAP en .env:
#   AZURE_GROUP_MAP=<group-uuid>=documents_CALIDAD,<group-uuid>=documents_HELIAP2
_GROUP_MAP: dict[str, str] = {}

# Colecciones con acceso global: accesibles para cualquier usuario autenticado
# sin necesidad de pertenecer a un grupo concreto.
# Se pobla desde sharepoint_sites.json (global_access: true) al arrancar.
_GLOBAL_COLLECTIONS: List[str] = []

# Cargar mapeo manual desde .env como semilla inicial (si existe)
_raw_map = os.getenv("AZURE_GROUP_MAP", "")
if _raw_map.strip():
    for pair in _raw_map.split(","):
        if "=" in pair:
            gid, coll = pair.split("=", 1)
            _GROUP_MAP[gid.strip()] = coll.strip()


def update_group_map(new_map: dict[str, str]) -> None:
    """
    Actualiza el mapeo grupo_uuid → colección en tiempo de ejecución.

    Llamado desde main.py después de que sync_permissions() resuelve
    los UUIDs de los grupos de Azure AD definidos en sharepoint_sites.json.
    Los grupos del .env (AZURE_GROUP_MAP) se mantienen y los nuevos se añaden.
    """
    global _GROUP_MAP
    _GROUP_MAP.update(new_map)
    logger.info(f"Group map actualizado: {len(_GROUP_MAP)} entradas totales")


def update_global_collections(collections: List[str]) -> None:
    """
    Registra las colecciones con acceso global (global_access: true en sharepoint_sites.json).

    Llamado desde main.py al arrancar. Estas colecciones se añaden automáticamente
    a cualquier usuario autenticado válido, independientemente de sus grupos JWT.
    """
    global _GLOBAL_COLLECTIONS
    _GLOBAL_COLLECTIONS = list(collections)
    logger.info(f"Colecciones globales registradas: {_GLOBAL_COLLECTIONS}")


# ── JWKS client con caché (rota automáticamente) ───────────

@lru_cache(maxsize=1)
def _get_jwks_client() -> Optional[PyJWKClient]:
    if not _AZURE_TENANT_ID:
        return None
    jwks_uri = f"https://login.microsoftonline.com/{_AZURE_TENANT_ID}/discovery/v2.0/keys"
    try:
        return PyJWKClient(jwks_uri, cache_jwk_set=True, lifespan=3600)
    except Exception as e:
        logger.warning(f"No se pudo inicializar JWKS client: {e}")
        return None


# ── Función principal ──────────────────────────────────────

def extract_allowed_collections(authorization_header: Optional[str]) -> Optional[List[str]]:
    """
    Valida el Bearer token de Azure AD y devuelve las colecciones permitidas.

    Returns:
        - None  → validación desactivada o token ausente; usa lógica de headers normal
        - []    → token inválido o sin grupos autorizados (denegar acceso)
        - [...]  → lista de colecciones Qdrant permitidas para este usuario
    """
    if not _JWT_ENABLED:
        return None

    if not authorization_header or not authorization_header.startswith("Bearer "):
        return None

    raw_token = authorization_header.split(" ", 1)[1].strip()
    if not raw_token:
        return None

    jwks = _get_jwks_client()
    if jwks is None:
        logger.warning("JWKS client no disponible, saltando validación JWT")
        return None

    try:
        signing_key = jwks.get_signing_key_from_jwt(raw_token)
        payload = jwt_decode(
            raw_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=_AZURE_CLIENT_ID or None,
            options={"verify_exp": True, "verify_aud": bool(_AZURE_CLIENT_ID)},
        )
    except ExpiredSignatureError:
        logger.warning("Token JWT expirado")
        return []
    except InvalidTokenError as e:
        logger.warning(f"Token JWT inválido: {e}")
        return []
    except Exception as e:
        logger.warning(f"Error validando JWT: {e}")
        return None  # Fallo abierto: si el JWKS no responde, no bloqueamos

    # ── Extraer colecciones del payload ───────────────────

    # 1. Claim personalizado "tenant_collections" (más sencillo si el frontend lo mete)
    custom = payload.get("tenant_collections")
    if custom:
        result: List[str] = []
        if isinstance(custom, str):
            result = [c.strip() for c in custom.split(",") if c.strip()]
        elif isinstance(custom, list):
            result = [str(c).strip() for c in custom if c]
        # Añadir colecciones globales aunque venga claim personalizado
        return list(dict.fromkeys(result + _GLOBAL_COLLECTIONS))

    # 2. Roles de la aplicación → mapear a colecciones
    app_roles: List[str] = payload.get("roles", []) or []
    collections_from_roles = [r for r in app_roles if r.startswith("documents_")]
    if collections_from_roles:
        return list(dict.fromkeys(collections_from_roles + _GLOBAL_COLLECTIONS))

    # 3. Grupos de Azure AD → mapear via group_map
    if _GROUP_MAP:
        user_groups: List[str] = payload.get("groups", []) or []
        mapped = [_GROUP_MAP[g] for g in user_groups if g in _GROUP_MAP]
        # Siempre incluir colecciones globales para usuarios autenticados
        all_collections = list(dict.fromkeys(mapped + _GLOBAL_COLLECTIONS))
        if not mapped and not _GLOBAL_COLLECTIONS:
            logger.info(f"Usuario sub={payload.get('sub','?')} sin grupos mapeados a colecciones")
        return all_collections

    # 4. Sin mapeo configurado pero hay colecciones globales → devolver solo las globales
    if _GLOBAL_COLLECTIONS:
        return list(_GLOBAL_COLLECTIONS)

    # 5. Sin ningún mapeo → usuario válido, acceso a todas las colecciones
    #    (mantiene comportamiento actual; el administrador puede añadir mapeo después)
    return None


def get_user_info(authorization_header: Optional[str]) -> dict:
    """Extrae info básica del token para logging (sin validar firma completa)."""
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return {}
    try:
        raw = authorization_header.split(" ", 1)[1].strip()
        # Decodificar sin verificar para solo leer claims de identidad
        payload = jwt_decode(raw, options={"verify_signature": False})
        return {
            "sub": payload.get("sub", ""),
            "upn": payload.get("upn") or payload.get("preferred_username", ""),
            "name": payload.get("name", ""),
        }
    except Exception:
        return {}
