# 🧪 Guía Completa de Setup y Testing - JARVIS RAG System

**Versión**: 4.0  
**Proyecto**: TFG - Universidad Rey Juan Carlos  
**Última actualización**: 29 de Enero de 2026

---

## 📑 Índice

1. [Prerequisitos y Verificación Inicial](#prerequisitos)
2. [Verificación de Servicios](#verificacion-servicios)
3. [Verificación de Logs](#verificacion-logs)
4. [Verificación de Modelos Ollama](#modelos-ollama)
5. [Testing de Endpoints API](#testing-api)
6. [Configuración de OpenWebUI](#configuracion-openwebui)
7. [Testing en OpenWebUI](#testing-openwebui)
8. [Archivos Adjuntos en Chat](#archivos-adjuntos)
9. [Ingestión de Documentos](#ingestion-documentos)
10. [Búsquedas RAG](#busquedas-rag)
11. [Web Search y Scraping](#web-search)
12. [Monitoreo de Estado](#monitoreo-estado)
13. [Consultas de Base de Datos](#consultas-db)
14. [Health Checks](#health-checks)
15. [Monitoreo con Prometheus y Grafana](#monitoreo-prometheus)
16. [Documentación de API (OpenAPI)](#documentacion-api)
17. [Tests Automatizados](#tests-automatizados)
18. [Logs y Debugging](#logs-debugging)
19. [Troubleshooting](#troubleshooting)
20. [Checklist Completo](#checklist)

---

<a name="prerequisitos"></a>
## 1. ✅ Prerequisitos y Verificación Inicial

### 1.1 Verificar Build Completado

```powershell
cd c:\TFG-RAG-Clean

# Ver estado de todos los servicios
docker compose ps

# Debes ver todos los servicios "running" o "healthy"
```

### 1.2 Ver Logs del Build

```powershell
# Backend
docker compose logs tfg-backend --tail 5

# Pipelines  
docker compose logs pipelines --tail 5

# LiteLLM
docker compose logs litellm --tail 5
```

**Esperado**: Líneas finales indicando que está listo:
```
✓ Aplicación lista
Uvicorn running on http://0.0.0.0:8002
```

### 1.3 Si el Build Falló

```powershell
# Cancelar si está colgado
Ctrl+C

# Rebuild limpio
docker compose build tfg-backend pipelines litellm --no-cache

# Levantar
docker compose up -d tfg-backend pipelines litellm
```

---

<a name="verificacion-servicios"></a>
## 2. 📋 Verificación de Servicios

```powershell
docker compose ps
```

### Servicios Críticos

| Servicio | Puerto | Estado Esperado |
|----------|--------|-----------------|
| `tfg-backend` | 8002 | ✅ Up (healthy) |
| `pipelines` | 9100 | ✅ Up |
| `litellm` | 4001 | ✅ Up |
| `ollama` | 11435 | ✅ Up (healthy) |
| `qdrant` | 6335 | ✅ Up |
| `postgres` | 5433 | ✅ Up (healthy) |
| `redis` | 6380 | ✅ Up (healthy) |
| `openwebui` | 3002 | ✅ Up |
| `prometheus` | 9091 | ✅ Up |
| `grafana` | 3003 | ✅ Up |
| `pgadmin` | 5051 | ✅ Up |
| `nginx` | 80/8443 | ✅ Up |

### Si Algún Servicio No Está "Up"

```powershell
# Ver por qué falló
docker compose logs [nombre-servicio] --tail 50

# Reintentar
docker compose restart [nombre-servicio]
```

---

<a name="verificacion-logs"></a>
## 3. 📋 Verificación de Logs

### 3.1 Backend

```powershell
docker compose logs tfg-backend --tail 30
```

**Buscar líneas (✅ BUENO)**:
```
✓ Qdrant conectado: http://qdrant:6335
✓ RAG Retriever inicializado
✓ Memory Manager inicializado
✓ OCR Pipeline inicializado
✅ Aplicación lista
```

**Errores comunes (❌ MALO)**:
```
ModuleNotFoundError: No module named 'httpx'
  → SOLUCIÓN: docker compose build tfg-backend --no-cache

Connection refused to qdrant
  → SOLUCIÓN: docker compose restart qdrant
```

### 3.2 Pipelines

```powershell
docker compose logs pipelines --tail 30
```

**Buscar líneas (✅ BUENO)**:
```
✓ JARVIS inicializado
  Backend: http://tfg-backend:8002
  LiteLLM: http://litellm:4001
🚀 Starting JARVIS
```

### 3.3 LiteLLM

```powershell
docker compose logs litellm --tail 30
```

**Buscar líneas (✅ BUENO)**:
```
✅ Loaded model: llama3.1
✅ Loaded model: llava
```

---

<a name="modelos-ollama"></a>
## 4. 🤖 Verificación de Modelos Ollama

```powershell
docker compose exec ollama ollama list
```

**Esperado**:
```
NAME                            SIZE
qwen2.5:32b-instruct-q4_K_M    19GB
qwen2.5vl:7b                   6.0GB
rag-qwen-ft:latest             ~8GB
```

**Si falta algún modelo**:
```powershell
docker compose exec ollama ollama pull qwen2.5:32b-instruct-q4_K_M
docker compose exec ollama ollama pull qwen2.5vl:7b
```

**Nota**:
`rag-qwen-ft:latest` es un modelo ajustado del proyecto. Si no aparece en `ollama list`,
hay que crearlo explícitamente a partir del modelo base y del adaptador LoRA del proyecto.

---

<a name="testing-api"></a>
## 5. 🔌 Testing de Endpoints API

### 5.1 Health Check

```powershell
Invoke-RestMethod -Uri http://localhost:8002/health
```

**Esperado**:
```json
{
  "status": "healthy",
  "components": {
    "qdrant": "connected",
    "postgres": "connected",
    "ocr": "ready"
  }
}
```

### 5.2 Listar Documentos

```powershell
Invoke-RestMethod -Uri http://localhost:8002/documents/list
```

**Esperado** (sin documentos):
```json
{"total": 0, "documents": []}
```

**Esperado** (con documentos):
```json
{
  "total": 3,
  "documents": [
    {"filename": "Manual_ISO.pdf"},
    {"filename": "Politica_Calidad.pdf"}
  ]
}
```

### 5.3 Web Search

```powershell
Invoke-RestMethod -Uri "http://localhost:8002/web-search?q=test"
```

### 5.4 Chat (Modo Chat)

```powershell
$body = '{"message":"hola","mode":"chat"}'
Invoke-RestMethod -Uri "http://localhost:8002/chat" `
  -Method POST `
  -ContentType "application/json" `
  -Body $body
```

**Esperado**: Respuesta sin fuentes (`sources: []`)

### 5.5 Chat (Modo RAG)

```powershell
$body = '{"message":"política de calidad","mode":"rag"}'
Invoke-RestMethod -Uri "http://localhost:8002/chat" `
  -Method POST `
  -ContentType "application/json" `
  -Body $body
```

**Esperado**: Respuesta con fuentes (`sources: [{...}]`)

### 5.6 Búsqueda Vectorial Directa

```powershell
$searchBody = @{
    query = "políticas de empresa"
    mode = "hybrid"
    k = 5
    alpha = 0.5
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://localhost:8002/api/v1/search `
  -Body $searchBody -ContentType "application/json"
```

---

<a name="configuracion-openwebui"></a>
## 6. 🖥️ Configuración de OpenWebUI

### 6.1 Primer Acceso

1. Abrir http://localhost:3002
2. Click en "Sign up"
3. Completar formulario:
   - Name: Tu nombre
   - Email: tu@email.com
   - Password: (contraseña segura)
4. Click "Create Account"

> **Nota**: El primer usuario registrado es administrador.

### 6.2 Verificar JARVIS

1. En la interfaz, ver selector de modelos (arriba)
2. Expandir lista
3. **Debes ver**:
   - ✅ **JARVIS** (el pipeline)
   - ✅ llama3.1
   - ✅ llava

### 6.3 Configurar Pipelines URL (Si No Aparece)

1. Ir a **Settings** (⚙️)
2. **Admin Panel** → **Settings** → **Connections**
3. Verificar/añadir:
   - **Pipelines URL**: `http://pipelines:9100`
   - ✅ Enabled
4. Click "Save"
5. Refrescar página

**Forzar recarga de pipelines**:
```powershell
docker compose restart pipelines openwebui
```

---

<a name="testing-openwebui"></a>
## 7. 💬 Testing en OpenWebUI

### 7.1 Seleccionar Modelo

1. En el chat, selector de modelo (arriba)
2. Seleccionar: **"JARVIS"**

### 7.2 Comando /listar

**Escribir**:
```
/listar
```

**Esperado**:
```
📋 Consultando documentos disponibles...

Total de documentos: N

1. 📄 Manual_ISO.pdf
2. 📄 Politica_Calidad.pdf

💡 Puedes preguntarme sobre cualquiera de estos documentos.
```

### 7.3 Chat Normal (Sin Fuentes)

**Escribir**:
```
Hola, ¿cómo estás?
```

**Esperado**: Respuesta conversacional SIN sección "📚 Fuentes"

### 7.4 RAG (Con Fuentes)

**Escribir** (si tienes documentos indexados):
```
¿Qué dice la política de calidad?
```

**Esperado**:
```
📚 Consultando documentos internos...

Según la Política de Calidad (documento MAP-003)...

---
### 📚 Fuentes Citadas:

[1] 📄 Politica_Calidad.pdf (pág. 2) - relevancia: 0.87
```

### 7.5 Web Search

**Escribir**:
```
Busca en internet el precio del Bitcoin
```

**Esperado**:
```
🌐 Buscando en internet...

El precio actual del Bitcoin es aproximadamente...

---
### 🌐 Fuentes Web:

[1] CoinMarketCap - Bitcoin Price
```

### 7.6 Web Scraping

**Escribir**:
```
Analiza esta URL https://es.wikipedia.org/wiki/Inteligencia_artificial
```

**Esperado**:
```
🔍 Analizando URL: https://...

✅ El contenido se está procesando e indexando.
```

### 7.7 Verificar Estado de Ingestión

**Escribir**:
```
cómo va?
```

**Esperado**: Tabla con estado de archivos en proceso

---

<a name="archivos-adjuntos"></a>
## 8. 📎 Archivos Adjuntos en Chat (Procesamiento Local)

> ⚠️ **Esta función NO usa el backend RAG** - procesa directamente con Ollama.

### 8.1 Probar Archivo Adjunto

1. Ir a http://localhost:3002 (OpenWebUI)
2. Subir un PDF/TXT directamente en el chat (botón de adjuntar)
3. Escribir: "resume este documento"

**Resultado esperado**:
```
📎 **Procesando archivo adjunto localmente...**

[Resumen del contenido]

---
📎 *Archivo procesado localmente con Ollama: nombre.pdf*
```

### 8.2 Diferencias con RAG

| Característica | Archivo en Chat | RAG (documentos indexados) |
|----------------|-----------------|---------------------------|
| Procesamiento | 100% local (Ollama) | Backend + LiteLLM |
| OCR | ❌ No disponible | ✅ PaddleOCR |
| Búsqueda vectorial | ❌ No | ✅ Sí |
| Persistencia | ❌ Una sesión | ✅ Permanente |

### 8.3 PDFs Escaneados

Los PDFs escaneados (sin texto seleccionable) **NO funcionan** en chat directo.

**Solución**: Usar la carpeta `data/watch/`:
```powershell
# 1. Copiar PDF escaneado a watch
Copy-Item "C:\ruta\pdf_escaneado.pdf" "C:\TFG-RAG-Clean\data\watch\"

# 2. Forzar procesamiento con OCR
Invoke-RestMethod -Method Post -Uri http://localhost:8003/scan

# 3. Esperar indexación (30 segundos)
Start-Sleep 30

# 4. Consultar via RAG en chat: "resume el documento pdf_escaneado"
```

---

<a name="ingestion-documentos"></a>
## 9. 📄 Ingestión de Documentos

### 9.1 Ingestión Automática (Watch Folder)

```powershell
# Crear archivo de prueba
$testContent = "Este es un documento de prueba para el sistema RAG."
Set-Content -Path "C:\TFG-RAG-Clean\data\watch\test_ingestion.txt" -Value $testContent

# Forzar escaneo
Invoke-RestMethod -Method Post -Uri http://localhost:8003/scan

# Verificar estado (esperar 10 segundos)
Start-Sleep -Seconds 10
Invoke-RestMethod -Uri http://localhost:8002/documents/ingestion-status
```

**Resultado esperado**: `test_ingestion.txt` con status `completed`

### 9.2 Upload Manual via API

```powershell
# Subir archivo
$filePath = "C:\ruta\a\documento.pdf"
$form = @{
    file = Get-Item $filePath
}
Invoke-RestMethod -Uri "http://localhost:8002/upload" -Method Post -Form $form
```

### 9.3 Verificar Indexación

```powershell
# Ver logs del indexer
docker compose logs tfg-indexer --tail 50

# Ver estadísticas de Qdrant
Invoke-RestMethod http://localhost:8002/documents/stats
```

**Buscar en logs**:
```
✓ Documento procesado e indexado: tu-documento.pdf (50 chunks)
```

### 9.4 Borrado Automático

Cuando eliminas un archivo de `data/watch/`, se elimina automáticamente de Qdrant.

```powershell
# Eliminar archivo
Remove-Item "C:\TFG-RAG-Clean\data\watch\test_ingestion.txt"

# Verificar que se borró de Qdrant
Invoke-RestMethod -Uri http://localhost:8002/documents/list
```

---

<a name="busquedas-rag"></a>
## 10. 🔍 Búsquedas RAG

### 10.1 Búsqueda Semántica

```powershell
$searchBody = @{
    query = "requisitos de auditoría"
    mode = "semantic"
    k = 5
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://localhost:8002/api/v1/search `
  -Body $searchBody -ContentType "application/json"
```

### 10.2 Búsqueda Híbrida (Recomendada)

```powershell
$searchBody = @{
    query = "políticas de empresa"
    mode = "hybrid"
    k = 5
    alpha = 0.5
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://localhost:8002/api/v1/search `
  -Body $searchBody -ContentType "application/json"
```

### 10.3 Filtrar por Documento Específico

```powershell
$searchBody = @{
    query = "calidad"
    mode = "hybrid"
    k = 5
    filter = @{
        filename = "Politica_Calidad.pdf"
    }
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://localhost:8002/api/v1/search `
  -Body $searchBody -ContentType "application/json"
```

---

<a name="web-search"></a>
## 11. 🌐 Web Search y Scraping

### 11.1 Búsqueda Web

```powershell
Invoke-RestMethod -Uri "http://localhost:8002/web-search?q=últimas%20noticias%20tecnología"
```

### 11.2 Web Scraping

```powershell
$scrapeBody = @{
    url = "https://es.wikipedia.org/wiki/Inteligencia_artificial"
    tenant_id = "default"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://localhost:8002/scrape `
  -Body $scrapeBody -ContentType "application/json"
```

---

<a name="monitoreo-estado"></a>
## 12. 📊 Monitoreo de Estado

### 12.1 Via API

```powershell
Invoke-RestMethod -Uri http://localhost:8002/documents/ingestion-status?limit=20
```

### 12.2 Via Chat

En OpenWebUI preguntar:
- "cómo va?"
- "status de documentos"
- "qué tal va la indexación?"

### 12.3 Via PostgreSQL Directo

```powershell
docker exec -it tfg-postgres psql -U rag_user -d rag_system -c "SELECT * FROM ingestion_status ORDER BY updated_at DESC LIMIT 10;"
```

---

<a name="consultas-db"></a>
## 13. 💾 Consultas de Base de Datos

### 13.1 PostgreSQL - Conversaciones

```powershell
docker exec -it tfg-postgres psql -U rag_user -d rag_system -c "SELECT id, user_id, query, created_at FROM conversation_history ORDER BY created_at DESC LIMIT 10;"
```

### 13.2 PostgreSQL - Documentos

```powershell
docker exec -it tfg-postgres psql -U rag_user -d rag_system -c "SELECT filename, status, chunk_count, created_at FROM documents ORDER BY created_at DESC;"
```

### 13.3 Qdrant - Ver Colecciones

```powershell
Invoke-RestMethod -Uri http://localhost:6335/collections
```

### 13.4 Qdrant - Estadísticas de Documentos

```powershell
Invoke-RestMethod -Uri http://localhost:6335/collections/documents | ConvertTo-Json -Depth 5
```

### 13.5 Redis - Ver Caché

```powershell
docker exec -it tfg-redis redis-cli KEYS "*"
docker exec -it tfg-redis redis-cli INFO stats
```

---

<a name="health-checks"></a>
## 14. 🏥 Health Checks

```powershell
# Backend
Invoke-RestMethod -Uri http://localhost:8002/health

# Indexer
Invoke-RestMethod -Uri http://localhost:8003/health

# Qdrant
Invoke-RestMethod -Uri http://localhost:6335/collections

# Ollama (modelos)
Invoke-RestMethod -Uri http://localhost:11435/api/tags

# Prometheus
Invoke-RestMethod -Uri http://localhost:9091/-/ready

# Grafana
Invoke-RestMethod -Uri http://localhost:3003/api/health
```

### Script de Verificación Completa

```powershell
$services = @(
    @{Name="Backend"; Url="http://localhost:8002/health"},
    @{Name="Indexer"; Url="http://localhost:8003/health"},
    @{Name="Qdrant"; Url="http://localhost:6335/readyz"},
    @{Name="Ollama"; Url="http://localhost:11435/api/tags"},
    @{Name="Prometheus"; Url="http://localhost:9091/-/ready"},
    @{Name="Grafana"; Url="http://localhost:3003/api/health"}
)

foreach ($svc in $services) {
    try {
        Invoke-WebRequest -Uri $svc.Url -TimeoutSec 5 | Out-Null
        Write-Host "✅ $($svc.Name)" -ForegroundColor Green
    } catch {
        Write-Host "❌ $($svc.Name)" -ForegroundColor Red
    }
}
```

---

<a name="monitoreo-prometheus"></a>
## 15. 📈 Monitoreo con Prometheus y Grafana

### 15.1 Acceso a Prometheus

```powershell
Start-Process "http://localhost:9091"
```

**Verificar targets**:
- Ir a **Status** → **Targets**
- Todos deben estar **UP** (verde)

**Targets configurados**:

| Job | Endpoint | Función |
|-----|----------|---------|
| `prometheus` | localhost:9091 | Prometheus mismo |
| `tfg-backend` | tfg-backend:8002 | API principal con métricas |
| `qdrant` | qdrant:6335 | Base de datos vectorial |

### 15.2 Acceso a Grafana

```powershell
Start-Process "http://localhost:3003"
```

**Credenciales por defecto**:
- **Usuario**: `admin`
- **Contraseña**: Ver variable `GRAFANA_PASSWORD` en `.env` (default: `admin`)

> 💡 La contraseña está definida en el archivo `.env` como `GRAFANA_PASSWORD`.
> En desarrollo local típicamente es `dummy_grafana_password` u otro valor personalizado.

### 15.3 Dashboard JARVIS RAG

**URL directa**: http://localhost:3003/d/jarvis-tfg-dashboard

El dashboard incluye:
- Estado de servicios: Backend, Qdrant, Prometheus
- Requests por segundo por endpoint
- Latencia p50/p95
- Uso de memoria del backend

### 15.4 Queries PromQL Útiles

```promql
# Requests por segundo por endpoint
sum(rate(http_requests_total{job="tfg-backend"}[1m])) by (endpoint)

# Latencia p95
histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{job="tfg-backend"}[5m])) by (le))

# Errores 5xx
sum(rate(http_requests_total{job="tfg-backend",status=~"5.."}[5m]))

# Memoria del backend en GB
process_resident_memory_bytes{job="tfg-backend"} / 1024 / 1024 / 1024
```

### 15.5 Métricas Disponibles del Backend

| Métrica | Tipo | Descripción |
|---------|------|-------------|
| `http_requests_total` | Counter | Total de requests por endpoint |
| `http_request_duration_seconds` | Histogram | Latencia de requests |
| `process_resident_memory_bytes` | Gauge | Memoria usada |
| `app_info` | Info | Información de la aplicación |

---

<a name="documentacion-api"></a>
## 16. 📚 Documentación de API (OpenAPI)

### 16.1 Swagger UI (Interactivo)

```powershell
Start-Process "http://localhost:8002/docs"
```

Permite probar todos los endpoints directamente.

### 16.2 ReDoc (Estático)

```powershell
Start-Process "http://localhost:8002/redoc"
```

Documentación en formato más legible.

### 16.3 OpenAPI JSON

```powershell
Invoke-RestMethod http://localhost:8002/openapi.json | ConvertTo-Json -Depth 3
```

### 16.4 Endpoints Disponibles

| Tag | Endpoints |
|-----|-----------|
| Health | `/health`, `/` |
| Documents | `/documents/list`, `/documents/stats`, `/upload`, etc. |
| Search | `/api/v1/search` |
| Chat | `/chat` |
| Web | `/web-search`, `/scrape` |

---

<a name="tests-automatizados"></a>
## 17. 🧪 Tests Automatizados

### 17.1 Estructura de Tests

```
tests/
├── integration/
│   └── test_api_endpoints.py  # Tests de API
└── e2e/
    └── test_rag_flow.py       # Test completo del flujo
```

### 17.2 Ejecutar Tests de Integración

```powershell
# Requiere servicios corriendo
pytest tests/integration/test_api_endpoints.py -v
```

**Tests incluidos**:
- Health endpoints
- OpenAPI availability
- Documents endpoints
- Search endpoints
- Chat endpoints

### 17.3 Ejecutar Tests E2E

```powershell
pytest tests/e2e/test_rag_flow.py -v -s
```

**Flujo del test**:
1. Sube documento de prueba
2. Espera indexación
3. Consulta RAG sobre el documento
4. Verifica respuesta con fuentes
5. Limpia documento

### 17.4 Ejecutar Todos los Tests con Cobertura

```powershell
pytest tests/ -v --cov=backend/app --cov-report=html
```

---

<a name="logs-debugging"></a>
## 18. 🔧 Logs y Debugging

### 18.1 Ver Todos los Logs

```powershell
docker compose logs -f
```

### 18.2 Logs Específicos

```powershell
docker compose logs tfg-backend --tail 50
docker compose logs tfg-indexer --tail 50
docker compose logs pipelines --tail 50
docker compose logs litellm --tail 20
docker compose logs qdrant --tail 20
```

### 18.3 Debug Mode en Pipeline

El pipeline tiene `DEBUG_MODE: bool = True` por defecto.

Ver detección de intención:
```powershell
docker compose logs pipelines --tail 20
```

**Buscar líneas**:
```
🎯 Usuario: user@empresa.com
📨 Mensaje: ¿Qué dice la política?
🤖 Acción detectada: rag
```

---

<a name="troubleshooting"></a>
## 19. ❓ Troubleshooting

### Problema 1: "JARVIS" NO aparece en OpenWebUI

```powershell
# 1. Verificar pipelines corriendo
docker compose logs pipelines --tail 30

# 2. Verificar archivo existe
ls services\openwebui\pipelines\jarvis.py

# 3. Reiniciar
docker compose restart pipelines openwebui

# 4. Esperar 30 seg, refrescar navegador
```

### Problema 2: Error 500 en /web-search

```powershell
# Ver error exacto
docker compose logs tfg-backend --tail 50

# Si dice "No module named 'bs4'"
docker compose build tfg-backend --no-cache
docker compose restart tfg-backend
```

### Problema 3: NO encuentra documentos (mode=rag)

```powershell
# 1. Ver estadísticas
Invoke-RestMethod http://localhost:8002/documents/stats

# Si points_count = 0, no hay docs

# 2. Copiar PDF de prueba
Copy-Item "C:\documento.pdf" "c:\TFG-RAG-Clean\data\watch\"

# 3. Ver logs indexer
docker compose logs tfg-indexer -f

# Esperar: "✓ Documento procesado e indexado"
```

### Problema 4: Grafana NO muestra datos

1. Verificar Prometheus targets en http://localhost:9091/targets
2. Verificar datasource en Grafana → Configuration → Data Sources
3. Verificar que backend expone `/metrics`:
   ```powershell
   Invoke-RestMethod http://localhost:8002/metrics
   ```

### Problema 5: LLM muy lento

**Causas**: CPU en lugar de GPU, modelo grande

**Solución**: Usar modelo ligero Q4 o reducir contexto

### Problema 6: Qdrant muestra "unhealthy"

Esto es cosmético si Qdrant responde:
```powershell
Invoke-RestMethod http://localhost:6335/collections
```

### Problema 7: pgAdmin no conecta

1. Email: `admin@example.com`
2. Password pgAdmin: `admin`
3. Password PostgreSQL: `changeme`

---

<a name="checklist"></a>
## 20. ✅ Checklist Completo de Verificación

### Infraestructura

| Verificación | Comando | ☐ |
|-------------|---------|---|
| Docker corriendo | `docker ps` | ☐ |
| Todos servicios up | `docker compose ps` | ☐ |
| Backend sin errores | `docker compose logs tfg-backend --tail 20` | ☐ |
| Pipelines cargado | Log: "JARVIS inicializado" | ☐ |
| Modelos Ollama | `docker compose exec ollama ollama list` | ☐ |

### Endpoints API

| Verificación | Comando | ☐ |
|-------------|---------|---|
| Health OK | `GET /health` → "healthy" | ☐ |
| Documents list | `GET /documents/list` → respuesta | ☐ |
| Web search | `GET /web-search?q=test` → resultados | ☐ |
| Metrics | `GET /metrics` → prometheus format | ☐ |
| OpenAPI | `GET /docs` → Swagger UI | ☐ |

### OpenWebUI

| Verificación | Acción | ☐ |
|-------------|--------|---|
| Acceso web | http://localhost:3002 carga | ☐ |
| Login/registro | Crear cuenta o login | ☐ |
| JARVIS visible | En selector de modelos | ☐ |
| `/listar` funciona | Muestra documentos | ☐ |
| Chat normal | Sin fuentes | ☐ |
| RAG funciona | Con fuentes (si hay docs) | ☐ |
| Web search | Fuentes web | ☐ |

### Monitoreo

| Verificación | URL | ☐ |
|-------------|-----|---|
| Promethe targets UP | http://localhost:9091/targets | ☐ |
| Grafana accesible | http://localhost:3003 | ☐ |
| Dashboard con datos | JARVIS dashboard | ☐ |
| pgAdmin accesible | http://localhost:5051 | ☐ |
| Qdrant dashboard | http://localhost:6335/dashboard | ☐ |

### Ingestión

| Verificación | Acción | ☐ |
|-------------|--------|---|
| Watch folder existe | `data/watch/` | ☐ |
| Archivo se indexa | Copiar PDF, esperar 30s | ☐ |
| Status tracking | `GET /documents/ingestion-status` | ☐ |
| Borrado funciona | Eliminar archivo, verificar | ☐ |

---

## 🎉 ¡Sistema 100% Funcional!

Si todos los checks están ✅, el sistema está completamente operativo.

**Documentos relacionados**:
- [USER_GUIDE.md](USER_GUIDE.md) - Para usuarios finales
- [DATABASE_STORAGE_GUIDE.md](DATABASE_STORAGE_GUIDE.md) - Bases de datos y credenciales
- [TECHNICAL_ARCHITECTURE.md](TECHNICAL_ARCHITECTURE.md) - Arquitectura técnica

---

**JARVIS Team** | 2025


