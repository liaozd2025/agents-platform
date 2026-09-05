import { createHash, randomBytes } from "node:crypto";
import { constants } from "node:fs";
import {
  mkdir,
  open,
  readFile,
  readdir,
  stat,
  unlink,
  writeFile,
} from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "typebox";

import {
  createAgentSession,
  DefaultResourceLoader,
  ModelRuntime,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";
import {
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
} from "@earendil-works/pi-ai";

const PROTOCOL = "yuxi.pi-jsonl.v1";
const OUTPUTS_ROOT = resolve(process.cwd(), "outputs");
const SKILLS_ROOTS = ["/home/gem/skills", "/home/gem/user-data/agents/skills"];
const SKILL_DIR = "/home/gem/skills/pi-golden";
const SKILL_VERSION = "1.0.0";
const GOLDEN = "YUXI_PI_GOLDEN_V1";
const PATCH =
  "--- /dev/null\n+++ b/pi-golden.txt\n@@ -0,0 +1 @@\n+YUXI_PI_GOLDEN_V1\n\\ No newline at end of file\n";
const MAX_OUTPUT_FILES = 200;
const MAX_OUTPUT_FILE_BYTES = 64 * 1024 * 1024;
const MAX_OUTPUT_BYTES = 256 * 1024 * 1024;
const MAX_PATCH_FILE_BYTES = 256 * 1024;
const MAX_PATCH_BYTES = 16 * 1024 * 1024;
const MAX_EVENT_BYTES = 16 * 1024;
const MAX_TRUNCATION_CONTINUATIONS = 3;
const TRUNCATED_STOP_REASONS = new Set([
  "length",
  "max_tokens",
  "max_output_tokens",
]);
const CONTINUE_TRUNCATED_RESPONSE =
  "Continue exactly where the previous response stopped. Do not repeat completed content. " +
  "Finish the active task, including any required tool calls and deliverables.";
const runnerPath = fileURLToPath(import.meta.url);

const sha256 = (value) => createHash("sha256").update(value).digest("hex");

/** 构造 PI 任务提示，并让本次交付目录覆盖任务中的冲突路径。 */
function buildTaskPrompt(task, projectRoot, outputRoot) {
  // ponytail: 先用提示优先级保留通用 workspace 写入；模型再次越界时再增加 write-set gate。
  return (
    `Complete the user task in the current sandbox. The Project workspace is ${projectRoot}.` +
    `\n\nUser task:\n${task}\n\nExecution requirement: ` +
    `Put every generated deliverable under ${outputRoot}. ` +
    "This assigned directory overrides any conflicting output path in the user task. " +
    "Do not put generated deliverables elsewhere. " +
    "After creating a deliverable, call submit_artifact with its path relative to this assigned directory. " +
    "Only explicitly submitted files are delivered; do not submit dependencies, caches or temporary files. " +
    "Submit again after editing a submitted file. Tasks without deliverable files need not submit anything. " +
    "Limits: 200 files, 64 MiB per file, 256 MiB total. Project edits remain in the Project workspace; " +
    "the delivery patch describes added deliverable files only, not the Project source diff."
  );
}

async function directoryDigest(root) {
  const entries = [];
  async function walk(path) {
    for (const entry of await readdir(path, { withFileTypes: true })) {
      const child = resolve(path, entry.name);
      entries.push({
        path: child,
        type: entry.isDirectory()
          ? "directory"
          : entry.isFile()
            ? "file"
            : "other",
      });
      if (entry.isDirectory()) await walk(child);
    }
  }
  await walk(root);
  const hash = createHash("sha256");
  for (const entry of entries.sort((left, right) =>
    Buffer.compare(Buffer.from(left.path), Buffer.from(right.path)),
  )) {
    hash.update(relative(root, entry.path).replaceAll("\\", "/"));
    hash.update("\0");
    hash.update(`${entry.type}\0`);
    if (entry.type !== "file") continue;
    hash.update(Buffer.from([(await stat(entry.path)).mode & 0o111]));
    hash.update(await readFile(entry.path));
    hash.update("\0");
  }
  return hash.digest("hex");
}

async function inspectRuntime() {
  const packageRoot = dirname(runnerPath);
  const lock = JSON.parse(
    await readFile(resolve(packageRoot, "package-lock.json"), "utf8"),
  );
  const pi = JSON.parse(
    await readFile(
      resolve(
        packageRoot,
        "node_modules/@earendil-works/pi-coding-agent/package.json",
      ),
      "utf8",
    ),
  );
  const skills = {};
  for (const root of SKILLS_ROOTS) {
    let entries;
    try {
      entries = await readdir(root, { withFileTypes: true });
    } catch (error) {
      if (error?.code === "ENOENT") continue;
      throw error;
    }
    for (const entry of entries.filter((item) => item.isDirectory())) {
      const path = resolve(root, entry.name);
      skills[path] = await directoryDigest(path);
    }
  }
  return {
    runner_protocol: PROTOCOL,
    runner_digest: sha256(await readFile(runnerPath)),
    pi_version: pi.version,
    pi_integrity:
      lock.packages["node_modules/@earendil-works/pi-coding-agent"].integrity,
    node_version: process.version.slice(1),
    skills,
  };
}

function newFilePatch(relativePath, content) {
  if (content.includes(0)) return `Binary file b/${relativePath} added\n`;
  if (content.length === 0) {
    return `diff --git a/${relativePath} b/${relativePath}\nnew file mode 100644\nindex 0000000..e69de29\n`;
  }
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(content);
  } catch {
    return `Binary file b/${relativePath} added\n`;
  }
  const endsWithNewline = text.endsWith("\n");
  const lines = endsWithNewline
    ? text.slice(0, -1).split("\n")
    : text.split("\n");
  const noNewlineMarker = endsWithNewline
    ? ""
    : "\\ No newline at end of file\n";
  return `--- /dev/null\n+++ b/${relativePath}\n@@ -0,0 +1,${lines.length} @@\n${lines.map((line) => `+${line}`).join("\n")}\n${noNewlineMarker}`;
}

/** Linux 沙箱逐级固定目录 fd，避免中间目录符号链接和检查后替换。 */
async function openOutputFile(root, path, flags = constants.O_RDONLY) {
  const parts = path.split("/");
  if (
    !path ||
    Buffer.byteLength(path) > 1024 ||
    /[\\\x00-\x1f\x7f]/.test(path) ||
    parts.some((part) => !part || part === "." || part === "..")
  )
    throw new Error("PI output path must be a safe relative path");
  let directory = await open("/", constants.O_RDONLY | constants.O_DIRECTORY);
  try {
    for (const part of [
      ...resolve(root).split("/").filter(Boolean),
      ...parts.slice(0, -1),
    ]) {
      const next = await open(
        `/proc/self/fd/${directory.fd}/${part}`,
        constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW,
      );
      await directory.close();
      directory = next;
    }
    return await open(
      `/proc/self/fd/${directory.fd}/${parts.at(-1)}`,
      flags | constants.O_NOFOLLOW | constants.O_NONBLOCK,
      0o600,
    );
  } finally {
    await directory.close();
  }
}

/** 流式摘要仅保留小文本补丁所需字节，大文件不整块加载。 */
async function inspectOutputFile(root, path) {
  const handle = await openOutputFile(root, path);
  try {
    const before = await handle.stat();
    if (!before.isFile()) throw new Error("PI output must be a regular file");
    if (before.size > MAX_OUTPUT_FILE_BYTES)
      throw new Error("PI output file exceeds 64 MiB size limit");
    const hash = createHash("sha256");
    const buffer = Buffer.alloc(64 * 1024);
    const smallContent = [];
    let size = 0;
    while (true) {
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, null);
      if (!bytesRead) break;
      size += bytesRead;
      if (size > MAX_OUTPUT_FILE_BYTES)
        throw new Error("PI output file exceeds 64 MiB size limit");
      hash.update(buffer.subarray(0, bytesRead));
      if (before.size <= MAX_PATCH_FILE_BYTES && size <= MAX_PATCH_FILE_BYTES)
        smallContent.push(Buffer.from(buffer.subarray(0, bytesRead)));
    }
    const after = await handle.stat();
    if (
      size !== before.size ||
      after.size !== before.size ||
      after.mtimeMs !== before.mtimeMs ||
      after.ctimeMs !== before.ctimeMs
    ) {
      throw new Error("PI output changed while collecting results");
    }
    return {
      path,
      sha256: hash.digest("hex"),
      size,
      content:
        size <= MAX_PATCH_FILE_BYTES ? Buffer.concat(smallContent) : null,
    };
  } finally {
    await handle.close();
  }
}

/** 登记当前 attempt 的明确交付物，历史会话工具消息不重放登记。 */
function createArtifactTool(root, artifacts) {
  return {
    name: "submit_artifact",
    label: "交付文件",
    description:
      "登记当前交付目录内的普通文件。path 是相对路径；修改后须重新登记。",
    parameters: Type.Object({ path: Type.String() }),
    executionMode: "sequential",
    async execute(_id, { path }) {
      if (
        [
          ".pi-agent",
          "pi-session",
          ".pi-artifacts.json",
          ".pi-output.patch",
        ].includes(path.split("/")[0])
      )
        throw new Error("PI internal files cannot be submitted");
      if (!artifacts.has(path) && artifacts.size >= MAX_OUTPUT_FILES)
        throw new Error("PI output exceeds 200 submitted files");
      const { content: _content, ...item } = await inspectOutputFile(
        root,
        path,
      );
      const total =
        [...artifacts.values()].reduce((sum, file) => sum + file.size, 0) -
        (artifacts.get(path)?.size || 0) +
        item.size;
      if (total > MAX_OUTPUT_BYTES)
        throw new Error("PI output exceeds 256 MiB total size limit");
      artifacts.set(path, item);
      return {
        content: [
          { type: "text", text: `已登记 ${path} (${item.size} bytes)` },
        ],
        details: item,
      };
    },
  };
}

async function createOutputRefs(root, artifacts) {
  const items = [];
  const patches = [];
  let outputBytes = 0;
  let patchBytes = 0;
  for (const [path, submitted] of [...artifacts].sort(([a], [b]) =>
    a.localeCompare(b),
  )) {
    const { content, ...item } = await inspectOutputFile(root, path);
    if (item.sha256 !== submitted.sha256 || item.size !== submitted.size)
      throw new Error(`PI submitted artifact changed; submit again: ${path}`);
    outputBytes += item.size;
    if (outputBytes > MAX_OUTPUT_BYTES)
      throw new Error("PI output exceeds total size limit");
    items.push(item);
    const summary = `Deliverable b/${path} added (${item.size} bytes, sha256 ${item.sha256}); content omitted from delivery patch\n`;
    let filePatch = content === null ? summary : newFilePatch(path, content);
    if (patchBytes + Buffer.byteLength(filePatch) + 1 > MAX_PATCH_BYTES)
      filePatch = summary;
    patchBytes += Buffer.byteLength(filePatch) + 1;
    if (patchBytes > MAX_PATCH_BYTES)
      throw new Error("PI output patch exceeds size limit");
    patches.push(filePatch);
  }
  const artifact = `${JSON.stringify({ files: items }, null, 2)}\n`;
  const patch = patches.join("\n");
  for (const [path, content] of [
    [".pi-artifacts.json", artifact],
    [".pi-output.patch", patch],
  ]) {
    const handle = await openOutputFile(
      root,
      path,
      constants.O_WRONLY | constants.O_CREAT,
    );
    try {
      if (!(await handle.stat()).isFile())
        throw new Error("PI metadata must be a regular file");
      await handle.truncate(0);
      await handle.writeFile(content, "utf8");
    } finally {
      await handle.close();
    }
  }
  return {
    artifact: {
      path: ".pi-artifacts.json",
      sha256: sha256(artifact),
      files: items,
    },
    patch: { path: ".pi-output.patch", sha256: sha256(patch) },
  };
}

async function createResources(root, manifest, outputRoot) {
  const skillPaths = (manifest?.skill_bundle?.items || []).map(
    (item) => item.path,
  );
  const settingsManager = SettingsManager.inMemory(
    {
      retry: { enabled: false },
      images: { blockImages: !manifest?.model?.input?.includes("image") },
    },
    { projectTrusted: false },
  );
  const resourceLoader = new DefaultResourceLoader({
    cwd: root,
    agentDir: resolve(outputRoot, ".pi-agent"),
    settingsManager,
    additionalSkillPaths: skillPaths,
    // SDK 的 noSkills 保留 additionalSkillPaths；空 prompt 显式关闭 SYSTEM 文件发现。
    noSkills: true,
    systemPrompt: "",
    appendSystemPrompt: [],
    noExtensions: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    agentsFilesOverride: () => ({
      agentsFiles: (manifest?.context?.project_instructions || []).map(
        (item) => {
          if (
            item.path !== "AGENTS.md" ||
            typeof item.content !== "string" ||
            Buffer.byteLength(item.content) > 64 * 1024 ||
            sha256(item.content) !== item.sha256
          )
            throw new Error("PI Project instructions snapshot is invalid");
          return { path: resolve(root, item.path), content: item.content };
        },
      ),
    }),
  });
  await resourceLoader.reload();
  const skills = resourceLoader.getSkills();
  if (
    skills.diagnostics.some((item) => item.type === "error") ||
    skills.skills.length !== skillPaths.length
  ) {
    throw new Error("locked PI Skills were not loaded");
  }
  return { settingsManager, resourceLoader };
}

function emit(type, sequence, value, job) {
  const key = ["artifact", "patch", "session"].includes(type)
    ? "ref"
    : "payload";
  process.stdout.write(
    `${JSON.stringify({ event_id: `${job.attempt_id}:${sequence}:${type}`, sequence, type, [key]: value })}\n`,
  );
}

function boundedEventValue(value) {
  const serialized = JSON.stringify(value ?? null);
  const bytes = Buffer.from(serialized);
  if (bytes.length <= MAX_EVENT_BYTES) return value ?? null;
  const bounded = {
    truncated: true,
    preview: bytes.subarray(0, MAX_EVENT_BYTES / 2).toString("utf8"),
  };
  for (const key of ["path", "file_path"]) {
    const identifier = value?.[key];
    if (typeof identifier === "string" && Buffer.byteLength(identifier) <= 1024)
      bounded[key] = identifier;
  }
  return bounded;
}

function subscribeToolEvents(session, emitEvent) {
  return session.subscribe((event) => {
    if (event.type === "tool_execution_start") {
      emitEvent("tool_call", {
        tool_call_id: event.toolCallId,
        name: event.toolName,
        args: boundedEventValue(event.args),
      });
    } else if (event.type === "tool_execution_end") {
      emitEvent("tool_result", {
        tool_call_id: event.toolCallId,
        name: event.toolName,
        content: boundedEventValue(event.result),
        is_error: Boolean(event.isError),
      });
    }
  });
}

function assistantText(message) {
  return (message?.content || [])
    .filter((item) => item.type === "text" && typeof item.text === "string")
    .map((item) => item.text)
    .join("");
}

async function promptUntilComplete(session, prompt) {
  const parts = [];
  let lastAssistant;
  const unsubscribe = session.subscribe((event) => {
    if (event.type === "message_end" && event.message?.role === "assistant")
      lastAssistant = event.message;
  });
  try {
    for (let continuation = 0; ; continuation++) {
      lastAssistant = undefined;
      const continuationPrompt =
        continuation === 0
          ? prompt
          : `${CONTINUE_TRUNCATED_RESPONSE}\n\nPartial response so far:\n${parts.join("")}`;
      await session.prompt(continuationPrompt);
      if (!lastAssistant)
        throw new Error("PI completed without a final assistant message");
      parts.push(assistantText(lastAssistant));
      if (!TRUNCATED_STOP_REASONS.has(lastAssistant.stopReason))
        return parts.join("").trim();
      if (continuation === MAX_TRUNCATION_CONTINUATIONS)
        throw new Error("PI response repeatedly reached the model output limit");
    }
  } finally {
    unsubscribe();
  }
}

async function runGolden(job, outputRoot) {
  const sessionDir = resolve(outputRoot, "pi-session");
  const outputPath = resolve(outputRoot, "pi-golden.txt");
  const legacyOutputPath = resolve(OUTPUTS_ROOT, "pi-golden.txt");
  const patchPath = resolve(outputRoot, "pi-golden.patch");
  await mkdir(sessionDir, { recursive: true });
  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage(
      fauxToolCall(
        "read",
        { path: resolve(SKILL_DIR, "SKILL.md") },
        { id: "read-skill" },
      ),
      {
        stopReason: "toolUse",
      },
    ),
    (context) => {
      const prompt = context.messages
        .find((message) => message.role === "user")
        ?.content?.find((item) => item.type === "text")?.text;
      const hasConflictingOutputPath =
        typeof job.task === "string" && job.task.includes(legacyOutputPath);
      const legacyOutputIndex = prompt?.lastIndexOf(legacyOutputPath) ?? -1;
      const assignedOutputIndex = prompt?.lastIndexOf(outputRoot) ?? -1;
      const assignedDirectoryWins =
        legacyOutputIndex >= 0 && assignedOutputIndex > legacyOutputIndex;
      const targetPath =
        !hasConflictingOutputPath || assignedDirectoryWins
          ? outputPath
          : legacyOutputPath;
      return fauxAssistantMessage(
        fauxToolCall(
          "write",
          { path: targetPath, content: GOLDEN },
          { id: "write-golden" },
        ),
        { stopReason: "toolUse" },
      );
    },
    fauxAssistantMessage("YUXI_PI_", { stopReason: "length" }),
    fauxAssistantMessage("GOLDEN_V1"),
  ]);
  const modelRuntime = await ModelRuntime.create({
    modelsPath: null,
    refreshOnCreate: false,
  });
  modelRuntime.registerNativeProvider(faux.provider);
  const { settingsManager, resourceLoader } = await createResources(
    outputRoot,
    job.manifest,
    outputRoot,
  );
  const { session } = await createAgentSession({
    cwd: outputRoot,
    modelRuntime,
    model: faux.getModel(),
    tools: ["read", "write"],
    resourceLoader,
    settingsManager,
    sessionManager: SessionManager.create(outputRoot, sessionDir),
  });
  let sequence = 0;
  const emitEvent = (type, value) => emit(type, sequence++, value, job);
  const unsubscribe = subscribeToolEvents(session, emitEvent);
  try {
    emitEvent("log", { message: "pi_started" });
    const task =
      typeof job.task === "string" && job.task.trim()
        ? job.task
        : "Use the pi-golden Skill and complete its task exactly.";
    const text = await promptUntilComplete(
      session,
      buildTaskPrompt(task, process.cwd(), outputRoot),
    );
    const artifact = await readFile(outputPath);
    if (
      artifact.toString("utf8") !== GOLDEN ||
      text !== GOLDEN ||
      faux.state.callCount !== 4
    ) {
      throw new Error("PI golden task did not complete exactly once");
    }
    const sessionPath = session.sessionFile;
    if (!sessionPath || !(await stat(sessionPath)).isFile())
      throw new Error("PI session was not persisted");
    await writeFile(patchPath, PATCH, "utf8");
    const artifactRef = { path: "pi-golden.txt", sha256: sha256(artifact) };
    const patchRef = { path: "pi-golden.patch", sha256: sha256(PATCH) };
    const sessionRef = {
      path: relative(outputRoot, sessionPath).replaceAll("\\", "/"),
      sha256: sha256(await readFile(sessionPath)),
    };
    emitEvent("artifact", artifactRef);
    emitEvent("patch", patchRef);
    emitEvent("session", sessionRef);
    emitEvent(
      "final",
      {
        text,
        output_subdir: job.output_subdir,
        artifact: artifactRef,
        patch: patchRef,
        session: sessionRef,
      },
    );
  } finally {
    unsubscribe();
    session.dispose();
  }
}

/** 只从已验证的字节副本 fork，原 session 路径永不打开写入。 */
async function createTaskSessionManager(job, projectRoot, sessionDir) {
  const source = job.manifest?.context?.session_source;
  const previous = job.previous_session;
  if (!source && !previous)
    return SessionManager.create(projectRoot, sessionDir);
  if (
    !source ||
    typeof previous?.content !== "string" ||
    Buffer.byteLength(previous.content) > 16 * 1024 * 1024 ||
    previous.sha256 !== source.ref?.sha256 ||
    sha256(previous.content) !== previous.sha256
  )
    throw new Error("PI previous session snapshot is invalid");
  const path = `/home/gem/yuxi-session-${randomBytes(12).toString("hex")}.jsonl`;
  const file = await open(
    path,
    constants.O_RDWR |
      constants.O_CREAT |
      constants.O_EXCL |
      constants.O_NOFOLLOW,
    0o600,
  );
  try {
    // 先 unlink 再写入；fork 的同步读取只依赖当前进程 fd，不暴露可替换的来源路径。
    await unlink(path);
    await file.writeFile(previous.content, "utf8");
    return SessionManager.forkFrom(
      `/proc/self/fd/${file.fd}`,
      projectRoot,
      sessionDir,
    );
  } finally {
    await file.close();
  }
}

/** 全 session 差额归属当前 Run；SDK 全零缺省 usage 保守标为未知。 */
function taskTokenUsage(session, manager, before, firstEntry, model) {
  const after = session.getSessionStats().tokens;
  const delta = Object.fromEntries(
    Object.keys(before).map((key) => [key, after[key] - before[key]]),
  );
  if (
    Object.values(delta).some(
      (value) => !Number.isSafeInteger(value) || value < 0,
    )
  )
    throw new Error("PI token usage delta is invalid");
  const usages = manager
    .getEntries()
    .slice(firstEntry)
    .flatMap((entry) => {
      if (entry.type === "message" && entry.message?.role === "assistant")
        return [entry.message.usage];
      if (["compaction", "branch_summary"].includes(entry.type))
        return [entry.usage];
      return [];
    });
  const reported = usages.filter((usage) =>
    ["input", "output", "cacheRead", "cacheWrite"].some(
      (key) => Number.isFinite(usage?.[key]) && usage[key] > 0,
    ),
  ).length;
  const total = {
    input_tokens: delta.input + delta.cacheRead + delta.cacheWrite,
    output_tokens: delta.output,
    total_tokens: delta.total,
  };
  const spec = model.spec || `yuxi:${model.model_id}`;
  return {
    schema_version: 2,
    model_call_count: usages.length,
    usage_reported_call_count: reported,
    usage_unavailable_call_count: usages.length - reported,
    complete: usages.length > 0 && reported === usages.length,
    models: {
      [spec]: {
        model: {
          model_id: model.model_id,
          provider_id: spec.split(":")[0],
          identity_source: "configured_spec",
        },
        model_call_count: usages.length,
        usage_reported_call_count: reported,
        usage: reported
          ? {
              ...total,
              input_token_details: {
                cache_read: delta.cacheRead,
                cache_creation: delta.cacheWrite,
              },
            }
          : {},
      },
    },
    total,
  };
}

async function runTask(job, outputRoot) {
  const model = job.manifest?.model;
  if (!model || typeof job.task !== "string" || !job.task.trim())
    throw new Error("PI task or model is missing");
  if (
    !Object.values({
      anthropic: "anthropic-messages",
      gemini: "google-generative-ai",
      openai: "openai-completions",
      openrouter: "openai-completions",
    }).includes(model.api)
  ) {
    throw new Error(`unsupported PI model api: ${model.api}`);
  }
  const credentials = job.credentials;
  if (!credentials || typeof credentials !== "object")
    throw new Error("PI model credentials are missing");
  const apiKey =
    typeof credentials.api_key === "string" ? credentials.api_key.trim() : "";
  const headers = credentials.headers ?? {};
  if (
    !headers ||
    typeof headers !== "object" ||
    Array.isArray(headers)
  )
    throw new Error("PI model credential headers are invalid");
  const authHeaders = new Set([
    "authorization",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
  ]);
  const hasHeaderAuth = Object.entries(headers).some(
    ([name, value]) =>
      authHeaders.has(name.toLowerCase()) &&
      typeof value === "string" &&
      value.trim(),
  );
  if (!apiKey && !hasHeaderAuth)
    throw new Error("PI model credentials are missing or unsupported");
  const sessionDir = resolve(outputRoot, "pi-session");
  await mkdir(sessionDir, { recursive: true });
  const modelRuntime = await ModelRuntime.create({
    modelsPath: null,
    refreshOnCreate: false,
  });
  modelRuntime.registerProvider("yuxi", {
    name: "Yuxi",
    baseUrl: model.base_url,
    ...(apiKey ? { apiKey } : {}),
    authHeader: Boolean(apiKey),
    headers,
    api: model.api,
    models: [
      {
        id: model.model_id,
        name: model.display_name || model.model_id,
        api: model.api,
        reasoning: model.reasoning === true,
        input: model.input || ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: model.context_window,
        maxTokens: model.max_tokens,
        samplingParams: model.sampling_params || {},
      },
    ],
  });
  const piModel = modelRuntime.getModel("yuxi", model.model_id);
  if (!piModel) throw new Error("PI model registration failed");
  const projectRoot = process.cwd();
  const artifacts = new Map();
  const { settingsManager, resourceLoader } = await createResources(
    projectRoot,
    job.manifest,
    outputRoot,
  );
  const sessionManager = await createTaskSessionManager(
    job,
    projectRoot,
    sessionDir,
  );
  const { session } = await createAgentSession({
    cwd: projectRoot,
    modelRuntime,
    model: piModel,
    tools: job.manifest.policy.tools,
    customTools: [createArtifactTool(outputRoot, artifacts)],
    resourceLoader,
    settingsManager,
    sessionManager,
  });
  const beforeTokens = session.getSessionStats().tokens;
  const firstEntry = sessionManager.getEntries().length;
  let sequence = 0;
  const emitEvent = (type, value) => emit(type, sequence++, value, job);
  const unsubscribe = subscribeToolEvents(session, emitEvent);
  try {
    emitEvent("log", { message: "pi_started", model: model.model_id });
    const text = await promptUntilComplete(
      session,
      buildTaskPrompt(job.task, process.cwd(), outputRoot),
    );
    if (!text)
      throw new Error("PI completed without a final assistant message");
    const sessionPath = session.sessionFile;
    if (!sessionPath || !(await stat(sessionPath)).isFile())
      throw new Error("PI session was not persisted");
    const refs = await createOutputRefs(outputRoot, artifacts);
    const sessionRef = {
      path: relative(outputRoot, sessionPath).replaceAll("\\", "/"),
      sha256: sha256(await readFile(sessionPath)),
    };
    emitEvent("artifact", refs.artifact);
    emitEvent("patch", refs.patch);
    emitEvent("session", sessionRef);
    emitEvent("final", {
      text,
      output_subdir: job.output_subdir,
      artifact: refs.artifact,
      patch: refs.patch,
      session: sessionRef,
      token_usage: taskTokenUsage(
        session,
        sessionManager,
        beforeTokens,
        firstEntry,
        model,
      ),
    });
  } finally {
    unsubscribe();
    session.dispose();
  }
}

if (process.argv[2] === "--inspect") {
  process.stdout.write(`${JSON.stringify(await inspectRuntime())}\n`);
} else {
  let job;
  if (process.argv[2] === "--job") {
    const jobPath = String(process.argv[3] || "");
    if (!/^\/home\/gem\/yuxi-secret-[a-f0-9]{24}\.json$/.test(jobPath))
      throw new Error("invalid PI job path");
    const serializedJob = await readFile(jobPath, "utf8");
    await unlink(jobPath);
    job = JSON.parse(serializedJob);
  } else {
    const encoded = process.argv[2];
    if (!encoded) throw new Error("missing PI job envelope");
    job = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
    if (job.manifest?.model || job.credentials)
      throw new Error("model jobs require --job tmpfs transport");
  }
  if (!/^pi-runs\/[a-f0-9]{24}$/.test(job.output_subdir || ""))
    throw new Error("invalid PI output subdir");
  const outputRoot = resolve(OUTPUTS_ROOT, job.output_subdir);
  await mkdir(outputRoot, { recursive: true });
  await (job.manifest?.model
    ? runTask(job, outputRoot)
    : runGolden(job, outputRoot));
}
