"""安装、校验并缓存 PageFerry PDF 大型资源包的运行状态。"""

from __future__ import annotations

import errno
import hashlib
import logging
import math
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from threading import Event, RLock, Thread
from typing import BinaryIO, Literal, Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen

from .assets import (
    PdfAsset,
    PdfAssetManifest,
    hash_pdf_asset,
    is_pdf_asset_valid,
    pdf_asset_download_urls,
    pdf_asset_path,
    select_pdf_assets,
)

PDF_RESOURCE_CHUNK_SIZE = 1024 * 1024
DEFAULT_REQUIRED_PDF_RESOURCE_PACKS = ("layout", "fonts-common-zh-cn")
PDF_RESOURCE_DISK_FULL_ERRNOS = {
    errno.ENOSPC,
    getattr(errno, "EDQUOT", errno.ENOSPC),
}

logger = logging.getLogger(__name__)

type PdfResourceState = Literal[
    "ready",
    "missing",
    "downloading",
    "cancelling",
    "failed",
    "cancelled",
]
type PdfResourceErrorCode = Literal[
    "insufficient_disk_space",
    "download_failed",
    "integrity_check_failed",
    "filesystem_error",
]
type ProgressCallback = Callable[[str, int], None]
type AssetCompletedCallback = Callable[[PdfAsset, Path, bool], None]
type CancelCheck = Callable[[], bool]
type FileReplacer = Callable[[str | Path, str | Path], None]


class DiskUsage(Protocol):
    """描述磁盘空间检查只需要读取的 free bytes。"""

    free: int


class UrlOpener(Protocol):
    """描述 urllib opener 只允许以 keyword 传入 timeout 的调用边界。"""

    def __call__(self, request: Request, *, timeout: float) -> AbstractContextManager[BinaryIO]:
        """打开一个流式 HTTP response。"""

        ...


class PdfAssetSyncError(RuntimeError):
    """表示 PDF 资源下载、校验或原子落盘失败。"""

    def __init__(
        self,
        message: str,
        error_code: PdfResourceErrorCode = "download_failed",
    ) -> None:
        """保存可供 UI 稳定判断的错误码, 不暴露下载 URL。"""

        super().__init__(message)
        self.error_code = error_code


class PdfAssetDownloadCancelled(PdfAssetSyncError):
    """表示用户在资源完成原子落盘前取消了下载。"""

    def __init__(self) -> None:
        """构造不需要映射为 failed 状态的内部取消信号。"""

        super().__init__("PDF 资源下载已取消", "download_failed")


@dataclass(frozen=True, slots=True)
class PdfAssetSyncResult:
    """记录一个资源最终路径及本次是否实际下载。"""

    asset_id: str
    path: Path
    downloaded: bool


@dataclass(frozen=True, slots=True)
class PdfResourcePackStatus:
    """描述一个 required pack 的安装进度。"""

    pack: str
    size_bytes: int
    completed_bytes: int
    ready: bool


@dataclass(frozen=True, slots=True)
class PdfResourceStatus:
    """描述 frontend 轮询所需且不含本地路径或远端 URL 的资源状态。"""

    pack_revision: str
    state: PdfResourceState
    total_bytes: int
    completed_bytes: int
    current_asset_id: str | None
    error_code: PdfResourceErrorCode | None
    resources: tuple[PdfResourcePackStatus, ...]


@dataclass(frozen=True, slots=True)
class _AssetFileState:
    """缓存一个文件的轻量 stat 指纹与 checksum 结论。"""

    signature: tuple[int, int, int, int, int]
    # None 表示启动时只见过 stat、尚未读取完整文件; 它不能被当成 ready。
    valid: bool | None


class PdfResourcesNotReadyError(RuntimeError):
    """表示 required PDF 资源缺失或没有通过 manifest 完整性校验。"""

    def __init__(self, unavailable_packs: Sequence[str]) -> None:
        """记录不可用 pack 名称, 不暴露本地路径。"""

        super().__init__("pdf_resources_not_ready")
        self.unavailable_packs = tuple(unavailable_packs)


def sync_pdf_asset(
    asset: PdfAsset,
    *,
    url: str,
    destination: Path,
    timeout: float = 120.0,
    progress_callback: ProgressCallback | None = None,
    cancel_requested: CancelCheck | None = None,
    open_url: UrlOpener | None = None,
    replace_file: FileReplacer | None = None,
    validate_existing: bool = True,
) -> bool:
    """下载单个资产, 通过 size/SHA 校验后才原子替换目标文件。"""

    if not math.isfinite(timeout) or timeout <= 0:
        raise PdfAssetSyncError("PDF 资源 timeout 必须是大于 0 的有限数字", "download_failed")
    opener = open_url or urlopen
    replacer = replace_file or os.replace
    temporary_path: Path | None = None
    try:
        _raise_if_cancelled(cancel_requested)
        if destination.exists() and destination.is_dir():
            raise PdfAssetSyncError(
                f"PDF 资源目标不能是目录: {destination}",
                "filesystem_error",
            )
        if validate_existing and is_pdf_asset_valid(destination, asset):
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = Request(
            url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "PageFerry-pdf-assets/1",
            },
        )
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f".{destination.name}.",
            suffix=".download",
            dir=destination.parent,
            delete=False,
        ) as target:
            temporary_path = Path(target.name)
            # urllib.request.urlopen 的第二个位置参数是 request data, 不能把 timeout 当位置参数。
            with opener(request, timeout=timeout) as response:
                _stream_pdf_asset(
                    response,
                    target,
                    asset,
                    progress_callback=progress_callback,
                    cancel_requested=cancel_requested,
                )
            _raise_if_cancelled(cancel_requested)
            target.flush()
            os.fsync(target.fileno())
        _raise_if_cancelled(cancel_requested)
        replacer(temporary_path, destination)
        temporary_path = None
        return True
    except PdfAssetSyncError:
        raise
    except (URLError, HTTPException, TimeoutError, ConnectionError) as error:
        raise PdfAssetSyncError(f"PDF 资源下载失败: {asset.asset_id}", "download_failed") from error
    except OSError as error:
        error_code: PdfResourceErrorCode = (
            "insufficient_disk_space"
            if error.errno in PDF_RESOURCE_DISK_FULL_ERRNOS
            else "filesystem_error"
        )
        raise PdfAssetSyncError(
            f"PDF 资源文件操作失败: {asset.asset_id}",
            error_code,
        ) from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def sync_pdf_asset_pack(
    manifest: PdfAssetManifest,
    pack_path: Path,
    *,
    assets: Sequence[PdfAsset] | None = None,
    base_url: str | None = None,
    timeout: float = 120.0,
    progress_callback: ProgressCallback | None = None,
    asset_completed_callback: AssetCompletedCallback | None = None,
    cancel_requested: CancelCheck | None = None,
    open_url: UrlOpener | None = None,
    replace_file: FileReplacer | None = None,
    validate_existing: bool = True,
) -> tuple[PdfAssetSyncResult, ...]:
    """按 manifest 顺序尝试主源、fallback 与 upstream, 每个文件独立原子收敛。"""

    results: list[PdfAssetSyncResult] = []
    selected_assets = assets if assets is not None else manifest.assets
    for asset in selected_assets:
        _raise_if_cancelled(cancel_requested)
        destination = pdf_asset_path(pack_path, asset)
        try:
            if validate_existing and is_pdf_asset_valid(destination, asset):
                results.append(
                    PdfAssetSyncResult(
                        asset_id=asset.asset_id,
                        path=destination,
                        downloaded=False,
                    )
                )
                if asset_completed_callback is not None:
                    asset_completed_callback(asset, destination, False)
                continue
        except OSError as error:
            raise PdfAssetSyncError(
                f"无法校验现有 PDF 资源: {destination}",
                "filesystem_error",
            ) from error

        urls = pdf_asset_download_urls(manifest, asset, base_url=base_url)
        last_error: PdfAssetSyncError | None = None
        downloaded = False
        for url in urls:
            _raise_if_cancelled(cancel_requested)
            if progress_callback is not None:
                progress_callback(asset.asset_id, 0)
            try:
                downloaded = sync_pdf_asset(
                    asset,
                    url=url,
                    destination=destination,
                    timeout=timeout,
                    progress_callback=(
                        None
                        if progress_callback is None
                        else lambda completed, asset_id=asset.asset_id: progress_callback(
                            asset_id, completed
                        )
                    ),
                    cancel_requested=cancel_requested,
                    open_url=open_url,
                    replace_file=replace_file,
                    # pack 入口已经校验过目标; fallback 切换时不应反复哈希大型旧文件。
                    validate_existing=False,
                )
                break
            except PdfAssetDownloadCancelled:
                raise
            except PdfAssetSyncError as error:
                # 候选源各用独立临时文件, 坏内容不能污染下一次尝试或旧文件。
                last_error = error
        else:
            error_code = last_error.error_code if last_error is not None else "download_failed"
            raise PdfAssetSyncError(
                f"PDF 资源所有下载来源均失败: {asset.asset_id} "
                f"(尝试 {len(urls)} 个来源), 最后一次错误: {last_error}",
                error_code,
            ) from last_error
        results.append(
            PdfAssetSyncResult(
                asset_id=asset.asset_id,
                path=destination,
                downloaded=downloaded,
            )
        )
        if asset_completed_callback is not None:
            asset_completed_callback(asset, destination, downloaded)
    return tuple(results)


def _stream_pdf_asset(
    response: BinaryIO,
    target: BinaryIO,
    asset: PdfAsset,
    *,
    progress_callback: Callable[[int], None] | None = None,
    cancel_requested: CancelCheck | None = None,
) -> None:
    """边写临时文件边校验 size 与 SHA-256, 并在每个 chunk 响应取消。"""

    digest = hashlib.sha256()
    size = 0
    while chunk := response.read(PDF_RESOURCE_CHUNK_SIZE):
        _raise_if_cancelled(cancel_requested)
        target.write(chunk)
        digest.update(chunk)
        size += len(chunk)
        if size > asset.size_bytes:
            raise PdfAssetSyncError(
                f"PDF 资源超过 manifest size: expected={asset.size_bytes}, actual>{size}",
                "integrity_check_failed",
            )
        if progress_callback is not None:
            progress_callback(size)
    _raise_if_cancelled(cancel_requested)
    if size != asset.size_bytes:
        raise PdfAssetSyncError(
            f"PDF 资源 size 校验失败: expected={asset.size_bytes}, actual={size}",
            "integrity_check_failed",
        )
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != asset.sha256:
        raise PdfAssetSyncError(
            f"PDF 资源 SHA-256 校验失败: expected={asset.sha256}, actual={actual_sha256}",
            "integrity_check_failed",
        )


def _raise_if_cancelled(cancel_requested: CancelCheck | None) -> None:
    """在网络与原子替换边界把用户取消转换为内部控制信号。"""

    if cancel_requested is not None and cancel_requested():
        raise PdfAssetDownloadCancelled()


class PdfResourceInstaller:
    """用单 worker 管理 required PDF 资源的检查、安装、取消与重试。"""

    def __init__(
        self,
        manifest: PdfAssetManifest,
        pack_path: Path,
        *,
        required_packs: Sequence[str] = DEFAULT_REQUIRED_PDF_RESOURCE_PACKS,
        timeout: float = 120.0,
        open_url: UrlOpener | None = None,
        disk_usage: Callable[[Path], DiskUsage] | None = None,
        close_timeout: float = 0.5,
    ) -> None:
        """固定 manifest、安装目录与 required packs, 但不自动触发下载。"""

        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("PDF 资源 timeout 必须是大于 0 的有限数字")
        if not math.isfinite(close_timeout) or close_timeout < 0:
            raise ValueError("PDF 资源 close_timeout 不能是负数或非有限数字")
        self.manifest = manifest
        self.pack_path = pack_path.expanduser().resolve()
        self.assets = select_pdf_assets(manifest, packs=tuple(required_packs))
        self.timeout = timeout
        self.close_timeout = close_timeout
        self._open_url = open_url
        self._disk_usage = disk_usage or shutil.disk_usage
        self._lock = RLock()
        self._cancel_event = Event()
        self._worker: Thread | None = None
        self._initialized = False
        self._closed = False
        self._state: PdfResourceState = "missing"
        self._error_code: PdfResourceErrorCode | None = None
        self._current_asset_id: str | None = None
        self._current_asset_bytes = 0
        self._file_states: dict[str, _AssetFileState] = {}

    def initialize(self) -> PdfResourceStatus:
        """启动时只记录轻量文件指纹, 不让大型资源 checksum 阻塞 sidecar ready。"""

        with self._lock:
            if not self._initialized:
                self._refresh_file_states_locked(verify_unknown=False)
                self._state = "ready" if self._all_assets_ready_locked() else "missing"
                self._initialized = True
            return self._status_locked(refresh=False)

    def ensure_ready(self) -> PdfResourceStatus:
        """同步校验 required 资源; 不可用时 fail-closed, 且绝不隐式下载。"""

        self.initialize()
        with self._lock:
            self._refresh_file_states_locked(verify_unknown=True)
            if not self._all_assets_ready_locked():
                # 任务 gate 不能把正在下载、取消或可重试失败的 UI 状态抹成 missing。
                if self._state == "ready":
                    self._state = "missing"
                    self._error_code = None
                unavailable_packs = tuple(
                    dict.fromkeys(
                        asset.pack for asset in self.assets if not self._asset_ready_locked(asset)
                    )
                )
                raise PdfResourcesNotReadyError(unavailable_packs)
            self._state = "ready"
            self._error_code = None
            return self._status_locked(refresh=False)

    def status(self) -> PdfResourceStatus:
        """返回准确状态; 首次查询校验未知文件, 未变化时不重复计算 checksum。"""

        self.initialize()
        with self._lock:
            return self._status_locked(refresh=True)

    def start_install(self) -> PdfResourceStatus:
        """显式启动 required packs 安装, 活跃 worker 存在时保持幂等。"""

        self.initialize()
        with self._lock:
            if self._closed:
                self._state = "failed"
                self._error_code = "filesystem_error"
                return self._status_locked(refresh=False)
            if self._worker is not None and self._worker.is_alive():
                return self._status_locked(refresh=False)

            self._refresh_file_states_locked(verify_unknown=True)
            if self._all_assets_ready_locked():
                self._state = "ready"
                self._error_code = None
                return self._status_locked(refresh=False)
            disk_error = self._disk_space_error_locked()
            if disk_error is not None:
                self._state = "failed"
                self._error_code = disk_error
                self._current_asset_id = None
                self._current_asset_bytes = 0
                return self._status_locked(refresh=False)

            self._cancel_event.clear()
            self._state = "downloading"
            self._error_code = None
            self._current_asset_id = None
            self._current_asset_bytes = 0
            self._worker = Thread(
                target=self._run_install,
                name="pdf-resources",
                daemon=True,
            )
            self._worker.start()
            return self._status_locked(refresh=False)

    def cancel(self) -> PdfResourceStatus:
        """请求当前 worker 在下一个安全边界停止, 不删除已经校验落盘的文件。"""

        self.initialize()
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                self._cancel_event.set()
                # cancelling 表示取消请求已送达但 worker 尚未退出, 不能提前伪装成 cancelled。
                self._state = "cancelling"
            return self._status_locked(refresh=False)

    def close(self) -> None:
        """请求 worker 停止并有界等待, 避免卡住的网络连接阻塞 sidecar 退出。"""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancel_event.set()
            worker = self._worker
        if worker is not None:
            # daemon worker 即使卡在 DNS/connect 也不会阻止进程退出; 正常响应取消时仍会回收。
            worker.join(timeout=self.close_timeout)

    def _run_install(self) -> None:
        """在唯一 worker 中下载当前缺失资产, 并把异常收敛为稳定状态。"""

        try:
            with self._lock:
                pending_assets = tuple(
                    asset for asset in self.assets if not self._asset_ready_locked(asset)
                )
            sync_pdf_asset_pack(
                self.manifest,
                self.pack_path,
                assets=pending_assets,
                timeout=self.timeout,
                progress_callback=self._record_progress,
                asset_completed_callback=self._record_asset_completed,
                cancel_requested=self._cancel_event.is_set,
                open_url=self._open_url,
                # pending_assets 来自同一把锁保护的 checksum cache, 无需立刻再哈希一次。
                validate_existing=False,
            )
            with self._lock:
                self._state = "ready" if self._all_assets_ready_locked() else "missing"
                self._error_code = None
                self._current_asset_id = None
                self._current_asset_bytes = 0
        except PdfAssetDownloadCancelled:
            with self._lock:
                self._state = "cancelled"
                self._error_code = None
                self._current_asset_id = None
                self._current_asset_bytes = 0
        except PdfAssetSyncError as error:
            with self._lock:
                self._refresh_file_states_locked(verify_unknown=True)
                self._state = "failed"
                self._error_code = error.error_code
                self._current_asset_id = None
                self._current_asset_bytes = 0
        except Exception:
            # Worker 不能让未预期异常在线程里悄悄失联, 对外只给稳定文件错误码。
            logger.exception("PDF resource installer worker failed unexpectedly")
            with self._lock:
                self._state = "failed"
                self._error_code = "filesystem_error"
                self._current_asset_id = None
                self._current_asset_bytes = 0

    def _record_progress(self, asset_id: str, completed_bytes: int) -> None:
        """接收下载线程的单资产进度, 候选切换时允许安全回退到零。"""

        with self._lock:
            if self._state == "downloading":
                self._current_asset_id = asset_id
                self._current_asset_bytes = completed_bytes

    def _record_asset_completed(
        self,
        asset: PdfAsset,
        _destination: Path,
        _downloaded: bool,
    ) -> None:
        """在下载流校验完成后立刻缓存有效指纹, 避免切换资产时二次哈希。"""

        with self._lock:
            self._remember_file_state_locked(asset, valid=True)
            self._current_asset_id = None
            self._current_asset_bytes = 0

    def _status_locked(self, *, refresh: bool) -> PdfResourceStatus:
        """在持锁状态下聚合 total、completed 与 pack 粒度进度。"""

        if refresh:
            self._refresh_file_states_locked(verify_unknown=True)
        all_ready = self._all_assets_ready_locked()
        active = self._worker is not None and self._worker.is_alive()
        if all_ready and not active:
            self._state = "ready"
            self._error_code = None
        elif self._state == "ready" and not all_ready:
            self._state = "missing"

        resource_rows: list[PdfResourcePackStatus] = []
        total_completed = 0
        for pack in dict.fromkeys(asset.pack for asset in self.assets):
            pack_assets = tuple(asset for asset in self.assets if asset.pack == pack)
            pack_size = sum(asset.size_bytes for asset in pack_assets)
            pack_completed = sum(
                asset.size_bytes for asset in pack_assets if self._asset_ready_locked(asset)
            )
            if self._current_asset_id is not None:
                current_asset = next(
                    (asset for asset in pack_assets if asset.asset_id == self._current_asset_id),
                    None,
                )
                if current_asset is not None and not self._asset_ready_locked(current_asset):
                    pack_completed += min(self._current_asset_bytes, current_asset.size_bytes)
            total_completed += pack_completed
            resource_rows.append(
                PdfResourcePackStatus(
                    pack=pack,
                    size_bytes=pack_size,
                    completed_bytes=pack_completed,
                    ready=all(self._asset_ready_locked(asset) for asset in pack_assets),
                )
            )
        return PdfResourceStatus(
            pack_revision=self.manifest.pack_revision,
            state=self._state,
            total_bytes=sum(asset.size_bytes for asset in self.assets),
            completed_bytes=total_completed,
            current_asset_id=self._current_asset_id,
            error_code=self._error_code,
            resources=tuple(resource_rows),
        )

    def _disk_space_error_locked(self) -> PdfResourceErrorCode | None:
        """检查剩余空间, 区分容量不足与路径本身不可访问。"""

        required_bytes = sum(
            asset.size_bytes for asset in self.assets if not self._asset_ready_locked(asset)
        )
        try:
            self.pack_path.mkdir(parents=True, exist_ok=True)
            if self._disk_usage(self.pack_path).free < required_bytes:
                return "insufficient_disk_space"
            return None
        except OSError:
            return "filesystem_error"

    def _refresh_file_states_locked(self, *, verify_unknown: bool) -> None:
        """刷新 stat 指纹, 并按调用边界决定是否读取未知 snapshot 的完整内容。"""

        for asset in self.assets:
            path = pdf_asset_path(self.pack_path, asset)
            try:
                if not path.is_file():
                    self._file_states.pop(asset.asset_id, None)
                    continue
                stat = path.stat()
            except OSError:
                self._file_states.pop(asset.asset_id, None)
                continue
            signature = (
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )
            cached = self._file_states.get(asset.asset_id)
            if (
                cached is not None
                and cached.signature == signature
                and (cached.valid is not None or not verify_unknown)
            ):
                continue
            valid: bool | None = False
            if stat.st_size == asset.size_bytes and not verify_unknown:
                # lifespan 只能确认“可能可用”; 公开状态或任务 gate 校验前必须保持非 ready。
                valid = None
            elif stat.st_size == asset.size_bytes:
                try:
                    digest, size = hash_pdf_asset(path)
                    valid = size == asset.size_bytes and digest == asset.sha256
                except OSError:
                    valid = False
            self._file_states[asset.asset_id] = _AssetFileState(signature=signature, valid=valid)

    def _remember_file_state_locked(self, asset: PdfAsset, *, valid: bool) -> None:
        """下载流已完成 checksum 后只读取 stat, 避免对 130MiB 模型再次哈希。"""

        path = pdf_asset_path(self.pack_path, asset)
        stat = path.stat()
        signature = (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
        self._file_states[asset.asset_id] = _AssetFileState(signature=signature, valid=valid)

    def _asset_ready_locked(self, asset: PdfAsset) -> bool:
        """读取一个资产的缓存校验结论。"""

        state = self._file_states.get(asset.asset_id)
        return state is not None and state.valid

    def _all_assets_ready_locked(self) -> bool:
        """判断全部 required assets 是否已经通过 checksum。"""

        return all(self._asset_ready_locked(asset) for asset in self.assets)
