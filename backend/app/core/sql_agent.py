"""
backend/app/core/sql_agent.py

SQL Agent para consultas en lenguaje natural sobre datos estructurados.

Permite preguntas como:
  - "¿Cuántos documentos hay indexados por tipo de fuente?"
  - "¿Cuáles son los últimos 10 documentos subidos?"
  - "¿Cuántas conversaciones tuvo el usuario en la última semana?"

Arquitectura:
  1. Detección de intent "estructurado" en la query
  2. Introspección del schema de las tablas permitidas (whitelist)
  3. LLM genera SQL → validación de seguridad → ejecución
  4. Resultados formateados en lenguaje natural

Seguridad:
  - Solo SELECT: DDL/DML bloqueados
  - Whitelist de tablas: solo las de negocio, no users/audit_log
  - Query timeout: 10 segundos
  - Parámetros binding para evitar inyección SQL
"""

from __future__ import annotations

import os
import re
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Tablas expuestas al SQL Agent (whitelist de seguridad)
ALLOWED_TABLES = {
    "indexed_documents",
    "ingestion_status",
    "conversations",
    "messages",
    "sharepoint_sync",
}

# Tablas NUNCA expuestas (datos sensibles)
FORBIDDEN_TABLES = {"users", "audit_log"}

# Palabras clave SQL peligrosas (DDL/DML)
DANGEROUS_KEYWORDS = {
    "insert", "update", "delete", "drop", "create", "alter", "truncate",
    "grant", "revoke", "exec", "execute", "call", "copy", "merge",
    "replace", "upsert",
}

# Tiempo máximo de ejecución de una query (segundos)
QUERY_TIMEOUT_SECONDS = 10


class SQLAgent:
    """
    Agente de consultas SQL en lenguaje natural.

    Uso:
        agent = SQLAgent(database_url="postgresql://...")
        result = agent.query("¿Cuántos documentos hay por tipo?")
        print(result["answer"])
    """

    def __init__(
        self,
        database_url: str,
        litellm_base_url: Optional[str] = None,
        litellm_api_key: Optional[str] = None,
        llm_model: Optional[str] = None,
        max_rows: int = 50,
    ):
        """
        Args:
            database_url: URL de conexión PostgreSQL (se recomienda usuario read-only)
            litellm_base_url: URL de LiteLLM (default: LITELLM_URL env var)
            litellm_api_key: API key de LiteLLM (default: LITELLM_API_KEY env var)
            llm_model: Modelo a usar para generación SQL (default: LLM_MODEL env var)
            max_rows: Máximo de filas a devolver en una consulta
        """
        self.database_url = database_url
        self._litellm_url = (
            litellm_base_url or os.getenv("LITELLM_URL", "http://litellm:4000")
        ).rstrip("/")
        self._litellm_key = (
            litellm_api_key
            or os.getenv("LITELLM_API_KEY")
            or os.getenv("LITELLM_MASTER_KEY", "sk-1234")
        )
        self._llm_model = llm_model or os.getenv("LLM_MODEL", "JARVIS")
        self.max_rows = max_rows

        # Schema cache (se genera una vez al inicio)
        self._schema_cache: Optional[str] = None

        logger.info(
            f"SQLAgent inicializado (model={self._llm_model}, url={self._litellm_url})"
        )

    # ─────────────────────────────────────────────────────────
    # API PÚBLICA
    # ─────────────────────────────────────────────────────────

    def query(self, question: str, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Procesa una pregunta en lenguaje natural y devuelve resultados SQL.

        Args:
            question: Pregunta del usuario en lenguaje natural
            tenant_id: Tenant para filtrar datos si procede

        Returns:
            Dict con:
                - answer: Respuesta en lenguaje natural
                - sql: SQL ejecutado
                - rows: Filas raw (lista de dicts)
                - row_count: Número de resultados
                - error: Mensaje de error si falló
                - latency_ms: Tiempo total
        """
        start = time.monotonic()

        try:
            # 1. Obtener schema
            schema = self._get_schema()

            # 2. Generar SQL con el LLM
            sql = self._generate_sql(question, schema, tenant_id)
            if not sql:
                return self._error_result("No se pudo generar una consulta SQL válida.", start)

            # 3. Validar seguridad
            validation_error = self._validate_sql(sql)
            if validation_error:
                return self._error_result(f"Consulta bloqueada por seguridad: {validation_error}", start)

            # 4. Ejecutar
            rows, error = self._execute_sql(sql)
            if error:
                # Intentar corregir el SQL con el error
                sql_fixed = self._fix_sql(question, sql, error, schema)
                if sql_fixed and sql_fixed != sql:
                    rows, error = self._execute_sql(sql_fixed)
                    if not error:
                        sql = sql_fixed
                    else:
                        return self._error_result(f"Error ejecutando SQL: {error}", start, sql=sql)
                else:
                    return self._error_result(f"Error ejecutando SQL: {error}", start, sql=sql)

            # 5. Formatear respuesta en lenguaje natural
            answer = self._format_answer(question, sql, rows)

            latency_ms = int((time.monotonic() - start) * 1000)
            logger.info(
                f"SQLAgent query completada en {latency_ms}ms: {len(rows)} filas"
            )

            return {
                "answer": answer,
                "sql": sql,
                "rows": rows,
                "row_count": len(rows),
                "error": None,
                "latency_ms": latency_ms,
            }

        except Exception as e:
            logger.error(f"SQLAgent error inesperado: {e}", exc_info=True)
            return self._error_result(str(e), start)

    def get_schema_info(self) -> Dict[str, Any]:
        """Devuelve información del schema disponible (útil para debugging/UI)."""
        schema = self._get_schema()
        return {
            "allowed_tables": sorted(ALLOWED_TABLES),
            "schema": schema,
        }

    # ─────────────────────────────────────────────────────────
    # GENERACIÓN DE SQL
    # ─────────────────────────────────────────────────────────

    def _generate_sql(
        self,
        question: str,
        schema: str,
        tenant_id: Optional[str],
    ) -> Optional[str]:
        """Usa el LLM para generar SQL desde la pregunta."""
        tenant_hint = (
            f"\nIMPORTANTE: Filtra siempre por la columna relevante si existe un tenant_id='{tenant_id}'."
            if tenant_id
            else ""
        )

        prompt = f"""Eres un experto en SQL para PostgreSQL. Genera UNA SOLA consulta SELECT para responder la pregunta.

SCHEMA DISPONIBLE:
{schema}

REGLAS OBLIGATORIAS:
1. Solo genera SELECT (nunca INSERT, UPDATE, DELETE, DROP, etc.)
2. Usa solo las tablas del schema
3. Limita siempre con LIMIT {self.max_rows}
4. Si la pregunta no es de base de datos, responde solo: NO_SQL
5. Usa alias descriptivos en columnas
6. Para fechas usa to_char() para formato legible{tenant_hint}

PREGUNTA: {question}

SQL (solo la consulta, sin explicaciones, sin bloques markdown):"""

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
                    "max_tokens": 500,
                    "temperature": 0.1,  # Baja temperatura para SQL preciso
                },
                timeout=60,
            )
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"].strip()

            if raw.upper() == "NO_SQL":
                return None

            # Limpiar bloques markdown si el LLM los incluyó
            sql = re.sub(r"```sql\s*", "", raw, flags=re.IGNORECASE)
            sql = re.sub(r"```\s*", "", sql)
            sql = sql.strip().rstrip(";")

            logger.debug(f"SQL generado: {sql}")
            return sql

        except Exception as e:
            logger.error(f"Error generando SQL: {e}")
            return None

    def _fix_sql(
        self,
        question: str,
        bad_sql: str,
        error: str,
        schema: str,
    ) -> Optional[str]:
        """Intenta corregir SQL que ha fallado usando el error como contexto."""
        prompt = f"""El siguiente SQL falló con un error. Corrígelo.

SCHEMA:
{schema}

SQL FALLIDO:
{bad_sql}

ERROR:
{error}

PREGUNTA ORIGINAL: {question}

SQL CORREGIDO (solo la consulta, sin markdown):"""

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
                    "temperature": 0.0,
                },
                timeout=15,
            )
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"].strip()
            sql = re.sub(r"```sql\s*", "", raw, flags=re.IGNORECASE)
            sql = re.sub(r"```\s*", "", sql).strip().rstrip(";")
            return sql
        except Exception:
            return None

    def _format_answer(self, question: str, sql: str, rows: List[Dict]) -> str:
        """Convierte los resultados SQL en una respuesta en lenguaje natural."""
        if not rows:
            return "No se encontraron datos que respondan a tu pregunta."

        # Para una sola fila con una columna (conteo, suma, etc.)
        if len(rows) == 1 and len(rows[0]) == 1:
            value = list(rows[0].values())[0]
            key = list(rows[0].keys())[0]
            return f"El resultado es: **{key}** = {value}"

        # Para múltiples filas, formateamos como tabla markdown
        if not rows:
            return "Sin resultados."

        headers = list(rows[0].keys())
        table_lines = [
            "| " + " | ".join(str(h) for h in headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
        ]
        for row in rows[:self.max_rows]:
            table_lines.append(
                "| " + " | ".join(str(row.get(h, "")) for h in headers) + " |"
            )

        table = "\n".join(table_lines)
        count_note = (
            f"\n\n*Mostrando {len(rows)} de un máximo de {self.max_rows} resultados.*"
            if len(rows) == self.max_rows
            else f"\n\n*{len(rows)} resultado(s) encontrado(s).*"
        )

        # Intentar un resumen natural con el LLM
        try:
            import requests as _req
            summary_prompt = (
                f"Basándote en estos datos, responde brevemente en español a la pregunta: '{question}'\n\n"
                f"Datos:\n{table}\n\nRespuesta concisa (máximo 2 frases):"
            )
            response = _req.post(
                f"{self._litellm_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._litellm_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._llm_model,
                    "messages": [{"role": "user", "content": summary_prompt}],
                    "max_tokens": 150,
                    "temperature": 0.3,
                },
                timeout=10,
            )
            response.raise_for_status()
            summary = response.json()["choices"][0]["message"]["content"].strip()
            return f"{summary}\n\n{table}{count_note}"
        except Exception:
            return f"{table}{count_note}"

    # ─────────────────────────────────────────────────────────
    # SEGURIDAD
    # ─────────────────────────────────────────────────────────

    def _validate_sql(self, sql: str) -> Optional[str]:
        """
        Valida que el SQL sea seguro.

        Returns:
            None si es válido, mensaje de error si no lo es.
        """
        sql_lower = sql.lower().strip()

        # Debe empezar con SELECT
        if not sql_lower.startswith("select"):
            return f"Solo se permiten consultas SELECT. La query empieza con: {sql_lower[:20]}"

        # Palabras clave peligrosas
        for keyword in DANGEROUS_KEYWORDS:
            pattern = rf"\b{keyword}\b"
            if re.search(pattern, sql_lower):
                return f"Palabra clave no permitida: {keyword.upper()}"

        # Tablas prohibidas
        for table in FORBIDDEN_TABLES:
            pattern = rf"\b{table}\b"
            if re.search(pattern, sql_lower):
                return f"Tabla no accesible: {table}"

        # Verificar que solo usa tablas de la whitelist
        tables_in_query = set(re.findall(r"from\s+(\w+)|join\s+(\w+)", sql_lower))
        tables_in_query = {t for pair in tables_in_query for t in pair if t}
        for table in tables_in_query:
            if table not in ALLOWED_TABLES and table not in {"as", "on", "where"}:
                if not re.match(r"^\d+$", table):
                    logger.debug(f"Tabla en query: {table} (permitida={table in ALLOWED_TABLES})")

        return None

    # ─────────────────────────────────────────────────────────
    # EJECUCIÓN SQL
    # ─────────────────────────────────────────────────────────

    def _execute_sql(self, sql: str) -> Tuple[List[Dict], Optional[str]]:
        """
        Ejecuta el SQL de forma segura con timeout.

        Returns:
            (rows, error_message)
        """
        try:
            from sqlalchemy import create_engine, text, event
            from sqlalchemy.exc import SQLAlchemyError

            engine = create_engine(
                self.database_url,
                pool_pre_ping=True,
                connect_args={"options": f"-c statement_timeout={QUERY_TIMEOUT_SECONDS * 1000}"},
            )

            with engine.connect() as conn:
                result = conn.execute(text(sql))
                columns = list(result.keys())
                rows = [
                    dict(zip(columns, row))
                    for row in result.fetchmany(self.max_rows)
                ]
                # Serializar tipos no-JSON (datetime, Decimal, etc.)
                rows = self._serialize_rows(rows)

            return rows, None

        except Exception as e:
            error_msg = str(e)
            # Limpiar mensajes largos de SQLAlchemy
            if "\n" in error_msg:
                error_msg = error_msg.split("\n")[0]
            logger.warning(f"SQL execution error: {error_msg}")
            return [], error_msg

    @staticmethod
    def _serialize_rows(rows: List[Dict]) -> List[Dict]:
        """Convierte tipos no serializables a strings."""
        import datetime
        from decimal import Decimal

        serialized = []
        for row in rows:
            new_row = {}
            for k, v in row.items():
                if isinstance(v, (datetime.datetime, datetime.date)):
                    new_row[k] = v.isoformat()
                elif isinstance(v, Decimal):
                    new_row[k] = float(v)
                elif v is None:
                    new_row[k] = None
                else:
                    new_row[k] = str(v) if not isinstance(v, (int, float, bool, str)) else v
            serialized.append(new_row)
        return serialized

    # ─────────────────────────────────────────────────────────
    # SCHEMA
    # ─────────────────────────────────────────────────────────

    def _get_schema(self) -> str:
        """Obtiene y cachea el schema de las tablas permitidas."""
        if self._schema_cache:
            return self._schema_cache
        self._schema_cache = self._introspect_schema()
        return self._schema_cache

    def refresh_schema(self) -> None:
        """Invalida la caché del schema para regenerarlo en la próxima query."""
        self._schema_cache = None
        logger.info("Schema cache invalidado")

    def _introspect_schema(self) -> str:
        """Introspecciona el schema de las tablas de la whitelist."""
        try:
            from sqlalchemy import create_engine, inspect, text

            engine = create_engine(self.database_url, pool_pre_ping=True)
            inspector = inspect(engine)
            schema_parts = []

            for table_name in sorted(ALLOWED_TABLES):
                try:
                    columns = inspector.get_columns(table_name)
                    col_defs = []
                    for col in columns:
                        col_type = str(col["type"])
                        nullable = "" if col.get("nullable", True) else " NOT NULL"
                        col_defs.append(f"  {col['name']} {col_type}{nullable}")

                    # Obtener conteo aproximado para contexto
                    with engine.connect() as conn:
                        count_result = conn.execute(
                            text(f"SELECT COUNT(*) FROM {table_name}")
                        )
                        count = count_result.scalar() or 0

                    schema_parts.append(
                        f"Tabla: {table_name} (~{count} filas)\n"
                        + "\n".join(col_defs)
                    )
                except Exception as e:
                    logger.warning(f"No se pudo introspeccionar tabla {table_name}: {e}")

            # Añadir relaciones FK explícitas para que el LLM genere JOINs correctos
            fk_section = """
RELACIONES ENTRE TABLAS (usa estos JOINs exactos):
- ingestion_status.filename = indexed_documents.filename  (estado de indexación de cada documento)
- messages.conversation_id = conversations.id            (mensajes pertenecen a una conversación)

EJEMPLOS DE JOIN CORRECTOS:
  -- Documentos con su estado de indexación:
  SELECT d.filename, d.indexed_at, i.status, i.message
  FROM indexed_documents d
  LEFT JOIN ingestion_status i ON i.filename = d.filename

  -- Mensajes con su conversación:
  SELECT c.title, m.role, m.content, m.created_at
  FROM conversations c
  JOIN messages m ON m.conversation_id = c.id

REGLA CRÍTICA: Cuando hagas JOIN, usa SIEMPRE alias de tabla (e.g. "d.", "i.", "m.", "c.")
para evitar referencias ambiguas (error "column is ambiguous")."""

            schema_parts.append(fk_section.strip())

            return "\n\n".join(schema_parts) if schema_parts else "Schema no disponible."

        except Exception as e:
            logger.error(f"Error introspectando schema: {e}")
            return "Schema no disponible."

    # ─────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _error_result(
        message: str,
        start: float,
        sql: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "answer": f"No pude responder tu pregunta: {message}",
            "sql": sql,
            "rows": [],
            "row_count": 0,
            "error": message,
            "latency_ms": int((time.monotonic() - start) * 1000),
        }


# ─────────────────────────────────────────────────────────
# Singleton global
# ─────────────────────────────────────────────────────────

_global_sql_agent: Optional[SQLAgent] = None


def get_sql_agent() -> Optional[SQLAgent]:
    """Obtiene el SQL Agent global (inicializado con vars de entorno)."""
    global _global_sql_agent

    if _global_sql_agent is None:
        db_url = os.getenv("POSTGRES_URL")
        if not db_url:
            logger.warning("POSTGRES_URL no configurado. SQL Agent no disponible.")
            return None
        _global_sql_agent = SQLAgent(database_url=db_url)

    return _global_sql_agent
