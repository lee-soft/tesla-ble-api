#!/usr/bin/env python3
"""
Minimal example client for tesla-ble-api — standard library only.

Usage:
    export TESLA_API=http://192.168.1.50:8710
    export TESLA_API_TOKEN=$(cat ~/tesla/api_token)
    python3 client.py state
    python3 client.py warm
    python3 client.py scare
    python3 client.py treats
    python3 client.py trunk-close
    python3 client.py lights-on          # (sends a default flash-pulse)
"""
import json
import os
import sys
import urllib.request

API   = os.environ.get("TESLA_API", "http://127.0.0.1:8710").rstrip("/")
TOKEN = os.environ.get("TESLA_API_TOKEN", "")


def call(action, body=None, method="POST"):
    url = "%s/%s" % (API, action)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer %s" % TOKEN)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "health"
    # GET endpoints:
    if action in ("state", "status", "health", "actions"):
        print(json.dumps(call(action, method="GET"), indent=2))
        return
    body = json.loads(sys.argv[2]) if len(sys.argv) > 2 else None
    print(json.dumps(call(action, body=body), indent=2))


if __name__ == "__main__":
    main()
