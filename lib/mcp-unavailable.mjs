// Fail-closed MCP stub: starts cleanly and exposes zero tools when Pi SDK or
// Steak Pi is unavailable, so the host sees USAP as unavailable with a named reason.
import { createInterface } from "node:readline";

const reason = process.argv[2] || "USAP runtime unavailable";
process.stderr.write(`[steak-antigravity-usap] unavailable: ${reason}\n`);

function send(msg) {
  process.stdout.write(`${JSON.stringify(msg)}\n`);
}

const rl = createInterface({ input: process.stdin });
rl.on("line", (line) => {
  if (!line.trim()) return;
  let req;
  try {
    req = JSON.parse(line);
  } catch {
    return;
  }
  const { id, method, params } = req;
  if (method === "initialize") {
    return send({
      jsonrpc: "2.0",
      id,
      result: {
        protocolVersion: params?.protocolVersion ?? "2025-06-18",
        capabilities: { tools: {} },
        serverInfo: { name: "steak-antigravity-usap", version: "0.1.0" },
      },
    });
  }
  if (method === "ping") return send({ jsonrpc: "2.0", id, result: {} });
  if (method === "tools/list") return send({ jsonrpc: "2.0", id, result: { tools: [] } });
  if (method === "tools/call") {
    return send({
      jsonrpc: "2.0",
      id,
      result: { isError: true, content: [{ type: "text", text: `USAP unavailable: ${reason}` }] },
    });
  }
  if (id !== undefined) {
    send({ jsonrpc: "2.0", id, error: { code: -32601, message: `method not found: ${method}` } });
  }
});
