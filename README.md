# HEIMDAL – Azure API Management as an LLM & Agent Gateway

> *"I see everything."* – Heimdall, guardian of the Bifrost

A demo showcase of **Azure API Management (APIM) as an AI gateway**: one endpoint for every model and agent tool, with
**per-user token accounting**, a **configurable model router** (prompt length / prompt complexity), and **MCP** exposure.
Models run in **Microsoft Foundry**, infrastructure is **Bicep** in **Sweden Central**, custom code is **Python only**.
No private networking (demo).

```mermaid
flowchart LR
  subgraph Clients
    T[thor / loki / hulk / black-widow]:::c
    S[tony-stark – premium]:::c
    N[Notebooks · Agent Framework · Foundry Agent]:::c
  end
  T & S & N -->|api-key = user identity| B
  subgraph Bifrost[APIM – Bifrost]
    B[LLM API /bifrost/v1] --> R{Router<br/>length · complexity}
    L[llm-token-limit<br/>Mjolnir]:::p --> R
    M[llm-emit-token-metric<br/>All-Seeing Eye]:::p
    MCP[MCP server /shield-mcp]
  end
  R -->|tier-pym| F1[gpt-6-luna]
  R -->|tier-stark| F2[gpt-6-sol]
  R -->|tier-thanos| F3[MAI-Thinking-1]
  subgraph Foundry[Microsoft Foundry · Sweden Central]
    F1; F2; F3
  end
  MCP --> FN[Azure Function · S.H.I.E.L.D. MCP · Python]
  M --> AI[(App Insights + Log Analytics)]
  classDef c fill:#eef;classDef p fill:#fed
```

## What's inside

| Path | Purpose |
|---|---|
| `infra/` | Bicep: monitoring, Foundry + model deployments, APIM, RBAC, Function app, APIs/products/subscriptions |
| `policies/bifrost-router.xml` | The brain: token limit → model routing → managed-identity call → token metrics → `x-heimdal-*` headers |
| `policies/shield-mcp.xml` | MCP rate limit + function key injection |
| `src/shield-mcp/` | Python Azure Function (MCP extension) with MCU tools |
| `scripts/deploy.py`, `teardown.py` | Python wrappers around `az`; write `.env` for the notebooks |
| `notebooks/` | `00`–`08` demos (below) |

### Naming (MCU theme)
`rg-heimdal-asgard-swc` · `apim-heimdal-bifrost-*` · `fdry-heimdal-asgard-*` / `proj-heimdal-avengers` · `*-allseeing-*` (monitoring) ·
`func-heimdal-shield-mcp-*` · products `avengers-initiative` / `infinity-gauntlet` · users `thor, loki, hulk, black-widow, tony-stark` ·
tiers `pym` / `stark` / `thanos`.

## Deploy

Prereqs: Azure CLI (`az login`), Python 3.11+, permission to create APIM/Foundry/Function resources and role assignments, quota for the models.

```bash
pip install -r requirements.txt
python scripts/deploy.py --check-models                # are the models in swedencentral? (edit infra/main.bicepparam)
python scripts/deploy.py --email you@contoso.com       # ~20-40 min (APIM), writes .env
```

> **Models**: `main.bicepparam` is pre-filled with `gpt-6-luna`, `gpt-6-sol` and `MAI-Thinking-1` (versions/formats are placeholders – verify with `--check-models`
> or in the Foundry portal and adjust). Change the `models` / `tierModels` params to use any other deployments.

Teardown: `python scripts/teardown.py`.

## Demo flow (notebooks)

| # | Notebook | Shows |
|---|---|---|
| 00 | `00_setup_asgard` | Architecture, deploy, env check |
| 01 | `01_bifrost_basic_call` | OpenAI-compatible gateway call, routing headers |
| 02 | `02_all_seeing_eye_usage` | **Token usage per user** (custom metrics + LLM gateway logs, KQL) |
| 03 | `03_router_prompt_length` | Router by **prompt length**, client overrides |
| 04 | `04_router_complexity` | Router by **prompt complexity** – heuristic vs LLM classifier, live reconfig |
| 05 | `05_mjolnir_token_limits` | Per-user TPM limits, 429 for one user only |
| 06 | `06_shield_mcp_via_bifrost` | **MCP** server (Azure Function) governed by APIM |
| 07 | `07_foundry_agent_via_bifrost` | **Foundry Agent Service** using the gateway as its model |
| 08 | `08_agent_framework_via_bifrost` | **Agent Framework** agent: LLM + MCP tools, both via APIM |

## Router configuration

APIM **named values** (change in portal, `set_router(...)` in notebooks, or Bicep param `router`):

| Named value | Values | Meaning |
|---|---|---|
| `router-mode` | `length` \| `complexity` \| `hybrid` | which signal(s) decide; hybrid = higher tier wins |
| `length-threshold-tokens` | e.g. `500` | est. prompt tokens (chars/4): `<T` pym, `<4T` stark, else thanos |
| `complexity-mode` | `heuristic` \| `llm` | policy scoring vs small-model classifier (falls back to heuristic) |
| `complexity-threshold` | e.g. `3` | score for stark; `2×` for thanos |
| `model-tier-pym/stark/thanos`, `model-classifier` | deployment names | tier → model mapping |
| `tpm-standard`, `tpm-premium` | e.g. `5000` | tokens per minute per user by product |

Clients may also force `model="tier-thanos"` or a concrete deployment name; `model="heimdal-auto"` (or omitted) routes automatically.

## Known notes / caveats
- **Token metrics** require *Custom metrics with dimensions* on the App Insights resource (Usage & estimated costs → Custom metrics → With dimensions; usually default for new workspace-based resources). For streaming, send `stream_options={"include_usage": true}`.
- Logs/metrics lag a few minutes. The LLM logs table (`ApiManagementGatewayLlmLog`) gives exact per-request tokens and model.
- MCP via APIM uses API version `2025-09-01-preview`. The Function MCP extension needs the `mcp_extension` system key; `deploy.py` injects it into the `shield-mcp-key` named value after code publish.
- Foundry connection (notebook 07) and Agent Framework (notebook 08) use preview SDK/APIs – adjust to current docs if signatures changed.
- This is a demo: public endpoints, subscription keys, no VNet. Harden (Entra/JWT auth, private endpoints, Key Vault named values) for real use.
