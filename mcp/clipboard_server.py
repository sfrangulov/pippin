#!/usr/bin/env python3
"""Example MCP server (stdio, stdlib only): read and write the macOS clipboard."""
import json
import subprocess
import sys

TOOLS = [
    {"name": "get", "description": "Get the text currently on the clipboard.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
    {"name": "set", "description": "Put text on the clipboard.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
]


def handle(method, params):
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "clipboard", "version": "0.1"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name, args = params["name"], params.get("arguments") or {}
        if name == "get":
            text = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
            return {"content": [{"type": "text", "text": text[:6000] or "(clipboard is empty)"}]}
        if name == "set":
            subprocess.run(["pbcopy"], input=args.get("text", ""), text=True)
            return {"content": [{"type": "text", "text": "Copied to the clipboard."}]}
        return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
    if method == "ping":
        return {}
    raise KeyError(method)


for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue  # notification
    try:
        out = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg["method"], msg.get("params") or {})}
    except KeyError:
        out = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "method not found"}}
    print(json.dumps(out), flush=True)
