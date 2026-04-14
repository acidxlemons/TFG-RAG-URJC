# 📚 Documentación del Proyecto JARVIS

> Sistema RAG Corporativo Inteligente — TFG Universidad Rey Juan Carlos 2026

## Índice de Documentos

### 🚀 Inicio Rápido
| Documento | Descripción |
|-----------|-------------|
| [DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md) | Guía paso a paso de despliegue con Docker Compose |
| [ENV_CONFIGURATION.md](ENV_CONFIGURATION.md) | Todas las variables de entorno del `.env` |
| [TECHNOLOGY_STACK.md](TECHNOLOGY_STACK.md) | Stack tecnológico completo del proyecto |

### 📘 Arquitectura y Código
| Documento | Descripción |
|-----------|-------------|
| [TECHNICAL_ARCHITECTURE.md](TECHNICAL_ARCHITECTURE.md) | Arquitectura de microservicios detallada |
| [CODEBASE_REFERENCE.md](CODEBASE_REFERENCE.md) | Mapa de archivos de código fuente |
| [STUDENT_GUIDE.md](STUDENT_GUIDE.md) | Guía para la defensa académica del TFG |
| [FINE_TUNING_GUIDE.md](FINE_TUNING_GUIDE.md) | Entrenamiento LoRA sobre Qwen 2.5 |

### 🔌 Integraciones
| Documento | Descripción |
|-----------|-------------|
| [BOE_INTEGRATION.md](BOE_INTEGRATION.md) | Servidor MCP para el Boletín Oficial del Estado |
| [MCP_EXPLICADO.md](MCP_EXPLICADO.md) | Qué es MCP y cómo se usa en el proyecto |
| [SHAREPOINT_INTEGRATION.md](SHAREPOINT_INTEGRATION.md) | Sincronización con SharePoint vía Graph API |
| [SHAREPOINT_RAG_GUIDE.md](SHAREPOINT_RAG_GUIDE.md) | Guía de RAG con documentos de SharePoint |
| [ADVANCED_EXTENSIONS.md](ADVANCED_EXTENSIONS.md) | SSO, NGINX avanzado, extensiones MCP |

### 👤 Usuario y Testing
| Documento | Descripción |
|-----------|-------------|
| [USER_GUIDE.md](USER_GUIDE.md) | Manual de usuario: todos los modos de JARVIS |
| [TESTING_GUIDE.md](TESTING_GUIDE.md) | Guía de testing y validación del sistema |
| [USER_FEEDBACK_FORM.md](USER_FEEDBACK_FORM.md) | Formulario de feedback de usuarios |

### 📋 Otros
| Documento | Descripción |
|-----------|-------------|
| [CHANGELOG.md](CHANGELOG.md) | Historial de cambios del proyecto (v2.1 incluido) |
| [DEPLOYMENT_GANTT.md](DEPLOYMENT_GANTT.md) | Diagrama GANTT del despliegue |

---

## 🏗️ Arquitectura Resumida

```
Usuario → NGINX (TLS) → OpenWebUI → Pipeline JARVIS (14 modos)
                                        ├── Backend RAG (FastAPI) → Qdrant / LiteLLM / Ollama
                                        ├── Indexer (SharePoint + OCR)
                                        └── MCP-BOE (API del BOE)
```

**Modelos de IA:**
- `rag-qwen-ft:latest` — LLM principal (fine-tuned con LoRA sobre Qwen 2.5)
- `qwen2.5:32b-instruct-q4_K_M` — Modelo alternativo de texto
- `qwen2.5vl:7b` — Modelo de visión para imágenes
- `paraphrase-multilingual-MiniLM-L12-v2` — Embeddings (384 dim)

**Stack:** Python 3.11 · FastAPI · Docker Compose · Qdrant · PostgreSQL · Redis · Ollama · LiteLLM · Prometheus · Grafana

## 🆕 Novedades v2.1

| Componente | Descripción |
|------------|-------------|
| `core/sql_agent.py` | Agente NL→SQL con whitelist, timeout y auto-corrección |
| `core/auth.py` | Validación JWT Azure AD (JWKS + PyJWT), activable con `AZURE_JWT_VALIDATION=true` |
| `api/query.py` | Endpoint unificado `POST /api/v1/query` con auto-routing RAG+SQL |
| `docker-compose.yml` | Límites de recursos (`deploy.resources.limits`) para 6 servicios |
| `services/litellm/config.yaml` | Secretos gestionados por variables de entorno (no hardcodeados) |
