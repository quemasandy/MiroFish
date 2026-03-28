# MiroFish — Roadmap de Mejoras

> Generado: 2026-03-28 | Basado en análisis de runtime, logs y código fuente.
> Estado actual: Migración Zep → Neo4j operativa. Grafo en construcción (chunk 1893/2184).

---

## 📊 Métricas Actuales del Sistema

| Métrica | Valor |
|---|---|
| Documento fuente | Chat WhatsApp "Team Quito 👍" (816 KB, 762K caracteres) |
| Chunks totales | 2,184 |
| Entidades extraídas | ~6,400 (estimado final, ~2.95 por chunk) |
| Relaciones extraídas | ~3,500 (estimado final, ~1.6 por chunk) |
| Chunks vacíos (0 entidades) | 26 de 1,489 procesados (1.7%) |
| Velocidad | ~100 chunks cada 3.5 min (~30 chunks/min) |
| Tiempo total estimado | ~73 min para 2,184 chunks |
| Neo4j RAM | 821 MB / 3.8 GB (21% uso) |
| Neo4j CPU | ~41% |
| Neo4j disco I/O (escritura) | 400 MB (acumulado) |

---

## ✅ Completado (Realizado hoy)
- **#0 Deadlocks en Mac:** Variables de entorno `TOKENIZERS_PARALLELISM` y `PYTORCH_ENABLE_MPS_FALLBACK` implementadas para MPS. Bug de estados "paused" arreglado en el API.
- **#1 Corrupción de JSON en Neo4j:** Se reemplazó el casteo estándar de diccionarios por `json.dumps(..., ensure_ascii=False)` en `graph_builder.py` para preservar integridad.
- **#2 Inundación de Warnings en Neo4j:** Se eliminaron las consultas a campos temporales obsoletos herencia de Zep (`valid_at`, `invalid_at`, `expired_at`) limpiando por completo la consola del backend.
- **#3 Polling excesivo de Frontend:** Aumentamos la frecuencia del temporizador de `10000ms` a `30000ms` en los componentes de interfaz para descargar la CPU del servidor durante la consulta `fetchGraphData()`.
- **#5 Multihilo para LLM Extractor:** Sustituimos el procesamiento secuencial por un despachador asíncrono `ThreadPoolExecutor(max_workers=4)`. La aceleración extrajo minutos cruciales al proceso de lectura.
- **#6 Inyección Cypher UNWIND:** El agrupar Nodos y Relaciones por categoría de label y llamar a un `UNWIND` mitigó de 8 viajes a DB por chunk a solo un par de peticiones por bloque de carga.

---

## 🟡 Prioridad Media — Pendientes

### 4. Driver Neo4j: múltiples instancias sin compartir
Cada servicio crea su propio `GraphDatabase.driver()`:
- `GraphBuilderService.__init__`
- `ZepToolsService.__init__`
- `EntityReader.__init__`
- `ZepGraphMemoryUpdater.__init__`

El driver de Neo4j tiene connection pooling interno, pero crear múltiples drivers genera pools separados.

**Mejora propuesta**: Crear un **singleton** `get_neo4j_driver()` en `config.py` o un módulo `db.py`:

```python
# backend/app/db.py
from neo4j import GraphDatabase
from .config import Config

_driver = None

def get_neo4j_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            Config.NEO4J_URI,
            auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD)
        )
    return _driver
```

---

## 🟢 Prioridad Baja — Limpieza Post-Migración

### 6.5. Manejo elegante de apagado de servidor (`npm run dev`)
En `package.json`, `npm run dev` detiene el servidor de manera brusca causando fallos en los logs (`ESRCH: No such process`).
**Mejora propuesta:** Ajustar la configuración de `concurrently` o implementar un manejador de señales (SIGINT/SIGTERM) en la aplicación Flask/uv para asegurar el cierre limpio de recursos y el driver de Neo4j al presionar `Ctrl+C`.

---

### 7. Referencia residual a `zep_cloud` en código generado
[`ontology_generator.py:364`](file:///Users/andy/MiroFish/backend/app/services/ontology_generator.py#L364)

```python
'from zep_cloud.external_clients.ontology import EntityModel, EntityText, EdgeModel',
```

Dentro del método `generate_python_code()`. Es un string literal (no un import real), pero genera código inválido. Reemplazar por imports locales o dataclasses de Pydantic.

### 8. Parámetro muerto `zep_api_key`
[`oasis_profile_generator.py:184`](file:///Users/andy/MiroFish/backend/app/services/oasis_profile_generator.py#L184)

```python
zep_api_key: Optional[str] = None,  # Ya no se usa
```

### 9. Comentarios/docstrings en chino que mencionan "Zep"
Archivos afectados (solo comentarios, no lógica):
- `zep_graph_memory_updater.py` — "更新到Zep图谱中"
- `graph.py` — "创建Zep图谱", "等待Zep处理数据"
- `simulation.py` — "Zep实体读取与过滤"
- `graph_builder.py:3` — "替代Zep Cloud"

### 10. Renombrar archivos con prefijo "zep_"
| Actual | Propuesto |
|---|---|
| `zep_tools.py` | `graph_tools.py` |
| `zep_entity_reader.py` | `entity_reader.py` |
| `zep_graph_memory_updater.py` | `graph_memory_updater.py` |

> Requiere actualizar imports en: `simulation.py`, `graph.py`, `oasis_profile_generator.py`, `report_agent.py`, etc.

### 11. Proyectos huérfanos en disco
Hay **13 proyectos** en `backend/uploads/projects/`, muchos de los cuales son intentos fallidos de la era Zep (los que daban `500` en generación de ontología). Solo `proj_4ca016d49f11` es el activo.

Considerar agregar un **endpoint de limpieza** o un script para eliminar proyectos sin grafo asociado.

---

## 🔵 Mejoras Futuras (Nuevas Features)

### 12. Índices Neo4j para performance
Actualmente no se crean índices explícitos. Con grafos grandes, las queries de búsqueda por `graph_id` y `name` se vuelven lentas.

```cypher
CREATE INDEX entity_graph_id IF NOT EXISTS FOR (n:Entity) ON (n.graph_id);
CREATE INDEX entity_name IF NOT EXISTS FOR (n:Entity) ON (n.name);
CREATE INDEX entity_uuid IF NOT EXISTS FOR (n:Entity) ON (n.uuid);
CREATE INDEX graphmeta_graph_id IF NOT EXISTS FOR (g:GraphMeta) ON (g.graph_id);
```

**Ejecutar en Neo4j Browser** (`http://localhost:7474`) o agregar al startup del servicio.

### 13. Búsqueda semántica con embeddings
La búsqueda actual (`search_graph` en `zep_tools.py`) es por **keywords** (split + scoring). Zep tenía búsqueda semántica.

**Mejora propuesta**:
- Generar embeddings para `summary` de cada nodo y `fact` de cada edge.
- Almacenar embeddings en Neo4j (propiedad vectorial) o en un vector store separado.
- Implementar búsqueda por similitud coseno en las queries.

### 14. Resumir entidades duplicadas
Con ~6,400 entidades extraídas de un chat de WhatsApp, habrá muchas entidades que son la misma persona con diferentes nombres (ej: "Andy", "andy", "@Andy"). El MERGE por `name` ayuda pero no es perfecto.

**Mejora propuesta**: Post-procesamiento con LLM que identifique y fusione entidades similares.

### 15. Mejorar el manejo del `__del__` para cerrar drivers
Los destructores `__del__` no son confiables en Python (pueden no ejecutarse). Mejor usar **context managers** o cierre explícito.

```python
class GraphBuilderService:
    def close(self):
        self.driver.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()
```

### 16. Internacionalización de logs
Los logs mezclan chino (系统内部) y español (contenido del usuario). Considerar:
- Logs internos → inglés (estándar de la industria)
- Mensajes de UI → español (idioma del usuario)

---

## 🗓️ Orden de Ejecución Sugerido

| Fase | Items | Cuándo |
|---|---|---|
| **Inmediato** (cuando termine la carga actual) | #1, #2, #7, #8 | Hoy |
| **Semana 1** | #3, #4, #12 | Próximos días |
| **Semana 2** | #5, #6, #9, #10, #11 | Siguiente iteración |
| **Backlog** | #13, #14, #15, #16 | Cuando el core esté estable |
