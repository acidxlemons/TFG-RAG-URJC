"""
backend/app/api/health.py

Health and metrics endpoints.
"""

from typing import Dict
import logging
import os
import time

import requests as _requests
from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.state import app_state, LITELLM_BASE_URL, LITELLM_API_KEY

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)

DEFAULT_SECRET_VALUES = {
    "POSTGRES_PASSWORD": {"changeme"},
    "MINIO_ROOT_PASSWORD": {"minio_password"},
    "LITELLM_MASTER_KEY": {"sk-1234"},
    "GRAFANA_PASSWORD": {"admin"},
    "WEBHOOK_SECRET": {"changeme"},
    "JWT_SECRET": {"change-this-jwt-secret"},
    "ENCRYPTION_KEY": {"change-this-32-char-key-here!!"},
}


def _get_default_secret_warnings() -> list[Dict[str, str]]:
    warnings = []
    for env_name, insecure_values in DEFAULT_SECRET_VALUES.items():
        current = os.getenv(env_name)
        if current and current in insecure_values:
            warnings.append(
                {
                    "env": env_name,
                    "message": f"{env_name} sigue usando un valor por defecto inseguro",
                }
            )
    return warnings


@router.get("/health")
async def health_check() -> Dict:
    """Basic health check."""
    return {"status": "healthy"}


@router.get("/health/detailed")
async def detailed_health() -> Dict:
    """Detailed health check with service status."""
    health_status = {
        "status": "healthy",
        "services": {},
    }

    try:
        qdrant = app_state.qdrant
        collections = qdrant.get_collections()
        health_status["services"]["qdrant"] = {
            "status": "healthy",
            "collections": len(collections.collections),
        }
    except Exception as e:
        health_status["status"] = "degraded"
        health_status["services"]["qdrant"] = {
            "status": "unhealthy",
            "error": str(e),
        }

    try:
        retriever = app_state.retriever
        embedder = getattr(retriever, "embedder", None)
        if embedder is None:
            raise RuntimeError("Retriever embedder not initialized")
        health_status["services"]["embeddings"] = {
            "status": "healthy",
            "model": embedder.model_name,
            "dimension": embedder.dimension,
        }
    except Exception as e:
        health_status["status"] = "degraded"
        health_status["services"]["embeddings"] = {
            "status": "unhealthy",
            "error": str(e),
        }

    try:
        ocr = app_state.ocr_pipeline
        health_status["services"]["ocr"] = {
            "status": "healthy",
            "language": getattr(ocr, "lang", "unknown"),
            "workers": getattr(ocr, "num_workers", "unknown"),
            "gpu": getattr(ocr, "use_gpu", "unknown"),
        }
    except Exception as e:
        health_status["status"] = "degraded"
        health_status["services"]["ocr"] = {
            "status": "unhealthy",
            "error": str(e),
        }

    try:
        counts = app_state.memory.get_operational_counts()
        health_status["services"]["sql_registry"] = {
            "status": "healthy",
            **counts,
        }
    except Exception as e:
        health_status["status"] = "degraded"
        health_status["services"]["sql_registry"] = {
            "status": "unhealthy",
            "error": str(e),
        }

    # Redis ping
    try:
        redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
        import redis as _redis
        r = _redis.from_url(redis_url, socket_connect_timeout=2)
        r.ping()
        health_status["services"]["redis"] = {"status": "healthy", "url": redis_url}
    except Exception as e:
        health_status["status"] = "degraded"
        health_status["services"]["redis"] = {"status": "unhealthy", "error": str(e)}

    # LiteLLM / Ollama — modelos disponibles
    try:
        t0 = time.time()
        resp = _requests.get(
            f"{LITELLM_BASE_URL}/v1/models",
            headers={"Authorization": f"Bearer {LITELLM_API_KEY}"},
            timeout=5,
        )
        resp.raise_for_status()
        model_ids = [m["id"] for m in resp.json().get("data", [])]
        health_status["services"]["litellm"] = {
            "status": "healthy",
            "latency_ms": round((time.time() - t0) * 1000),
            "models": model_ids,
        }
    except Exception as e:
        health_status["status"] = "degraded"
        health_status["services"]["litellm"] = {"status": "unhealthy", "error": str(e)}

    # QueryProcessor state
    try:
        qp = app_state.query_processor
        health_status["services"]["query_processor"] = {
            "status": "healthy",
            "expansion_enabled": qp.enable_expansion,
            "max_expansions": qp.max_expansions,
            "primary_model": qp._primary_model,
            "analytical_model": qp._analytical_model,
        }
    except Exception as e:
        health_status["status"] = "degraded"
        health_status["services"]["query_processor"] = {"status": "unhealthy", "error": str(e)}

    secret_warnings = _get_default_secret_warnings()
    health_status["security"] = {
        "status": "warning" if secret_warnings else "healthy",
        "default_secret_warnings": secret_warnings,
    }

    return health_status


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus metrics endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
