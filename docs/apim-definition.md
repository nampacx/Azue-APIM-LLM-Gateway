# 🌈 Bifrost – what is defined in API Management

This is the **inventory** of everything that gets created inside the APIM instance (nicknamed **Bifrost**) and where it
is defined. For the *why* and the request flow, read [apim-integration.md](apim-integration.md).

| Defined in | Creates |
|---|---|
| [`infra/modules/apim.bicep`](../infra/modules/apim.bicep) | the service, loggers, diagnostics, backends, named values |
| [`infra/modules/apim-apis.bicep`](../infra/modules/apim-apis.bicep) | products, APIs, operations, policies, subscriptions |
| [`infra/modules/rbac.bicep`](../infra/modules/rbac.bicep) | role assignments for APIM's managed identity on Foundry |
| [`policies/bifrost-router.xml`](../policies/bifrost-router.xml) | policy of the LLM API |
| [`policies/shield-mcp.xml`](../policies/shield-mcp.xml) | policy of the MCP API |

## 🏰 1. The service

| Setting | Value |
|---|---|
| Resource | `Microsoft.ApiManagement/service` (API version `2025-09-01-preview`) |
| Name | `apim-heimdal-bifrost-<suffix>` |
| SKU / capacity | parameter `sku`, capacity `1` |
| Identity | **system-assigned managed identity** (used to call Foundry) |
| Network | public, no VNet (demo) |

## 👥 2. Products and users (subscriptions)

| Product | Tier | Token budget | Users (subscription = user) |
|---|---|---|---|
| `avengers-initiative` | standard | `{{tpm-standard}}` tokens/min | `thor`, `loki`, `hulk`, `black-widow` |
| `infinity-gauntlet` | premium | `{{tpm-premium}}` tokens/min | `tony-stark` |

- Both products are `published`, require a subscription, need no approval and contain **both APIs**.
- Subscriptions are generated from the `demoUsers` array and scoped to the user's product. The subscription **name is the
  user identity** that shows up in metrics, limits and the `x-heimdal-user` header.
- Key is sent in the `api-key` header (or `subscription-key` query) so OpenAI-style clients work unchanged.

## 🔀 3. APIs

### `bifrost-llm` – Bifrost LLM Gateway
| | |
|---|---|
| Path | `/bifrost/v1` |
| Operation | `POST /chat/completions` (OpenAI-compatible) |
| `serviceUrl` | placeholder – the real backend is chosen in the policy |
| Policy | `bifrost-router.xml` |

Policy pipeline, in order:

1. 🪪 resolve user from the subscription
2. 🔨 `llm-token-limit` – per-user tokens/min (**Mjolnir**), 429 when exceeded
3. 🧠 route: length signal, complexity signal (heuristic and/or LLM classifier), client overrides
4. 🔁 rewrite `model`, `set-backend-service`, `authentication-managed-identity`, strip the client's key
5. 👁️ `llm-emit-token-metric` – dimensions `User`, `Model`, `Tier`, `Agent` (from `x-agent` header), `API ID` (**All-Seeing Eye**)
6. 🏷️ outbound `x-heimdal-user|tier|model|route-reason` headers

### `shield-mcp` – S.H.I.E.L.D. MCP
| | |
|---|---|
| Path | `/shield-mcp` |
| Operations | `POST`, `GET`, `DELETE` on `/mcp` (streamable HTTP) |
| `serviceUrl` | the Azure Function app (`mcpBackendUrl`) |
| Policy | `shield-mcp.xml` |

Policy: `rate-limit-by-key` 30 calls/min per subscription → inject `x-functions-key` from `{{shield-mcp-key}}` →
rewrite URI to `/runtime/webhooks/mcp` → delete client credentials → forward **unbuffered**.
Tools served by the Function: `list_avengers`, `get_avenger`, `get_mission_status`, `locate_infinity_stone`.

> It is a plain HTTP API, not `type: mcp`: the preview MCP API type returned 500 on `tools/call` on this instance.

## 🔌 4. Backends

| Backend | URL | Used for |
|---|---|---|
| `asgard-foundry` | `https://<foundry>.openai.azure.com/openai/v1` | OpenAI models (`gpt-*`) |
| `asgard-foundry-mai` | `https://<foundry>.services.ai.azure.com/mai/v1` | MAI models (need `max_completion_tokens`) |

The MCP Function is not a backend object; it is the `serviceUrl` of the `shield-mcp` API.

## 🎛️ 5. Named values (live configuration)

| Name | Default | Meaning |
|---|---|---|
| `router-mode` | `hybrid` | `length` \| `complexity` \| `hybrid` (higher tier wins) |
| `complexity-mode` | `heuristic` | `heuristic` \| `llm` |
| `length-threshold-tokens` | `500` | `<T` pym, `<4T` stark, else thanos |
| `complexity-threshold` | `3` | score for stark; `2×` for thanos |
| `model-tier-pym` / `-stark` / `-thanos` | from `tierModels` | tier → deployment |
| `model-classifier` | small model | deployment used by the LLM classifier |
| `mai-deployments` | `none` or list | deployments served on `/mai/v1` |
| `foundry-openai-base` | Foundry v1 URL | base URL for the classifier `send-request` |
| `tpm-standard` / `tpm-premium` | `5000` / `100000` | tokens per minute per product |
| `shield-mcp-key` 🔒 | set by deploy script | Function `mcp_extension` system key (secret) |

Change live with `set_router(...)` from `notebooks/heimdal_helpers.py` (wraps `az apim nv update`). A Bicep redeploy resets them.

## 👁️ 6. Observability wiring

| Piece | Purpose |
|---|---|
| Logger `heimdal-appinsights` + diagnostic `applicationinsights` (`metrics: true`) | custom token metrics → App Insights `AppMetrics` |
| Logger `azuremonitor` + diagnostic `azuremonitor` (`largeLanguageModel.logs: enabled`) | per-request tokens + model → `ApiManagementGatewayLlmLog` |
| Diagnostic setting `to-log-analytics` | ships `GatewayLogs` and `GatewayLlmLogs` + metrics to Log Analytics |
| Workbook `Heimdal - LLM token usage` ([`workbook.bicep`](../infra/modules/workbook.bicep)) | tokens per user, model per user |

No request/response bodies are logged (privacy, and global body capture would break MCP streaming).

## 🔐 7. Security model

- Callers authenticate with **subscription keys**; the key is removed before forwarding.
- APIM → Foundry uses the **managed identity** with roles *Cognitive Services OpenAI User* and *Cognitive Services User* (for MAI).
- APIM → Function uses a **secret named value** with the function key.
- Demo only: public endpoints. For production use Entra/JWT validation, private endpoints and Key Vault-backed named values.
