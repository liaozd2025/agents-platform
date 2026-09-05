import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

test("startup inspector rejects stale runner, PI version and dependency integrity", () => {
  const directory = mkdtempSync(join(tmpdir(), "yuxi-pi-verify-"));
  try {
    const source = "current runner source";
    writeFileSync(join(directory, "runner.mjs"), source);
    writeFileSync(join(directory, "package-lock.json"), JSON.stringify({ packages: {
      "node_modules/@earendil-works/pi-coding-agent": { version: "0.84.2", integrity: "sha512-expected" },
    } }));
    const expected = {
      runner_digest: createHash("sha256").update(source).digest("hex"),
      pi_version: "0.84.2", pi_integrity: "sha512-expected",
    };
    for (const field of [null, ...Object.keys(expected)]) {
      const actual = field ? { ...expected, [field]: "stale" } : expected;
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
