#!/usr/bin/env node
/**
 * Query all Slid Phi team seats in one pass.
 * Usage: node scripts/team-seats-query.mjs [--dm <agent_id>]
 * Reads ~/.config/agent-rider/teams/*.json (never prints keys).
 * For each seat: mint L1 rider JWT, read its DM threads + notifications,
 * and confirm the public channel is visible. Exit 0 with JSON summary.
 */
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { join } from "node:path";

const BASE = (process.env.AGENT_RIDER_BASE || "https://agentrider.fly.dev").replace(/\/+$/, "");
const DIR = process.env.TEAM_SEATS_DIR || (process.env.HOME + "/.config/agent-rider/teams");
const PEERS = (process.env.MUSE_PEERS || [
  "c34d9ac1c8a3f8f0", "2e69df930a3beaff", "c83275891efd76a0", "443fa43c2917f5d5",
  "fbe0912fa4ddaa27", "6e1031b9adb12231", "935da5959f787cc8", "6122039b85c05140",
  "826803ab2fcca042", "1e503e7745ca2202",
].join(",")).split(",").map((s) => s.trim()).filter(Boolean);
const DM_ARG = (process.argv.find((a) => a.startsWith("--dm")) || "").split("=")[1] || null;

async function issueRider(apiKey) {
  const res = await fetch(`${BASE}/api/rider/issue`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({ level: "L1", scopes: ["*"] }),
  });
  if (!res.ok) return null;
  return (await res.json()).rider || null;
}

async function jget(path, token) {
  const res = await fetch(`${BASE}${path}`, { headers: token ? { "X-Agent-Rider": token } : {} });
  if (!res.ok) return { _status: res.status };
  return res.json();
}

const out = { ok: true, seats: {} };
for (const f of readdirSync(DIR).filter((x) => x.endsWith(".json")).sort()) {
  const team = f.replace(/\.json$/, "");
  const row = { ok: false };
  try {
    const seat = JSON.parse(readFileSync(join(DIR, f), "utf8"));
    row.agent_id = seat.agent_id;
    const token = await issueRider(seat.api_key);
    if (!token) { row.error = "rider_issue_failed"; out.seats[team] = row; continue; }
    row.mint = true;
    const ch = await jget("/api/channels/lab-team/messages?limit=5", null);
    row.channel_visible = Array.isArray(ch.messages);
    if (DM_ARG) {
      const th = await jget(`/api/dm/${DM_ARG}`, token);
      row.thread_with = DM_ARG;
      row.thread_status = th._status || 200;
      row.thread_messages = (th.messages || []).length;
      row.thread_preview = (th.messages || []).slice(-3).map((m) => ({
        id: m.id, from: m.from_agent_id, at: m.created_at, content: (m.content || "").slice(0, 160),
      }));
    } else {
      let total = 0, threads = 0;
      for (const peer of PEERS) {
        const th = await jget(`/api/dm/${peer}`, token);
        if (th._status) continue;
        const n = (th.messages || []).length;
        if (n) { threads++; total += n; }
      }
      row.dm_threads_nonempty = threads;
      row.dm_messages = total;
    }
    const notif = await jget("/api/notifications?unread=true", token);
    row.unread_notifications = (notif.notifications || []).length;
    row.ok = true;
  } catch (e) {
    row.error = String(e).slice(0, 120);
  }
  out.seats[team] = row;
}
console.log(JSON.stringify(out, null, 2));
