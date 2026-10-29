# Runa — every constraint, in one place

**The golden rule:** the AI *judges*, the code *controls*, a human *decides*.
The AI says what a message means and how sure it is. Plain Python decides
whether anything may be written to SAP. Anything risky or unclear goes to a
supervisor. Runa never guesses and never crashes: when in doubt it asks, or
hands over.

---

## 1. The real-world constraints Runa weighs (learned from the chat, not hard-coded)

Runa reads the operations group in the background and keeps a list of
**facts** about right now. Nobody types them into a form: the AI extracts
them from normal messages, and each one expires on its own.

| Constraint | Example from the chat | Stays true for | Newer fact replaces older? |
|---|---|---|---|
| Driver duty (when a driver started) | "Budi udah nyetir dari jam 5 pagi" | 24 h | Yes |
| Driver availability | "Agus standby di pool Cikampek" | 12 h | Yes |
| Manpower (crew on the dock) | "Kru bongkar malam ini cuma 2 orang" | 12 h | No |
| Equipment | "Forklift 2 rusak" | 12 h | Yes |
| Weather | "Hujan deras di Cikarang" | 3 h | No |
| Route / traffic | "Tol Cikampek macet total, +2 jam" | 3 h | No |
| Stock | "Stok mie tinggal 0,8 hari" | 24 h | Yes |
| Dock condition / hours | "Dock 1 tergenang" | 12 h | No |
| Stated rule | "Supir maksimal 12 jam kerja" | Never expires (evidence for a rule) | — |

These are the mentor's constraints (weather, stock, delivery date, manpower,
driver availability, safety/long shifts), all derived from the chat.

---

## 2. Warehouse rules Runa learned from chat history (approved by the supervisor)

`/pelajari` makes Runa look for rules in past messages. A rule is only
proposed if **someone stated it**, or it is a **pattern seen on at least 2
different days**. It is only used after a supervisor approves it with `/setuju`.

From the demo history (14 rules: 10 stated, 4 patterns):

**Stated by someone in the group**
1. Rudi (CV Mitra Kemasan) may not drive after 20:00 (company policy).
2. Drivers work at most 12 hours a day including rest; past that, the driver must be replaced.
3. When instant noodle stock (FG-4471) falls below 1 day, the shipment must be received the same day, even at night.
4. Overtime is at most 3 hours.
5. Unloading a full truck needs at least 3 crew.
6. With only 1 forklift working, 240 cartons take 2 hours instead of 1, and trucks must be spaced 2 hours apart.
7. Dock hours are 06:00–22:00; trucks arriving after 22:00 wait until 06:00.
8. Every Friday 11:30–13:00 the unloading crew drops to 1 person (Friday prayers).
9. Every Friday afternoon tol Cikampek is heavily congested (about +2 hours).
10. Saturday is a half-day shift at Cikarang DC.

**Patterns seen on 2+ days**
11. On Saturdays the unloading crew is 3 people (instead of 5).
12. On weekday mornings the standard unloading crew is 5 people.
13. When a driver is at or near the 12-hour limit, Agus (Cikampek pool) is the relief driver.
14. Dock 1 has no roof: no unloading there in the rain, so trucks go to another dock.

Measured: Runa found **10/10** planted rules and fell for **0/4** traps
(one-off events that look like rules but aren't).

---

## 3. Hard limits in code (the AI can never override these)

| Limit | Value | Why |
|---|---|---|
| Confidence to write to SAP | Both scores ≥ **0.85** (which delivery + what happened) | Below that, Runa asks instead of guessing |
| Questions per event | At most **2**, then a human takes over | No endless back-and-forth |
| Moving an arrival time | At most **24 h** | Bigger moves need a human |
| Reported shortage | At most **50 %** of the delivery | Bigger shortages need a human check |
| Driver hours | At most **12 h** a day including rest (UU 22/2009 Pasal 90) | Safety: computed by code from the facts, never taken from the AI |
| Keeping a dock open late | At most **6 h** past closing (past midnight allowed) | Sanity limit |
| Options shown to a supervisor | At most **4** (a–d) | Readable in a chat |
| SAP forms Runa can fill | ETA change, goods-receipt shortage, quality complaint | Anything else work-related goes to a human |
| Planner actions | Reschedule, extend dock, swap driver, driver rests, change dock | Only actions Runa can really carry out in SAP |
| Docks | DOCK-01, DOCK-02 | Only real docks |

---

## 4. Checks before every SAP write

1. **Not an instruction to the system.** Text like "abaikan instruksi sebelumnya" is never processed; it goes to a supervisor.
2. **Work, not small talk.** "Siap bu" is ignored and nothing is written.
3. **The delivery number matches.** If the person typed a DO number, the AI must use exactly that one. An unknown number gets "DO itu tidak ada di sistem".
4. **The goods match.** If the person named a product ("mie"), the chosen delivery must actually carry it.
5. **No hedge words.** "Kayaknya", "mungkin", "sekitar", "insya Allah" make it an estimate, so Runa asks "Itu sudah pasti, atau masih perkiraan?".
6. **The time is clear.** "Jam 7" (morning or evening?) gets a question. A time that has already passed gets a question. The AI's time must match what the person literally wrote (within 5 minutes).
7. **The units match.** If someone counts in a different unit ("3 dus" for a delivery counted in pieces), Runa asks how many that is.
8. **The rulebook passes** (section 3). Otherwise: handoff to a supervisor.
9. **After writing: consequences.** Does the new arrival time miss the dock hours? Then it's a conflict, and the Planner starts.

Every write is logged on the SAP side with who said what, the scores and
what changed. `/undo` reverses it with a new transaction; nothing is deleted.

---

## 5. Checks on the Planner's options (when a delivery misses the dock)

The Planner (Claude) proposes 2–4 options. Code removes any option that:

- breaks an approved rule, or marks itself as breaking one;
- cites a fact Runa doesn't know, or one that has expired;
- cites a rule a supervisor hasn't approved;
- uses an action outside the fixed list;
- reschedules to a time before the truck can arrive, or into the past;
- extends the dock to an invalid time, or more than 6 hours;
- swaps to a driver Runa doesn't know, or to the same driver;
- moves to the dock the truck is already going to, or to a dock that doesn't exist;
- still has the truck arriving while the dock is closed (it doesn't solve the problem);
- keeps a driver at the wheel past 12 hours.

Then the **Reviewer (GPT-5.4, a different AI vendor)** checks every option
against every rule and fact. It takes an option out only when it ties the
problem to a real rule or fact id. It follows two principles:
- **The specific rule beats the general one.** "Receive even at night" overrides normal dock hours.
- **Limits that protect people are never overridden:** driver hours, crew minimums, overtime.

If no option survives, Runa says so honestly ("no option keeps every rule")
and hands the decision to the supervisor.

---

## 6. Checks on the stage reader (GPT-5.4, side job after each reply)

A stage is written to SAP only if:
- the DO number is written in **this** message and exists in SAP;
- the stage is a real one (Diproduksi → Dikemas → Siap kirim → Dalam perjalanan → Di gerbang → Dibongkar → Diterima);
- it moves **forward** only, never back;
- the quoted evidence words are really in the message;
- confidence is ≥ 0.85.

Problems it hears ("kurang bubble wrap") go to the log as notes; they never
change SAP. If GPT-5.4 is down, Claude does the job instead.

---

## 7. Who may do what

| Action | Who |
|---|---|
| Report anything in the group | Everyone |
| Answer Runa's question about their own report | Only the person who reported it |
| Choose option a / b on a conflict | Supervisors only (group admins). A driver typing "b" does nothing |
| `/undo`, `/reset` | Supervisors only (group admins) |
| `/pelajari` (find rules), `/setuju` or `/tolak` (approve or reject) | Supervisors only |
| Write to SAP over the API | Only with the secret key (`X-Runa-Key`); reading is open |
| Ask through Joule (A2A) | Read-only: Joule can never change SAP |

---

## 8. Honest limits (good to know for Q&A)

- SAP is a **mock** that speaks the real S/4HANA Inbound Delivery OData API (`API_INBOUND_DELIVERY_SRV`), not a live S/4HANA system.
- Runa reads **text**. A photo is read by its caption; a photo with no words is left alone.
- Three SAP forms (ETA, shortage, quality). Other work messages are understood, then handed to a human.
- The demo runs on a fixed demo clock (9 Sep, 19:42). A real-clock mode exists but is off.
- Two docks in the demo data.

---

## 9. Numbers for the slides

- Holdout test (unseen messages): **37/41 correct (90 %)**, **0 unsafe writes**
- Surprise / edge-case tests: **121/121** pass
- Rule learner: **10/10** rules found, **0/4** traps taken
- Two AI vendors check each other: Claude (via Amazon Bedrock on SAP AI Core) and GPT-5.4 (on SAP AI Core)
- AWS: Amazon Bedrock (Claude), Amazon DynamoDB (everything saved), AWS IAM (access to one table only)
