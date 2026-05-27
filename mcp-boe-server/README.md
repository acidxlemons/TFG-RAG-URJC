# 🏛️ MCP Server BOE - Boletín Oficial del Estado

Servidor MCP (Model Context Protocol) para consultar el Boletín Oficial del Estado de España.

---

## ✅ Estado: Funcionando

```
[SUCCESS] MCP SERVER FUNCIONA CORRECTAMENTE!
Herramientas: 7
- get_boe_summary      → 52040 chars (143 items)
- search_legislation   → 6724 chars (15 resultados)
```

---

## 📋 Herramientas Disponibles

| Herramienta | Descripción | Ejemplo |
|-------------|-------------|---------|
| `get_boe_summary` | Sumario del BOE | Hoy o fecha específica |
| `search_legislation` | Buscar legislación | "vivienda", "defensa" |
| `get_law_text` | Texto completo | BOE-A-2018-16673 |
| `get_law_metadata` | Metadatos de ley | Fecha, departamento |
| `get_law_analysis` | Análisis jurídico | Modifica/modificada por |
| `get_subjects` | Lista de materias | Categorías del BOE |
| `resolve_law_name` | Nombre a ID | LOPD → BOE-A-2018-16673 |

---

## 🚀 Cómo Usarlo AHORA

### Opción 1: Desde la Terminal (Test)

Ejecuta el script de prueba para verificar que funciona:

```bash
cd mcp-boe-server
python test_mcp_real.py
```

**Qué hace**: Conecta al servidor MCP, lista herramientas, y ejecuta búsquedas reales.

### Opción 2: Con Claude Desktop (Recomendado)

Claude Desktop de Anthropic soporta MCP nativamente. Es la forma más fácil de "ver la magia".

**Paso 1: Instalar Claude Desktop**
- Descarga desde [claude.ai/desktop](https://claude.ai/desktop)

**Paso 2: Configurar el servidor**
Edita: `%APPDATA%\Claude\claude_desktop_config.json` (Windows)
O: `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac)

```json
{
  "mcpServers": {
    "boe": {
      "command": "python",
      "args": ["C:/TFG-RAG-Clean/mcp-boe-server/mcp_boe_server.py", "--stdio"]
    }
  }
}
```

**Paso 3: Reiniciar Claude Desktop**

**Paso 4: Preguntar**
Escribe en Claude Desktop:
> "¿Qué se ha publicado hoy en el BOE?"

Claude automáticamente llamará a `get_boe_summary` y te dará la respuesta.

### Opción 3: Otros Clientes MCP

| Cliente | Tipo | Instrucciones |
|---------|------|---------------|
| **Continue.dev** | VS Code Extension | Añadir al `config.json` |
| **Cline** | VS Code Extension | Configuración similar a Claude |
| **Cursor** | Editor AI | Soporta MCP nativo |

---

## 🔮 Uso Futuro: OpenWebUI

OpenWebUI está añadiendo soporte nativo para MCP. Cuando esté listo:

### Estado Actual (Experimental)

OpenWebUI tiene soporte MCP **experimental** que requiere:
- Modelo con "Function Calling" habilitado
- Configuración manual de OpenAPI Tools

**Por qué no funciona todavía al 100%:**
- El protocolo "streamable-http" requiere gestión de sesiones SSE
- Los modelos locales (Llama 3.1) no siempre entienden cuándo usar tools

### Cuando esté listo (futuro)

Esperamos que OpenWebUI simplifique la configuración a algo como:

```yaml
# Configuración futura esperada en OpenWebUI
mcp_servers:
  - name: boe
    url: http://mcp-boe:8010/mcp
```

Y podrás preguntar directamente en el chat:
> "Busca en el BOE legislación sobre teletrabajo"

El modelo automáticamente usará la herramienta sin programar nada.

### Mientras tanto (producción)

Usamos la **Vía Robusta**: El pipeline `jarvis.py` detecta "BOE" en tu mensaje y llama directamente al backend. Es más fiable para producción.

---

## 🐳 Docker

El servidor ya está incluido en el stack:

```bash
# Ver logs
docker logs tfg-mcp-boe -f

# Reiniciar
docker restart tfg-mcp-boe

# Puerto expuesto en el host
# http://localhost:8011
#
# Puerto interno del contenedor
# http://mcp-boe:8010
```

---

## 📁 Archivos

```
mcp-boe-server/
├── mcp_boe_server.py      # Servidor MCP principal
├── boe_connector.py       # Conector API BOE
├── test_mcp_real.py       # Test de verificación
├── requirements.txt       # pip install fastmcp requests
├── Dockerfile             # Imagen Docker
├── claude_desktop_config.json  # Ejemplo de config
└── README.md              # Este archivo
```

---

## 🧪 Verificación Rápida

```bash
# 1. Verificar que el contenedor está corriendo
docker ps | grep mcp-boe

# 2. Health check local
curl http://localhost:8011/health

# 3. Test completo
cd mcp-boe-server
python test_mcp_real.py
```

---

*Desarrollado para TFG - Universidad Rey Juan Carlos*
