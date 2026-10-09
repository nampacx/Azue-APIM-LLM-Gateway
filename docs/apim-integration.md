# How the APIM integration works

This document explains how Azure API Management (APIM, nicknamed **Bifrost**) sits between clients and the
models/tools, what each piece of the policy does, and how everything is wired up in Bicep.

Files involved:

| File | Role |
|---|---|
| `infra/config/heimdal.json` | Models, tier mapping, router defaults, token limits, demo users (shared by Bicep, scripts, notebooks) |
| `infra/modules/network.bicep` | VNet + delegated subnet for APIM **outbound VNet integration** |
| `infra/modules/apim.bicep` | APIM instance, logging/metrics wiring, backends, **named values** (router config) |
| `infra/modules/apim-apis.bicep` | Products, **subscriptions (= users)**, the LLM API, the MCP API, attaching the policies |
| `infra/modules/rbac.bicep` | Lets APIM's managed identity call Foundry; data-plane roles for the demo operator |
| `infra/modules/foundry-connection.bicep` | Foundry project connection to the gateway (for Foundry agents) |
| `policies/bifrost-router.xml` | The LLM policy (chat + responses): limit → route → call → measure |
| `policies/bifrost-embeddings.xml` | Operation policy for `/embeddings` (no router) |
| `policies/shield-mcp.xml` | The MCP policy: rate limit + function key |
| `notebooks/heimdal_helpers.py` | Client side: `ask()`, `set_router()`, KQL helpers |

## 1. Big picture

```
client (key = user)                       APIM "Bifrost"                                   backend
─────────────────  api-key: <sub key>   ┌──────────────────────────────────────┐
OpenAI SDK/notebook ───────────────────▶│ /bifrost/v1/chat/completions|responses│──MI token──▶ Foundry /openai/v1   (gpt-*)
                                        │   bifrost-router.xml                 │──MI token──▶ Foundry /mai/v1      (MAI-*)
                                        │ /bifrost/v1/embeddings               │──MI token──▶ Foundry /openai/v1   (embeddings)
Agent / MCP client ────────────────────▶│ /shield-mcp                          │──func key──▶ Azure Function (MCP)
                                        │   shield-mcp.xml                     │   via VNet subnet (Function denies all other sources)
                                        └───────────┬──────────────────────────┘
                                                    │ metrics + logs
                                                    ▼
                                        App Insights (AppMetrics) · Log Analytics (ApiManagementGatewayLlmLog)
```

Three ideas carry the whole design:

1. **Identity = APIM subscription.** Every demo user has their own subscription key. APIM knows who calls
   (`context.Subscription.Name`), so limits and metrics are per user without any custom auth code.
2. **Clients never see backend credentials.** The client's key is deleted before forwarding; APIM calls Foundry with
   its own **system-assigned managed identity** and calls the Function with a **function key** stored as a secret named value.
   The Function additionally only accepts traffic from APIM's outbound VNet subnet, so the gateway is the only way in.
3. **Behaviour is configuration.** The router reads **named values** (`{{router-mode}}` …), so you can change routing live
   without redeploying anything.

## 2. Users, products, subscriptions

Defined in `infra/modules/apim-apis.bicep`.

- **Products** group APIs and carry the tier: `avengers-initiative` (standard) and `infinity-gauntlet` (premium).
  Both contain both APIs (LLM + MCP).
- **Subscriptions** are created from the `demoUsers` array, one per user, scoped to the user's product
  (`scope: <apim>/products/<product>`). The subscription *name* is the user name (`thor`, `loki`, …).
- Both APIs read the key from the **`api-key` header**, so standard OpenAI clients work: they just put the key in
  `api-key`. `heimdal_helpers.client()` sends it in both `api-key` and `Authorization`. A key in the URL
  (`?subscription-key=`) is rejected with 400 by the policies (keys in URLs end up in logs).

`context.Subscription.Name` → user, `context.Product.Id` → tier of limits.

## 3. The LLM API (`bifrost-llm`)

- Path `bifrost/v1`, operations `POST /chat/completions`, `POST /responses` (both routed by `bifrost-router.xml`) and
  `POST /embeddings` → OpenAI-compatible, so `base_url = https://<apim>.azure-api.net/bifrost/v1` is all a client needs.
- `/embeddings` has its own operation policy **without `<base />`** (`bifrost-embeddings.xml`): no tiers, `heimdal-auto`
  → `{{model-embedding}}`, a per-user call rate limit and an `Embedding Tokens` metric (`emit-metric`; the `llm-*`
  policies don't support the embeddings schema).
- The API's `serviceUrl` is a placeholder; the real backend is chosen **inside the policy** with `set-backend-service`.
- Two APIM **backends** exist (`apim.bicep`): `asgard-foundry` (`…openai.azure.com/openai/v1`) and
  `asgard-foundry-mai` (`…services.ai.azure.com/mai/v1`, for MAI models).

### Policy walkthrough (`policies/bifrost-router.xml`)

Order in the `<inbound>` section:

| Step | What happens | Policy element |
|---|---|---|
| 0 | Reject a subscription key sent in the URL (400) | `choose` / `return-response` |
| 1 | Resolve caller: `user = Subscription.Name` (or `anonymous`) | `set-variable user` |
| 2 | **Token limit per user per minute.** Counter key = subscription id; a `choose` on the product picks one of two `llm-token-limit` policies with a static `{{tpm-premium}}` (product `infinity-gauntlet`) or `{{tpm-standard}}` limit. Prompt tokens are *estimated* before the call. Returns HTTP **429** when exceeded and a header `x-heimdal-remaining-tokens` | `llm-token-limit` |
| 3a | Parse body once into `body`; extract `requestedModel` and `isAuto` (empty, `heimdal-auto` or `auto`); flatten the prompt into `promptText` – chat `messages[]` or Responses `instructions` + `input` (string or items), string and multi-part content | `set-variable` |
| 3b | **Length signal**: `promptTokens = chars / 4`. Tier index = `<T` → 0, `<4T` → 1, else 2 (`T = {{length-threshold-tokens}}`) | `lengthTier` |
| 3c | **Complexity signal (heuristic)**: score from code markers (+2), reasoning keywords (max +3), multiple questions (+1/+2), math words (+1), long conversation >6 msgs (+1). Tier index: `<C` → 0, `<2C` → 1, else 2 (`C = {{complexity-threshold}}`) | `complexityScore`, `heuristicTier` |
| 3d | **Complexity signal (LLM)**: only if `router-mode != length`, `complexity-mode == llm` and the model is auto (`isAuto`: empty, `heimdal-auto`, `auto`). Sends the first 4000 chars to the small `{{model-classifier}}` and expects `simple` / `medium` / `complex`. Any error or odd answer → `-1` → heuristic is used instead (`ignore-error="true"`) | `send-request`, `llmTier` |
| 3e | **Decide**: tier, deployment and a human-readable reason (see below) | `tier`, `routedModel`, `routeReason` |
| 4 | Rewrite `model` in the body to the deployment, pick backend, authenticate with the managed identity, remove the client's `api-key` | `set-body`, `set-backend-service`, `authentication-managed-identity`, `set-header delete` |
| 5 | Emit per-user token metrics | `llm-emit-token-metric` |

`<backend>` just forwards the request. `<outbound>` adds the routing headers.

### How the tier is decided

```
requested model            result
─────────────────────────  ────────────────────────────────────────────────
tier-pym|stark|thanos      that tier                       (reason: client-forced-tier)
any other non-empty name   "explicit": used as deployment  (reason: client-selected-deployment)
empty / heimdal-auto/auto  automatic, by {{router-mode}}:
                             length      → lengthTier
                             complexity  → LLM tier if available, else heuristic tier
                             hybrid      → max(lengthTier, complexityTier)
```

Tier → deployment comes from `model-tier-pym|stark|thanos` named values.

### MAI models

MAI deployments (`{{mai-deployments}}`, comma list) live on a different endpoint (`/mai/v1`) and accept only
`max_completion_tokens`. When the routed deployment is in that list, the policy selects the `asgard-foundry-mai`
backend and renames `max_tokens` → `max_completion_tokens`. If MAI is disabled the named value is `none`
(named values cannot be empty).

### Local Ollama models (optional)

With `HEIMDAL_ENABLE_OLLAMA=true`, the azd `preprovision` hook (`scripts/ollama_tunnel.py`) makes sure a local
Ollama is serving the requested model(s) and opens an **ngrok tunnel** to it. The tunnel runs with an ngrok traffic
policy: `basic-auth` (a generated secret kept in the azd env as `HEIMDAL_OLLAMA_AUTH`) and a Host rewrite to
`localhost`. Bicep then creates the `asgard-ollama` backend (`<ngrok url>/v1`, Ollama's OpenAI-compatible surface),
fills `{{ollama-models}}`, and stores the credentials base64-encoded in the secret named value `{{ollama-basic-auth}}`.

When the routed model is in `{{ollama-models}}`, the policy selects `asgard-ollama`. It skips the managed identity
and sends `Authorization: Basic {{ollama-basic-auth}}`, overriding the client's `Authorization` header, plus
`ngrok-skip-browser-warning`. `x-heimdal-route-reason` is prefixed with `local-ollama;`. Token limits and token
metrics work unchanged, because Ollama returns OpenAI-style `usage`. `/embeddings` uses the same check, for example
for `nomic-embed-text`. Because the check runs on the *routed* model, a tier named value
(`model-tier-pym` = `llama3.2`) can also send a whole tier to the local model.

To see whether the local model really served a call, check the gateway log: `ApiManagementGatewayLogs.BackendId` is
`asgard-ollama`, and `BackendTime` / `BackendResponseCode` show latency and tunnel failures (0 / 502 / 504 when the
laptop or ngrok is offline). The workbook section *Local Ollama via ngrok* and `KQL_CALLS_BY_BACKEND` join this with
`ApiManagementGatewayLlmLog` to get tokens and the model name.

### Response headers (what the client sees)

| Header | Content |
|---|---|
| `x-heimdal-user` | subscription name |
| `x-heimdal-tier` | `pym` / `stark` / `thanos` / `explicit` |
| `x-heimdal-model` | deployment that served the call |
| `x-heimdal-route-reason` | e.g. `mode=hybrid;complexity=heuristic;tokens~120;score=3;llmTier=-1` |
| `x-heimdal-remaining-tokens` | what is left of the per-minute budget |

`ask()` in `heimdal_helpers.py` reads exactly these.

### Gotcha: `{{named-value}}` in policy

`{{name}}` is substituted by APIM **textually**, before the C# expression compiles. That is why numeric ones
are wrapped as `int.Parse("{{length-threshold-tokens}}")` and string ones are quoted. A missing named value fails
the policy, so every name used in the XML must exist in `apim.bicep`.

## 4. Authentication to Foundry

- APIM has a **system-assigned managed identity** (`identity: SystemAssigned` in `apim.bicep`).
- `rbac.bicep` grants it, on the Foundry account, **Cognitive Services OpenAI User** (OpenAI models) and – only when
  `enableMai` is on – **Cognitive Services User** (needed for non-OpenAI formats such as MAI).
- `rbac.bicep` also grants the demo operator (`deployerPrincipalId`: the azd user / the `az login` identity of
  `deploy.py`) **Azure AI User** on the Foundry account (notebook 07) and **Log Analytics Reader** (`kql()`).
- The policy requests a token for `https://cognitiveservices.azure.com` with `authentication-managed-identity`.
- `apim-apis` has `dependsOn: [rbac]` so the roles exist before the first call.
- The classifier call (`send-request`) authenticates the same way.
- Exception: the optional local Ollama backend has no Entra identity. The policy sends the ngrok basic-auth
  credentials from the secret named value `ollama-basic-auth` instead.

## 5. The MCP API (`shield-mcp`)

- A plain HTTP APIM API (POST/GET/DELETE `/mcp`), path `/shield-mcp`, rewritten to the Function's `/runtime/webhooks/mcp` and forwarded unbuffered. (The preview `type: 'mcp'` API ran in managed-tools mode on this instance and returned 500 on `tools/call`.)
- The endpoint template maps to the Functions MCP extension: `/runtime/webhooks/mcp`
  (`endpoints` is an *object* keyed by endpoint name, even though docs show an array).
- Policy (`shield-mcp.xml`):
  - `rate-limit-by-key`: 30 calls/minute per subscription.
  - Adds `x-functions-key` = `{{shield-mcp-key}}` (the Functions `mcp_extension` system key).
  - Deletes the client's `api-key` / `Ocp-Apim-Subscription-Key` so they never reach the Function.
  - `forward-request buffer-response="false"` and **no access to `context.Response.Body`** – either one would
    buffer the stream and break MCP streaming.
- `shield-mcp-key` gets a placeholder on the very first deployment; `scripts/deploy.py` (or the azd postdeploy hook)
  replaces it with the real key after the Function code is published, because that key only exists after the deployment.
  Later deployments get the current key through the secure `mcpKey` parameter (`deploy.py` reads it from the Function,
  azd from `HEIMDAL_MCP_KEY`, which the hook stores in the azd environment), so a Bicep-only redeploy keeps MCP working.
- **Network lock-down:** APIM (v2 tier) has **outbound VNet integration** into `snet-apim-outbound` (delegated to
  `Microsoft.Web/serverFarms`, service endpoint `Microsoft.Web`). The Function's access restrictions allow only that
  subnet (`ipSecurityRestrictionsDefaultAction: Deny`), so calling the Function directly – even with the key – gets 403.
  The deployment (SCM) endpoint keeps its own open rules for `azd deploy` / zip deploy (Entra ID protected).

## 6. Observability

Configured in `apim.bicep`:

| Piece | Purpose |
|---|---|
| Logger `heimdal-appinsights` + diagnostic with `metrics: true` | Enables custom metrics from `llm-emit-token-metric` into App Insights (`AppMetrics`). The logger uses the connection string + APIM's **managed identity** (*Monitoring Metrics Publisher*), no instrumentation key |
| `llm-emit-token-metric` (policy) | Metrics `Prompt/Completion/Total Tokens` with dimensions **User, Model, Tier, Agent (from the `x-agent` request header), API ID** |
| Logger `azuremonitor` + diagnostic with `largeLanguageModel.logs: enabled` | Per-request token usage and model into `ApiManagementGatewayLlmLog` |
| Diagnostic setting `to-log-analytics` | Ships `GatewayLogs` and `GatewayLlmLogs` to the Log Analytics workspace |

Deliberate choices:

- **No request/response body logging.** It would store prompts, and global response-body capture breaks MCP streaming.
- The LLM log has no user column; join it to `ApiManagementGatewayLogs` on `CorrelationId` (`ApimSubscriptionId` = user).
  See `KQL_TOKENS_BY_USER_LLMLOG` in `heimdal_helpers.py`.
- Ingestion lags a few minutes → `kql_wait()` retries until rows show up.
- For streaming calls clients should send `stream_options={"include_usage": true}` or token counts are missing.

## 7. Runtime configuration (named values)

Created in `apim.bicep` from the `router`, `tierModels`, `tokenLimits` params:

| Named value | Meaning |
|---|---|
| `router-mode` | `length` \| `complexity` \| `hybrid` |
| `complexity-mode` | `heuristic` \| `llm` |
| `length-threshold-tokens` | tier boundary T (est. tokens); stark < 4T ≤ thanos |
| `complexity-threshold` | score boundary C; stark < 2C ≤ thanos |
| `model-tier-pym/stark/thanos` | deployment per tier |
| `model-classifier` | small model used for LLM classification |
| `model-embedding` | embedding deployment for `/embeddings` with `heimdal-auto` |
| `mai-deployments` | deployments served on `/mai/v1` |
| `foundry-openai-base` | base URL for the classifier call |
| `tpm-standard` / `tpm-premium` | tokens per minute per product |
| `shield-mcp-key` (secret) | Function key for MCP |

Change them without redeploying:

```python
from heimdal_helpers import set_router
set_router(router_mode="hybrid", length_threshold_tokens="200")   # '_' becomes '-'
```

This runs `az apim nv update …`; the policy sees the new value within about a minute (APIM caches named values).
Remember that a redeploy of the Bicep resets them to the values in `infra/config/heimdal.json`.

## 8. Request lifecycle (example)

`ask("Compare Kafka and SQS step by step", user="thor")`:

1. SDK sends `POST /bifrost/v1/chat/completions` with `api-key: <thor key>`, `model: heimdal-auto`.
2. APIM validates the subscription → `user = thor`, product `avengers-initiative`.
3. `llm-token-limit` checks thor's per-minute budget (standard TPM).
4. Router computes length and complexity (`compare` + `step by step` → score ≥ 2), mode `hybrid` → e.g. tier `stark`.
5. Body `model` is rewritten to the stark deployment; APIM calls `asgard-foundry` with its managed identity.
6. Response returns; outbound adds `x-heimdal-*` headers.
7. Token metric (user=thor, model, tier) and an LLM log row are emitted; they appear in KQL after a few minutes.

## 9. Extending it

- **New tier model:** change `tierModels` (and `models`) in `infra/config/heimdal.json` (or set the `model-tier-*` named value live).
- **New user:** add to `demoUsers` in `infra/config/heimdal.json`; a subscription is created and the deploy script / azd
  hook writes its key to `.env` as `HEIMDAL_KEY_<NAME>`.
- **Local model:** `azd env set HEIMDAL_ENABLE_OLLAMA true` + `HEIMDAL_OLLAMA_MODEL <name>`, then `azd provision`
  (see README → *Local Ollama via ngrok*).
- **New scoring rule:** edit `complexityScore` in `bifrost-router.xml` and redeploy `apim-apis`.
- **Harden for production:** Entra/JWT validation instead of subscription keys, private endpoints (inbound to APIM,
  Foundry, Function), Key Vault-backed named values, `token-quota` budgets, content safety policies.
