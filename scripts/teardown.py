"""Delete the HEIMDAL resource group (and purge soft-deleted APIM / Foundry accounts)."""
import argparse
import shutil
import subprocess

AZ = shutil.which("az") or "az"


def run(*args: str) -> None:
    print("> az", " ".join(args))
    subprocess.run([AZ, *args], check=False)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--resource-group", default="rg-heimdal-asgard-swc")
    p.add_argument("--yes", action="store_true", help="skip confirmation")
    args = p.parse_args()
    if not args.yes and input(f"Delete resource group {args.resource_group}? [y/N] ").lower() != "y":
        return
    run("group", "delete", "-n", args.resource_group, "--yes")
    print("APIM and Foundry accounts are soft-deleted; purge them if you want to reuse the same names:")
    print("  az apim deletedservice list / purge   |   az cognitiveservices account list-deleted / purge")


if __name__ == "__main__":
    main()
