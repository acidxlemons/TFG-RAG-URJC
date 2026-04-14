# backend/app/core/rag/reranker.py
import logging
import os
from typing import Any, List

logger = logging.getLogger(__name__)

DEFAULT_RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

try:
    from sentence_transformers import CrossEncoder

    CROSS_ENCODER_AVAILABLE = True
except ImportError:
    CROSS_ENCODER_AVAILABLE = False
    logger.warning("sentence-transformers no esta instalado. El reranker no funcionara.")


class Reranker:
    """
    Reranker basado en Cross-Encoder para mejorar la relevancia
    de los documentos recuperados.
    """

    def __init__(self, model_name_or_path: str = None):
        self.model = None
        self.model_name = self._resolve_model_name(
            model_name_or_path or os.getenv("RERANKER_MODEL")
        )
        self.device = (os.getenv("RERANKER_DEVICE", "cpu") or "cpu").strip().lower()

        if not CROSS_ENCODER_AVAILABLE:
            logger.warning("Reranker deshabilitado por falta de dependencias.")
            return

        try:
            self.model = self._load_model(self.model_name, self.device)
            logger.info("Modelo Cross-Encoder cargado correctamente")
        except Exception as e:
            logger.error(f"Error cargando el modelo Cross-Encoder '{self.model_name}': {e}")
            self.model = None

    @staticmethod
    def _resolve_model_name(model_name_or_path: str | None) -> str:
        configured = (model_name_or_path or "").strip()
        if not configured:
            return DEFAULT_RERANKER_MODEL

        looks_like_path = any(
            marker in configured for marker in (os.sep, "/", "\\")
        ) or configured.startswith(".")
        if looks_like_path and not os.path.exists(configured):
            logger.warning(
                "RERANKER_MODEL apunta a '%s' pero no existe. Se usara '%s'.",
                configured,
                DEFAULT_RERANKER_MODEL,
            )
            return DEFAULT_RERANKER_MODEL

        return configured

    def _load_model(self, model_name: str, device: str):
        logger.info(f"Cargando modelo Cross-Encoder: {model_name} (device={device})")
        try:
            return CrossEncoder(model_name, device=device)
        except TypeError:
            logger.warning("CrossEncoder no soporta parametro 'device'; se usa la configuracion por defecto.")
            self.device = "default"
            return CrossEncoder(model_name)

    def _score_results(self, query: str, results: List[Any]) -> List[Any]:
        pairs = [[query, getattr(result, "text", "")] for result in results]
        scores = self.model.predict(pairs)

        for idx, result in enumerate(results):
            result.score = float(scores[idx])

        results.sort(key=lambda item: item.score, reverse=True)
        return results

    def _maybe_fallback_to_cpu(self, error: Exception) -> bool:
        if self.device in {"cpu", "default"}:
            return False

        error_text = str(error).lower()
        if "cuda" not in error_text and "cublas" not in error_text and "device-side" not in error_text:
            return False

        try:
            logger.warning("Fallo CUDA en reranker; reintentando en CPU.")
            self.model = self._load_model(self.model_name, "cpu")
            self.device = "cpu"
            return True
        except Exception as fallback_error:
            logger.error(f"No se pudo reconfigurar el reranker a CPU: {fallback_error}")
            self.model = None
            return False

    def rerank(self, query: str, results: List[Any], top_k: int = 5) -> List[Any]:
        """
        Reordena una lista de resultados usando el cross-encoder.
        """
        if not self.model or not results:
            return results[:top_k]

        try:
            return self._score_results(query, results)[:top_k]
        except Exception as e:
            if self._maybe_fallback_to_cpu(e) and self.model is not None:
                try:
                    return self._score_results(query, results)[:top_k]
                except Exception as retry_error:
                    logger.error(f"Error durante el reranking tras fallback a CPU: {retry_error}")
                    return results[:top_k]

            logger.error(f"Error durante el reranking: {e}")
            return results[:top_k]
