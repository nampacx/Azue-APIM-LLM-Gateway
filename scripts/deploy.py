"""HEIMDAL deploy script (Python only; wraps the Azure CLI).

Usage:
    python scripts/deploy.py --check-models            # list Foundry models available in swedencentral
    python scripts/deploy.py --email you@contoso.com   # deploy infra + MCP function + write .env
    python scripts/deploy.py --skip-infra              # re-publish function + refresh .env only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AZ = shutil.which("az") or "az"
APIM_API = "2025-09-01-preview"


def az(*args: str, check: bool = True, capture: bool = True) -> str:
    proc = subprocess.run([AZ, *args], capture_output=capture, text=True)
    if check and proc.returncode != 0:
        sys.exit(f"az {' '.join(args[:3])} failed:\n{proc.stderr or proc.stdout}")
    return (proc.stdout or "").strip()


def az_json(*args: str):
    return json.loads(az(*args, "-o", "json"))


def declared_models() -> list[dict]:
    """Parse the `models` array of infra/main.bicepparam (simple `key: value` objects)."""
    text = (ROOT / "infra" / "main.bicepparam").read_text(encoding="utf-8")
    block = text.split("param models = [", 1)[1].split("\n]\n", 1)[0]
    out = []
    for obj in re.findall(r"\{(.*?)\}", block, flags=re.S):
        out.append({k: v.strip("'") for k, v in re.findall(r"(\w+):\s*([^\n]+)", obj)})
    return out


def check_models(location: str) -> None:
    print(f"Models available in {location} (Foundry/AIServices):")
    try:
        available = az_json("cognitiveservices", "model", "list", "-l", location)
    except SystemExit as e:
        print(e)
        print("\nCould not list models (missing permission?). Verify in the Foundry portal instead.")
        return
    index = {(m["model"]["format"], m["model"]["name"]): m["model"]["version"] for m in available}
    for m in declared_models():
        hit = [v for (f, n), v in index.items() if n.lower() == m["model"].lower()]
        status = f"OK (versions: {', '.join(sorted(set(hit)))})" if hit else "NOT FOUND in this region/subscription"
        print(f"  {m['name']:<18} {m['model']:<18} {status}")


def deploy_infra(args) -> dict:
    az("group", "create", "-n", args.resource_group, "-l", args.location, "--tags", "project=heimdal")
    env = os.environ.copy()
    env["HEIMDAL_PUBLISHER_EMAIL"] = args.email
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


def wire_mcp_key(rg: str, app: str, apim: str) -> None:
    key = ""
    for _ in range(20):
        keys = json.loads(az("functionapp", "keys", "list", "-g", rg, "-n", app, "-o", "json", check=False) or "{}")
        key = (keys.get("systemKeys") or {}).get("mcp_extension", "")
        if key:
            break
        time.sleep(15)
    if not key:
        sys.exit("mcp_extension system key not available yet; re-run with --skip-infra in a minute.")
    az("apim", "nv", "update", "--service-name", apim, "-g", rg, "--named-value-id", "shield-mcp-key",
       "--value", key, "--secret", "true")
    print("Wired Function MCP key into APIM named value 'shield-mcp-key'.")


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
        f"HEIMDAL_FOUNDRY_PROJECT_ENDPOINT=https://{out['foundryAccountName']}.services.ai.azure.com/api/projects/{out['foundryProjectName']}",
        f"HEIMDAL_FUNCTION_APP={out['functionAppName']}",
        f"HEIMDAL_APPINSIGHTS={out['appInsightsName']}",
        f"HEIMDAL_LOG_WORKSPACE_ID={out['logAnalyticsWorkspaceId']}",
    ]
    for user in ("thor", "loki", "hulk", "black-widow", "tony-stark"):
        res = az_json("rest", "--method", "post", "--url",
                      f"{base}/subscriptions/{user}/listSecrets?api-version={APIM_API}")
        lines.append(f"HEIMDAL_KEY_{user.upper().replace('-', '_')}={res['primaryKey']}")
    (ROOT / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {ROOT / '.env'} (git-ignored). Open notebooks/00_setup_asgard.ipynb next.")


def main() -> None:
    p = argparse.ArgumentParser(description="Deploy HEIMDAL (APIM LLM/agent gateway) to Azure")
    p.add_argument("--resource-group", default="rg-heimdal-asgard-swc")
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
