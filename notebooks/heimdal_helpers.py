"""Shared helpers for the HEIMDAL demo notebooks (Python only).

What this module is
-------------------
A thin client-side toolbox used by every notebook in ``notebooks/``. It does NOT contain any routing
logic - the routing, token limiting and metric emission all happen inside Azure API Management
(policy ``policies/bifrost-router.xml``). This module only:

1. talks to the APIM gateway like any OpenAI client would       -> ``client()``, ``ask()``
2. changes the router's live configuration (APIM named values)    -> ``set_router()``
3. queries Log Analytics to show who used how many tokens         -> ``kql()``, ``kql_wait()``, ``KQL_*``

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
    raw = client(user).chat.completions.with_raw_response.create(model=model, messages=messages, **kw)
    h, resp = raw.headers, raw.parse()
    return {
        "user": h.get("x-heimdal-user"),
        "tier": h.get("x-heimdal-tier"),
        "model": h.get("x-heimdal-model"),
        "reason": h.get("x-heimdal-route-reason"),
        "remaining_tokens": h.get("x-heimdal-remaining-tokens"),
        "prompt_tokens": resp.usage.prompt_tokens if resp.usage else None,
        "completion_tokens": resp.usage.completion_tokens if resp.usage else None,
        "answer": resp.choices[0].message.content,
    }


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


def set_router(**values: str) -> None:
    """Change router settings live, e.g. set_router(router_mode='length', length_threshold_tokens='200').

    Keys map to APIM named values with '_' -> '-'. Policy picks the change up within ~1 minute.

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


def kql_wait(query: str, hours: int = 3, timeout_min: int = 12, interval_s: int = 30) -> pd.DataFrame:
    """Re-run a query until it returns rows (App Insights / Log Analytics ingestion lags by a few minutes).

    Returns the (possibly empty) result once rows appear or ``timeout_min`` has passed, so callers
    can still show an empty table plus diagnostics instead of an exception.
    """
    import time

    deadline = time.time() + timeout_min * 60
    while True:
        df = kql(query, hours=hours)
        if len(df) or time.time() > deadline:
            return df
        print(f"no rows yet – waiting {interval_s}s for ingestion...")
        time.sleep(interval_s)


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
