import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

test("startup inspector rejects stale runner, PI dependencies and unpatched shell stdin", () => {
  const directory = mkdtempSync(join(tmpdir(), "yuxi-pi-verify-"));
  try {
    const source = "current runner source";
    const shell = join(directory, "shell.py");
    const sourceChecker = new URL("../../backend/package/yuxi/pi_runner/patch-shell-input.py", import.meta.url);
    const checker = readFileSync(existsSync(sourceChecker) ? sourceChecker : "/opt/yuxi-pi-runner/patch-shell-input.py", "utf8")
      .replace("/opt/python3.12/lib/python3.12/site-packages/app/api/v1/shell.py", shell);
    writeFileSync(join(directory, "patch-shell-input.py"), checker);
    writeFileSync(join(directory, "runner.mjs"), source);
    writeFileSync(join(directory, "package-lock.json"), JSON.stringify({ packages: {
      "node_modules/@earendil-works/pi-coding-agent": { version: "0.84.2", integrity: "sha512-expected" },
    } }));
    const expected = {
      runner_digest: createHash("sha256").update(source).digest("hex"),
      pi_version: "0.84.2", pi_integrity: "sha512-expected",
    };
    for (const field of [null, ...Object.keys(expected), "shell_stdin"]) {
      writeFileSync(shell, "async def write_to_process(request):\n    await " + (field === "shell_stdin"
        ? "terminal_manager.execute_command(request.id, request.input, async_mode=True)\n"
        : "asyncio.to_thread(session.bash_session.pane.send_keys, request.input, enter=request.press_enter)\n"));
      const actual = field && field !== "shell_stdin" ? { ...expected, [field]: "stale" } : expected;
      const runner = join(directory, "inspect.mjs");
      writeFileSync(runner, `console.log(${JSON.stringify(JSON.stringify(actual))})`);
      const result = spawnSync(process.execPath, [fileURLToPath(new URL("./verify-runtime.mjs", import.meta.url)), directory, runner], {
        encoding: "utf8",
      });
      if (field) {
        assert.notEqual(result.status, 0);
        assert.ok(result.stderr.includes(`runtime_mismatch: ${field}`), result.stderr);
      } else {
        assert.equal(result.status, 0, result.stderr);
        assert.equal(JSON.parse(result.stdout).status, "verified");
      }
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
