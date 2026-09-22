"""使用一次性 API Key 启动真实 CLI 页面，不读用户配置。"""

import json
import os
from pathlib import Path

from yuxi_cli.chat_web import ChatWebServer
from yuxi_cli.client import YuxiClient
from yuxi_cli.config import Remote

if __name__ == "__main__":
    config = json.loads((Path(os.environ["KB_E2E_STATE_DIR"]) / "client.json").read_text())
    client = YuxiClient(
        Remote(name="client-test", url=f"http://127.0.0.1:{os.environ['CLIENT_API_PORT']}", api_key=config["key"])
    )
    server = ChatWebServer(
        ("127.0.0.1", int(os.environ["CLIENT_CLI_PORT"])), client, "client-test", "synthetic-page-token"
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        client.close()
