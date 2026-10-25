"""
Which AI models can Runa call on SAP AI Core right now?

    python list_aicore_models.py

Lists the deployments in Runa's resource group (AICORE_RESOURCE_GROUP in
.env, usually "default"): their id, model name and status. Only RUNNING
deployments can be called. Uses the AI Core login from .env; prints no
secrets.
"""
import httpx

from agents import _aicore_token
from config import AICORE_API_URL, AICORE_DEPLOYMENT, AICORE_RESOURCE_GROUP


def model_of(d: dict) -> str:
    details = d.get("details") or {}
    backend = (details.get("resources") or {}).get("backend_details") or {}
    model = backend.get("model") or {}
    if model.get("name"):
        return f"{model['name']} {model.get('version') or ''}".strip()
    return d.get("configurationName") or "?"


def main():
    r = httpx.get(f"{AICORE_API_URL.rstrip('/')}/v2/lm/deployments",
                  headers={"Authorization": f"Bearer {_aicore_token()}",
                           "AI-Resource-Group": AICORE_RESOURCE_GROUP}, timeout=30)
    r.raise_for_status()
    deployments = r.json().get("resources", [])
    print(f"Resource group '{AICORE_RESOURCE_GROUP}': {len(deployments)} deployments\n")
    print(f"{'ID':<18} {'STATUS':<10} {'SCENARIO':<16} MODEL")
    for d in sorted(deployments, key=lambda d: d.get("status") != "RUNNING"):
        mark = "   <- Runa uses this one" if d.get("id") == AICORE_DEPLOYMENT else ""
        print(f"{d.get('id', '?'):<18} {d.get('status', '?'):<10} "
              f"{d.get('scenarioId', '?'):<16} {model_of(d)}{mark}")


if __name__ == "__main__":
    main()
