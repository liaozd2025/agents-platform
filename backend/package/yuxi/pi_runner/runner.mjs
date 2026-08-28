import { createHash } from "node:crypto";
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
const MAX_OUTPUT_ENTRIES = 1000;
const MAX_OUTPUT_FILES = 200;
const MAX_OUTPUT_FILE_BYTES = 8 * 1024 * 1024;
const MAX_OUTPUT_BYTES = 32 * 1024 * 1024;
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

async function collectOutputFiles(root) {
  const files = [];
  let entries = 0;
  async function walk(path) {
    for (const entry of await readdir(path, { withFileTypes: true })) {
      if (
        [
          ".pi-agent",
          "pi-session",
          ".pi-artifacts.json",
          ".pi-output.patch",
        ].includes(entry.name)
      )
        continue;
      if (++entries > MAX_OUTPUT_ENTRIES)
        throw new Error("PI output contains too many entries");
      const child = resolve(path, entry.name);
      if (entry.isDirectory()) await walk(child);
      else if (entry.isFile()) {
        if (files.length >= MAX_OUTPUT_FILES)
          throw new Error("PI output contains too many files");
        files.push(child);
      } else {
        throw new Error("PI output contains an unsupported entry");
      }
    }
  }
  await walk(root);
  return files.sort();
}

function newFilePatch(root, path, content) {
  const relativePath = relative(root, path).replaceAll("\\", "/");
  if (/[\r\n]/.test(relativePath))
    throw new Error("PI output path contains a line break");
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

async function readOutputFile(path) {
  const handle = await open(path, "r");
  try {
    const before = await handle.stat();
    if (!before.isFile() || before.size > MAX_OUTPUT_FILE_BYTES)
      throw new Error("PI output file exceeds size limit");
    const content = Buffer.alloc(before.size);
    let offset = 0;
    while (offset < content.length) {
      const { bytesRead } = await handle.read(
        content,
        offset,
        content.length - offset,
        offset,
      );
      if (!bytesRead) break;
      offset += bytesRead;
    }
    const after = await handle.stat();
    if (
      offset !== before.size ||
      after.size !== before.size ||
      after.mtimeMs !== before.mtimeMs
    ) {
      throw new Error("PI output changed while collecting results");
    }
    return content;
  } finally {
    await handle.close();
  }
}

async function createOutputRefs(root) {
  const files = await collectOutputFiles(root);
  const items = [];
  const patches = [];
  let outputBytes = 0;
  let patchBytes = 0;
  for (const path of files) {
    const content = await readOutputFile(path);
    outputBytes += content.length;
    if (outputBytes > MAX_OUTPUT_BYTES)
      throw new Error("PI output exceeds total size limit");
    items.push({
      path: relative(root, path).replaceAll("\\", "/"),
      sha256: sha256(content),
      size: content.length,
    });
    const filePatch = newFilePatch(root, path, content);
    patchBytes += Buffer.byteLength(filePatch);
    if (patchBytes > MAX_PATCH_BYTES)
      throw new Error("PI output patch exceeds size limit");
    patches.push(filePatch);
  }
  const artifactPath = resolve(root, ".pi-artifacts.json");
  const patchPath = resolve(root, ".pi-output.patch");
  const artifact = `${JSON.stringify({ files: items }, null, 2)}\n`;
  const patch = patches.join("\n");
  await writeFile(artifactPath, artifact, "utf8");
  await writeFile(patchPath, patch, "utf8");
  return {
    artifact: {
      path: ".pi-artifacts.json",
      sha256: sha256(artifact),
      files: items,
    },
    patch: { path: ".pi-output.patch", sha256: sha256(patch) },
  };
}

async function createResources(root, manifest) {
  const skillPaths = (manifest?.skill_bundle?.items || []).map(
    (item) => item.path,
  );
  const settingsManager = SettingsManager.inMemory(
    { retry: { enabled: false } },
    { projectTrusted: false },
  );
  const resourceLoader = new DefaultResourceLoader({
    cwd: root,
    agentDir: resolve(root, ".pi-agent"),
    settingsManager,
    additionalSkillPaths: skillPaths,
    noExtensions: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
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
    fauxAssistantMessage(
      fauxToolCall(
        "write",
        { path: outputPath, content: GOLDEN },
        { id: "write-golden" },
      ),
      {
        stopReason: "toolUse",
      },
    ),
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
    const text = await promptUntilComplete(
      session,
      "Use the pi-golden Skill and complete its task exactly.",
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
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: model.context_window,
        maxTokens: model.max_tokens,
        samplingParams: model.sampling_params || {},
      },
    ],
  });
  const piModel = modelRuntime.getModel("yuxi", model.model_id);
  if (!piModel) throw new Error("PI model registration failed");
  const { settingsManager, resourceLoader } = await createResources(
    outputRoot,
    job.manifest,
  );
  const { session } = await createAgentSession({
    cwd: outputRoot,
    modelRuntime,
    model: piModel,
    tools: job.manifest.policy.tools,
    resourceLoader,
    settingsManager,
    sessionManager: SessionManager.create(outputRoot, sessionDir),
  });
  let sequence = 0;
  const emitEvent = (type, value) => emit(type, sequence++, value, job);
  const unsubscribe = subscribeToolEvents(session, emitEvent);
  try {
    emitEvent("log", { message: "pi_started", model: model.model_id });
    const text = await promptUntilComplete(
      session,
      `Complete the user task in the current sandbox. The Project workspace is ${process.cwd()}. ` +
        `Put every generated deliverable under ${outputRoot}.\n\nUser task:\n${job.task}`,
    );
    if (!text)
      throw new Error("PI completed without a final assistant message");
    const sessionPath = session.sessionFile;
    if (!sessionPath || !(await stat(sessionPath)).isFile())
      throw new Error("PI session was not persisted");
    const refs = await createOutputRefs(outputRoot);
    const sessionRef = {
      path: relative(outputRoot, sessionPath).replaceAll("\\", "/"),
      sha256: sha256(await readFile(sessionPath)),
    };
    emitEvent("artifact", refs.artifact);
    emitEvent("patch", refs.patch);
    emitEvent("session", sessionRef);
    emitEvent(
      "final",
      {
        text,
        output_subdir: job.output_subdir,
        artifact: refs.artifact,
        patch: refs.patch,
        session: sessionRef,
      },
    );
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
