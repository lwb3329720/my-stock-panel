"""Run with the existing MCP environment; no credentials on the command line."""
import json
import os
import sys
from pathlib import Path


def main():
    sys.path.insert(0, str(Path(os.environ["HUATAI_RUNTIME_ROOT"]) / "src"))
    from desk.mcp_runtime import MCPRuntime

    request = json.load(sys.stdin)
    runtime = MCPRuntime(Path(os.environ["HUATAI_CACHE_ROOT"]),
                         Path(os.environ["HUATAI_PROFILE"]), "cordis-desktop")
    if request.get("inventory"):
        result = runtime.inventory(request["server"])
    else:
        result = runtime.call(request["server"], request["tool"], request["arguments"],
                              refresh=request.get("refresh", False))
        if result["status"] == "pending":
            result = runtime.wait(request["server"], request["tool"], request["arguments"])
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
