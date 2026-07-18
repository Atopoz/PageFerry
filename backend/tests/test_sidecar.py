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
    terminated_by_test = False
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
        if process.poll() is None:
            terminated_by_test = True
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    # POSIX 上 Uvicorn 会保留 SIGTERM；Windows 的 Popen.terminate 则直接使用
    # TerminateProcess，并按 Python contract 把退出码设为 1。
    expected_return_codes = {0, -signal.SIGTERM}
    if sys.platform == "win32" and terminated_by_test:
        expected_return_codes.add(1)
    assert process.returncode in expected_return_codes
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
