# Home & Project Initialization

<details>
<summary>Relevant source files</summary>

The following files were used as context for generating this wiki page:

- [frontend/index.html](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/index.html)
- [frontend/package-lock.json](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/package-lock.json)
- [frontend/package.json](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/package.json)
- [frontend/src/App.vue](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/App.vue)
- [frontend/src/assets/logo/MiroFish_logo_left.jpeg](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/assets/logo/MiroFish_logo_left.jpeg)
- [frontend/src/store/pendingUpload.js](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/store/pendingUpload.js)
- [frontend/src/views/Home.vue](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Home.vue)
- [frontend/src/views/Process.vue](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Process.vue)

</details>



The **Home & Project Initialization** phase serves as the entry point for the MiroFish simulation lifecycle. It facilitates the ingestion of "reality seeds" (unstructured documents) and the definition of simulation requirements. This stage focuses on data staging and navigation preparation rather than immediate processing, ensuring a smooth transition to the heavy computational tasks of graph construction.

## Home.vue Landing Page

The landing page provides a dual-purpose interface: a marketing "Hero" section [frontend/src/views/Home.vue:15-49](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Home.vue#L15-L49) and a functional "Interaction Console" [frontend/src/views/Home.vue:122-195](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Home.vue#L122-L195). The console is where users initialize new simulation projects by providing the necessary context and data.

### Data Ingestion (Reality Seeds)
The system supports multiple file formats for ground-truth data, referred to in the UI as "Reality Seeds" [frontend/src/views/Home.vue:127-128](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Home.vue#L127-L128).
*   **Supported Formats**: PDF, Markdown (MD), and Plain Text (TXT) [frontend/src/views/Home.vue:143](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Home.vue#L143).
*   **Interaction**: Users can drag-and-drop files into the `upload-zone` or use a standard file browser via `triggerFileInput` [frontend/src/views/Home.vue:131-144](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Home.vue#L131-L144).
*   **State Management**: Selected files are stored in a local `files` array within the component before being committed to the global store [frontend/src/views/Home.vue:155-162](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Home.vue#L155-L162).

### Simulation Requirements
Users define the objective of the simulation through a natural language prompt in the `code-input` textarea [frontend/src/views/Home.vue:176-182](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Home.vue#L176-L182). This requirement serves as the guiding context for the `ReportAgent` and the `SimulationConfigGenerator` in later stages.

**Project Initialization Data Flow**
The following diagram illustrates how user input is captured and transitioned from the Home view to the staging area.

| Diagram: Home Initialization Flow |
| :--- |
| ```mermaid
graph TD
    subgraph "Home.vue (View Layer)"
        A["handleFileSelect()"] -->|File Objects| B["files Array"]
        C["v-model"] -->|String| D["formData.simulationRequirement"]
        E["startSimulation()"] -->|Triggers| F["setPendingUpload()"]
    end

    subgraph "pendingUpload.js (Store Layer)"
        F --> G["state.files"]
        F --> H["state.simulationRequirement"]
        F --> I["state.isPending = true"]
    end

    subgraph "Navigation"
        I --> J["router.push('/process')"]
    end
``` |
Sources: [frontend/src/views/Home.vue:144-191](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Home.vue#L144-L191), [frontend/src/store/pendingUpload.js:7-17](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/store/pendingUpload.js#L7-L17)

## Staging & Store Management

MiroFish uses a specialized store, `pendingUpload.js`, to stage data during the transition from the Home view to the Process view. This prevents data loss during route changes and allows the Process view to handle the actual multi-part API requests (file uploads followed by project creation).

### pendingUpload Store
The store is implemented using Vue 3's `reactive` state [frontend/src/store/pendingUpload.js:7-11](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/store/pendingUpload.js#L7-L11).

| Function | Role | File Reference |
| :--- | :--- | :--- |
| `setPendingUpload` | Stashes files and text requirements into the reactive state. | [frontend/src/store/pendingUpload.js:13-17](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/store/pendingUpload.js#L13-L17) |
| `getPendingUpload` | Retrieves the current staged data for the Process view. | [frontend/src/store/pendingUpload.js:19-25](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/store/pendingUpload.js#L19-L25) |
| `clearPendingUpload` | Resets the state after successful project initialization. | [frontend/src/store/pendingUpload.js:27-31](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/store/pendingUpload.js#L27-L31) |

Sources: [frontend/src/store/pendingUpload.js:1-34](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/store/pendingUpload.js#L1-L34)

## Project Initialization Logic

When the "Start Engine" button (`startSimulation`) is clicked in `Home.vue`, the system performs validation before navigating [frontend/src/views/Home.vue:189-193](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Home.vue#L189-L193).

1.  **Validation**: The `canSubmit` computed property ensures that at least one file is selected and a simulation requirement is provided [frontend/src/views/Home.vue:192](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Home.vue#L192).
2.  **Staging**: The `setPendingUpload` function is called to move data from the component's local state to the `pendingUpload` store [frontend/src/store/pendingUpload.js:13-17](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/store/pendingUpload.js#L13-L17).
3.  **Navigation**: The application uses `vue-router` to navigate to the `/process` route [frontend/src/App.vue:1-3](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/App.vue#L1-L3), where the `Process.vue` component will take over to initiate the backend API calls [frontend/src/views/Process.vue:1-22](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Process.vue#L1-L22).

**Code Entity Association: Home to Store**
This diagram bridges the UI event handlers to the underlying state management entities.

| Diagram: Code Entity Association |
| :--- |
| ```mermaid
graph LR
    subgraph "frontend/src/views/Home.vue"
        UI_BTN["startSimulation()"]
        UI_FILES["files: File[]"]
        UI_REQ["formData.simulationRequirement"]
    end

    subgraph "frontend/src/store/pendingUpload.js"
        STORE_STATE["state: reactive"]
        SET_FUNC["setPendingUpload(files, req)"]
    end

    UI_BTN --> SET_FUNC
    UI_FILES -.->|passed to| SET_FUNC
    UI_REQ -.->|passed to| SET_FUNC
    SET_FUNC -->|updates| STORE_STATE
``` |
Sources: [frontend/src/views/Home.vue:189-191](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Home.vue#L189-L191), [frontend/src/store/pendingUpload.js:5-17](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/store/pendingUpload.js#L5-L17), [frontend/src/views/Process.vue:1-22](https://github.com/666ghj/MiroFish/blob/1536a793/frontend/src/views/Process.vue#L1-L22)
