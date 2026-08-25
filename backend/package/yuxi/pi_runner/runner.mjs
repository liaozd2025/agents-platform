import { createHash } from "node:crypto";
import { mkdir, readFile, readdir, stat, unlink, writeFile } from "node:fs/promises";
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
const OUTPUT_ROOT = "/home/gem/user-data/outputs";
const SKILL_DIR = "/home/gem/skills/pi-golden";
const SKILL_VERSION = "1.0.0";
const GOLDEN = "YUXI_PI_GOLDEN_V1";
const PATCH = "--- /dev/null\n+++ b/pi-golden.txt\n@@ -0,0 +1 @@\n+YUXI_PI_GOLDEN_V1\n";
const runnerPath = fileURLToPath(import.meta.url);

const sha256 = (value) => createHash("sha256").update(value).digest("hex");
const canonicalJson = (value) => {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
};

async function directoryDigest(root) {
  const files = [];
  async function walk(path) {
    for (const entry of await readdir(path, { withFileTypes: true })) {
      const child = resolve(path, entry.name);
      if (entry.isDirectory()) await walk(child);
      else if (entry.isFile()) files.push(child);
    }
  }
  await walk(root);
  const hash = createHash("sha256");
  for (const path of files.sort()) {
    hash.update(relative(root, path).replaceAll("\\", "/"));
    hash.update("\0");
    hash.update(await readFile(path));
    hash.update("\0");
  }
  return hash.digest("hex");
}

async function skillMountReadOnly() {
  const probe = resolve(SKILL_DIR, ".yuxi-write-probe");
  try {
    await writeFile(probe, "probe", "utf8");
  } catch {
    return true;
  }
  await unlink(probe);
  return false;
}

async function inspectRuntime() {
  const packageRoot = dirname(runnerPath);
  const lock = JSON.parse(await readFile(resolve(packageRoot, "package-lock.json"), "utf8"));
  const pi = JSON.parse(
    await readFile(resolve(packageRoot, "node_modules/@earendil-works/pi-coding-agent/package.json"), "utf8"),
  );
  const skillItem = {
    slug: "pi-golden",
    version: SKILL_VERSION,
    path: SKILL_DIR,
    digest: await directoryDigest(SKILL_DIR),
  };
  return {
    runner_protocol: PROTOCOL,
    runner_digest: sha256(await readFile(runnerPath)),
    pi_version: pi.version,
    pi_integrity: lock.packages["node_modules/@earendil-works/pi-coding-agent"].integrity,
    node_version: process.version.slice(1),
    skills: { "pi-golden": skillItem.digest },
    skill_bundle: {
      id: "pi-golden",
      version: SKILL_VERSION,
      digest: sha256(canonicalJson([skillItem])),
      items: [skillItem],
      read_only: await skillMountReadOnly(),
    },
  };
}

function emit(type, sequence, value, job) {
  const key = ["artifact", "patch", "session"].includes(type) ? "ref" : "payload";
  process.stdout.write(
    `${JSON.stringify({ event_id: `${job.attempt_id}:${sequence}:${type}`, sequence, type, [key]: value })}\n`,
  );
}

async function run(job) {
  const sessionDir = resolve(OUTPUT_ROOT, "pi-session");
  const outputPath = resolve(OUTPUT_ROOT, "pi-golden.txt");
  const patchPath = resolve(OUTPUT_ROOT, "pi-golden.patch");
  await mkdir(sessionDir, { recursive: true });

  const faux = fauxProvider();
  faux.setResponses([
    fauxAssistantMessage(
      fauxToolCall("read", { path: resolve(SKILL_DIR, "SKILL.md") }, { id: "read-skill" }),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(
      fauxToolCall("write", { path: outputPath, content: GOLDEN }, { id: "write-golden" }),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(GOLDEN),
  ]);
  const modelRuntime = await ModelRuntime.create({ modelsPath: null, refreshOnCreate: false });
  modelRuntime.registerNativeProvider(faux.provider);
  const settingsManager = SettingsManager.inMemory({ retry: { enabled: false } }, { projectTrusted: false });
  const resourceLoader = new DefaultResourceLoader({
    cwd: OUTPUT_ROOT,
    agentDir: resolve(OUTPUT_ROOT, ".pi-agent"),
    settingsManager,
    additionalSkillPaths: [SKILL_DIR],
    noExtensions: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
  });
  await resourceLoader.reload();
  const skills = resourceLoader.getSkills();
  if (skills.diagnostics.some((item) => item.type === "error") || skills.skills.length !== 1) {
    throw new Error("locked pi-golden Skill was not loaded");
  }

  const { session } = await createAgentSession({
    cwd: OUTPUT_ROOT,
    modelRuntime,
    model: faux.getModel(),
    tools: ["read", "write"],
    resourceLoader,
    settingsManager,
    sessionManager: SessionManager.create(OUTPUT_ROOT, sessionDir),
  });
  try {
    emit("log", 0, { message: "pi_started" }, job);
    await session.prompt("Use the pi-golden Skill and complete its task exactly.");
    const artifact = await readFile(outputPath);
    if (artifact.toString("utf8") !== GOLDEN || faux.state.callCount !== 3) {
      throw new Error("PI golden task did not complete exactly once");
    }
    const sessionPath = session.sessionFile;
    if (!sessionPath || !(await stat(sessionPath)).isFile()) throw new Error("PI session was not persisted");
    await writeFile(patchPath, PATCH, "utf8");
    const artifactRef = { path: "pi-golden.txt", sha256: sha256(artifact) };
    const patchRef = { path: "pi-golden.patch", sha256: sha256(PATCH) };
    const sessionRef = {
      path: relative(OUTPUT_ROOT, sessionPath).replaceAll("\\", "/"),
      sha256: sha256(await readFile(sessionPath)),
    };
    emit("artifact", 1, artifactRef, job);
    emit("patch", 2, patchRef, job);
    emit("session", 3, sessionRef, job);
    emit("final", 4, { text: GOLDEN, artifact: artifactRef, patch: patchRef, session: sessionRef }, job);
  } finally {
    session.dispose();
  }
}

if (process.argv[2] === "--inspect") {
  process.stdout.write(`${JSON.stringify(await inspectRuntime())}\n`);
} else {
  const encoded = process.argv[2];
  if (!encoded) throw new Error("missing base64 job envelope");
  await run(JSON.parse(Buffer.from(encoded, "base64url").toString("utf8")));
}
