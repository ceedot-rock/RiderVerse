# EMAIL_ADAPTER_NEXT.md — inbound email → RiderXL thread (design sketch)

Status: NOT BUILT. This is the design for a later lane. No live email is
sent or received by the current demo; nothing here is wired up.

## Goal

Let an email thread behave like a group-chat thread: inbound mail becomes
messages in a RiderXL group, and RiderXL's replies (task cards, approval
cards, answers) go back out as email. Useful for members who live in their
inbox, and for non-member guests who never open the chat UI.

## Inbound design (sketch only)

1. **Mailbox.** A dedicated lab mailbox (e.g. `riderxl@<lab-domain>`) polled
   via IMAP IDLE, or a webhook receiver if the provider supports inbound
   parse. Lab-only: poll loop, 60s interval, stdlib `imaplib` + `email`.
2. **Thread mapping.** `In-Reply-To` / `References` headers map a message to
   an existing group (`group_id` stored per Message-ID). No match + subject
   starts with `[RiderXL]` → create a new group named from the subject.
3. **Identity mapping.** From-address → identity_id. First-seen address gets
   a member row of type `email-guest` with the display name taken from the
   From display-name or the local-part. Same permission-first rule: RiderXL
   does nothing for that identity until it grants trust. Grant/deny over
   email is a problem (see open questions) — the safe default is: email
   guests can READ (they get RiderXL's public output as replies) but RiderXL
   never acts on their @agent mentions until they grant via the web UI.
4. **Message ingestion.** Plain-text body → `POST /api/groups/{id}/messages`
   with the mapped member_id. Attachments: stored under
   `chat/data/attachments/<group_id>/`, referenced in the message payload,
   never executed. HTML bodies stripped to text; cap 2000 chars like the
   web composer.
5. **Outbound.** RiderXL's agent_text / task_card / approval_card messages
   render to a plain-text email template and are queued for send via the
   lab's approved sender. Approval cards MUST include one-time approve/deny
   links (signed tokens, 24h expiry) — email approvals are the riskiest
   part (forwarded mail = forwarded authority).

## Open questions (must be answered before building)

- How does an email-only user grant/deny permission without the web UI?
  Options: signed magic link, or read-only-by-default as sketched above.
- Approval-by-email is an authority-delegation primitive. Needs Corey's
  explicit sign-off on the threat model before any implementation.
- Rate limits / loop protection: never reply to auto-responders, mailing
  lists, or noreply addresses (check List-Unsubscribe, Precedence, X-Auto-Response-Suppress).

## Why this is a doc, not code

Inbound email is an *authority* surface (anyone who can send mail can talk
to the agent). The permission-first model has to be airtight before mail
gets a voice in the thread. Sketched here so the shape is decided before
anyone reaches for imaplib.
