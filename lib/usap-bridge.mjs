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
import { randomBytes } from "node:crypto";
import { promises as fs, realpathSync, statSync, readFileSync } from "node:fs";
import * as path from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath, pathToFileURL } from "node:url";

const PLUGIN_VERSION = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version;
if (typeof PLUGIN_VERSION !== "string" || !PLUGIN_VERSION.trim()) {
  throw new Error("steak-antigravity-usap: missing valid version in package.json");
}

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

// JSON-RPC error codes used over stdio.
export const PARSE_ERROR = -32700;
export const INVALID_REQUEST = -32600;
export const INTERNAL_ERROR = -32603;

// How long shutdown() waits for start() before exiting anyway.
export const SHUTDOWN_READY_TIMEOUT_MS = 3000;

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

// Classify one stdin line before touching it. A line is only ever dispatched as a
// JSON-RPC request when it parses to a plain object, so a `null`/number/string/array
// payload can never reach message destructuring or crash the process.
export function parseMessage(line) {
  if (typeof line !== "string" || !line.trim()) return { kind: "empty" };
  let value;
  try {
    value = JSON.parse(line);
  } catch {
    return { kind: "parse-error" };
  }
  if (!isPlainObject(value)) return { kind: "invalid-request" };
  return { kind: "request", message: value };
}

// Tracks the one-shot lifecycle of a tools/call reply: the MCP spec forbids a result
// for a cancelled request, and progress notifications sent after the final reply are
// meaningless to the client, so both are dropped here.
export function createReplyGuard() {
  let finished = false;
  const open = (signal) => !finished && !signal?.aborted;
  return {
    isFinished: () => finished,
    finish: () => {
      finished = true;
    },
    canReply: open,
    canUpdate: open,
  };
}

// Unique sibling temp path: a leftover from a crashed bridge whose pid was reused can
// never collide (and trip EEXIST under flag `wx`) with a later run's temp file.
export function tempSiblingPath(filePath) {
  return `${filePath}.${process.pid}.${Date.now()}.${randomBytes(6).toString("hex")}.tmp`;
}

// Resolve the ready promise, or null when start() has not settled inside the timeout.
export async function settleWithin(promise, timeoutMs = SHUTDOWN_READY_TIMEOUT_MS) {
  let timer;
  const timeout = new Promise((resolve) => {
    // Referenced on purpose: the caller awaits the race, and the timer is cleared once
    // it settles, so it never delays a healthy shutdown.
    timer = setTimeout(() => resolve(null), timeoutMs);
  });
  try {
    return await Promise.race([Promise.resolve(promise).then((value) => value, () => null), timeout]);
  } finally {
    clearTimeout(timer);
  }
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
const MAX_EVENT_COUNT = 10_000;
const MAX_SEEN_RUN_EVENTS = 256;
export const DEFAULT_STALL_THRESHOLD_MS = 5 * 60 * 1000;
export const USAP_EVENT_KINDS = new Set(["complete", "stall", "route_hop"]);
// Must match scripts/ui_transcript_sync.py _SAFE_RUN_ID_RE exactly.
export const SAFE_RUN_ID_RE = /^[A-Za-z0-9._:-]{1,64}$/;

function plainText(value, maxLen = 120) {
  if (typeof value !== "string") return "";
  const clean = value
    .replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return clean.length > maxLen ? `${clean.slice(0, Math.max(1, maxLen - 1))}…` : clean;
}

function taskCount(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
}

function boundedCount(value) {
  return Math.min(MAX_EVENT_COUNT, taskCount(value));
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

function sanitizeRunIds(rawIds) {
  if (!Array.isArray(rawIds)) return [];
  const out = [];
  const seen = new Set();
  for (const item of rawIds) {
    if (typeof item !== "string" || !SAFE_RUN_ID_RE.test(item) || seen.has(item)) continue;
    seen.add(item);
    out.push(item);
    if (out.length >= 16) break;
  }
  return out;
}

function stringList(raw, maxItems = 4, maxLen = 40) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const entry of raw) {
    const clean = plainText(entry, maxLen);
    if (clean) out.push(clean);
    if (out.length >= maxItems) break;
  }
  return out;
}

function normalizeHop(raw) {
  const clean = plainText(raw, 96);
  if (!clean) return "";
  return clean.replace(/\s*->\s*/g, " -> ");
}

function taskRouteHops(task) {
  const hops = [];
  if (Array.isArray(task.routeFallbacks)) {
    for (const entry of task.routeFallbacks.slice(-4)) {
      const hop = normalizeHop(entry);
      if (hop) hops.push(hop);
    }
  }
  if (task.reviewFallback && typeof task.reviewFallback === "object" && !Array.isArray(task.reviewFallback)) {
    const to = plainText(task.reviewFallback.to, 64);
    if (to && to !== "none") hops.push(`fallback -> ${to}`);
  }
  return hops;
}

function taskWorktreeBadge(task) {
  const wt = task.worktree && typeof task.worktree === "object" && !Array.isArray(task.worktree) ? task.worktree : null;
  const branch = plainText(wt?.branch ?? task.worktreeBranch, 48);
  const wtPath = plainText(wt?.path ?? wt?.worktreePath ?? task.worktreePath, 64);
  const dirty = wt?.dirty === true || task.worktreeDirty === true;
  if (!branch && !wtPath) return "";
  const base = branch && wtPath ? `${branch} (${wtPath})` : branch || wtPath;
  return `wt ${base}${dirty ? "*" : ""}`;
}

function taskDependencyBadge(task, key, stateByRef) {
  const explicitAwaiting = stringList(task.awaitingDependencies, 4, 36);
  const deps = stringList(task.dependsOn, 4, 36);
  if (key === "queued") {
    if (explicitAwaiting.length > 0) return `awaiting ${explicitAwaiting.join(", ")}`;
    if (deps.length > 0) {
      const pending = deps.filter((ref) => stateByRef.get(ref) !== "done");
      const shown = pending.length > 0 ? pending : deps;
      return `awaiting ${shown.join(", ")}`;
    }
    return "";
  }
  if (deps.length > 0) return `after ${deps.join(", ")}`;
  return "";
}

function runStalledSet(run) {
  const set = new Set();
  if (!Array.isArray(run?.stalledTasks)) return set;
  for (const item of run.stalledTasks) {
    if (typeof item === "string") {
      const clean = plainText(item, 80);
      if (clean) set.add(clean);
    } else if (item && typeof item === "object" && !Array.isArray(item)) {
      for (const field of [item.taskId, item.id, item.label]) {
        const clean = plainText(field, 80);
        if (clean) set.add(clean);
      }
    }
  }
  return set;
}

function taskStallState(task, key, observedAt, stalledSet, stallThresholdMs) {
  const active = !TERMINAL_STATES.has(key) && key !== "queued";
  if (!active) return { stalled: false, idleSpan: "" };
  const id = plainText(task.taskId ?? task.id, 80);
  const label = plainText(task.label, 80);
  const lastProgress =
    typeof task.lastProgressAt === "number" && Number.isFinite(task.lastProgressAt) ? task.lastProgressAt : null;
  const idleMs = lastProgress !== null ? Math.max(0, observedAt - lastProgress) : 0;
  const explicit = task.stalled === true || (id && stalledSet.has(id)) || (label && stalledSet.has(label));
  const timedIdle = stallThresholdMs > 0 && lastProgress !== null && idleMs >= stallThresholdMs;
  if (!explicit && !timedIdle) return { stalled: false, idleSpan: "" };
  const idleSpan =
    typeof task.stalledAfterMs === "number" && Number.isFinite(task.stalledAfterMs) && task.stalledAfterMs > 0
      ? elapsed(0, task.stalledAfterMs)
      : lastProgress !== null && idleMs > 0
        ? elapsed(lastProgress, observedAt)
        : "";
  return { stalled: true, idleSpan };
}

function runTaskStats(run, observedAt, stallThresholdMs) {
  const tasks = (Array.isArray(run?.tasks) ? run.tasks : []).filter(
    (task) => task && typeof task === "object" && !Array.isArray(task)
  );
  const keys = tasks.map((task) => stateKey(task.state));
  const stalledSet = runStalledSet(run);
  let doneCount = 0;
  let failedCount = 0;
  let queuedCount = 0;
  let workingCount = 0;
  let hopCount = 0;
  let worktreeCount = 0;
  let stalledCount = 0;
  const stateByRef = new Map();
  tasks.forEach((task, index) => {
    const k = keys[index];
    const tid = plainText(task.taskId ?? task.id, 80);
    const lbl = plainText(task.label, 80);
    if (tid) stateByRef.set(tid, k);
    if (lbl) stateByRef.set(lbl, k);
    if (k === "done") doneCount += 1;
    else if (PROBLEM_STATES.has(k)) failedCount += 1;
    else if (k === "queued") queuedCount += 1;
    else workingCount += 1;
  });
  const meta = tasks.map((task, index) => {
    const k = keys[index];
    const hops = taskRouteHops(task);
    const worktree = taskWorktreeBadge(task);
    const dep = taskDependencyBadge(task, k, stateByRef);
    const stall = taskStallState(task, k, observedAt, stalledSet, stallThresholdMs);
    hopCount += hops.length;
    if (worktree) worktreeCount += 1;
    if (stall.stalled) stalledCount += 1;
    return { key: k, hops, worktree, dep, stall };
  });
  return {
    tasks,
    meta,
    taskCount: tasks.length,
    doneCount,
    failedCount,
    queuedCount,
    workingCount,
    hopCount,
    worktreeCount,
    stalledCount,
  };
}

// Build a strictly metadata-only pointer record for usap-events/<pid>.jsonl.
// Never carries worker output, labels, errors, prompts, or free-form strings.
export function buildUsapEventRecord(kind, input = {}, at = Date.now()) {
  const safeKind = USAP_EVENT_KINDS.has(kind) ? kind : "complete";
  const runs = Array.isArray(input.runs)
    ? input.runs.filter((r) => r && typeof r === "object" && !Array.isArray(r))
    : input.run && typeof input.run === "object" && !Array.isArray(input.run)
      ? [input.run]
      : [];
  const collectedIds = [...(Array.isArray(input.runIds) ? input.runIds : [])];
  for (const run of runs) {
    const rid = run.runId ?? run.id;
    if (typeof rid === "string") collectedIds.push(rid);
  }
  const runIds = sanitizeRunIds(collectedIds);
  let taskCountVal = input.taskCount;
  let doneCountVal = input.doneCount;
  let failedCountVal = input.failedCount;
  if (runs.length > 0 && taskCountVal === undefined && doneCountVal === undefined && failedCountVal === undefined) {
    taskCountVal = 0;
    doneCountVal = 0;
    failedCountVal = 0;
    for (const run of runs) {
      const stats = runTaskStats(run, taskCount(at), 0);
      taskCountVal += stats.taskCount;
      doneCountVal += stats.doneCount;
      failedCountVal += stats.failedCount;
    }
  }
  return {
    kind: safeKind,
    runIds,
    taskCount: boundedCount(taskCountVal),
    doneCount: boundedCount(doneCountVal),
    failedCount: boundedCount(failedCountVal),
    at: taskCount(at),
  };
}

// Detect newly surfaced deterministic stall or route_hop events for a run without duplicates.
export function detectRunEventNotices(run, seenByRun = new Map(), now = Date.now(), options = {}) {
  if (!run || typeof run !== "object" || Array.isArray(run)) return [];
  const rawRunId = run.runId ?? run.id;
  if (typeof rawRunId !== "string" || !SAFE_RUN_ID_RE.test(rawRunId)) return [];
  const stallThresholdMs =
    typeof options.stallThresholdMs === "number" && Number.isFinite(options.stallThresholdMs) && options.stallThresholdMs >= 0
      ? options.stallThresholdMs
      : DEFAULT_STALL_THRESHOLD_MS;
  const observedAt =
    typeof run.observedAt === "number" && Number.isFinite(run.observedAt) ? run.observedAt : taskCount(now);
  const stats = runTaskStats(run, observedAt, stallThresholdMs);
  const prev = seenByRun.get(rawRunId) ?? { hops: 0, stalled: 0 };
  const notices = [];
  if (stats.hopCount > prev.hops) {
    notices.push(
      buildUsapEventRecord(
        "route_hop",
        {
          runIds: [rawRunId],
          taskCount: stats.taskCount,
          doneCount: stats.doneCount,
          failedCount: stats.failedCount,
        },
        now
      )
    );
  }
  if (stats.stalledCount > prev.stalled) {
    notices.push(
      buildUsapEventRecord(
        "stall",
        {
          runIds: [rawRunId],
          taskCount: stats.taskCount,
          doneCount: stats.doneCount,
          failedCount: stats.failedCount,
        },
        now
      )
    );
  }
  if (seenByRun.has(rawRunId)) {
    // Refresh recency (LRU) so active long-running runs are not evicted ahead of idle ones.
    seenByRun.delete(rawRunId);
  } else if (seenByRun.size >= MAX_SEEN_RUN_EVENTS) {
    const oldestKey = seenByRun.keys().next().value;
    if (oldestKey !== undefined) seenByRun.delete(oldestKey);
  }
  seenByRun.set(rawRunId, {
    hops: Math.max(prev.hops, stats.hopCount),
    stalled: stats.stalledCount,
  });
  return notices;
}

export function formatRunView(run, now = Date.now(), options = {}) {
  if (!run || typeof run !== "object" || Array.isArray(run)) return null;
  const stallThresholdMs =
    typeof options.stallThresholdMs === "number" && Number.isFinite(options.stallThresholdMs) && options.stallThresholdMs >= 0
      ? options.stallThresholdMs
      : DEFAULT_STALL_THRESHOLD_MS;
  const observedAt =
    typeof run.observedAt === "number" && Number.isFinite(run.observedAt) ? run.observedAt : taskCount(now);
  const stats = runTaskStats(run, observedAt, stallThresholdMs);
  const total = stats.taskCount;
  const parts = [`${stats.doneCount} done`];
  if (stats.failedCount) parts.push(`${stats.failedCount} failed`);
  if (stats.workingCount || stats.queuedCount) parts.push(`${stats.workingCount} working`, `${stats.queuedCount} queued`);
  if (stats.stalledCount) parts.push(`${stats.stalledCount} stalled`);
  if (stats.hopCount) parts.push(`${stats.hopCount} ${stats.hopCount === 1 ? "route hop" : "route hops"}`);
  if (stats.worktreeCount) parts.push(`${stats.worktreeCount} ${stats.worktreeCount === 1 ? "worktree" : "worktrees"}`);
  const runId = plainText(run.runId ?? run.id);
  const route =
    run.selection && typeof run.selection === "object" && !Array.isArray(run.selection)
      ? plainText(run.selection.route)
      : "";
  const routeSuffix = route ? ` · ${route}` : "";
  const rows = [
    `${stateIcon(stateKey(run.state))} Subagents · ${parts.join(" · ")} · ${total - stats.workingCount - stats.queuedCount}/${total}${runId ? ` · ${runId}` : ""}`,
  ];
  stats.tasks.slice(0, MAX_RUN_ROWS).forEach((task, index) => {
    const info = stats.meta[index];
    const ended = typeof task.endedAt === "number" && Number.isFinite(task.endedAt) ? task.endedAt : observedAt;
    const span = elapsed(task.startedAt, ended);
    const label = plainText(task.label) || plainText(task.taskId) || plainText(task.task) || "Unnamed task";
    const extras = [];
    if (info.dep) extras.push(info.dep);
    if (info.hops.length > 0) extras.push(`hop ${info.hops.slice(-2).join(", ")}`);
    else if (routeSuffix) extras.push(route);
    if (info.worktree) extras.push(info.worktree);
    if (info.stall.stalled) extras.push(info.stall.idleSpan ? `STALLED (${info.stall.idleSpan} idle)` : "STALLED");
    const extraSuffix = extras.length > 0 ? ` · ${extras.join(" · ")}` : "";
    rows.push(`${stateIcon(info.key)} ${label}  ${info.key.replace(/_/g, " ")}${span ? ` · ${span}` : ""}${extraSuffix}`);
  });
  if (stats.tasks.length > MAX_RUN_ROWS) {
    rows.push(`… +${stats.tasks.length - MAX_RUN_ROWS} more · hub status for details`);
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

// `${parentPid}.json.${pid}.tmp` (legacy) and `${parentPid}.json.${pid}.${time}.${rand}.tmp`.
const BY_PID_TMP_RE = /^(\d+)\.json\.[\w.-]+\.tmp$/;
// `${sessionId}.json.${pid}.tmp` / `${sessionId}.json.${pid}.${time}.${rand}.tmp` sharing the
// `usap/` directory with the session map files.
const USAP_TMP_RE = /^[\w-]+\.json\.[\w.-]+\.tmp$/;
const EVENT_FILE_RE = /^(\d+)\.(?:jsonl|cursor\.json|cursor\.lock)$/;
const TEMP_FILE_MAX_AGE_MS = 60 * 60 * 1000;

export async function sweepStaleBridgeFiles(baseDir, nowMs = Date.now(), maxAgeMs = 24 * 60 * 60 * 1000) {
  const dirs = [
    { dir: path.join(baseDir, "usap"), mode: "usap" },
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
      let pid = null;
      if (mode === "usap") {
        if (!USAP_TMP_RE.test(name)) continue;
      } else if (mode === "by-pid") {
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
      }
      const pidDead = pid !== null && !pidIsAlive(pid);
      const full = path.join(dir, name);
      try {
        const st = await fs.lstat(full);
        if (st.isSymbolicLink() || !st.isFile()) continue;
        const stale = nowMs - st.mtimeMs >= (mode === "events" ? maxAgeMs : TEMP_FILE_MAX_AGE_MS);
        let drop;
        if (mode === "events") drop = pidDead && stale;
        else if (mode === "usap") drop = stale;
        else drop = pidDead || (stale && name.endsWith(".tmp"));
        if (!drop) continue;
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
    const mapTmp = tempSiblingPath(mapFile);
    await fs.writeFile(mapTmp, JSON.stringify({ sessionId, sessionFile: file, createdAt: Date.now() }), {
      flag: "wx",
      mode: 0o600,
    });
    await fs.rename(mapTmp, mapFile);
  }

  const parentPid = process.env.STEAK_ANTIGRAVITY_PARENT_PID ?? "";
  let eventsPath = null;
  if (file && PID_RE.test(parentPid)) {
    const byPid = path.join(dataDir, "usap", "by-pid");
    await fs.mkdir(byPid, { recursive: true, mode: 0o700 });
    const target = path.join(byPid, `${parentPid}.json`);
    const temporary = tempSiblingPath(target);
    await fs.writeFile(
      temporary,
      JSON.stringify({ parentPid: Number(parentPid), bridgePid: process.pid, sessionId, sessionFile: file, createdAt: Date.now() }),
      { flag: "wx", mode: 0o600 }
    );
    await fs.rename(temporary, target);

    eventsPath = path.join(dataDir, "usap-events", `${parentPid}.jsonl`);
    await fs.mkdir(path.dirname(eventsPath), { recursive: true, mode: 0o700 });
    session.subscribe((event) => {
      const message = event?.message;
      if (event?.type !== "message_end" || message?.customType !== "ultraterm-subagents-complete") return;
      const details = message.details && typeof message.details === "object" ? message.details : {};
      const record = buildUsapEventRecord(
        "complete",
        {
          runIds: details.runIds,
          run: details.run,
          runs: details.runs,
          taskCount: details.taskCount,
          doneCount: details.doneCount,
          failedCount: details.failedCount,
        },
        Date.now()
      );
      void fs
        .appendFile(eventsPath, `${JSON.stringify(record)}\n`, {
          mode: 0o600,
        })
        .catch((error) => log(`could not append USAP completion notice: ${error?.message ?? error}`));
    });
  }

  const tools = TOOL_NAMES.map((name) => ({ name, definition: session.getToolDefinition(name) }));
  const missing = tools.filter((tool) => !tool.definition).map((tool) => tool.name);
  if (missing.length) throw new Error(`Steak Pi did not register ${missing.join(", ")}`);
  return { session, tools, hostSession: file, eventsPath };
}

let ready;
const inflight = new Map();
const seenRunEvents = new Map();

function appendUsapEvents(eventsPath, records) {
  if (!eventsPath || !Array.isArray(records) || records.length === 0) return;
  const payload = `${records.map((record) => JSON.stringify(record)).join("\n")}\n`;
  void fs
    .appendFile(eventsPath, payload, { mode: 0o600 })
    .catch((error) => log(`could not append USAP event pointer: ${error?.message ?? error}`));
}

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
  const method = message?.method;
  const params = message?.params;
  const id = message?.id;
  if (method === "initialize") {
    return reply(id, {
      protocolVersion: params?.protocolVersion ?? "2025-06-18",
      capabilities: { tools: {} },
      serverInfo: { name: "steak-antigravity-usap", version: PLUGIN_VERSION },
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
    const controller = new AbortController();
    // Register before awaiting `ready`, otherwise a notifications/cancelled that lands
    // while the host session is still starting has nothing to abort and is lost.
    if (id !== undefined && id !== null) inflight.set(id, controller);
    const guard = createReplyGuard();
    try {
      let state;
      try {
        state = await ready;
      } catch (error) {
        if (guard.canReply(controller.signal)) {
          reply(id, {
            isError: true,
            content: [{ type: "text", text: `USAP is unavailable from Antigravity: ${error?.message ?? error}. Work directly and say so.` }],
          });
        }
        return;
      }
      if (!guard.canReply(controller.signal)) return;
      const tool = state.tools.find((entry) => entry.name === params?.name);
      if (!tool) return fail(id, -32602, `unknown tool ${params?.name}`);
      const context = state.session.extensionRunner.createContext();
      const { args } =
        tool.name === "ultraterm_subagents"
          ? await withOwnerDefault(params?.arguments ?? {})
          : { args: params?.arguments ?? {} };
      const token = params?._meta?.progressToken;
      let step = 0;
      let last = 0;
      const onUpdate = (update) => {
        if (state.eventsPath && update?.details?.run && typeof update.details.run === "object") {
          appendUsapEvents(state.eventsPath, detectRunEventNotices(update.details.run, seenRunEvents));
        }
        if (token === undefined) return;
        // Progress that arrives after the final reply (or after a cancel) is dropped.
        if (!guard.canUpdate(controller.signal)) return;
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
      // A cancelled request gets no result at all (MCP spec).
      if (!guard.canReply(controller.signal)) return;
      const content = textParts(result);
      let presented = content;
      const run = result?.details?.run;
      if (run && typeof run === "object" && !Array.isArray(run) && (run.runId || run.id || Array.isArray(run.tasks))) {
        if (state.eventsPath) {
          appendUsapEvents(state.eventsPath, detectRunEventNotices(run, seenRunEvents));
        }
        const view = formatRunView(run);
        if (view) presented = withRunView(view, content);
      }
      return reply(id, {
        content: presented,
        isError: false,
      });
    } catch (error) {
      if (guard.canReply(controller.signal)) {
        reply(id, { isError: true, content: [{ type: "text", text: String(error?.message ?? error) }] });
      }
      return;
    } finally {
      guard.finish();
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
    const parsed = parseMessage(line);
    if (parsed.kind === "empty") return;
    if (parsed.kind === "parse-error") return fail(null, PARSE_ERROR, "parse error");
    // `null`, numbers, strings and arrays are not JSON-RPC requests.
    if (parsed.kind !== "request") return fail(null, INVALID_REQUEST, "invalid request");
    const message = parsed.message;
    void handle(message).catch((error) => {
      const id = message?.id;
      if (id !== undefined && id !== null) fail(id, INTERNAL_ERROR, String(error?.message ?? error));
    });
  });
  const shutdown = () => {
    for (const controller of inflight.values()) controller.abort();
    inflight.clear();
    // Never let a hung start() keep the bridge alive after stdin closes.
    const force = setTimeout(() => process.exit(0), SHUTDOWN_READY_TIMEOUT_MS + 500);
    void settleWithin(ready, SHUTDOWN_READY_TIMEOUT_MS).then((state) => {
      try {
        state?.session?.dispose();
      } catch {
        // Bridge never finished starting.
      } finally {
        clearTimeout(force);
        process.exit(0);
      }
    });
  };
  input.on("close", shutdown);
  process.on("SIGTERM", shutdown);
}
