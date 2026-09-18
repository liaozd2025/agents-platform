"""PI HTTP E2E 的受控模型上游；文件和命令仍由真实 PI 工具执行。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CASE = re.compile(r"YUXI_PI_HTTP:([0-9a-f]{32}):(artifact|cancel|steer|error|report)")
OBSERVATIONS: dict[str, list[dict]] = {}
LOCK = threading.Lock()


def plan_response(request: dict) -> tuple[str, str, dict | str]:
    """按实际模型输入与工具结果选择固定响应，拒绝跳过真实工具的路径。"""
    if request.get("model") == "pi-delegation-controlled" and request.get("stream") is True:
        response = plan_delegation_response(request)
        nonce, stage = re.findall(r"PI_DELEGATION:([a-f0-9]+):([ABCS])", json.dumps(request["messages"]))[-1]
        return nonce, f"delegation-{stage}-{len(request['messages'])}", response
    if request.get("model") != "pi-http-controlled" or request.get("stream") is not True:
        raise ValueError("invalid_model_or_stream")
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages_required")
    user_texts = [str(message.get("content", "")) for message in messages if message.get("role") == "user"]
    match = next((match for text in reversed(user_texts) if (match := CASE.search(text))), None)
    if match is None:
        raise ValueError("case_marker_missing")
    nonce, scenario = match.groups()
    names = {tool.get("function", {}).get("name") for tool in request.get("tools", [])}
    task_index = next(
        index
        for index in range(len(messages) - 1, -1, -1)
        if messages[index].get("role") == "user" and CASE.search(str(messages[index].get("content", "")))
    )
    tool_messages = [message for message in messages[task_index + 1 :] if message.get("role") == "tool"]

    if "pi_sandbox" in names:
        if not tool_messages:
            arguments = {"description": user_texts[-1]}
            if "YUXI_PI_EXPECT_HISTORY:" in user_texts[-1]:
                arguments["continue_session"] = True
            return nonce, "parent_delegate", {"name": "pi_sandbox", "args": arguments}
        if scenario == "error":
            assert "执行状态: failed" in str(tool_messages[-1]), "provider failure was not returned to parent"
            assert "content_filter" in str(tool_messages[-1]), "provider failure reason was lost"
            return nonce, "parent_failure", "PI_HTTP_FAILURE_REPORTED"
        if scenario == "report":
            content = str(tool_messages[-1].get("content", ""))
            if "PI Run:" in content:
                assert "阶段状态: completed" in content, "structured stage status missing"
                path = re.search(r"/home/gem/user-data/[^\s]+/report\.json", content)
                assert path, "report evidence was not delivered"
                return nonce, "parent_verify", {"name": "read_file", "args": {"file_path": path[0]}}
            expected = re.search(r"YUXI_PI_REPORT_EXPECT:(\d+):(\d+):(true|false):(\d+)", user_texts[-1])
            assert expected, "independent report expectation missing"
            report = json.loads(content[content.index("{") : content.rindex("}") + 1])
            assert report["row_count"] == int(expected[1]) and report["displayed_row_count"] == int(expected[2]), report
            assert report["preview_truncated"] is (expected[3] == "true") and report["truncated"] is False, report
            assert report["net_sales"] == expected[4], report
            assert "More lines remain" not in content, "report evidence was truncated"
            return nonce, "parent_complete", f"PI_REPORT_VERIFIED: {expected[1]} records, net sales {expected[4]}"
        if scenario != "artifact" or "PI_HTTP_CHILD_OK" not in json.dumps(tool_messages, ensure_ascii=False):
            raise ValueError("child_result_missing")
        if "large.bin" not in json.dumps(tool_messages):
            raise ValueError("child_artifact_missing")
        return nonce, "parent_complete", "PI_HTTP_ROOT_OK"

    if not {"bash", "submit_artifact"} <= names:
        raise ValueError("pi_tools_missing")
    prompt = user_texts[-1]
    expected_history = re.search(r"YUXI_PI_EXPECT_HISTORY:([0-9a-f]{32})", prompt)
    if expected_history and f"YUXI_PI_HTTP:{expected_history[1]}:" not in json.dumps(messages[:task_index]):
        raise ValueError("previous_pi_session_missing")
    project_match = re.search(r"Project workspace is (/home/gem/user-data/projects/[0-9a-f-]+)\.", prompt)
    output_match = re.search(r"Put every generated deliverable under (/[^\s]+)\. ", prompt)
    if not project_match or not output_match:
        raise ValueError("assigned_directories_missing")
    project, output = project_match[1], output_match[1]
    if not re.fullmatch(re.escape(project) + r"/outputs/pi-runs/[0-9a-f]{24}", output):
        raise ValueError("assigned_directory_not_attempt_local")

    if scenario == "error":
        return nonce, "pi_error", "不可交付的部分答案"
    if scenario == "report":
        if not tool_messages:
            script = (
                "from __future__ import annotations\n"
                "import ast,csv,json,re\nfrom decimal import Decimal\n"
                "from pathlib import Path\nfrom types import SimpleNamespace\n"
                f"rows=list(csv.DictReader(Path({project + '/uploads/sales.csv'!r}).open()))\n"
                f"source=Path({project + '/uploads/mysql_reporter_query.py'!r})\n"
                "names={'MySQLSecurityChecker','limit_result_size','format_query_result','run_query'}\n"
                "tree=ast.parse(source.read_text())\n"
                "tree.body=[node for node in tree.body "
                "if isinstance(node,(ast.ClassDef,ast.FunctionDef)) and node.name in names]\n"
                "scope={'re':re,'json':json,'Path':Path,'Any':object,"
                "'load_mysql_config':lambda:{},"
                "'create_connection':lambda config:SimpleNamespace(open=True,close=lambda:None),"
                "'execute_query_with_timeout':lambda *args,**kwargs:rows}\n"
                "exec(compile(tree,str(source),'exec'),scope)\n"
                f"out=Path({output!r})\n"
                "preview=scope['run_query']('SELECT sales, returns, detail FROM fixture_sales',"
                "60,str(out/'full-results.json'))\n"
                "(out/'query-preview.txt').write_text(preview)\n"
                "preview_facts=json.loads(preview.splitlines()[0])\n"
                "full=json.loads((out/'full-results.json').read_text())\n"
                "gross=sum(Decimal(row['sales']) for row in full['rows'])\n"
                "returns=sum(Decimal(row['returns']) for row in full['rows'])\n"
                "result={'row_count':full['row_count'],'displayed_row_count':preview_facts['displayed_row_count'],"
                "'preview_truncated':preview_facts['truncated'],'truncated':full['truncated'],'gross_sales':str(gross),"
                "'returns':str(returns),'net_sales':str(gross-returns)}\n"
                "(out/'report.json').write_text(json.dumps(result))\n"
                "with (out/'report.csv').open('w') as stream:\n"
                " writer=csv.DictWriter(stream,fieldnames=result.keys());writer.writeheader();writer.writerow(result)\n"
                "print(preview)\nprint('PI_REPORT_RAW\\x00OUTPUT')\n"
            )
            return nonce, "pi_report_execute", {"name": "bash", "args": {"command": f"python -c {shlex.quote(script)}"}}
        assert "PI_REPORT_RAW" in str(tool_messages), "real data calculation missing"
        for filename in ("report.json", "report.csv", "full-results.json"):
            if f"已登记 {filename}" not in str(tool_messages):
                return (
                    nonce,
                    f"pi_report_submit_{filename}",
                    {
                        "name": "submit_artifact",
                        "args": {
                            "path": filename,
                            "stage_status": "completed",
                            "checks": ["生产查询函数完整导出固定 SQL 结果，核对预览截断及全量销售退货合计"],
                            "unresolved_items": [],
                        },
                    },
                )
        return nonce, "pi_report_complete", "报告已生成，完整性与合计见 report.json。"

    if not tool_messages:
        if scenario == "artifact":
            command = (
                f"pwd > cwd.txt; mkdir -p node_modules {shlex.quote(output + '/cache')}; "
                "for n in $(seq 1 220); do echo dependency > node_modules/$n; "
                f"echo cache > {shlex.quote(output + '/cache')}/$n; done; "
                f"head -c 9437184 /dev/zero > {shlex.quote(output + '/large.bin')}; "
                "printf 'PI_HTTP_CREATE_OK\\n'"
            )
        elif scenario == "steer":
            script = (
                "import time\nfrom pathlib import Path\n"
                "Path('steer-started').write_text('started')\n"
                "print('PI_HTTP_STEER_RUNNING',flush=True)\n"
                "for _ in range(300):\n"
                " if Path('steer-release').exists(): break\n"
                " time.sleep(.1)\n"
                "else: raise RuntimeError('steer release timed out')\n"
                "Path('steer-finished').write_text('finished')\n"
                "print('PI_HTTP_STEER_FINISHED',flush=True)\n"
            )
            command = f"python -u -c {shlex.quote(script)}"
        else:
            script = (
                "import os,time\nfrom pathlib import Path\n"
                "Path('cancel-writer.pid').write_text(str(os.getpid()))\n"
                "with open('cancel-growth.log','ab',buffering=0) as output:\n"
                " while True:\n  output.write(b'tick\\n')\n  print('PI_HTTP_WRITING',flush=True)\n  time.sleep(.1)\n"
            )
            command = f"python -u -c {shlex.quote(script)}"
        return nonce, "pi_execute", {"name": "bash", "args": {"command": command}}

    if scenario in {"cancel", "steer"}:
        raise ValueError("stopped_task_unexpectedly_called_model_again")
    results = json.dumps(tool_messages, ensure_ascii=False)
    if "PI_HTTP_CREATE_OK" not in results:
        raise ValueError("real_bash_result_missing")
    submitted = any(message.get("tool_call_id", "").startswith("pi_submit-") for message in tool_messages)
    if not submitted:
        return nonce, "pi_submit", {"name": "submit_artifact", "args": {"path": "large.bin"}}
    if "已登记 large.bin (9437184 bytes)" not in results:
        raise ValueError("real_submit_result_missing")
    return nonce, "pi_complete", "PI_HTTP_CHILD_OK"


def plan_delegation_response(body):
    """按调用方和已发生的工具结果选择下一步，拒绝错误历史。"""
    messages = body["messages"]
    user = next(item["content"] for item in reversed(messages) if item["role"] == "user")
    if not isinstance(user, str):
        user = "\n".join(part.get("text", "") for part in user)
    nonce, stage = re.findall(r"PI_DELEGATION:([a-f0-9]+):([ABCS])", user)[-1]
    names = {item["function"]["name"] for item in body.get("tools", [])}
    parent = "pi_sandbox" in names
    last = messages[-1]
    if parent:
        question_calls = {
            call["id"]
            for message in messages
            for call in message.get("tool_calls", [])
            if call["function"]["name"] == "ask_user_question"
        }
        if last["role"] == "user":
            arguments = {"description": f"PI_DELEGATION:{nonce}:{stage} 完成阶段并交付核验文本。"}
            if "MISSING_EVIDENCE" in user:
                arguments["description"] += " MISSING_EVIDENCE"
            if stage == "C":
                sources = [
                    re.search(r"PI Run: ([\w-]+)", str(item.get("content", "")))
                    for item in messages
                    if item["role"] == "tool"
                ]
                sources = [match.group(1) for match in sources if match]
                assert sources, "parent cannot identify original PI run from its tool results"
                arguments["source_run_id"] = sources[-2]
            return {"name": "pi_sandbox", "args": arguments}
        content = str(last.get("content", ""))
        if stage == "S" and "缺少申请科室" in content:
            return {"name": "ask_user_question", "args": {"questions": [{"question": "请补充申请科室"}]}}
        if stage == "S" and last.get("tool_call_id") in question_calls:
            answer = json.loads(content)["answer"]["answer"]
            sources = re.findall(r"PI Run: ([\w-]+)", json.dumps(messages))
            return {
                "name": "pi_sandbox",
                "args": {
                    "description": f"PI_DELEGATION:{nonce}:S SUPPLIED_VALUE={answer}",
                    "source_run_id": sources[-1],
                },
            }
        if "PI Run:" in content:
            paths = re.findall(r"/home/gem/user-data/[^\s]+/verification\.txt", content)
            assert paths, "PI result omitted verification file"
            return {"name": "read_file", "args": {"file_path": paths[-1]}}
        if "MISSING_VALUE" in content:
            sources = re.findall(r"PI Run: ([\w-]+)", json.dumps(messages))
            return {
                "name": "pi_sandbox",
                "args": {
                    "description": f"PI_DELEGATION:{nonce}:{stage} REPAIR 补齐验收缺项。",
                    "source_run_id": sources[-1],
                },
            }
        if stage == "S":
            answers = [
                json.loads(item["content"])["answer"]["answer"]
                for item in messages
                if item.get("role") == "tool" and item.get("tool_call_id") in question_calls
            ]
            assert answers and f"申请科室：{answers[-1]}" in content, "supplement not delivered"
        else:
            assert f"{stage}: verified" in content, "parent must read actual verification bytes"
        assert "More lines remain" not in content, "complete verification file was reported as truncated"
        return f"阶段 {stage} 已读取验收文件并核对。"

    # 新任务的最后一条 user 消息之前才是旧历史，避免把本次多轮误算成继承。
    latest_user = max(index for index, item in enumerate(messages) if item["role"] == "user")
    prior = json.dumps(messages[:latest_user], ensure_ascii=False)
    if stage == "B":
        assert f"PI_DELEGATION:{nonce}:A" not in prior, "independent check inherited editing history"
    if stage == "C":
        assert f"PI_DELEGATION:{nonce}:A" in prior, "correction lost original editing history"
        assert f"PI_DELEGATION:{nonce}:B" not in prior, "correction inherited the independent check"
    supplied = re.search(r"SUPPLIED_VALUE=([\w-]+)", user)
    if stage == "S":
        if supplied is None:
            if last["role"] == "user":
                return {
                    "name": "submit_artifact",
                    "args": {"stage_status": "needs_input", "checks": [], "unresolved_items": ["缺少申请科室"]},
                }
            return "阶段未完成：缺少申请科室，请主智能体向用户补充。"
        assert "缺少申请科室" in prior, "supplement lost the incomplete stage history"
    output_root = re.search(r"Put every generated deliverable under ([^\n]+?)\. ", user).group(1)
    if last["role"] == "user":
        value = "MISSING_VALUE" if "MISSING_EVIDENCE" in user and "REPAIR" not in user else f"{stage}: verified"
        if supplied:
            value = f"申请科室：{supplied[1]}"
        return {"name": "write", "args": {"path": f"{output_root}/verification.txt", "content": f"{value}\n{nonce}\n"}}
    if '"name": "submit_artifact"' not in json.dumps(messages[latest_user:]):
        return {
            "name": "submit_artifact",
            "args": {
                "path": "verification.txt",
                "stage_status": "completed",
                "checks": ["已写入阶段核验文件"],
                "unresolved_items": [],
            },
        }
    return f"阶段 {stage} 完成；验收依据 verification.txt；未解决项：无。"


class ReplayHandler(BaseHTTPRequestHandler):
    """只提供测试模型、健康及当前用例的脱敏观察记录。"""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        """返回测试上游健康与指定用例的阶段记录。"""
        if self.path == "/health":
            self.write_json(200, {"status": "ok", "service": "pi-http-replay"})
        elif re.fullmatch(r"/observations/[0-9a-f]{32}", self.path):
            with LOCK:
                records = list(OBSERVATIONS.get(self.path.rsplit("/", 1)[1], []))
            self.write_json(200, {"records": records})
        else:
            self.write_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        """验证模型请求并发送固定工具或文本响应。"""
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.write_json(404, {"error": "not_found"})
            return
        if self.headers.get("authorization") != "Bearer pi-http-replay-key":
            self.write_json(422, {"error": "invalid_authorization"})
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            if not 0 < length <= 2 * 1024 * 1024:
                raise ValueError("invalid_body_size")
            body = json.loads(self.rfile.read(length))
            nonce, phase, response = plan_response(body)
        except (AssertionError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self.write_json(422, {"error": str(exc)})
            return
        with LOCK:
            records = OBSERVATIONS.setdefault(nonce, [])
            if len(records) >= (64 if body["model"] == "pi-delegation-controlled" else 20):
                self.write_json(422, {"error": "unexpected_model_loop"})
                return
            records.append({"phase": phase})
        common = {
            "id": f"pi-http-{nonce}",
            "object": "chat.completion.chunk",
            "model": body["model"],
            "created": int(time.time()),
        }
        if isinstance(response, str):
            delta, finish = {"content": response}, "content_filter" if phase == "pi_error" else "stop"
        else:
            delta, finish = (
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"{phase}-{nonce}",
                            "type": "function",
                            "function": {"name": response["name"], "arguments": json.dumps(response["args"])},
                        }
                    ]
                },
                "tool_calls",
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]},
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            },
        ]
        if phase == "pi_execute":
            chunks.insert(
                0, {"choices": [{"index": 0, "delta": {"content": "PI_HTTP_WORKING"}, "finish_reason": None}]}
            )
        try:
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps({**common, **chunk})}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def write_json(self, status: int, payload: dict) -> None:
        """输出有长度的JSON响应，避免测试客户端等待连接关闭。"""
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        """测试日志不保存消息正文、请求头或认证信息。"""


def main() -> None:
    """在专用测试进程启动标准库HTTP服务。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--pid-file", type=Path)
    args = parser.parse_args()
    if args.pid_file:
        args.pid_file.write_text(str(os.getpid()), encoding="ascii")
    ThreadingHTTPServer(("0.0.0.0", args.port), ReplayHandler).serve_forever()


if __name__ == "__main__":
    main()
