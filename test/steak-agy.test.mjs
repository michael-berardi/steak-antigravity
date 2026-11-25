import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { formatRunView, withOwnerDefault, withRunView } from "../lib/usap-bridge.mjs";

const ROOT = fileURLToPath(new URL("../", import.meta.url));
const SKIP = new Set([".git", "node_modules", "__pycache__"]);

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (SKIP.has(name)) continue;
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, out);
    else out.push(full);
  }
  return out;
}

describe("steak-agy manifest & contracts", () => {
  it("has valid plugin.json, mcp_config.json, and hooks.json at plugin root", () => {
    const plugin = JSON.parse(readFileSync(join(ROOT, "plugin.json"), "utf8"));
    assert.equal(plugin.name, "steak-agy");

    const mcp = JSON.parse(readFileSync(join(ROOT, "mcp_config.json"), "utf8"));
    assert.ok(mcp.mcpServers?.usap?.command);

    const hooks = JSON.parse(readFileSync(join(ROOT, "hooks.json"), "utf8"));
    assert.ok(hooks["steak-agy-session"]?.SessionStart?.length > 0);
    assert.ok(hooks["steak-agy-transcript"]?.PreInvocation?.length > 0);
    assert.ok(hooks["steak-agy-verify"]?.PostToolUse?.length > 0);
    assert.ok(hooks["steak-agy-stop"]?.Stop?.length > 0);
  });

  it("is strictly provider-neutral (no hardcoded model IDs or operator/client names)", () => {
    const forbidden = [
      /\bclaude-(?:opus|sonnet|haiku|fable)-[0-9]/i,
      /\bgpt-[0-9]\.[0-9]-(?:sol|astra|terra|luna)/i,
      /\bgemini-[0-9]-(?:neon|carbon|hydrogen)/i,
      /\bdeepseek-v[0-9]/i,
      /\bqwen[0-9]-max/i,
      /\bLiberty Design Studio\b/i,
      /\blucero-?legal\b/i,
      /\bdubhaven\b/i,
      /\bplatastar\b/i,
      /\bplataplace\b/i,
    ];
    for (const file of walk(ROOT)) {
      if (file.endsWith("steak-agy.test.mjs")) continue;
      const content = readFileSync(file, "utf8");
      for (const rx of forbidden) {
        assert.ok(!rx.test(content), `Provider-neutrality violation (${rx}) in ${file}`);
      }
    }
  });
});

describe("usap-bridge helpers", () => {
  it("applies owner defaults from routing.json without hardcoding models", async () => {
    const dir = mkdtempSync(join(tmpdir(), "steak-agy-route-"));
    try {
      const routingFile = join(dir, "routing.json");
      writeFileSync(
        routingFile,
        JSON.stringify({
          workerDefault: { profile: "custom/worker-a" },
          reviewerDefault: { model: "custom/reviewer-b" },
        })
      );
      const workerRes = await withOwnerDefault({ tasks: [{ task: "t1" }] }, routingFile);
      assert.equal(workerRes.args.profile, "custom/worker-a");

      const revRes = await withOwnerDefault({ tasks: [{ task: "r1", role: "reviewer" }] }, routingFile);
      assert.equal(revRes.args.model, "custom/reviewer-b");

      const explicit = await withOwnerDefault({ model: "custom/explicit", tasks: [{ task: "t2" }] }, routingFile);
      assert.equal(explicit.args.model, "custom/explicit");
      assert.equal(explicit.note, undefined);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("formats compact USAP run views", () => {
    const view = formatRunView({
      runId: "run-123",
      state: "done",
      observedAt: 20_000,
      tasks: [{ label: "Audit", state: "done", startedAt: 10_000, endedAt: 15_000 }],
    });
    assert.match(view, /✓ Subagents · 1 done · 1\/1 · run-123/);
    assert.match(view, /✓ Audit\s+done · 5s/);
    const merged = withRunView(view, [{ type: "text", text: "details" }]);
    assert.equal(merged[0].text, `${view}\n\ndetails`);
  });
});
