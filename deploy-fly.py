#!/usr/bin/env python3
"""Deploy RiderXL to Fly via the Machines API (no flyctl needed).

Steps: app-create (if missing) -> allocate dedicated v6 IP -> machine-create
with the GHCR image, PORT/RIDERXL_HOST env, and public HTTP service.

Usage: python3 deploy-fly.py <image>   (e.g. ghcr.io/ceedot-rock/riderxl:<sha>)
Auth: stored custom.fly credential via the skill's surrogate helper.
"""
import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import add_surrogate_to_request, read_response_body  # noqa: E402

MACHINES = "https://api.machines.dev"
GRAPHQL = "https://api.fly.io/graphql"
ALLOWED = ("api.machines.dev", "api.fly.io")
CRED = "custom.fly"
APP = "riderxl"
REGION = "ewr"

image = sys.argv[1] if len(sys.argv) > 1 else None
if not image:
    sys.exit("usage: deploy-fly.py <image>")


def mapi(method, path, payload=None, timeout=120):
    req = urllib.request.Request(
        MACHINES + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"} if payload is not None else {},
        method=method,
    )
    add_surrogate_to_request(req, CRED, allowed_hosts=ALLOWED)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(read_response_body(resp).decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def gql(query, variables):
    req = urllib.request.Request(
        GRAPHQL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    add_surrogate_to_request(req, CRED, allowed_hosts=ALLOWED)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(read_response_body(resp).decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


# 1. app exists?
st, apps = mapi("GET", "/v1/apps?org_slug=personal")
names = [a.get("name") for a in (apps if isinstance(apps, list) else [])]
if APP not in names:
    st, out = mapi("POST", "/v1/apps", {"app_name": APP, "org_slug": "personal"})
    print("app-create:", st, json.dumps(out)[:200])
    app_id = out.get("id") if isinstance(out, dict) else None
else:
    print("app exists:", APP)
    st, out = mapi("GET", f"/v1/apps/{APP}")
    app_id = out.get("id") if isinstance(out, dict) else None
print("app_id:", app_id)

# 2. dedicated v6 IP (free). Shared v4 is broken on this org (silent null).
# GraphQL appId wants the app NAME, not the Machines-API id.
st, out = gql("""query($name: String!) {
  app(name: $name) { ipAddresses { nodes { address type } } }
}""", {"name": APP})
print("existing ips:", st, json.dumps(out)[:300])
has_v6 = "v6" in json.dumps(out)
if not has_v6:
    q = """mutation($appId: ID!) {
      allocateIpAddress(input: {appId: $appId, type: v6}) {
        ipAddress { address type }
      }
    }"""
    st, out = gql(q, {"appId": APP})
    print("allocate v6:", st, json.dumps(out)[:300])

# 3. machine create (service)
payload = {
    "name": "riderxl-web",
    "region": REGION,
    "config": {
        "image": image,
        "guest": {"cpus": 1, "cpu_kind": "shared", "memory_mb": 256},
        "env": {"PORT": "8080", "RIDERXL_HOST": "0.0.0.0"},
        "services": [
            {
                "protocol": "tcp",
                "internal_port": 8080,
                "ports": [
                    {"port": 80, "handlers": ["http"]},
                    {"port": 443, "handlers": ["tls", "http"]},
                ],
            }
        ],
        "restart": {"policy": "always"},
        "auto_destroy": False,
    },
}
st, out = mapi("POST", f"/v1/apps/{APP}/machines", payload, timeout=300)
print("machine-create:", st)
print(json.dumps(out, indent=2)[:1500])
if isinstance(out, dict) and out.get("id"):
    mid = out["id"]
    for _ in range(30):
        time.sleep(10)
        st, cur = mapi("GET", f"/v1/apps/{APP}/machines/{mid}")
        state = cur.get("state") if isinstance(cur, dict) else "?"
        print("state:", state)
        if state == "started":
            break
    print("machine id:", mid)
