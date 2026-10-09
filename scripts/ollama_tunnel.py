"""Heimdal's local lane: a model on your own machine (Ollama), exposed to APIM through an ngrok tunnel.

Runs as the azd `preprovision` hook (and from scripts/deploy.py) when HEIMDAL_ENABLE_OLLAMA=true:
  1. Ollama installed?  serving on localhost:11434?  (starts `ollama serve` if not)  model(s) pulled?  (pulls if not)
  2. ngrok installed + authtoken configured?  tunnel to :11434 running?  (starts one if not)
     The tunnel is protected by an ngrok traffic policy: basic-auth (secret generated once, kept in the azd env)
     and a Host rewrite to localhost (Ollama only accepts local Host headers).
  3. Verifies the tunnel end to end (401 without credentials, model list with credentials) and stores
     HEIMDAL_OLLAMA_URL / HEIMDAL_OLLAMA_MODELS / HEIMDAL_OLLAMA_AUTH in the azd env -> Bicep backend `asgard-ollama`.

Usage:
    python scripts/ollama_tunnel.py           # ensure everything (no-op unless HEIMDAL_ENABLE_OLLAMA=true)
    python scripts/ollama_tunnel.py --stop    # stop the ollama / ngrok processes this script started

Settings (azd env or environment):
    HEIMDAL_ENABLE_OLLAMA        true to enable (default false)
    HEIMDAL_OLLAMA_MODEL         model(s) to serve, comma-separated (default gemma4:e4b, served as gemma4-e4b)
    HEIMDAL_OLLAMA_NGROK_DOMAIN  optional static ngrok domain, e.g. my-name.ngrok-free.app
"""
from __future__ import annotations

import argparse
import base64
import functools
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLLAMA_PORT = 11434
OLLAMA_LOCAL = f"http://localhost:{OLLAMA_PORT}"
NGROK_API_PORTS = range(4040, 4046)  # each ngrok agent takes the next free inspection port
DEFAULT_MODEL = "gemma4:e4b"  # small, no reasoning mode: answers directly
AUTH_USER = "heimdal"


def log(msg: str) -> None:
    print(f"[ollama-tunnel] {msg}", flush=True)


def fail(msg: str) -> None:
    sys.exit(f"[ollama-tunnel] ERROR: {msg}")


# ---- settings / state ------------------------------------------------------
def azd_project() -> bool:
    """True inside an initialised azd project (hooks always get AZURE_ENV_NAME)."""
    return bool(shutil.which("azd")) and (bool(os.environ.get("AZURE_ENV_NAME")) or (ROOT / ".azure" / "config.json").exists())


@functools.cache
def azd_values() -> dict[str, str]:
    """azd injects env values into hooks; `azd env get-values` covers manual runs inside an azd project."""
    if not azd_project():
        return {}
    proc = subprocess.run([shutil.which("azd"), "env", "get-values"], capture_output=True, text=True, cwd=ROOT)
    values = {}
    for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
        k, sep, v = line.partition("=")
        if sep:
            values[k.strip()] = v.strip().strip('"')
    return values


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name) or azd_values().get(name) or default


def azd_set(name: str, value: str) -> None:
    if not azd_project() or azd_values().get(name) == value:
        return
    proc = subprocess.run([shutil.which("azd"), "env", "set", name, value], capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        fail(f"`azd env set {name}` failed: {proc.stderr or proc.stdout}")


def state_dir() -> Path:
    """Logs, generated ngrok policy and PIDs: .azure/<env>/ under azd, else .ollama-tunnel/ (both git-ignored)."""
    env = setting("AZURE_ENV_NAME") if azd_project() else ""
    d = ROOT / ".azure" / env if env else ROOT / ".ollama-tunnel"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state() -> dict:
    f = state_dir() / "ollama-tunnel.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def save_state(state: dict) -> None:
    (state_dir() / "ollama-tunnel.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---- helpers ---------------------------------------------------------------
def http(url: str, auth: str | None = None, timeout: float = 5) -> tuple[int, str]:
    """(status, body); status 0 = not reachable."""
    req = urllib.request.Request(url, headers={"ngrok-skip-browser-warning": "1"})
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return 0, ""


def start_detached(cmd: list[str], logfile: Path) -> int:
    """Start a background process that outlives this hook; output goes to logfile."""
    out = open(logfile, "ab")
    kwargs: dict = {"stdout": out, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "cwd": ROOT}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs).pid


def tail(logfile: Path, lines: int = 15) -> str:
    if not logfile.exists():
        return ""
    return "\n".join(logfile.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def wait_for(check, seconds: int):
    deadline = time.time() + seconds
    while time.time() < deadline:
        result = check()
        if result:
            return result
        time.sleep(1)
    return None


# ---- Ollama ----------------------------------------------------------------
def ollama_models() -> list[str] | None:
    status, body = http(f"{OLLAMA_LOCAL}/api/tags")
    if status != 200:
        return None
    return [m["name"] for m in json.loads(body).get("models", [])]


def has_model(installed: list[str], model: str) -> bool:
    wanted = model if ":" in model else f"{model}:latest"
    return wanted in installed or model in installed


def gateway_name(model: str) -> str:
    """Name the gateway (and Foundry agents) use: Foundry model ids only allow letters, digits, '-', '_' and '.',
    so 'gemma4:e4b' becomes 'gemma4-e4b'. 'llama3.2' / 'llama3.2:latest' stay 'llama3.2'."""
    name, _, tag = model.partition(":")
    return name if tag in ("", "latest") else f"{name}-{tag}"


def ensure_ollama(models: list[str], state: dict) -> list[str]:
    """Ollama serving + models pulled. Returns the gateway names (Foundry-safe aliases created with `ollama cp`)."""
    exe = shutil.which("ollama")
    if not exe:
        fail("Ollama is not installed. Install it from https://ollama.com/download "
             "(Windows: `winget install Ollama.Ollama`, macOS: `brew install ollama`, "
             "Linux: `curl -fsSL https://ollama.com/install.sh | sh`).")
    installed = ollama_models()
    if installed is None:
        log(f"Ollama is not serving on {OLLAMA_LOCAL} - starting `ollama serve`...")
        logfile = state_dir() / "ollama.log"
        state["ollamaPid"] = start_detached([exe, "serve"], logfile)
        save_state(state)
        wait_for(lambda: ollama_models() is not None, 30)
        installed = ollama_models()
        if installed is None:
            fail(f"Ollama did not come up within 30 s. Log ({logfile}):\n{tail(logfile)}")
    log(f"Ollama is serving on {OLLAMA_LOCAL}.")
    for model in models:
        if has_model(installed, model):
            log(f"Model '{model}' is available.")
            continue
        log(f"Model '{model}' not found locally - running `ollama pull {model}`...")
        if subprocess.run([exe, "pull", model]).returncode != 0:
            fail(f"`ollama pull {model}` failed. Check the model name at https://ollama.com/library.")
    # Foundry-safe aliases: `ollama cp` only adds a name for the same weights (no download, no extra disk).
    installed = ollama_models() or []
    names = []
    for model in models:
        alias = gateway_name(model)
        if alias != model.partition(":")[0] and not has_model(installed, alias):
            if subprocess.run([exe, "cp", model, alias], capture_output=True, text=True).returncode != 0:
                fail(f"`ollama cp {model} {alias}` failed.")
            log(f"Alias '{alias}' -> '{model}' created (Foundry model names cannot contain ':').")
        names.append(alias)
    return names


# ---- ngrok -----------------------------------------------------------------
def ensure_ngrok_installed() -> str:
    exe = shutil.which("ngrok")
    if not exe:
        fail("ngrok is not installed. Install it from https://ngrok.com/download "
             "(Windows: `winget install ngrok.ngrok`, macOS: `brew install ngrok`), then "
             "`ngrok config add-authtoken <token>` (token: https://dashboard.ngrok.com/get-started/your-authtoken).")
    proc = subprocess.run([exe, "config", "check"], capture_output=True, text=True)
    if proc.returncode != 0:
        fail(f"`ngrok config check` failed:\n{proc.stdout}{proc.stderr}")
    # A missing authtoken shows up when the agent starts (ERR_NGROK_4018); the config file path is not reliable
    # to read directly (the Microsoft Store build virtualizes it).
    return exe


def find_tunnel() -> str | None:
    """Public URL of a running ngrok tunnel that forwards to the Ollama port, if any."""
    for port in NGROK_API_PORTS:
        status, body = http(f"http://127.0.0.1:{port}/api/tunnels", timeout=2)
        if status != 200:
            continue
        for t in json.loads(body).get("tunnels", []):
            addr = str((t.get("config") or {}).get("addr", ""))
            if addr.rstrip("/").endswith(f":{OLLAMA_PORT}") and t.get("public_url", "").startswith("https://"):
                return t["public_url"].rstrip("/")
    return None


def write_policy(auth: str) -> Path:
    """ngrok traffic policy: basic-auth first, then rewrite Host so Ollama accepts the request."""
    policy = state_dir() / "ngrok-ollama.yml"
    policy.write_text(
        "# Generated by scripts/ollama_tunnel.py - contains the tunnel credentials, do not commit.\n"
        "on_http_request:\n"
        "  - actions:\n"
        "      - type: basic-auth\n"
        "        config:\n"
        "          credentials:\n"
        f"            - \"{auth}\"\n"
        "          enforce: true\n"
        "      - type: add-headers\n"
        "        config:\n"
        "          headers:\n"
        "            host: localhost\n",
        encoding="utf-8",
    )
    return policy


def ensure_tunnel(exe: str, auth: str, domain: str, state: dict) -> str:
    url = find_tunnel()
    if url:
        log(f"Found running ngrok tunnel {url} -> :{OLLAMA_PORT}.")
        if domain and domain not in url:
            log(f"Note: HEIMDAL_OLLAMA_NGROK_DOMAIN={domain} is set, but the running tunnel uses {url}.")
        return url
    log("No ngrok tunnel to Ollama - starting `ngrok http` with the Heimdal traffic policy...")
    cmd = [exe, "http", str(OLLAMA_PORT), "--traffic-policy-file", str(write_policy(auth)), "--log", "stdout"]
    if domain:
        cmd += ["--url", domain if domain.startswith("http") else f"https://{domain}"]
    logfile = state_dir() / "ngrok.log"
    state["ngrokPid"] = start_detached(cmd, logfile)
    save_state(state)
    url = wait_for(find_tunnel, 30)
    if not url and "ERR_NGROK_4018" in tail(logfile, 50):
        fail("ngrok has no authtoken. Run `ngrok config add-authtoken <token>` "
             "(token: https://dashboard.ngrok.com/get-started/your-authtoken), then re-run.")
    if not url:
        fail("ngrok did not open a tunnel within 30 s (free plan: only one agent may be online - stop other "
             f"ngrok sessions). Log ({logfile}):\n{tail(logfile)}")
    log(f"ngrok tunnel {url} -> :{OLLAMA_PORT} is up.")
    return url


def verify_tunnel(url: str, auth: str, models: list[str]) -> None:
    status, _ = http(f"{url}/v1/models", timeout=15)
    if status != 401:
        fail(f"The tunnel {url} answered {status or 'nothing'} without credentials (expected 401). "
             "An ngrok tunnel to Ollama is running without the Heimdal basic-auth policy - stop it "
             "and re-run so this script can start a protected one.")
    status, body = http(f"{url}/v1/models", auth=auth, timeout=15)
    if status != 200:
        fail(f"The tunnel {url} rejected the Heimdal credentials ({status or 'no answer'}). It was probably started "
             "with other credentials - stop that ngrok process and re-run.")
    served = [m["id"] for m in json.loads(body).get("data", [])]
    missing = [m for m in models if not has_model(served, m)]
    if missing:
        fail(f"The tunnel works but does not list {', '.join(missing)} (served: {', '.join(served)}).")
    log("Tunnel verified: 401 without credentials, models listed with credentials.")


# ---- entry points ----------------------------------------------------------
def ensure() -> dict[str, str]:
    """Make sure Ollama + ngrok are ready. Returns the settings Bicep needs (empty when disabled)."""
    if setting("HEIMDAL_ENABLE_OLLAMA", "false").lower() != "true":
        log("HEIMDAL_ENABLE_OLLAMA is not 'true' - skipping the local Ollama backend.")
        return {}
    models = [m.strip() for m in setting("HEIMDAL_OLLAMA_MODEL", DEFAULT_MODEL).split(",") if m.strip()]
    state = load_state()

    models = ensure_ollama(models, state)  # from here on: the gateway names (e.g. gemma4-e4b)
    exe = ensure_ngrok_installed()
    # Tunnel credentials: generated once, kept in the azd env (and the local state file for the non-azd path).
    auth = setting("HEIMDAL_OLLAMA_AUTH") or state.get("auth") or f"{AUTH_USER}:{secrets.token_urlsafe(24)}"
    state["auth"] = auth
    save_state(state)
    url = ensure_tunnel(exe, auth, setting("HEIMDAL_OLLAMA_NGROK_DOMAIN"), state)
    verify_tunnel(url, auth, models)

    result = {"HEIMDAL_OLLAMA_URL": url, "HEIMDAL_OLLAMA_MODELS": ",".join(models), "HEIMDAL_OLLAMA_AUTH": auth}
    for k, v in result.items():
        azd_set(k, v)
    log(f"APIM backend 'asgard-ollama' -> {url}/v1 for model(s): {', '.join(models)}")
    return result


def stop() -> None:
    state = load_state()
    for key in ("ngrokPid", "ollamaPid"):
        pid = state.pop(key, None)
        if not pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            log(f"Stopped {key[:-3]} (pid {pid}).")
        except OSError:
            log(f"{key[:-3]} (pid {pid}) was not running.")
    save_state(state)


def main() -> None:
    p = argparse.ArgumentParser(description="Expose a local Ollama model to APIM via ngrok")
    p.add_argument("--stop", action="store_true", help="stop the processes this script started")
    args = p.parse_args()
    stop() if args.stop else ensure()


if __name__ == "__main__":
    main()
