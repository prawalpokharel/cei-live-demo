#!/usr/bin/env python3
"""A REAL shared service — the hidden dependency of experiment #2.

Writes a freshness token twice a second. Worker jobs configured with
DEP_TOKEN pointing at this file genuinely stall (no work, no progress)
whenever the token goes stale — which happens exactly when this process
is suspended (node probe) or killed (node failure). The scheduler is
never told which jobs depend on which service; only probing reveals it.

    SVC_TOKEN=/tmp/cei-deps/svc0.tok python3 svc_writer.py
"""
import os
import time

TOKEN = os.environ.get("SVC_TOKEN", "/tmp/cei-deps/svc0.tok")
os.makedirs(os.path.dirname(TOKEN), exist_ok=True)

while True:
    with open(TOKEN + ".tmp", "w") as f:
        f.write(str(time.time()))
    os.replace(TOKEN + ".tmp", TOKEN)
    time.sleep(0.5)
