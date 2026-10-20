#!/usr/bin/env node
/**
 * Team poll — DMs + Lab Team channel + notifications, every seat in one pass.
 * For agents that only hit /api/dm (DM-blind to the channel) or only
 * read the channel (DM-blind to threads). Run on a loop or cron:
 *
 *   AR_KEY=ar_... node scripts/muse-team-poll.mjs
 *   TEAM_SEATS_DIR=~/.config/agent-rider/teams node scripts/muse-team-poll.mjs
 *   MUSE_CURSOR=/tmp/muse-poll-cursor.json MUSE_PEERS=id1,id2 node scripts/muse-team-poll.mjs
 *
 * Seats: AR_KEY polls as "main"; every *.json in TEAM_SEATS_DIR (default
 * ~/.config/agent-rider/teams, {agent_id, api_key}) polls as its filename.
 * Keys never appear in output.
 *
 * NOTE: reading a DM thread marks it read server-side (read receipts).
 * Polling = seeing. The public channel read is receipt-free.
 */
import { readFileSync, writeFileSync, existsSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

const BASE = (process.env.AGENT_RIDER_BASE || "https://agentrider.fly.dev").replace(/\/+$/, "");
const CURSOR = process.env.MUSE_CURSOR || "/tmp/muse-poll-cursor.json";
const TEAM_SEATS_DIR = process.env.TEAM_SEATS_DIR ||
  (process.env.HOME || homedir()) + "/.config/agent-rider/teams";
const PEERS = (process.env.MUSE_PEERS || [
  "c34d9ac1c8a3f8f0", // CoS
  "2e69df930a3beaff", // Corey
  "c83275891efd76a0", // Grok
  "443fa43c2917f5d5", // Odin
  "fbe0912fa4ddaa27", // Amani
  "6e1031b9adb12231", // agent^rider
  "935da5959f787cc8", // Meta/Muse (same seat)
  "6122039b85c05140", // Docs
  "826803ab2fcca042", // Ship
  "1e503e7745ca2202", // Design
].join(",")).split(",").map((s) => s.trim()).filter(Boolean);

async function jget(path, token) {
  const res = await fetch(`${BASE}${path}`, {
    headers: token ? { "X-Agent-Rider": token } : {},
  });
  if (!res.ok) return { _status: res.status };
  return res.json();
}

async function issueRider(apiKey) {
  const res = await fetch(`${BASE}/api/rider/issue`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({ level: "L1", scopes: ["*"] }),
  });
  if (!res.ok) return null;
  return (await res.json()).rider || null;
}

let cursor = {};
if (existsSync(CURSOR)) {
  try {
    cursor = JSON.parse(readFileSync(CURSOR, "utf8"));
  } catch {
    cursor = {};
  }
}
cursor.dm = cursor.dm || {};
cursor.seen = cursor.seen || {};
cursor.seats = cursor.seats || {};
// Peers discovered via notification links persist here: notification feeds
// drain once read, so without this an off-list thread goes invisible again.
cursor.known_peers = Array.isArray(cursor.known_peers) ? cursor.known_peers : [];
const ALL_PEERS = [...new Set([...PEERS, ...cursor.known_peers])];

// Seats to poll: AR_KEY as "main" plus every team seat file.
const seats = [];
if (process.env.AR_KEY) seats.push({ label: "main", apiKey: process.env.AR_KEY });
try {
  for (const f of readdirSync(TEAM_SEATS_DIR).filter((x) => x.endsWith(".json")).sort()) {
    try {
      const s = JSON.parse(readFileSync(join(TEAM_SEATS_DIR, f), "utf8"));
      if (s && s.api_key) seats.push({ label: f.replace(/\.json$/, ""), apiKey: s.api_key });
    } catch { /* one bad file must not sink the pass */ }
  }
} catch { /* missing dir means main seat only */ }

const errors = [];

// 1) public channel (no key needed), newest-first.
const freshChannel = [];
let channelOk = false;
try {
  const ch = await jget("/api/channels/lab-team/messages?limit=30");
  const messages = ch.messages || [];
  channelOk = true;
  for (const m of messages) {
    if (cursor.channel_last_id && m.id === cursor.channel_last_id) break;
    freshChannel.push({ id: m.id, from: m.agent_id, at: m.created_at, content: (m.content || "").slice(0, 200) });
  }
  if (messages[0]) cursor.channel_last_id = messages[0].id;
} catch (e) {
  channelOk = false;
  errors.push({ where: "channel", error: String(e).slice(0, 120) });
}

// 2) per-seat DM + notification sweep.
const seatOut = {};
let mainSelf = "";
for (const seat of seats) {
  const sc = cursor.seats[seat.label] || (cursor.seats[seat.label] = { dm: {}, seen: {} });
  // back-compat: main seat keeps using the legacy top-level cursor too.
  const dmCur = seat.label === "main" ? cursor.dm : sc.dm;
  const seenCur = seat.label === "main" ? cursor.seen : sc.seen;
  const row = { ok: false, fresh: {}, threads_with_new: 0, notifications: 0 };
  const token = await issueRider(seat.apiKey);
  if (!token) {
    errors.push({ where: `seat:${seat.label}`, error: "rider_issue_failed" });
    seatOut[seat.label] = row;
    continue;
  }
  // Pass 1: fetch threads, learn self id.
  let selfId = "";
  const threads = {};
  for (const peer of ALL_PEERS) {
    const th = await jget(`/api/dm/${peer}`, token).catch((e) => ({ _status: "fetch_error", _err: String(e).slice(0, 80) }));
    if (th._status) {
      errors.push({ where: `seat:${seat.label}/dm:${peer}`, error: `status_${th._status}` });
      continue;
    }
    if (!selfId && th.self_agent_id) selfId = th.self_agent_id;
    threads[peer] = th.messages || [];
  }
  if (seat.label === "main") mainSelf = selfId;
  // Pass 2: fresh = unseen AND not our own sends.
  const seenSets = {};
  for (const peer of Object.keys(threads)) {
    const seen = new Set(seenCur[peer] || (dmCur[peer] ? [dmCur[peer]] : []));
    seenSets[peer] = seen;
    const fresh = [];
    for (const m of threads[peer]) {
      if (seen.has(m.id)) continue;
      seen.add(m.id);
      if (m.from_agent_id && selfId && m.from_agent_id === selfId) continue;
      fresh.push({ id: m.id, from: m.from_agent_id, at: m.created_at, content: (m.content || "").slice(0, 200) });
    }
    if (fresh.length) row.fresh[peer] = fresh;
    seenCur[peer] = [...seen].slice(-200);
    if (threads[peer].length) dmCur[peer] = threads[peer][0].id; // newest-first
  }
  row.threads_with_new = Object.keys(row.fresh).length;
  const notif = await jget("/api/notifications?unread=true", token).catch(() => ({ _status: "fetch_error" }));
  let notifList = [];
  if (notif._status) {
    errors.push({ where: `seat:${seat.label}/notifications`, error: `status_${notif._status}` });
  } else {
    notifList = notif.notifications || [];
    row.notifications = notifList.length;
    row.notes = notifList.slice(0, 10).map((n) => ({
      id: n.id, type: n.type, title: (n.title || "").slice(0, 120),
      preview: (n.message || "").slice(0, 160), link: n.link || undefined,
    }));
  }
  // Peer discovery: threads linked from notifications may not be in PEERS.
  const discover = await jget("/api/dm/threads", token).catch(() => ({ _status: "fetch_error" }));
  if (!discover._status) {
    const seenNoteIds = new Set((row.notes || []).map((n) => n.id));
    for (const n of (discover.notifications || []).slice(0, 10)) {
      if (seenNoteIds.has(n.id)) continue;
      seenNoteIds.add(n.id);
      row.notes = row.notes || [];
      row.notes.push({
        id: n.id, type: n.type, title: (n.title || "").slice(0, 120),
        preview: (n.message || "").slice(0, 160), link: n.link || undefined,
      });
    }
  }
  const discovered = new Set();
  for (const n of [...notifList, ...((discover && discover.notifications) || [])]) {
    const m = String(n.link || "").match(/\/dm\/([0-9a-f-]{4,})/i);
    if (m && m[1] !== "threads") discovered.add(m[1]);
  }
  for (const id of [...discovered]) {
    if (!cursor.known_peers.includes(id)) cursor.known_peers.push(id);
  }
  for (const peer of [...discovered].filter((id) => !threads[id])) {
    const th = await jget(`/api/dm/${peer}`, token).catch(() => ({ _status: "fetch_error" }));
    if (th._status) {
      errors.push({ where: `seat:${seat.label}/dm:${peer}`, error: `status_${th._status}` });
      continue;
    }
    if (!threads[peer]) threads[peer] = [];
    const seen = new Set(seenCur[peer] || []);
    const fresh = [];
    for (const msg of th.messages || []) {
      if (seen.has(msg.id)) continue;
      seen.add(msg.id);
      if (msg.from_agent_id && selfId && msg.from_agent_id === selfId) continue;
      fresh.push({ id: msg.id, from: msg.from_agent_id, at: msg.created_at, content: (msg.content || "").slice(0, 200) });
    }
    if (fresh.length) row.fresh[peer] = fresh;
    seenCur[peer] = [...seen].slice(-200);
    if ((th.messages || []).length) dmCur[peer] = (th.messages || [])[0].id;
    row.threads_with_new = Object.keys(row.fresh).length;
  }
  row.ok = true;
  row.self = selfId || undefined;
  seatOut[seat.label] = row;
}

writeFileSync(CURSOR, JSON.stringify(cursor, null, 2));
console.log(JSON.stringify({
  ok: errors.length === 0,
  channel: { ok: channelOk, new: freshChannel.length, newest: freshChannel.slice(0, 5) },
  seats: seatOut,
  errors,
  self: mainSelf || undefined,
  dm_key_present: seats.length > 0,
}, null, 2));
