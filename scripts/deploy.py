"""HEIMDAL deploy script (Python only; wraps the Azure CLI).

Usage:
    python scripts/deploy.py --check-models            # list Foundry models available in swedencentral
    python scripts/deploy.py --email you@contoso.com   # deploy infra + MCP function + write .env
    python scripts/deploy.py --skip-infra              # re-publish function + refresh .env only

    HEIMDAL_ENABLE_OLLAMA=true python scripts/deploy.py ...   # also wire a local Ollama via ngrok (scripts/ollama_tunnel.py)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import ollama_tunnel

ROOT = Path(__file__).resolve().parent.parent
AZ = shutil.which("az") or "az"
APIM_API = "2025-09-01-preview"
# Shared with infra/main.bicep (loadJsonContent) and the notebooks: models, tiers, demo users.
CONFIG = json.loads((ROOT / "infra" / "config" / "heimdal.json").read_text(encoding="utf-8"))


def az(*args: str, check: bool = True, capture: bool = True) -> str:
    proc = subprocess.run([AZ, *args], capture_output=capture, text=True)
    if check and proc.returncode != 0:
        sys.exit(f"az {' '.join(args[:3])} failed:\n{proc.stderr or proc.stdout}")
    return (proc.stdout or "").strip()


def az_json(*args: str):
    return json.loads(az(*args, "-o", "json"))


def declared_models() -> list[dict]:
    """The `models` array of infra/config/heimdal.json."""
    return CONFIG["models"]


def check_models(location: str) -> None:
    print(f"Models available in {location} (Foundry/AIServices):")
    try:
        available = az_json("cognitiveservices", "model", "list", "-l", location)
    except SystemExit as e:
        print(e)
        print("\nCould not list models (missing permission?). Verify in the Foundry portal instead.")
        return
    index: dict[str, set[str]] = {}
    for m in available:
        index.setdefault(m["model"]["name"].lower(), set()).add(m["model"]["version"])
    for m in declared_models():
        versions = index.get(m["model"].lower(), set())
        if not versions:
            status = "NOT FOUND in this region/subscription"
        elif m["version"] in versions:
            status = f"OK (version {m['version']})"
        else:
            status = f"VERSION MISMATCH: declared {m['version']}, available {', '.join(sorted(versions))}"
        print(f"  {m['name']:<18} {m['model']:<18} {status}")


def current_mcp_key(rg: str, app: str) -> str:
    """The Function's mcp_extension key, or "" (no function yet / no code published yet)."""
    out = az("functionapp", "keys", "list", "-g", rg, "-n", app, "-o", "json", check=False)
    try:
        return (json.loads(out or "{}").get("systemKeys") or {}).get("mcp_extension", "")
    except json.JSONDecodeError:
        return ""


def signed_in_principal() -> tuple[str, str]:
    """(object id, principal type) of the az login identity, for the notebook data-plane roles."""
    oid = az("ad", "signed-in-user", "show", "--query", "id", "-o", "tsv", check=False)
    if oid:
        return oid, "User"
    account = json.loads(az("account", "show", "-o", "json", check=False) or "{}")
    if (account.get("user") or {}).get("type") == "servicePrincipal":
        oid = az("ad", "sp", "show", "--id", account["user"]["name"], "--query", "id", "-o", "tsv", check=False)
        if oid:
            return oid, "ServicePrincipal"
    print("Could not resolve the signed-in principal; skipping notebook role assignments.")
    return "", "User"


def deploy_infra(args) -> dict:
    # Local Ollama via ngrok (no-op unless HEIMDAL_ENABLE_OLLAMA=true): URL, models and tunnel credentials for main.bicepparam.
    os.environ.update(ollama_tunnel.ensure())
    az("group", "create", "-n", args.resource_group, "-l", args.location, "--tags", "project=heimdal")
    env = os.environ.copy()
    env["HEIMDAL_PUBLISHER_EMAIL"] = args.email
    env["HEIMDAL_PRINCIPAL_ID"], env["HEIMDAL_PRINCIPAL_TYPE"] = signed_in_principal()
    # Redeploy: pass the current MCP key so Bicep keeps the shield-mcp-key named value intact.
    previous = az("deployment", "group", "show", "-g", args.resource_group, "-n", "heimdal-main",
                  "--query", "properties.outputs.functionAppName.value", "-o", "tsv", check=False)
    if previous:
        env["HEIMDAL_MCP_KEY"] = current_mcp_key(args.resource_group, previous)
    print("Deploying Bicep (APIM can take 15-30 minutes on first run)...")
    cmd = [AZ, "deployment", "group", "create", "-g", args.resource_group, "-n", "heimdal-main",
           "-f", str(ROOT / "infra" / "main.bicep"), "-p", str(ROOT / "infra" / "main.bicepparam"),
           "--query", "properties.outputs", "-o", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        sys.exit(f"Deployment failed:\n{proc.stderr}")
    return {k: v["value"] for k, v in json.loads(proc.stdout).items()}


def read_outputs(args) -> dict:
    out = az_json("deployment", "group", "show", "-g", args.resource_group, "-n", "heimdal-main",
                  "--query", "properties.outputs")
    return {k: v["value"] for k, v in out.items()}


def publish_function(rg: str, app: str) -> None:
    src = ROOT / "src" / "shield-mcp"
    zip_path = Path(tempfile.gettempdir()) / "shield-mcp.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in src.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts and f.name != "local.settings.json":
                z.write(f, f.relative_to(src))
    print("Publishing S.H.I.E.L.D. MCP function...")
    az("functionapp", "deployment", "source", "config-zip", "-g", rg, "-n", app,
       "--src", str(zip_path), "--build-remote", "true")


def wire_mcp_key(rg: str, app: str, apim: str) -> str:
    key = ""
    for _ in range(20):
        key = current_mcp_key(rg, app)
        if key:
            break
        time.sleep(15)
    if not key:
        sys.exit("mcp_extension system key not available yet; re-run with --skip-infra in a minute.")
    az("apim", "nv", "update", "--service-name", apim, "-g", rg, "--named-value-id", "shield-mcp-key",
       "--value", key, "--secret", "true")
    print("Wired Function MCP key into APIM named value 'shield-mcp-key'.")
    return key


def write_env(args, out: dict) -> None:
    sub = az("account", "show", "--query", "id", "-o", "tsv")
    base = (f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{args.resource_group}"
            f"/providers/Microsoft.ApiManagement/service/{out['apimName']}")
    lines = [
        f"HEIMDAL_SUBSCRIPTION_ID={sub}",
        f"HEIMDAL_RESOURCE_GROUP={args.resource_group}",
        f"HEIMDAL_LOCATION={args.location}",
        f"HEIMDAL_APIM_NAME={out['apimName']}",
        f"HEIMDAL_GATEWAY_URL={out['gatewayUrl']}",
        f"HEIMDAL_LLM_BASE_URL={out['gatewayUrl']}/bifrost/v1",
        f"HEIMDAL_MCP_URL={out['gatewayUrl']}/shield-mcp/mcp",
        f"HEIMDAL_FOUNDRY_ACCOUNT={out['foundryAccountName']}",
        f"HEIMDAL_FOUNDRY_PROJECT={out['foundryProjectName']}",
        f"HEIMDAL_FOUNDRY_CONNECTION={out.get('foundryConnectionName', 'bifrost-apim')}",
        f"HEIMDAL_FOUNDRY_PROJECT_ENDPOINT=https://{out['foundryAccountName']}.services.ai.azure.com/api/projects/{out['foundryProjectName']}",
        f"HEIMDAL_FUNCTION_APP={out['functionAppName']}",
        f"HEIMDAL_APPINSIGHTS={out['appInsightsName']}",
        f"HEIMDAL_LOG_WORKSPACE_ID={out['logAnalyticsWorkspaceId']}",
    ]
    if str(out.get("ollamaEnabled", "")).lower() == "true":
        lines.append(f"HEIMDAL_OLLAMA_MODELS={os.environ.get('HEIMDAL_OLLAMA_MODELS', '')}")
    for user in (u["name"] for u in CONFIG["demoUsers"]):
        res = az_json("rest", "--method", "post", "--url",
                      f"{base}/subscriptions/{user}/listSecrets?api-version={APIM_API}")
        lines.append(f"HEIMDAL_KEY_{user.upper().replace('-', '_')}={res['primaryKey']}")
    (ROOT / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {ROOT / '.env'} (git-ignored). Open notebooks/00_setup_asgard.ipynb next.")


def azd_hook() -> None:
    """Post-deploy hook for `azd up`: read azd environment variables, wire the MCP key and write .env."""
    e = os.environ
    args = argparse.Namespace(resource_group=e["AZURE_RESOURCE_GROUP"], location=e.get("AZURE_LOCATION", "swedencentral"))
    out = {
        "apimName": e["HEIMDAL_APIM_NAME"], "gatewayUrl": e["HEIMDAL_GATEWAY_URL"],
        "foundryAccountName": e["HEIMDAL_FOUNDRY_ACCOUNT"], "foundryProjectName": e["HEIMDAL_FOUNDRY_PROJECT"],
        "functionAppName": e["HEIMDAL_FUNCTION_APP"], "appInsightsName": e["HEIMDAL_APPINSIGHTS"],
        "logAnalyticsWorkspaceId": e["HEIMDAL_LOG_WORKSPACE_ID"],
        "foundryConnectionName": e.get("HEIMDAL_FOUNDRY_CONNECTION", "bifrost-apim"),
        "ollamaEnabled": e.get("HEIMDAL_OLLAMA_ENABLED", "false"),
    }
    key = wire_mcp_key(args.resource_group, out["functionAppName"], out["apimName"])
    # Remember the key in the azd environment: main.parameters.json passes it to Bicep on the next
    # `azd provision`, so the shield-mcp-key named value is not reset to the placeholder.
    if e.get("HEIMDAL_MCP_KEY") != key:
        subprocess.run([shutil.which("azd") or "azd", "env", "set", "HEIMDAL_MCP_KEY", key], check=True,
                       capture_output=True, text=True)
    write_env(args, out)


def main() -> None:
    p = argparse.ArgumentParser(description="Deploy HEIMDAL (APIM LLM/agent gateway) to Azure")
    p.add_argument("--resource-group", default="rg-hmdl-dev")
    p.add_argument("--location", default="swedencentral")
    p.add_argument("--email", default=os.environ.get("HEIMDAL_PUBLISHER_EMAIL", ""))
    p.add_argument("--check-models", action="store_true")
    p.add_argument("--skip-infra", action="store_true")
    args = p.parse_args()

    if args.check_models:
        check_models(args.location)
        return
    if not args.skip_infra and not args.email:
        p.error("--email is required for APIM (publisher e-mail)")

    out = read_outputs(args) if args.skip_infra else deploy_infra(args)
    publish_function(args.resource_group, out["functionAppName"])
    wire_mcp_key(args.resource_group, out["functionAppName"], out["apimName"])
    write_env(args, out)


if __name__ == "__main__":
    main()
