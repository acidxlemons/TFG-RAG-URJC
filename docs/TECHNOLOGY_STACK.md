# 🛠️ Stack Tecnológico - JARVIS RAG System

**Proyecto**: TFG - Universidad Rey Juan Carlos  
**Propósito**: Explicación detallada de cada tecnología utilizada en el sistema.

---

## 📖 Índice

1. [Infraestructura Base](#1-infraestructura-base)
2. [Interfaz de Usuario](#2-interfaz-de-usuario)
3. [Backend de IA](#3-backend-de-ia)
4. [Almacenamiento](#4-almacenamiento)
5. [Observabilidad](#5-observabilidad)
6. [Seguridad](#6-seguridad)
7. [Integraciones Externas](#7-integraciones-externas)
   - [Web Search (DuckDuckGo)](#-web-search-duckduckgo)
   - [Web Scraping (aiohttp, BeautifulSoup, Playwright)](#-web-scraping)
   - [BOE API](#-boe-api)
   - [MCP (Model Context Protocol)](#-mcp-model-context-protocol)
   - [Microsoft Graph API](#-microsoft-graph-api)

---

## 1. Infraestructura Base

### 🐳 Docker & Docker Compose

**Qué es**: Plataforma de contenedores que permite empaquetar aplicaciones con todas sus dependencias.

**Por qué lo usamos**:
- **Reproducibilidad**: El mismo contenedor funciona igual en desarrollo y producción.
- **Aislamiento**: Cada servicio tiene su propio entorno, sin conflictos.
- **Orquestación**: Docker Compose gestiona los servicios principales del sistema y varios servicios auxiliares con un solo comando.

**Archivos clave**:
```
docker-compose.yml      # Definición de todos los servicios
.env                    # Variables de entorno (NO subir a Git)
.env.example            # Plantilla de variables
```

**Comandos esenciales**:
```bash
docker compose up -d              # Iniciar todo
docker compose down               # Parar todo
docker compose logs -f backend    # Ver logs de un servicio
docker compose restart openwebui  # Reiniciar servicio
```

---

### 🔒 NGINX (Reverse Proxy)

**Qué es**: Servidor web de alto rendimiento que actúa como "puerta de entrada" al sistema.

**Por qué lo usamos**:

| Función | Beneficio |
|---------|-----------|
| **SSL Termination** | Gestiona HTTPS, los servicios internos hablan HTTP simple |
| **Rate Limiting** | Protege contra ataques DDoS |
| **Security Headers** | HSTS, X-Frame-Options, CSP |
| **WebSocket Proxy** | Permite chat en tiempo real |
| **Load Balancing** | Distribuye carga (escalabilidad futura) |

**Diagrama de flujo**:
```
Internet → NGINX (8443) → OpenWebUI (8080)
                       → Backend (8002)
                       → Grafana (3002)
```

**Archivos clave**:
```
config/nginx/nginx.conf    # Configuración principal
config/nginx/ssl/          # Certificados SSL
```

---

## 2. Interfaz de Usuario

### 💬 OpenWebUI

**Qué es**: Interfaz de chat open-source similar a ChatGPT, pero self-hosted.

**Por qué lo usamos**:
- **UI moderna**: Experiencia de usuario familiar.
- **Pipelines**: Permite inyectar lógica Python antes/después de cada mensaje.
- **Multi-modelo**: Cambia entre modelos sin recargar.
- **SSO**: Integración con Azure AD, Google, GitHub.
- **Artefactos**: Genera código, tablas, diagramas.

**Nuestra personalización**:
- Pipeline `jarvis.py` que detecta intención y enruta a RAG, BOE, Web Search, etc.
- CSS corporativo para branding.
- Modelo por defecto configurado como "JARVIS".

**Puerto**: `3002` (externo) → `8080` (interno)

---

## 3. Backend de IA

### 🦙 Ollama

**Qué es**: Servidor de modelos LLM locales. Permite ejecutar modelos como Llama, Qwen, Mistral en tu propia GPU.

**Por qué local y no API**:
- ✅ **Soberanía de datos**: Tus documentos NUNCA salen de tu servidor.
- ✅ **Sin costes por token**: Paga electricidad, no API calls.
- ✅ **Sin límites de rate**: Usa el modelo todo lo que quieras.
- ✅ **Cumplimiento GDPR**: Datos en tu jurisdicción.

**Modelos instalados**:

| Modelo | Uso | VRAM |
|--------|-----|------|
| `llama3.1:8b-instruct-q8_0` | Chat general, RAG | ~8GB |
| `tfg-qwen-ft:latest` | Fine-tuned para este proyecto | ~6GB |
| `qwen2.5vl:7b` | Análisis de imágenes | ~8GB |

**Puerto**: `11435`

---

### 🔀 LiteLLM

**Qué es**: Proxy unificado para múltiples proveedores de LLM (Ollama, OpenAI, Anthropic, etc.).

**Por qué lo usamos**:
- **API unificada**: OpenWebUI habla con LiteLLM, LiteLLM enruta a Ollama.
- **Routing inteligente**: Fallbacks automáticos si un modelo falla.
- **Métricas**: Expone estadísticas de uso.
- **Aliases**: `gpt-4` se mapea automáticamente a `JARVIS`.

**Configuración**: `services/litellm/config.yaml`

**Puerto**: `4001`

---

### 🔍 RAG Backend (FastAPI)

**Qué es**: API REST que implementa toda la lógica de búsqueda y procesamiento de documentos.

**Capacidades**:

| Endpoint | Función |
|----------|---------|
| `/api/v1/search` | Búsqueda híbrida (semántica + léxica) |
| `/api/v1/chat` | Chat con contexto RAG |
| `/scrape/url` | Scrapea y indexa URLs |
| `/documents/list` | Lista documentos indexados |
| `/boe/*` | Endpoints del BOE |

**Tecnologías internas**:
- **Sentence Transformers**: Embeddings multilingües.
- **PaddleOCR**: OCR con GPU para PDFs escaneados.
- **LangChain**: Chunking semántico.

**Puerto**: `8002`

---

## 4. Almacenamiento

### 🧮 Qdrant (Vector Database)

**Qué es**: Base de datos especializada en búsqueda por similitud vectorial.

**Por qué no PostgreSQL para vectores**:
- ⚡ **10-100x más rápido** que pgvector para millones de documentos.
- 🎯 **HNSW nativo**: Algoritmo de búsqueda aproximada optimizado.
- 🔍 **Búsqueda híbrida**: Combina vectores + filtros de metadatos.

**Colecciones**:
```
documents       # Documentos corporativos (PDFs, DOCX)
webs            # Páginas web scrapeadas
documents_DEPT  # Colecciones por departamento (multi-tenant)
```

**Puerto**: `6335` (REST), `6336` (gRPC)

---

### 🐘 PostgreSQL

**Qué es**: Base de datos relacional para metadatos y configuración.

**Uso en el sistema**:
- Usuarios y sesiones de OpenWebUI.
- Historial de conversaciones.
- Configuración de pipelines.

**Puerto**: `5433`

---

### 💾 Redis

**Qué es**: Base de datos en memoria para cache de alta velocidad.

**Uso en el sistema**:
- **Cache de respuestas LLM**: Si la misma pregunta se repite, respuesta instantánea.
- **Cache de embeddings**: Evita recalcular vectores.
- **Rate limiting**: Contador de peticiones por usuario.

**TTL por defecto**: 1 hora

**Puerto**: `6380`

---

### 📦 MinIO

**Qué es**: Almacenamiento de objetos compatible con S3.

**Uso en el sistema**:
- Almacenar PDFs originales.
- Backup de documentos procesados.
- Artefactos de fine-tuning.

**Puerto**: `9002` (API), `9003` (Console)

---

## 5. Observabilidad

### 📊 Prometheus

**Qué es**: Sistema de monitoreo y alertas basado en métricas time-series.

**Métricas recolectadas**:
- Latencia de peticiones HTTP.
- Uso de CPU/RAM por contenedor.
- Métricas de GPU (NVIDIA).
- Contadores de queries RAG.

**Puerto**: `9091`

---

### 📈 Grafana

**Qué es**: Plataforma de visualización de métricas con dashboards.

**Dashboards incluidos**:
- **System Overview**: CPU, RAM, Disco.
- **GPU Monitoring**: Uso, temperatura, memoria.
- **RAG Performance**: Latencias, hits de cache.

**Acceso**: `http://localhost:3003` (user: `admin`)

**Puerto**: `3003`

---

### 🎮 DCGM Exporter

**Qué es**: Exportador de métricas de GPUs NVIDIA para Prometheus.

**Métricas expuestas**:
- `DCGM_FI_DEV_GPU_UTIL` - Utilización de GPU (%)
- `DCGM_FI_DEV_FB_USED` - Memoria usada (MB)
- `DCGM_FI_DEV_GPU_TEMP` - Temperatura (°C)

**Puerto**: `9401`

---

## 6. Seguridad

### 🔐 Autenticación SSO

El sistema soporta múltiples proveedores de identidad:

| Proveedor | Configuración | Ideal para |
|-----------|---------------|------------|
| **Azure AD** | OIDC | Empresas Microsoft 365 |
| **Google OAuth** | OAuth 2.0 | Startups |
| **GitHub** | OAuth 2.0 | Equipos técnicos |
| **Keycloak** | OIDC | Self-hosted, máximo control |

📖 Ver [SHAREPOINT_INTEGRATION.md](SHAREPOINT_INTEGRATION.md) para configuración detallada.

---

### 🛡️ Headers de Seguridad

NGINX inyecta automáticamente:
```
Strict-Transport-Security: max-age=31536000
X-Frame-Options: SAMEORIGIN
X-Content-Type-Options: nosniff
Content-Security-Policy: default-src 'self'
```

---

## 7. Integraciones Externas

### 🌐 Web Search (DuckDuckGo)

**Qué es**: Motor de búsqueda que respeta la privacidad. Usamos la librería `duckduckgo-search` en Python.

**Por qué DuckDuckGo**:
- ✅ **Zero-Cost**: No requiere API key ni pago.
- ✅ **Sin tracking**: No rastrea usuarios (GDPR-friendly).
- ✅ **Fallback robusto**: Si la librería falla (rate limiting), usamos HTML scraping.
- ✅ **Simple**: Una librería Python, sin servicios adicionales.

**Cómo funciona en JARVIS**:
```
Usuario: "Busca en internet las últimas noticias sobre IA"
    │
    ▼
Pipeline JARVIS detecta "busca en internet"
    │
    ▼
Backend → Intenta librería DDGS
    │
    ├── ✅ Éxito → Devuelve resultados
    │
    └── ❌ Rate limit → Fallback a HTML scraping
                        GET https://html.duckduckgo.com/html/
    │
    ▼
LLM resume los resultados y responde
```

**Librerías usadas**:
```python
# requirements.txt
duckduckgo-search>=6.0.0  # Librería principal
aiohttp                    # Para fallback HTTP
beautifulsoup4             # Para parsear HTML fallback
```

**Archivo clave**: `backend/app/api/web_search.py`

**Puerto**: Interno (llamada desde backend, sin servicio separado)

---

### 🕷️ Web Scraping

El sistema utiliza **dos motores de scraping** dependiendo del tipo de página web:

#### Motor 1: aiohttp + BeautifulSoup (Páginas Estáticas)

**Qué son**:
- **aiohttp**: Cliente HTTP asíncrono para Python.
- **BeautifulSoup4**: Parser HTML que extrae texto de páginas.

**Cuándo se usa**: Páginas web estáticas (HTML puro, sin JavaScript pesado).

**Ventajas**:
- ⚡ Muy rápido (~100ms por página).
- 💾 Bajo consumo de memoria.
- 🔧 Simple de mantener.

**Ejemplo de flujo**:
```python
async with aiohttp.ClientSession() as session:
    response = await session.get(url)
    html = await response.text()
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text()
```

---

#### Motor 2: Playwright (Páginas Dinámicas con JavaScript)

**Qué es**: Navegador headless que renderiza JavaScript como un navegador real.

**Cuándo se usa**: 
- Páginas React/Vue/Angular.
- Contenido cargado dinámicamente.
- SPAs (Single Page Applications).
- Páginas que requieren scroll para cargar.

**Ventajas**:
- ✅ Renderiza JavaScript completo.
- ✅ Puede esperar a elementos específicos.
- ✅ Soporta interacciones (click, scroll).

**Desventajas**:
- 🐢 Más lento (~2-5s por página).
- 💾 Mayor consumo de memoria.
- 🔧 Requiere Chromium instalado.

**Ejemplo de flujo**:
```python
async with async_playwright() as p:
    browser = await p.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.goto(url)
    await page.wait_for_load_state('networkidle')
    text = await page.inner_text('body')
```

---

#### Decisión Automática de Motor

El pipeline **JARVIS** decide automáticamente qué motor usar:

```
Usuario envía URL
    │
    ▼
¿Es conocida como JS-heavy? (SPA, React, etc.)
    │
    ├── SÍ → Playwright
    │
    └── NO → aiohttp + BeautifulSoup
              │
              ▼
         ¿Falló o contenido vacío?
              │
              ├── SÍ → Reintentar con Playwright
              │
              └── NO → Éxito, indexar contenido
```

**Archivos clave**:
```
backend/app/api/scrape.py              # Endpoint de scraping
backend/app/integrations/scraper/      # Módulos de scraping
  ├── aiohttp_scraper.py               # Motor HTTP simple
  └── playwright_scraper.py            # Motor con navegador
```

---

#### Alternativa Futura: Crawlee

Para proyectos de producción con miles de URLs, existe [Crawlee](https://crawlee.dev) de Apify:

| Característica | Stack Actual | Crawlee |
|----------------|--------------|---------|
| Retries automáticos | Manual | ✅ Built-in |
| Proxy rotation | ❌ | ✅ Built-in |
| Anti-blocking | ❌ | ✅ Built-in |
| Queue de URLs | Manual | ✅ Persistente |

> **Nota TFG**: El stack actual es suficiente para el alcance del proyecto. Crawlee sería una mejora para escenarios enterprise con alto volumen.

---

### 🏛️ BOE API

**Qué es**: Integración con la API Open Data del Boletín Oficial del Estado.

**Capacidades**:
- Sumario diario del BOE.
- Búsqueda de legislación.
- Texto consolidado de leyes.
- Análisis de referencias cruzadas.

📖 Ver [BOE_INTEGRATION.md](BOE_INTEGRATION.md)

---

### 🤖 MCP (Model Context Protocol)

**Qué es**: Protocolo estándar de Anthropic para conectar LLMs con herramientas externas.

**Implementación en JARVIS**:
- Servidor MCP en puerto `8011`.
- 7 herramientas BOE expuestas.
- Compatible con Claude Desktop, Cursor, scripts Python.

📖 Ver [ADVANCED_EXTENSIONS.md](ADVANCED_EXTENSIONS.md#3-model-context-protocol-mcp)

---

### ☁️ Microsoft Graph API

**Qué es**: API unificada de Microsoft para acceder a datos de M365.

**Uso en el sistema**:
- Sincronización de documentos desde SharePoint.
- Lectura de grupos de usuario para permisos.
- Delta sync incremental.

📖 Ver [SHAREPOINT_INTEGRATION.md](SHAREPOINT_INTEGRATION.md)

---

## 📊 Resumen de Puertos

| Puerto | Servicio | Acceso |
|--------|----------|--------|
| 8443/80 | NGINX | Público |
| 3002 | OpenWebUI | Interno (vía NGINX) |
| 3003 | Grafana | Interno |
| 4001 | LiteLLM | Interno |
| 6335 | Qdrant | Interno |
| 8002 | Backend | Interno |
| 8011 | MCP BOE | Interno |
| 9091 | Prometheus | Interno |
| 11435 | Ollama | Interno |

---

*Documento generado para TFG - Universidad Rey Juan Carlos*
