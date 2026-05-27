# 🧠 Guía Maestra de Fine-Tuning: Sistema JARVIS RAG

**Proyecto**: TFG - Universidad Rey Juan Carlos

> **"No estamos enseñando al modelo a hablar (LLM), estamos enseñando al modelo a buscar (Embeddings)."**

Esta documentación detalla qué es el Fine-Tuning, por qué lo hacemos, y cómo funciona nuestro sistema de **Mejora Continua** para hacer que la búsqueda de documentos sea cada vez más inteligente.

---

## 1. ¿Qué es el Fine-Tuning? (Conceptos Básicos)

Imagina un modelo de IA "Base" (como el que usamos al principio, `paraphrase-multilingual-MiniLM`) como un **estudiante universitario brillante**. Sabe leer, entiende muchos idiomas y tiene una cultura general excelente. Sin embargo, si le preguntas sobre protocolos internos de tu organización ("Norma P-040" o "Procedimiento DeptA"), se perderá porque **no conoce la jerga específica de tu organización**.

El **Fine-Tuning** (ajuste fino) es el equivalente a enviar a ese estudiante universitario a un **Máster Especializado en tu organización**. 
- No le enseñamos a leer de nuevo (ya sabe).
- Le enseñamos específicamente **qué significan los términos de tu organización** y cómo se relacionan entre sí.

### Diferencia Clave: LLM vs. Embeddings
En RAG (Retrieval Augmented Generation), hay dos cerebros:
1.  **El Bibliotecario (Embeddings Model):** Su trabajo es **encontrar** el libro correcto cuando haces una pregunta.
2.  **El Narrador (LLM - GPT/Llama):** Su trabajo es **leer** el libro que le da el bibliotecario y redactar una respuesta.

⚠️ **Nosotros hacemos Fine-Tuning al Bibliotecario (Embeddings).**
Si el bibliotecario te trae el libro equivocado, da igual lo bueno que sea el narrador; la respuesta será mala. Por eso optimizamos la búsqueda.

---

## 2. Nuestra Estrategia: Fine-Tuning Incremental

La mayoría de los proyectos hacen fine-tuning una vez y listo. Nosotros hemos implementado un sistema de **Mejora Continua (Incremental)**.

### ¿Qué significa "Incremental"?
Imagina que vas al gimnasio.
- **Entrenamiento tradicional:** Cada vez que vas, empiezas desde cero, olvidando todo lo que progresaste la semana pasada.
- **Entrenamiento Incremental (El nuestro):** Cada sesión construye sobre la musculatura que ya tienes. El modelo de hoy es mejor que el de ayer.

Nuestro script detecta automáticamente si ya existe un "modelo experto" previo y sigue entrenando **sobre esa base**, refinando su conocimiento sin perder lo aprendido.

---

## 3. ¿Cómo funciona técnicamente? (El "Under the Hood")

El proceso consta de 3 fases automáticas:

### Fase 1: Minería de Datos (Generación de El Dataset)
¿Cómo entrenamos al modelo si no tenemos miles de humanos escribiendo preguntas? **Usamos otra IA para crear el examen.**

- **Script:** `scripts/generate_dataset_from_qdrant.py`
- **Funcionamiento:**
    1. El script lee tus documentos reales desde Qdrant (Base de datos vectorial).
    2. Usa un LLM local (Ollama) para leer cada fragmento y le pide: *"Genera una pregunta que un usuario haría para buscar este fragmento específico"*.
    3. **Resultado:** Crea miles de pares **(Pregunta, Documento Correcto)**.
    4. Guardamos esto en `data/finetune_dataset_v3_embeddings.json`.

### Fase 2: El Gimnasio (Entrenamiento)
Aquí es donde el modelo "suda" y aprende.

- **Script:** `scripts/finetune_embeddings_v2.py`
- **Técnica:** Multiple Negatives Ranking Loss (MNR).
- **Funcionamiento:**
    1. El modelo toma una pregunta (ej: "Procedimiento de recepción").
    2. Busca cuál documento cree que es el mejor.
    3. Si acierta el documento correcto (Positive), recibe un "premio" (ajuste matemático positivo).
    4. Si elige otro documento (Hard Negative), recibe un "castigo" y ajusta sus neuronas para no volver a fallar.
    5. Repite esto miles de veces (Epochs) hasta que aprende las relaciones semánticas de tu organización.

### Fase 3: Despliegue y Re-Indexación
Un modelo más listo no sirve de nada si los libros en la biblioteca están ordenados con el criterio antiguo.
1. **Borramos** la base de datos vectorial antigua.
2. **Re-indexamos** todos los documentos pasándolos por el nuevo cerebro del modelo.
3. Ahora, cada documento tiene una "huella digital" (vector) generada por el experto, no por el estudiante generalista.

---

## 4. Guía Paso a Paso para Ejecutarlo

Cualquier ingeniero puede replicar esto siguiendo estos comandos.

### Paso 0: Prerrequisitos
Asegúrate de que Docker está corriendo.
```powershell
docker ps
# Debes ver tfg-backend, tfg-indexer, tfg-qdrant, tfg-ollama...
```

### Paso 1: Generar "El Examen" (Dataset)
Cuantos más datos, mejor. Recomendamos entre 1,000 y 5,000 para resultados profesionales.
*Tiempo estimado: 2-4 horas para 5,000 ejemplos.*

```powershell
# --limit define cuántos ejemplos generar.
python scripts/generate_dataset_from_qdrant.py --limit 5000 --output data/finetune_dataset_new.json
```
*Esto generará `data/finetune_dataset_new_embeddings.json`.*

### Paso 2: Entrenar al Modelo
Ejecutamos el entrenamiento dentro del contenedor Docker (donde están las librerías necesarias).
*Tiempo estimado: 30-45 mins (CPU).*

1. **Copiar** el dataset y el script al contenedor:
```powershell
docker cp data/finetune_dataset_new_embeddings.json tfg-backend:/workspace/
docker cp scripts/finetune_embeddings_v2.py tfg-backend:/workspace/
```

2. **Ejecutar** el entrenamiento:
   - `--epochs 3`: Número de pasadas completas por los datos.
   - `--cpu`: Fuerza uso de CPU (necesario si la GPU no es compatible con la versión de PyTorch actual).
```powershell
docker exec tfg-backend python3 /workspace/finetune_embeddings_v2.py --epochs 3 --dataset /workspace/finetune_dataset_new_embeddings.json --output /workspace/models/finetuned_embeddings --cpu
```

### Paso 3: Desplegar y Re-Indexar
Una vez entrenado, aplicamos los cambios.

1. **Guardar** el modelo en tu máquina local (por seguridad):
```powershell
docker cp tfg-backend:/workspace/models/finetuned_embeddings models/
```

2. **Limpiar** la base de datos antigua (Opcional pero recomendado para limpieza):
```powershell
# Borra las colecciones actuales
Invoke-RestMethod -Method Delete -Uri "http://localhost:6335/collections/documents"
Invoke-RestMethod -Method Delete -Uri "http://localhost:6335/collections/documents_CALIDAD"
Invoke-RestMethod -Method Delete -Uri "http://localhost:6335/collections/documents_deptA"

# Borra los marcadores de sincronización para obligar a re-leer todo
Remove-Item -Force data\watch\.delta_*.txt -ErrorAction SilentlyContinue
Remove-Item -Force data\watch\.synced_* -ErrorAction SilentlyContinue
```

3. **Reiniciar** servicios para aplicar el nuevo modelo:
   Asegúrate de que en `.env` tengas `EMBEDDING_MODEL=/workspace/models/finetuned_embeddings`.
```powershell
docker compose restart rag-backend indexer
```

4. **Forzar Inicio de Indexación**:
   El indexer comenzará a trabajar. Puedes acelerarlo forzando un escaneo:
```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8003/scan"
```

---

## 5. Resumen de Archivos Clave

| Archivo | Función |
|---------|---------|
| `scripts/generate_dataset_from_qdrant.py` | **El Minero**: Extrae conocimiento y crea preguntas. |
| `scripts/finetune_embeddings_v2.py` | **El Entrenador**: Define la lógica de aprendizaje (Loss function, Dataloader). Soporta modo incremental. |
| `models/finetuned_embeddings/` | **El Cerebro**: Carpeta que contiene los pesos neuronales del modelo final (.safetensors, config.json). |
| `.env` | **La Configuración**: Define qué modelo usa el sistema (`EMBEDDING_MODEL`). |

---

## 6. FAQ (Preguntas Frecuentes)

**¿Cuándo debo hacer Fine-Tuning de nuevo?**
- Cuando añadas una gran cantidad de documentación nueva sobre un tema que el modelo no conocía antes (ej: Nuevo proyecto secreto "Proyecto X").
- Si notas que las respuestas empiezan a ser menos precisas.

**¿Puedo perder calidad si entreno demasiado? (Overfitting)**
- Sí. Si entrenas 100 epochs con pocos datos, el modelo "memorizará" las respuestas en lugar de "entenderlas".
- Nuestra configuración de **3 a 5 epochs** es conservadora y segura para evitar esto.

**¿Por qué usamos CPU y no GPU?**
- Actualmente el contenedor usa una versión de PyTorch estable. Si tu hardware (RTX 5090) es muy nuevo, puede requerir versiones 'nightly' de PyTorch. El script tiene un flag `--cpu` para garantizar que funcione siempre, aunque sea más lento. "Lento y seguro" es mejor que "Rápido y roto".

---
**Autor:** Antigravity Agent
**Fecha:** 2026-01-09
