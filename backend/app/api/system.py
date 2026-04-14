# backend/app/api/system.py
"""
Router del Sistema — Health, Metrics y Root

Endpoints para monitoreo y diagnóstico:
- GET /: Información básica de la API.
- GET /health: Health check del sistema.
- GET /metrics: Métricas Prometheus (consumido por Prometheus cada 15s).
"""

import logging

from fastapi import APIRouter, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/health", tags=["System"])
async def health_check():
    """Health check del sistema (básico)"""
    return {
        "status": "healthy",
        "components": {
            "qdrant": "connected",
            "postgres": "connected",
            "ocr": "ready",
            "agent": "ready",
        },
    }


@router.get("/", tags=["System"])
async def root():
    """Root endpoint"""
    return {
        "name": "JARVIS RAG System API",
        "version": "2.1.0",
        "docs": "/docs",
    }


@router.get("/metrics", tags=["System"], include_in_schema=False)
async def metrics():
    """
    Prometheus metrics endpoint.
    Returns metrics in Prometheus text format.
    """
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )
