# ⚙️ Configuración de Variables de Entorno

**Proyecto**: TFG - Universidad Rey Juan Carlos  
**Propósito**: Documentación completa de todas las variables en `.env`

---

## 🚀 Inicio Rápido

```bash
# 1. Copiar plantilla
cp .env.example .env

# 2. Editar con tus valores
nano .env  # o tu editor preferido

# 3. Reiniciar servicios para aplicar cambios
docker compose down && docker compose up -d
```

---

## 📋 Referencia de Variables

### 🏠 Aplicación General

| Variable | Descripción | Requerido | Ejemplo |
|----------|-------------|-----------|---------|
| `APP_NAME` | Nombre del sistema | No | `Enterprise RAG System` |
| `APP_URL` | URL base de la aplicación | **Sí** | `https://mi-servidor.com` |
| `ENVIRONMENT` | Entorno de ejecución | No | `local`, `production` |

---

### 🔐 Azure AD (SSO + SharePoint)

> ⚠️ **Solo necesario si usas SSO corporativo o sincronización con SharePoint.**

| Variable | Descripción | Requerido | Cómo obtenerlo |
|----------|-------------|-----------|----------------|
| `AZURE_TENANT_ID` | ID del tenant de Azure | **Sí*** | Azure Portal → Azure AD → Overview |
| `AZURE_CLIENT_ID` | ID de la App Registration | **Sí*** | Azure Portal → App Registrations |
| `AZURE_CLIENT_SECRET` | Secret de la App | **Sí*** | Azure Portal → App → Certificates & Secrets |

*Solo requerido si `SHAREPOINT_MULTI_SITE=true` o usas SSO.

**Permisos de Graph API necesarios**:
- `User.Read` (Delegated)
- `Sites.Read.All` (Application) - Para SharePoint
- `GroupMember.Read.All` (Application) - Para permisos por grupo

---

### ☁️ SharePoint

| Variable | Descripción | Requerido | Ejemplo |
|----------|-------------|-----------|---------|
| `SHAREPOINT_MULTI_SITE` | Habilitar multi-sitio | No | `true` / `false` |
| `SHAREPOINT_SITE_ID` | ID del sitio SharePoint | No* | `tenant.sharepoint.com,abc123...` |
| `SHAREPOINT_FOLDER_PATH` | Carpeta a sincronizar | No | `Shared Documents/RAG` |
| `WEBHOOK_SECRET` | Secret para webhooks | **Sí** | Genera uno aleatorio |

*Usar `config/sharepoint_sites.json` para múltiples sitios.

---

### 🗄️ Base de Datos (PostgreSQL)

| Variable | Descripción | Requerido | Por defecto |
|----------|-------------|-----------|-------------|
| `POSTGRES_DB` | Nombre de la BD | No | `rag_system` |
| `POSTGRES_USER` | Usuario | No | `rag_user` |
| `POSTGRES_PASSWORD` | Contraseña | **Sí** | - |

> 🔒 **Seguridad**: Usa una contraseña fuerte de al menos 16 caracteres.

---

### 📦 Almacenamiento (MinIO)

| Variable | Descripción | Requerido | Por defecto |
|----------|-------------|-----------|-------------|
| `MINIO_ROOT_USER` | Usuario admin | No | `minio_admin` |
| `MINIO_ROOT_PASSWORD` | Contraseña admin | **Sí** | - |
| `MINIO_BUCKET` | Bucket para documentos | No | `documents` |

---

### 🤖 LiteLLM (Proxy de LLMs)

| Variable | Descripción | Requerido | Ejemplo |
|----------|-------------|-----------|---------|
| `LITELLM_MASTER_KEY` | API Key maestra | **Sí** | `sk-your-random-key` |

> 💡 OpenWebUI usa esta key para autenticarse con LiteLLM.

---

### 🔍 OCR y Procesamiento

| Variable | Descripción | Requerido | Por defecto |
|----------|-------------|-----------|-------------|
| `OCR_NUM_WORKERS` | Workers paralelos para OCR | No | `6` |
| `OCR_USE_GPU` | Usar GPU para OCR | No | `true` |
| `EMBEDDING_MODEL` | Ruta al modelo de embeddings | No | `/workspace/models/finetuned_embeddings` |

> ⚡ **Rendimiento**: Con GPU NVIDIA, `OCR_USE_GPU=true` es 10x más rápido.

---

### 📊 Monitoreo

| Variable | Descripción | Requerido | Por defecto |
|----------|-------------|-----------|-------------|
| `GRAFANA_PASSWORD` | Contraseña de Grafana | **Sí** | `admin` (cambiar!) |
| `LOG_FORMAT` | Formato de logs | No | `json` |
| `LOG_LEVEL` | Nivel de logging | No | `INFO` |

---

### 🔒 Seguridad

| Variable | Descripción | Requerido | Cómo generar |
|----------|-------------|-----------|--------------|
| `JWT_SECRET` | Secret para tokens JWT | **Sí** | `openssl rand -hex 32` |
| `ENCRYPTION_KEY` | Clave de encriptación | **Sí** | `openssl rand -hex 32` |

---

### 💾 Backups

| Variable | Descripción | Requerido | Ejemplo |
|----------|-------------|-----------|---------|
| `BACKUP_SCHEDULE` | Cron para backups | No | `0 2 * * *` (2am diario) |
| `BACKUP_RETENTION_DAYS` | Días de retención | No | `30` |
| `S3_BACKUP_BUCKET` | Bucket para backups | No | `rag-backups` |

---

### 🏢 Multi-Tenant

| Variable | Descripción | Requerido | Por defecto |
|----------|-------------|-----------|-------------|
| `DEFAULT_TENANT_ID` | Tenant por defecto | No | `tenant-default` |

> 📖 Ver [SHAREPOINT_INTEGRATION.md](SHAREPOINT_INTEGRATION.md) para configuración multi-tenant avanzada.

---

## 🔧 Configuraciones por Entorno

### Desarrollo Local (Mínimo)

```env
# .env para desarrollo
APP_URL=http://localhost:3002
ENVIRONMENT=local

# Bases de datos (usar valores simples)
POSTGRES_PASSWORD=dev_password_123
MINIO_ROOT_PASSWORD=minio_dev_123
GRAFANA_PASSWORD=admin

# LiteLLM
LITELLM_MASTER_KEY=sk-dev-key-12345

# Seguridad (generar en producción)
JWT_SECRET=dev_jwt_secret_not_for_production
ENCRYPTION_KEY=dev_encryption_key_not_for_prod

# Sin Azure (desarrollo offline)
SHAREPOINT_MULTI_SITE=false
```

### Producción (Completo)

```env
# .env para producción
APP_URL=https://jarvis.miempresa.com
ENVIRONMENT=production

# Azure (SSO + SharePoint)
AZURE_TENANT_ID=XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
AZURE_CLIENT_ID=YYYYYYYY-YYYY-YYYY-YYYY-YYYYYYYYYYYY
AZURE_CLIENT_SECRET=super_secret_value
SHAREPOINT_MULTI_SITE=true

# Bases de datos (contraseñas fuertes)
POSTGRES_PASSWORD=Pr0d_P@ssw0rd_Str0ng!
MINIO_ROOT_PASSWORD=M1n10_Pr0d_Secur3!
GRAFANA_PASSWORD=Gr@f@n@_Adm1n_2024!

# LiteLLM
LITELLM_MASTER_KEY=sk-prod-$(openssl rand -hex 16)

# Seguridad (SIEMPRE generar nuevos)
JWT_SECRET=$(openssl rand -hex 32)
ENCRYPTION_KEY=$(openssl rand -hex 32)
WEBHOOK_SECRET=$(openssl rand -hex 16)

# Monitoreo
LOG_FORMAT=json
LOG_LEVEL=WARNING
```

---

## ❓ Troubleshooting

### "Connection refused" a PostgreSQL
```bash
# Verificar que el contenedor está corriendo
docker compose ps postgres

# Ver logs
docker compose logs postgres
```
**Causa común**: `POSTGRES_PASSWORD` vacío o cambiado después de crear el volumen.

### OpenWebUI no conecta con LiteLLM
```bash
# Verificar que LiteLLM está healthy
curl http://localhost:4001/health
```
**Causa común**: `LITELLM_MASTER_KEY` no coincide entre `.env` y la config de LiteLLM.

### SharePoint no sincroniza
```bash
# Ver logs del indexer
docker compose logs indexer -f
```
**Causas comunes**:
- `AZURE_CLIENT_SECRET` expirado.
- App Registration sin permisos `Sites.Read.All`.
- `SHAREPOINT_SITE_ID` incorrecto.

---

## 🔐 Buenas Prácticas de Seguridad

1. **NUNCA** subir `.env` a Git (ya está en `.gitignore`).
2. **Rotar** contraseñas cada 90 días en producción.
3. **Usar** secretos de Azure Key Vault o AWS Secrets Manager en entornos cloud.
4. **Limitar** permisos de Graph API al mínimo necesario.
5. **Auditar** accesos regularmente en Azure Portal.

---

*Documento generado para TFG - Universidad Rey Juan Carlos*
