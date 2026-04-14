<p align="center">
  <img src="docs/assets/urjc-logo.png" alt="JARVIS - URJC" width="200">
</p>

<h1 align="center">🤖 JARVIS - Intelligent RAG Assistant</h1>

<p align="center">
  <strong>Trabajo de Fin de Grado - Universidad Rey Juan Carlos</strong><br>
  Sistema de IA conversacional con RAG, búsqueda web, OCR y conexión al BOE.
</p>

<p align="center">
  <a href="#-quick-start"><img src="https://img.shields.io/badge/Quick%20Start-5%20min-brightgreen?style=for-the-badge" alt="Quick Start"></a>
  <a href="#-características"><img src="https://img.shields.io/badge/Features-20+-blue?style=for-the-badge" alt="Features"></a>
  <a href="https://acidxlemons.github.io/TFG-JARVIS/"><img src="https://img.shields.io/badge/Landing-Page-purple?style=for-the-badge" alt="Landing Page"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776ab?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Docker-Compose-2496ed?logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/GPU-NVIDIA%20CUDA-76b900?logo=nvidia&logoColor=white" alt="NVIDIA">
  <img src="https://img.shields.io/badge/LLM-Qwen%202.5%20%2B%20Ollama-black?logo=meta&logoColor=white" alt="LLM">
  <img src="https://img.shields.io/badge/Vectors-Qdrant-dc382d" alt="Qdrant">
</p>

---

## 📖 Tabla de Contenidos

- [🎯 ¿Qué es esto?](#-qué-es-esto)
- [✨ Características](#-características)
- [🏗️ Arquitectura](#️-arquitectura)
- [🚀 Quick Start](#-quick-start)
- [⚙️ Configuración](#️-configuración)
- [📊 Monitoreo](#-monitoreo)
- [🎓 Fine-Tuning](#-fine-tuning)
- [🔐 Seguridad](#-seguridad)
- [📚 Documentación](#-documentación)
- [📄 Licencia](#-licencia)

---

## 🎯 ¿Qué es esto?

**JARVIS** es un asistente de IA inteligente desarrollado como TFG en la URJC que permite:
- Consultar documentos corporativos mediante lenguaje natural (RAG)
- Buscar información en internet en tiempo real
- Analizar páginas web y PDFs (incluidos escaneados con OCR)
- Consultar el **Boletín Oficial del Estado (BOE)** con resolución semántica
- Analizar imágenes con modelos de visión (Qwen 2.5 VL)

### El Problema que Resuelve

| ❌ Antes | ✅ Después |
|----------|-----------|
| "¿Dónde está ese documento?" | "¿Qué dice la normativa sobre X?" |
| Buscar en múltiples fuentes | Respuesta unificada con **fuentes citadas** |
| Consultar BOE manualmente | "Busca en el BOE la ley de protección de datos" → Respuesta inmediata |

### ¿Por qué Local y No ChatGPT/Copilot?

```
┌─────────────────────────────────────────────────────────────────┐
│  🔒 SOBERANÍA DE DATOS                                          │
│                                                                   │
│  • Documentos NUNCA salen de tu infraestructura                 │
│  • Cumplimiento GDPR/RGPD por diseño                            │
│  • Sin dependencia de APIs externas ni costes por token         │
│  • Control total sobre el modelo y sus respuestas               │
└─────────────────────────────────────────────────────────────────┘
```

---

## ✨ Características

### 🧠 Core RAG
- **🔍 Búsqueda Híbrida** — Semántica (embeddings MiniLM-L12-v2) + Léxica (BM25)
- **📚 Citaciones Automáticas** — Referencias con archivo y página de origen
- **🎯 Detección de Intención** — 14 modos de operación con encaminamiento inteligente
- **🌐 Multi-idioma** — Detecta y responde en español e inglés
- **🗄️ SQL Agent (v2.1)** — NL→SQL con validación por lista blanca y auto-corrección
- **🔐 JWT Auth (v2.1)** — Validación criptográfica de tokens Azure AD con JWKS

### 🏛️ Conectores Externos
- **📰 BOE API** — Resolución semántica de leyes + consulta de normativa
- **🔗 Web Scraping** — Renderizado JS con Playwright + extracción con Trafilatura
- **🌐 Web Search** — Búsqueda en internet vía DuckDuckGo con fallback HTML
- **☁️ SharePoint** — Sincronización delta incremental vía Microsoft Graph API

### 📄 Procesamiento de Documentos
- **📑 OCR Inteligente** — PaddleOCR con aceleración GPU para PDFs escaneados
- **📊 Multi-formato** — PDF, DOCX, TXT, imágenes
- **✂️ Chunking Semántico** — Fragmentación con solapamiento del 10-15%

### 📊 Observabilidad
- **📈 Prometheus + Grafana** — 3 dashboards: Backend, GPU y Base de Datos
- **🔍 GPU Monitoring** — NVIDIA DCGM Exporter para VRAM, temperatura, utilización

> **Nota:** El sistema soporta SSO con Azure AD y sincronización con SharePoint, pero estas funcionalidades requieren credenciales corporativas adicionales.

---

## 🏗️ Arquitectura

```
┌──────────────────────────────────────────────────────────────────────┐
│                              USUARIO                                   │
│                         (Browser / API)                                │
└─────────────────────────────┬────────────────────────────────────────┘
                              │ HTTPS (8443)
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         🔒 NGINX (SSL)                                 │
│                    Reverse Proxy + TLS + WebSocket                      │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
┌─────────────────────────┐     ┌─────────────────────────────┐
│      🖥️ OpenWebUI        │     │    🧠 Pipeline JARVIS        │
│    (Chat Interface)      │────▶│  Agente Encaminador (9099)   │
│   Puerto 3002 (ext)      │     │  14 modos de operación       │
│   + SSO Azure AD         │     │  Detección de intenciones    │
└─────────────────────────┘     └──────────────┬──────────────┘
                                               │
              ┌────────────────────────────────┼────────────────┐
              ▼                                ▼                ▼
┌──────────────────────┐   ┌──────────────────────┐  ┌──────────────┐
│   📡 RAG Backend      │   │  📤 Indexer (8003)    │  │ 🏛️ MCP-BOE   │
│   FastAPI (8002)      │   │  Sync SharePoint      │  │  (8011)      │
│  • Búsqueda Híbrida   │   │  • Graph API + MSAL   │  │  FastMCP     │
│  • Scraping + Web     │   │  • OCR (PaddleOCR)    │  │  API BOE     │
│  • Métricas Prometheus│   │  • Cron cada 5 min     │  └──────────────┘
└──────────┬───────────┘   └──────────┬───────────┘
           │                          │
  ┌────────┼────────┬────────────┬────┘
  ▼        ▼        ▼            ▼
┌────────┐ ┌──────┐ ┌──────────┐ ┌────────┐
│ 🧮 Qdrant│ │🐘 PG │ │🤖 LiteLLM│ │💾 Redis│
│(Vectors)│ │(Meta)│ │  Proxy   │ │(Cache) │
│  6335   │ │ 5433 │ │  4001    │ │ 6380   │
└────────┘ └──────┘ └────┬─────┘ └────────┘
                         │
                    ┌────┴────┐
                    ▼         ▼
          ┌──────────┐ ┌──────────────────────┐
          │Ollama    │ │ Modelos de IA:        │
          │(11435)   │ │ • JARVIS (rag-qwen-ft)│
          │GPU NVIDIA│ │ • Qwen 2.5 32B Q4     │
          └──────────┘ │ • Qwen 2.5 VL 7B      │
                       │ • MiniLM-L12-v2 (emb) │
                       └──────────────────────┘
```

### Servicios del Ecosistema

| Servicio | Puerto Externo | Función |
|----------|---------------|---------|
| `nginx` | 8443, 8080 | Proxy inverso + TLS + WebSocket |
| `openwebui` | 3002 | Interfaz de chat conversacional |
| `pipelines` (JARVIS) | 9100 | Agente encaminador inteligente |
| `backend` (FastAPI) | 8002 | Motor RAG, scraping, búsqueda web, métricas |
| `indexer` (FastAPI) | 8003 | Sincronización SharePoint, ingesta, OCR |
| `mcp-boe` | 8011 | Servidor MCP para consultas al BOE |
| `ollama` | 11435 | Servidor de modelos LLM locales (GPU) |
| `litellm` | 4001 | Proxy unificado de modelos + caché |
| `qdrant` | 6335 | Base de datos vectorial (HNSW) |
| `postgres` | 5433 | Base de datos relacional |
| `redis` | 6380 | Caché de respuestas (TTL 1h) |
| `minio` | 9002 | Almacenamiento de objetos (backup S3) |
| `prometheus` | 9091 | Recolección de métricas |
| `grafana` | 3003 | Dashboards de observabilidad |
| `dcgm-exporter` | 9401 | Métricas GPU NVIDIA |
| `pgadmin` | 5051 | Administración de PostgreSQL |

### 🤖 Modelos de IA Desplegados

| Modelo | Función | VRAM |
|--------|---------|------|
| `rag-qwen-ft:latest` (JARVIS) | LLM principal para consultas RAG | ~8 GB |
| `qwen2.5:32b-instruct-q4_K_M` | Modelo de texto alternativo | ~19 GB |
| `qwen2.5vl:7b` | Análisis de imágenes y OCR visual | ~6 GB |
| `paraphrase-multilingual-MiniLM-L12-v2` | Embeddings (384 dimensiones) | CPU |

### 🏛️ Servidor MCP BOE

El sistema incluye un **servidor MCP** (Model Context Protocol) para consultar el Boletín Oficial del Estado:

```bash
cd mcp-boe-server
python test_mcp_real.py
```

**Herramientas disponibles:**

| Herramienta | Descripción |
|-------------|-------------|
| `get_boe_summary` | Sumario del BOE de hoy |
| `search_legislation` | Buscar leyes por texto |
| `get_law_text` | Texto completo de una ley |
| `get_law_analysis` | Análisis jurídico |
| `resolve_law_name` | Resolver "LOPD" → BOE-A-2018-16673 |

📖 Ver [Documentación MCP](mcp-boe-server/README.md)

---

## 🚀 Quick Start

### Prerrequisitos

| Requisito | Mínimo | Recomendado |
|-----------|--------|-------------|
| **SO** | Ubuntu 22.04 LTS | Ubuntu 22.04+ |
| **RAM** | 32 GB | 64 GB |
| **CPU** | 4 cores | 8+ cores |
| **GPU** | NVIDIA 16 GB VRAM | NVIDIA 24+ GB VRAM |
| **CUDA** | 12.x | 12.x |
| **Disco** | 500 GB SSD | NVMe |
| **Docker** | v24+ con NVIDIA Container Toolkit | Latest |

### Instalación

```bash
# 1. Clonar repositorio
git clone https://github.com/acidxlemons/TFG-JARVIS.git
cd TFG-JARVIS

# 2. Configurar variables de entorno
cp .env.example .env
nano .env  # Editar con tus credenciales

# 3. Iniciar todos los servicios
docker compose up -d

# 4. Verificar que todo está corriendo
docker compose ps

# 5. Los modelos se descargan automáticamente (qwen2.5, qwen2.5vl)
```

### Verificar Instalación

```bash
# Health check del backend
curl http://localhost:8002/health

# Verificar Qdrant
curl http://localhost:6335/health

# Acceder a la interfaz
open https://localhost:8443   # macOS / a través de NGINX
start https://localhost:8443  # Windows
```

### Tu Primera Consulta

1. Abre `https://localhost:8443` (o `http://localhost:3002` directamente)
2. Selecciona el modelo **JARVIS**
3. Prueba estas consultas:
   - *"¿Qué documentos tienes?"* (lista de archivos indexados)
   - *"¿Cuál es la política de calidad?"* (consulta RAG)
   - *"Busca en el BOE la ley de protección de datos"* (consulta BOE)
   - *"Busca en internet noticias sobre IA"* (búsqueda web)

---

## ⚙️ Configuración

### Variables de Entorno Esenciales

```bash
# ========================================
# MÍNIMO REQUERIDO
# ========================================

# Contraseñas (CAMBIAR OBLIGATORIAMENTE)
POSTGRES_PASSWORD=CHANGE_THIS_PASSWORD
GRAFANA_PASSWORD=CHANGE_THIS_PASSWORD

# ========================================
# OPCIONAL - Azure AD / SharePoint
# (Solo si deseas SSO corporativo)
# ========================================

# AZURE_TENANT_ID=your-tenant-id
# AZURE_CLIENT_ID=your-client-id
# AZURE_CLIENT_SECRET=your-client-secret
```

### Configuración SharePoint (Opcional)

Si deseas sincronizar documentos desde SharePoint:

1. **Crear `config/sharepoint_sites.json`:**

```json
{
  "sites": [
    {
      "name": "MiSitio",
      "site_id": "tu-site-id-aqui",
      "folder_path": "Documents/RAG",
      "collection_name": "documents_MiSitio",
      "enabled": true
    }
  ]
}
```

2. **Obtener Site ID** desde [Microsoft Graph Explorer](https://developer.microsoft.com/graph/graph-explorer)

3. **Configurar permisos** en Azure Portal → App Registration → API Permissions:
   - `Sites.Read.All`
   - `Files.Read.All`

---

## 📊 Monitoreo

### Acceso a Dashboards

| Dashboard / DB | URL Local | Propósito |
|----------------|-----------|-----------|
| **OpenWebUI** | http://localhost:3002 | Interfaz de chat principal |
| **Grafana** | http://localhost:3003 | Dashboards de backend, GPU y BBDD |
| **Prometheus** | http://localhost:9091 | Métricas en bruto |
| **Qdrant UI** | http://localhost:6335/dashboard | Vectores y embeddings |
| **pgAdmin** | http://localhost:5051 | Administración PostgreSQL |
| **MinIO Console** | http://localhost:9003 | Almacenamiento de objetos |

### Métricas Clave

```promql
# Latencia p95 de búsquedas RAG
histogram_quantile(0.95, rate(rag_search_duration_seconds_bucket[5m]))

# Uso de GPU
DCGM_FI_DEV_GPU_UTIL

# Cache hit rate
rate(rag_cache_hits_total[5m]) / rate(rag_requests_total[5m])
```

---

## 🎓 Fine-Tuning

Este sistema soporta fine-tuning de modelos con LoRA:

| Componente | Técnica | Cuándo usarlo |
|------------|---------|---------------|
| **LLM (Qwen 2.5)** | LoRA | Adaptar terminología corporativa |
| **Embeddings** | Full FT | Vocabulario de dominio específico |

```bash
# 1. Generar dataset desde documentos indexados
python scripts/generate_dataset_from_qdrant.py --output data/dataset.json

# 2. Entrenar con LoRA
python scripts/finetune_lora.py --dataset data/dataset.json --epochs 3

# 3. Exportar a GGUF y registrar en Ollama
python scripts/export_gguf.py
```

📖 **[Ver Guía Completa de Fine-Tuning](docs/FINE_TUNING_GUIDE.md)**

---

## 🔐 Seguridad

### Modelo de Seguridad

```
┌─────────────────────────────────────────────────────────────────────┐
│                    FLUJO DE AUTORIZACIÓN                             │
├─────────────────────────────────────────────────────────────────────┤
│  1. Usuario → NGINX (TLS) → OpenWebUI                               │
│  2. SSO Azure AD → JWT con grupos del usuario                       │
│  3. Pipeline JARVIS mapea grupos → colecciones Qdrant               │
│  4. Búsqueda RAG filtra SOLO colecciones autorizadas               │
│  5. Servicios internos aislados en red Docker (rag-network)         │
└─────────────────────────────────────────────────────────────────────┘
```

### Checklist de Seguridad

- [ ] Cambiar TODAS las contraseñas en `.env`
- [ ] Configurar certificados SSL válidos
- [ ] Verificar que solo NGINX expone puertos al exterior (8443)
- [ ] Configurar Azure AD con MFA (si aplica)
- [ ] Revisar permisos de archivos (`chmod 600 .env`)

---

## 📚 Documentación

Toda la documentación está en la carpeta [`docs/`](docs/README.md).

### 🚀 Empezando
| Documento | Descripción |
|-----------|-------------|
| 📖 **[DEPLOYMENT_CHECKLIST.md](docs/DEPLOYMENT_CHECKLIST.md)** | Guía paso a paso de despliegue |
| ⚙️ **[ENV_CONFIGURATION.md](docs/ENV_CONFIGURATION.md)** | Configuración de variables de entorno |
| 🛠️ **[TECHNOLOGY_STACK.md](docs/TECHNOLOGY_STACK.md)** | Stack tecnológico completo |

### 📘 Guías Técnicas
| Documento | Descripción |
|-----------|-------------|
| 🏗️ **[TECHNICAL_ARCHITECTURE.md](docs/TECHNICAL_ARCHITECTURE.md)** | Arquitectura completa del sistema |
| 📜 **[CODEBASE_REFERENCE.md](docs/CODEBASE_REFERENCE.md)** | Mapa de archivos de código |
| 🎓 **[STUDENT_GUIDE.md](docs/STUDENT_GUIDE.md)** | Guía para defensa académica |
| 🧠 **[FINE_TUNING_GUIDE.md](docs/FINE_TUNING_GUIDE.md)** | Entrenamiento de modelos LoRA |

### 🔌 Integraciones
| Documento | Descripción |
|-----------|-------------|
| 🏛️ **[BOE_INTEGRATION.md](docs/BOE_INTEGRATION.md)** | Integración con el BOE |
| 🤖 **[mcp-boe-server/README.md](mcp-boe-server/README.md)** | Servidor MCP para el BOE |
| 🔗 **[ADVANCED_EXTENSIONS.md](docs/ADVANCED_EXTENSIONS.md)** | MCP, SSO, NGINX avanzado |
| ☁️ **[SHAREPOINT_INTEGRATION.md](docs/SHAREPOINT_INTEGRATION.md)** | Sincronización con SharePoint |

### 👤 Usuario Final
| Documento | Descripción |
|-----------|-------------|
| 📗 **[USER_GUIDE.md](docs/USER_GUIDE.md)** | Manual de usuario completo |
| 🧪 **[TESTING_GUIDE.md](docs/TESTING_GUIDE.md)** | Guía de testing y validación |

---

## 🛠️ Troubleshooting

<details>
<summary><strong>❌ "No encuentra documentos"</strong></summary>

```bash
# 1. Verificar que hay documentos indexados
curl http://localhost:6335/collections/documents

# 2. Verificar tenant_id correcto
# El tenant del usuario debe coincidir con el de los documentos

# 3. Indexar un documento de prueba
curl -X POST http://localhost:8002/api/upload -F "file=@test.pdf"
```
</details>

<details>
<summary><strong>❌ "Latencia muy alta (>5s)"</strong></summary>

```bash
# 1. Verificar GPU
nvidia-smi

# 2. Verificar si la caché Redis funciona
docker exec tfg-redis redis-cli DBSIZE

# 3. Verificar recursos
docker stats
```
</details>

<details>
<summary><strong>❌ "Error de conexión al backend"</strong></summary>

```bash
# 1. Verificar que backend está corriendo
docker compose ps tfg-backend

# 2. Ver logs
docker compose logs tfg-backend --tail=50

# 3. Reiniciar servicios
docker compose restart tfg-backend
```
</details>

---

## 📄 Licencia

Este proyecto está bajo la licencia **MIT**. Ver [LICENSE](LICENSE) para más detalles.

---

## 🌟 Créditos y Tecnologías

| Tecnología | Uso |
|------------|-----|
| [Qwen 2.5](https://qwenlm.github.io/) | Modelos de lenguaje (texto + visión) |
| [Qdrant](https://qdrant.tech/) | Base de datos vectorial |
| [Ollama](https://ollama.ai/) | Servidor de LLMs locales |
| [OpenWebUI](https://github.com/open-webui/open-webui) | Interfaz de chat |
| [LiteLLM](https://github.com/BerriAI/litellm) | Proxy de LLMs + caché |
| [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) | OCR open source |
| [Playwright](https://playwright.dev/) | Web scraping con JS rendering |
| [BOE API](https://boe.es/datosabiertos/) | Datos abiertos del BOE |

---

<p align="center">
  <strong>JARVIS - Trabajo de Fin de Grado</strong><br>
  <em>Universidad Rey Juan Carlos - 2026</em>
</p>

<p align="center">
  <a href="#-jarvis---intelligent-rag-assistant">⬆️ Volver arriba</a>
</p>
