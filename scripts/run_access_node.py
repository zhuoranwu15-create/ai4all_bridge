"""节点 agent 进程入口(node-only 远程机,如 aliyun2)。

起三件事:exec app(uvicorn,主线程)+ 出站 pull 循环 + 心跳循环(后台线程)。
**不调 init_db**:节点不碰 SQLite,一律走 HTTP 与中心通信。

启动前置(.env,见 runbook B Part 2):AI4ALL_ROLE=node、NODE_ID、CENTRAL_URL、
NODE_BASE_URL、AI4ALL_BRIDGE_SECRET(须与中心一致)。
"""
import logging
import signal
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from app import openclaw_gateway  # noqa: E402
from app.config import settings  # noqa: E402
from app.node_agent import (  # noqa: E402
    create_node_agent_app,
    run_heartbeat_loop,
    run_pull_loop,
)


logger = logging.getLogger("ai4all.run_access_node")


def main() -> None:
    # 节点进程默认无 handler,ai4all.node_agent 的 INFO 会被丢弃(只有 uvicorn 自带 logger 输出)。
    # 配置 root handler 使应用日志(含 node_send_text 计时)落 journald。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not settings.has_node_role:
        print(
            "AI4ALL_ROLE 不含 node 能力(当前=%s);本进程仅用于 node-only 机,退出。"
            % (settings.ai4all_role,),
            flush=True,
        )
        return
    if not (settings.node_id or "").strip():
        print("NODE_ID 必填(本机全局唯一,如 aliyun2);退出。", flush=True)
        return
    if not (settings.central_url or "").strip():
        print("CENTRAL_URL 必填(节点→中心稳定指纹地址);退出。", flush=True)
        return

    stop_event = threading.Event()

    pull_thread = threading.Thread(
        target=run_pull_loop, kwargs={"stop_event": stop_event}, daemon=True
    )
    heartbeat_thread = threading.Thread(
        target=run_heartbeat_loop, kwargs={"stop_event": stop_event}, daemon=True
    )
    pull_thread.start()
    heartbeat_thread.start()

    app = create_node_agent_app()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.node_agent_host,
            port=settings.node_agent_port,
            log_level="info",
        )
    )

    def _stop(_signum, _frame) -> None:
        stop_event.set()
        server.should_exit = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        server.run()  # 阻塞至 should_exit
    finally:
        stop_event.set()  # 通知后台循环收敛(daemon 线程随进程退出)
        try:
            openclaw_gateway.close_persistent_gateway_client()
        except Exception as err:
            logger.warning("persistent OpenClaw Gateway close failed: %s", err)


if __name__ == "__main__":
    main()
