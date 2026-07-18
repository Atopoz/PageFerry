# ruff: noqa: RUF002, RUF003 -- 中文测试说明保留自然标点。
"""验证 production sidecar 的动态端口 handshake 与退出 contract。"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen


def test_sidecar_announces_dynamic_port_and_serves_health(tmp_path: Path) -> None:
    """sidecar 必须在 lifespan ready 后回报端口，且 handshake 不泄漏 token。"""

    token = "sidecar-test-token-0123456789abcdef"
    process = subprocess.Popen(
        [
            sys.executable,
            "sidecar.py",
            "--port",
            "0",
            "--boot-token-stdin",
            "--data-dir",
            str(tmp_path / "app-data"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdin is not None
        process.stdin.write(f"{token}\n")
        process.stdin.close()
        assert process.stdout is not None
        with ThreadPoolExecutor(max_workers=1) as executor:
            line = executor.submit(process.stdout.readline).result(timeout=20)
        handshake = json.loads(line)
        assert handshake["event"] == "ready"
        assert isinstance(handshake["port"], int)
        assert handshake["port"] > 0
        assert token not in line

        with urlopen(
            f"http://127.0.0.1:{handshake['port']}/healthz",
            timeout=5,
        ) as response:
            payload = json.load(response)
        assert payload["data"]["service"] == "pageferry-api"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    # Uvicorn 完成 lifespan shutdown 后会重新抛出捕获到的 SIGTERM，保留真实退出原因。
    assert process.returncode in {0, -signal.SIGTERM}
    assert process.stderr is not None
    assert token not in process.stderr.read()


def test_sidecar_rejects_short_boot_token_without_echoing_it(tmp_path: Path) -> None:
    """冻结入口不能在弱 token 下启动，也不能把 token 回显到错误输出。"""

    token = "too-short"
    completed = subprocess.run(
        [
            sys.executable,
            "sidecar.py",
            "--boot-token",
            token,
            "--data-dir",
            str(tmp_path / "app-data"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert completed.returncode != 0
    assert token not in completed.stdout
    assert token not in completed.stderr
