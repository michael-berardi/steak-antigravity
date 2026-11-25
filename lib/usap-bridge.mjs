// Provider-neutral USAP stdio MCP bridge for Antigravity CLI (agy).
//
// Hosts Steak Pi's own USAP extension (extensions/ultraterm-subagents.ts)
// inside a headless Pi SDK session and exposes its real `ultraterm_subagents`
// and `ultraterm_hub` tools over stdio MCP.
//
// Provider neutrality: carries no hardcoded model names, provider preferences,
// fallback chains, or paid-route grants. Explicit arguments win; omitted
// model/profile reads the operator's `~/.config/ultraterm/routing.json`
// (`workerDefault` / `reviewerDefault`).
//
// argv: <pi-sdk-entry> <steak-pi-root> <plugin-data-dir> <session-id> <parent-model>
// stdout carries JSON-RPC only; diagnostics go to stderr.
import { promises as fs, realpathSync } from "node:fs";
import * as path from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath, pathToFileURL } from "node:url";

const [sdkEntry, steakRoot, dataDir, sessionId, parentModelId] = process.argv.slice(2);
const log = (...parts) => process.stderr.write(`[steak-agy-usap] ${parts.join(" ")}\n`);
console.log = (...parts) => log(...parts.map(String));
console.info = console.log;

const TOOL_NAMES = ["ultraterm_subagents", "ultraterm_hub"];
const PARENT_PROVIDER = "antigravity";
const PARENT_API = "steak-agy-parent";
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
  const isReview = Array.isArray(args.tasks) && args.tasks.some((task) => task?.role === "reviewer");
  const selector = isReview ? routing.reviewerDefault ?? routing.workerDefault : routing.workerDefault;
  const valid =
    selector &&
    typeof selector === "object" &&
    (typeof selector.profile === "string") !== (typeof selector.model === "string");
  if (!valid) {
    throw new Error(
      `Owner ${isReview ? "reviewer" : "worker"} default in ${routingPath} is missing or invalid; pass model or profile explicitly.`
    );
  }
  const key = selector.profile !== undefined ? "profile" : "model";
  return {
    args: { ...args, [key]: selector[key] },
    note: `Owner ${isReview ? "reviewer" : "worker"} default applied from routing.json: ${key} ${selector[key]}.`,
  };
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

async function sessionFileFor(sdk, cwd) {
  const mapDir = path.join(dataDir, "usap");
  await fs.mkdir(mapDir, { recursive: true, mode: 0o700 });
  const mapFile = path.join(mapDir, `${sessionId}.json`);
  try {
    const saved = JSON.parse(await fs.readFile(mapFile, "utf8"));
    if (saved?.sessionId === sessionId && typeof saved.sessionFile === "string") {
      await fs.access(saved.sessionFile);
      return { manager: sdk.SessionManager.open(saved.sessionFile), mapFile, existing: true };
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
  const sdk = await import(pathToFileURL(sdkEntry).href);
  const cwd = process.cwd();
  const agentDir = sdk.getAgentDir();
  const modelId = parentModelId || "antigravity";

  const standIn = (pi) => {
    pi.registerProvider(PARENT_PROVIDER, {
      name: "Antigravity (Steak AGY USAP parent)",
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
  const { manager, mapFile, existing } = await sessionFileFor(sdk, cwd);
  const { session } = await sdk.createAgentSession({ cwd, agentDir, resourceLoader, sessionManager: manager });
  const parent =
    session.modelRegistry?.find?.(PARENT_PROVIDER, modelId) ??
    session.extensionRunner?.createContext?.().modelRegistry?.find?.(PARENT_PROVIDER, modelId);
  if (!parent) throw new Error("Could not register Antigravity parent identity with Pi");
  await session.setModel(parent);

  const file = manager.getSessionFile?.();
  if (file && !existing) {
    await fs.writeFile(mapFile, JSON.stringify({ sessionId, sessionFile: file, createdAt: Date.now() }), {
      mode: 0o600,
    });
  }

  const parentPid = process.env.STEAK_AGY_PARENT_PID ?? "";
  if (file && PID_RE.test(parentPid)) {
    const byPid = path.join(dataDir, "usap", "by-pid");
    await fs.mkdir(byPid, { recursive: true, mode: 0o700 });
    const target = path.join(byPid, `${parentPid}.json`);
    const temporary = `${target}.${process.pid}.tmp`;
    await fs.writeFile(
      temporary,
      JSON.stringify({ parentPid: Number(parentPid), bridgePid: process.pid, sessionId, sessionFile: file, createdAt: Date.now() }),
      { mode: 0o600 }
    );
    await fs.rename(temporary, target);

    const events = path.join(dataDir, "usap-events", `${parentPid}.jsonl`);
    await fs.mkdir(path.dirname(events), { recursive: true, mode: 0o700 });
    session.subscribe((event) => {
      const message = event?.message;
      if (event?.type !== "message_end" || message?.customType !== "ultraterm-subagents-complete") return;
      const content = typeof message.content === "string" ? message.content : "";
      void fs
        .appendFile(events, `${JSON.stringify({ at: Date.now(), runIds: message.details?.runIds ?? [], text: content })}\n`, {
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
      serverInfo: { name: "steak-agy-usap", version: "0.1.0" },
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
      const { args, note } =
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
        content: note ? [{ type: "text", text: note }, ...presented] : presented,
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
  // Never leak parent UI Mode transcript variables to spawned workers.
  delete process.env.ULTRATERM_UI_TRANSCRIPT_FILE;
  delete process.env.ULTRATERM_UI_SESSION_ID;
  delete process.env.ULTRATERM_MAIN_SCREEN;
  delete process.env.CLAUDE_CODE_SESSION_ID;

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
