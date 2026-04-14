# Codebase Reference

## Propósito

Este documento describe la estructura real del repositorio y aclara qué carpetas forman parte del sistema activo. Su objetivo es evitar ambigüedades entre código operativo, documentación y memoria académica.

## Estructura general del proyecto

```text
TFG-JARVIS/
├── backend/                 # Backend principal y lógica RAG
├── config/                  # Configuración de servicios
├── database/                # Recursos auxiliares de datos
├── docs/                    # Documentación técnica y académica complementaria
├── github_pages/            # Recursos para publicación estática
├── mcp-boe-server/          # Servidor MCP del BOE
├── memoria/                 # Memoria del TFG en LaTeX
├── scripts/                 # Scripts de mantenimiento y experimentación
├── services/                # Servicios auxiliares y componentes desplegables
├── docker-compose.yml       # Orquestación principal
└── README.md                # Entrada general del repositorio
```

## Carpetas principales

### `backend/`

Contiene el backend principal del sistema. Aquí reside la lógica de procesamiento de consultas, recuperación documental, integraciones y exposición de endpoints.

Responsabilidades típicas:

- flujo RAG;
- integración con Qdrant;
- llamadas a modelos a través de LiteLLM u Ollama;
- scraping y procesamiento documental;
- métricas y endpoints de servicio.

**Estructura de ficheros clave (`backend/app/`)**:

| Fichero | Descripción |
|---------|-------------|
| `api/search.py` | Búsqueda híbrida (dense + sparse + reranking) con expansión paralela y validación JWT |
| `api/chat.py` | Chat RAG con streaming y memoria conversacional |
| `api/query.py` | (**v2.1**) Endpoint unificado RAG + SQL con auto-routing |
| `api/documents.py` | Subida, indexación y gestión de documentos |
| `api/routers/boe.py` | Consulta al BOE con resolución semántica de nombres de leyes |
| `core/sql_agent.py` | (**v2.1**) Agente NL→SQL: genera y ejecuta SELECT seguros sobre tablas de negocio |
| `core/auth.py` | (**v2.1**) Validación JWT Azure AD para multi-tenant (activable con `AZURE_JWT_VALIDATION=true`) |
| `core/query_processor.py` | Expansión de queries con LiteLLM HTTP, detección de intención |
| `core/retrieval.py` | Pipeline híbrido con filtros avanzados (filename, date_range, from_ocr, source_type) |
| `core/memory/manager.py` | Historial conversacional en PostgreSQL con summarización vía LiteLLM |
| `core/rag/retriever.py` | Recuperación semántica con HNSW en Qdrant |
| `processing/embeddings/sentence_transformer.py` | Embeddings con singleton thread-safe y caché Redis |
| `integrations/boe_connector.py` | Cliente API del BOE con resolución de 35+ nombres coloquiales |
| `integrations/sharepoint/` | Sincronización incremental con Microsoft Graph (delta tokens) |

### `services/`

Agrupa servicios auxiliares que forman parte de la arquitectura desplegable. Según la configuración concreta, aquí pueden residir componentes como el pipeline de OpenWebUI, el indexador o servicios específicos de soporte.

### `mcp-boe-server/`

Contiene el servidor MCP para el dominio del BOE. Este componente es relevante porque aclara una parte del alcance del TFG:

- existe un servidor MCP desarrollado específicamente para normativa;
- está validado como componente independiente;
- su existencia no implica que toda la interfaz principal funcione ya mediante MCP.

### `scripts/`

Incluye scripts de apoyo para tareas de operación, pruebas o experimentación, por ejemplo:

- generación de datasets;
- fine-tuning;
- validaciones;
- utilidades de mantenimiento.

### `config/`

Configura servicios del despliegue:

- NGINX,
- Prometheus,
- Grafana,
- integraciones específicas.

### `memoria/`

Es la referencia académica principal del proyecto. Su estructura vigente está documentada en [`../memoria/README.md`](../memoria/README.md).

La carpeta contiene:

- `main.tex` como documento principal;
- capítulos activos en `memoria/capitulos/`;
- bibliografía en `bibliografia.bib`;
- archivos de front matter como resumen y portada.

### `docs/`

Documentación complementaria del proyecto:

- guía académica;
- mapa del código;
- arquitectura técnica;
- integración con BOE y SharePoint;
- guías de despliegue y pruebas.

## Memoria académica activa

La memoria compila con los siguientes capítulos:

1. `01_Introduccion.tex`
2. `02_EstadoDelArte.tex`
3. `03_MarcoTecnologico.tex`
4. `04_SistemasRelacionados.tex`
5. `05_DescripcionFuncional.tex`
6. `06_DisenoImplementacion.tex`
7. `07_Metodologia.tex`
8. `08_Resultados.tex`
9. `09_Conclusiones.tex`

Los capítulos antiguos o duplicados que no formaban parte de la estructura activa se han eliminado para evitar inconsistencias.

## Artefactos generados

Los ficheros auxiliares de LaTeX (`.aux`, `.toc`, `.out`, `.bbl`, etc.) no forman parte de la estructura canónica del proyecto y se han retirado de `memoria/` para evitar que queden índices o referencias obsoletas cuando no se recompila el documento.

## Regla de consistencia

Si hay discrepancias entre documentación operativa y memoria académica, debe prevalecer:

1. el código realmente activo del repositorio;
2. la estructura definida en `memoria/main.tex`;
3. el alcance explicitado en la memoria del TFG.
