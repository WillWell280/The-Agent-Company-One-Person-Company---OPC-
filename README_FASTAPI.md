# AI Agent Collaboration Workspace · FastAPI Edition

This FastAPI application provides a configurable environment for building and running general-purpose AI agent workflows.

## Core Capabilities

- **Company Setup:** Configure the workflow name, description, manager, agent roles, skills, tasks, owners, dependencies, working methods, context scope, and acceptance criteria.
- **Flexible agents and tasks:** Add or remove agents and workflow tasks without a fixed domain template.
- **Multimodal input and output:** Mission input and task outputs support text and attachments. Images are passed to vision-capable models, while document content is extracted into prompt context when supported.
- **Background execution and persistence:** Workflows run in a background thread, while SQLite stores inputs, outputs, logs, reviews, and delivery history.
- **Manager review loop:** The workflow manager reviews every task output and can initiate automatic revisions with specific feedback.
- **Cancellable model requests:** Long-running model and image requests can be stopped without blocking the workflow UI.

## Local Development

```bash
python3 -m pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open:

```text
http://127.0.0.1:8000
```

## Deployment

Railway and Render use:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

For persistent SQLite storage, set:

```text
GENERIC_AGENT_RUNTIME_DIR=/data
```

You can also use the volume path provided by your hosting platform.

## Notes

- API keys are not written to SQLite. They remain in process memory and must be re-entered after a service restart unless supplied through the environment.
- Legacy workspace state is migrated without mixing old task outputs into the current workflow.
- Demo mode can run the complete workflow offline.
- Live multimodal behavior depends on the selected model's vision capabilities.
- See [`README.md`](README.md) for the complete product, deployment, security, and environment-variable documentation.
