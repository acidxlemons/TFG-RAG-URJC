# 📋 Checklist de Despliegue - JARVIS RAG System

**Proyecto**: TFG - Universidad Rey Juan Carlos

> **Documento de verificación pre-despliegue y criterios de éxito para cada fase**

---

## 🚀 Despliegue Rápido (Local/Desarrollo)

Si solo quieres levantar el sistema para probar, sigue estos pasos:

### Requisitos Previos
```bash
# Verificar Docker
docker --version    # Debe ser v24+
docker compose version  # Debe ser v2.20+

# Verificar GPU (opcional pero recomendado)
nvidia-smi          # Debe mostrar tu GPU
```

### Paso 1: Clonar y Configurar
```bash
git clone https://github.com/acidxlemons/TFG-RAG-URJC.git
cd TFG-RAG-URJC

# Copiar y editar configuración
cp .env.example .env
```

### Paso 2: Configurar `.env` (mínimo)
Edita `.env` con estos valores mínimos:
```env
APP_URL=http://localhost:3002
POSTGRES_PASSWORD=tu_password_seguro
MINIO_ROOT_PASSWORD=tu_minio_password
LITELLM_MASTER_KEY=sk-tu-key-aleatoria
GRAFANA_PASSWORD=admin
JWT_SECRET=genera_con_openssl_rand_hex_32
ENCRYPTION_KEY=genera_con_openssl_rand_hex_32
SHAREPOINT_MULTI_SITE=false
```

📖 Ver [ENV_CONFIGURATION.md](ENV_CONFIGURATION.md) para documentación completa.

### Paso 3: Levantar Servicios
```bash
# Levantar todo (primera vez tarda 5-10 min)
docker compose up -d

# Ver estado
docker compose ps

# Ver logs si algo falla
docker compose logs -f
```

### Paso 4: Descargar Modelo LLM
```bash
# Descargar modelo principal JARVIS (Qwen 2.5 14B fine-tuned, requiere ~8GB)
docker compose exec ollama ollama pull rag-qwen-ft:latest
# Fallback (opcional, requiere ~19GB VRAM)
# docker compose exec ollama ollama pull qwen2.5:32b-instruct-q4_K_M

# Verificar que se descargó
docker compose exec ollama ollama list
```

### Paso 5: Verificar Instalación
```bash
# Health check del backend
curl http://localhost:8002/health

# Health check de Qdrant
curl http://localhost:6335/health

# Abrir interfaz
start http://localhost:3002   # Windows
open http://localhost:3002    # macOS
xdg-open http://localhost:3002  # Linux
```

### ✅ Checklist Rápido
- [ ] `docker compose ps` muestra todos los servicios "Up"
- [ ] `http://localhost:3002` carga la interfaz de chat
- [ ] `http://localhost:8002/health` devuelve `{"status": "healthy"}`
- [ ] `http://localhost:3003` carga Grafana (user: admin)
- [ ] El modelo responde al escribir en el chat

---

## ⚙️ Configuración Opcional

### Habilitar Azure SSO
📖 Ver [SHAREPOINT_INTEGRATION.md](SHAREPOINT_INTEGRATION.md#4-sso-con-azure-ad-en-openwebui)

### Habilitar Sincronización SharePoint
📖 Ver [SHAREPOINT_INTEGRATION.md](SHAREPOINT_INTEGRATION.md)

### Habilitar MCP Server BOE
```bash
# Ya está incluido, solo verificar
curl http://localhost:8011/health

# Probar herramientas MCP
cd mcp-boe-server
python test_mcp_real.py
```
📖 Ver [ADVANCED_EXTENSIONS.md](ADVANCED_EXTENSIONS.md)

---

## 🏭 Despliegue Empresarial (Fases)

Para despliegues en producción con rollout gradual, sigue las fases siguientes.

---

## 🎯 Definición de Métricas Clave

### Errores Críticos
Un **error crítico** es cualquier fallo que:
- Impide completamente usar la funcionalidad (chat/OCR/RAG no responde)
- Causa pérdida de datos o información incorrecta del usuario
- Afecta a la seguridad (exposición de datos, bypass de permisos)
- Provoca caída del servicio (contenedores reiniciándose, OOM)
- Genera respuestas completamente erróneas o sin sentido

### Errores No Críticos
- Lentitud ocasional
- Problemas de formato en respuestas
- Errores de UI menores
- Timeouts recuperables

### Tiempos de Respuesta
| Funcionalidad | Objetivo | Aceptable | Crítico |
|---------------|----------|-----------|---------|
| Chat simple | < 2s | 2-4s | > 5s |
| OCR imagen | < 5s | 5-10s | > 15s |
| RAG consulta | < 3s | 3-6s | > 10s |
| Búsqueda web | < 5s | 5-10s | > 15s |

---

## ✅ Pre-Despliegue General

### Infraestructura
- [ ] Docker y contenedores funcionando correctamente
- [ ] GPU detectada y funcionando (`nvidia-smi` muestra RTX 5090)
- [ ] Grafana accesible en http://localhost:3003
- [ ] Prometheus recolectando métricas en http://localhost:9091
- [ ] OpenWebUI accesible en http://localhost:3002
- [ ] SSO/Azure AD configurado y probado
- [ ] Certificados SSL válidos (si aplica)

### Servicios Core
- [ ] Ollama respondiendo con modelo cargado
- [ ] LiteLLM proxy funcionando
- [ ] Qdrant con colecciones creadas
- [ ] PostgreSQL accesible

### Copias de Seguridad
- [ ] Backup de base de datos PostgreSQL
- [ ] Backup de colecciones Qdrant
- [ ] Backup de configuración Docker Compose
- [ ] Documentado proceso de rollback

---

## 📍 FASE 1: Piloto Limitado (2 semanas)

### Funcionalidades Habilitadas
- ✅ Chat conversacional
- ✅ OCR (procesamiento de imágenes)
- ❌ RAG (deshabilitado)
- ❌ Búsqueda web (deshabilitado)

### Checklist Pre-Fase
- [ ] Grupo piloto definido (lista de usuarios)
- [ ] Usuarios creados en sistema
- [ ] Permisos RAG/Web DESHABILITADOS en OpenWebUI
- [ ] Formulario de feedback distribuido a usuarios
- [ ] Canal de comunicación para incidencias establecido

### Criterios de Éxito (para pasar a Fase 2)
| Métrica | Objetivo | Mínimo Aceptable |
|---------|----------|------------------|
| Errores críticos totales | 0 | ≤ 2 (resueltos) |
| Tiempo respuesta chat | < 2s promedio | < 4s |
| Disponibilidad servicio | > 99% | > 95% |
| Satisfacción usuarios | > 80% | > 70% |

### Checklist Monitorización Diaria
- [ ] Revisar logs de errores en Grafana
- [ ] Verificar uso de GPU/CPU/RAM
- [ ] Revisar feedback de usuarios recibido
- [ ] Documentar incidencias en registro

### Checklist Fin de Fase
- [ ] Todas las incidencias críticas resueltas
- [ ] Informe de métricas de la fase
- [ ] Decisión Go/No-Go documentada

---

## 📍 FASE 2: Expansión Controlada (3 semanas)

### Funcionalidades Habilitadas
- ✅ Chat conversacional
- ✅ OCR
- ✅ RAG (por departamentos: Calidad, DeptA, etc.)
- ✅ Búsqueda web

### Checklist Pre-Fase
- [ ] 1 usuario por departamento seleccionado
- [ ] Colecciones RAG por departamento verificadas en Qdrant
- [ ] Permisos de grupo Azure AD → Colecciones RAG configurados
- [ ] SharePoint sync funcionando para todas las colecciones
- [ ] Búsqueda web habilitada para usuarios piloto

### Criterios de Éxito
| Métrica | Objetivo | Mínimo Aceptable |
|---------|----------|------------------|
| Errores críticos totales | 0 | ≤ 3 (resueltos) |
| Tiempo respuesta RAG | < 3s promedio | < 6s |
| RAG relevancia subjetiva* | > 80% | > 70% |
| Búsqueda web funcional | 100% | > 90% |

*Medido por feedback: "¿La respuesta fue útil y relevante?"

### Checklist Monitorización
- [ ] Métricas RAG en Grafana (filename_detection, listing_usage)
- [ ] Logs de sincronización SharePoint
- [ ] Uso de memoria Qdrant
- [ ] Feedback por departamento

### Checklist Fin de Fase
- [ ] Lista consolidada de mejoras técnicas necesarias
- [ ] Priorización de issues para Fase 3
- [ ] Recursos necesarios para mejoras identificados

---

## 📍 FASE 3: Mejoras Técnicas (1 semana máx)

### Checklist Pre-Fase
- [ ] Comunicación a usuarios sobre corte de servicio
- [ ] Lista priorizada de mejoras a implementar
- [ ] Plan de trabajo diario establecido

### Tipos de Mejoras Esperadas
- [ ] Optimización de prompts del sistema
- [ ] Ajustes de chunking/embedding si necesario
- [ ] Corrección de bugs reportados
- [ ] Mejoras de rendimiento
- [ ] Actualización de documentación

### Checklist Fin de Fase
- [ ] Todas las mejoras críticas implementadas
- [ ] Tests de regresión pasados
- [ ] Documentación actualizada

---

## 📍 FASE 4: Despliegue Parcial (2 semanas)

### Checklist Pre-Fase
- [ ] 50% de equipos identificados para despliegue
- [ ] Plan de escalado de recursos si necesario
- [ ] Alertas de carga configuradas en Grafana

### Criterios de Éxito
| Métrica | Objetivo | Crítico |
|---------|----------|---------|
| Uso GPU | < 80% sostenido | > 95% = escalar |
| Uso RAM | < 80% | > 90% = investigar |
| Tiempo respuesta | < 3s | > 8s = problema |
| Errores por hora | < 5 | > 20 = rollback |

### Señales de Rollback
Si ocurre cualquiera de estos, volver a Fase 3:
- [ ] Caída de servicio > 5 minutos
- [ ] Más de 10 errores críticos en un día
- [ ] Degradación sostenida de rendimiento > 50%
- [ ] Problemas de seguridad detectados

---

## 📍 FASE 5: Despliegue Total

### Checklist Pre-Fase
- [ ] Fase 4 completada sin issues mayores
- [ ] Capacidad de infraestructura verificada para carga completa
- [ ] Plan de soporte post-despliegue establecido
- [ ] Documentación de usuario final lista

### Post-Despliegue
- [ ] Monitorización intensiva primeras 48h
- [ ] Canal de soporte activo
- [ ] Proceso de escalado de incidencias definido

---

## 📞 Contactos y Escalado

| Nivel | Responsable | Contacto | Respuesta |
|-------|-------------|----------|-----------|
| L1 - Usuario | [TBD] | [email] | Mismo día |
| L2 - Técnico | [TBD] | [email] | 4 horas |
| L3 - Crítico | [TBD] | [teléfono] | 1 hora |

---

## 📝 Registro de Decisiones

| Fecha | Fase | Decisión | Justificación |
|-------|------|----------|---------------|
| | | | |

---

*Documento generado: 2026-01-13*
*Última actualización: [fecha]*
