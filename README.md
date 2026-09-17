# BOM Processor Agent

A hosted AI agent for electronic contract manufacturing. Built on Microsoft Foundry using the Agent Framework, powered by **DeepSeek-V4**.

**Primary objective:** Learn customer assembly BOM formats and develop per-customer parser scripts that normalize BOMs into a stardardized quoting format.

The agent is exposed via the OpenAI-compatible `/responses` protocol and can be invoked programmatically or from the Foundry Portal.

## Prerequisites

- Python 3.12+
- [Azure Developer CLI (azd)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [Foundry Toolkit VS Code extension](https://marketplace.visualstudio.com/items?itemName=ms-windows-ai-studio.windows-ai-studio) (for Agent Inspector)
- Access to the `et-foundry` Azure AI Foundry project

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
    model="bom-processor",
    input="Parse the BOM at boms/acme/sample.xlsx and normalize it for quoting.",
)
print(response.output_text)
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

Permissions are assigned during deployment (see setup.md).