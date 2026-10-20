#!/usr/bin/env node
// Focused regression test for scripts/muse-team-poll.mjs (node:test, no deps).
// Spins a stub Rider API, runs the real poller as a child process, asserts:
//  - newest-first threads report only unseen, non-own messages as fresh
//  - a second run with the same cursor reports nothing new (quiet)
//  - failing seat key and erroring peer surface in `errors`, not silence
//  - team seats dir is polled per seat
import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { execFile } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const POLLER = join(HERE, "muse-team-poll.mjs");

// Newest-first thread fixtures.
const THREADS = {
  peerA: [
    { id: "m3", from_agent_id: "peerA", created_at: "t3", content: "hello new" },
    { id: "m2", from_agent_id: "SELF", created_at: "t2", content: "my own send" },
    { id: "m1", from_agent_id: "peerA", created_at: "t1", content: "old news" },
  ],
  peerB: [{ id: "n1", from_agent_id: "peerB", created_at: "t1", content: "solo" }],
  "519f567c0fa19436": [{ id: "q1", from_agent_id: "519f567c0fa19436", created_at: "t1", content: "off-list hello" }],
};
const CHANNEL = [
  { id: "c2", agent_id: "peerA", created_at: "t2", content: "chan new" },
  { id: "c1", agent_id: "peerB", created_at: "t1", content: "chan old" },
];

function stub() {
  return createServer((req, res) => {
    const u = new URL(req.url, "http://x");
    const send = (code, obj) => {
      res.writeHead(code, { "Content-Type": "application/json" });
      res.end(JSON.stringify(obj));
    };
    if (req.method === "POST" && u.pathname === "/api/rider/issue") {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", () => {
        const auth = req.headers.authorization || "";
        if (auth.includes("BAD")) return send(401, { error: "bad key" });
        send(200, { rider: "tok-" + auth.slice(-4) });
      });
      return;
    }
    if (u.pathname === "/api/channels/lab-team/messages") {
      return send(200, { messages: CHANNEL });
    }
    if (u.pathname === "/api/notifications") {
      return send(200, { notifications: [{ id: "nt1", type: "dm", title: "ping" }] });
    }
    if (u.pathname === "/api/dm/threads") {
      return send(200, {
        self_agent_id: "self-1", messages: [],
        notifications: [{ id: "nt9", type: "dm", title: "New message", message: "off-list hello", link: "/dm/519f567c0fa19436" }],
      });
    }
    const m = u.pathname.match(/^\/api\/dm\/([^/]+)$/);
    if (m) {
      if (m[1] === "peerERR") return send(500, { error: "boom" });
      const withSelf = (THREADS[m[1]] || []).map((x) => ({
        ...x, from_agent_id: x.from_agent_id === "SELF" ? "self-1" : x.from_agent_id,
      }));
      return send(200, { self_agent_id: "self-1", messages: withSelf });
    }
    send(404, {});
  });
}

function runPoll(env) {
  return new Promise((resolve, reject) => {
    execFile(process.execPath, [POLLER], { env, timeout: 20000 }, (err, stdout, stderr) => {
      if (err) return reject(new Error("poller failed: " + stderr.slice(0, 300)));
      resolve(JSON.parse(stdout));
    });
  });
}

test("fresh, quiet-second-run, errors surfaced, seats polled", async (t) => {
  const srv = stub();
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  const port = srv.address().port;
  t.after(() => srv.close());
  const dir = mkdtempSync(join(tmpdir(), "seats-"));
  writeFileSync(join(dir, "team1.json"), JSON.stringify({ agent_id: "tm1", api_key: "GOOD1" }));
  writeFileSync(join(dir, "broken.json"), JSON.stringify({ agent_id: "tmX", api_key: "BAD" }));
  const cursor = join(mkdtempSync(join(tmpdir(), "cur-")), "c.json");
  const base = {
    ...process.env,
    AGENT_RIDER_BASE: `http://127.0.0.1:${port}`,
    AR_KEY: "GOODMAIN",
    TEAM_SEATS_DIR: dir,
    MUSE_CURSOR: cursor,
    MUSE_PEERS: "peerA,peerB,peerERR",
  };

  const first = await runPoll(base);
  assert.equal(first.channel.new, 2);
  const main = first.seats.main;
  assert.ok(main.ok);
  // First sight: all unseen non-own history (m3 + m1); m2 excluded as own send.
  // Plus the discovered off-list peer found via notification link.
  assert.deepEqual(Object.keys(main.fresh).sort(), ["519f567c0fa19436", "peerA", "peerB"]);
  assert.deepEqual(main.fresh.peerA.map((m) => m.id), ["m3", "m1"]);
  assert.deepEqual(main.fresh["519f567c0fa19436"].map((m) => m.id), ["q1"]);
  assert.equal(main.notes.find((n) => n.id === "nt9").preview, "off-list hello");
  assert.deepEqual(main.fresh.peerB.map((m) => m.id), ["n1"]);
  // team seat polled too; broken seat + erroring peer surface.
  assert.ok(first.seats.team1.ok);
  assert.equal(first.seats.broken.ok, false);
  const where = first.errors.map((e) => e.where).sort();
  assert.ok(where.includes("seat:broken"), JSON.stringify(where));
  assert.ok(where.includes("seat:main/dm:peerERR"), JSON.stringify(where));
  assert.equal(first.ok, false);

  const second = await runPoll(base);
  assert.equal(second.channel.new, 0);
  assert.deepEqual(second.seats.main.fresh, {});
  assert.equal(second.ok, false); // errors persist, but nothing re-reports
  const cur = JSON.parse(readFileSync(cursor, "utf8"));
  assert.ok((cur.seen.peerA || []).includes("m3"));
  // Discovered off-list peer persists: thread stays polled after feeds drain.
  assert.ok((cur.known_peers || []).includes("519f567c0fa19436"));
});
