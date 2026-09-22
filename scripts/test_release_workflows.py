"""发布事件与应用、CLI 发布边界的回归检查。"""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"


class ReleaseWorkflowTests(unittest.TestCase):
    """阻止候选检查缺失和应用 Release 误触发 CLI 上传。"""

    def assert_cold_build_budget(self, workflow: str) -> None:
        """检查 Runtime job 具有覆盖冷缓存构建的最小预算。"""
        self.assertRegex(
            workflow,
            r"(?m)^    timeout-minutes: (?:[6-9][0-9]|[1-9][0-9]{2,})$",
        )

    def assert_release_events(self, workflows: dict[str, str]) -> None:
        """检查各发布门禁的真实事件声明。"""
        for name in (
            "trust",
            "test",
            "web",
            "ruff",
            "system-tests",
            "dependency-audit",
            "deploy",
        ):
            events = workflows[name].split("\non:\n", 1)[1].split("\n\n", 1)[0]
            push = events.split("  push:\n", 1)[1]
            self.assertRegex(
                push, r"(?m)^    tags: \['v\[0-9\]\*'\]$", f"{name}: 缺少版本 tag 触发"
            )
        cli_events = (
            workflows["publish-yuxi-cli"].split("\non:\n", 1)[1].split("\n\n", 1)[0]
        )
        self.assertEqual(
            re.findall(r"^  (\w+):", cli_events, re.MULTILINE),
            ["workflow_dispatch"],
            "CLI 必须独立手动发布",
        )

    def assert_minio_sources(self, files: dict[str, str]) -> None:
        """所有真实拉取入口使用同一可获取制品。"""
        expected = (
            "quay.io/minio/minio:RELEASE.2023-03-20T20-16-18Z@sha256:"
            "6d770d7f255cda1f18d841ffc4365cb7e0d237f6af6a15fcdb587480cd7c3b93"
        )
        for name, content in files.items():
            references = re.findall(r"[\w./-]*minio/minio:[^\s\"',]+", content)
            reference = expected.split("@", 1)[0] if name.startswith("scripts/init.") else expected
            self.assertEqual(references, [reference], name)

    def test_minio_pull_entries_and_unavailable_source_regression(self) -> None:
        """正常配置一致；任一入口恢复不可获取的 Docker Hub 来源都会失败。"""
        root = WORKFLOWS.parents[1]
        files = {
            name: (root / name).read_text()
            for name in (
                "docker-compose.yml", "docker-compose.prod.yml", "scripts/init.sh",
                "scripts/init.ps1", "backend/test/e2e/knowledge/compose.yaml",
            )
        }
        self.assert_minio_sources(files)
        for name, content in files.items():
            with self.subTest(path=name), self.assertRaises(AssertionError):
                self.assert_minio_sources(files | {name: content.replace("quay.io/minio/", "minio/")})

    def test_installer_minio_reference_can_be_retagged(self) -> None:
        """用真实预拉脚本验证安装入口仍使用可重标记的 tag。"""
        root = WORKFLOWS.parents[1]
        with tempfile.TemporaryDirectory() as directory:
            fake_docker = Path(directory) / "docker"
            fake_docker.write_text(
                '#!/bin/sh\n'
                'printf "%s\\n" "$*" >> "$CI117_DOCKER_LOG"\n'
                'if [ "$1" = tag ]; then case "$3" in *@*) exit 1;; esac; fi\n'
            )
            fake_docker.chmod(0o755)
            log = Path(directory) / "calls"
            env = os.environ | {"PATH": directory + os.pathsep + os.environ["PATH"], "CI117_DOCKER_LOG": str(log)}
            for name in ("init.sh", "init.ps1"):
                reference = re.search(r"quay.io/minio/minio:[^\s\"',]+", (root / "scripts" / name).read_text())[0]
                result = subprocess.run(
                    ["bash", str(root / "scripts/pull_image.sh"), reference],
                    env=env, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"tag m.daocloud.io/{reference} {reference}", log.read_text().splitlines())
            result = subprocess.run(
                ["bash", str(root / "scripts/pull_image.sh"), reference + "@sha256:" + "0" * 64],
                env=env, capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)

    def assert_docs_build_without_pages(self, workflow: str) -> None:
        """PR 构建不依赖仓库已经开通 Pages。"""
        build = workflow.split("  build:\n", 1)[1].split("  deploy:\n", 1)[0]
        self.assertIn("run: pnpm run build", build)
        self.assertNotIn("actions/configure-pages@", build)

    def test_docs_build_without_pages(self) -> None:
        """真实文档工作流可在没有 Pages 站点时完成构建。"""
        self.assert_docs_build_without_pages((WORKFLOWS / "deploy.yml").read_text())

    def test_docs_build_rejects_pages_prerequisite(self) -> None:
        """恢复构建前的 Pages 配置时检查拒绝。"""
        workflow = (WORKFLOWS / "deploy.yml").read_text().replace(
            "  build:\n", "  build:\n    uses: actions/configure-pages@v6\n", 1
        )
        with self.assertRaises(AssertionError):
            self.assert_docs_build_without_pages(workflow)

    def test_repository_release_events(self) -> None:
        """当前配置覆盖候选与正式 tag，CLI 仅手动触发。"""
        self.assert_release_events(
            {path.stem: path.read_text() for path in WORKFLOWS.glob("*.yml")}
        )

    def test_runtime_system_tests_have_cold_build_budget(self) -> None:
        """冷缓存构建不能因过短 job 超时而跳过运行链路。"""
        workflow = (WORKFLOWS / "system-tests.yml").read_text()
        self.assert_cold_build_budget(workflow)

    def test_runtime_system_tests_reject_short_build_budget(self) -> None:
        """恢复 35 分钟冷构建预算时 gate 必须失败。"""
        workflow = (WORKFLOWS / "system-tests.yml").read_text().replace(
            "    timeout-minutes: 60\n", "    timeout-minutes: 35\n", 1
        )
        with self.assertRaises(AssertionError):
            self.assert_cold_build_budget(workflow)

    def test_missing_tag_trigger_is_rejected(self) -> None:
        """恢复仅监听分支的缺陷时对应门禁必须失败。"""
        workflows = {path.stem: path.read_text() for path in WORKFLOWS.glob("*.yml")}
        for name in (
            "trust",
            "test",
            "web",
            "ruff",
            "system-tests",
            "dependency-audit",
            "deploy",
        ):
            with (
                self.subTest(workflow=name),
                self.assertRaisesRegex(AssertionError, f"{name}: 缺少版本 tag 触发"),
            ):
                self.assert_release_events(
                    workflows
                    | {name: workflows[name].replace("    tags: ['v[0-9]*']\n", "")}
                )

    def test_application_release_cannot_publish_cli(self) -> None:
        """恢复应用 Release 触发时禁止重复上传独立 CLI 包。"""
        workflows = {path.stem: path.read_text() for path in WORKFLOWS.glob("*.yml")}
        workflows["publish-yuxi-cli"] = workflows["publish-yuxi-cli"].replace(
            "on:\n", "on:\n  release:\n    types: [published]\n", 1
        )
        with self.assertRaisesRegex(AssertionError, "CLI 必须独立手动发布"):
            self.assert_release_events(workflows)


if __name__ == "__main__":
    unittest.main()
