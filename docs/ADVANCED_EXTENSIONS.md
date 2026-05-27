# 🔌 Guía de Extensiones Avanzadas - JARVIS RAG System

**Proyecto**: TFG - Universidad Rey Juan Carlos  
**Versión**: 1.0  
**Fecha**: Enero 2026

---

## 📖 Índice

1. [Nginx como Reverse Proxy](#1-nginx-como-reverse-proxy)
2. [Autenticación SSO (Azure AD, Shibboleth, etc)](#2-autenticación-sso)
3. [Model Context Protocol (MCP)](#3-model-context-protocol-mcp)
4. [Implementación de MCP en JARVIS](#4-implementación-de-mcp-en-jarvis)
5. [Trabajo Futuro](#5-trabajo-futuro)
   - [Roadmap MCP](#51-roadmap-mcp)
   - [Crawlee: Web Scraping de Producción](#52-crawlee-web-scraping-de-producción)

---

## 1. Nginx como Reverse Proxy

### 1.1 ¿Qué es Nginx?

**Nginx** (pronunciado "engine-x") es un servidor web de alto rendimiento que también funciona como:
- **Reverse Proxy**: Redirige peticiones a servicios internos
- **Load Balancer**: Distribuye carga entre múltiples instancias
- **SSL/TLS Terminator**: Maneja HTTPS para los servicios backend
- **Caché**: Almacena respuestas para reducir latencia

### 1.2 Diagrama de Arquitectura

```
                    INTERNET
                        │
                        ▼
              ┌─────────────────┐
              │     NGINX       │  ◄── Puerto 8443 (HTTPS)
              │  Reverse Proxy  │      puerto 8080 (redirect)
              └────────┬────────┘
                       │
         ┌─────────────┼─────────────┐
         ▼             ▼             ▼
   ┌──────────┐  ┌──────────┐  ┌──────────┐
   │ OpenWebUI│  │ Backend  │  │ Grafana  │
   │ :8080    │  │ :8002    │  │ :3002    │
   └──────────┘  └──────────┘  └──────────┘
```

### 1.3 Funciones en JARVIS

| Función | Configuración | Beneficio |
|---------|---------------|-----------|
| **SSL Termination** | `ssl_certificate`, `ssl_certificate_key` | HTTPS para todos los servicios |
| **Rate Limiting** | `limit_req_zone` | Protección contra DDoS |
| **Security Headers** | `add_header HSTS`, etc. | Hardening de seguridad |
| **WebSocket Proxy** | `proxy_set_header Upgrade` | Chat en tiempo real |
| **Upload Handling** | `client_max_body_size 500M` | Subida de PDFs grandes |

### 1.4 Ubicación de Configuración

```
config/nginx/
├── nginx.conf          # Configuración principal
└── ssl/
    ├── cert.pem        # Certificado SSL
    └── key.pem         # Clave privada
```

---

## 2. Autenticación SSO

OpenWebUI soporta múltiples proveedores de identidad. Este proyecto usa **Azure AD** como proveedor principal.

### 2.1 Azure AD (Microsoft Entra ID) - IMPLEMENTADO ✅

**Qué es**: Servicio de identidad de Microsoft para empresas con Microsoft 365.

**Por qué Azure AD**:
- ✅ Los usuarios ya tienen cuenta corporativa (mismo login que Outlook/Teams)
- ✅ Integración con grupos de Azure → permisos RAG automáticos
- ✅ Single Sign-On real (no hay segundo login)
- ✅ MFA ya configurado a nivel de empresa

#### Configuración en Azure Portal

1. **Registrar aplicación**:
   - Azure Portal → Azure Active Directory → App registrations → New registration
   - Nombre: `JARVIS OpenWebUI`
   - Redirect URI: `https://tu-dominio.com/oauth/oidc/callback`

2. **Configurar permisos**:
   - API permissions → Add → Microsoft Graph → Delegated:
     - `openid`
     - `profile`
     - `email`
     - `User.Read`
     - `GroupMember.Read.All` (para grupos RAG)

3. **Crear secreto**:
   - Certificates & secrets → New client secret
   - Copiar el valor (solo visible una vez)

4. **Obtener IDs**:
   - Overview → Application (client) ID
   - Overview → Directory (tenant) ID

#### Configuración en docker-compose.yml

```yaml
openwebui:
  environment:
    # SSO Azure AD
    ENABLE_OAUTH_SIGNUP: "true"
    OAUTH_PROVIDER: oidc
    OPENID_PROVIDER_URL: https://login.microsoftonline.com/${AZURE_TENANT_ID}/v2.0/.well-known/openid-configuration
    OAUTH_CLIENT_ID: ${AZURE_CLIENT_ID}
    OAUTH_CLIENT_SECRET: ${AZURE_CLIENT_SECRET}
    OAUTH_SCOPES: openid profile email
    OPENID_REDIRECT_URI: ${APP_URL}/oauth/oidc/callback
```

#### Variables en .env

```env
# Azure AD SSO
AZURE_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_SECRET=tu_secreto_aqui
APP_URL=https://jarvis.tuempresa.com
```

#### Flujo de Autenticación

```
Usuario abre JARVIS
    │
    ▼
OpenWebUI → Redirect a login.microsoftonline.com
    │
    ▼
Usuario introduce credenciales Microsoft
(o ya está logueado en Teams/Outlook)
    │
    ▼
Azure AD valida → Devuelve JWT con claims
    │
    ▼
OpenWebUI extrae:
├── email (identidad)
├── name (display)
└── groups (para permisos RAG)
    │
    ▼
Usuario accede a JARVIS con permisos correctos
```

#### Mapeo de Grupos Azure → Colecciones RAG

El pipeline `jarvis.py` usa los grupos del usuario para filtrar documentos:

```python
# En jarvis.py
user_groups = user_info.get("groups", [])
# Mapeo: "SG-Calidad" → colección "calidad"
allowed_collections = [group_to_collection(g) for g in user_groups]
```

📖 Ver [SHAREPOINT_INTEGRATION.md](SHAREPOINT_INTEGRATION.md) para configuración detallada de grupos.

---

### 2.2 Alternativas a Azure AD

Si no tienes Microsoft 365, aquí hay otras opciones:

#### Opción A: Google OAuth



**Configuración en `docker-compose.yml`**:
```yaml
environment:
  ENABLE_OAUTH_SIGNUP: "true"
  OAUTH_PROVIDER: google
  GOOGLE_CLIENT_ID: ${GOOGLE_CLIENT_ID}
  GOOGLE_CLIENT_SECRET: ${GOOGLE_CLIENT_SECRET}
```

**Pasos**:
1. Ir a [Google Cloud Console](https://console.cloud.google.com)
2. Crear proyecto → APIs & Services → Credentials
3. Create OAuth 2.0 Client ID
4. Redirect URI: `https://tu-dominio.com/oauth/google/callback`
5. Copiar Client ID y Secret al `.env`

#### Opción B: Shibboleth (Ámbito Universitario)

**Shibboleth** es el estándar de facto para federaciones de identidad académica (SAML 2.0). Es lo que usa la URJC y la mayoría de universidades.

**Reto Técnico**:
OpenWebUI habla **OIDC** (moderno), mientras que Shibboleth habla **SAML** (clásico). No se entienden directamente.

**Solución de Implementación**:
Se requiere un "puente" intermedio. **Keycloak** es la pieza clave:

```
[OpenWebUI] ◄──OIDC──► [Keycloak] ◄──SAML──► [Shibboleth URJC]
```

1. Configurar Keycloak como "Identity Broker".
2. Añadir Shibboleth como "Identity Provider" en Keycloak (importando el XML de metadatos de la universidad).
3. OpenWebUI confía en Keycloak, y Keycloak confía en la Universidad.

**Configuración Teórica**:
```yaml
# docker-compose.yml
keycloak:
  image: quay.io/keycloak/keycloak:latest
  environment:
    - KC_FEATURES=token-exchange,scripts  # Habilitar SAML
```

> **Nota TFG**: Esta sería la integración ideal para un despliegue real en la infraestructura de la universidad.

#### Opción C: GitHub OAuth

**Configuración**:
```yaml
environment:
  ENABLE_OAUTH_SIGNUP: "true"
  OAUTH_PROVIDER: github
  GITHUB_CLIENT_ID: ${GITHUB_CLIENT_ID}
  GITHUB_CLIENT_SECRET: ${GITHUB_CLIENT_SECRET}
```

**Pasos**:
1. GitHub → Settings → Developer Settings → OAuth Apps
2. New OAuth App
3. Callback URL: `https://tu-dominio.com/oauth/github/callback`

#### Opción D: Keycloak (Self-Hosted)


**Keycloak** es un servidor de identidad open-source que puedes hostear tú mismo.

**docker-compose.yml adicional**:
```yaml
keycloak:
  image: quay.io/keycloak/keycloak:latest
  environment:
    KEYCLOAK_ADMIN: admin
    KEYCLOAK_ADMIN_PASSWORD: ${KEYCLOAK_PASSWORD}
  command: start-dev
  ports:
    - "8180:8080"
```

**Configuración OpenWebUI**:
```yaml
environment:
  ENABLE_OAUTH_SIGNUP: "true"
  OAUTH_PROVIDER: oidc
  OAUTH_CLIENT_ID: jarvis-client
  OAUTH_CLIENT_SECRET: ${KEYCLOAK_CLIENT_SECRET}
  OPENID_PROVIDER_URL: http://keycloak:8080/realms/jarvis
  OPENID_REDIRECT_URI: https://tu-dominio.com/oauth/oidc/callback
```

**Ventajas de Keycloak**:
- ✅ 100% self-hosted (sin dependencias externas)
- ✅ Soporta LDAP, Active Directory, social login
- ✅ Multi-factor authentication
- ✅ Gestión de usuarios con UI

#### Opción E: Authentik (Alternativa Moderna)

```yaml
authentik:
  image: ghcr.io/goauthentik/server:latest
  environment:
    AUTHENTIK_SECRET_KEY: ${AUTHENTIK_SECRET}
    AUTHENTIK_POSTGRESQL__HOST: postgres
```

### 2.5 Comparativa de Proveedores SSO

| Proveedor | Self-Hosted | Dificultad | Ideal para |
|-----------|-------------|------------|------------|
| Google OAuth | ❌ | ⭐ Fácil | Demos, startups |
| GitHub OAuth | ❌ | ⭐ Fácil | Equipos técnicos |
| Azure AD | ❌ | ⭐⭐ Media | Empresas Microsoft |
| **Keycloak** | ✅ | ⭐⭐⭐ Alta | Máximo control |
| Authentik | ✅ | ⭐⭐ Media | Homelab, self-hosted |

### 2.6 Configuración Básica Sin SSO (Desarrollo)

Para desarrollo local sin SSO:
```yaml
environment:
  ENABLE_OAUTH_SIGNUP: "false"
  ENABLE_SIGNUP: "true"  # Registro manual habilitado
```

---

## 3. Model Context Protocol (MCP)

### 3.1 ¿Qué es MCP? (Explicación Conceptual)

Imagina un **Robot**.

1.  **El Cerebro (LLM)**: Es el modelo de inteligencia artificial (Claude, Llama, GPT-4). Piensa, razona y decide.
2.  **Los Brazos (MCP Tools)**: Son herramientas que le permiten interactuar con el mundo (buscar en el BOE, leer archivos, consultar bases de datos).
3.  **El Sistema Nervioso (Protocolo MCP)**: Es el estándar que conecta el cerebro con los brazos.

Sin MCP, el LLM es un "cerebro en un frasco": muy listo pero aislado.
Con MCP, el LLM puede **actuar**.

### 3.2 ¿Dónde está el LLM? (La gran diferencia)

Es crucial distinguir entre una llamada manual y una llamada agéntica.

#### A. En la Terminal (Lo que probamos con scripts)
```
[Tú] ──(mando a distancia)──> [MCP Server]
```
*   **¿Quién decide?**: TÚ (el programador).
*   **¿Hay inteligencia?**: No. Es ejecución ciega.
*   **Uso**: Testing, scripts automatizados fijos.

#### B. En una Aplicación Real (OpenWebUI/Claude)
```
[Usuario] ──> [LLM] ──(decisión autónoma)──> [MCP Server]
```
*   **¿Quién decide?**: El LLM.
*   **El Proceso Mental**:
    1.  Usuario pregunta: "¿Qué dice la ley de vivienda?"
    2.  LLM piensa: *"Necesito información legal. Tengo una herramienta llamada `search_boe`. Voy a usarla."*
    3.  LLM envía orden JSON al servidor MCP.
    4.  Servidor responde al LLM.
    5.  LLM explica la respuesta al usuario.

### 3.3 Arquitectura "Híbrida" de JARVIS

En este proyecto hemos implementado **dos vías paralelas** para gestionar herramientas, buscando el equilibrio perfecto entre fiabilidad e innovación.

| Característica | 1. Vía Robusta (Router JARVIS) | 2. Vía Agéntica (MCP) |
|----------------|--------------------------------|-----------------------|
| **Tecnología** | Python Code (`jarvis.py`) | Protocolo MCP + LLM Tools |
| **Control** | **Imperativo**: `if "BOE" in text: call()` | **Autónomo**: El modelo decide. |
| **Fiabilidad** | **100%**. Siempre que digas "BOE", busca. | **Variable**. Depende de la "destreza" del modelo. |
| **Escalabilidad**| **Baja**. Requiere programar cada caso. | **Infinita**. Añades tools y el modelo aprende a usarlas. |
| **Caso de Uso** | Funciones críticas (Búsqueda diaria) | Exploración, herramientas complejas, Análisis de Datos |

> **Conclusión del TFG**: "JARVIS utiliza una arquitectura híbrida donde las tareas críticas usan rutas deterministas (Pipelines) para garantizar robustez, mientras que se expone una infraestructura MCP para permitir capacidades agénticas avanzadas y escalabilidad futura."

### 3.4 Arquitectura Técnica

```mermaid
graph TD
    User[Usuario] --> OpenWebUI
    
    subgraph "Vía Robusta (Producción)"
        OpenWebUI --> Jarvis[Pipeline JARVIS]
        Jarvis --"Detecta 'BOE'"--> PyFunc[Backend API]
    end
    
    subgraph "Vía Agéntica (Estándar MCP)"
        OpenWebUI --"Function Calling"--> Llama[LLM (Llama 3.1)]
        Llama --"Decide usar herramienta"--> MCP[MCP Server]
    end
    
    PyFunc --> BOE[(API BOE)]
    MCP --> BOE
```

---

## 4. Implementación de MCP en JARVIS

### 4.1 Estructura Propuesta

```
services/
├── mcp-servers/
│   ├── rag_server.py       # MCP Server para RAG
│   ├── boe_server.py       # MCP Server para BOE
│   ├── metrics_server.py   # MCP Server para Grafana/Prometheus
│   └── requirements.txt
```

### 4.2 Ejemplo: MCP Server para RAG

```python
# services/mcp-servers/rag_server.py
"""
MCP Server para el sistema RAG de JARVIS.
Expone herramientas de búsqueda y listado de documentos.
"""

import asyncio
import httpx
from mcp.server import Server
from mcp.types import Tool, TextContent

# Crear servidor MCP
server = Server("jarvis-rag")

BACKEND_URL = "http://rag-backend:8000"


@server.tool()
async def search_documents(query: str, k: int = 5, mode: str = "hybrid") -> str:
    """
    Busca documentos relevantes en la base de conocimiento.
    
    Args:
        query: Texto de búsqueda
        k: Número de resultados (default: 5)
        mode: Modo de búsqueda - "semantic", "keyword", "hybrid"
    
    Returns:
        Documentos relevantes con scores de relevancia
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BACKEND_URL}/api/v1/search",
            json={"query": query, "k": k, "mode": mode}
        )
        results = response.json()
        
    # Formatear para el LLM
    formatted = []
    for i, doc in enumerate(results.get("results", []), 1):
        formatted.append(
            f"[{i}] {doc['filename']} (relevancia: {doc['score']:.2f})\n"
            f"    {doc['content'][:200]}..."
        )
    
    return "\n\n".join(formatted) if formatted else "No se encontraron documentos."


@server.tool()
async def list_documents(collection: str = "documents") -> str:
    """
    Lista todos los documentos indexados.
    
    Args:
        collection: Colección a consultar ("documents" o "webs")
    
    Returns:
        Lista de documentos con metadatos
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BACKEND_URL}/documents/list")
        data = response.json()
    
    docs = data.get("documents", [])
    if not docs:
        return "No hay documentos indexados."
    
    lines = [f"📚 Total: {len(docs)} documentos\n"]
    for doc in docs[:20]:  # Limitar a 20
        lines.append(f"  • {doc['filename']}")
    
    return "\n".join(lines)


@server.resource("documents://stats")
async def get_document_stats() -> str:
    """Estadísticas del sistema de documentos."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BACKEND_URL}/documents/stats")
        return response.text


if __name__ == "__main__":
    asyncio.run(server.run())
```

### 4.3 Ejemplo: MCP Server para BOE

```python
# services/mcp-servers/boe_server.py
"""
MCP Server para consultas al Boletín Oficial del Estado.
"""

import asyncio
import httpx
from mcp.server import Server

server = Server("jarvis-boe")

BOE_API = "https://www.boe.es/datosabiertos/api"


@server.tool()
async def search_boe(query: str, limit: int = 5) -> str:
    """
    Busca en el Boletín Oficial del Estado.
    
    Args:
        query: Términos de búsqueda (ej: "teletrabajo", "protección datos")
        limit: Número máximo de resultados
    
    Returns:
        Documentos legales relevantes del BOE
    """
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{BOE_API}/buscar/boe.json",
            params={"texto": query, "page": 1}
        )
        data = response.json()
    
    results = data.get("data", {}).get("items", [])[:limit]
    
    if not results:
        return f"No se encontraron resultados en el BOE para: {query}"
    
    formatted = [f"📜 Resultados del BOE para '{query}':\n"]
    for item in results:
        formatted.append(
            f"• {item.get('titulo', 'Sin título')}\n"
            f"  ID: {item.get('id')}\n"
            f"  Fecha: {item.get('fecha_publicacion')}\n"
            f"  Departamento: {item.get('departamento')}\n"
        )
    
    return "\n".join(formatted)


@server.tool()
async def get_boe_document(boe_id: str) -> str:
    """
    Obtiene el contenido de un documento específico del BOE.
    
    Args:
        boe_id: Identificador del documento (ej: "BOE-A-2020-11043")
    
    Returns:
        Contenido del documento legal
    """
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{BOE_API}/documento/{boe_id}")
        data = response.json()
    
    doc = data.get("data", {})
    return (
        f"📜 {doc.get('titulo', 'Documento')}\n\n"
        f"Fecha: {doc.get('fecha_publicacion')}\n"
        f"Departamento: {doc.get('departamento')}\n\n"
        f"Contenido:\n{doc.get('texto', 'No disponible')[:2000]}..."
    )


@server.tool()
async def get_daily_summary(date: str = None) -> str:
    """
    Obtiene el sumario del BOE de un día específico.
    
    Args:
        date: Fecha en formato YYYYMMDD (default: hoy)
    
    Returns:
        Resumen de publicaciones del día
    """
    from datetime import datetime
    
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{BOE_API}/diario/boe/{date}")
        data = response.json()
    
    items = data.get("data", {}).get("sumario", [])[:10]
    
    if not items:
        return f"No hay publicaciones para {date}"
    
    formatted = [f"📅 BOE del {date}:\n"]
    for item in items:
        formatted.append(f"  • {item.get('titulo', 'Sin título')}")
    
    return "\n".join(formatted)


if __name__ == "__main__":
    asyncio.run(server.run())
```

### 4.4 Configuración Docker para MCP

```yaml
# docker-compose.yml (añadir)
mcp-rag:
  build: ./services/mcp-servers
  environment:
    - BACKEND_URL=http://rag-backend:8000
  depends_on:
    - tfg-backend
  networks:
    - rag-network

mcp-boe:
  build: ./services/mcp-servers
  command: python boe_server.py
  networks:
    - rag-network
```

### 4.5 Integración con Claude Desktop

Archivo `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "jarvis-rag": {
      "command": "docker",
      "args": ["exec", "-i", "mcp-rag", "python", "rag_server.py"]
    },
    "jarvis-boe": {
      "command": "docker", 
      "args": ["exec", "-i", "mcp-boe", "python", "boe_server.py"]
    }
  }
}
```

---

## 5. Cómo Añadir Nuevos Servidores MCP

Si quieres usar servidores de la comunidad (ej. de `awesome-mcp-servers`), sigue estos pasos:

### 5.1 Ejemplo: Añadir Filesystem MCP (Node.js)

Queremos permitir que el LLM lea archivos de una carpeta local.

#### Paso 1: Dockerizar el Servicio

La mayoría de MCPs comunitarios son scripts Node.js o Python. Lo mejor es crearles un contenedor.

Crea `services/mcp-servers/filesystem/Dockerfile`:
```dockerfile
FROM node:18-alpine
WORKDIR /app
RUN npm install @modelcontextprotocol/server-filesystem
CMD ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"]
```

#### Paso 2: Añadir al Docker Compose

Edita `docker-compose.yml`:
```yaml
  mcp-filesystem:
    build: ./services/mcp-servers/filesystem
    volumes:
      - ./data/docs:/data  # Carpeta que el LLM podrá leer
    networks:
      - rag-network
```

#### Paso 3: Configurar Clientes

**Para Claude Desktop (`claude_desktop_config.json`)**:
```json
"filesystem": {
  "command": "docker",
  "args": ["exec", "-i", "tfg-mcp-filesystem", "npx", "@modelcontextprotocol/server-filesystem", "/data"]
}
```

**Para OpenWebUI (HTTP)**:
El servidor debe soportar SSE/HTTP. Si el MCP comunitario solo soporta STDIO (lo más común), necesitas un "bridge" o wrapper HTTP.

> 🛠️ **Nota**: FastMCP (Python) soporta HTTP nativo. Los servidores oficiales de Node.js suelen ser solo STDIO.

### 5.2 Checklist para MCPs Externos

1. **Lenguaje**: ¿Es Python o Node.js?
2. **Transporte**: ¿Soporta HTTP (para OpenWebUI) o solo STDIO (para Claude)?
3. **Seguridad**: ¿A qué datos le das acceso? (Cuidado con Filesystem o Database)
4. **Docker**: Siempre aísla el servicio en su propio contenedor.

---

## 6. Trabajo Futuro

### 5.1 Mejoras Propuestas con MCP

| Prioridad | Mejora | Descripción |
|-----------|--------|-------------|
| 🔴 Alta | MCP Server RAG | Exponer búsqueda vectorial |
| 🔴 Alta | MCP Server BOE | Consultas legales |
| 🟡 Media | MCP Server Metrics | Monitoreo desde chat |
| 🟡 Media | MCP Server Web Scraper | Indexación on-demand |
| 🟢 Baja | MCP Server Email | Integración con correo |

### 5.2 Crawlee: Web Scraping de Producción

#### ¿Qué es Crawlee?

[Crawlee](https://github.com/apify/crawlee-python) es una librería de scraping empresarial desarrollada por Apify. Diseñada específicamente para extraer datos para **IA, LLMs, RAG y GPTs**.

#### ¿Por qué considerar Crawlee?

| Característica | Stack Actual (aiohttp + BS4) | Crawlee |
|----------------|------------------------------|---------|
| **Retries automáticos** | ❌ Manual | ✅ Built-in con backoff exponencial |
| **Proxy rotation** | ❌ No soportado | ✅ Integrado |
| **Anti-blocking** | ❌ Sin protección | ✅ Fingerprint rotation, headers |
| **Persistencia** | ❌ Sin cola | ✅ Queue persistente de URLs |
| **Cambio HTTP → Browser** | ❌ Manual | ✅ Automático si falla HTTP |
| **Asyncio nativo** | ✅ Sí | ✅ Sí |

#### Arquitectura Propuesta

```
                    ┌─────────────────────────────────────────┐
                    │           CRAWLEE ROUTER                │
                    └─────────────────┬───────────────────────┘
                                      │
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
    ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
    │  BeautifulSoup   │   │   Playwright     │   │     HTTP         │
    │     Crawler      │   │     Crawler      │   │    Crawler       │
    │   (HTML parser)  │   │  (JS rendering)  │   │   (APIs/JSON)    │
    └──────────────────┘   └──────────────────┘   └──────────────────┘
              │                       │                       │
              └───────────────────────┴───────────────────────┘
                                      │
                                      ▼
                         ┌─────────────────────┐
                         │   Qdrant Indexer    │
                         │  (colección webs)   │
                         └─────────────────────┘
```

#### Implementación Ejemplo

```python
# backend/app/integrations/scraper/crawlee_scraper.py

from crawlee.beautifulsoup_crawler import BeautifulSoupCrawler
from crawlee.playwright_crawler import PlaywrightCrawler
from crawlee import Request

class CrawleeScraperService:
    """Scraper empresarial con Crawlee."""
    
    async def scrape_url(self, url: str, use_browser: bool = False) -> str:
        """
        Scrapea una URL con retry automático y anti-blocking.
        
        Args:
            url: URL a scrapear
            use_browser: True para páginas con JavaScript
        
        Returns:
            Texto extraído de la página
        """
        extracted_text = ""
        
        async def handler(context):
            nonlocal extracted_text
            # Crawlee maneja automáticamente:
            # - Retries con backoff
            # - Headers realistas
            # - Proxy rotation (si configurado)
            extracted_text = await context.page.inner_text('body')
        
        if use_browser:
            crawler = PlaywrightCrawler(
                max_requests_per_crawl=1,
                headless=True,
            )
        else:
            crawler = BeautifulSoupCrawler(
                max_requests_per_crawl=1,
            )
        
        crawler.router.default_handler(handler)
        await crawler.run([url])
        
        return extracted_text
    
    async def scrape_with_fallback(self, url: str) -> str:
        """Intenta HTTP primero, luego Playwright si falla."""
        try:
            text = await self.scrape_url(url, use_browser=False)
            if len(text.strip()) < 100:  # Contenido vacío/mínimo
                raise ValueError("Contenido insuficiente, usar browser")
            return text
        except Exception:
            return await self.scrape_url(url, use_browser=True)
```

#### Configuración Docker

```yaml
# docker-compose.yml (servicio adicional)
services:
  crawlee-worker:
    build:
      context: ./backend
      dockerfile: Dockerfile.crawlee
    environment:
      - PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
    volumes:
      - crawlee_storage:/app/.storage  # Queue persistente
    depends_on:
      - qdrant
```

#### Dependencias

```txt
# requirements-crawlee.txt
crawlee[beautifulsoup]>=0.5.0
crawlee[playwright]>=0.5.0
```

#### Migración Gradual

1. **Fase 1**: Mantener scraper actual, añadir Crawlee en paralelo
2. **Fase 2**: Usar Crawlee para URLs que fallan con stack actual
3. **Fase 3**: Migrar completamente cuando esté validado

#### Cuándo NO usar Crawlee

- Proyectos pequeños con pocas URLs
- Sitios que no bloquean requests
- Cuando la simplicidad es prioridad (TFG/demos)

> **Nota TFG**: El stack actual (aiohttp + BeautifulSoup + Playwright) es suficiente para el alcance académico. Crawlee se recomienda como mejora post-producción para escenarios enterprise.

---

### 5.3 Frase para Defensa de TFG

> "La arquitectura actual integra las funcionalidades directamente en el pipeline de OpenWebUI. Como evolución natural, se podría adoptar el **Model Context Protocol (MCP)** de Anthropic, un estándar abierto que permitiría:
> 
> 1. **Modularizar** cada integración (RAG, BOE, Web) como servidores independientes
> 2. **Reutilizar** estas capacidades desde cualquier cliente MCP (Claude, otros LLMs)
> 3. **Escalar** añadiendo nuevos servidores sin modificar el core
> 4. **Testear** cada componente de forma aislada
>
> Además, para escenarios de producción con alto volumen de scraping, se propone migrar a **Crawlee** de Apify, que ofrece retries automáticos, rotación de proxies y anti-blocking.
>
> Esto convertiría JARVIS de una aplicación monolítica a una **plataforma extensible de IA**."

### 5.4 Referencias

- [MCP Specification](https://modelcontextprotocol.io)
- [Anthropic MCP SDK](https://github.com/anthropics/mcp)
- [Crawlee Python](https://github.com/apify/crawlee-python)
- [Crawlee Documentation](https://crawlee.dev/python/)
- [OpenWebUI OAuth Docs](https://docs.openwebui.com/features/authentication)
- [Nginx Documentation](https://nginx.org/en/docs/)
- [Keycloak Getting Started](https://www.keycloak.org/getting-started)

---

*Documento generado para TFG - Universidad Rey Juan Carlos*
