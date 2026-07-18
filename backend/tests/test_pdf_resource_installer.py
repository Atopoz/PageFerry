"""验证 PDF required resource packs 安装器的状态、并发、取消与完整性边界。"""

from __future__ import annotations

import errno
import hashlib
import io
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import Request

import pytest

import modules.pdf.resource_installer as installer_module
from modules.pdf.assets import PdfAssetManifest, parse_pdf_asset_manifest, pdf_asset_path
from modules.pdf.resource_installer import (
    PdfAssetSyncError,
    PdfResourceInstaller,
    PdfResourcesNotReadyError,
    PdfResourceStatus,
    sync_pdf_asset,
)


def _manifest(asset_contents: list[tuple[str, str, str, bytes]]) -> PdfAssetManifest:
    """用小型内存内容创建覆盖多个 pack 的严格 manifest。"""

    assets: list[dict[str, object]] = []
    for asset_id, pack, file_name, content in asset_contents:
        assets.append(
            {
                "asset_id": asset_id,
                "pack": pack,
                "relative_path": f"{pack}/{file_name}",
                "distribution_path": f"{pack}/{file_name}",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "fallback_urls": [f"https://github.example.test/releases/download/v1/{file_name}"],
                "upstream_url": f"https://upstream.example.test/{file_name}",
            }
        )
    return parse_pdf_asset_manifest(
        {
            "schema_version": 1,
            "pack_id": "test-pack",
            "pack_revision": "v1",
            "default_base_url": "https://cdn.example.test/pdf/v1/",
            "assets": assets,
        }
    )


def _wait_for_status(
    installer: PdfResourceInstaller,
    predicate: Callable[[PdfResourceStatus], bool],
    *,
    timeout: float = 2.0,
) -> PdfResourceStatus:
    """轮询短测试 worker, 直到状态满足断言或给出明确超时。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = installer.status()
        if predicate(status):
            return status
        time.sleep(0.005)
    raise AssertionError(f"PDF resource status did not converge: {installer.status()}")


class _BlockingResponse:
    """返回首段后阻塞下一次 read, 让测试稳定观察进度与取消。"""

    def __init__(self, content: bytes, first_read: Event, release: Event) -> None:
        """保存响应内容与两个线程同步事件。"""

        self._content = content
        self._first_read = first_read
        self._release = release
        self._offset = 0

    def __enter__(self) -> _BlockingResponse:
        """模拟 urlopen response context manager。"""

        return self

    def __exit__(self, *_args: object) -> None:
        """内存 fake 没有需要释放的系统资源。"""

    def read(self, _size: int) -> bytes:
        """第一次返回一个字节, 第二次等待测试放行。"""

        if self._offset == 0:
            self._offset = 1
            self._first_read.set()
            return self._content[:1]
        if self._offset < len(self._content):
            if not self._release.wait(timeout=2):
                raise TimeoutError("test did not release response")
            remaining = self._content[self._offset :]
            self._offset = len(self._content)
            return remaining
        return b""


def test_status_only_includes_default_required_packs_and_caches_checksums(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lifespan 不哈希, 首次公开状态校验后不重哈希, optional pack 不进入 contract。"""

    contents = [
        ("layout", "layout", "layout.onnx", b"layout"),
        ("common", "fonts-common-zh-cn", "common.ttf", b"common"),
        ("japanese", "fonts-ja", "ja.ttf", b"japanese"),
    ]
    manifest = _manifest(contents)
    pack_path = tmp_path / "pdf" / manifest.pack_revision
    for asset in manifest.assets:
        if asset.pack == "fonts-ja":
            continue
        destination = pdf_asset_path(pack_path, asset)
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected = next(
            content for asset_id, _, _, content in contents if asset_id == asset.asset_id
        )
        destination.write_bytes(expected)

    original_hash = installer_module.hash_pdf_asset
    hashed_paths: list[Path] = []

    def recording_hash(path: Path) -> tuple[str, int]:
        """记录真正读取完整文件的次数。"""

        hashed_paths.append(path)
        return original_hash(path)

    monkeypatch.setattr(installer_module, "hash_pdf_asset", recording_hash)
    installer = PdfResourceInstaller(manifest, pack_path)
    try:
        first = installer.initialize()
        second = installer.status()
        third = installer.status()
    finally:
        installer.close()

    assert first.state == "missing"
    assert second.state == third.state == "ready"
    assert [resource.pack for resource in second.resources] == [
        "layout",
        "fonts-common-zh-cn",
    ]
    assert len(hashed_paths) == 2


def test_ensure_ready_fails_closed_without_downloading_and_caches_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务 gate 必须校验同尺寸坏文件、拒绝继续, 且不能借机访问网络。"""

    expected = b"good"
    manifest = _manifest([("layout", "layout", "layout.onnx", expected)])
    pack_path = tmp_path / "pdf" / manifest.pack_revision
    destination = pdf_asset_path(pack_path, manifest.assets[0])
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"evil")
    hash_calls: list[Path] = []
    original_hash = installer_module.hash_pdf_asset

    def recording_hash(path: Path) -> tuple[str, int]:
        """记录任务 gate 的完整 checksum。"""

        hash_calls.append(path)
        return original_hash(path)

    def fail_open(_request: Request, *, timeout: float) -> io.BytesIO:
        """普通 runtime 校验不得隐式下载。"""

        raise AssertionError(timeout)

    monkeypatch.setattr(installer_module, "hash_pdf_asset", recording_hash)
    installer = PdfResourceInstaller(
        manifest,
        pack_path,
        required_packs=("layout",),
        open_url=fail_open,
    )
    try:
        assert installer.initialize().state == "missing"
        with pytest.raises(PdfResourcesNotReadyError) as caught:
            installer.ensure_ready()
        with pytest.raises(PdfResourcesNotReadyError):
            installer.ensure_ready()
    finally:
        installer.close()

    assert caught.value.unavailable_packs == ("layout",)
    assert hash_calls == [destination]


def test_install_reuses_cached_invalid_result_instead_of_rehashing_old_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次检查已判坏的同尺寸文件不应在每个下载候选前重复计算 checksum。"""

    expected = b"good"
    manifest = _manifest([("layout", "layout", "layout.onnx", expected)])
    pack_path = tmp_path / "pdf" / manifest.pack_revision
    destination = pdf_asset_path(pack_path, manifest.assets[0])
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"evil")
    original_hash = installer_module.hash_pdf_asset
    hash_calls: list[Path] = []

    def recording_hash(path: Path) -> tuple[str, int]:
        """记录旧目标文件的完整 checksum 次数。"""

        hash_calls.append(path)
        return original_hash(path)

    def successful_open(_request: Request, *, timeout: float) -> io.BytesIO:
        """返回通过 manifest 校验的新内容。"""

        assert timeout == 120.0
        return io.BytesIO(expected)

    monkeypatch.setattr(installer_module, "hash_pdf_asset", recording_hash)
    installer = PdfResourceInstaller(
        manifest,
        pack_path,
        required_packs=("layout",),
        open_url=successful_open,
    )
    try:
        assert installer.initialize().state == "missing"
        installer.start_install()
        assert _wait_for_status(installer, lambda status: status.state == "ready").state == "ready"
    finally:
        installer.close()

    assert hash_calls == [destination]
    assert destination.read_bytes() == expected


def test_install_is_single_worker_cancel_stays_cancelling_until_worker_stops(
    tmp_path: Path,
) -> None:
    """重复 install 不并发, cancel 在阻塞 read 返回前只报告 cancelling。"""

    expected = b"download-me"
    manifest = _manifest([("layout", "layout", "layout.onnx", expected)])
    first_read = Event()
    release = Event()
    open_calls = 0

    def blocking_open(_request: Request, *, timeout: float) -> _BlockingResponse:
        """只允许唯一 worker 创建一个阻塞响应。"""

        nonlocal open_calls
        assert timeout == 120.0
        open_calls += 1
        return _BlockingResponse(expected, first_read, release)

    installer = PdfResourceInstaller(
        manifest,
        tmp_path / "pdf" / manifest.pack_revision,
        required_packs=("layout",),
        open_url=blocking_open,
    )
    try:
        assert installer.start_install().state == "downloading"
        assert first_read.wait(timeout=1)
        assert installer.start_install().state == "downloading"
        cancel_status = installer.cancel()
        assert cancel_status.state == "cancelling"
        assert open_calls == 1
        release.set()
        final = _wait_for_status(installer, lambda status: status.state == "cancelled")
    finally:
        release.set()
        installer.close()

    assert final.completed_bytes == 0
    assert not pdf_asset_path(installer.pack_path, manifest.assets[0]).exists()


def test_task_gate_does_not_overwrite_active_download_state(tmp_path: Path) -> None:
    """下载中误入任务 gate 仍需 fail-closed, 但不能破坏进度轮询状态。"""

    expected = b"download-me"
    manifest = _manifest([("layout", "layout", "layout.onnx", expected)])
    first_read = Event()
    release = Event()

    def blocking_open(_request: Request, *, timeout: float) -> _BlockingResponse:
        """让显式安装稳定停在 downloading。"""

        assert timeout == 120.0
        return _BlockingResponse(expected, first_read, release)

    installer = PdfResourceInstaller(
        manifest,
        tmp_path / "pdf" / manifest.pack_revision,
        required_packs=("layout",),
        open_url=blocking_open,
    )
    try:
        assert installer.start_install().state == "downloading"
        assert first_read.wait(timeout=1)
        with pytest.raises(PdfResourcesNotReadyError):
            installer.ensure_ready()
        assert installer.status().state == "downloading"
        release.set()
        final = _wait_for_status(installer, lambda status: status.state == "ready")
    finally:
        release.set()
        installer.close()

    assert final.completed_bytes == len(expected)


def test_completed_asset_is_cached_before_next_download_without_rehash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """切换到下一资产前应立即计入完成字节, polling 不得重哈希刚落盘文件。"""

    first_content = b"first-asset"
    second_content = b"second-asset"
    manifest = _manifest(
        [
            ("first", "layout", "first.onnx", first_content),
            ("second", "fonts-common-zh-cn", "second.ttf", second_content),
        ]
    )
    second_opened = Event()
    release_second = Event()
    hash_calls: list[Path] = []

    original_hash = installer_module.hash_pdf_asset

    def recording_hash(path: Path) -> tuple[str, int]:
        """记录 checksum 调用并委托原实现。"""

        hash_calls.append(path)
        return original_hash(path)

    monkeypatch.setattr(installer_module, "hash_pdf_asset", recording_hash)

    def staged_open(request: Request, *, timeout: float) -> io.BytesIO:
        """首资产立即返回, 第二资产在 urlopen 阶段阻塞。"""

        assert timeout == 120.0
        if request.full_url.endswith("first.onnx"):
            return io.BytesIO(first_content)
        second_opened.set()
        if not release_second.wait(timeout=2):
            raise TimeoutError("test did not release second download")
        return io.BytesIO(second_content)

    installer = PdfResourceInstaller(
        manifest,
        tmp_path / "pdf" / manifest.pack_revision,
        open_url=staged_open,
    )
    try:
        installer.start_install()
        assert second_opened.wait(timeout=1)
        during_second = installer.status()
        assert during_second.completed_bytes == len(first_content)
        assert hash_calls == []
        release_second.set()
        final = _wait_for_status(installer, lambda status: status.state == "ready")
    finally:
        release_second.set()
        installer.close()

    assert final.completed_bytes == len(first_content) + len(second_content)
    assert hash_calls == []


@pytest.mark.parametrize(
    ("disk_usage", "expected_error"),
    [
        (lambda _path: SimpleNamespace(free=0), "insufficient_disk_space"),
        (
            lambda _path: (_ for _ in ()).throw(OSError("unreadable filesystem")),
            "filesystem_error",
        ),
    ],
)
def test_disk_check_uses_stable_and_truthful_error_codes(
    tmp_path: Path,
    disk_usage: Callable[[Path], object],
    expected_error: str,
) -> None:
    """磁盘不足与路径不可访问必须使用不同稳定错误码, 且不能启动网络。"""

    manifest = _manifest([("layout", "layout", "layout.onnx", b"layout")])

    def fail_open(_request: Request, *, timeout: float) -> io.BytesIO:
        """磁盘检查失败后不得访问任何下载源。"""

        assert timeout == 120.0
        raise AssertionError("network must not start")

    installer = PdfResourceInstaller(
        manifest,
        tmp_path / "pdf" / manifest.pack_revision,
        required_packs=("layout",),
        open_url=fail_open,
        disk_usage=disk_usage,  # type: ignore[arg-type]
    )
    try:
        status = installer.start_install()
    finally:
        installer.close()

    assert status.state == "failed"
    assert status.error_code == expected_error


def test_failed_download_can_retry_and_preserves_candidate_order(tmp_path: Path) -> None:
    """失败后再次 install 应重试, 并继续遵守 CDN、GitHub、upstream 顺序。"""

    expected = b"retry-content"
    manifest = _manifest([("layout", "layout", "layout.onnx", expected)])
    should_succeed = False
    requested_urls: list[str] = []

    def retrying_open(request: Request, *, timeout: float) -> io.BytesIO:
        """第一次让所有来源失败, 第二次只让 upstream 成功。"""

        assert timeout == 120.0
        requested_urls.append(request.full_url)
        if should_succeed and request.full_url.startswith("https://upstream.example.test/"):
            return io.BytesIO(expected)
        raise URLError("synthetic unavailable source")

    installer = PdfResourceInstaller(
        manifest,
        tmp_path / "pdf" / manifest.pack_revision,
        required_packs=("layout",),
        open_url=retrying_open,
    )
    try:
        installer.start_install()
        failed = _wait_for_status(installer, lambda status: status.state == "failed")
        should_succeed = True
        installer.start_install()
        ready = _wait_for_status(installer, lambda status: status.state == "ready")
    finally:
        installer.close()

    expected_order = [
        "https://cdn.example.test/pdf/v1/layout/layout.onnx",
        "https://github.example.test/releases/download/v1/layout.onnx",
        "https://upstream.example.test/layout.onnx",
    ]
    assert failed.error_code == "download_failed"
    assert requested_urls == expected_order + expected_order
    assert ready.completed_bytes == len(expected)


@pytest.mark.parametrize("network_error", [TimeoutError("timeout"), ConnectionError("reset")])
def test_network_os_errors_are_not_reported_as_filesystem_failures(
    tmp_path: Path,
    network_error: OSError,
) -> None:
    """connect/read 产生的 OSError 子类必须保持 download_failed 语义。"""

    expected = b"network-content"
    manifest = _manifest([("layout", "layout", "layout.onnx", expected)])

    def failed_open(_request: Request, *, timeout: float) -> io.BytesIO:
        """模拟网络层 timeout 或 connection reset。"""

        assert timeout == 120.0
        raise network_error

    with pytest.raises(PdfAssetSyncError) as caught:
        sync_pdf_asset(
            manifest.assets[0],
            url="https://cdn.example.test/pdf/v1/layout/layout.onnx",
            destination=tmp_path / "layout.onnx",
            open_url=failed_open,
        )

    assert caught.value.error_code == "download_failed"


def test_enospc_during_download_keeps_insufficient_disk_space_error(tmp_path: Path) -> None:
    """preflight 之后磁盘写满仍必须给 UI 可操作的容量错误。"""

    expected = b"disk-content"
    manifest = _manifest([("layout", "layout", "layout.onnx", expected)])

    def disk_full_open(_request: Request, *, timeout: float) -> io.BytesIO:
        """模拟下载阶段底层文件系统返回 ENOSPC。"""

        assert timeout == 120.0
        raise OSError(errno.ENOSPC, "synthetic disk full")

    with pytest.raises(PdfAssetSyncError) as caught:
        sync_pdf_asset(
            manifest.assets[0],
            url="https://cdn.example.test/pdf/v1/layout/layout.onnx",
            destination=tmp_path / "layout.onnx",
            open_url=disk_full_open,
        )

    assert caught.value.error_code == "insufficient_disk_space"


def test_close_is_bounded_when_urlopen_is_stuck(tmp_path: Path) -> None:
    """卡在 connect 的 daemon worker 不得让 sidecar shutdown 等满请求 timeout。"""

    expected = b"stuck"
    manifest = _manifest([("layout", "layout", "layout.onnx", expected)])
    open_started = Event()
    release = Event()

    def stuck_open(_request: Request, *, timeout: float) -> io.BytesIO:
        """模拟无法被 cancel Event 立即打断的底层 connect。"""

        assert timeout == 120.0
        open_started.set()
        if not release.wait(timeout=2):
            raise TimeoutError("test cleanup did not release urlopen")
        return io.BytesIO(expected)

    installer = PdfResourceInstaller(
        manifest,
        tmp_path / "pdf" / manifest.pack_revision,
        required_packs=("layout",),
        open_url=stuck_open,
        close_timeout=0.01,
    )
    installer.start_install()
    assert open_started.wait(timeout=1)
    started = time.monotonic()
    installer.close()
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.2


def test_new_installer_defers_recovery_checksum_until_status_or_task_gate(tmp_path: Path) -> None:
    """新 installer 的 lifespan 保持轻量, 公开状态或任务 gate 再恢复 ready。"""

    expected = b"already-installed"
    manifest = _manifest([("layout", "layout", "layout.onnx", expected)])
    pack_path = tmp_path / "pdf" / manifest.pack_revision
    destination = pdf_asset_path(pack_path, manifest.assets[0])
    destination.parent.mkdir(parents=True)
    destination.write_bytes(expected)

    first = PdfResourceInstaller(manifest, pack_path, required_packs=("layout",))
    second = PdfResourceInstaller(manifest, pack_path, required_packs=("layout",))
    try:
        assert first.initialize().state == "missing"
        assert second.initialize().state == "missing"
        assert first.status().state == "ready"
        assert second.ensure_ready().state == "ready"
    finally:
        first.close()
        second.close()
