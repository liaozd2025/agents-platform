"""在启动 AIO 前关闭静态后缀免认证，避免查询参数绕过文件保护。"""

import os
from pathlib import Path

template = Path("/opt/gem/nginx-server-with-auth.conf")
prefix, marker, rest = template.read_text().partition(
    'map "$request_method:$request_uri" $target_workflow {'
)
body, closing, suffix = rest.partition("\n}")
if not marker or not closing or "@proxy_with_auth" not in body:
    raise RuntimeError("unsupported Sandbox authentication template")
template.write_text(prefix + marker + "\n    default @proxy_with_auth;\n}" + suffix)
os.execv("/opt/gem/run.sh", ["/opt/gem/run.sh"])
