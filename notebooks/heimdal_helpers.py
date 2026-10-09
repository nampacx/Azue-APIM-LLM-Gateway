"""Shared helpers for the HEIMDAL demo notebooks (Python only).

What this module is
-------------------
A thin client-side toolbox used by every notebook in ``notebooks/``. It does NOT contain any routing
logic - the routing, token limiting and metric emission all happen inside Azure API Management
(policy ``policies/bifrost-router.xml``). This module only:

1. talks to the APIM gateway like any OpenAI client would       -> ``client()``, ``ask()``, ``respond()``, ``embed()``,
                                                                   ``ask_all()``, ``generate_traffic()``, ``burst()``
2. changes the router's live configuration (APIM named values)    -> ``set_router()``
3. queries Log Analytics to show who used how many tokens         -> ``kql()``, ``kql_wait()``, ``KQL_*``,
                                                                   ``token_metrics_by_user()``, ``check_llm_log()``, ``throttling_*()``
4. wires MCP / Foundry / Agent Framework clients to the gateway   -> ``mcp_calls()``, ``foundry_*()``, ``agent_framework_ask()``

Notebook cells keep only the parameters of their scenario (prompts, users, thresholds ...) and call these.

How the pieces fit together
---------------------------
::

    notebook --ask()--> APIM /bifrost/v1/chat/completions --(policy picks model)--> Foundry
                              |                                                        |
                              +-- x-heimdal-* response headers  <----------------------+
                              +-- token metrics/logs --> App Insights / Log Analytics <-- kql()

    notebook --set_router()--> `az apim nv update` --> APIM named values (read by the policy)

See ``docs/apim-integration.md`` for the full explanation of the APIM side.

Configuration (environment)
---------------------------
Values come from the ``.env`` file in the repo root, which ``scripts/deploy.py`` (or the azd postdeploy
hook ``scripts/azd_hook.py``) writes after deployment. It is loaded below with ``override=True`` so the
file wins over stale shell variables. Variables used here:

==============================  ===============================================================
``HEIMDAL_LLM_BASE_URL``        Gateway base URL, e.g. ``https://<apim>.azure-api.net/bifrost/v1``
``HEIMDAL_KEY_<USER>``          APIM subscription key per demo user (``THOR``, ``BLACK_WIDOW`` ...)
``HEIMDAL_APIM_NAME``           APIM instance name (needed by ``set_router``)
``HEIMDAL_RESOURCE_GROUP``      Resource group of the APIM instance (needed by ``set_router``)
``HEIMDAL_LOG_WORKSPACE_ID``    Log Analytics workspace GUID (needed by ``kql``)
==============================  ===============================================================
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI

# .env lives one level above notebooks/. override=True: the deployed values must win over anything
# already exported in the shell, otherwise a notebook can silently talk to an old deployment.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

# Demo users. Each one is an APIM *subscription* with the same name (see infra/modules/apim-apis.bicep);
# the subscription key is the user's identity for token limits and token metrics.
# Same list Bicep deploys: infra/config/heimdal.json (demoUsers).
CONFIG = json.loads((Path(__file__).resolve().parent.parent / "infra" / "config" / "heimdal.json").read_text(encoding="utf-8"))
USERS = [u["name"] for u in CONFIG["demoUsers"]]

# Display labels for the ``x-heimdal-tier`` response header. "explicit" means the caller named a
# concrete deployment (or tier) instead of letting the router choose.
TIER_EMOJI = {"pym": "🐜 pym", "stark": "🦾 stark", "thanos": "🫰 thanos", "explicit": "🎯 explicit"}


def env(name: str) -> str:
    """Return a required environment variable or fail with a hint on how to create it.

    Raises:
        RuntimeError: if the variable is unset/empty (usually means the deployment has not run yet,
        so ``.env`` does not exist).
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} missing – run `python scripts/deploy.py` first (see 00_setup_asgard.ipynb).")
    return value


def user_key(user: str) -> str:
    """APIM subscription key of a demo user, read from ``HEIMDAL_KEY_<USER>`` (``black-widow`` -> ``BLACK_WIDOW``)."""
    return env("HEIMDAL_KEY_" + user.upper().replace("-", "_"))


def config_summary() -> None:
    """Print the loaded ``HEIMDAL_*`` settings (keys hidden) and which demo users have a key."""
    print({k: v for k, v in os.environ.items() if k.startswith("HEIMDAL_") and "KEY" not in k})
    print("users with keys:", [u for u in USERS if os.environ.get("HEIMDAL_KEY_" + u.upper().replace("-", "_"))])


# ---- Calling the gateway ---------------------------------------------------------------
def client(user: str = "thor") -> OpenAI:
    """OpenAI SDK pointed at the Bifrost gateway; the APIM subscription key identifies the user.

    The key is sent twice on purpose:
      * ``api_key``          -> the SDK turns it into ``Authorization: Bearer <key>`` (required by the SDK, ignored by APIM)
      * ``api-key`` header   -> the header APIM actually reads for the subscription
        (``subscriptionKeyParameterNames.header = 'api-key'`` in infra/modules/apim-apis.bicep)

    The gateway never forwards this key to Foundry; the policy deletes it and authenticates to the
    backend with APIM's managed identity instead.
    """
    key = user_key(user)
    return OpenAI(base_url=env("HEIMDAL_LLM_BASE_URL"), api_key=key, default_headers={"api-key": key})


def ask(prompt: str, user: str = "thor", model: str = "heimdal-auto", system: str | None = None, **kw) -> dict:
    """Send one chat completion through the gateway and return answer + routing metadata.

    Args:
        prompt: user message text.
        user: which demo user (APIM subscription) makes the call; drives token limits and metrics.
        model: what to ask the router for:
            * ``"heimdal-auto"`` (default) / ``"auto"`` / omitted -> router decides (length / complexity / hybrid)
            * ``"tier-pym"`` | ``"tier-stark"`` | ``"tier-thanos"`` -> force a tier, router maps it to a deployment
            * any other string -> used as the Foundry deployment name as-is (tier ``explicit``)
        system: optional system message (counts towards prompt length / complexity too).
        **kw: passed through to ``chat.completions.create`` (``max_tokens``, ``temperature`` ...).
            ``max_retries`` is taken out and set on the client instead: the SDK retries a 429 on its own
            (honouring ``Retry-After``), so pass ``max_retries=0`` to see the gateway's throttling.

    Returns:
        dict with the routing decision (read from the ``x-heimdal-*`` response headers that the APIM
        outbound policy sets) plus the model output:

        ``user``              APIM subscription name that made the call
        ``tier``              ``pym`` | ``stark`` | ``thanos`` | ``explicit``
        ``model``             Foundry deployment that really served the request
        ``reason``            why the router chose it (mode, estimated tokens, complexity score ...)
        ``remaining_tokens``  tokens left in this user's per-minute budget (``llm-token-limit``)
        ``prompt_tokens`` / ``completion_tokens``  usage reported by the model
        ``answer``            the assistant message text

    Raises:
        openai.RateLimitError: HTTP 429 when the user's tokens-per-minute limit is exhausted.
    """
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    # with_raw_response gives access to HTTP headers, which the plain SDK return value hides.
    c = client(user).with_options(max_retries=kw.pop("max_retries", 2))
    raw = c.chat.completions.with_raw_response.create(model=model, messages=messages, **kw)
    resp = raw.parse()
    return {
        **_route(raw.headers),
        "prompt_tokens": resp.usage.prompt_tokens if resp.usage else None,
        "completion_tokens": resp.usage.completion_tokens if resp.usage else None,
        "answer": resp.choices[0].message.content,
    }


def _route(h) -> dict:
    """Routing decision from the ``x-heimdal-*`` response headers set by the APIM outbound policy."""
    return {
        "user": h.get("x-heimdal-user"),
        "tier": h.get("x-heimdal-tier"),
        "model": h.get("x-heimdal-model"),
        "reason": h.get("x-heimdal-route-reason"),
        "remaining_tokens": h.get("x-heimdal-remaining-tokens"),
    }


def respond(input: str, user: str = "thor", model: str = "heimdal-auto", **kw) -> dict:
    """Like ``ask()`` but via the Responses API (``POST /responses``) - same router, same headers."""
    raw = client(user).responses.with_raw_response.create(model=model, input=input, **kw)
    resp = raw.parse()
    return {
        **_route(raw.headers),
        "prompt_tokens": resp.usage.input_tokens if resp.usage else None,
        "completion_tokens": resp.usage.output_tokens if resp.usage else None,
        "answer": resp.output_text,
    }


def embed(inputs: list[str], user: str = "thor", model: str = "heimdal-auto") -> dict:
    """Embed ``inputs`` through the gateway (``POST /embeddings``; no router - ``heimdal-auto`` maps to ``model-embedding``)."""
    raw = client(user).embeddings.with_raw_response.create(model=model, input=inputs)
    data = raw.parse().data
    return {"model": raw.headers.get("x-heimdal-model"), "vectors": len(data),
            "dims": len(data[0].embedding) if data else 0}


def ask_all(prompts: dict[str, str], user: str = "thor", **kw) -> list[dict]:
    """``ask()`` every prompt of ``{label: text}``; rows carry ``prompt`` (= label) and ``text`` for ``show()``."""
    return [{"prompt": label, "text": text, **ask(text, user=user, **kw)} for label, text in prompts.items()]


def generate_traffic(prompts: list[str], users: list[str] = USERS, calls_per_user: tuple[int, int] = (2, 4)) -> list[dict]:
    """Every user sends a random number (``calls_per_user`` = min, max) of randomly picked prompts."""
    import random

    rows = []
    for user in users:
        for _ in range(random.randint(*calls_per_user)):
            p = random.choice(prompts)
            rows.append({"prompt": p[:40], **ask(p, user=user)})
    return rows


def burst(user: str, n: int = 40, prompt: str = "Write a 150-word story about Mjolnir.",
          max_completion_tokens: int = 300, workers: int = 20) -> dict:
    """Fire ``n`` calls for one user in parallel and count how many the token limit let through.

    Parallel so the calls land within one minute (one-by-one they spread over several minutes and the
    per-minute budget refills in between). ``max_retries=0`` because the SDK would otherwise silently
    wait out and retry a 429.
    """
    import openai
    from concurrent.futures import ThreadPoolExecutor

    def one(_):
        try:
            return ask(prompt, user=user, max_completion_tokens=max_completion_tokens, max_retries=0)["remaining_tokens"]
        except openai.RateLimitError:
            return "429"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, range(n)))
    ok = [int(r) for r in results if r not in ("429", None)]
    return {"user": user, "ok": len(results) - results.count("429"), "throttled(429)": results.count("429"),
            "lowest_budget_left": min(ok, default=None)}   # tokens left in the per-minute budget


def show(rows: list[dict], cols=("tier", "model", "prompt_tokens", "completion_tokens", "reason")) -> pd.DataFrame:
    """Turn a list of ``ask()`` results into a compact table for notebook display.

    Add a ``"prompt"`` key to each row (e.g. ``{**ask(p), "prompt": p}``) to get the prompt as first
    column. Columns that are not present in the rows are skipped instead of raising.
    """
    df = pd.DataFrame(rows)
    return df[[c for c in ("prompt", *cols) if c in df.columns]]


# ---- APIM named values (router configuration) -------------------------------------
def _az(*args: str) -> str:
    """Run an Azure CLI command and return stdout; raise with stderr on a non-zero exit.

    ``shutil.which`` is needed on Windows where ``az`` is ``az.cmd`` and ``subprocess`` would not
    find it by bare name. Requires a prior ``az login``.
    """
    proc = subprocess.run([shutil.which("az") or "az", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return proc.stdout


def set_router(wait_s: int = 0, **values: str) -> None:
    """Change router settings live, e.g. set_router(router_mode='length', length_threshold_tokens='200').

    Keys map to APIM named values with '_' -> '-'. Policy picks the change up within ~1 minute, so pass
    ``wait_s=60`` to sleep until the next call sees the new setting.

    The named values are the knobs the policy reads as ``{{name}}`` (see ``policies/bifrost-router.xml``
    and the table in README.md): ``router-mode``, ``complexity-mode``, ``length-threshold-tokens``,
    ``complexity-threshold``, ``model-tier-pym|stark|thanos``, ``model-classifier``, ``tpm-standard``,
    ``tpm-premium`` ...  No redeploy is needed - that is the point of using named values.

    Needs ``az login`` with rights to update the APIM instance. Values are always sent as strings.
    """
    for k, v in values.items():
        _az("apim", "nv", "update", "--service-name", env("HEIMDAL_APIM_NAME"), "-g", env("HEIMDAL_RESOURCE_GROUP"),
            "--named-value-id", k.replace("_", "-"), "--value", str(v))
        print(f"router: {k.replace('_', '-')} = {v}")
    if wait_s:
        import time

        print(f"waiting {wait_s}s for named value propagation...")
        time.sleep(wait_s)


# ---- Log Analytics ---------------------------------------------------------------
def kql(query: str, hours: int = 24) -> pd.DataFrame:
    """Run a KQL query against the gateway's Log Analytics workspace and return a DataFrame.

    Authenticates with ``DefaultAzureCredential`` (``az login`` is enough locally). The Azure SDK
    imports are local so notebooks that never query logs do not need those packages loaded.

    Args:
        query: KQL text (see the ``KQL_*`` constants below for ready-made ones).
        hours: look-back window (``timespan``).

    Raises:
        RuntimeError: if the query only partially succeeded (the partial error is included).
    """
    from azure.identity import DefaultAzureCredential
    from azure.monitor.query import LogsQueryClient, LogsQueryStatus

    result = LogsQueryClient(DefaultAzureCredential()).query_workspace(
        env("HEIMDAL_LOG_WORKSPACE_ID"), query, timespan=timedelta(hours=hours))
    if result.status != LogsQueryStatus.SUCCESS:
        raise RuntimeError(result.partial_error)
    table = result.tables[0]
    return pd.DataFrame(table.rows, columns=table.columns)


def kql_wait(query: str, hours: int = 3, timeout_min: int = 12, interval_s: int = 30, min_rows: int = 1) -> pd.DataFrame:
    """Re-run a query until it returns rows (App Insights / Log Analytics ingestion lags by a few minutes).

    Returns the (possibly incomplete) result once ``min_rows`` rows appear or ``timeout_min`` has passed,
    so callers can still show an empty table plus diagnostics instead of an exception.
    """
    import time

    deadline = time.time() + timeout_min * 60
    while True:
        df = kql(query, hours=hours)
        if len(df) >= min_rows or time.time() > deadline:
            return df
        print(f"{len(df)}/{min_rows} rows yet – waiting {interval_s}s for ingestion...")
        time.sleep(interval_s)


# ---- Log analysis used by the notebooks -------------------------------------------------
def token_metrics_by_user(hours: int = 3) -> pd.DataFrame:
    """Per-user tokens from the custom metrics, pivoted User x metric name (waits for ingestion).

    If nothing arrived in time, prints that and returns the list of metrics that exist at all instead.
    """
    df = kql_wait(KQL_TOKENS_BY_USER_METRIC, hours=hours)
    if df.empty:
        print("Still empty. Which metrics exist at all?")
        return kql(KQL_METRIC_NAMES, hours=hours * 2)
    return df.pivot_table(index="User", columns="Name", values="Tokens", aggfunc="sum").fillna(0)


def check_llm_log(rows: list[dict], user: str, timeout_min: int = 12, interval_s: int = 30) -> pd.DataFrame:
    """Look ``ask_all()`` results up in the APIM LLM gateway log: which model really served which prompt?

    The log holds no prompt text, so each log row is matched to its prompt by user + prompt token count
    (the client gets the same count back in the response). Logs lag a few minutes, so this polls.
    """
    tokens = {r["prompt_tokens"] for r in rows}
    logs = kql_wait(f"""
    ApiManagementGatewayLlmLog
    | where TimeGenerated > ago(1h)
    | join kind=inner (ApiManagementGatewayLogs | project CorrelationId, User = ApimSubscriptionId) on CorrelationId
    | where User == '{user}' and PromptTokens in ({', '.join(str(t) for t in tokens)})
    | summarize arg_max(TimeGenerated, CompletionTokens, ModelName) by PromptTokens
    | project TimeGenerated, PromptTokens, CompletionTokens, LogModel = ModelName
    """, hours=1, timeout_min=timeout_min, interval_s=interval_s, min_rows=len(tokens))
    df = pd.DataFrame(rows).rename(columns={"model": "header_model"})
    df = df.merge(logs.rename(columns={"PromptTokens": "prompt_tokens"}), on="prompt_tokens", how="left")
    # the log reports model name + version (gpt-6.1-sol-2026-09-29); the header has the deployment name (gpt-6-1-sol)
    df["match"] = [str(l).replace(".", "-").startswith(h) for h, l in zip(df["header_model"], df["LogModel"])]
    return df[["prompt", "tier", "header_model", "LogModel", "match", "prompt_tokens", "CompletionTokens", "reason"]]


def throttling_by_user(minutes: int = 5) -> pd.DataFrame:
    """Allowed vs 429 vs other responses per user in the last ``minutes`` (waits for ingestion)."""
    return kql_wait(f"""
    ApiManagementGatewayLogs
    | where TimeGenerated > ago({minutes}m)
    | summarize Requests = count(),
                Allowed = countif(ResponseCode == 200),
                Throttled_429 = countif(ResponseCode == 429),
                Other = countif(ResponseCode !in (200, 429))
        by User = ApimSubscriptionId
    | order by Requests desc
    """, hours=1)


def throttling_per_minute(minutes: int = 15, plot: bool = True) -> pd.DataFrame:
    """Allowed vs 429 per user and minute over the last ``minutes``; optionally as a bar chart."""
    df = kql(f"""
    ApiManagementGatewayLogs
    | where TimeGenerated > ago({minutes}m)
    | summarize Allowed = countif(ResponseCode == 200), Throttled_429 = countif(ResponseCode == 429)
        by User = ApimSubscriptionId, Minute = bin(TimeGenerated, 1m)
    | order by Minute asc
    """, hours=1)
    if plot and len(df):
        df.pivot_table(index="Minute", columns="User", values=["Allowed", "Throttled_429"], fill_value=0) \
          .plot.bar(figsize=(10, 4), title="Allowed vs throttled per minute")
    return df


def throttling_source(minutes: int = 15) -> pd.DataFrame:
    """Who sent the 429s? Empty ``BackendResponseCode`` = the gateway's token limit (never reached the
    model); ``BackendResponseCode == 429`` = the Foundry deployment itself."""
    return kql(f"""
    ApiManagementGatewayLogs
    | where TimeGenerated > ago({minutes}m) and ResponseCode == 429
    | summarize Count = count() by User = ApimSubscriptionId, BackendResponseCode
    """, hours=1)


# ---- MCP and agents through the gateway ----------------------------------------------------
async def mcp_calls(calls: list[tuple[str, dict]], user: str = "thor") -> None:
    """List the S.H.I.E.L.D. MCP tools behind APIM and call each ``(tool, arguments)`` in ``calls``.

    The APIM subscription key in ``api-key`` is the only credential; the gateway injects the function key.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(env("HEIMDAL_MCP_URL"), headers={"api-key": user_key(user)}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("tools:", [t.name for t in (await s.list_tools()).tools])
            for tool, args in calls:
                res = await s.call_tool(tool, args)
                print(f"{tool}({args}) -> {res.content[0].text}")


def foundry_project():
    """Foundry project client + the ``ApiManagement`` connection to the gateway (created by Bicep with the
    APIM key of the agent user, see infra/modules/foundry-connection.bicep)."""
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    project = AIProjectClient(endpoint=env("HEIMDAL_FOUNDRY_PROJECT_ENDPOINT"), credential=DefaultAzureCredential())
    return project, project.connections.get(env("HEIMDAL_FOUNDRY_CONNECTION"))


def foundry_agent_ask(project, question: str, agent_name: str, instructions: str, model: str = "heimdal-auto") -> str:
    """Create (a new version of) a Foundry prompt agent on ``<gateway connection>/<model>`` and ask it once."""
    from azure.ai.projects.models import PromptAgentDefinition

    agent = project.agents.create_version(
        agent_name=agent_name,
        definition=PromptAgentDefinition(model=f"{env('HEIMDAL_FOUNDRY_CONNECTION')}/{model}", instructions=instructions))
    resp = project.get_openai_client().responses.create(
        input=question, extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}})
    return resp.output_text


async def agent_framework_ask(question: str, instructions: str, user: str = "tony-stark", agent_tag: str = "friday",
                              name: str = "Agent", model: str = "heimdal-auto", **options) -> str:
    """Run a Microsoft Agent Framework agent once: LLM via the gateway's OpenAI endpoint, tools from the
    S.H.I.E.L.D. MCP server via APIM. Both use the same APIM key plus the ``x-agent`` header, which the
    gateway uses as the ``Agent`` metric dimension. ``options`` -> ``default_options`` (e.g. ``reasoning_effort``).
    """
    from agent_framework import Agent, MCPStreamableHTTPTool
    from agent_framework.openai import OpenAIChatCompletionClient
    from openai import AsyncOpenAI

    headers = {"api-key": user_key(user), "x-agent": agent_tag}
    llm = OpenAIChatCompletionClient(model=model, async_client=AsyncOpenAI(
        base_url=env("HEIMDAL_LLM_BASE_URL"), api_key=headers["api-key"], default_headers=headers))
    shield = MCPStreamableHTTPTool(name="shield", url=env("HEIMDAL_MCP_URL"), static_headers=headers)
    async with Agent(client=llm, name=name, tools=shield, default_options=options, instructions=instructions) as agent:
        return (await agent.run(question)).text


# ---- Ready-made KQL queries ---------------------------------------------------------
# Two independent sources of per-user token usage exist; notebook 02 shows both:
#   1. custom metrics   -> emitted by <llm-emit-token-metric> in the policy, land in App Insights (AppMetrics)
#   2. LLM gateway logs -> written by APIM's diagnostic setting (largeLanguageModel.logs), land in
#                          ApiManagementGatewayLlmLog; exact per request, no prompt contents.

# Per-user tokens from the llm-emit-token-metric custom metrics (pivot done in pandas so empty results stay readable).
# "User" / "Model" are the dimensions set in the policy; Name is the metric (Total/Prompt/Completion Tokens).
KQL_TOKENS_BY_USER_METRIC = """
AppMetrics
| where Name in ("Total Tokens", "Prompt Tokens", "Completion Tokens")
| extend User = tostring(Properties["User"]), Model = tostring(Properties["Model"])
| summarize Tokens = sum(Sum) by User, Model, Name
| order by User asc
"""

# Same metrics, split by the Agent dimension (the x-agent request header; "none" when a client sends no tag).
KQL_TOKENS_BY_AGENT_METRIC = """
AppMetrics
| where Name in ("Total Tokens", "Prompt Tokens", "Completion Tokens")
| extend User = tostring(Properties["User"]), Agent = tostring(Properties["Agent"]), Model = tostring(Properties["Model"])
| summarize Tokens = sum(Sum) by Agent, User, Model, Name
| order by Agent asc
"""

# What metrics has the gateway emitted at all? (diagnostics if the query above is empty)
KQL_METRIC_NAMES = """
AppMetrics
| summarize Samples = count(), LastSeen = max(TimeGenerated) by Name
| order by LastSeen desc
"""

# Per-user tokens from the LLM gateway log. The LLM log has no user column, so it is joined to the
# general gateway log via CorrelationId, which carries the APIM subscription name (= demo user).
KQL_TOKENS_BY_USER_LLMLOG = """
ApiManagementGatewayLlmLog
| join kind=inner (ApiManagementGatewayLogs | project CorrelationId, User = ApimSubscriptionId) on CorrelationId
| summarize Requests = count(), Prompt = sum(PromptTokens), Completion = sum(CompletionTokens), Total = sum(TotalTokens)
    by User, ModelName
| order by Total desc
"""
