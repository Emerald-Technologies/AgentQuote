# BOM Processor Agent

A hosted AI agent for electronic contract manufacturing. Built on Microsoft Foundry using the Agent Framework, powered by **gpt-5.6-luna** (Azure Government deployment).

**Primary objective:** Learn customer assembly BOM formats and develop per-customer parser scripts that normalize BOMs into a standardized quoting format.

The agent is exposed via the OpenAI-compatible `/responses` protocol and runs as an Azure Container App in Azure Government.

## Deployments

| Environment | Hosting | Model | Endpoint |
|---|---|---|---|
| **Azure Gov** (current) | Container App `bom-processor-gov` in `et-bom-ca-env` (usgovvirginia) | `gpt-5.6-luna` (10K TPM) | `https://bom-processor-gov.calmglacier-732dba63.usgovvirginia.azurecontainerapps.us/responses` |
| Commercial (legacy) | Foundry Hosted Agent | `DeepSeek-V4-Flash` | See "Current Deployment Values" in setup.md |

> **Note:** Foundry Hosted Agents are not available in Azure Government regions. The Gov deployment uses Container Apps with the same container image — the agent code is identical.

## Prerequisites

- Python 3.12+
- [Azure Developer CLI (azd)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [Foundry Toolkit VS Code extension](https://marketplace.visualstudio.com/items?itemName=ms-windows-ai-studio.windows-ai-studio) (for Agent Inspector)
- Access to the `AzureGovAgents` Foundry project (Gov) or `CodingAssistant` project (commercial)

## Project Structure

```
agentQuote/
├── main.py                  # Agent entrypoint
├── agent.manifest.yaml      # Agent metadata
├── requirements.txt         # Python dependencies
├── Dockerfile               # Container image
├── README.md                # This file
├── setup.md                 # Deployment walkthrough
├── .env                     # Local secrets (not committed)
├── format/
│   └── finalformat.xlsx     # Official BOM schema (quoting module target)
├── parsers/                 # Per-customer BOM parser scripts
│   └── <customer_name>/
│       └── parse.py
├── boms/                    # Customer sample BOMs for reference
│   └── <customer_name>/
│       └── sample_bom.xlsx
├── dev/
│   └── deploy-agent.ps1     # Deployment automation script
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
| `AZURE_AI_MODEL_DEPLOYMENT_NAME_FLASH` | The model deployment name (e.g. `gpt-5.6-luna` in Gov, `DeepSeek-V4-Flash` in commercial) |
| `BOM_STORAGE_CONNECTION_STRING` | Connection string for the BOM storage account |

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
  -d '{"input": "Analyze the BOM sample at boms/acme/sample.xlsx and create a parser."}'
```

## Debugging in VS Code

1. Press **F5** and select **"Debug Coding Assistant (agentdev + Inspector)"**
2. The Foundry Toolkit Agent Inspector will open automatically
3. Send a BOM file path and ask the agent to analyze it

## Official BOM Format

The quoting module expects BOMs in the format defined by `format/finalformat.xlsx`:

| Column | Type | Description |
|---|---|---|
| `line_number` | int | Sequence number |
| `item_number` | str | Customer's part/item identifier |
| `item_name` | str | Description of the item |
| `revision` | str | Revision letter/number |
| `item_category` | str | Part, Document, Assembly, PCB |
| `unit_of_measure` | str | EA, FT, IN, etc. |
| `quantity` | float | Per-assembly quantity |
| `reference_designator` | str | PCB refdes (e.g. R1,R2,C1-C5) |
| `bom_notes` | str | Production notes, RoHS status |
| `manufacturer_1`..`manufacturer_8` | str | Manufacturer names |
| `manufacturer_item_number_1`..`manufacturer_item_number_8` | str | Manufacturer part numbers |

## Using This Agent from Other Repos

This agent speaks the OpenAI-compatible `/responses` protocol. Once deployed, other repos can call it via the hosted endpoint.

### Python example (using `openai` SDK)

```python
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

# Azure Gov uses the .us audience; commercial uses cognitiveservices.azure.com
token_provider = get_bearer_token_provider(
    DefaultAzureCredential(), "https://ai.azure.us/.default"
)

client = AzureOpenAI(
    azure_endpoint="https://bom-processor-gov.calmglacier-732dba63.usgovvirginia.azurecontainerapps.us",
    azure_ad_token_provider=token_provider,
    api_version="2025-01-01-preview",
)

response = client.responses.create(
    model="bom-processor",
    input="Parse the BOM at boms/acme/sample.xlsx and normalize it for quoting.",
)
print(response.output_text)
```

> **Note:** The Container App endpoint is currently open (no auth enforced at ingress). If you need Entra-authenticated access, add Container App authentication or put it behind a private endpoint.

## Deploying

### Azure Government (Container Apps)

Hosted Agents are not available in Gov regions, so the agent runs as a Container App. See `setup.md` for the full walkthrough. Key steps:

```powershell
# 1. Build and push the image to the Gov ACR
az acr build --registry etContainerRegistry --image bom-processor:<tag> `
  --platform linux/amd64 --file Dockerfile .

# 2. Update the container app to the new image
az containerapp update --name bom-processor-gov --resource-group et-rg-adf `
  --image etcontainerregistry.azurecr.us/bom-processor:<tag>
```

### Commercial (Foundry Hosted Agent)

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

For production (Gov), the container app's system-assigned managed identity is granted **Foundry User** on the `azuregovagents-resource` Cognitive Services account.

> **Gov scope fix:** the `azure-ai-projects` SDK hardcodes the token scope to `https://ai.azure.com/.default`, which Gov managed-identity endpoints reject. `main.py` wraps the credential to translate the scope to `https://ai.azure.us/.default` when the project endpoint is a `.azure.us` host.

Permissions are assigned during deployment (see setup.md).