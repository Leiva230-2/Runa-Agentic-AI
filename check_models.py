"""Find which model strings your API key can call. Run this first."""
import anthropic
from dotenv import load_dotenv
load_dotenv()

CANDIDATES = [
    "claude-sonnet-4-6", "claude-sonnet-5", "claude-opus-4-6",
    "claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-opus-5",
]

client = anthropic.Anthropic()
ok = []
for model in CANDIDATES:
    try:
        client.messages.create(model=model, max_tokens=5,
                               messages=[{"role": "user", "content": "hi"}])
        print(f"  OK      {model}")
        ok.append(model)
    except Exception as e:
        print(f"  FAILED  {model}  ({type(e).__name__})")

print("\nPut working strings in .env:")
print(f"  MODEL_RESOLVER={ok[0] if ok else '<none worked>'}")
print(f"  MODEL_LISTENER={ok[-1] if ok else '<none worked>'}")
