// T-003 PI sidecar: pi-ai with ZERO tools registered.
//
// SPEC §2.14 decision 1: `@earendil-works/pi-ai` is used as a pure LLM call
// library from a Node sidecar process. Tools in pi-ai must be declared by the
// caller (TypeBox schemas) and handled by the caller; this file declares none and
// imports no execution capability. Therefore there is no execution surface, and the
// §4.7 hard gate ("PI must not touch untrusted fetched content without isolation")
// is satisfied by construction rather than by a container or micro-VM.
//
// Wire protocol (see `atlas/cognition/PROTOCOL.md`):
//   stdin  : one JSON job per line
//   stdout : lines beginning with FRAME_PREFIX carrying one JSON response each
//            every other stdout line is ignored as noise; stderr is diagnostics only
//   exit   : 0 when every job produced a result frame or a handled degrade, 2 when a
//            job was rejected as a protocol violation (loud), 1 on an internal crash
//
// The model's output is NEVER executed, parsed as code, or interpolated into a
// command. It is only ever returned as data.

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

import { createModels, createProvider, fauxAssistantMessage, fauxProvider } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";

import { extractSingleJson } from "./json-extract.mjs";

export const PROTOCOL = "atlas.cognition.sidecar/1";
export const FRAME_PREFIX = "#atlas-cognition/1#";
/**
 * 数据通道 = **stdout**，但只有带 `FRAME_PREFIX` 的行才算协议数据。
 *
 * 为什么不另开 fd 3：在**本机 WSL（Ubuntu-24.04 / drvfs）实测**，
 * 父进程用 `os.pipe()` + `pass_fds` 传来的 fd 3 在 Node 侧**不可写**——
 * `writeSync(3, ...)` 与 `createWriteStream(null, {fd: 3})` 均返回
 * `EINVAL: invalid argument, write`（同一进程写 stdout/stderr 完全正常）。
 * 见 `tools/t003_ioprobe.py` 的三种模式对照。
 *
 * 因此改用"前缀指纹"方案：数据与噪声共用 stdout，但**只有带指纹的行是数据**，
 * 其余一律忽略。这同时也是对"库往 stdout 打警告"的真实防御，
 * 并且有专门的测试把它钉死（noise 注入）。
 */
export const DATA_CHANNEL = 1;
/** Default proxy bypass so loopback test servers are never sent through the proxy. */
export const DEFAULT_NO_PROXY = "127.0.0.1,localhost,::1";

const SIDECAR_SOURCE = new URL(import.meta.url);
const SIDECAR_SOURCE_TEXT = readFileSync(SIDECAR_SOURCE, "utf8");
const SIDECAR_SHA256 =
  "sha256:" + createHash("sha256").update(SIDECAR_SOURCE_TEXT).digest("hex");

/**
 * 本模块**静态 import** 的模块名（含本仓库内的相对导入）。
 *
 * 它才是真实的能力面：`process.getBuiltinModule("child_process")` 之类的**探测**
 * 只是查询"这个内建模块存在吗"，并不引入能力；而任何在本文件顶层 import 进来的
 * 模块都必然可用。因此隔离证据用这份清单，而不是"探测结果"。
 */
function staticImports(source) {
  const names = new Set();
  const pattern = /^\s*import\s+(?:[^'"]*?\s+from\s+)?["']([^"']+)["']/gm;
  for (const match of source.matchAll(pattern)) {
    names.add(match[1]);
  }
  return [...names].sort();
}

const SIDECAR_IMPORTS = staticImports(SIDECAR_SOURCE_TEXT);

// --------------------------------------------------------------------------- //
// Minimal environment
// --------------------------------------------------------------------------- //

const BASE_ENV_KEYS = ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"];

/** 调用方在**进程启动时**用 `ATLAS_COGNITION_HOST_ENV` 声明的宿主变量白名单。
 *
 *  必须在 `applyMinimalEnv` 之前捕获：环境被重建之后，这个变量自己也不再存在。 */
const HOST_ENV_ALLOWLIST = (process.env.ATLAS_COGNITION_HOST_ENV ?? "")
  .split(",")
  .map((name) => name.trim())
  .filter(Boolean);

/**
 * Rebuild `process.env` from scratch.
 *
 * The sidecar inherits NOTHING from the host by default: `hostEnvAllowlist` names the
 * only host variables it may read (e.g. `HTTPS_PROXY`). The API key never travels
 * through the environment at all — it arrives in the job payload and lives in a
 * closure — so an injected instruction cannot exfiltrate it via `process.env`.
 */
export function applyMinimalEnv({
  hostEnvAllowlist = HOST_ENV_ALLOWLIST,
  env = {},
  proxyUrl = "",
  noProxy = "",
} = {}) {
  const dropped = Object.keys(process.env);
  const next = {
    NODE_ENV: "production",
    NODE_NO_WARNINGS: "1",
  };
  for (const key of BASE_ENV_KEYS) {
    const value = process.env[key];
    if (value !== undefined) {
      next[key] = value;
    }
  }
  if (!next.PATH) {
    next.PATH = "/usr/local/bin:/usr/bin:/bin";
  }
  for (const [key, value] of Object.entries(env)) {
    if (typeof value === "string") {
      next[key] = value;
    }
  }
  for (const key of hostEnvAllowlist) {
    const value = process.env[key];
    if (value !== undefined) {
      next[key] = value;
    }
  }
  if (proxyUrl) {
    // Global fetch only honours proxy env vars when this opt-in is set before Node
    // starts. We exec'd node with it already set; re-asserting it here keeps the
    // value correct if the caller changed the proxy URL.
    next.NODE_USE_ENV_PROXY = "1";
    next.HTTPS_PROXY = proxyUrl;
    next.HTTP_PROXY = proxyUrl;
    next.NO_PROXY = noProxy ? `${noProxy},${DEFAULT_NO_PROXY}` : DEFAULT_NO_PROXY;
    next.no_proxy = next.NO_PROXY;
  }
  for (const key of Object.keys(process.env)) {
    delete process.env[key];
  }
  Object.assign(process.env, next);
  return { applied: Object.keys(next).sort(), droppedCount: dropped.length };
}

// --------------------------------------------------------------------------- //
// Provider / model
// --------------------------------------------------------------------------- //

function buildModel(call) {
  return {
    id: call.model,
    name: call.model,
    api: "openai-completions",
    provider: call.provider,
    baseUrl: call.baseUrl,
    reasoning: call.reasoning === true,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: call.contextWindow ?? 128000,
    maxTokens: call.maxTokens ?? 4096,
  };
}

function buildModels(call) {
  const model = buildModel(call);
  const apiKey = call.apiKey ?? "";
  const provider = createProvider({
    id: call.provider,
    name: call.provider,
    baseUrl: call.baseUrl,
    auth: {
      apiKey: {
        name: `${call.provider} API key`,
        resolve: async ({ signal }) => {
          signal.throwIfAborted();
          if (!apiKey) {
            return undefined;
          }
          return { auth: { apiKey }, source: "job-payload" };
        },
      },
    },
    models: [model],
    api: openAICompletionsApi(),
  });
  // A keyless provider cannot be "configured"; report that as a config error rather
  // than letting pi-ai surface a generic message.
  const models = createModels({
    authContext: {
      async env() {
        return undefined;
      },
      async fileExists() {
        return false;
      },
    },
  });
  models.setProvider(provider);
  return { models, model };
}

function buildFauxModels(call) {
  // Deterministic transport used by the test suite: the response is scripted, so the
  // test proves what the *sidecar* did (declared zero tools, ran no child process)
  // without depending on a remote model being reachable or well-behaved.
  const faux = fauxProvider({
    api: "openai-completions",
    provider: call.provider,
    models: [{ id: call.model, name: call.model, reasoning: false, input: ["text"] }],
  });
  const models = createModels({
    authContext: {
      async env() {
        return undefined;
      },
      async fileExists() {
        return false;
      },
    },
  });
  models.setProvider(faux.provider);
  faux.setResponses([fauxAssistantMessage(call.fauxText ?? "", { stopReason: call.fauxStopReason ?? "stop" })]);
  return { models, model: faux.getModel(), faux };
}

// --------------------------------------------------------------------------- //
// Error classification
// --------------------------------------------------------------------------- //

const STATUS_PATTERN = /(?:^|\D)([1-5]\d\d)(?:\D|$)/;

/** Map a thrown provider error / errorMessage onto a degrade reason code. */
export function classifyFailure(message, stopReason) {
  const text = String(message ?? "");
  if (stopReason === "aborted" || /abort/i.test(text)) {
    return { reason: "transport_timeout", detail: text };
  }
  if (/timed out|timeout/i.test(text)) {
    return { reason: "transport_timeout", detail: text };
  }
  const match = STATUS_PATTERN.exec(text);
  if (match) {
    return { reason: "http_error", status: Number.parseInt(match[1], 10), detail: text };
  }
  if (/ECONNREFUSED|ENOTFOUND|EAI_AGAIN|ECONNRESET|ETIMEDOUT|UND_ERR|fetch failed|socket hang up/i.test(text)) {
    return { reason: "transport_error", detail: text };
  }
  return { reason: "transport_error", detail: text };
}

function textOf(message) {
  return (message.content ?? [])
    .filter((block) => block.type === "text")
    .map((block) => block.text)
    .join("");
}

function thinkingChars(message) {
  return (message.content ?? [])
    .filter((block) => block.type === "thinking")
    .reduce((total, block) => total + (block.thinking ?? "").length, 0);
}

// --------------------------------------------------------------------------- //
// Operations
// --------------------------------------------------------------------------- //

function transcriptOf(call) {
  return {
    systemPrompt: call.systemPrompt,
    userContent: call.userContent,
    toolsDeclared: 0,
  };
}

export function inspect(job) {
  const allow = job.hostEnvAllowlist ?? [];
  return {
    protocol: PROTOCOL,
    sidecarSha256: SIDECAR_SHA256,
    node: process.version,
    platform: process.platform,
    pid: process.pid,
    toolsRegistered: 0,
    capabilities: {
      // **真实能力面** = 本模块静态 import 的模块清单。pi-ai 的工具必须由调用方
      // 用 TypeBox 声明并自行处理；本文件声明 0 个，也没有导入任何执行能力面。
      static_imports: SIDECAR_IMPORTS,
      imported_module_count: SIDECAR_IMPORTS.length,
      // 这些名字**存在于 Node 内建模块表**里（因此"没导入"必须由上面的清单证明，
      // 而不是用"探测不到"来证明）。
      builtin_available: {
        child_process: process.getBuiltinModule("child_process") !== undefined,
        worker_threads: process.getBuiltinModule("worker_threads") !== undefined,
        net: process.getBuiltinModule("net") !== undefined,
        fs: process.getBuiltinModule("fs") !== undefined,
      },
      // CommonJS `require` 作用域不存在（ESM）；即便存在也没有 child_process 变量。
      require_available: typeof require === "function",
      // pi-ai 的工具声明（本边车恒为空）。
      declared_tools: [],
      declared_tool_count: 0,
      // 本进程自己记的账：从未创建子进程 / 从未走 shell。
      child_process_used: false,
      shell_used: false,
    },
    env: {
      names: Object.keys(process.env).sort(),
      hostEnvAllowlist: allow,
      // Confirm no secret-shaped names leaked in.
      leaked_secret_names: Object.keys(process.env)
        .filter((name) => /API_KEY|_TOKEN|SECRET|PASSWORD|CREDENTIAL/i.test(name))
        .sort(),
    },
    transcript: transcriptOf(job),
  };
}

export async function callModel(job) {
  const call = job.call ?? {};
  const started = Date.now();
  const controller = new AbortController();
  const timeoutMs = call.timeoutMs ?? 60000;
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let models;
  let model;
  let apiKeyMissing = false;
  if (call.transport === "faux") {
    ({ models, model } = buildFauxModels(call));
  } else {
    if (!call.apiKey) {
      apiKeyMissing = true;
    }
    ({ models, model } = buildModels(call));
  }

  const tools = []; // ZERO tools. This is the isolation claim, in code.
  const base = {
    tools,
    apiKey: call.apiKey,
    signal: controller.signal,
    maxRetries: 0,
    timeoutMs,
    temperature: call.temperature ?? 0,
    // Never ride the ambient proxy for a loopback/faux target.
    env: call.noProxy ? { no_proxy: call.noProxy } : undefined,
  };
  const options =
    call.transport === "faux"
      ? { ...base, sessionId: undefined }
      : { ...base, ...(call.maxOutputTokens ? { maxTokens: call.maxOutputTokens } : {}) };

  try {
    if (apiKeyMissing) {
      const finishedAt = Date.now();
      return {
        status: "degraded",
        reason: "config_error",
        detail: "no api key supplied in the job payload",
        model: call.model,
        provider: call.provider,
        response_model: "",
        elapsed_ms: finishedAt - started,
        text: "",
        thinking_chars: 0,
        tool_calls: 0,
        tools_declared: tools.length,
        usage: null,
        error_message: "no api key supplied in the job payload",
      };
    }

    const message = await models.completeSimple(
      model,
      {
        systemPrompt: call.systemPrompt,
        messages: [{ role: "user", content: call.userContent, timestamp: Date.now() }],
        tools,
      },
      options,
    );

    const elapsed = Date.now() - started;
    const usage = message.usage ?? null;
    const text = textOf(message);
    const errorMessage = message.errorMessage ?? "";

    if (message.stopReason === "error" || message.stopReason === "aborted") {
      const failure = classifyFailure(errorMessage || message.stopReason, message.stopReason);
      return {
        status: "degraded",
        reason: failure.reason,
        detail: failure.detail,
        http_status: failure.status ?? null,
        model: call.model,
        provider: call.provider,
        response_model: message.responseModel ?? "",
        elapsed_ms: elapsed,
        text: "",
        thinking_chars: thinkingChars(message),
        tool_calls: (message.content ?? []).filter((b) => b.type === "toolCall").length,
        tools_declared: tools.length,
        usage,
        error_message: errorMessage,
      };
    }

    if (text.trim() === "") {
      return {
        status: "degraded",
        reason: "empty_completion",
        detail: `stopReason=${message.stopReason}`,
        model: call.model,
        provider: call.provider,
        response_model: message.responseModel ?? "",
        elapsed_ms: elapsed,
        text: "",
        thinking_chars: thinkingChars(message),
        tool_calls: (message.content ?? []).filter((b) => b.type === "toolCall").length,
        tools_declared: tools.length,
        usage,
        error_message: errorMessage,
      };
    }

    return {
      status: "text",
      reason: null,
      detail: null,
      model: call.model,
      provider: call.provider,
      response_model: message.responseModel ?? "",
      elapsed_ms: elapsed,
      text,
      thinking_chars: thinkingChars(message),
      tool_calls: (message.content ?? []).filter((b) => b.type === "toolCall").length,
      tools_declared: tools.length,
      usage,
      error_message: errorMessage,
    };
  } catch (error) {
    const elapsed = Date.now() - started;
    const failure = classifyFailure(error?.message ?? String(error), null);
    return {
      status: "degraded",
      reason: failure.reason,
      detail: failure.detail,
      http_status: failure.status ?? null,
      model: call.model,
      provider: call.provider,
      response_model: "",
      elapsed_ms: elapsed,
      text: "",
      thinking_chars: 0,
      tool_calls: 0,
      tools_declared: tools.length,
      usage: null,
      error_message: String(error?.message ?? error),
    };
  } finally {
    clearTimeout(timer);
  }
}

export async function parseOutput(job) {
  const found = extractSingleJson(job.text ?? "");
  if (found === null) {
    return { ok: false, strategy: null, value: null };
  }
  return { ok: true, strategy: found.strategy, value: found.value, start: found.start, end: found.end };
}

// --------------------------------------------------------------------------- //
// Protocol loop
// --------------------------------------------------------------------------- //

function emit(frame) {
  process.stdout.write(FRAME_PREFIX + JSON.stringify(frame) + "\n");
}

async function handle(job) {
  const jobId = job?.jobId ?? "";
  const envelope = {
    protocol: PROTOCOL,
    jobId,
    sidecarSha256: SIDECAR_SHA256,
    node: process.version,
    toolsRegistered: 0,
  };
  const operation = job?.operation;
  if (operation === "inspect") {
    return { ...envelope, result: inspect(job) };
  }
  if (operation === "call") {
    const result = await callModel(job);
    return { ...envelope, result };
  }
  if (operation === "parse") {
    const result = await parseOutput(job);
    return { ...envelope, result };
  }
  const error = new Error(`unsupported operation: ${JSON.stringify(operation)}`);
  error.code = "unsupported_operation";
  throw error;
}

async function main() {
  // The environment is rebuilt before the first model call so nothing the host put
  // there (credentials included) is visible to the pi-ai stack.
  const envReport = applyMinimalEnv();

  let buffer = "";
  const jobs = [];
  for await (const chunk of process.stdin) {
    buffer += chunk;
  }
  buffer += "\n";
  for (const line of buffer.split("\n")) {
    if (line.trim() === "") {
      continue;
    }
    try {
      jobs.push(JSON.parse(line));
    } catch (error) {
      emit({
        protocol: PROTOCOL,
        jobId: "",
        sidecarSha256: SIDECAR_SHA256,
        node: process.version,
        toolsRegistered: 0,
        error: { code: "bad_job", message: `job line is not JSON: ${error.message}` },
      });
      process.exitCode = 2;
    }
  }

  for (const job of jobs) {
    if (job?.protocol !== PROTOCOL) {
      emit({
        protocol: PROTOCOL,
        jobId: job?.jobId ?? "",
        sidecarSha256: SIDECAR_SHA256,
        node: process.version,
        toolsRegistered: 0,
        error: {
          code: "protocol_mismatch",
          message: `expected ${PROTOCOL}, got ${JSON.stringify(job?.protocol)}`,
        },
      });
      process.exitCode = 2;
      continue;
    }
    reapplyEnv(job, envReport);
    if (job.noise) {
      // Test hook: emit lines that are NOT protocol frames onto the data channel, to
      // prove the reader ignores noise instead of mis-parsing it. Real Node warnings
      // (e.g. the experimental EnvHttpProxyAgent notice) land on stderr, but a library
      // that writes to stdout would land here — this is the regression guard for that.
      process.stdout.write("ExperimentalWarning: someone wrote to stdout\n");
      process.stdout.write('{"toolsRegistered": 999, "not": "a frame"}\n');
      process.stdout.write("\n");
    }
    try {
      emit(await handle(job));
    } catch (error) {
      emit({
        protocol: PROTOCOL,
        jobId: job?.jobId ?? "",
        sidecarSha256: SIDECAR_SHA256,
        node: process.version,
        toolsRegistered: 0,
        error: { code: error?.code ?? "internal_error", message: String(error?.message ?? error) },
      });
      process.exitCode = process.exitCode ?? 2;
    }
  }
}

function reapplyEnv(job, envReport) {
  const call = job.call ?? {};
  const proxyUrl = call.proxyUrl ?? "";
  if (proxyUrl || job.noise) {
    applyMinimalEnv({ proxyUrl, noProxy: call.noProxy ?? "" });
  }
  if (envReport && process.env.ATLAS_COGNITION_REPORT_ENV === "1") {
    process.stderr.write("[sidecar] env=" + JSON.stringify(envReport.applied) + "\n");
  }
}

await main();
