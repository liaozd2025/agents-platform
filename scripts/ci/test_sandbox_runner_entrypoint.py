"""通过子进程验证旧部署预算转换与失败时不启动命令。"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class RunnerEntrypointTests(unittest.TestCase):
    """只观察子进程最终环境及退出结果。"""

    def run_entrypoint(self, values):
        """使用无部署凭据的独立环境执行转换入口。"""
        env = {"PATH": os.environ["PATH"]} | values
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "docker/sandbox-runner-entrypoint.py"),
                sys.executable,
                "-c",
                'import json,os; print(json.dumps({k:v for k,v in os.environ.items() if k.startswith("SANDBOX_CONTAINER_")}))',
            ],
            env=env,
            text=True,
            capture_output=True,
        )

    def test_legacy_custom_budget_reaches_child_and_new_values_win(self):
        """量纲正确且显式新预算覆盖旧部署值。"""
        result = self.run_entrypoint(
            {
                "SANDBOX_MEMORY_MB": "6144",
                "SANDBOX_TMPFS_MB": "512",
                "SANDBOX_CPUS": "1.5",
                "SANDBOX_MAX_INSTANCES": "7",
                "SANDBOX_MAX_INSTANCES_PER_USER": "3",
                "SANDBOX_DOCKER_PIDS_LIMIT": "768",
                "SANDBOX_CONTAINER_PIDS_LIMIT": "1024",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        self.assertEqual(values["SANDBOX_CONTAINER_MEMORY_BYTES"], "6442450944")
        self.assertEqual(values["SANDBOX_CONTAINER_TMP_BYTES"], "536870912")
        self.assertEqual(values["SANDBOX_CONTAINER_CPUS"], "1.5")
        self.assertEqual(values["SANDBOX_CONTAINER_MAX_CONTAINERS"], "7")
        self.assertEqual(values["SANDBOX_CONTAINER_MAX_CONTAINERS_PER_USER"], "3")
        self.assertEqual(values["SANDBOX_CONTAINER_PIDS_LIMIT"], "1024")

    def test_invalid_budgets_never_execute_child(self):
        """非法旧值、非正预算及非有限 CPU 在执行进程之前拒绝。"""
        for values in (
            {"SANDBOX_MEMORY_MB": "bad"},
            {"SANDBOX_TMPFS_MB": "0"},
            {"SANDBOX_CPUS": "nan"},
            {"SANDBOX_CONTAINER_CPUS": "inf"},
            {"SANDBOX_CONTAINER_MAX_CONTAINERS_PER_USER": "-1"},
            {"SANDBOX_MEMORY_MB": "4"},
            {"SANDBOX_CPUS": "0.0001"},
            {"SANDBOX_TMPFS_MB": "4097"},
            {"SANDBOX_MAX_INSTANCES_PER_USER": "7"},
        ):
            with self.subTest(values=values):
                result = self.run_entrypoint(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_blank_compose_values_preserve_fork_defaults(self):
        """未设置的新变量不能把 fork 默认预算恢复为上游值。"""
        result = self.run_entrypoint({"SANDBOX_CONTAINER_MEMORY_BYTES": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        self.assertEqual(values["SANDBOX_CONTAINER_MEMORY_BYTES"], "4294967296")
        self.assertEqual(values["SANDBOX_CONTAINER_CPUS"], "2.0")
        self.assertEqual(values["SANDBOX_CONTAINER_MAX_CONTAINERS"], "6")
        self.assertEqual(values["SANDBOX_CONTAINER_MAX_CONTAINERS_PER_USER"], "4")


if __name__ == "__main__":
    unittest.main()
