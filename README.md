# AI Agent Collaboration Workspace

AI Agent Collaboration Workspace is a configurable, general-purpose product for building and running teams of AI agents. Users can define each agent's role and skills, design dependency-aware task workflows, set acceptance criteria, and run missions with text, images, and document attachments.

## Core Capabilities

- **Configurable AI agent teams:** Add, remove, and customize agents without being locked into a predefined use case.
- **Dependency-aware orchestration:** Define task owners, dependencies, context scope, working methods, output modalities, and acceptance criteria.
- **Autopilot and Expert Mode:** Run an entire workflow automatically or execute, edit, and review individual tasks manually.
- **Manager review loops:** A designated workflow manager evaluates each output and can trigger automatic revisions with actionable feedback.
- **Multimodal context:** Provide text, images, PDFs, Word documents, spreadsheets, CSV files, and other attachments as mission context.
- **Image generation:** Generate image attachments through supported image models or the configured text-to-image service.
- **Web research:** Enable selected tasks to retrieve and cite current web sources through Tavily, AnySearch, Brave Search, or Serper.
- **Persistent delivery history:** Store mission input, outputs, logs, review records, and completed deliveries in SQLite.
- **Session isolation:** Each browser session uses a separate SQLite store.

## Project Structure

```text
.
├── app/
│   ├── main.py                 # FastAPI application and routes
│   ├── sqlite_store.py         # Session-scoped SQLite runtime state
│   ├── templates/
│   │   ├── app.html            # Main application UI
│   │   └── partials/results.html
│   └── static/app.css
├── studio/
│   ├── generic_workflow.py     # Agents, tasks, dependencies, and content packages
│   ├── generic_engine.py       # Execution, review loops, images, and delivery
│   ├── file_extractor.py       # Uploaded document extraction
│   ├── llm_service.py          # Demo and live model integrations
│   ├── retry.py                # Cancellable API retries and recovery
│   └── web_search.py           # Web research provider integrations
├── tests/                      # Unit and workflow regression tests
├── requirements.txt
├── Procfile
├── railway.json
└── render.yaml
```

## Local Setup

### Option 1: Direct Uvicorn Startup

```bash
python3 -m pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open:

```text
http://127.0.0.1:8000
```

### Option 2: Deployment Helper

```bash
python3 deploy.py setup
python3 deploy.py start --open
```

Useful commands:

```bash
python3 deploy.py status
python3 deploy.py logs -f
python3 deploy.py restart --open
python3 deploy.py stop
```

## How to Use the Product

1. Configure an AI provider, API key, and model in the left sidebar. Select **None** to run the offline Demo model.
2. Open **Company Setup** to define agents, tasks, owners, dependencies, context scope, working methods, and acceptance criteria.
3. Return to **Agent Office**, enter a mission brief, and upload any reference images or documents.
4. Select **Autopilot** to run the full workflow, or use **Expert Mode** for task-by-task control.
5. Review task outputs and manager feedback under **Task Outputs**.
6. Download completed work from **Delivery History**.

## Supported Model Providers

- OpenRouter
- Google Gemini
- OpenAI
- Anthropic
- Built-in offline Demo mode

Live model and multimodal behavior depends on the capabilities of the selected provider and model.

## Web Research

Tasks with **Web Research** enabled require a configured search provider and API key. Supported providers:

- Tavily
- AnySearch
- Brave Search
- Serper

You can enter the key in the sidebar or provide it through `SEARCH_API_KEY` and provider-specific environment variables.

## Deployment

Railway and Render use:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

For persistent SQLite storage, attach a volume and set:

```text
GENERIC_AGENT_RUNTIME_DIR=/data
```

The application also recognizes `RAILWAY_VOLUME_MOUNT_PATH`.

## Environment Variables

| Variable | Purpose |
| --- | --- |
| `GENERIC_AGENT_RUNTIME_DIR` | Directory for session SQLite databases |
| `GENERIC_AGENT_DB` | Explicit SQLite database path |
| `SEARCH_PROVIDER` | Default web research provider |
| `SEARCH_API_KEY` | Generic web research API key |
| `TAVILY_API_KEY` | Tavily API key |
| `ANYSEARCH_API_KEY` | AnySearch API key |
| `BRAVE_SEARCH_API_KEY` | Brave Search API key |
| `SERPER_API_KEY` | Serper API key |

## Security and Data Handling

- No API keys are bundled with the application.
- API keys are kept in process memory and are removed before state is written to SQLite.
- Uploaded attachments are stored as base64-encoded data in the session database.
- The default attachment limit is 8 MB per file and 12 attachments per content package.
- Web content is treated as untrusted external input, and research prompts explicitly prohibit following instructions embedded in source snippets.
- Download filenames are sanitized before being returned to the browser.

## Testing

```bash
python3 -m unittest discover -s tests -v
```
