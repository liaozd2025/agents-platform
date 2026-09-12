"""检查部署后的 JavaScript 模块响应，防止预览 worker 被当作二进制文件。"""

import sys
from urllib.request import urlopen

if len(sys.argv) != 2:
    raise SystemExit("用法: python3 scripts/check_module_mime.py <模块 URL>")

with urlopen(sys.argv[1], timeout=15) as response:
    media_type = response.headers.get_content_type()
    if response.status != 200 or media_type not in {"text/javascript", "application/javascript"}:
        raise SystemExit(f"FAIL: HTTP {response.status}, Content-Type: {media_type}")
    print(f"PASS: HTTP {response.status}, Content-Type: {media_type}")
