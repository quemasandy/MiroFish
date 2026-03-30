# Plan: Resume de Simulaciones desde Última Ronda Completada

## Contexto

Cuando una simulación de MiroFish falla (timeout de API LLM, crash del proceso, SIGKILL, etc.), actualmente se pierde todo el progreso y hay que empezar desde cero. Esto es especialmente doloroso en simulaciones largas (30+ rondas, ~25min por ronda). El estado de la base de datos SQLite y los action logs se preservan en disco, pero no hay mecanismo para reanudar desde la última ronda completada.

**Objetivo**: Agregar un parámetro `resume` al flujo de simulación para que, tras un fallo, se pueda continuar desde la última ronda completada sin perder el progreso.

**Trade-off aceptado**: Al reanudar, los agentes pierden su estado in-memory de LLM (embeddings, caché de inferencia). Sin embargo, mantienen todo su contexto via la DB (posts, social graph, historial de acciones). Esto es aceptable: los agentes "olvidan" las últimas interacciones exactas pero conservan todo el mundo simulado.

---

## Archivos a modificar

| Archivo | Cambio |
|---------|--------|
| `backend/scripts/run_parallel_simulation.py` | Core: lógica de resume en ambas plataformas |
| `backend/app/services/simulation_runner.py` | Pasar `--resume` al subprocess, manejar estado |
| `backend/app/api/simulation.py` | Aceptar parámetro `resume` en endpoint |
| `frontend/src/components/Step3Simulation.vue` | Botón de "Reanudar" cuando simulación falló |

---

## Paso 1: `run_parallel_simulation.py` — Lógica core de resume

### 1a. Agregar `--resume` al argparse (~línea 1676)

```python
parser.add_argument(
    '--resume',
    action='store_true',
    default=False,
    help='Reanudar simulación desde la última ronda completada'
)
```

### 1b. Función helper: determinar última ronda completada

Leer `actions.jsonl` y buscar el último evento `round_end` para determinar desde qué ronda reanudar:

```python
def get_last_completed_round(actions_jsonl_path: str) -> int:
    """Lee actions.jsonl y retorna el número de la última ronda completada (con round_end)."""
    last_round = -1  # -1 = ninguna ronda completada
    if not os.path.exists(actions_jsonl_path):
        return last_round
    with open(actions_jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if entry.get("event_type") == "round_end":
                    last_round = max(last_round, entry.get("round", -1))
            except:
                continue
    return last_round
```

### 1c. Función helper: obtener max rowid de la DB

```python
def get_max_rowid_from_db(db_path: str) -> int:
    """Obtiene el máximo rowid de la tabla trace."""
    if not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(rowid) FROM trace")
    result = cursor.fetchone()[0]
    conn.close()
    return result or 0
```

### 1d. Parchear `create_db` de OASIS para resume

**Problema**: `Platform.__init__` llama `create_db(db_path)` que ejecuta `CREATE TABLE` (sin `IF NOT EXISTS`). Con DB existente, esto falla.

**Solución**: Monkey-patch temporal que detecta si las tablas ya existen y no intenta crearlas:

```python
def patch_create_db_for_resume():
    """Parchea create_db de OASIS para que no falle con DB existente."""
    import oasis.social_platform.database as db_module
    original_create_db = db_module.create_db

    def resume_create_db(db_path=None):
        if db_path and os.path.exists(db_path):
            # DB ya existe — solo conectar sin ejecutar CREATE TABLE
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            return conn, cursor
        return original_create_db(db_path)

    db_module.create_db = resume_create_db
    # También parchear en platform.py que ya lo importó
    import oasis.social_platform.platform as platform_module
    platform_module.create_db = resume_create_db
    return original_create_db
```

### 1e. Parchear `env.reset()` para resume

**Problema**: `env.reset()` llama `generate_custom_agents()` que hace `sign_up` para todos los agentes, insertando duplicados en la tabla `user`.

**Solución**: En resume, solo hacer `connect_platform_channel` (ya existe en OASIS: `agents_generator.py:537`) sin `sign_up`:

```python
async def resume_reset(env):
    """Reset sin signup — solo conecta agentes al channel y arranca la plataforma."""
    env.platform_task = asyncio.create_task(env.platform.running())
    from oasis.social_agent.agents_generator import connect_platform_channel
    env.agent_graph = connect_platform_channel(
        channel=env.channel,
        agent_graph=env.agent_graph
    )
```

### 1f. Modificar `run_twitter_simulation()` (~línea 1245)

Agregar parámetro `resume: bool = False`. Cuando `resume=True`:

1. **No borrar DB existente** — saltar las líneas 1296-1297 (`os.remove(db_path)`)
2. **Parchear `create_db`** — llamar `patch_create_db_for_resume()` antes de `oasis.make()`
3. **Usar `resume_reset` en vez de `env.reset()`** — línea 1306
4. **Inicializar `last_rowid` desde DB** — `get_max_rowid_from_db(db_path)` en vez de 0
5. **Saltar initial posts** — no ejecutar el bloque de líneas 1316-1358
6. **Saltar `log_simulation_start()`** — no escribir evento de inicio duplicado
7. **Calcular `start_round`** — usar `get_last_completed_round(actions_jsonl_path)`
8. **Ajustar el loop principal**: `for round_num in range(start_round, total_rounds)` en vez de `range(total_rounds)`
9. **Restaurar `sandbox_clock.time_step`** — setear a `start_round` para que el reloj no empiece desde 0
10. **Log de reanudación** — escribir un evento `simulation_resume` en actions.jsonl

### 1g. Modificar `run_reddit_simulation()` (~línea 1443)

Cambios idénticos al de Twitter (es una copia casi exacta).

### 1h. Modificar `main()` (~línea 1652)

- Pasar `args.resume` a ambas funciones de simulación
- En resume, abrir `simulation.log` en modo append (`'a'`) en vez de write (`'w'`)

---

## Paso 2: `simulation_runner.py` — Backend service

### 2a. Agregar `resume` a `start_simulation()` (~línea 318)

Agregar parámetro `resume: bool = False`.

### 2b. Agregar `--resume` al comando subprocess (~línea 421)

```python
if resume:
    cmd.append("--resume")
```

### 2c. No limpiar logs en resume

El flag `resume` implica NO borrar DB ni action logs existentes.

### 2d. Abrir simulation.log en modo append cuando resume

Línea 433: cambiar `'w'` a `'a'` cuando `resume=True`:
```python
main_log_file = open(main_log_path, 'a' if resume else 'w', encoding='utf-8')
```

### 2e. Inicializar posiciones del monitor desde tamaño actual de archivos

En `_monitor_simulation` (línea 501), cuando es resume, `twitter_position` y `reddit_position` deben empezar en el final actual de los archivos, no en 0. Para comunicar esto, guardar el tamaño actual del archivo en `run_state` antes de arrancar el subprocess.

Agregar campos `twitter_log_position` y `reddit_log_position` a `SimulationRunState`. En `start_simulation` cuando `resume=True`, setear estos al tamaño actual del archivo. En `_monitor_simulation`, leer estos valores como posiciones iniciales.

### 2f. Preservar contadores de acciones en resume

Cuando `resume=True`, cargar el `run_state` existente y actualizar en vez de crear uno nuevo. Preservar `twitter_actions_count`, `reddit_actions_count`, `current_round`, etc.

---

## Paso 3: `simulation.py` — API endpoint

### 3a. Aceptar `resume` en el request (~línea 1500)

```python
resume = data.get('resume', False)
```

### 3b. Lógica de estado para resume (~línea 1536)

Cuando `resume=True`:
- NO ejecutar `cleanup_simulation_logs` (no borrar nada)
- Permitir resume desde estados `FAILED`, `COMPLETED`, `STOPPED`
- Verificar que existen DBs previas (si no existen, es un error — no hay nada que reanudar)
- NO usar `force=True` internamente

### 3c. Pasar `resume` a `SimulationRunner.start_simulation()` (~línea 1599)

```python
run_state = SimulationRunner.start_simulation(
    simulation_id=simulation_id,
    platform=platform,
    max_rounds=max_rounds,
    resume=resume,
    ...
)
```

### 3d. Response con info de resume

Agregar `"resumed": True` y `"resumed_from_round": N` a la respuesta.

---

## Paso 4: Frontend — Botón de reanudar

### 4a. `Step3Simulation.vue` — Agregar función `doResumeSimulation`

Similar a `doStartSimulation` pero:
- Enviar `resume: true` en vez de `force: true`
- NO llamar `resetAllState()` (preservar estado visual)
- Log: "Reanudando simulación desde ronda N..."

### 4b. Botón de "Reanudar" visible cuando `phase === 'failed'` o status es FAILED/STOPPED

Agregar un botón junto al botón de "Reiniciar" existente:
```html
<button @click="doResumeSimulation" class="action-btn resume">
  Reanudar desde última ronda
</button>
```

---

## Verificación

1. **Test manual — resume tras error forzado**:
   - Iniciar simulación con 10 rondas
   - Esperar a que complete ~5 rondas
   - Matar el proceso (`kill -9 <PID>`)
   - Verificar que `run_state.json` muestra `runner_status: "failed"`
   - Hacer POST `/api/simulation/start` con `resume: true`
   - Verificar que:
     - La simulación arranca desde la ronda ~5 (no desde 0)
     - `actions.jsonl` continúa sin duplicar rondas previas
     - La DB no se borra (datos anteriores preservados)
     - Los contadores en el frontend son correctos

2. **Test — resume sin DB previa**:
   - Intentar resume en una simulación sin DB
   - Debe retornar error claro: "No hay datos previos para reanudar"

3. **Test — OASIS create_db patch**:
   - Verificar que el monkey-patch de `create_db` funciona cuando la DB existe
   - Verificar que una simulación nueva (sin resume) sigue funcionando normal
