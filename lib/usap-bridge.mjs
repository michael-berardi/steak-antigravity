// Provider-neutral USAP stdio MCP bridge for Antigravity CLI (agy).
//
// Hosts Steak Pi's own USAP extension (extensions/ultraterm-subagents.ts)
// inside a headless Pi SDK session and exposes its real `ultraterm_subagents`
// and `ultraterm_hub` tools over stdio MCP.
//
// Provider neutrality: carries no hardcoded model names, provider preferences,
// fallback chains, or paid-route grants. Explicit arguments win; omitted
// model/profile validates that `~/.config/ultraterm/routing.json` declares a
// default or worker chain and leaves model/profile unset for Steak Pi to resolve.
//
// argv: <pi-sdk-entry> <steak-pi-root> <plugin-data-dir> <session-id> <parent-model>
// stdout carries JSON-RPC only; diagnostics go to stderr.
import { promises as fs, realpathSync, statSync } from "node:fs";
import * as path from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath, pathToFileURL } from "node:url";

const [sdkEntry, steakRoot, dataDir, sessionId, parentModelId] = process.argv.slice(2);
const log = (...parts) => process.stderr.write(`[steak-antigravity-usap] ${parts.join(" ")}\n`);
console.log = (...parts) => log(...parts.map(String));
console.info = console.log;

const TOOL_NAMES = ["ultraterm_subagents", "ultraterm_hub"];
const PARENT_PROVIDER = "antigravity";
const PARENT_API = "steak-antigravity-parent";
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PID_RE = /^\d+$/;
const ROUTING_PATH = path.join(process.env.HOME ?? "", ".config", "ultraterm", "routing.json");

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}
function reply(id, result) {
  send({ jsonrpc: "2.0", id, result });
}
function fail(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

export async function withOwnerDefault(args, routingPath = ROUTING_PATH) {
  if (!args || typeof args !== "object") return { args: {} };
  if (args.harness !== undefined || args.model !== undefined || args.profile !== undefined) {
    return { args };
  }
  let routing;
  try {
    routing = JSON.parse(await fs.readFile(routingPath, "utf8"));
  } catch {
    throw new Error(
      `No owner worker default (${routingPath} unreadable); pass model or profile explicitly — no default was guessed.`
    );
  }
  if (!routing || typeof routing !== "object" || Array.isArray(routing)) {
    throw new Error(
      `Owner routing config in ${routingPath} is invalid; pass model or profile explicitly.`
    );
  }
  const isReview = Array.isArray(args.tasks) && args.tasks.some((task) => task?.role === "reviewer");
  const selector = isReview ? routing.reviewerDefault ?? routing.workerDefault : routing.workerDefault;
  const hasChain =
    Array.isArray(routing.textWorkerChain) &&
    routing.textWorkerChain.some((entry) => typeof entry === "string" && entry.trim().length > 0);
  const validSelector =
    selector &&
    typeof selector === "object" &&
    !Array.isArray(selector) &&
    (typeof selector.profile === "string" && selector.profile.trim().length > 0) !==
      (typeof selector.model === "string" && selector.model.trim().length > 0);
  if (!validSelector && !hasChain) {
    throw new Error(
      `Owner ${isReview ? "reviewer" : "worker"} default in ${routingPath} is missing or invalid; pass model or profile explicitly.`
    );
  }
  // Leave model/profile unset so Steak Pi resolves the automatic worker chain,
  // taskClasses, free routes, quota spillover, and implicitReview natively
  // without turning an implicit dispatch into an override requiring routeReason.
  return { args };
}

const TERMINAL_STATES = new Set(["done", "failed", "aborted", "cancelled", "timed_out", "interrupted"]);
const PROBLEM_STATES = new Set(["failed", "aborted", "cancelled", "timed_out", "interrupted"]);
const MAX_RUN_ROWS = 32;

function plainText(value) {
  return typeof value === "string"
    ? value.replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, " ").replace(/\s+/g, " ").trim()
    : "";
}

function taskCount(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
}

function stateKey(value) {
  return plainText(value).toLowerCase().replace(/\s+/g, "_") || "unknown";
}

function stateIcon(state) {
  return state === "done" ? "✓" : PROBLEM_STATES.has(state) ? "!" : state === "queued" ? "○" : "·";
}

function elapsed(startedAt, endedAt) {
  if (typeof startedAt !== "number" || !Number.isFinite(startedAt)) return "";
  const seconds = Math.max(0, Math.floor((taskCount(endedAt) - startedAt) / 1000));
  return seconds < 60
    ? `${seconds}s`
    : seconds < 3600
      ? `${Math.floor(seconds / 60)}m ${seconds % 60}s`
      : `${Math.floor(seconds / 3600)}h ${Math.floor(seconds / 60) % 60}m`;
}

export function formatRunView(run, now = Date.now()) {
  if (!run || typeof run !== "object" || Array.isArray(run)) return null;
  const tasks = (Array.isArray(run.tasks) ? run.tasks : []).filter(
    (task) => task && typeof task === "object" && !Array.isArray(task)
  );
  const keys = tasks.map((task) => stateKey(task.state));
  const done = keys.filter((k) => k === "done").length;
  const failed = keys.filter((k) => PROBLEM_STATES.has(k)).length;
  const queued = keys.filter((k) => k === "queued").length;
  const working = keys.filter((k) => !TERMINAL_STATES.has(k) && k !== "queued").length;
  const total = tasks.length;
  const parts = [`${done} done`];
  if (failed) parts.push(`${failed} failed`);
  if (working || queued) parts.push(`${working} working`, `${queued} queued`);
  const runId = plainText(run.runId ?? run.id);
  const observedAt =
    typeof run.observedAt === "number" && Number.isFinite(run.observedAt) ? run.observedAt : taskCount(now);
  const route =
    run.selection && typeof run.selection === "object" && !Array.isArray(run.selection)
      ? plainText(run.selection.route)
      : "";
  const routeSuffix = route ? ` · ${route}` : "";
  const rows = [
    `${stateIcon(stateKey(run.state))} Subagents · ${parts.join(" · ")} · ${total - working - queued}/${total}${runId ? ` · ${runId}` : ""}`,
  ];
  tasks.slice(0, MAX_RUN_ROWS).forEach((task, index) => {
    const ended = typeof task.endedAt === "number" && Number.isFinite(task.endedAt) ? task.endedAt : observedAt;
    const span = elapsed(task.startedAt, ended);
    const label = plainText(task.label) || plainText(task.taskId) || plainText(task.task) || "Unnamed task";
    rows.push(`${stateIcon(keys[index])} ${label}  ${stateKey(task.state).replace(/_/g, " ")}${span ? ` · ${span}` : ""}${routeSuffix}`);
  });
  if (tasks.length > MAX_RUN_ROWS) {
    rows.push(`… +${tasks.length - MAX_RUN_ROWS} more · hub status for details`);
  }
  return rows.join("\n");
}

export function withRunView(view, content) {
  const blocks = content
    .map((part) => (typeof part?.text === "string" ? { type: "text", text: part.text } : null))
    .filter(Boolean);
  if (!blocks.length) return [{ type: "text", text: view }];
  return [{ type: "text", text: `${view}\n\n${blocks[0].text}` }, ...blocks.slice(1)];
}

export async function isRegularOwnedFile(filePath) {
  try {
    const st = await fs.lstat(filePath);
    if (!st.isFile() || st.isSymbolicLink()) return false;
    if (typeof process.getuid === "function" && st.uid !== process.getuid()) return false;
    return true;
  } catch {
    return false;
  }
}

function pidIsAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === "EPERM";
  }
}

const BY_PID_TMP_RE = /^\d+\.json\.(\d+)\.tmp$/;
const EVENT_FILE_RE = /^(\d+)\.(?:jsonl|cursor\.json|cursor\.lock)$/;

export async function sweepStaleBridgeFiles(baseDir, nowMs = Date.now(), maxAgeMs = 24 * 60 * 60 * 1000) {
  const dirs = [
    { dir: path.join(baseDir, "usap", "by-pid"), mode: "by-pid" },
    { dir: path.join(baseDir, "usap-events"), mode: "events" },
  ];
  let removed = 0;
  for (const { dir, mode } of dirs) {
    let entries;
    try {
      entries = await fs.readdir(dir);
    } catch {
      continue;
    }
    for (const name of entries) {
      let pid;
      let requireAge = false;
      if (mode === "by-pid") {
        if (name.endsWith(".json")) {
          const stem = name.slice(0, -".json".length);
          if (!PID_RE.test(stem)) continue;
          pid = Number(stem);
        } else {
          const match = BY_PID_TMP_RE.exec(name);
          if (!match) continue;
          pid = Number(match[1]);
        }
      } else {
        const match = EVENT_FILE_RE.exec(name);
        if (!match) continue;
        pid = Number(match[1]);
        requireAge = true;
      }
      if (pidIsAlive(pid)) continue;
      const full = path.join(dir, name);
      try {
        const st = await fs.lstat(full);
        if (st.isSymbolicLink() || !st.isFile()) continue;
        if (requireAge && nowMs - st.mtimeMs < maxAgeMs) continue;
        await fs.unlink(full);
        removed += 1;
      } catch {
        // Ignore concurrent cleanup.
      }
    }
  }
  return removed;
}

export function resolveBridgeCwd(env = process.env, fallback = process.cwd()) {
  const raw = (env.STEAK_ANTIGRAVITY_WORKSPACE_CWD || env.ULTRATERM_WORKSPACE_CWD || "").trim();
  if (raw && path.isAbsolute(raw)) {
    try {
      const resolved = realpathSync(raw);
      if (statSync(resolved).isDirectory()) return resolved;
    } catch {
      // Fall through to fallback.
    }
  }
  return fallback;
}

async function sessionFileFor(sdk, cwd, agentDir) {
  const mapDir = path.join(dataDir, "usap");
  await fs.mkdir(mapDir, { recursive: true, mode: 0o700 });
  const mapFile = path.join(mapDir, `${sessionId}.json`);
  const sessionsRoot = path.resolve(path.join(agentDir, "sessions"));
  try {
    const saved = JSON.parse(await fs.readFile(mapFile, "utf8"));
    if (
      saved?.sessionId === sessionId &&
      typeof saved.sessionFile === "string" &&
      path.isAbsolute(saved.sessionFile) &&
      (await isRegularOwnedFile(saved.sessionFile))
    ) {
      const real = realpathSync(saved.sessionFile);
      const rel = path.relative(sessionsRoot, real);
      if (rel && !rel.startsWith("..") && !path.isAbsolute(rel)) {
        return { manager: sdk.SessionManager.open(real), mapFile, existing: true };
      }
      log(`ignored out-of-root sessionFile in ${mapFile}: ${real}`);
    }
  } catch {
    // First bridge start for this session id.
  }
  const allocated = sdk.SessionManager.create(cwd).getSessionFile?.();
  if (typeof allocated !== "string" || !allocated) {
    throw new Error("Pi did not allocate a USAP host session file");
  }
  await fs.mkdir(path.dirname(allocated), { recursive: true, mode: 0o700 });
  await fs.writeFile(allocated, "", { flag: "wx", mode: 0o600 });
  return { manager: sdk.SessionManager.open(allocated), mapFile, existing: false };
}

async function start() {
  if (!UUID_RE.test(sessionId ?? "")) {
    throw new Error(`Session id is missing or not a UUID (got ${JSON.stringify(sessionId ?? null)})`);
  }
  void sweepStaleBridgeFiles(dataDir).catch(() => {});
  const sdk = await import(pathToFileURL(sdkEntry).href);
  const cwd = resolveBridgeCwd();
  const agentDir = sdk.getAgentDir();
  const modelId = parentModelId || "antigravity";

  const standIn = (pi) => {
    pi.registerProvider(PARENT_PROVIDER, {
      name: "Antigravity (Steak Antigravity USAP parent)",
      baseUrl: "https://antigravity-parent.invalid",
      apiKey: "antigravity-parent-never-sends",
      api: PARENT_API,
      streamSimple() {
        throw new Error("The Antigravity USAP parent is a stand-in; it never runs inference through Pi.");
      },
      models: [
        {
          id: modelId,
          name: `Antigravity ${modelId}`.trim(),
          input: ["text", "image"],
          reasoning: true,
          contextWindow: 200000,
          maxTokens: 64000,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        },
      ],
    });
  };

  const resourceLoader = new sdk.DefaultResourceLoader({
    cwd,
    agentDir,
    noExtensions: true,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    additionalExtensionPaths: [path.join(steakRoot, "extensions", "ultraterm-subagents.ts")],
    extensionFactories: [standIn],
  });
  await resourceLoader.reload();
  const { manager, mapFile, existing } = await sessionFileFor(sdk, cwd, agentDir);
  const { session } = await sdk.createAgentSession({ cwd, agentDir, resourceLoader, sessionManager: manager });
  const parent =
    session.modelRegistry?.find?.(PARENT_PROVIDER, modelId) ??
    session.extensionRunner?.createContext?.().modelRegistry?.find?.(PARENT_PROVIDER, modelId);
  if (!parent) throw new Error("Could not register Antigravity parent identity with Pi");
  await session.setModel(parent);

  const file = manager.getSessionFile?.();
  if (file && !existing) {
    const mapTmp = `${mapFile}.${process.pid}.tmp`;
    await fs.writeFile(mapTmp, JSON.stringify({ sessionId, sessionFile: file, createdAt: Date.now() }), {
      flag: "wx",
      mode: 0o600,
    });
    await fs.rename(mapTmp, mapFile);
  }

  const parentPid = process.env.STEAK_ANTIGRAVITY_PARENT_PID ?? "";
  if (file && PID_RE.test(parentPid)) {
    const byPid = path.join(dataDir, "usap", "by-pid");
    await fs.mkdir(byPid, { recursive: true, mode: 0o700 });
    const target = path.join(byPid, `${parentPid}.json`);
    const temporary = `${target}.${process.pid}.tmp`;
    await fs.writeFile(
      temporary,
      JSON.stringify({ parentPid: Number(parentPid), bridgePid: process.pid, sessionId, sessionFile: file, createdAt: Date.now() }),
      { flag: "wx", mode: 0o600 }
    );
    await fs.rename(temporary, target);

    const events = path.join(dataDir, "usap-events", `${parentPid}.jsonl`);
    await fs.mkdir(path.dirname(events), { recursive: true, mode: 0o700 });
    session.subscribe((event) => {
      const message = event?.message;
      if (event?.type !== "message_end" || message?.customType !== "ultraterm-subagents-complete") return;
      void fs
        .appendFile(events, `${JSON.stringify({ at: Date.now(), runIds: message.details?.runIds ?? [] })}\n`, {
          mode: 0o600,
        })
        .catch((error) => log(`could not append USAP completion notice: ${error?.message ?? error}`));
    });
  }

  const tools = TOOL_NAMES.map((name) => ({ name, definition: session.getToolDefinition(name) }));
  const missing = tools.filter((tool) => !tool.definition).map((tool) => tool.name);
  if (missing.length) throw new Error(`Steak Pi did not register ${missing.join(", ")}`);
  return { session, tools, hostSession: file };
}

let ready;
const inflight = new Map();

function describe(definition) {
  const guidelines = (definition.promptGuidelines ?? []).map((line) => `- ${line}`).join("\n");
  const snippet = definition.promptSnippet ? `\n\n${definition.promptSnippet}` : "";
  return `${definition.description}${snippet}${guidelines ? `\n\nGuidelines (Steak Pi USAP):\n${guidelines}` : ""}`;
}

function textParts(result) {
  const parts = Array.isArray(result?.content) ? result.content : [];
  const content = parts.map((part) => (part?.type === "text" ? { type: "text", text: part.text } : null)).filter(Boolean);
  return content.length ? content : [{ type: "text", text: JSON.stringify(result?.details ?? result ?? null) }];
}

async function handle(message) {
  const { id, method, params } = message;
  if (method === "initialize") {
    return reply(id, {
      protocolVersion: params?.protocolVersion ?? "2025-06-18",
      capabilities: { tools: {} },
      serverInfo: { name: "steak-antigravity-usap", version: "0.1.0" },
    });
  }
  if (method === "ping") return reply(id, {});
  if (method === "notifications/cancelled") {
    inflight.get(params?.requestId)?.abort();
    return;
  }
  if (method?.startsWith("notifications/")) return;
  if (method === "tools/list") {
    try {
      const { tools } = await ready;
      return reply(id, {
        tools: tools.map(({ name, definition }) => ({
          name,
          description: describe(definition),
          inputSchema: JSON.parse(JSON.stringify(definition.parameters ?? { type: "object" })),
        })),
      });
    } catch (error) {
      log(`unavailable: ${error?.message ?? error}`);
      return reply(id, { tools: [] });
    }
  }
  if (method === "tools/call") {
    let state;
    try {
      state = await ready;
    } catch (error) {
      return reply(id, {
        isError: true,
        content: [{ type: "text", text: `USAP is unavailable from Antigravity: ${error?.message ?? error}. Work directly and say so.` }],
      });
    }
    const tool = state.tools.find((entry) => entry.name === params?.name);
    if (!tool) return fail(id, -32602, `unknown tool ${params?.name}`);
    const controller = new AbortController();
    inflight.set(id, controller);
    try {
      const context = state.session.extensionRunner.createContext();
      const { args } =
        tool.name === "ultraterm_subagents"
          ? await withOwnerDefault(params?.arguments ?? {})
          : { args: params?.arguments ?? {} };
      const token = params?._meta?.progressToken;
      let step = 0;
      let last = 0;
      const onUpdate = (update) => {
        if (token === undefined) return;
        const now = Date.now();
        if (now - last < 1000) return;
        last = now;
        const msg = (Array.isArray(update?.content) ? update.content : [])
          .map((part) => (part?.type === "text" ? part.text : ""))
          .join("\n")
          .slice(0, 2000);
        if (msg) {
          send({
            jsonrpc: "2.0",
            method: "notifications/progress",
            params: { progressToken: token, progress: ++step, message: msg },
          });
        }
      };
      const result = await tool.definition.execute(`agy-${id}`, args, controller.signal, onUpdate, context);
      const content = textParts(result);
      let presented = content;
      const run = result?.details?.run;
      if (run && typeof run === "object" && !Array.isArray(run) && (run.runId || run.id || Array.isArray(run.tasks))) {
        const view = formatRunView(run);
        if (view) presented = withRunView(view, content);
      }
      return reply(id, {
        content: presented,
        isError: false,
      });
    } catch (error) {
      return reply(id, { isError: true, content: [{ type: "text", text: String(error?.message ?? error) }] });
    } finally {
      inflight.delete(id);
    }
  }
  if (id !== undefined) fail(id, -32601, `method not found: ${method}`);
}

function invokedDirectly() {
  if (!process.argv[1]) return false;
  try {
    if (pathToFileURL(process.argv[1]).href === import.meta.url) return true;
    return realpathSync(process.argv[1]) === fileURLToPath(import.meta.url);
  } catch {
    return false;
  }
}

if (invokedDirectly()) {
  // Never leak parent UI Mode transcript or parent pane/harness identity to spawned workers.
  for (const key of Object.keys(process.env)) {
    if (
      key.startsWith("ULTRATERM_UI_") ||
      key === "ULTRATERM_MAIN_SCREEN" ||
      key === "CLAUDE_CODE_SESSION_ID" ||
      key === "STEAK_ANTIGRAVITY_OWNED_PATHS"
    ) {
      delete process.env[key];
    }
  }

  ready = start();
  ready.then(
    (state) => log(`ready; host session ${state.hostSession ?? "(in memory)"}`),
    (error) => log(`failed: ${error?.stack ?? error}`)
  );

  const input = createInterface({ input: process.stdin });
  input.on("line", (line) => {
    if (!line.trim()) return;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      return fail(null, -32700, "parse error");
    }
    void handle(message).catch(
      (error) => message.id !== undefined && fail(message.id, -32603, String(error?.message ?? error))
    );
  });
  const shutdown = async () => {
    for (const controller of inflight.values()) controller.abort();
    try {
      (await ready).session.dispose();
    } catch {
      // Bridge never finished starting.
    }
    process.exit(0);
  };
  input.on("close", shutdown);
  process.on("SIGTERM", shutdown);
}
