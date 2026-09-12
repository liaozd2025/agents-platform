"""修正锁定基础镜像的stdin路由，并只读验证prebuilt镜像具有同一实现。"""

import ast
import sys
from pathlib import Path

path = Path("/opt/python3.12/lib/python3.12/site-packages/app/api/v1/shell.py")
source = path.read_text(encoding="utf-8")
old = """        # OpenHands 基于命令执行，将输入作为命令执行
        command = request.input

        # 异步执行命令
        await terminal_manager.execute_command(request.id, command, async_mode=True)"""
call = "asyncio.to_thread(session.bash_session.pane.send_keys, request.input, enter=request.press_enter)"
if "--check" not in sys.argv:
    if source.count(old) != 1:
        raise RuntimeError("PI shell stdin补丁与基础镜像不匹配")
    source = source.replace(old, f"        # 向当前前台进程输入，不创建新命令或覆盖会话状态。\n        await {call}")
    path.write_text(source, encoding="utf-8")

handler = next(
    node
    for node in ast.parse(source).body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "write_to_process"
)
awaited = [ast.unparse(node.value) for node in ast.walk(handler) if isinstance(node, ast.Await)]
if awaited != [call]:
    raise RuntimeError("PI image runtime_mismatch: shell_stdin（write路由必须直接输入当前PTY）")
