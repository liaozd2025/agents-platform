"""图片输入能力判定与视觉转述。

主模型不接受图片输入时（实测 DashScope 的 qwen3.7-max 对任何图片都返回 400），
把图片交给管理员配置的视觉理解模型转述成文本，让主模型继续本轮工作。

本模块提供两个消费点共用的能力：

- 能力判定：模型声明的 ``input_modalities`` 里是否确定不含 ``image``；
  ``["text"]`` 表示"确定不支持图片"，空声明表示"未知"（不能据此预判）。
- 视觉转述：把一条消息里的图片块替换为视觉模型输出的文本转述。

消费者：``agents/middlewares/model_input.py``（错误驱动的兜底，覆盖未声明的模型）与
``services/chat_service.py``（已声明不支持图片时的预路由，主模型一次都不必失败）。
"""

from __future__ import annotations

import difflib
import hashlib
import posixpath
import re
import time
from typing import Any

from langchain_core.messages import HumanMessage
from yuxi.config.options import system_options
from yuxi.models.providers.cache import model_cache
from yuxi.utils.logging_config import logger

IMAGE_MODALITY = "image"
VISION_MODEL_OPTION_KEY = "vision_model"

# 视觉转述的两层进程内缓存：
# 1) 不接受图片的模型按 spec 记住，避免每轮都先发一次注定失败的请求；
# 2) 转述结果按 (视觉模型, 图片指纹, 问题指纹) 缓存，多步工具循环里同一张图只付一次费。
_VISION_CACHE_TTL_SECONDS = 600.0
_non_image_model_specs: dict[str, float] = {}
_vision_transcripts: dict[tuple[str, str, str], tuple[float, str]] = {}

# 转述是"描述图片 + 给结论"的轻任务：给视觉模型限长并关闭思考。
# 实测 qwen3.7-flash 对 ~1MB 图片：默认思考不限制 53s/2657 tokens，两者都加 1.7s，
# 转述质量没有可感知差异。支持 enable_thinking 的供应商才吃得到加速，
# 不支持的会在请求失败后退回朴素调用（见 _params_rejected_specs）。
_TRANSCRIBE_MAX_TOKENS = 1000
_params_rejected_specs: set[str] = set()

# 路径抄写纠错：长随机文件名（hex hash）是模型抄写易错点，抄错一个字符就会 read_file 404。
# 纠正只认"同一目录下、相似度达标、且无第二个同样接近的候选"的路径，避免把模型本来想读的
# 另一个相似文件改成别的文件。
_PATH_PATTERN = re.compile(r"/(?:[\w.\-]+/)+[\w.\-]+")
_PATH_SIMILAR_MIN_RATIO = 0.9
_PATH_AMBIGUITY_MARGIN = 0.03


def collect_absolute_paths(text: str) -> list[str]:
    """提取文本中的绝对路径（去重保序）。"""
    return list(dict.fromkeys(_PATH_PATTERN.findall(text or "")))


def closest_real_path(path: str, real_paths: list[str]) -> tuple[str | None, float, bool]:
    """找与 path 最接近的真实路径，返回 (路径, 相似度, 是否有歧义)。

    只在同一父目录内比较：实际缺陷模式是文件名里的 hash 被抄错，目录不变；
    目录都不一致时视为模型本意就是别的路径，不纠正。
    """
    dirname = posixpath.dirname(path)
    if not dirname:
        return None, 0.0, False
    best, best_ratio, second = None, 0.0, 0.0
    for real in real_paths:
        if real == path or posixpath.dirname(real) != dirname:
            continue
        ratio = difflib.SequenceMatcher(None, path, real).ratio()
        if ratio > best_ratio:
            second, best, best_ratio = best_ratio, real, ratio
        elif ratio > second:
            second = ratio
    if best_ratio < _PATH_SIMILAR_MIN_RATIO:
        return None, best_ratio, False
    ambiguous = best is not None and second > 0 and (best_ratio - second) < _PATH_AMBIGUITY_MARGIN
    return best, best_ratio, ambiguous


def correct_transcript_paths(transcript: str, reference_text: str) -> str:
    """把转述文本里被抄错的路径纠正为消息原文中的真实路径。

    转述若抄错路径，主模型会照抄并导致 read_file 404（实测
    ``9fc2ac55…`` 被抄成 ``9fc2c554…``）。无真实路径可参照时不做任何替换。
    """
    real_paths = collect_absolute_paths(reference_text)
    if not real_paths:
        return transcript
    corrected = transcript
    for candidate in collect_absolute_paths(transcript):
        if candidate in real_paths:
            continue
        best, _ratio, ambiguous = closest_real_path(candidate, real_paths)
        if best and not ambiguous:
            corrected = corrected.replace(candidate, best)
            logger.info(f"转述中的路径已自动纠正: {candidate} -> {best}")
    return corrected


_TRANSCRIBE_INSTRUCTION = (
    "你是视觉转述器。请阅读随附图片，输出一段供不支持图片输入的模型继续工作的文本转述：\n"
    "1. 客观描述图片内容（主体、可见文字、版式构图、风格与颜色），不要臆造看不清的细节。\n"
    "2. 如果下面的用户消息提出了问题或任务，一并给出结论。\n"
    "3. 如果用户消息中给出了图片文件路径，原样保留该路径。\n"
    "只输出转述与结论，不要重复这些要求。"
)


def model_spec_of(model: Any) -> str:
    """读取模型的 yuxi spec；外部模型或未标注元数据时返回空串。"""
    metadata = getattr(model, "metadata", None)
    spec = metadata.get("yuxi_model_spec") if isinstance(metadata, dict) else None
    if isinstance(spec, str) and spec:
        return spec
    fallback = getattr(model, "model_name", None) or getattr(model, "model", None)
    return str(fallback or "")


def message_image_blocks(message: Any) -> list[dict[str, Any]]:
    """取消息里的图片块（贴图与 read_file 工具图片都体现在 content_blocks 上）。"""
    return [block for block in getattr(message, "content_blocks", []) if block.get("type") == "image"]


def message_has_image(message: Any) -> bool:
    return bool(message_image_blocks(message))


def message_text(message: Any) -> str:
    """取消息的文本部分，忽略图片、推理与工具调用块。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    return "".join(
        block.get("text", "") for block in getattr(message, "content_blocks", []) if block.get("type") == "text"
    ).strip()


def _model_info(spec: str | None):
    """按 spec 读取模型信息；未登记或读取失败时返回 None。"""
    if not spec:
        return None
    try:
        return model_cache.get_model_info(spec)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取模型 {spec} 的信息失败: {exc}")
        return None


def model_declared_modalities(spec: str | None) -> list[str]:
    """读取模型声明的输入模态；未登记或未声明时返回空列表。"""
    info = _model_info(spec)
    modalities = getattr(info, "input_modalities", None) if info else None
    return [str(item) for item in modalities] if isinstance(modalities, list) else []


def _supports_tuning(spec: str | None) -> bool:
    """调优参数（extra_body/enable_thinking）只有 OpenAI 兼容系客户端有意义。

    Anthropic / Gemini 客户端会把 extra_body 转成请求体里的未知字段、被供应商拒绝；
    这里直接跳过，省掉一次注定失败的试错请求。OpenAI 官方等仍可能拒绝
    （由 ``_params_rejected_specs`` 的退回路径兜底）。
    """
    return getattr(_model_info(spec), "provider_type", "") == "openai"


def model_is_declared_text_only(spec: str | None) -> bool:
    """模型是否被明确声明为不支持图片（声明非空且不含 image）。

    空声明表示"未知能力"，不能据此预判，仍由中间件的错误兜底处理。
    """
    modalities = model_declared_modalities(spec)
    return bool(modalities) and IMAGE_MODALITY not in modalities


def is_known_non_image_model(spec: str) -> bool:
    expiry = _non_image_model_specs.get(spec)
    return bool(expiry and expiry > time.monotonic())


def mark_non_image_model(spec: str) -> None:
    """记录该模型不接受图片输入，后续请求直接走转述。"""
    if spec:
        _non_image_model_specs[spec] = time.monotonic() + _VISION_CACHE_TTL_SECONDS


async def resolve_vision_model_spec() -> str | None:
    """读取管理员配置的视觉理解模型；未配置或读取失败时返回 None（调用方降级）。"""
    try:
        configured = (await system_options.get()).get(VISION_MODEL_OPTION_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取视觉理解模型配置失败: {exc}")
        return None
    return configured.strip() if isinstance(configured, str) and configured.strip() else None


def _image_fingerprint(blocks: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for block in blocks:
        digest.update(str(block.get("base64") or block.get("url") or "").encode("utf-8"))
    return digest.hexdigest()


async def _transcribe_once(*, vision_model: Any, vision_spec: str, question: str, blocks: list[dict]) -> str | None:
    """调用一次视觉模型得到转述文本；同一图片与问题命中缓存时不再重复调用。"""
    cache_key = (vision_spec, _image_fingerprint(blocks), hashlib.sha256(question.encode("utf-8")).hexdigest())
    cached = _vision_transcripts.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    sidecar_content: list[dict[str, Any]] = [{"type": "text", "text": _TRANSCRIBE_INSTRUCTION}, *blocks]
    if question:
        sidecar_content.append({"type": "text", "text": question})
    try:
        response = await vision_model.ainvoke([HumanMessage(content=sidecar_content)])
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"视觉模型 {vision_spec} 转述图片失败: {exc}")
        return None

    transcript = message_text(response)
    if not transcript:
        logger.warning(f"视觉模型 {vision_spec} 返回空转述")
        return None
    transcript = correct_transcript_paths(transcript, question)
    _vision_transcripts[cache_key] = (time.monotonic() + _VISION_CACHE_TTL_SECONDS, transcript)
    logger.info(f"图片已由视觉模型 {vision_spec} 转述，字符数={len(transcript)}")
    return transcript


def _load_vision_model(vision_spec: str, *, use_tuning: bool) -> Any:
    """加载视觉模型；use_tuning 时附带限长与关闭思考（仅部分供应商支持）。"""
    from yuxi.models.chat import load_chat_model  # 仅图片兜底路径需要，避免常态加载模型工厂

    if not use_tuning:
        return load_chat_model(vision_spec)
    return load_chat_model(
        vision_spec,
        max_tokens=_TRANSCRIBE_MAX_TOKENS,
        extra_body={"enable_thinking": False},
    )


async def transcribe_message_images(message: Any, *, exclude_spec: str | None = None) -> Any | None:
    """把消息里的图片块替换为视觉模型的转述文本，返回消息副本；不可用时返回 None。

    ``exclude_spec`` 是主模型 spec：与视觉模型相同时不再调用（必然同样被拒）。
    返回 ``None`` 时调用方应降级（既有 OCR 兜底或原始错误），不得伪造转述内容。
    """
    blocks = message_image_blocks(message)
    if not blocks:
        return None

    vision_spec = await resolve_vision_model_spec()
    if not vision_spec:
        logger.warning("未配置视觉理解模型，图片转述不可用")
        return None
    if exclude_spec and vision_spec == exclude_spec:
        logger.warning(f"视觉理解模型与主模型相同（{vision_spec}），跳过图片转述")
        return None

    # 调优参数（限长/关思考）只有 OpenAI 兼容系供应商吃得到：其余直接朴素调用；
    # OpenAI 兼容系里仍可能被拒绝（如官方 API 不认识 enable_thinking），失败后退回并记住
    use_tuning = _supports_tuning(vision_spec) and vision_spec not in _params_rejected_specs
    try:
        vision_model = _load_vision_model(vision_spec, use_tuning=use_tuning)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"加载视觉理解模型 {vision_spec} 失败: {exc}")
        return None

    transcript = await _transcribe_once(
        vision_model=vision_model,
        vision_spec=vision_spec,
        question=message_text(message),
        blocks=blocks,
    )
    if transcript is None and use_tuning:
        logger.warning(f"视觉模型 {vision_spec} 带调优参数调用失败，退回朴素调用重试一次")
        _params_rejected_specs.add(vision_spec)
        try:
            vision_model = _load_vision_model(vision_spec, use_tuning=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"加载视觉理解模型 {vision_spec} 失败: {exc}")
            return None
        transcript = await _transcribe_once(
            vision_model=vision_model,
            vision_spec=vision_spec,
            question=message_text(message),
            blocks=blocks,
        )
    if transcript is None:
        return None

    # 保留原文（含图片路径线索），只把图片块换成转述文本
    text_parts = [block for block in message.content_blocks if block.get("type") == "text"]
    return message.model_copy(
        update={
            "content": [
                *text_parts,
                {
                    "type": "text",
                    "text": (
                        f'<image_transcript source="{vision_spec}">\n'
                        f"以下内容由视觉模型转述，不是原图本身：\n{transcript}\n</image_transcript>"
                    ),
                },
            ]
        }
    )
