"""azd postdeploy hook: wires the Function MCP key into APIM and writes .env for the notebooks."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy import azd_hook  # noqa: E402

if __name__ == "__main__":
    azd_hook()
