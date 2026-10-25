"""
Can Runa call GPT on SAP AI Core, and which way works?

    python probe_gpt.py

Tries a one-line question in the ways SAP AI Core offers for OpenAI models,
and prints which one answered. Uses the AI Core login from .env; prints no
secrets. Costs a few tokens.
"""
import httpx

from agents import _aicore_token
from config import AICORE_API_URL, AICORE_RESOURCE_GROUP

GPT_DEPLOYMENT = "d1a82575de60e02a"          # gpt-5.4, from list_aicore_models.py
ORCHESTRATION = "d8c092efb8980093"           # defaultOrchestrationConfig
QUESTION = 'Reply with exactly this JSON and nothing else: {"ok": true}'
BASE = AICORE_API_URL.rstrip("/")


def headers():
    return {"Authorization": f"Bearer {_aicore_token()}",
            "AI-Resource-Group": AICORE_RESOURCE_GROUP}


def attempt(label, url, body):
    try:
        r = httpx.post(url, json=body, headers=headers(), timeout=60)
    except httpx.HTTPError as e:
        print(f"[{label}] could not connect: {e}")
        return
    print(f"[{label}] HTTP {r.status_code}: {r.text[:300]}")


def main():
    messages = [{"role": "user", "content": QUESTION}]
    for version in ("2024-10-21", "2025-04-01-preview"):
        attempt(f"direct, api-version {version}",
                f"{BASE}/v2/inference/deployments/{GPT_DEPLOYMENT}/chat/completions"
                f"?api-version={version}",
                {"messages": messages, "max_completion_tokens": 200})
    attempt("orchestration v2",
            f"{BASE}/v2/inference/deployments/{ORCHESTRATION}/v2/completion",
            {"config": {"modules": {"prompt_templating": {
                "prompt": {"template": messages},
                "model": {"name": "gpt-5.4"}}}}})
    attempt("orchestration v1",
            f"{BASE}/v2/inference/deployments/{ORCHESTRATION}/completion",
            {"orchestration_config": {"module_configurations": {
                "llm_module_config": {"model_name": "gpt-5.4", "model_params": {}},
                "templating_module_config": {"template": messages}}}})


if __name__ == "__main__":
    main()
