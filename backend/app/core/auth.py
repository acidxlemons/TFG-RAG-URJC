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
# Formato en .env: AZURE_GROUP_MAP=<group-uuid>=documents_CALIDAD,<group-uuid>=documents_HELIAP2
# Si no se configura, cualquier token válido accede a todas las colecciones del tenant.
_GROUP_MAP: dict[str, str] = {}
_raw_map = os.getenv("AZURE_GROUP_MAP", "")
if _raw_map.strip():
    for pair in _raw_map.split(","):
        if "=" in pair:
            gid, coll = pair.split("=", 1)
            _GROUP_MAP[gid.strip()] = coll.strip()


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
        if isinstance(custom, str):
            return [c.strip() for c in custom.split(",") if c.strip()]
        if isinstance(custom, list):
            return [str(c).strip() for c in custom if c]

    # 2. Roles de la aplicación → mapear a colecciones
    app_roles: List[str] = payload.get("roles", []) or []
    collections_from_roles = [r for r in app_roles if r.startswith("documents_")]
    if collections_from_roles:
        return collections_from_roles

    # 3. Grupos de Azure AD → mapear via AZURE_GROUP_MAP
    if _GROUP_MAP:
        user_groups: List[str] = payload.get("groups", []) or []
        mapped = [_GROUP_MAP[g] for g in user_groups if g in _GROUP_MAP]
        if mapped:
            return mapped
        # Usuario autenticado pero sin grupos mapeados
        logger.info(f"Usuario con sub={payload.get('sub','?')} no tiene grupos mapeados a colecciones")
        return []

    # 4. Sin mapeo configurado → usuario válido, acceso a todas las colecciones
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
