# ruff: noqa: RUF001, RUF002, RUF003 -- 中文说明保留自然标点。
"""以冻结后可执行入口启动 PageFerry FastAPI sidecar。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from core.settings import Settings
from main import create_app

_READY_EVENT = "ready"


def _build_parser() -> argparse.ArgumentParser:
    """创建只暴露本地发布 runtime 所需参数的 CLI parser。"""

    parser = argparse.ArgumentParser(prog="pageferry-backend")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="绑定 127.0.0.1 的端口；0 表示由系统分配。",
    )
    token_source = parser.add_mutually_exclusive_group(required=True)
    token_source.add_argument(
        "--boot-token",
        help=argparse.SUPPRESS,
    )
    token_source.add_argument(
        "--boot-token-stdin",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="PageFerry app-data 根目录。",
    )
    return parser


def _validated_port(value: int) -> int:
    """拒绝超出 TCP 端口范围的输入，同时允许 0 请求动态端口。"""

    if value < 0 or value > 65535:
        raise ValueError("port 必须位于 0 到 65535 之间")
    return value


def _bound_port(server: uvicorn.Server) -> int:
    """从 Uvicorn 已监听的 socket 读取系统实际分配的端口。"""

    for http_server in server.servers or ():
        for socket in http_server.sockets or ():
            address = socket.getsockname()
            if isinstance(address, tuple) and len(address) >= 2:
                return int(address[1])
    raise RuntimeError("sidecar 已启动但没有可用的监听 socket")


def _read_boot_token(arguments: argparse.Namespace) -> str:
    """从显式参数或 stdin 读取 token，并限制输入体积且不回显原值。"""

    raw_token = sys.stdin.readline(257) if arguments.boot_token_stdin else arguments.boot_token
    token = raw_token.strip()
    if len(token) < 32 or len(token) > 256:
        _build_parser().error("boot token 长度无效")
    return token


async def _serve(settings: Settings) -> None:
    """启动 Uvicorn，并在 lifespan 与监听 socket 都就绪后输出 handshake。"""

    configuration = uvicorn.Config(
        create_app(settings),
        host="127.0.0.1",
        port=settings.port,
        loop="asyncio",
        http="h11",
        ws="none",
        access_log=False,
        log_level="warning",
    )
    server = uvicorn.Server(configuration)
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        if server_task.done():
            await server_task
            raise RuntimeError("sidecar 在 ready handshake 前退出")
        await asyncio.sleep(0.01)

    # stdout 是 Rust 与冻结 sidecar 之间的窄协议。token 绝不能进入 handshake 或日志。
    print(
        json.dumps(
            {"event": _READY_EVENT, "port": _bound_port(server)},
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    await server_task


def main(argv: Sequence[str] | None = None) -> int:
    """解析安全启动参数并阻塞运行 sidecar，直到收到系统退出信号。"""

    arguments = _build_parser().parse_args(argv)
    try:
        port = _validated_port(arguments.port)
    except ValueError as error:
        _build_parser().error(str(error))

    token = _read_boot_token(arguments)

    # 冻结入口不读取启动目录中的 .env，避免从终端或 Finder 启动时被 cwd 配置劫持。
    settings = Settings(
        host="127.0.0.1",
        port=port,
        data_dir=arguments.data_dir,
        boot_token=token,
        debug=False,
        _env_file=None,
    )
    asyncio.run(_serve(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
