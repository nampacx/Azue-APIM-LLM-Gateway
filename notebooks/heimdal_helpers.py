"""Shared helpers for the HEIMDAL demo notebooks (Python only)."""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

USERS = ["thor", "loki", "hulk", "black-widow", "tony-stark"]
TIER_EMOJI = {"pym": "🐜 pym", "stark": "🦾 stark", "thanos": "🫰 thanos", "explicit": "🎯 explicit"}


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} missing – run `python scripts/deploy.py` first (see 00_setup_asgard.ipynb).")
    return value


def user_key(user: str) -> str:
    return env("HEIMDAL_KEY_" + user.upper().replace("-", "_"))


def client(user: str = "thor") -> OpenAI:
    """OpenAI SDK pointed at the Bifrost gateway; the APIM subscription key identifies the user."""
    key = user_key(user)
    return OpenAI(base_url=env("HEIMDAL_LLM_BASE_URL"), api_key=key, default_headers={"api-key": key})


def ask(prompt: str, user: str = "thor", model: str = "heimdal-auto", system: str | None = None, **kw) -> dict:
    """Send one chat completion through the gateway and return answer + routing metadata."""
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
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
    df = pd.DataFrame(rows)
    return df[[c for c in ("prompt", *cols) if c in df.columns]]


# ---- APIM named values (router configuration) -------------------------------------
def _az(*args: str) -> str:
    proc = subprocess.run([shutil.which("az") or "az", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return proc.stdout


def set_router(**values: str) -> None:
    """Change router settings live, e.g. set_router(router_mode='length', length_threshold_tokens='200').

    Keys map to APIM named values with '_' -> '-'. Policy picks the change up within ~1 minute.
    """
    for k, v in values.items():
        _az("apim", "nv", "update", "--service-name", env("HEIMDAL_APIM_NAME"), "-g", env("HEIMDAL_RESOURCE_GROUP"),
            "--named-value-id", k.replace("_", "-"), "--value", str(v))
        print(f"router: {k.replace('_', '-')} = {v}")


# ---- Log Analytics ---------------------------------------------------------------
def kql(query: str, hours: int = 24) -> pd.DataFrame:
    from azure.identity import DefaultAzureCredential
    from azure.monitor.query import LogsQueryClient, LogsQueryStatus

    result = LogsQueryClient(DefaultAzureCredential()).query_workspace(
        env("HEIMDAL_LOG_WORKSPACE_ID"), query, timespan=timedelta(hours=hours))
    if result.status != LogsQueryStatus.SUCCESS:
        raise RuntimeError(result.partial_error)
    table = result.tables[0]
    return pd.DataFrame(table.rows, columns=table.columns)


def kql_wait(query: str, hours: int = 3, timeout_min: int = 12, interval_s: int = 30) -> pd.DataFrame:
    """Re-run a query until it returns rows (App Insights / Log Analytics ingestion lags by a few minutes)."""
    import time

    deadline = time.time() + timeout_min * 60
    while True:
        df = kql(query, hours=hours)
        if len(df) or time.time() > deadline:
            return df
        print(f"no rows yet – waiting {interval_s}s for ingestion...")
        time.sleep(interval_s)


# Per-user tokens from the llm-emit-token-metric custom metrics (pivot done in pandas so empty results stay readable).
KQL_TOKENS_BY_USER_METRIC = """
AppMetrics
| where Name in ("Total Tokens", "Prompt Tokens", "Completion Tokens")
| extend User = tostring(Properties["User"]), Model = tostring(Properties["Model"])
| summarize Tokens = sum(Sum) by User, Model, Name
| order by User asc
"""

# What metrics has the gateway emitted at all? (diagnostics if the query above is empty)
KQL_METRIC_NAMES = """
AppMetrics
| summarize Samples = count(), LastSeen = max(TimeGenerated) by Name
| order by LastSeen desc
"""

KQL_TOKENS_BY_USER_LLMLOG = """
ApiManagementGatewayLlmLog
| join kind=inner (ApiManagementGatewayLogs | project CorrelationId, User = ApimSubscriptionId) on CorrelationId
| summarize Requests = count(), Prompt = sum(PromptTokens), Completion = sum(CompletionTokens), Total = sum(TotalTokens)
    by User, ModelName
| order by Total desc
"""
