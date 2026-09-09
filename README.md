# Runa — MVP

*Your conversations, recorded in SAP.*

An agent that joins an operations group chat, decides whether a business event
occurred, scores its own confidence two ways, asks a clarifying question when
it is unsure, and posts confirmed events to SAP.

## Setup

```bash
cd ~/runa
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then paste your keys in
```

**Run this first** — it tells you which model strings your key can call:

```bash
python check_models.py
```

If any fail, copy the working strings into `.env`.

## Run

Two terminals, both with the venv activated.

```bash
# terminal 1 — the SAP system of record
uvicorn sap_mock:app --port 8000

# terminal 2 — scripted demo (start here)
python replay.py

# terminal 2 — or the live Telegram bot
python bot.py
```

`replay.py` runs the identical pipeline without Telegram. Use it to capture
your trace, and as the fallback if Telegram misbehaves during the demo.

## What happens

```
Telegram message
  → Listener   Haiku. Business event, or chatter? Most messages are discarded.
  → Resolver   Sonnet. Which delivery? What happened? TWO confidence scores.
  → if either score < 0.85 → ask in Bahasa Indonesia, park the event, resume on reply
  → Transactor PATCH the inbound delivery via OData, with reason code and source link
  → Conflict   Does the new state break something else? Escalate unprompted.
```

The dual confidence score is the design decision that matters. Identity
confidence ("which delivery is this?") and content confidence ("what exactly
happened?") fail independently. A driver can be unmistakably identified while
saying something vague — that is exactly the "kayaknya" case, and a single
blended score would hide it.

## The SAP mock

`sap_mock.py` implements the real `API_INBOUND_DELIVERY_SRV` OData v2 contract:
the same entity set, field names, key syntax and `{"d": {...}}` envelope that
S/4HANA returns. Runa makes genuine HTTP calls against a genuine interface
shape, so pointing at a live sandbox is a change to `SAP_BASE` in `.env` and
nothing else.

Inspect what landed:

```bash
curl -s localhost:8000/_debug/state | python3 -m json.tool
```

## Capture your demo evidence

You need three artifacts for the proposal and the pitch:

1. Screenshot of the Telegram thread — the agent asking, a human answering
2. Screenshot of `_debug/state` showing the changed SAP record
3. `logs/trace.jsonl`, and the transaction count printed by `replay.py`

## Migrating to AWS

`agents.py` is the only file that talks to a model. Swap the client and
nothing else changes:

```python
# now
client = anthropic.Anthropic()
resp = client.messages.create(model=..., ...)

# on Bedrock
import boto3
client = boto3.client("bedrock-runtime", region_name="us-east-1")
resp = client.converse(modelId="us.anthropic.claude-sonnet-4-6", ...)
```

## Troubleshooting

**Bot receives nothing in a group.** BotFather → `/setprivacy` → **Disable**.
Then remove and re-add the bot to the group. This is the most common failure.

**`Connection refused`.** The SAP mock is not running. Start terminal 1.

**`model_not_found`.** Run `check_models.py` and update `.env`.

**Agent asks a question every time.** Lower `CONFIDENCE_THRESHOLD` toward 0.75.
Raise it toward 0.9 if it writes when it should have asked.
