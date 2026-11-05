"""确定性模型替身的协议拒绝条件。"""

import pytest

from test.support.openai_replay_server import attachment_contexts, validate_request


@pytest.mark.parametrize("mutation", ["none", "drop_input", "reorder", "move_image"])
def test_replay_requires_whole_ordered_multimodal_batch(mutation):
    """缺失整组输入、顺序改变或图片错位必须被模型 oracle 拒绝。"""
    users = []
    for input_index in range(2):
        for position in range(2):
            content = [
                {
                    "type": "text",
                    "text": (f"DETERMINISTIC_AGENT_E2E_OK DETERMINISTIC_ATTACHMENT_BATCH BATCH_INPUT_{input_index}_MESSAGE_{position}"),
                }
            ]
            if position == 0:
                content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64,test"}})
            users.append({"role": "user", "content": content})
    if mutation == "drop_input":
        users = users[2:]
    elif mutation == "reorder":
        users = users[2:] + users[:2]
    elif mutation == "move_image":
        users[1]["content"].append(users[0]["content"].pop())
    body = {
        "model": "deterministic-chat",
        "stream": True,
        "messages": [{"role": "system", "content": "# 图片生成技能"}, *users],
        "tools": [{"type": "function", "function": {"name": "present_artifacts"}}],
    }
    expected = {
        "none": None,
        "drop_input": "attachment_batch_messages_missing_or_reordered",
        "reorder": "attachment_batch_messages_missing_or_reordered",
        "move_image": "attachment_batch_image_position_mismatch",
    }
    assert validate_request("Bearer ci-replay-key", body) == expected[mutation]


@pytest.mark.parametrize(
    ("marker", "context", "error"),
    [
        ("none", "<attachment_context>batch-0.txt</attachment_context>", "attachment_on_non_anchor_message"),
        ("batch-0.txt", "", "attachment_anchor_missing"),
        ("batch-0.txt", "<attachment_context>batch-0.txt batch-1.txt</attachment_context>", "attachment_cross_input_anchor"),
        ("batch-0.txt", "<attachment_context>batch-0.txt</attachment_context>", None),
    ],
)
def test_replay_checks_each_input_attachment_anchor(marker, context, error):
    """独立模型 oracle 拒绝附件错挂到首条或其他 Input 的消息。"""
    body = {
        "model": "deterministic-chat",
        "stream": True,
        "messages": [
            {"role": "system", "content": "# 图片生成技能"},
            {"role": "user", "content": f"DETERMINISTIC_AGENT_E2E_OK DETERMINISTIC_ATTACHMENT_ANCHOR:{marker} {context}"},
        ],
        "tools": [{"type": "function", "function": {"name": "present_artifacts"}}],
    }
    assert validate_request("Bearer ci-replay-key", body) == error


@pytest.mark.timeout(3)
@pytest.mark.parametrize("kind", ["anchor", "absent"])
def test_replay_handles_many_unterminated_attachment_tags_with_bounded_work(kind):
    """大量未闭合标签不能让两个附件检查入口重复扫描整段文本。"""
    marker = "DETERMINISTIC_ATTACHMENT_ANCHOR:batch-0.txt" if kind == "anchor" else "DETERMINISTIC_ATTACHMENT_ABSENT:batch-1.txt"
    body = {
        "model": "deterministic-chat",
        "stream": True,
        "messages": [
            {"role": "system", "content": "# 图片生成技能"},
            {"role": "user", "content": f"DETERMINISTIC_AGENT_E2E_OK {marker} " + "<attachment_context>" * 25_000},
        ],
        "tools": [{"type": "function", "function": {"name": "present_artifacts"}}],
    }
    expected = "attachment_anchor_missing" if kind == "anchor" else "future_attachment_in_current_input"
    assert validate_request("Bearer ci-replay-key", body) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("plain", []),
        ("<attachment_context></attachment_context>", [""]),
        ("before<attachment_context>one\ntwo</attachment_context>after", ["one\ntwo"]),
        (
            "<attachment_context>outer<attachment_context>inner</attachment_context></attachment_context>",
            ["outer<attachment_context>inner"],
        ),
        ("<attachment_context>a</attachment_context><attachment_context>b</attachment_context><attachment_context>c", ["a", "b"]),
    ],
)
def test_replay_attachment_tag_extraction_preserves_protocol_cases(content, expected):
    """固定空、跨行、嵌套、多段和末段未闭合样本，保持 oracle 内容语义。"""
    assert attachment_contexts(content) == expected


@pytest.mark.parametrize(
    ("authorization", "change", "expected_error"),
    [
        (None, {}, "invalid_authorization"),
        ("Bearer ci-replay-key", {"model": "other-model"}, "invalid_model"),
        ("Bearer ci-replay-key", {"stream": False}, "stream_required"),
        ("Bearer ci-replay-key", {"messages": [{"role": "user", "content": "wrong"}]}, "expected_input_missing"),
        (
            "Bearer ci-replay-key",
            {"messages": [{"role": "user", "content": "DETERMINISTIC_AGENT_E2E_OK"}]},
            "preloaded_skill_missing",
        ),
        ("Bearer ci-replay-key", {"tools": []}, "preloaded_tool_missing"),
    ],
)
def test_replay_rejects_invalid_model_contract(authorization, change, expected_error):
    """模型替身必须拒绝未经过预期适配层的请求。"""
    body = {
        "model": "deterministic-chat",
        "stream": True,
        "messages": [
            {"role": "system", "content": "# 图片生成技能"},
            {"role": "user", "content": "DETERMINISTIC_AGENT_E2E_OK"},
        ],
        "tools": [{"type": "function", "function": {"name": "present_artifacts"}}],
    }
    assert validate_request(authorization, {**body, **change}) == expected_error


def test_replay_rejects_unexpected_tool_result():
    """工具结果与 replay 剧本不一致时拒绝继续。"""
    body = {
        "model": "deterministic-chat",
        "stream": True,
        "messages": [
            {"role": "system", "content": "# 图片生成技能"},
            {"role": "user", "content": "DETERMINISTIC_AGENT_E2E_OK"},
            {"role": "tool", "tool_call_id": "call-preloaded-tool", "content": "unexpected result"},
        ],
        "tools": [{"type": "function", "function": {"name": "present_artifacts"}}],
    }
    assert validate_request("Bearer ci-replay-key", body) == "tool_execution_result_missing"


def test_replay_rejects_changed_bound_root_for_prepared_run():
    """运行中根文件变化不能被确定性模型的成功响应掩盖。"""
    body = {
        "model": "deterministic-chat",
        "stream": True,
        "messages": [
            {"role": "system", "content": "# 图片生成技能\nBOUND_SKILL_bbbb"},
            {"role": "user", "content": "DETERMINISTIC_AGENT_E2E_OK DETERMINISTIC_BOUND_ROOT:aaaa"},
        ],
        "tools": [{"type": "function", "function": {"name": "present_artifacts"}}],
    }
    assert validate_request("Bearer ci-replay-key", body) == "bound_root_snapshot_mismatch"
    body["messages"][0]["content"] = "# 图片生成技能\nBOUND_SKILL_aaaa"
    assert validate_request("Bearer ci-replay-key", body) is None


def test_replay_requires_imported_mcp_tool_in_creation_flow():
    """创建时导入的 MCP 必须进入真实模型工具集合。"""
    body = {
        "model": "deterministic-chat",
        "stream": True,
        "messages": [
            {"role": "system", "content": "# 图片生成技能"},
            {"role": "user", "content": "DETERMINISTIC_AGENT_E2E_OK DETERMINISTIC_CREATE_MCP"},
        ],
        "tools": [{"type": "function", "function": {"name": "present_artifacts"}}],
    }
    assert validate_request("Bearer ci-replay-key", body) == "creation_mcp_tool_missing"
    body["tools"].append({"type": "function", "function": {"name": "effect_probe"}})
    assert validate_request("Bearer ci-replay-key", body) is None


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ("missing_tool", "office_parser_missing"),
        ("missing_text", "office_parser_result_missing"),
        ("missing_path", "office_parser_result_missing"),
        ("wrong_call", "office_parser_result_missing"),
        ("valid", None),
    ],
)
def test_office_replay_requires_real_parser_text_path_and_call_identity(mutation, expected):
    """Office 模型替身只接受真正解析结果，错误文本不能伪装成成功。"""
    import json

    body = {
        "model": "deterministic-chat",
        "stream": True,
        "messages": [
            {"role": "system", "content": "# 图片生成技能"},
            {"role": "user", "content": "DETERMINISTIC_AGENT_E2E_OK DETERMINISTIC_OFFICE_PATH:/home/user/workspace/report.docx"},
        ],
        "tools": [{"type": "function", "function": {"name": "ocr_parse_file"}}],
    }
    result = {"preview": "Office backend content 37", "parsed_path": "/home/user/workspace/outputs/ocr/result/document.md"}
    if mutation == "missing_tool":
        body["tools"] = []
    elif mutation == "missing_text":
        result["preview"] = "error"
    elif mutation == "missing_path":
        result["parsed_path"] = ""
    body["messages"].append(
        {"role": "tool", "tool_call_id": "wrong" if mutation == "wrong_call" else "call-office-parser", "content": json.dumps(result)}
    )
    assert validate_request("Bearer ci-replay-key", body) == expected


def test_office_replay_http_requests_parser_then_returns_verified_answer():
    """通过实际 HTTP 和 SSE 确认解析前后阶段，复用同一确定性协议。"""
    import httpx
    import json
    from test.support.openai_replay_server import ReplayHandler, serve_replay_handler

    body = {
        "model": "deterministic-chat",
        "stream": True,
        "messages": [
            {"role": "system", "content": "# 图片生成技能"},
            {"role": "user", "content": "DETERMINISTIC_AGENT_E2E_OK DETERMINISTIC_OFFICE_PATH:/home/user/workspace/report.docx"},
        ],
        "tools": [{"type": "function", "function": {"name": "ocr_parse_file"}}],
    }
    with serve_replay_handler(ReplayHandler, host="127.0.0.1", api_host="127.0.0.1") as url:
        with httpx.Client(base_url=url, trust_env=False) as client:
            response = client.post("/chat/completions", headers={"Authorization": "Bearer ci-replay-key"}, json=body)
            assert response.status_code == 200 and '"name": "ocr_parse_file"' in response.text
            body["messages"].append(
                {
                    "role": "tool",
                    "tool_call_id": "call-office-parser",
                    "content": json.dumps(
                        {
                            "preview": "Office backend content 37",
                            "parsed_path": "/home/user/workspace/outputs/ocr/report/document.md",
                        }
                    ),
                }
            )
            response = client.post("/chat/completions", headers={"Authorization": "Bearer ci-replay-key"}, json=body)
            assert response.status_code == 200 and "DETERMINISTIC_AGENT_E2E_OK" in response.text
