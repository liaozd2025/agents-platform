import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// 启动依赖必须比较当前源码与真实镜像；打印 inspect JSON 不能证明版本匹配。
const expectedRoot = process.argv[2] || "/opt/yuxi-pi-expected";
const runner = process.argv[3] || "/opt/yuxi-pi-runner/runner.mjs";
const runtime = JSON.parse(execFileSync(process.execPath, [runner, "--inspect"], {
  encoding: "utf8", timeout: 30_000, maxBuffer: 1024 * 1024,
}));
const lock = JSON.parse(readFileSync(join(expectedRoot, "package-lock.json"), "utf8"));
const pi = lock.packages["node_modules/@earendil-works/pi-coding-agent"];
const expected = {
  runner_digest: createHash("sha256").update(readFileSync(join(expectedRoot, "runner.mjs"))).digest("hex"),
  pi_version: pi.version,
  pi_integrity: pi.integrity,
};
for (const [field, value] of Object.entries(expected)) {
  if (runtime[field] !== value) throw new Error(`PI image runtime_mismatch: ${field}`);
}
console.log(JSON.stringify({ status: "verified", ...expected }));
