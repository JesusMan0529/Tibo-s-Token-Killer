"""A real child process for transport tests; never calls OpenAI."""

import json
import sys
import time


for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    result = {}
    if method == "account/rateLimits/read":
        print(json.dumps({"method": "test/notification", "params": {"text": "中文"}}), flush=True)
        result = {"ordinaryUsageAllowed": True, "rateLimits": {
            "limitId": "codex", "primary": {"usedPercent": 25, "windowDurationMins": 300}}}
    elif method == "test/error":
        print(json.dumps({"id": message["id"], "error": {"code": -32000, "message": "test error"}}), flush=True)
        continue
    elif method == "test/timeout":
        time.sleep(0.5)
        continue
    print(json.dumps({"id": message["id"], "result": result}), flush=True)
