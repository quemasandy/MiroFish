# MiroFish — Roadmap

> Actualizado: 2026-03-28

---

## Bugs corregidos (14 total)

| # | Descripcion | Archivo(s) |
|---|-------------|------------|
| B1 | `logging` usado antes de importarlo — crash de subprocess | `scripts/run_parallel_simulation.py:48` |
| B2 | Reddit initial posts: lista en `env.step()` — crash ronda 0 | `scripts/run_parallel_simulation.py:1534` |
| B3 | SQLite `conn.close()` sin `finally` — leak por ronda (x2) | `scripts/run_parallel_simulation.py:831,680` |
| B4 | Graph builder threads saturan Flask — workers dinamicos + yield | `services/graph_builder.py:265,287` |
| B5 | `state.json` no sincroniza al completar — status "running" forever | `services/simulation_runner.py:545` |
| B6 | File handle leak si `Popen()` falla | `services/simulation_runner.py:469` |
| B7 | `_run_states` dict sin lock — race condition multi-thread | `services/simulation_runner.py:219` |
| B8 | Polling endpoints heredan timeout 5min — ahora 15s | `frontend/src/api/simulation.js` |
| B9 | `bare except:` captura SystemExit | `api/simulation.py:965` |
| B10 | `stopSimulation` sin retry | `frontend/src/api/simulation.js:92` |
| B11 | Windows taskkill sin fallback — zombie processes | `services/simulation_runner.py:748` |
| B12 | Log drain post-exit sin retry — acciones perdidas | `services/simulation_runner.py:518` |
| B13 | IPC JSON parcial — atomic write (tmp+rename) | `simulation_ipc.py:148`, `run_parallel_simulation.py:430` |
| B14 | `_enrich_action_context` silencia errores en DEBUG | `scripts/run_parallel_simulation.py:997` |

Mejoras anteriores: MPS deadlocks, JSON corruption Neo4j, Neo4j Zep warnings, polling 10s→30s, LLM ThreadPool x4, Cypher UNWIND batch.

---

## Pendiente — Arquitectura

| ID | Que | Donde | Prioridad |
|----|-----|-------|-----------|
| P1 | Neo4j driver singleton (evitar pools duplicados) | `graph_builder`, `zep_tools`, `entity_reader`, `graph_memory_updater` → nuevo `db.py` | Media |
| P2 | Indices Neo4j (`graph_id`, `name` en Entity) | Ejecutar en Neo4j o agregar a startup | Media |
| P3 | Graceful shutdown (SIGINT handler para cerrar Neo4j driver) | `run.py` / `package.json` | Baja |
| P4 | CORS restrictivo para produccion (`origins: "*"` → env var) | `app/__init__.py` | Baja |

---

## Backlog — Features

- Busqueda semantica con embeddings (reemplazar keyword search en `zep_tools.py`)
- Entity deduplication post-LLM ("Andy" vs "@Andy")
- Context managers para drivers Neo4j (reemplazar `__del__`)
- Endpoint limpieza de proyectos huerfanos

---

## Limpieza Zep (baja prioridad)

- `ontology_generator.py:364` — string literal `zep_cloud` en codigo generado
- `oasis_profile_generator.py:184` — parametro muerto `zep_api_key`
- Renombrar `zep_*.py` → `graph_*.py` (actualizar imports)
