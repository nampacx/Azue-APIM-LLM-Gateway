# 🌈 Bifrost – what is defined in API Management

> 💡 **TL;DR:** This is the **inventory** of everything created inside the APIM instance (nicknamed **Bifrost**) and the file that defines it: one service, two products, two APIs, two backends, a set of live named values, and the logging that feeds the dashboards.
>
> 📖 For the *why* and the request flow, read [apim-integration.md](apim-integration.md). For how a caller becomes a user, read [user.md](user.md).

| | Section | In one line |
|---|---|---|
| 🏰 | 1. The service | StandardV2/PremiumV2, managed identity, outbound VNet |
| 👥 | 2. Products and users | two tiers, one subscription per user |
| 🔀 | 3. APIs | `bifrost-llm` and `shield-mcp` |
| 🔌 | 4. Backends | Foundry OpenAI and Foundry MAI |
| 🎛️ | 5. Named values | router and limit settings you can change live |
| 👁️ | 6. Observability | token metrics and LLM logs |
| 🔐 | 7. Security model | who authenticates how |

---

## 🗂️ Where it is defined

| Defined in | Creates |
|---|---|
| [`infra/config/heimdal.json`](../infra/config/heimdal.json) | models, tiers, router defaults, token limits, demo users |
| [`infra/modules/network.bicep`](../infra/modules/network.bicep) | VNet + delegated subnet for APIM outbound VNet integration |
| [`infra/modules/apim.bicep`](../infra/modules/apim.bicep) | the service, loggers, diagnostics, backends, named values |
| [`infra/modules/apim-apis.bicep`](../infra/modules/apim-apis.bicep) | products, APIs, operations, policies, subscriptions |
| [`infra/modules/rbac.bicep`](../infra/modules/rbac.bicep) | role assignments for APIM's managed identity on Foundry (+ demo operator roles) |
| [`infra/modules/foundry-connection.bicep`](../infra/modules/foundry-connection.bicep) | Foundry project connection `bifrost-apim` to the gateway |
| [`policies/bifrost-router.xml`](../policies/bifrost-router.xml) | policy of the LLM API (chat completions + responses). Explained in [policies.md](policies.md) and [routing.md](routing.md) |
| [`policies/bifrost-embeddings.xml`](../policies/bifrost-embeddings.xml) | operation policy of `POST /embeddings`. Explained in [embeddings-mcp-policies.md](embeddings-mcp-policies.md) |
| [`policies/shield-mcp.xml`](../policies/shield-mcp.xml) | policy of the MCP API. Explained in [embeddings-mcp-policies.md](embeddings-mcp-policies.md#5--mcp-rate-limit-and-function-key) |

---

## 🏰 1. The service

| Setting | Value |
|---|---|
| 📦 Resource | `Microsoft.ApiManagement/service` (API version `2025-09-01-preview`) |
| 🏷️ Name | `apim-hmdl-bfrst-<suffix>` |
| 📏 SKU / capacity | parameter `sku` (`StandardV2` or `PremiumV2`), capacity `1` |
| 🪪 Identity | **system-assigned managed identity** (used to call Foundry and App Insights) |
| 🌐 Network | public gateway; **outbound VNet integration** (`snet-apim-outbound`) so the MCP Function can allow only APIM |

---

## 👥 2. Products and users (subscriptions)

| | Product | Tier | Token budget | Users (subscription = user) |
|---|---|---|---|---|
| 🟦 | `avengers-initiative` | standard | `{{tpm-standard}}` tokens/min | `thor`, `loki`, `hulk`, `black-widow` |
| 🟪 | `infinity-gauntlet` | premium | `{{tpm-premium}}` tokens/min | `tony-stark` |

- Both products are `published`, require a subscription, need no approval and contain **both APIs**.
- Subscriptions are generated from the `demoUsers` array and scoped to the user's product. The subscription **name is the user identity** that shows up in metrics, limits and the `x-heimdal-user` header.
- The key is sent in the `api-key` header so OpenAI-style clients work unchanged. Keys in the URL (`subscription-key` query) are rejected with 400 by the policies.

> 🛡️ The full story of how a key becomes a user is in [user.md](user.md).

---

## 🔀 3. APIs

### 🧠 `bifrost-llm` – Bifrost LLM Gateway

| | |
|---|---|
| Path | `/bifrost/v1` |
| Operations | `POST /chat/completions`, `POST /responses` (routed), `POST /embeddings` (OpenAI-compatible) |
| `serviceUrl` | placeholder – the real backend is chosen in the policy |
| Policy | `bifrost-router.xml` (API); `bifrost-embeddings.xml` on `embeddings` (no `<base />`, skips the router) |

Policy pipeline, in order:

0. 🚫 reject a subscription key in the URL (400)
1. 🪪 resolve user from the subscription
2. 🔨 `llm-token-limit` – per-user tokens/min (**Mjolnir**, one static limit per product branch), 429 when exceeded
3. 🧭 route: length signal, complexity signal (heuristic and/or LLM classifier), client overrides
4. 🔁 rewrite `model`, `set-backend-service`, `authentication-managed-identity`, strip the client's key
5. 👁️ `llm-emit-token-metric` – dimensions `User`, `Model`, `Tier`, `Agent` (from `x-agent` header), `API ID` (**All-Seeing Eye**)
6. 🏷️ outbound `x-heimdal-user|tier|model|route-reason` headers

### 🛡️ `shield-mcp` – S.H.I.E.L.D. MCP

| | |
|---|---|
| Path | `/shield-mcp` |
| Operations | `POST`, `GET`, `DELETE` on `/mcp` (streamable HTTP) |
| `serviceUrl` | the Azure Function app (`mcpBackendUrl`) |
| Policy | `shield-mcp.xml` |

Policy: `rate-limit-by-key` 30 calls/min per subscription → inject `x-functions-key` from `{{shield-mcp-key}}` → rewrite URI to `/runtime/webhooks/mcp` → delete client credentials → forward **unbuffered**.

🦸 Tools served by the Function: `list_avengers`, `get_avenger`, `get_mission_status`, `locate_infinity_stone`.

> ℹ️ It is a plain HTTP API, not `type: mcp`: the preview MCP API type returned 500 on `tools/call` on this instance.

> 🔒 The Function only accepts calls from APIM's outbound subnet (access restrictions, default action `Deny`), so the function key alone is not enough to bypass the gateway.

---

## 🔌 4. Backends

| Backend | URL | Used for |
|---|---|---|
| `asgard-foundry` | `https://<foundry>.openai.azure.com/openai/v1` | OpenAI models (`gpt-*`) |
| `asgard-foundry-mai` | `https://<foundry>.services.ai.azure.com/mai/v1` | MAI models (need `max_completion_tokens`) |
| `asgard-ollama` (optional) | `https://<your>.ngrok-free.app/v1` | Local Ollama models via ngrok (basic auth); only with `HEIMDAL_ENABLE_OLLAMA=true` |

> ℹ️ The MCP Function is not a backend object; it is the `serviceUrl` of the `shield-mcp` API.

---

## 🎛️ 5. Named values (live configuration)

| Name | Default | Meaning |
|---|---|---|
| `router-mode` | `hybrid` | `length` \| `complexity` \| `hybrid` (higher tier wins) |
| `complexity-mode` | `heuristic` | `heuristic` \| `llm` |
| `length-threshold-tokens` | `500` | `<T` pym, `<4T` stark, else thanos |
| `complexity-threshold` | `3` | score for stark; `2×` for thanos |
| `model-tier-pym` / `-stark` / `-thanos` | from `tierModels` | tier → deployment |
| `model-classifier` | small model | deployment used by the LLM classifier |
| `model-embedding` | `text-embedding-3-small` | deployment for `/embeddings` with `heimdal-auto` |
| `mai-deployments` | `none` or list | deployments served on `/mai/v1` |
| `ollama-models` | `none` or list | models served by the local Ollama backend (`asgard-ollama`) |
| `ollama-basic-auth` 🔒 | `not-configured` or secret | base64 `user:password` for the ngrok basic-auth in front of Ollama |
| `foundry-openai-base` | Foundry v1 URL | base URL for the classifier `send-request` |
| `tpm-standard` / `tpm-premium` | `5000` / `100000` | tokens per minute per product |
| `shield-mcp-key` 🔒 | set by deploy script | Function `mcp_extension` system key (secret); kept on redeploy via the `mcpKey` parameter |

> 📜 What each router value does in the policy: [routing.md](routing.md) and [policies.md](policies.md#6--changing-the-behaviour-at-runtime).

> ⚡ Change them live with `set_router(...)` from `notebooks/heimdal_helpers.py` (wraps `az apim nv update`). A Bicep redeploy resets the router values to `infra/config/heimdal.json`. How to use it: [apim-integration.md](apim-integration.md).

---

## 👁️ 6. Observability wiring

| Piece | Purpose |
|---|---|
| Logger `heimdal-appinsights` (connection string + managed identity) + diagnostic `applicationinsights` (`metrics: true`) | custom token metrics → App Insights `AppMetrics` |
| Logger `azuremonitor` + diagnostic `azuremonitor` (`largeLanguageModel.logs: enabled`) | per-request tokens + model → `ApiManagementGatewayLlmLog` |
| Diagnostic setting `to-log-analytics` | ships `GatewayLogs` and `GatewayLlmLogs` + metrics to Log Analytics |
| Workbook `Heimdal - LLM token usage` ([`workbook.bicep`](../infra/modules/workbook.bicep)) | tokens per user, model per user; **Local Ollama via ngrok** section: calls per backend (`BackendId`), latency, failures, latest local calls |

> 🙈 No request/response bodies are logged (privacy, and global body capture would break MCP streaming).

---

## 🔐 7. Security model

| | Hop | How it authenticates |
|---|---|---|
| 🧑‍💻 → 🌈 | Caller → APIM | **subscription key** in the `api-key` header; the key is removed before forwarding. URL keys are rejected |
| 🌈 → 🧠 | APIM → Foundry | **managed identity** with *Cognitive Services OpenAI User* (+ *Cognitive Services User* when MAI is enabled) |
| 🌈 → 📈 | APIM → App Insights | **managed identity** (*Monitoring Metrics Publisher*) |
| 🌈 → 🛡️ | APIM → Function | **secret named value** with the function key **and** the Function only accepts APIM's VNet subnet |

> ⚠️ **Demo only:** public gateway endpoint and subscription keys. For production use Entra/JWT validation, private endpoints and Key Vault-backed named values.
