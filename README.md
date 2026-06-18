# Coding Assistant Agent

A hosted AI coding assistant built on Microsoft Foundry using the Agent Framework and powered by **DeepSeek-V4-Pro**.

The agent helps developers write, review, debug, and understand code across all programming languages and frameworks. It is exposed via the OpenAI-compatible `/responses` protocol, so it can be consumed from any repo that supports the Responses API.

## Prerequisites

- Python 3.12+
- [Azure Developer CLI (azd)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [Foundry Toolkit VS Code extension](https://marketplace.visualstudio.com/items?itemName=ms-windows-ai-studio.windows-ai-studio) (for Agent Inspector)
- Access to the `et-foundry` Azure AI Foundry project

## Project Structure

```
aiFoundrySetup/
├── main.py                  # Agent entrypoint
├── agent.yaml               # Foundry hosting manifest
├── agent.manifest.yaml      # Agent metadata
├── requirements.txt         # Python dependencies
├── Dockerfile               # Container image
├── .env                     # Local secrets (not committed)
├── .env.example             # Secret template for other repos
└── .vscode/
    ├── launch.json          # VS Code debug configurations
    └── tasks.json           # VS Code tasks (server + inspector)
```

## Local Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy and fill in environment variables
copy .env.example .env
# Edit .env — values are pre-filled for the et-foundry project
```

### Environment Variables

| Variable | Description |
|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | Your Azure AI Foundry project endpoint URL |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME_PRO` | The pro model deployment name (e.g. `DeepSeek-V4-Pro`) |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME_FLASH` | The flash model deployment name (e.g. `DeepSeek-V4-Flash`) |

## Running Locally

```bash
# Run directly
python main.py

# Run with agentdev (enables Agent Inspector in VS Code)
agentdev run main.py --verbose --port 8088 -- --server
```

The agent listens on `http://localhost:8088`.

### Test with curl

```bash
curl -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input": "Write a Python function to reverse a linked list."}'
```

## Debugging in VS Code

1. Press **F5** and select **"Debug Coding Assistant (agentdev + Inspector)"**
2. The Foundry Toolkit Agent Inspector will open automatically
3. Send messages and inspect the full conversation flow, tool calls, and traces

## Using This Agent from Other Repos

This agent speaks the OpenAI-compatible `/responses` protocol. Once deployed to Foundry, other repos can call it via the Foundry-hosted endpoint.

### Python example (using `openai` SDK)

```python
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

token_provider = get_bearer_token_provider(
    DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
)

client = AzureOpenAI(
    azure_endpoint="<your-deployed-agent-endpoint>",
    azure_ad_token_provider=token_provider,
    api_version="2025-01-01-preview",
)

response = client.responses.create(
    model="coding-assistant",
    input="Explain the difference between a mutex and a semaphore.",
)
print(response.output_text)
```

### VS Code Copilot Instructions (`.github/copilot-instructions.md`)

Add this to any repo to use the deployed agent as a custom Copilot context:

```markdown
This repository uses a self-hosted coding assistant agent at:
<your-deployed-agent-endpoint>

When asking coding questions, the agent follows OWASP Top 10 security practices,
writes idiomatic code, and includes inline comments for non-obvious logic.
```

## Deploying to Foundry

```bash
# Deploy with Azure Developer CLI
azd up
```

Or use the **Foundry Toolkit** extension → **Deploy Agent** from the Activity Bar.

## Authentication

The agent uses `DefaultAzureCredential`. For local development, run:

```bash
az login
```

For production, assign the **Azure AI User** role to the agent's managed identity.

Permissions needed
# 1. Allow agent container runtime to call the model (run twice - one per identity)
az role assignment create \
  --assignee "9a574021-0728-4f3c-bf9a-7966b97f1070" \
  --role "Foundry User" \
  --scope "/subscriptions/be5a3beb-686a-4703-aaa8-ceb70667ad77/resourceGroups/ems-rg-e1/providers/Microsoft.CognitiveServices/accounts/et-foundry-e2"

az role assignment create \
  --assignee "462a694e-5c1e-4864-a5e1-cb309f488e54" \
  --role "Foundry User" \
  --scope "/subscriptions/be5a3beb-686a-4703-aaa8-ceb70667ad77/resourceGroups/ems-rg-e1/providers/Microsoft.CognitiveServices/accounts/et-foundry-e2"

# 2. Allow Foundry to pull the container image from ACR
az role assignment create \
  --assignee "59a291d9-4ad5-4fe9-9cae-a530bbbd7a86" \
  --role "AcrPull" \
  --scope "/subscriptions/be5a3beb-686a-4703-aaa8-ceb70667ad77/resourceGroups/ems-rg-e1/providers/Microsoft.ContainerRegistry/registries/etfoundryacr"