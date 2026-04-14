# backend/app/core/memory/manager.py
"""
Sistema de Memoria Conversacional por Usuario
Almacena historial en Postgres y genera resúmenes automáticos
"""

from __future__ import annotations

from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import logging
import os

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.exc import IntegrityError
try:
    # Postgres recomendado
    from sqlalchemy.dialects.postgresql import JSONB as JSONType
except Exception:  # pragma: no cover
    # Fallback si no es Postgres
    from sqlalchemy import JSON as JSONType  # type: ignore

logger = logging.getLogger(__name__)

Base = declarative_base()


# ============================================
# MODELOS DE BASE DE DATOS
# ============================================

class User(Base):
    """Tabla de usuarios (desde Azure SSO)"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    azure_id = Column(String(255), unique=True, nullable=False, index=True)
    # Mantengo NOT NULL pero meto fallback al crear si Azure no trae email
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)

    # Relaciones
    conversations = relationship(
        "Conversation",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_users_azure_id", "azure_id"),
        Index("ix_users_email", "email"),
    )


class Conversation(Base):
    """Conversación (sesión de chat)"""
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(500))  # Auto-generado del primer mensaje
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_archived = Column(Boolean, default=False)

    # Resumen de la conversación (generado periódicamente)
    summary = Column(Text, nullable=True)
    summary_generated_at = Column(DateTime, nullable=True)

    # Metadata adicional  ⬅️  ATRIBUTO PYTHON 'meta', COLUMNA SQL 'metadata'
    meta = Column('metadata', JSONType, default=dict)

    # Relaciones
    user = relationship("User", back_populates="conversations")
    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.created_at.asc()",
    )

    __table_args__ = (
        Index("ix_conversations_user_id", "user_id"),
        Index("ix_conversations_updated_at", "updated_at"),
        Index("ix_conversations_is_archived", "is_archived"),
    )


class Message(Base):
    """Mensaje individual en una conversación"""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)

    role = Column(String(50), nullable=False)  # 'user', 'assistant', 'system'
    content = Column(Text, nullable=False)

    # Metadata de RAG
    sources_used = Column(JSONType, nullable=True)  # Lista de documentos citados
    retrieval_count = Column(Integer, default=0)  # Cuántos docs se recuperaron

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Métricas de ejecución (alineado con init.sql)
    tokens_used = Column(Integer, default=0)
    processing_time_ms = Column(Integer, nullable=True)

    # Metadata adicional (tokens, latencia, modelo, tenant, errores, etc.)
    # ⬅️  ATRIBUTO PYTHON 'meta', COLUMNA SQL 'metadata'
    meta = Column('metadata', JSONType, default=dict)

    # Relaciones
    conversation = relationship("Conversation", back_populates="messages")

    __table_args__ = (
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
    )


class IngestionStatus(Base):
    """Estado de ingestión de documentos"""
    __tablename__ = "ingestion_status"

    filename = Column(String(255), primary_key=True)
    status = Column(String(50), nullable=False)  # pending, processing, completed, failed
    message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_ingestion_status_updated_at", "updated_at"),
    )


class IndexedDocument(Base):
    """Registro SQL de documentos indexados."""
    __tablename__ = "indexed_documents"

    id = Column(Integer, primary_key=True)
    filename = Column(String(500), nullable=False)
    source_path = Column(Text, nullable=False)
    source_type = Column(String(50), nullable=False)
    file_hash = Column(String(64), unique=True, nullable=False, index=True)
    file_size = Column(BigInteger, nullable=True)
    mime_type = Column(String(100), nullable=True)
    page_count = Column(Integer, nullable=True)
    chunk_count = Column(Integer, nullable=True)
    from_ocr = Column(Boolean, default=False)
    indexed_at = Column(DateTime, default=datetime.utcnow)
    indexed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    meta = Column("metadata", JSONType, default=dict)
    status = Column(String(50), default="indexed")

    __table_args__ = (
        Index("ix_indexed_documents_filename", "filename"),
        Index("ix_indexed_documents_status", "status"),
        Index("ix_indexed_documents_indexed_at", "indexed_at"),
    )


class SharePointSyncState(Base):
    """Estado persistido de sincronizaciones SharePoint."""
    __tablename__ = "sharepoint_sync"

    id = Column(Integer, primary_key=True)
    site_id = Column(String(255), nullable=False)
    folder_path = Column(Text, nullable=False)
    delta_token = Column(Text, nullable=True)
    last_sync = Column(DateTime, nullable=True)
    subscription_id = Column(String(255), nullable=True)
    subscription_expires = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)

    __table_args__ = (
        Index("ix_sharepoint_sync_site_folder", "site_id", "folder_path"),
    )


class AuditLog(Base):
    """Audit log operativo mínimo."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(100), nullable=False, index=True)
    resource_type = Column(String(50), nullable=True, index=True)
    resource_id = Column(Integer, nullable=True)
    details = Column(JSONType, default=dict)
    ip_address = Column(Text, nullable=True)
    user_agent = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)



# ============================================
# GESTOR DE MEMORIA
# ============================================

@dataclass
class ConversationContext:
    """Contexto de conversación para el LLM"""
    conversation_id: int
    user_id: int
    messages: List[Dict]  # [{"role": "user", "content": "..."}]
    summary: Optional[str]  # Resumen de mensajes antiguos
    total_messages: int


class MemoryManager:
    """
    Gestor de Memoria Conversacional

    Funciones:
    1. Almacenar mensajes de usuarios
    2. Recuperar historial por conversación
    3. Generar resúmenes automáticos
    4. Limpiar conversaciones antiguas
    """

    def __init__(
        self,
        database_url: str,
        summarizer_llm=None,  # LLM para generar resúmenes (opcional)
        max_messages_before_summary: int = 10,
        context_window_size: int = 5,  # Últimos N mensajes siempre incluidos
        litellm_base_url: Optional[str] = None,
        litellm_api_key: Optional[str] = None,
        llm_model: Optional[str] = None,
    ):
        # Conexión a Postgres
        self.engine = create_engine(database_url, pool_pre_ping=True, future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False)

        self.summarizer_llm = summarizer_llm
        self.max_messages_before_summary = max(1, int(max_messages_before_summary))
        self.context_window_size = max(1, int(context_window_size))

        # Configuración HTTP para LiteLLM (fallback de summarizer_llm)
        self._litellm_url = (
            litellm_base_url
            or os.getenv("LITELLM_URL", "http://litellm:4000")
        ).rstrip("/")
        self._litellm_key = (
            litellm_api_key
            or os.getenv("LITELLM_API_KEY")
            or os.getenv("LITELLM_MASTER_KEY", "sk-1234")
        )
        self._llm_model = llm_model or os.getenv("LLM_MODEL", "JARVIS")

        # Permite ajustar cada cuánto se regenera el resumen vía env
        self.summary_ttl_seconds = int(os.getenv("SUMMARY_TTL_SECONDS", str(3600)))

        logger.info("MemoryManager inicializado")

    @staticmethod
    def _coerce_datetime(value: Optional[Any]) -> Optional[datetime]:
        """Normaliza strings ISO o datetime a UTC naive."""
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            if value.tzinfo:
                return value.astimezone(timezone.utc).replace(tzinfo=None)
            return value
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if parsed.tzinfo:
                return parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except Exception:
            logger.warning("No se pudo parsear datetime: %s", value)
            return None

    @staticmethod
    def _normalize_string_list(values: Optional[Any]) -> List[str]:
        if values is None or values == "":
            raw_values: List[Any] = []
        elif isinstance(values, list):
            raw_values = values
        else:
            raw_values = [values]
        items = []
        for value in raw_values:
            text = str(value or "").strip()
            if text and text not in items:
                items.append(text)
        return items

    @classmethod
    def _merge_metadata(cls, current: Optional[Dict], incoming: Optional[Dict]) -> Dict:
        """Merge conservador para metadata JSON."""
        merged = dict(current or {})
        for key, value in (incoming or {}).items():
            if value is None:
                continue
            if isinstance(value, list):
                current_value = merged.get(key, [])
                if isinstance(current_value, list):
                    current_list = current_value
                elif current_value is None or current_value == "":
                    current_list = []
                else:
                    current_list = [current_value]
                merged[key] = cls._normalize_string_list(
                    cls._normalize_string_list(current_list) + cls._normalize_string_list(value)
                )
            elif isinstance(value, dict) and isinstance(merged.get(key), dict):
                child = dict(merged.get(key) or {})
                child.update(value)
                merged[key] = child
            else:
                merged[key] = value
        return merged

    # ==================== GESTIÓN DE USUARIOS ====================

    @staticmethod
    def _sync_user_fields(user: User, email: Optional[str], name: Optional[str]) -> None:
        """Actualiza datos volátiles del usuario cuando llegan del IdP o del cliente."""
        user.last_active = datetime.utcnow()
        if email and user.email != email:
            user.email = email
        if name and user.name != name:
            user.name = name

    def get_or_create_user(self, azure_id: str, email: Optional[str], name: Optional[str]) -> int:
        """
        Obtiene o crea usuario desde Azure SSO
        Returns: user_id
        """
        session = self.Session()
        try:
            safe_email = email or f"{azure_id}@unknown.local"

            user = session.query(User).filter(User.azure_id == azure_id).one_or_none()
            if user:
                self._sync_user_fields(user, safe_email, name)
                session.commit()
                return user.id

            user = session.query(User).filter(User.email == safe_email).one_or_none()
            if user:
                if user.azure_id != azure_id:
                    other = session.query(User).filter(User.azure_id == azure_id).one_or_none()
                    if other and other.id != user.id:
                        logger.warning(
                            "Conflicto resolviendo usuario: azure_id %s ya pertenece al user %s; "
                            "se conserva el registro asociado al email %s",
                            azure_id,
                            other.id,
                            safe_email,
                        )
                    else:
                        user.azure_id = azure_id
                self._sync_user_fields(user, safe_email, name)
                session.commit()
                return user.id

            user = User(azure_id=azure_id, email=safe_email, name=name or "")
            session.add(user)
            session.commit()
            logger.info(f"Usuario creado: {safe_email} (ID: {user.id})")
            return user.id
        except IntegrityError:
            session.rollback()
            user = session.query(User).filter(
                (User.azure_id == azure_id) | (User.email == safe_email)
            ).one_or_none()
            if user:
                if user.azure_id != azure_id and not session.query(User).filter(User.azure_id == azure_id).one_or_none():
                    user.azure_id = azure_id
                self._sync_user_fields(user, safe_email, name)
                session.commit()
                return user.id
            raise
        except Exception as e:
            session.rollback()
            logger.error(f"Error get_or_create_user: {e}")
            raise
        finally:
            session.close()

    def ensure_user_reference(self, user_id: int, email: Optional[str] = None, name: Optional[str] = None) -> int:
        """
        Garantiza que exista un usuario utilizable a partir de un user_id externo.

        Casos que cubre:
        - El user_id ya existe en la tabla users.
        - El cliente manda email pero no azure_id: reutilizamos ese usuario si existe.
        - El cliente solo manda un user_id numérico: creamos/reutilizamos una identidad local estable.
        """
        external_user_id = int(user_id)
        synthetic_azure_id = f"local-user:{external_user_id}"

        session = self.Session()
        try:
            user = session.get(User, external_user_id)
            if user:
                self._sync_user_fields(user, email, name)
                session.commit()
                return user.id

            if email:
                user = session.query(User).filter(User.email == email).one_or_none()
                if user:
                    self._sync_user_fields(user, email, name)
                    session.commit()
                    return user.id

            user = session.query(User).filter(User.azure_id == synthetic_azure_id).one_or_none()
            if user:
                self._sync_user_fields(user, email, name)
                session.commit()
                return user.id

            safe_email = email or f"user-{external_user_id}@unknown.local"
            user = User(
                azure_id=synthetic_azure_id,
                email=safe_email,
                name=name or f"User {external_user_id}",
            )
            session.add(user)
            session.commit()
            logger.info(f"Usuario local creado para user_id externo {external_user_id}: {user.id}")
            return user.id
        except Exception as e:
            session.rollback()
            logger.error(f"Error ensure_user_reference: {e}")
            raise
        finally:
            session.close()

    # ==================== GESTIÓN DE CONVERSACIONES ====================

    def create_conversation(self, user_id: int, title: Optional[str] = None) -> int:
        """Crea nueva conversación y devuelve su ID"""
        session = self.Session()
        try:
            conversation = Conversation(user_id=user_id, title=title or "Nueva conversación")
            session.add(conversation)
            session.commit()
            logger.info(f"Conversación creada: {conversation.id} para user {user_id}")
            return conversation.id
        except Exception as e:
            session.rollback()
            logger.error(f"Error create_conversation: {e}")
            raise
        finally:
            session.close()

    def get_user_conversations(
        self,
        user_id: int,
        include_archived: bool = False,
        limit: int = 20,
    ) -> List[Dict]:
        """Lista conversaciones de un usuario"""
        session = self.Session()
        try:
            q = session.query(Conversation).filter(Conversation.user_id == user_id)
            if not include_archived:
                q = q.filter(Conversation.is_archived.is_(False))
            conversations = (
                q.order_by(Conversation.updated_at.desc())
                .limit(max(1, int(limit)))
                .all()
            )
            # Nota: len(c.messages) potencialmente hace N+1; aceptable a estos tamaños
            return [
                {
                    "id": c.id,
                    "title": c.title,
                    "created_at": c.created_at.isoformat(),
                    "updated_at": c.updated_at.isoformat(),
                    "message_count": len(c.messages),
                    "has_summary": c.summary is not None,
                }
                for c in conversations
            ]
        finally:
            session.close()

    # ==================== GESTIÓN DE MENSAJES ====================

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        sources_used: Optional[List[Dict]] = None,
        metadata: Optional[Dict] = None,
        tokens_used: Optional[int] = None,
        processing_time_ms: Optional[int] = None,
    ) -> int:
        """
        Añade mensaje a una conversación y actualiza metadatos básicos.
        Returns: message_id
        """
        session = self.Session()
        try:
            if role not in {"user", "assistant", "system"}:
                raise ValueError(f"role inválido: {role}")

            convo = session.get(Conversation, conversation_id)
            if not convo:
                raise ValueError(f"Conversación {conversation_id} no encontrada")

            message = Message(
                conversation_id=conversation_id,
                role=role,
                content=content or "",
                sources_used=sources_used or [],
                retrieval_count=len(sources_used) if sources_used else 0,
                tokens_used=int(tokens_used or 0),
                processing_time_ms=int(processing_time_ms) if processing_time_ms is not None else None,
                meta=metadata or {},
            )
            session.add(message)

            # Actualizar timestamp de conversación
            convo.updated_at = datetime.utcnow()

            # Auto-generar título si es el primer mensaje del usuario
            if role == "user" and (not convo.title or convo.title == "Nueva conversación"):
                convo.title = self._generate_title(content)

            session.commit()
            logger.debug(f"Mensaje añadido a conversación {conversation_id}")
            return message.id
        except Exception as e:
            session.rollback()
            logger.error(f"Error add_message: {e}")
            raise
        finally:
            session.close()

    def get_conversation_context(self, conversation_id: int, max_tokens: Optional[int] = None) -> ConversationContext:
        """
        Obtiene contexto de conversación para el LLM.

        Lógica:
        - Si hay <= max_messages_before_summary: devuelve todos.
        - Si hay >, genera (o reutiliza) resumen para los antiguos y devuelve resumen + últimos N.
        """
        session = self.Session()
        try:
            convo = session.get(Conversation, conversation_id)
            if not convo:
                raise ValueError(f"Conversación {conversation_id} no encontrada")

            messages: List[Message] = (
                session.query(Message)
                .filter(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.asc())
                .all()
            )
            total = len(messages)

            # Caso simple: pocos mensajes
            if total <= self.max_messages_before_summary:
                context_messages = [
                    {"role": m.role, "content": m.content, "sources": m.sources_used} for m in messages
                ]
                return ConversationContext(
                    conversation_id=conversation_id,
                    user_id=convo.user_id,
                    messages=context_messages,
                    summary=None,
                    total_messages=total,
                )

            # Caso con resumen
            should_regenerate = (
                convo.summary is None
                or (
                    convo.summary_generated_at
                    and (datetime.utcnow() - convo.summary_generated_at) > timedelta(seconds=self.summary_ttl_seconds)
                )
            )

            if should_regenerate:
                # Resumimos todos salvo la ventana más reciente
                to_summarize = messages[:-self.context_window_size] if self.context_window_size < total else messages
                summary = self._generate_summary(to_summarize)
                convo.summary = summary
                convo.summary_generated_at = datetime.utcnow()
                session.commit()
                logger.info(f"Resumen generado para conversación {conversation_id}")

            recent_msgs = messages[-self.context_window_size:] if self.context_window_size < total else messages
            context_messages = [
                {"role": m.role, "content": m.content, "sources": m.sources_used} for m in recent_msgs
            ]

            # Truncado blando por tokens si se pide
            if max_tokens:
                context_messages = self._truncate_by_tokens(context_messages, max_tokens)

            return ConversationContext(
                conversation_id=conversation_id,
                user_id=convo.user_id,
                messages=context_messages,
                summary=convo.summary,
                total_messages=total,
            )
        except Exception as e:
            logger.error(f"Error get_conversation_context: {e}")
            raise
        finally:
            session.close()

    # ==================== RESÚMENES AUTOMÁTICOS ====================

    def _generate_summary(self, messages: List[Message]) -> str:
        """
        Genera resumen de mensajes antiguos usando LLM.

        Orden de preferencia:
        1. summarizer_llm explícito (LangChain o callable)
        2. LiteLLM via HTTP (vars de entorno LITELLM_URL / LITELLM_API_KEY)
        3. Fallback: contador de mensajes (sin LLM disponible)
        """
        if not messages:
            return "Sin mensajes previos."

        # Construir texto de la conversación (truncado por seguridad)
        joined = "\n\n".join(
            f"{m.role.upper()}: {(m.content or '')[:800]}"
            for m in messages
        )
        prompt = (
            "Resume la siguiente conversación en 2-3 párrafos concisos. "
            "Destaca: temas principales, preguntas clave del usuario, documentos citados y conclusiones.\n\n"
            f"Conversación:\n{joined}\n\nResumen:"
        )

        # Opción 1: cliente LLM explícito
        if self.summarizer_llm:
            try:
                if hasattr(self.summarizer_llm, "invoke"):
                    raw = self.summarizer_llm.invoke(prompt)
                elif callable(self.summarizer_llm):
                    raw = self.summarizer_llm(prompt)
                else:
                    raw = None
                if raw:
                    summary = self._llm_text(raw)
                    return summary.strip() or f"Conversación con {len(messages)} mensajes previos."
            except Exception as e:
                logger.warning(f"summarizer_llm falló: {e}. Intentando LiteLLM HTTP.")

        # Opción 2: LiteLLM via HTTP
        try:
            import requests as _req
            response = _req.post(
                f"{self._litellm_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._litellm_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 400,
                    "temperature": 0.3,
                },
                timeout=30,
            )
            response.raise_for_status()
            summary = response.json()["choices"][0]["message"]["content"]
            return summary.strip() if summary else f"Conversación con {len(messages)} mensajes previos."
        except Exception as e:
            logger.warning(f"LiteLLM HTTP no disponible para resumen: {e}")
            return f"Conversación con {len(messages)} mensajes previos."

    # ==================== UTILIDADES ====================

    def _generate_title(self, first_message: str) -> str:
        """Genera título simple desde el primer mensaje del usuario."""
        s = (first_message or "").strip()
        if len(s) <= 50:
            return s if s else "Nueva conversación"
        return s[:50].rstrip() + "…"

    def _truncate_by_tokens(self, messages: List[Dict], max_tokens: int) -> List[Dict]:
        """
        Truncado muy aproximado (1 token ≈ 4 chars) desde el principio,
        conservando los últimos mensajes.
        """
        if max_tokens <= 0 or not messages:
            return messages
        budget_chars = max_tokens * 4
        # contamos de atrás adelante para preservar los más recientes
        out: List[Dict] = []
        used = 0
        for msg in reversed(messages):
            chunk = msg.get("content", "") or ""
            size = len(chunk)
            if used + size <= budget_chars:
                out.append(msg)
                used += size
            else:
                # incluir parte final si queda presupuesto
                remaining = budget_chars - used
                if remaining > 0:
                    clipped = {**msg, "content": chunk[-remaining:]}
                    out.append(clipped)
                break
        return list(reversed(out))

    @staticmethod
    def _llm_text(raw) -> str:
        """Normaliza respuesta del LLM a string."""
        # Compat con LangChain ChatOpenAI, llamadores directos, etc.
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        # LangChain LLMResult-like
        try:
            content = getattr(raw, "content", None)
            if isinstance(content, str):
                return content
        except Exception:
            pass
        # OpenAI-style dicts
        if isinstance(raw, dict):
            # e.g., {"choices":[{"message":{"content":"..."}}]}
            try:
                return (
                    raw.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
            except Exception:
                return str(raw)
        return str(raw)

    # ==================== LIMPIEZA Y MANTENIMIENTO ====================

    def archive_conversation(self, conversation_id: int) -> None:
        """Archiva conversación (no la elimina)"""
        session = self.Session()
        try:
            convo = session.get(Conversation, conversation_id)
            if convo and not convo.is_archived:
                convo.is_archived = True
                session.commit()
                logger.info(f"Conversación {conversation_id} archivada")
        finally:
            session.close()

    def delete_old_conversations(self, days: int = 90) -> int:
        """
        Elimina conversaciones antiguas archivadas.
        Returns: número de conversaciones eliminadas
        """
        session = self.Session()
        try:
            cutoff = datetime.utcnow() - timedelta(days=int(days))
            deleted = (
                session.query(Conversation)
                .filter(Conversation.is_archived.is_(True))
                .filter(Conversation.updated_at < cutoff)
                .delete(synchronize_session=False)
            )
            session.commit()
            logger.info(f"Eliminadas {deleted} conversaciones antiguas")
            return int(deleted or 0)
        finally:
            session.close()

    # ==================== ESTADÍSTICAS ====================

    def get_user_stats(self, user_id: int) -> Dict:
        """Estadísticas del usuario"""
        session = self.Session()
        try:
            user = session.get(User, user_id)
            if not user:
                return {}
            total_conversations = session.query(Conversation).filter(Conversation.user_id == user_id).count()
            total_messages = (
                session.query(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .filter(Conversation.user_id == user_id)
                .count()
            )
            return {
                "user_id": user_id,
                "email": user.email,
                "name": user.name,
                "total_conversations": total_conversations,
                "total_messages": total_messages,
                "member_since": user.created_at.isoformat(),
                "last_active": user.last_active.isoformat(),
            }
        finally:
            session.close()

    # ==================== REGISTRO DE DOCUMENTOS ====================

    def upsert_document_record(
        self,
        *,
        filename: str,
        source_path: str,
        source_type: str,
        file_hash: str,
        file_size: Optional[int] = None,
        mime_type: Optional[str] = None,
        page_count: Optional[int] = None,
        chunk_count: Optional[int] = None,
        from_ocr: bool = False,
        indexed_by: Optional[int] = None,
        metadata: Optional[Dict] = None,
        status: str = "indexed",
    ) -> Dict[str, Any]:
        """Inserta o actualiza el registro SQL de un documento indexado."""
        session = self.Session()
        try:
            metadata = dict(metadata or {})
            collection_name = str(metadata.get("collection_name") or "").strip()
            active_collections = self._normalize_string_list(metadata.get("active_collections"))
            if collection_name and collection_name not in active_collections:
                active_collections.append(collection_name)
            if active_collections:
                metadata["active_collections"] = active_collections

            record = (
                session.query(IndexedDocument)
                .filter(IndexedDocument.file_hash == file_hash)
                .one_or_none()
            )

            if record is None:
                record = IndexedDocument(
                    filename=filename,
                    source_path=source_path,
                    source_type=source_type,
                    file_hash=file_hash,
                    file_size=file_size,
                    mime_type=mime_type,
                    page_count=page_count,
                    chunk_count=chunk_count,
                    from_ocr=from_ocr,
                    indexed_by=indexed_by,
                    meta=metadata,
                    status=status,
                )
                session.add(record)
            else:
                record.filename = filename or record.filename
                record.source_path = source_path or record.source_path
                record.source_type = source_type or record.source_type
                record.file_size = file_size if file_size is not None else record.file_size
                record.mime_type = mime_type or record.mime_type
                record.page_count = page_count if page_count is not None else record.page_count
                record.chunk_count = chunk_count if chunk_count is not None else record.chunk_count
                record.from_ocr = bool(from_ocr)
                record.indexed_by = indexed_by if indexed_by is not None else record.indexed_by
                record.status = status or record.status
                record.meta = self._merge_metadata(record.meta, metadata)

            session.commit()
            return {
                "id": record.id,
                "filename": record.filename,
                "status": record.status,
                "active_collections": self._normalize_string_list((record.meta or {}).get("active_collections")),
            }
        except Exception as e:
            session.rollback()
            logger.error(f"Error upsert_document_record: {e}")
            raise
        finally:
            session.close()

    def mark_document_deleted(
        self,
        filename: str,
        *,
        collection_name: Optional[str] = None,
        source_path: Optional[str] = None,
    ) -> int:
        """Marca un documento como eliminado y actualiza sus colecciones activas."""
        session = self.Session()
        try:
            records = (
                session.query(IndexedDocument)
                .filter(IndexedDocument.filename == filename)
                .all()
            )

            updated = 0
            for record in records:
                if source_path and record.source_path != source_path:
                    continue

                metadata = dict(record.meta or {})
                active_collections = self._normalize_string_list(metadata.get("active_collections"))
                deleted_collections = self._normalize_string_list(metadata.get("deleted_collections"))

                if collection_name:
                    if active_collections and collection_name not in active_collections:
                        continue
                    active_collections = [item for item in active_collections if item != collection_name]
                    if collection_name not in deleted_collections:
                        deleted_collections.append(collection_name)
                else:
                    active_collections = []

                metadata["active_collections"] = active_collections
                if deleted_collections:
                    metadata["deleted_collections"] = deleted_collections

                record.meta = metadata
                record.status = "deleted" if not active_collections else "indexed"
                updated += 1

            session.commit()
            return updated
        except Exception as e:
            session.rollback()
            logger.error(f"Error mark_document_deleted: {e}")
            raise
        finally:
            session.close()

    def get_document_registry(
        self,
        *,
        limit: int = 100,
        status: Optional[str] = None,
        source_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lista documentos registrados en SQL."""
        session = self.Session()
        try:
            query = session.query(IndexedDocument)
            if status:
                query = query.filter(IndexedDocument.status == status)
            if source_type:
                query = query.filter(IndexedDocument.source_type == source_type)
            rows = (
                query.order_by(IndexedDocument.indexed_at.desc())
                .limit(max(1, int(limit)))
                .all()
            )
            return [
                {
                    "id": row.id,
                    "filename": row.filename,
                    "source_path": row.source_path,
                    "source_type": row.source_type,
                    "file_hash": row.file_hash,
                    "file_size": row.file_size,
                    "mime_type": row.mime_type,
                    "page_count": row.page_count,
                    "chunk_count": row.chunk_count,
                    "from_ocr": row.from_ocr,
                    "indexed_at": row.indexed_at.isoformat() if row.indexed_at else None,
                    "status": row.status,
                    "metadata": row.meta or {},
                }
                for row in rows
            ]
        finally:
            session.close()

    # ==================== SHAREPOINT SYNC ====================

    def update_sharepoint_sync_state(
        self,
        *,
        site_id: str,
        folder_path: str,
        delta_token: Optional[str] = None,
        last_sync: Optional[Any] = None,
        subscription_id: Optional[str] = None,
        subscription_expires: Optional[Any] = None,
        is_active: bool = True,
    ) -> int:
        """Upsert del estado de sincronización SharePoint."""
        session = self.Session()
        try:
            row = (
                session.query(SharePointSyncState)
                .filter(
                    SharePointSyncState.site_id == site_id,
                    SharePointSyncState.folder_path == folder_path,
                )
                .one_or_none()
            )
            if row is None:
                row = SharePointSyncState(
                    site_id=site_id,
                    folder_path=folder_path,
                )
                session.add(row)

            if delta_token is not None:
                row.delta_token = delta_token
            row.last_sync = self._coerce_datetime(last_sync) or datetime.utcnow()
            if subscription_id is not None:
                row.subscription_id = subscription_id
            if subscription_expires is not None:
                row.subscription_expires = self._coerce_datetime(subscription_expires)
            row.is_active = bool(is_active)

            session.commit()
            return row.id
        except Exception as e:
            session.rollback()
            logger.error(f"Error update_sharepoint_sync_state: {e}")
            raise
        finally:
            session.close()

    def get_sharepoint_sync_states(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Lista estados SharePoint persistidos."""
        session = self.Session()
        try:
            rows = (
                session.query(SharePointSyncState)
                .order_by(SharePointSyncState.last_sync.desc())
                .limit(max(1, int(limit)))
                .all()
            )
            return [
                {
                    "id": row.id,
                    "site_id": row.site_id,
                    "folder_path": row.folder_path,
                    "delta_token_present": bool(row.delta_token),
                    "last_sync": row.last_sync.isoformat() if row.last_sync else None,
                    "subscription_id": row.subscription_id,
                    "subscription_expires": row.subscription_expires.isoformat() if row.subscription_expires else None,
                    "is_active": row.is_active,
                }
                for row in rows
            ]
        finally:
            session.close()

    # ==================== AUDITORÍA ====================

    def log_audit_event(
        self,
        *,
        action: str,
        resource_type: Optional[str] = None,
        resource_id: Optional[int] = None,
        user_id: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> int:
        """Añade un evento al audit log."""
        session = self.Session()
        try:
            row = AuditLog(
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details or {},
                ip_address=ip_address,
                user_agent=user_agent,
            )
            session.add(row)
            session.commit()
            return row.id
        except Exception as e:
            session.rollback()
            logger.error(f"Error log_audit_event: {e}")
            return 0
        finally:
            session.close()

    def get_audit_log(
        self,
        *,
        limit: int = 100,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lista eventos recientes del audit log."""
        session = self.Session()
        try:
            query = session.query(AuditLog)
            if action:
                query = query.filter(AuditLog.action == action)
            if resource_type:
                query = query.filter(AuditLog.resource_type == resource_type)
            rows = (
                query.order_by(AuditLog.created_at.desc())
                .limit(max(1, int(limit)))
                .all()
            )
            return [
                {
                    "id": row.id,
                    "user_id": row.user_id,
                    "action": row.action,
                    "resource_type": row.resource_type,
                    "resource_id": row.resource_id,
                    "details": row.details or {},
                    "ip_address": row.ip_address,
                    "user_agent": row.user_agent,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]
        finally:
            session.close()

    def get_operational_counts(self) -> Dict[str, int]:
        """Conteos ligeros para health checks y paneles operativos."""
        session = self.Session()
        try:
            return {
                "documents_total": session.query(IndexedDocument).count(),
                "documents_indexed": session.query(IndexedDocument).filter(IndexedDocument.status == "indexed").count(),
                "documents_failed": session.query(IndexedDocument).filter(IndexedDocument.status == "failed").count(),
                "documents_deleted": session.query(IndexedDocument).filter(IndexedDocument.status == "deleted").count(),
                "sharepoint_sync_rows": session.query(SharePointSyncState).count(),
                "audit_events": session.query(AuditLog).count(),
            }
        finally:
            session.close()

    # ==================== GESTIÓN DE ESTADO DE INGESTIÓN ====================

    def update_ingestion_status(self, filename: str, status: str, message: Optional[str] = None) -> None:
        """Actualiza el estado de ingestión de un archivo"""
        session = self.Session()
        try:
            # Upsert
            obj = session.get(IngestionStatus, filename)
            if not obj:
                obj = IngestionStatus(filename=filename, status=status, message=message)
                session.add(obj)
            else:
                obj.status = status
                if message is not None:
                    obj.message = message
                # updated_at se actualiza solo
            
            session.commit()
            logger.info(f"Estado actualizado para {filename}: {status}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error update_ingestion_status: {e}")
        finally:
            session.close()

    def get_ingestion_status(self, limit: int = 20) -> List[Dict]:
        """Obtiene los estados más recientes"""
        session = self.Session()
        try:
            items = (
                session.query(IngestionStatus)
                .order_by(IngestionStatus.updated_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "filename": i.filename,
                    "status": i.status,
                    "message": i.message,
                    "updated_at": i.updated_at.isoformat(),
                }
                for i in items
            ]
        finally:
            session.close()

    def delete_ingestion_status(self, filename: str) -> None:
        """Elimina el estado de un archivo (ej. cuando se borra)"""
        session = self.Session()
        try:
            obj = session.get(IngestionStatus, filename)
            if obj:
                session.delete(obj)
                session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Error delete_ingestion_status: {e}")
        finally:
            session.close()

    def cleanup_old_statuses(self, days: int = 30) -> int:
        """Elimina estados de ingestión antiguos (mantenimiento)"""
        session = self.Session()
        try:
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=days)
            
            deleted = (
                session.query(IngestionStatus)
                .filter(IngestionStatus.updated_at < cutoff)
                .filter(IngestionStatus.status.in_(["completed", "deleted", "failed"]))
                .delete(synchronize_session=False)
            )
            session.commit()
            logger.info(f"Limpiados {deleted} estados de ingestión antiguos")
            return int(deleted or 0)
        except Exception as e:
            session.rollback()
            logger.error(f"Error cleanup_old_statuses: {e}")
            return 0
        finally:
            session.close()


# ============================================
# EJEMPLO DE USO
# ============================================

if __name__ == "__main__":
    # Cambia la URL a tu entorno local si quieres probar rápidamente
    memory = MemoryManager(
        database_url="postgresql://rag_user:changeme@localhost:5432/rag_system",
        max_messages_before_summary=10,
        context_window_size=5,
    )

    # Crear usuario (desde Azure SSO)
    user_id = memory.get_or_create_user(
        azure_id="azure-user-123",
        email=None,  # demo: sin email → fallback seguro
        name="Juan Pérez",
    )

    # Crear conversación
    conv_id = memory.create_conversation(user_id, title="Consulta sobre contratos")

    # Añadir mensajes
    memory.add_message(conv_id, role="user", content="¿Cuál es el plazo del contrato X?")
    memory.add_message(
        conv_id,
        role="assistant",
        content="El contrato tiene un plazo de 12 meses según la cláusula 5. [contrato_X.pdf p.3]",
        sources_used=[{"filename": "contrato_X.pdf", "page": 3, "citation": "[contrato_X.pdf p.3]"}],
    )

    # Obtener contexto para el LLM
    context = memory.get_conversation_context(conv_id)
    print(f"Conversación {context.conversation_id} (mensajes totales: {context.total_messages})")
    if context.summary:
        print(f"Resumen: {context.summary[:120]}…")
    print("Mensajes recientes:")
    for msg in context.messages:
        print(f"- {msg['role']}: {msg['content'][:100]}…")
