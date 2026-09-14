"""在 Agent 沙盒中提交 Kie 图片任务并收取图片。"""

import argparse
import io
import json
import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from PIL import Image, UnidentifiedImageError


MODELS = {
    "gpt-image-2": {
        "text": "gpt-image-2-text-to-image",
        "edit": "gpt-image-2-image-to-image",
        "images": "input_urls",
        "max_images": 16,
        "ratios": "auto 1:1 3:2 2:3 4:3 3:4 5:4 4:5 16:9 9:16 2:1 1:2 3:1 1:3 21:9 9:21".split(),
    },
    "nano-banana-2": {
        "text": "nano-banana-2",
        "edit": "nano-banana-2",
        "images": "image_input",
        "max_images": 14,
        "ratios": "auto 1:1 2:3 3:2 1:4 4:1 3:4 4:3 4:5 5:4 1:8 8:1 9:16 16:9 21:9".split(),
    },
}
MAX_IMAGE_BYTES = 30 * 1024 * 1024


def run(argv: list[str] | None = None) -> dict:
    """执行图片生成命令，返回可供 Agent 使用的结构化结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--prompt", required=True)
    generate.add_argument("--model", choices=MODELS, default="gpt-image-2")
    generate.add_argument("--image-url", action="append", default=[])
    generate.add_argument("--aspect-ratio", default="auto")
    generate.add_argument("--resolution", choices=["1K", "2K", "4K"], default="1K")
    collect = commands.add_parser("collect")
    collect.add_argument("--task-id", required=True)
    upload = commands.add_parser("upload")
    upload.add_argument("--file", required=True)
    args = parser.parse_args(argv)
    if args.command == "collect":
        return collect_images(valid_task_id(args.task_id))
    if args.command == "upload":
        return upload_image(args.file)
    model = MODELS[args.model]
    if not args.prompt.strip() or len(args.prompt) > 20000:
        raise ValueError("prompt 必须为 1 到 20000 字的非空文本")
    if len(args.image_url) > model["max_images"] or args.aspect_ratio not in model["ratios"]:
        raise ValueError("参考图数量或宽高比超出所选模型范围")
    for url in args.image_url:
        https_url(url)
    if args.model == "gpt-image-2" and args.resolution != "1K":
        unsupported = {"auto", "3:1", "1:3", "9:21"}
        unsupported.update({"5:4", "4:5"} if args.resolution == "2K" else {"1:1"})
        if args.aspect_ratio in unsupported:
            raise ValueError("GPT Image 2 不支持此分辨率与宽高比组合")
    payload = {"prompt": args.prompt, "aspect_ratio": args.aspect_ratio, "resolution": args.resolution}
    if args.image_url or args.model == "nano-banana-2":
        payload[model["images"]] = args.image_url
    if args.model == "nano-banana-2":
        payload["output_format"] = "png"
    data = api_request(
        "POST",
        "https://api.kie.ai/api/v1/jobs/createTask",
        json={"model": model["edit" if args.image_url else "text"], "input": payload},
    )
    return {"state": "submitted", "task_id": valid_task_id(data.get("taskId"))}


def upload_image(filename: str) -> dict:
    """读取当前用户目录内的普通图片，经 Kie 上传接口取得参考地址。"""
    cwd = Path.cwd()
    user_data = Path("/home/gem/user-data")
    root = user_data if cwd.is_relative_to(user_data) else cwd
    path = Path(filename)
    relative = (path if path.is_absolute() else cwd / path).relative_to(root)
    if not relative.parts or ".." in relative.parts:
        raise ValueError("参考图片必须位于当前用户文件目录")
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        with os.fdopen(fd, "rb") as image:
            if not stat.S_ISREG(os.fstat(image.fileno()).st_mode):
                raise ValueError("参考图片必须是普通文件")
            content = image.read(MAX_IMAGE_BYTES + 1)
    finally:
        os.close(directory_fd)
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError("参考图片超过 30 MiB 限制")
    extension = image_extension(content)
    mime = "image/jpeg" if extension == "jpg" else f"image/{extension}"
    data = api_request(
        "POST",
        "https://kieai.redpandaai.co/api/file-stream-upload",
        files={"file": (f"{uuid4().hex}.{extension}", content, mime)},
        data={"uploadPath": "image-gen"},
    )
    return {"image_url": https_url(data.get("downloadUrl"))}


def collect_images(task_id: str) -> dict:
    """查询原任务；仅成功且图片有效时写入当前工作目录。"""
    data = api_request("GET", "https://api.kie.ai/api/v1/jobs/recordInfo", params={"taskId": task_id})
    if data.get("taskId") != task_id:
        raise ValueError("Kie 返回了不同任务的结果")
    state = data.get("state")
    if not isinstance(state, str):
        raise ValueError("Kie 返回无效任务状态")
    if state in {"waiting", "queuing", "generating"}:
        return {"state": state, "task_id": task_id}
    if state == "fail":
        raise ValueError(f"Kie 图片生成失败：{data.get('failMsg') or '未提供原因'}")
    remote_model = data.get("model")
    if (
        state != "success"
        or not isinstance(remote_model, str)
        or remote_model not in {item[k] for item in MODELS.values() for k in ("text", "edit")}
    ):
        raise ValueError("Kie 返回未知状态或不支持的模型")
    raw_result = data.get("resultJson")
    if not isinstance(raw_result, str):
        raise ValueError("Kie 返回无效图片结果")
    result = json.loads(raw_result)
    urls = result.get("resultUrls") if isinstance(result, dict) else None
    if not isinstance(urls, list) or not urls:
        raise ValueError("Kie 成功响应缺少图片地址")
    files = []
    for url in urls:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(https_url(url), timeout=30, allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise ValueError(f"图片下载失败（HTTP {response.status_code}）")
                image = bytearray()
                for chunk in response.iter_content(65536):
                    image.extend(chunk)
                    if len(image) > MAX_IMAGE_BYTES:
                        raise ValueError("图片超过 30 MiB 限制")
        extension = image_extension(image)
        output_dir = Path.cwd() / "outputs"
        output_dir.mkdir(exist_ok=True)
        directory_fd = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        name = f"image-{uuid4().hex}.{extension}"
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(image)
            except BaseException:
                os.unlink(name, dir_fd=directory_fd)
                raise
        finally:
            os.close(directory_fd)
        files.append(str(output_dir / name))
    return {"state": "success", "task_id": task_id, "files": files}


def image_extension(content: bytes | bytearray) -> str:
    """验证可解码的 PNG、JPEG 或 WebP，并返回与内容一致的后缀。"""
    try:
        with Image.open(io.BytesIO(content), formats=["PNG", "JPEG", "WEBP"]) as image:
            image.load()
            return {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[str(image.format)]
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError("响应或参考文件不是有效图片") from exc


def https_url(value: object) -> str:
    """校验图片 URL，禁止本地协议和内嵌凭据。"""
    if not isinstance(value, str):
        raise ValueError("图片地址必须是 HTTPS URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("图片地址必须是无内嵌凭据的 HTTPS URL")
    return value


def valid_task_id(value: object) -> str:
    """拒绝缺失或无法安全回传的任务编号。"""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("缺少有效 taskId；请核对 Kie 任务记录，勿自动重新提交")
    return value


def api_request(method: str, url: str, **kwargs) -> dict:
    """向 Kie 固定接口发送请求，不重试付费提交或跟随跳转。"""
    key = os.environ.get("KIE_API_KEY", "").strip()
    if not key:
        raise ValueError("请在 Agent 沙盒环境变量中配置 KIE_API_KEY")
    with requests.Session() as session:
        session.trust_env = False
        response = session.request(
            method,
            url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
            allow_redirects=False,
            **kwargs,
        )
        if response.status_code != 200:
            raise ValueError(f"Kie HTTP 请求失败（{response.status_code}）")
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("code"), int) and body["code"] != 200:
            raise ValueError(f"Kie 请求失败（code={body['code']}）：{body.get('msg') or '未提供原因'}")
        if (
            not isinstance(body, dict)
            or body.get("code") != 200
            or body.get("success") is False
            or not isinstance(body.get("data"), dict)
        ):
            raise ValueError("Kie 返回失败状态或无效响应")
        return body["data"]


def main() -> None:
    """输出 JSON 与退出码，隐藏凭据并保留原任务查询入口。"""
    try:
        result = run()
    except (ValueError, OSError, requests.RequestException) as exc:
        message = str(exc) if isinstance(exc, ValueError) else f"请求或文件操作失败（{type(exc).__name__}）"
        key = os.environ.get("KIE_API_KEY", "").strip()
        if key:
            message = message.replace(key, "[REDACTED]")
        result = {"state": "error", "message": message[:500]}
        if "--task-id" in sys.argv:
            result["task_id"] = sys.argv[sys.argv.index("--task-id") + 1]
        if len(sys.argv) > 1 and sys.argv[1] == "generate" and isinstance(exc, requests.RequestException):
            result["state"] = "submission_unknown"
            result["message"] = "提交结果未知；请核对 Kie 任务记录，勿自动重新提交"
        print(json.dumps(result, ensure_ascii=False), flush=True)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
