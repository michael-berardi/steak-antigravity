import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  statSync,
  symlinkSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import {
  INVALID_REQUEST,
  SHUTDOWN_READY_TIMEOUT_MS,
  createReplyGuard,
  formatRunView,
  isRegularOwnedFile,
  parseMessage,
  resolveBridgeCwd,
  settleWithin,
  sweepStaleBridgeFiles,
  tempSiblingPath,
  withOwnerDefault,
  withRunView,
} from "../lib/usap-bridge.mjs";

const ROOT = fileURLToPath(new URL("../", import.meta.url));
const SKIP = new Set([".git", "node_modules", "__pycache__"]);
const BRIDGE = join(ROOT, "lib/usap-bridge.mjs");
const SESSION_UUID = "0f0e0d0c-0b0a-4908-8706-050403020100";

// Minimal Pi SDK stand-in so stdio behaviour can be exercised without a real Pi
// runtime: it registers both USAP tool names and can hang, delay, or emit a late
// progress update on demand.
const FAKE_SDK_SOURCE = `
import * as path from "node:path";

const AGENT_DIR = process.env.FAKE_AGENT_DIR;
const HANG = process.env.FAKE_HANG_READY === "1";
const READY_DELAY_MS = Number(process.env.FAKE_READY_DELAY_MS || 0);
const EXEC_DELAY_MS = Number(process.env.FAKE_EXEC_DELAY_MS || 0);
const LATE_UPDATE_MS = Number(process.env.FAKE_LATE_UPDATE_MS || -1);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function getAgentDir() {
  return AGENT_DIR;
}

export class DefaultResourceLoader {
  async reload() {
    // A real hung start() holds live handles (open sockets, pending timers); the interval
    // reproduces that so only the shutdown timeout can end the process.
    if (HANG) await new Promise(() => setInterval(() => {}, 1000));
    await sleep(READY_DELAY_MS);
  }
}

export class SessionManager {
  static create() {
    return { getSessionFile: () => path.join(AGENT_DIR, "sessions", "host.json") };
  }
  static open(file) {
    return { getSessionFile: () => file };
  }
}

function definition(name) {
  return {
    name,
    description: "stand-in " + name,
    parameters: { type: "object" },
    async execute(toolCallId, args, signal, onUpdate) {
      if (LATE_UPDATE_MS >= 0) {
        setTimeout(() => onUpdate({ content: [{ type: "text", text: "late progress" }] }), LATE_UPDATE_MS);
      }
      onUpdate({ content: [{ type: "text", text: "working" }] });
      await sleep(EXEC_DELAY_MS);
      return { content: [{ type: "text", text: name + " done" }], details: {} };
    },
  };
}

export function createAgentSession() {
  return {
    session: {
      modelRegistry: { find: () => ({ id: "stand-in-parent" }) },
      setModel: async () => {},
      getToolDefinition: (name) => definition(name),
      subscribe: () => {},
      dispose: () => {},
      extensionRunner: { createContext: () => ({}) },
    },
  };
}
`;

// Write `lines` ([delayMs, line]) into a spawned bridge, close stdin, and collect stdout.
async function runBridge({ lines = [], closeAfterMs = 1200, env = {}, timeoutMs = 15_000 }) {
  const dir = mkdtempSync(join(tmpdir(), "steak-antigravity-bridge-"));
  const agentDir = join(dir, "agent");
  mkdirSync(join(agentDir, "sessions"), { recursive: true });
  const sdkPath = join(dir, "stand-in-pi-sdk.mjs");
  writeFileSync(sdkPath, FAKE_SDK_SOURCE);
  const child = spawn(
    process.execPath,
    ["--no-warnings", BRIDGE, sdkPath, ROOT, join(dir, "data"), SESSION_UUID, "antigravity"],
    {
      cwd: dir,
      env: {
        ...process.env,
        FAKE_AGENT_DIR: agentDir,
        STEAK_ANTIGRAVITY_PARENT_PID: "",
        STEAK_ANTIGRAVITY_WORKSPACE_CWD: dir,
        ...env,
      },
      stdio: ["pipe", "pipe", "pipe"],
    }
  );
  const stdoutChunks = [];
  const stderrChunks = [];
  child.stdout.on("data", (chunk) => stdoutChunks.push(chunk));
  child.stderr.on("data", (chunk) => stderrChunks.push(chunk));
  const exited = new Promise((resolve) => child.on("close", (code, signal) => resolve({ code, signal })));
  const timers = lines.map(([at, line]) =>
    setTimeout(() => {
      try {
        child.stdin.write(`${line}\n`);
      } catch {
        // Bridge already gone.
      }
    }, at)
  );
  timers.push(
    setTimeout(() => {
      try {
        child.stdin.end();
      } catch {
        // Bridge already gone.
      }
    }, closeAfterMs)
  );
  const killer = setTimeout(() => child.kill("SIGKILL"), timeoutMs);
  let result;
  try {
    const { code, signal } = await exited;
    const stdout = Buffer.concat(stdoutChunks).toString("utf8");
    // Every stdout line must be JSON-RPC: diagnostics belong on stderr.
    const messages = stdout
      .split("\n")
      .filter((line) => line.trim())
      .map((line) => JSON.parse(line));
    result = { code, signal, messages, stderr: Buffer.concat(stderrChunks).toString("utf8") };
  } finally {
    clearTimeout(killer);
    for (const timer of timers) clearTimeout(timer);
    rmSync(dir, { recursive: true, force: true });
  }
  return result;
}

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (SKIP.has(name)) continue;
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, out);
    else out.push(full);
  }
  return out;
}

describe("steak-antigravity manifest & contracts", () => {
  it("has valid plugin.json, mcp_config.json, and hooks.json at plugin root", () => {
    const plugin = JSON.parse(readFileSync(join(ROOT, "plugin.json"), "utf8"));
    assert.equal(plugin.name, "steak-antigravity");

    const mcp = JSON.parse(readFileSync(join(ROOT, "mcp_config.json"), "utf8"));
    assert.ok(mcp.mcpServers?.usap?.command);

    const hooks = JSON.parse(readFileSync(join(ROOT, "hooks.json"), "utf8"));
    assert.ok(hooks["steak-antigravity-session"]?.SessionStart?.length > 0);
    assert.ok(hooks["steak-antigravity-transcript"]?.PreInvocation?.length > 0);
    assert.ok(hooks["steak-antigravity-verify"]?.PostToolUse?.length > 0);
    assert.ok(hooks["steak-antigravity-stop"]?.Stop?.length > 0);
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
      if (file.endsWith("steak-antigravity.test.mjs")) continue;
      const content = readFileSync(file, "utf8");
      for (const rx of forbidden) {
        assert.ok(!rx.test(content), `Provider-neutrality violation (${rx}) in ${file}`);
      }
    }
  });

  it("keeps package.json, plugin.json, CHANGELOG.md, and MCP serverInfo versions aligned", () => {
    const pkg = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8"));
    const plugin = JSON.parse(readFileSync(join(ROOT, "plugin.json"), "utf8"));
    const changelog = readFileSync(join(ROOT, "CHANGELOG.md"), "utf8");
    const bridge = readFileSync(join(ROOT, "lib/usap-bridge.mjs"), "utf8");
    const unavailable = readFileSync(join(ROOT, "lib/mcp-unavailable.mjs"), "utf8");
    assert.equal(plugin.version, pkg.version, "plugin.json version must match package.json");
    assert.match(changelog, new RegExp("^## " + pkg.version.replaceAll(".", "\\.") + "(?:\\s|$)", "m"), "CHANGELOG.md must document current version");
    assert.doesNotMatch(bridge, /serverInfo:.*version:\s*"0\.1\.0"/, "usap-bridge must read version from package.json");
    assert.doesNotMatch(unavailable, /serverInfo:.*version:\s*"0\.1\.0"/, "mcp-unavailable must read version from package.json");
  });
});

describe("usap-bridge helpers", () => {
  it("preserves implicit dispatches when routing.json is valid so Steak Pi resolves chains natively", async () => {
    const dir = mkdtempSync(join(tmpdir(), "steak-antigravity-route-"));
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

  it("resolves workspace cwd from STEAK_ANTIGRAVITY_WORKSPACE_CWD, rejects symlinks, and sweeps dead-pid files", async () => {
    const dir = mkdtempSync(join(tmpdir(), "steak-antigravity-cwd-"));
    try {
      assert.equal(resolveBridgeCwd({ STEAK_ANTIGRAVITY_WORKSPACE_CWD: dir }, "/fallback"), realpathSync(dir));
      assert.equal(resolveBridgeCwd({}, "/fallback"), "/fallback");
      const notADir = join(dir, "plain.txt");
      writeFileSync(notADir, "hi");
      assert.equal(resolveBridgeCwd({ STEAK_ANTIGRAVITY_WORKSPACE_CWD: notADir }, "/fallback"), "/fallback");

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

describe("usap-bridge stdio hardening", () => {
  it("classifies non-object JSON-RPC payloads as invalid requests instead of crashing", () => {
    for (const line of ["null", "42", "0", "true", '"str"', "[1]", "[]", '[{"jsonrpc":"2.0"}]']) {
      assert.deepEqual(parseMessage(line), { kind: "invalid-request" }, `payload ${line}`);
    }
    assert.deepEqual(parseMessage("{not json"), { kind: "parse-error" });
    assert.deepEqual(parseMessage("   \n"), { kind: "empty" });
    assert.equal(parseMessage(undefined).kind, "empty");
    const ok = parseMessage('{"jsonrpc":"2.0","id":3,"method":"tools/list"}');
    assert.equal(ok.kind, "request");
    assert.equal(ok.message.method, "tools/list");
    assert.equal(INVALID_REQUEST, -32600);
  });

  it("guards replies and progress notifications with one lifecycle flag", () => {
    const live = new AbortController();
    const guard = createReplyGuard();
    assert.equal(guard.canReply(live.signal), true);
    assert.equal(guard.canUpdate(live.signal), true);
    guard.finish();
    assert.equal(guard.isFinished(), true);
    assert.equal(guard.canReply(live.signal), false);
    assert.equal(guard.canUpdate(live.signal), false);

    const aborted = new AbortController();
    aborted.abort();
    const cancelled = createReplyGuard();
    assert.equal(cancelled.canReply(aborted.signal), false);
    assert.equal(cancelled.canUpdate(aborted.signal), false);
    assert.equal(cancelled.canReply(undefined), true);
  });

  it("settles a hung ready promise as null within the shutdown timeout", async () => {
    assert.equal(SHUTDOWN_READY_TIMEOUT_MS, 3000);
    assert.equal(await settleWithin(new Promise(() => {}), 50), null);
    assert.equal(await settleWithin(Promise.reject(new Error("boom")), 50), null);
    const state = { session: {} };
    assert.equal(await settleWithin(Promise.resolve(state), 50), state);
  });

  it("mints collision-free temp names and sweeps stale map/by-pid temps older than an hour", async () => {
    const dir = mkdtempSync(join(tmpdir(), "steak-antigravity-tmp-"));
    try {
      const usap = join(dir, "usap");
      const byPid = join(usap, "by-pid");
      mkdirSync(byPid, { recursive: true });
      const mapFile = join(usap, `${SESSION_UUID}.json`);
      const first = tempSiblingPath(mapFile);
      const second = tempSiblingPath(mapFile);
      assert.notEqual(first, second, "temp names must not collide under flag wx");
      assert.match(basename(first), /\.json\.\d+\.\d+\.[0-9a-f]{12}\.tmp$/);
      assert.ok(first.startsWith(`${mapFile}.${process.pid}.`));

      const staleMapTmp = join(usap, basename(first));
      const freshMapTmp = join(usap, basename(second));
      const legacyMapTmp = `${mapFile}.${process.pid}.tmp`;
      const mapRecord = join(usap, `${SESSION_UUID}.json`);
      const deadPidLegacyTmp = join(byPid, "12345.json.99999998.tmp");
      const oldLivePidTmp = join(byPid, `${process.pid}.json.${process.pid}.${Date.now()}.aaaaaaaabbbb.tmp`);
      const deadPidFreshTmp = join(byPid, `99999997.json.${process.pid}.${Date.now()}.ccccccccdddd.tmp`);
      const liveFreshTmp = join(byPid, `${process.pid}.json.${process.pid}.${Date.now()}.eeeeeeeeffff.tmp`);
      const liveRecord = join(byPid, `${process.pid}.json`);
      for (const file of [staleMapTmp, freshMapTmp, legacyMapTmp, mapRecord, deadPidLegacyTmp, oldLivePidTmp, deadPidFreshTmp, liveFreshTmp, liveRecord]) {
        writeFileSync(file, "{}");
      }
      const now = Date.now();
      const oldSec = (now - 2 * 60 * 60 * 1000) / 1000;
      utimesSync(staleMapTmp, oldSec, oldSec);
      utimesSync(oldLivePidTmp, oldSec, oldSec);
      utimesSync(liveRecord, oldSec, oldSec);

      const removed = await sweepStaleBridgeFiles(dir, now, 24 * 60 * 60 * 1000);
      assert.equal(removed, 4); // staleMapTmp + deadPidLegacyTmp + oldLivePidTmp + deadPidFreshTmp
      for (const kept of [freshMapTmp, legacyMapTmp, mapRecord, liveFreshTmp, liveRecord]) {
        assert.equal(statSync(kept).isFile(), true, `${basename(kept)} should survive the sweep`);
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("answers initialize, two -32600 errors, and tools/list, and stays alive after bad payloads (spawn)", async () => {
    const { code, messages, stderr } = await runBridge({
      lines: [
        [50, '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'],
        [100, "null"],
        [150, "[1]"],
        [200, "42"],
        [250, '"str"'],
        [300, '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'],
      ],
      closeAfterMs: 1600,
    });
    assert.equal(code, 0, stderr);
    const init = messages.find((message) => message.id === 1);
    assert.equal(init?.result?.serverInfo?.name, "steak-antigravity-usap");
    const invalid = messages.filter((message) => message.error?.code === -32600);
    assert.equal(invalid.length, 4);
    for (const message of invalid) assert.equal(message.id, null);
    const list = messages.find((message) => message.id === 2);
    assert.deepEqual(list.result.tools.map((tool) => tool.name).sort(), ["ultraterm_hub", "ultraterm_subagents"]);
    assert.ok(messages.every((message) => message.jsonrpc === "2.0"));
    assert.doesNotMatch(stderr, /unhandled|UnhandledPromise/i);
  });

  it("honours notifications/cancelled that arrives while tools/call is still awaiting ready (spawn)", async () => {
    const { code, messages, stderr } = await runBridge({
      env: { FAKE_READY_DELAY_MS: "800", FAKE_EXEC_DELAY_MS: "50" },
      lines: [
        [50, '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"ultraterm_hub","arguments":{},"_meta":{"progressToken":"tok-7"}}}'],
        [250, '{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":7,"reason":"user"}}'],
        [1500, '{"jsonrpc":"2.0","id":8,"method":"tools/list"}'],
      ],
      closeAfterMs: 2400,
    });
    assert.equal(code, 0, stderr);
    // The cancelled request id must never receive a result or error.
    assert.equal(messages.filter((message) => message.id === 7).length, 0);
    const list = messages.find((message) => message.id === 8);
    assert.equal(list?.result?.tools?.length, 2);
  });

  it("drops progress notifications emitted after the final reply (spawn)", async () => {
    const { code, messages, stderr } = await runBridge({
      // Later than the 1s progress throttle, so only the finished flag can suppress it.
      env: { FAKE_LATE_UPDATE_MS: "1400" },
      lines: [
        [
          50,
          '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"ultraterm_hub","arguments":{},"_meta":{"progressToken":"tok-9"}}}',
        ],
      ],
      closeAfterMs: 2800,
    });
    assert.equal(code, 0, stderr);
    const replyIndex = messages.findIndex((message) => message.id === 9);
    assert.ok(replyIndex >= 0, "tools/call must reply");
    assert.equal(messages[replyIndex].result.isError, false);
    const late = messages.slice(replyIndex + 1).filter((message) => message.method === "notifications/progress");
    assert.equal(late.length, 0, "no progress after the final reply");
    const early = messages.slice(0, replyIndex).filter((message) => message.method === "notifications/progress");
    assert.ok(early.length >= 1, "in-flight progress is still forwarded");
  });

  it("exits after stdin closes even when start() hangs (spawn)", async () => {
    const started = Date.now();
    const { code, signal, messages, stderr } = await runBridge({
      env: { FAKE_HANG_READY: "1" },
      lines: [[50, '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}']],
      closeAfterMs: 400,
      timeoutMs: 20_000,
    });
    const elapsed = Date.now() - started;
    assert.equal(signal, null, "bridge must exit on its own, not be force-killed");
    assert.equal(code, 0, stderr);
    assert.equal(messages.find((message) => message.id === 1)?.result?.serverInfo?.name, "steak-antigravity-usap");
    assert.ok(elapsed < 12_000, `bridge lingered ${elapsed}ms after stdin closed`);
  });
});
