import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, symlinkSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { formatRunView, isRegularOwnedFile, resolveBridgeCwd, sweepStaleBridgeFiles, withOwnerDefault, withRunView } from "../lib/usap-bridge.mjs";

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
  it("preserves implicit dispatches when routing.json is valid so Steak Pi resolves chains natively", async () => {
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
      assert.equal(workerRes.args.profile, undefined);
      assert.equal(workerRes.args.model, undefined);

      const revRes = await withOwnerDefault({ tasks: [{ task: "r1", role: "reviewer" }] }, routingFile);
      assert.equal(revRes.args.model, undefined);
      assert.equal(revRes.args.profile, undefined);

      const explicit = await withOwnerDefault({ model: "custom/explicit", tasks: [{ task: "t2" }] }, routingFile);
      assert.equal(explicit.args.model, "custom/explicit");
      assert.equal(explicit.note, undefined);

      const missing = join(dir, "missing.json");
      await assert.rejects(() => withOwnerDefault({ tasks: [{ task: "t3" }] }, missing), /unreadable/);

      // textWorkerChain-only routing is valid
      const chainOnly = join(dir, "chain-only.json");
      writeFileSync(chainOnly, JSON.stringify({ textWorkerChain: ["custom/worker-chain"] }));
      const chainRes = await withOwnerDefault({ tasks: [{ task: "t4" }] }, chainOnly);
      assert.equal(chainRes.args.model, undefined);
      assert.equal(chainRes.args.profile, undefined);

      // Invalid routing configs (null, empty chain, empty object) fail closed
      for (const bad of ["null", "[]", "{}", JSON.stringify({ textWorkerChain: [""] })]) {
        const badPath = join(dir, "bad.json");
        writeFileSync(badPath, bad);
        await assert.rejects(() => withOwnerDefault({ tasks: [{ task: "t5" }] }, badPath), /invalid|missing/);
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("resolves workspace cwd from STEAK_AGY_WORKSPACE_CWD, rejects symlinks, and sweeps dead-pid files", async () => {
    const dir = mkdtempSync(join(tmpdir(), "steak-agy-cwd-"));
    try {
      assert.equal(resolveBridgeCwd({ STEAK_AGY_WORKSPACE_CWD: dir }, "/fallback"), realpathSync(dir));
      assert.equal(resolveBridgeCwd({}, "/fallback"), "/fallback");
      const notADir = join(dir, "plain.txt");
      writeFileSync(notADir, "hi");
      assert.equal(resolveBridgeCwd({ STEAK_AGY_WORKSPACE_CWD: notADir }, "/fallback"), "/fallback");

      // isRegularOwnedFile accepts owned regular files and rejects symlinks
      assert.equal(await isRegularOwnedFile(notADir), true);
      const linkPath = join(dir, "link.txt");
      symlinkSync(notADir, linkPath);
      assert.equal(await isRegularOwnedFile(linkPath), false);

      const byPid = join(dir, "usap", "by-pid");
      const events = join(dir, "usap-events");
      await import("node:fs/promises").then(async (fs) => {
        await fs.mkdir(byPid, { recursive: true });
        await fs.mkdir(events, { recursive: true });
        const deadJson = join(byPid, "99999999.json");
        const deadTmp = join(byPid, "12345.json.99999998.tmp");
        const liveJson = join(byPid, `${process.pid}.json`);
        const linkJson = join(byPid, "99999997.json");
        const recentEvent = join(events, "99999996.jsonl");
        const oldEvent = join(events, "99999995.jsonl");
        const oldCursor = join(events, "99999995.cursor.json");
        const oldLock = join(events, "99999995.cursor.lock");
        await fs.writeFile(deadJson, "{}");
        await fs.writeFile(deadTmp, "{}");
        await fs.writeFile(liveJson, "{}");
        symlinkSync(liveJson, linkJson);
        await fs.writeFile(recentEvent, "{}\n");
        await fs.writeFile(oldEvent, "{}\n");
        await fs.writeFile(oldCursor, "{}");
        await fs.writeFile(oldLock, "");
        const now = Date.now();
        const oldSec = (now - 48 * 3600 * 1000) / 1000;
        utimesSync(oldEvent, oldSec, oldSec);
        utimesSync(oldCursor, oldSec, oldSec);
        utimesSync(oldLock, oldSec, oldSec);
        const removed = await sweepStaleBridgeFiles(dir, now, 24 * 3600 * 1000);
        assert.equal(removed, 5); // deadJson + deadTmp + oldEvent + oldCursor + oldLock
        assert.equal(statSync(liveJson).isFile(), true);
        assert.equal(statSync(recentEvent).isFile(), true);
        assert.equal(await isRegularOwnedFile(linkJson), false);
      });
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
