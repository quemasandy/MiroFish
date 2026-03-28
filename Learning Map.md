# 🗺️ Learning Map: Arquitectura y Tecnología de MiroFish

Esta es tu hoja de ruta ("Learning Map") personalizada, diseñada a partir del análisis profundo de la arquitectura, patrones de diseño y stack tecnológico que componen este proyecto. Para poder dominar MiroFish y construir sistemas similares de Inteligencia Artificial (IA) y Grafos en el futuro, los conceptos están priorizados de más a menos importante.

---

## 🔴 Prioridad Alta: Los Cimientos y el Core del Negocio

Para entender **por qué** y **cómo** está construido el proyecto a nivel fundamental.

### 1. Arquitectura de Software y Patrones de Diseño
*   **Clean Architecture (Arquitectura Limpia):** La forma en la que el proyecto separa las responsabilidades (`api`, `services`, `models`, `utils`). Debes estudiar la **Regla de Dependencia**: cómo las interfaces gráficas y las APIs (Flask/Vue) dependen de los Casos de Uso/Servicios, y estos dependen de las Entidades de Dominio, nunca al revés.
*   **Domain-Driven Design (DDD):** Entender la diferencia entre Entidades (con identidad), Value Objects, Casos de Uso/Servicios de Dominio. Identificar el "Core Business" (que aquí es la construcción del grafo de conocimiento y la simulación).
*   **Inyección y Gestión de Dependencias (Singleton Pattern):** Entender cómo se gestionan ciclos de vida de objetos costosos (como el Driver a la base de datos u objetos LLM) para compartirlos en la aplicación sin consumir demasiados recursos ("Connection Pooling").
*   **Programación Asíncrona y Concurrencia:** Debes dominar técnicas de Multihilo (Multi-threading) y procesos asíncronos. Ejemplo práctico del proyecto: el uso de `ThreadPoolExecutor` para despachar lectura de "chunks" al LLM de forma paralela sin bloquear el servidor.

### 2. Bases de Datos de Grafos (Graph Databases)
*   **Teoría de Grafos:** Nodos (Entidades), Aristas (Relaciones) y Propiedades. Concepto rudimentario de las matemáticas detrás de un grafo dirigido.
*   **Neo4j y Lenguaje Cypher:** Este es tu motor de base de datos.
    *   **Cypher básico:** `MATCH`, `CREATE`, `MERGE` (fundamental para evitar nodos duplicados).
    *   **Cypher avanzado de rendimiento:** Evitar n+1 queries. Uso de `UNWIND` para inserciones en lote (batch processing) pasando un array de objetos JSON de una sola vez hacia la base de datos.
    *   **Indexación:** Cómo crear índices en campos como `UUID` o `name` para que la búsqueda en bases de grafos gigantes sea instantánea.

### 3. IA Generativa y Sistemas Multi-Agente
*   **Prompt Engineering para Estructuración de Datos:** No se trata de "chatear", sino de forzar a un LLM (modelo de lenguaje) a recibir texto libre (ej: chat de WhatsApp) y forzarlo a devolver información estructurada que mapee a reglas de Neo4j.
*   **Integración de APIs de IA (OpenAI SDK / OpenRouter):** Cómo interactuar programáticamente con modelos fundacionales (GPT, Claude, etc.), manejar parámetros como temperatura y control de estructura JSON.
*   **Sistemas de Agentes y Roles (🐪 Camel-AI / OASIS):** Teoría de Agentes de IA autónomos. Cómo simular "personajes" a los que se les dota de memoria, perfiles, reglas del entorno e interacciones entre ellos (simulaciones multi-agente).

---

## 🟡 Prioridad Media: Implementación Backend y Frontend

Las herramientas y ecosistemas concretos elegidos para materializar la arquitectura y conectar la base de datos con el usuario.

### 4. Backend (Ecosistema Python Avanzado)
*   **Manejo de Paquetes con `uv`:** Herramientas modernas y rápidas de gestión de dependencias y entornos virtuales en Python.
*   **Flask & APIs REST:** Cómo construir APIs sin estado, manejar enrutamiento, CORS y gestionar "Long-Running Processes" (peticiones API que tardan minutos en resolverse al procesar texto con IA).
*   **Pydantic:** Validación rigurosa de estructuras de datos. Esencial cuando trabajas con LLMs para asegurar (o intentar asegurar) que el output generado es parseable y no romperá tu base de datos (problemas típicos de serialización/deserialización y ASCII/UTF-8).
*   **Ingeniería de Textos:** Conocimientos en `chardet`, limpieza de texto, tokenización y manejo experto de codificaciones (encodings) por si procesas documentos dispares (PDFs con `PyMuPDF`, .txt rudimentarios, etc.).

### 5. Frontend UI/UX y Visualización
*   **Vue.js 3 (Composition API):** Conocimiento del flujo de estado y reactividad. Manejo en ciclos de vida de componentes (ej: `<Transition>` elements, `ref()`, `reactive()`).
*   **Vite:** El "bundler" principal, compresión y transpilación rápida.
*   **D3.js (Data-Driven Documents):** Este framework es una prioridad si quieres ser dueño total del interfaz. Aprender cómo aplicar físicas (force-directed graphs) para visualizar gráficamente los nodos y conexiones extraídas de forma interactiva y performante en el navegador.
*   **Comunicación Cliente-Servidor Constante:** Técnicas de "Polling" (JavaScript `setInterval`) vs "WebSockets" para comunicar de manera eficiente progreso y actualizaciones de fondo sin asfixiar la CPU del servidor.

---

## 🟢 Prioridad Baja: Infraestructura y Entreplanta

El empaquetado y la forma en la que se despliega la app.

### 6. DevOps y Herramientas Especiales
*   **Docker y Docker Compose:** Containerizar servicios (la base de datos de Neo4j ya usa un contenedor local). Conceptos de volúmenes persistentes (`neo4j_data`) para que la base de datos no se reinicie a 0. Healthchecks.
*   **Gestión de Variables de Entorno (`.env` y `dotenv`):** Patrones de seguridad 101. Separar lógica de código vs configuración de entorno y APIs externas secretas.
*   **Concurrencia de Scripts (`concurrently` y Node):** Herramientas para levantar el frontend y backend en un mismo entorno de terminal fluidamente en fase de desarrollo.

---

> **💡 Consejo de tu Mentor:** No intentes aprenderte las librerías concretas de memoria (Vue o Flask pueden cambiar o pasar de moda). **Enfócate en los conceptos base de la Prioridad Alta:** *Clean Architecture, Sistemas de Agentes y Teoría de Grafos*. Esos conceptos trascenderán este stack tecnológico y te permitirán construir soluciones equivalentes en Go, Rust, React, o cualquier tecnología que elijas mañana.
