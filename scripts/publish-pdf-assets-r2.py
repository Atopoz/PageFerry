"""把经过 manifest 校验的 PageFerry PDF 资源发布到 Cloudflare R2。"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import secrets
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
DEFAULT_MANIFEST = BACKEND_ROOT / "resources" / "pdf_assets" / "manifest.json"
DEFAULT_LICENSES_DIR = DEFAULT_MANIFEST.parent / "licenses"
DEFAULT_WRANGLER_COMMAND = "wrangler"
R2_CACHE_CONTROL = "public, max-age=31536000, immutable"
DEFAULT_FETCH_TIMEOUT = 300.0
CHUNK_SIZE = 1024 * 1024
_PUBLIC_VERIFY_ATTEMPTS = 3
_R2_BUCKET_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])\Z")

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from modules.pdf.assets import (  # noqa: E402
    PdfAssetManifest,
    PdfAssetManifestError,
    hash_pdf_asset,
    load_pdf_asset_manifest,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


class PublicResponse(Protocol):
    """描述公网资源校验所需的最小 HTTP response 接口。"""

    status: int

    def read(self, size: int = -1) -> bytes:
        """读取最多指定字节的 response body。"""

    def __enter__(self) -> PublicResponse:
        """进入 response context。"""

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        """退出 response context。"""


Fetcher = Callable[..., PublicResponse]


class R2PublishError(RuntimeError):
    """表示发布前校验、R2 查询或上传失败。"""


class _R2PublicBodyMismatch(R2PublishError):
    """表示公网 body 在一次传输中未通过 size 或 SHA-256 校验。"""


@dataclass(frozen=True, slots=True)
class R2PublishObject:
    """描述一个版本目录下不可变的 R2 对象。"""

    key: str
    public_url: str
    source: Path
    size_bytes: int
    sha256: str
    content_type: str


@dataclass(frozen=True, slots=True)
class R2PublishResult:
    """记录一个远端对象本次是上传还是复用。"""

    key: str
    uploaded: bool


def build_publish_plan(
    manifest_path: Path,
    source_dir: Path,
    licenses_dir: Path,
) -> tuple[PdfAssetManifest, tuple[R2PublishObject, ...]]:
    """校验全部本地文件后生成二进制、license、manifest 的固定发布顺序。"""

    manifest_path = manifest_path.expanduser().resolve()
    source_dir = source_dir.expanduser().resolve()
    licenses_dir = licenses_dir.expanduser().resolve()
    manifest = load_pdf_asset_manifest(manifest_path)
    _validate_manifest_base_url(manifest)
    if manifest.default_base_url is None:
        raise R2PublishError("canonical manifest default_base_url 不能为空")
    base_url = manifest.default_base_url
    prefix = f"pdf/{manifest.pack_revision}"

    binary_objects: list[R2PublishObject] = []
    for asset in manifest.assets:
        source = source_dir / asset.relative_path
        _verify_local_file(
            source,
            expected_size=asset.size_bytes,
            expected_sha256=asset.sha256,
            label=asset.asset_id,
        )
        binary_objects.append(
            R2PublishObject(
                key=f"{prefix}/{asset.distribution_path}",
                public_url=urljoin(base_url, asset.distribution_path),
                source=source,
                size_bytes=asset.size_bytes,
                sha256=asset.sha256,
                content_type=_content_type(source),
            )
        )

    license_objects = tuple(
        _local_object(
            prefix,
            base_url,
            path,
            f"licenses/{path.relative_to(licenses_dir).as_posix()}",
        )
        for path in _license_files(licenses_dir)
    )
    manifest_object = _local_object(prefix, base_url, manifest_path, "manifest.json")
    objects = (*binary_objects, *license_objects, manifest_object)
    _reject_duplicate_keys(objects)
    return manifest, objects


def publish_plan(
    bucket: str,
    objects: Sequence[R2PublishObject],
    *,
    wrangler_command: Sequence[str],
    runner: Runner = subprocess.run,
    fetcher: Fetcher = urlopen,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
) -> tuple[R2PublishResult, ...]:
    """通过公网 URL 核验对象, 缺失时用 wrangler 上传并再次校验。"""

    if _R2_BUCKET_RE.fullmatch(bucket) is None:
        raise R2PublishError("R2 bucket 名称必须是 3-63 位小写字母、数字或连字符")
    if not wrangler_command:
        raise R2PublishError("wrangler command 不能为空")
    if not math.isfinite(timeout) or timeout <= 0:
        raise R2PublishError("公网校验 timeout 必须是大于 0 的有限数字")

    results: list[R2PublishResult] = []
    for item in objects:
        if _inspect_public_object(item, fetcher=fetcher, timeout=timeout):
            results.append(R2PublishResult(key=item.key, uploaded=False))
            continue

        # 发布前再次校验源文件, 避免首次全量检查后文件被替换。
        _verify_local_file(
            item.source,
            expected_size=item.size_bytes,
            expected_sha256=item.sha256,
            label=item.key,
        )
        _upload_remote_object(
            bucket,
            item,
            wrangler_command=wrangler_command,
            runner=runner,
        )
        if not _inspect_public_object(item, fetcher=fetcher, timeout=timeout):
            raise R2PublishError(f"R2 对象上传后公网仍返回 404: {item.public_url}")
        results.append(R2PublishResult(key=item.key, uploaded=True))
    return tuple(results)


def publish_pdf_assets(
    bucket: str,
    manifest_path: Path,
    source_dir: Path | None,
    licenses_dir: Path,
    *,
    wrangler_command: Sequence[str],
    runner: Runner = subprocess.run,
    fetcher: Fetcher = urlopen,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
) -> tuple[R2PublishResult, ...]:
    """先完成全部本地校验, 再执行不可变的 R2 资源发布。"""

    manifest = load_pdf_asset_manifest(manifest_path.expanduser().resolve())
    resolved_source_dir = (
        source_dir.expanduser().resolve()
        if source_dir is not None
        else PROJECT_ROOT / ".data" / "pdf" / manifest.pack_revision
    )
    _, objects = build_publish_plan(manifest_path, resolved_source_dir, licenses_dir)
    return publish_plan(
        bucket,
        objects,
        wrangler_command=wrangler_command,
        runner=runner,
        fetcher=fetcher,
        timeout=timeout,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 R2 bucket、本地资源目录与 wrangler 命令。"""

    parser = argparse.ArgumentParser(
        description="校验并发布版本化 PDF 资源; 相同 key 内容不一致时拒绝覆盖。"
    )
    parser.add_argument("--bucket", required=True, help="目标 R2 bucket 名称。")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="canonical PDF 资源 manifest 路径。",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="版本化资源根目录; 默认使用 .data/pdf/<pack_revision>。",
    )
    parser.add_argument(
        "--licenses-dir",
        type=Path,
        default=DEFAULT_LICENSES_DIR,
        help="随版本发布的 license 文件目录。",
    )
    parser.add_argument(
        "--wrangler-command",
        default=os.environ.get("PAGEFERRY_WRANGLER_COMMAND", DEFAULT_WRANGLER_COMMAND),
        help="wrangler 启动命令; 需要 npx 时可传入带版本的完整命令。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_FETCH_TIMEOUT,
        help="单个公网完整性校验请求 timeout 秒数, 默认 300。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行一次 R2 发布, 并以稳定退出码报告上传与复用数量。"""

    args = parse_args(argv)
    try:
        wrangler_command = tuple(shlex.split(args.wrangler_command))
        results = publish_pdf_assets(
            args.bucket,
            args.manifest,
            args.source_dir,
            args.licenses_dir,
            wrangler_command=wrangler_command,
            timeout=args.timeout,
        )
    except (OSError, ValueError, PdfAssetManifestError, R2PublishError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    uploaded = sum(result.uploaded for result in results)
    reused = len(results) - uploaded
    print(f"PDF R2 资源发布完成: uploaded={uploaded}, reused={reused}, bucket={args.bucket}")
    return 0


def _local_object(
    prefix: str,
    base_url: str,
    source: Path,
    relative_key: str,
) -> R2PublishObject:
    """校验普通发布文件并生成带内容摘要的对象描述。"""

    if not source.is_file():
        raise R2PublishError(f"发布文件不存在或不是普通文件: {source}")
    sha256, size_bytes = hash_pdf_asset(source)
    return R2PublishObject(
        key=f"{prefix}/{relative_key}",
        public_url=urljoin(base_url, relative_key),
        source=source,
        size_bytes=size_bytes,
        sha256=sha256,
        content_type=_content_type(source),
    )


def _validate_manifest_base_url(manifest: PdfAssetManifest) -> None:
    """拒绝缺少地址或 revision 路径不匹配的 completion manifest。"""

    if manifest.default_base_url is None:
        raise R2PublishError("canonical manifest default_base_url 不能为空")
    expected_suffix = f"/pdf/{manifest.pack_revision}/"
    actual_path = urlsplit(manifest.default_base_url).path
    if not actual_path.endswith(expected_suffix):
        raise R2PublishError(
            "canonical manifest default_base_url path 必须以 "
            f"{expected_suffix} 结尾: {manifest.default_base_url}"
        )


def _license_files(licenses_dir: Path) -> tuple[Path, ...]:
    """按稳定相对路径列出 license 文件, 并拒绝符号链接。"""

    if not licenses_dir.is_dir():
        raise R2PublishError(f"PDF 资源 license 目录不存在: {licenses_dir}")
    files = tuple(sorted(path for path in licenses_dir.rglob("*") if path.is_file()))
    if not files:
        raise R2PublishError(f"PDF 资源 license 目录为空: {licenses_dir}")
    symlinks = [path for path in files if path.is_symlink()]
    if symlinks:
        raise R2PublishError(f"PDF 资源 license 不能使用符号链接: {symlinks[0]}")
    return files


def _verify_local_file(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    label: str,
) -> None:
    """用 size 与 SHA-256 校验待发布文件。"""

    if not path.is_file():
        raise R2PublishError(f"本地 PDF 资源不存在或不是普通文件: {label}: {path}")
    actual_sha256, actual_size = hash_pdf_asset(path)
    if actual_size != expected_size:
        raise R2PublishError(
            f"本地 PDF 资源 size 校验失败: {label}: expected={expected_size}, actual={actual_size}"
        )
    if actual_sha256 != expected_sha256:
        raise R2PublishError(
            f"本地 PDF 资源 SHA-256 校验失败: {label}: "
            f"expected={expected_sha256}, actual={actual_sha256}"
        )


def _inspect_public_object(
    item: R2PublishObject,
    *,
    fetcher: Fetcher,
    timeout: float,
) -> bool:
    """重试公网 body 校验, 避免把偶发截断误判为不可变对象冲突。"""

    last_mismatch: _R2PublicBodyMismatch | None = None
    for _attempt in range(_PUBLIC_VERIFY_ATTEMPTS):
        try:
            return _inspect_public_object_once(item, fetcher=fetcher, timeout=timeout)
        except _R2PublicBodyMismatch as error:
            last_mismatch = error
    if last_mismatch is None:
        raise R2PublishError(f"R2 公网对象校验没有产生结果: {item.public_url}")
    raise R2PublishError(str(last_mismatch)) from last_mismatch


def _inspect_public_object_once(
    item: R2PublishObject,
    *,
    fetcher: Fetcher,
    timeout: float,
) -> bool:
    """用带随机 query 的单次公网 GET 流式校验对象, 只有 404 表示缺失。"""

    request = Request(
        _cache_busted_url(item.public_url),
        headers={
            "Accept": "application/octet-stream",
            "Cache-Control": "no-cache",
            "User-Agent": "PageFerry-r2-publisher/1",
        },
    )
    try:
        response = fetcher(request, timeout=timeout)
    except HTTPError as error:
        if error.code == 404:
            return False
        raise R2PublishError(
            f"查询 R2 公网对象失败: HTTP {error.code}: {item.public_url}"
        ) from error
    except (HTTPException, OSError, URLError) as error:
        raise R2PublishError(f"查询 R2 公网对象失败: {item.public_url}") from error

    try:
        with response:
            if response.status == 404:
                return False
            if response.status != 200:
                raise R2PublishError(
                    f"查询 R2 公网对象失败: HTTP {response.status}: {item.public_url}"
                )
            _verify_public_body(response, item)
        return True
    except R2PublishError:
        raise
    except (HTTPException, OSError, URLError) as error:
        raise R2PublishError(f"读取 R2 公网对象失败: {item.public_url}") from error


def _verify_public_body(response: PublicResponse, item: R2PublishObject) -> None:
    """流式计算公网 response 的 size 与 SHA-256, 冲突时拒绝覆盖。"""

    digest = hashlib.sha256()
    size = 0
    while chunk := response.read(CHUNK_SIZE):
        digest.update(chunk)
        size += len(chunk)
        if size > item.size_bytes:
            raise _R2PublicBodyMismatch(
                f"R2 不可变对象内容冲突, 拒绝覆盖: {item.public_url}"
            )
    if size != item.size_bytes or digest.hexdigest() != item.sha256:
        raise _R2PublicBodyMismatch(
            f"R2 不可变对象内容冲突, 拒绝覆盖: {item.public_url}"
        )


def _cache_busted_url(url: str) -> str:
    """为每次公网校验添加新的随机 query, 绕过 CDN 旧响应缓存。"""

    parsed = urlsplit(url)
    query = urlencode({"pageferry_verify": secrets.token_hex(16)})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _upload_remote_object(
    bucket: str,
    item: R2PublishObject,
    *,
    wrangler_command: Sequence[str],
    runner: Runner,
) -> None:
    """用固定 metadata 上传一个确认缺失的 R2 对象。"""

    result = _run_wrangler(
        [
            *wrangler_command,
            "r2",
            "object",
            "put",
            f"{bucket}/{item.key}",
            "--remote",
            "--file",
            str(item.source),
            "--content-type",
            item.content_type,
            "--cache-control",
            R2_CACHE_CONTROL,
        ],
        runner,
    )
    if result.returncode != 0:
        raise R2PublishError(f"上传 R2 对象失败: {item.key}: {_brief_error(result)}")


def _run_wrangler(
    command: list[str],
    runner: Runner,
) -> subprocess.CompletedProcess[str]:
    """以参数数组运行 wrangler, 不经过 shell 展开任何用户输入。"""

    try:
        return runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    except OSError as error:
        raise R2PublishError("无法启动 wrangler 命令") from error


def _brief_error(result: subprocess.CompletedProcess[str]) -> str:
    """压缩 wrangler 错误输出, 避免终端被大段日志淹没。"""

    text = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
    return text[-500:]


def _content_type(path: Path) -> str:
    """为当前 PDF 资源格式返回稳定的 HTTP Content-Type。"""

    suffix = path.suffix.casefold()
    return {
        ".json": "application/json; charset=utf-8",
        ".onnx": "application/octet-stream",
        ".otf": "font/otf",
        ".ttf": "font/ttf",
        ".txt": "text/plain; charset=utf-8",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }.get(suffix, "application/octet-stream")


def _reject_duplicate_keys(objects: Sequence[R2PublishObject]) -> None:
    """拒绝会让某个发布阶段覆盖前一阶段的重复 object key。"""

    seen: set[str] = set()
    for item in objects:
        if item.key in seen:
            raise R2PublishError(f"R2 发布计划 object key 重复: {item.key}")
        seen.add(item.key)


if __name__ == "__main__":
    raise SystemExit(main())
