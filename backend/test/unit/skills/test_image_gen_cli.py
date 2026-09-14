"""通过 CLI 与外部 HTTP 边界验证图片生成协议。"""

import importlib.util
import io
import json
from pathlib import Path

import pytest
import requests
from PIL import Image


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """加载实际随技能分发的脚本。"""
    path = Path(__file__).resolve().parents[3] / "package/yuxi/agents/skills/buildin/image-gen/scripts/image_gen.py"
    spec = importlib.util.spec_from_file_location("image_gen_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KIE_API_KEY", "test-kie-key")
    return module


@pytest.fixture
def http(monkeypatch):
    """用固定协议响应替代外部服务，记录实际准备的请求。"""
    replies, sent = [], []

    def send(session, request, **kwargs):
        sent.append(request)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        response = requests.Response()
        response.status_code = 200
        if isinstance(reply, tuple):
            response.status_code, reply = reply
        response._content = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        response.raw = io.BytesIO(response._content)
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    return replies, sent


def test_generate_uses_gpt_image_2_and_returns_task_without_downloading(cli, monkeypatch):
    """提交成功只交付任务编号，不伪装为图片生成成功。"""

    def send(session, request, **kwargs):
        assert request.url == "https://api.kie.ai/api/v1/jobs/createTask"
        assert request.headers["Authorization"] == "Bearer test-kie-key"
        assert json.loads(request.body) == {
            "model": "gpt-image-2-text-to-image",
            "input": {"prompt": "画一只猫", "aspect_ratio": "auto", "resolution": "1K"},
        }
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"code":200,"data":{"taskId":"task-123"}}'
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    assert cli.run(["generate", "--prompt", "画一只猫"]) == {"state": "submitted", "task_id": "task-123"}
    assert not Path("outputs").exists()


@pytest.mark.parametrize(
    ("model", "image", "expected_model", "image_field"),
    [
        ("gpt-image-2", "https://example.com/ref.png", "gpt-image-2-image-to-image", "input_urls"),
        ("nano-banana-2", "https://example.com/ref.png", "nano-banana-2", "image_input"),
        ("nano-banana-2", None, "nano-banana-2", "image_input"),
    ],
)
def test_generate_maps_model_and_reference_images(cli, http, model, image, expected_model, image_field):
    """两种模型分别使用官方协议的参考图字段。"""
    replies, sent = http
    replies.append({"code": 200, "data": {"taskId": "task-123"}})
    args = ["generate", "--prompt", "海报", "--model", model]
    if image:
        args.extend(["--image-url", image])
    cli.run(args)
    body = json.loads(sent[0].body)
    assert body["model"] == expected_model
    assert body["input"][image_field] == ([image] if image else [])
    if model == "nano-banana-2":
        assert body["input"]["output_format"] == "png"


@pytest.mark.parametrize(
    "args",
    [
        ["--model", "Qwen/Qwen-Image"],
        ["--prompt", " "],
        ["--prompt", "a" * 20001],
        ["--image-url", "file:///etc/passwd"],
        ["--image-url", "https://user:password@example.com/ref.png"],
        ["--resolution", "4K"],
        ["--resolution", "4K", "--aspect-ratio", "1:1"],
        ["--resolution", "2K", "--aspect-ratio", "5:4"],
        ["--model", "nano-banana-2", "--aspect-ratio", "2:1"],
        ["--model", "nano-banana-2"] + ["--image-url", "https://example.com/ref.png"] * 15,
    ],
)
def test_invalid_generation_input_never_sends_paid_request(cli, http, args):
    """在模型输入边界拒绝非法参数，不消耗额度。"""
    with pytest.raises((ValueError, SystemExit)):
        cli.run(["generate", "--prompt", "猫", *args])
    assert not http[1]


def test_collect_saves_decodable_image_without_sending_key_to_download(cli, http):
    """以图片文件内容和实际请求头证明收取结果。"""
    replies, sent = http
    image = io.BytesIO()
    Image.new("RGB", (2, 3), "red").save(image, format="PNG")
    replies.extend(
        [
            {
                "code": 200,
                "data": {
                    "taskId": "task-123",
                    "model": "gpt-image-2-text-to-image",
                    "state": "success",
                    "resultJson": '{"resultUrls":["https://example.com/result.png"]}',
                },
            },
            image.getvalue(),
        ]
    )
    result = cli.run(["collect", "--task-id", "task-123"])
    assert result["state"] == "success"
    assert result["task_id"] == "task-123"
    assert len(result["files"]) == 1
    path = Path(result["files"][0])
    assert path.parent == Path.cwd() / "outputs"
    with Image.open(path) as saved:
        assert saved.size == (2, 3)
        assert saved.getpixel((0, 0)) == (255, 0, 0)
    assert sent[0].url.endswith("recordInfo?taskId=task-123")
    assert "Authorization" not in sent[1].headers
    assert "https://" not in json.dumps(result)


@pytest.mark.parametrize("state", ["waiting", "queuing", "generating"])
def test_collect_pending_preserves_task_and_never_resubmits(cli, http, state):
    """查询未完成任务不会新建任务或产生图片。"""
    replies, sent = http
    replies.append({"code": 200, "data": {"taskId": "task-123", "state": state}})
    assert cli.run(["collect", "--task-id", "task-123"]) == {"state": state, "task_id": "task-123"}
    assert [req.method for req in sent] == ["GET"]
    assert not Path("outputs").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"taskId": "another-task"},
        {"state": []},
        {"model": []},
        {"resultJson": {}},
        {"state": "unexpected"},
        {"state": "fail", "failMsg": "rejected"},
        {"resultJson": "{}"},
        {"resultJson": "not json"},
        {"model": "other-model"},
        {"resultJson": '{"resultUrls":["file:///etc/passwd"]}'},
    ],
)
def test_collect_rejects_failed_or_invalid_results(cli, http, change):
    """拒绝失败、串任务和无效结果，不发布图片。"""
    replies, _ = http
    replies.append(
        {
            "code": 200,
            "data": {
                "taskId": "task-123",
                "model": "nano-banana-2",
                "state": "success",
                "resultJson": '{"resultUrls":["https://example.com/result.png"]}',
                **change,
            },
        }
    )
    with pytest.raises(ValueError):
        cli.run(["collect", "--task-id", "task-123"])
    assert not Path("outputs").exists()


def test_collect_html_download_is_not_an_image(cli, http):
    """CDN 返回错误页面时不得写出伪图片。"""
    replies, _ = http
    replies.extend(
        [
            {
                "code": 200,
                "data": {
                    "taskId": "task-123",
                    "model": "nano-banana-2",
                    "state": "success",
                    "resultJson": '{"resultUrls":["https://example.com/result.png"]}',
                },
            },
            b"<html>error</html>",
        ]
    )
    with pytest.raises(ValueError, match="图片"):
        cli.run(["collect", "--task-id", "task-123"])
    assert not Path("outputs").exists()


def test_upload_reference_image_returns_kie_url(cli, http):
    """本地参考图经官方上传入口取得可用于编辑的 URL。"""
    replies, sent = http
    Image.new("RGB", (2, 3), "blue").save("reference.png")
    replies.append({"success": True, "code": 200, "data": {"downloadUrl": "https://example.com/reference.png"}})
    assert cli.run(["upload", "--file", "reference.png"]) == {"image_url": "https://example.com/reference.png"}
    assert sent[0].url == "https://kieai.redpandaai.co/api/file-stream-upload"
    assert sent[0].headers["Authorization"] == "Bearer test-kie-key"
    assert b'name="file"' in sent[0].body
    assert b"image/png" in sent[0].body
    assert b"image-gen" in sent[0].body


@pytest.mark.parametrize("kind", ["outside", "symlink", "directory_symlink", "text", "large"])
def test_upload_rejects_invalid_reference_before_network(cli, http, tmp_path, kind):
    """文件副作用边界拒绝越界、链接与无效图片。"""
    Image.new("RGB", (2, 3)).save("reference.png")
    if kind == "outside":
        path = "../reference.png"
    elif kind == "symlink":
        Path("link.png").symlink_to("reference.png")
        path = "link.png"
    elif kind == "directory_symlink":
        Path("linked").symlink_to(tmp_path, target_is_directory=True)
        path = "linked/reference.png"
    elif kind == "text":
        Path("reference.png").write_text("not image")
        path = "reference.png"
    else:
        with open("reference.png", "wb") as image:
            image.truncate(30 * 1024 * 1024 + 1)
        path = "reference.png"
    with pytest.raises((ValueError, OSError)):
        cli.run(["upload", "--file", path])
    assert not http[1]


def test_output_symlink_cannot_redirect_writes(cli, http, tmp_path):
    """outputs 被替换为链接时不向链接目标写入。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    Path("outputs").symlink_to(outside, target_is_directory=True)
    image = io.BytesIO()
    Image.new("RGB", (2, 3)).save(image, format="PNG")
    http[0].extend(
        [
            {
                "code": 200,
                "data": {
                    "taskId": "task-123",
                    "model": "nano-banana-2",
                    "state": "success",
                    "resultJson": '{"resultUrls":["https://example.com/result.png"]}',
                },
            },
            image.getvalue(),
        ]
    )
    with pytest.raises(OSError):
        cli.run(["collect", "--task-id", "task-123"])
    assert not list(outside.iterdir())


@pytest.mark.parametrize(
    "reply",
    [
        (401, {}),
        (302, {}),
        {"code": 402, "data": {}},
        {"code": 200, "data": []},
        {"code": 200, "success": False, "data": {}},
        [],
        b"invalid json",
        {"code": 200, "data": {}},
        {"code": 200, "data": {"taskId": "../task"}},
    ],
)
def test_generate_rejects_invalid_provider_reply(cli, http, reply):
    """HTTP 成功、错误 JSON 或缺任务编号均不能冒充任务创建成功。"""
    http[0].append(reply)
    with pytest.raises(ValueError):
        cli.run(["generate", "--prompt", "猫"])
    assert len(http[1]) == 1


def test_missing_key_never_sends_request(cli, http, monkeypatch):
    """缺少沙盒密钥时明确失败。"""
    monkeypatch.delenv("KIE_API_KEY")
    with pytest.raises(ValueError, match="KIE_API_KEY"):
        cli.run(["generate", "--prompt", "猫"])
    assert not http[1]


@pytest.mark.parametrize("command", ["generate", "collect"])
def test_cli_timeout_retains_task_or_reports_unknown_submission(cli, http, monkeypatch, capsys, command):
    """CLI 超时不会泄露异常中的凭据，也不会自动发起新任务。"""
    http[0].append(requests.Timeout("test-kie-key"))
    args = ["--prompt", "猫"] if command == "generate" else ["--task-id", "task-123"]
    monkeypatch.setattr("sys.argv", ["image_gen.py", command, *args])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "test-kie-key" not in output
    result = json.loads(output)
    if command == "generate":
        assert result["state"] == "submission_unknown"
    else:
        assert result["task_id"] == "task-123"
    assert len(http[1]) == 1


def test_cli_redacts_key_from_provider_failure(cli, http, monkeypatch, capsys):
    """上游失败原因即使回显密钥，也不得出现在工具结果中。"""
    http[0].append({"code": 200, "data": {"taskId": "task-123", "state": "fail", "failMsg": "test-kie-key"}})
    monkeypatch.setattr("sys.argv", ["image_gen.py", "collect", "--task-id", "task-123"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "test-kie-key" not in capsys.readouterr().out


def test_collect_rejects_oversized_download(cli, http):
    """下载体积受限，不能把超大响应写入 outputs。"""
    http[0].extend(
        [
            {
                "code": 200,
                "data": {
                    "taskId": "task-123",
                    "model": "nano-banana-2",
                    "state": "success",
                    "resultJson": '{"resultUrls":["https://example.com/result.png"]}',
                },
            },
            b"a" * (30 * 1024 * 1024 + 1),
        ]
    )
    with pytest.raises(ValueError, match="30 MiB"):
        cli.run(["collect", "--task-id", "task-123"])
    assert not Path("outputs").exists()
