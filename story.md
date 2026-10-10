# 📖 The Story of HEIMDAL

> *"I see everything."* – Heimdall, guardian of the Bifrost 🌈

Asgard (your company) has many heroes (users and agents) who all want to talk to powerful models (**Microsoft Foundry**).
Letting everyone call the models directly means no idea who spends what, no limits, and no way to steer traffic.
So we put a guardian on the bridge: **Azure API Management – the Bifrost**. The notebooks walk through its powers, one chapter at a time. Chapters 00–08 use only Foundry models. Chapter 09 is an optional extra lane to a model on your own laptop.

```
⚡ 00 setup → 🌈 01 first call → 👁️ 02 usage → 📏 03 length → 🧠 04 complexity → 🔨 05 limits → 🛡️ 06 MCP → 🤖 07 Foundry agent → 🦸 08 agent framework → 🦙 09 local lane (optional)
```

What is configured in APIM is described in [docs/apim-definition.md](docs/apim-definition.md); how it works in
[docs/apim-integration.md](docs/apim-integration.md); the policies and the routing rules, with their XML, in
[docs/policies.md](docs/policies.md) and [docs/routing.md](docs/routing.md).

## 🎬 Chapters

### ⚡ 00 · `00_setup_asgard` – Asgard awakens
Shows the architecture and the Python deploy path (`scripts/deploy.py`), then checks that the `.env` written by
the deployment is complete (gateway URL, user keys). `azd up` is the main deploy path and writes the same `.env`: see the [README](README.md#-deploy-with-azd). **Run first.**

### 🌈 01 · `01_bifrost_basic_call` – Crossing the Bifrost
The first call. An OpenAI-compatible request goes to the gateway with only a gateway URL and a personal key – no Foundry
endpoint, no credentials. The response headers `x-heimdal-user|tier|model|route-reason` reveal who asked and which model answered.
The same door also serves the **Responses API** (same router) and **embeddings** (no router, see [docs/embeddings-mcp-policies.md](docs/embeddings-mcp-policies.md)). A short optional cell shows a local model; the full tour is chapter 09.

### 👁️ 02 · `02_all_seeing_eye_usage` – Token usage per user
Five heroes (`thor`, `loki`, `hulk`, `black-widow`, `tony-stark`) generate traffic. The notebook then queries Log Analytics (KQL):
custom metrics from `llm-emit-token-metric` (User / Model / Tier) and exact per-request tokens from the LLM gateway log.
It waits and retries because ingestion takes a few minutes. The same data is in the Azure Monitor workbook.

### 📏 03 · `03_router_prompt_length` – Router by prompt length
`router-mode = length`: short prompts go to the small model (**pym**), medium to **stark**, huge to **thanos**.
Also shows how a client can override the router with `model="tier-thanos"` or a concrete deployment. The rules in detail: [docs/routing.md](docs/routing.md).

### 🧠 04 · `04_router_complexity` – Router by prompt complexity
Two ways to judge difficulty: a free **heuristic** score in the policy (code, reasoning words, math, multi-questions) or a small
**LLM classifier** (`simple|medium|complex`, heuristic as fallback; it costs one extra model call, see [docs/routing.md](docs/routing.md#-does-the-classifier-cost-extra)). The notebook switches modes live via named values, compares
decisions, verifies them against the gateway log and ends on the default **hybrid** mode (higher of length and complexity wins).

### 🔨 05 · `05_mjolnir_token_limits` – Per-user token quotas
`llm-token-limit` gives each user a tokens-per-minute budget (small for the Avengers, large for Tony Stark). A burst from one hero gets
**HTTP 429** while the others keep working. The notebook charts allowed vs throttled calls per minute and shows whether a 429 came
from the gateway or from the model.

### 🛡️ 06 · `06_shield_mcp_via_bifrost` – S.H.I.E.L.D. tools via MCP
An MCP server written as an **Azure Function (Python)** is exposed through APIM: subscription auth, rate limiting, and the function key
injected by the gateway. The Function itself only accepts traffic from APIM's VNet subnet – the gateway is the only way in. The notebook lists the tools (`list_avengers`, `get_avenger`, `get_mission_status`, `locate_infinity_stone`) and calls them.

### 🤖 07 · `07_foundry_agent_via_bifrost` – Foundry Agent Service → Bifrost
Uses the bring-your-own-model connection to the gateway that the deployment created in the Foundry project and creates an agent on the router alias `heimdal-auto`.
Every agent LLM call now flows through HEIMDAL and is attributed to `black-widow`. *(Uses preview SDKs.)*

### 🦸 08 · `08_agent_framework_via_bifrost` – Agents with tools
The last chapter on Foundry models. A **Microsoft Agent Framework** agent (“Friday”) whose model calls *and* MCP tools both go through APIM – one key, one audit trail.
The `x-agent` header tags the traffic so the agent appears as its own `Agent` dimension in the token metrics. *(Framework evolves quickly.)*

### 🦙 09 · `09_local_ollama_via_bifrost` – Heimdall's local lane *(optional)*
A model on the **demo laptop** joins the realm. Ollama serves it, and an ngrok tunnel protected by basic auth connects it to APIM (backend `asgard-ollama`).
Clients only change the `model` name. The notebook compares local and Foundry answers side by side, streams through the tunnel, and shows that
the tunnel refuses direct calls. It also sends the whole `pym` tier to the laptop for a moment, runs a **Foundry agent** on the local model (`bifrost-apim/<model>`, attributed to `black-widow`), and finds the local calls in the token metrics and in the gateway log (`BackendId = asgard-ollama`). The same view is in the workbook section *Local Ollama via ngrok*.
*(Needs `HEIMDAL_ENABLE_OLLAMA=true` at deploy time and the laptop online. See [docs/local-ollama.md](docs/local-ollama.md).)*

## 🧰 Helper
[`notebooks/heimdal_helpers.py`](notebooks/heimdal_helpers.py) holds the shared code: `client()`, `ask()` (returns the routing headers),
`set_router()` (live named-value changes) and the KQL helpers with retry (`kql_wait()`).

## 🏁 The moral
One gateway gives you **identity** (who), **accounting** (how much), **control** (limits and routing) and **one door** for models and tools –
cloud or on your own laptop – without changing a line in the clients.
