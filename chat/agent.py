"""RiderXL: the shared group-chat agent, a participant in the thread.

Three jobs:
  (a) ANSWER simple questions from a local knowledge base / pattern
      responses. NO external LLM calls. The `responder` hook below is where
      a real model plugs in later (same shape as agent-games' StubLLM ->
      OpenAICompatibleLLM split): `respond(prompt, context) -> str`.
  (b) TAKE TASKS via @agent commands, posting structured task cards.
  (c) CHECK IN: anything spend-like or external produces an APPROVAL card
      and the agent WAITS. It must NOT act until members approve -- the
      anti-Instinct rule. In this lab demo, even an approval only posts a
      confirmation; no real action is wired to anything.

The agent is permission-first: server.py only calls handle_message() for
members whose identity has granted trust (see chat/permissions.py). An
untrusted identity's @agent mention yields a permission-request card
instead, produced by the server -- the agent never speaks to strangers.
"""
from __future__ import annotations

import re
import time

AGENT_MEMBER_ID = "agent"
AGENT_NAME = "RiderXL"

# Scopes the agent asks a personal identity for (shown on the card).
# Review fix 2026-10-07: approvals ride on a DEDICATED spend:approve scope
# (not chat:write — privilege conflation). The permission card grants all
# four; approve/deny of spend-like requests checks spend:approve only.
AGENT_SCOPES = ["chat:read", "chat:write", "spend:propose", "spend:approve"]

# Words that make a request "spend-like or external" -> approval required.
SPEND_KEYWORDS = (
    "book", "buy", "purchase", "pay", "send money", "order", "reserve",
    "subscribe", "wire", "transfer", "checkout",
)


class ScriptedResponder:
    """Deterministic scripted responder. Same interface a real model backend
    would implement: respond(prompt, context) -> str.

    To plug in a real model later: implement this method on a new class
    (e.g. OpenAICompatibleResponder posting to an OpenAI-compatible
    /chat/completions endpoint, configured via env like AG_LLM_URL /
    AG_LLM_KEY in agent-games) and pass it as RiderXL(responder=...).
    Lab rule: no network calls from the demo; keep the scripted one here.
    """

    def __init__(self):
        self.rules = [
            ("help", self._help),
            ("what can you do", self._help),
            ("who's here", self._who),
            ("who is here", self._who),
            ("who is in", self._who),
            ("members", self._who),
            ("what time", self._time),
            ("what date", self._time),
            ("rules", self._rules),
            ("permission", self._rules),
            ("trust", self._rules),
        ]

    def respond(self, prompt: str, context: dict) -> str:
        low = prompt.lower()
        for match, fn in self.rules:
            if match in low:
                return fn(context)
        return (
            "I don't know how to do that yet. Try '@agent help' to see what "
            "I can do, '@agent plan the trip', or '@agent split the bill'."
        )

    # -- knowledge base --
    def _help(self, ctx: dict) -> str:
        return (
            "I'm RiderXL, the shared agent for this group. I can:\n"
            "- Answer simple questions (try: who's here, what time is it)\n"
            "- Take tasks: '@agent plan the trip', '@agent split the bill'\n"
            "- Ask before anything spend-like: I post an approval card and "
            "wait -- I never act without a member's approval.\n"
            "I only work for people who grant me permission (chat:read, "
            "chat:write). Revoke anytime with the 'Revoke trust' button."
        )

    def _who(self, ctx: dict) -> str:
        names = ctx.get("member_names", [])
        if not names:
            return "Just me so far -- nobody else has joined this group."
        return "In this group: " + ", ".join(names) + "."

    def _time(self, ctx: dict) -> str:
        return "Server time: " + time.strftime("%Y-%m-%d %H:%M %Z", time.localtime())

    def _rules(self, ctx: dict) -> str:
        return (
            "Permission-first rules I follow:\n"
            "1. I do nothing for you until you grant me chat:read + chat:write.\n"
            "2. Guests (no platform account) can still read everything I post.\n"
            "3. Anything spend-like or external needs an approval card -- "
            "I wait for a member to approve; I never act first.\n"
            "4. 'Revoke trust' cuts my access to you immediately."
        )


class RiderXL:
    def __init__(self, responder=None):
        self.responder = responder or ScriptedResponder()

    # -- entry point called by server.py for a trusted member's message --
    def handle_message(self, group: dict, member: dict, text: str) -> list[dict]:
        """Return a list of events to append to the thread. Each event:
        {"kind": "agent_text"|"task_card"|"approval_card",
         "text": str, "payload": dict}."""
        low = text.lower()
        if "@agent" not in low:
            # Watch for bill-split amount replies even without a mention.
            card_evt = self._maybe_bill_entry(group, member, text)
            return [card_evt] if card_evt else []
        if self._is_spend_like(low):
            return [self._approval_card(group, member, text)]
        if "plan the trip" in low or "plan trip" in low:
            return [self._trip_plan_card(group, member)]
        if "split the bill" in low or "split bill" in low:
            return [self._bill_split_card(group, member)]
        # knowledge-base answer
        prompt = re.sub(r"(?i)@agent\s*", "", text).strip() or "help"
        ctx = {
            "member_names": [m["display_name"] for m in group["members"]
                             if m["member_id"] != AGENT_MEMBER_ID],
        }
        return [{"kind": "agent_text",
                 "text": self.responder.respond(prompt, ctx),
                 "payload": {}}]

    # -- task cards --
    def _new_task(self, group: dict, kind: str, title: str, payload: dict) -> dict:
        tid = f"task-{len(group['tasks']) + 1:03d}"
        task = {"task_id": tid, "kind": kind, "title": title,
                "status": "open", "created_ts": time.time(),
                "payload": payload}
        group["tasks"][tid] = task
        return task

    def _trip_plan_card(self, group, member) -> dict:
        names = [m["display_name"] for m in group["members"]
                 if m["member_id"] != AGENT_MEMBER_ID]
        task = self._new_task(group, "trip_plan", "Trip plan", {
            "items": [
                "Pick dates (reply with e.g. 'dates: Oct 18-20')",
                "Pick destination (reply with e.g. 'destination: Philly')",
                "Pick budget per person (reply with e.g. 'budget: 200')",
            ],
            "filled": {},
            "travelers": names,
        })
        return {"kind": "task_card",
                "text": f"Trip plan started for {', '.join(names) or 'the group'}. "
                        "Reply to fill in the blanks -- I'll keep the card updated.",
                "payload": {"task_id": task["task_id"]}}

    def _bill_split_card(self, group, member) -> dict:
        task = self._new_task(group, "bill_split", "Bill split", {"entries": {}})
        return {"kind": "task_card",
                "text": "Bill split started. Everyone reply with your amount "
                        "like 'Alex 24.50' and I'll compute who owes what.",
                "payload": {"task_id": task["task_id"]}}

    def _maybe_bill_entry(self, group, member, text) -> dict | None:
        m = re.match(r"^\s*([A-Za-z][\w .'-]{0,30}?)\s+(\d+(?:\.\d{1,2})?)\s*$", text)
        if not m:
            return None
        open_splits = [t for t in group["tasks"].values()
                       if t["kind"] == "bill_split" and t["status"] == "open"]
        if not open_splits:
            return None
        name, amount = m.group(1).strip(), float(m.group(2))
        if amount <= 0 or amount > 100000:
            return None
        task = open_splits[-1]
        task["payload"]["entries"][name] = round(amount, 2)
        total = sum(task["payload"]["entries"].values())
        n = len(task["payload"]["entries"])
        share = round(total / n, 2)
        lines = [f"{k}: ${v:.2f}" for k, v in task["payload"]["entries"].items()]
        return {"kind": "task_card",
                "text": ("Bill so far -- " + "; ".join(lines) +
                         f". Total ${total:.2f} across {n}, "
                         f"fair share ${share:.2f} each."),
                "payload": {"task_id": task["task_id"]}}

    # -- approval check-in (the anti-Instinct rule) --
    def _is_spend_like(self, low: str) -> bool:
        return any(k in low for k in SPEND_KEYWORDS)

    def _approval_card(self, group, member, text) -> dict:
        request = re.sub(r"(?i)@agent\s*", "", text).strip()
        task = self._new_task(group, "approval",
                              "Approval needed", {
                                  "request": request or "(see message above)",
                                  "requested_by": member["display_name"],
                                  "requested_by_identity": member["identity_id"],
                                  "note": ("This looks spend-like or external, so I'm "
                                           "checking in first. I will NOT act until a "
                                           "member approves."),
                              })
        task["status"] = "pending_approval"
        return {"kind": "approval_card",
                "text": (f"{member['display_name']} asked me to do something "
                         "spend-like or external. Approval required -- I won't "
                         "act until a member taps Approve."),
                "payload": {"task_id": task["task_id"]}}

    # -- approval resolution, called by server.py --
    def resolve_approval(self, group: dict, task: dict,
                         approver: dict, approved: bool) -> dict:
        if approved:
            task["status"] = "approved"
            return {"kind": "agent_text",
                    "text": (f"Approved by {approver['display_name']}. In this lab "
                             "demo I stop here -- no real action is wired to "
                             "anything outside the chat. (Rule kept: I never "
                             "acted before approval.)"),
                    "payload": {"task_id": task["task_id"]}}
        task["status"] = "denied"
        return {"kind": "agent_text",
                "text": (f"Denied by {approver['display_name']}. Task cancelled -- "
                         "nothing happened, as it should."),
                "payload": {"task_id": task["task_id"]}}
