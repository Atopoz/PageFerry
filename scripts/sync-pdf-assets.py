"""显式同步并校验 PageFerry PDF 大型资源包。"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.error import URLError
from urllib.request import Request, urlopen

from platformdirs import user_data_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
DEFAULT_MANIFEST = BACKEND_ROOT / "resources" / "pdf_assets" / "manifest.json"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from modules.pdf.assets import (  # noqa: E402
    PdfAsset,
    PdfAssetManifest,
    PdfAssetManifestError,
    is_pdf_asset_valid,
    load_pdf_asset_manifest,
    pdf_asset_download_url,
    pdf_asset_pack_path,
    pdf_asset_path,
    select_pdf_assets,
)

CHUNK_SIZE = 1024 * 1024


class PdfAssetSyncError(RuntimeError):
    """表示 PDF 资源下载、校验或原子落盘失败。"""


@dataclass(frozen=True, slots=True)
class PdfAssetSyncResult:
    """记录一个资源最终路径及本次是否实际下载。"""

    asset_id: str
    path: Path
    downloaded: bool


def resolve_pack_destination(
    manifest: PdfAssetManifest,
    data_dir: Path | None,
) -> Path:
    """解析 app-data/pdf 下带 pack revision 的安装目录。"""

    app_data_dir = (
        data_dir.expanduser().resolve()
        if data_dir is not None
        else Path(user_data_path("PageFerry", appauthor=False, roaming=False))
    )
    return pdf_asset_pack_path(app_data_dir, manifest)


def sync_pdf_asset(
    asset: PdfAsset,
    *,
    url: str,
    destination: Path,
    timeout: float = 120.0,
) -> bool:
    """同步单个资产, 有效旧文件直接复用, 新文件校验后原子替换。"""

    if not math.isfinite(timeout) or timeout <= 0:
        raise PdfAssetSyncError("PDF 资源 timeout 必须是大于 0 的有限数字")
    temporary_path: Path | None = None
    try:
        if destination.exists() and destination.is_dir():
            raise PdfAssetSyncError(f"PDF 资源目标不能是目录: {destination}")
        if is_pdf_asset_valid(destination, asset):
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
            with urlopen(request, timeout=timeout) as response:
                _stream_asset(response, target, asset)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
        return True
    except PdfAssetSyncError:
        raise
    except (OSError, URLError) as error:
        raise PdfAssetSyncError(f"PDF 资源同步失败: {asset.asset_id}") from error
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
) -> tuple[PdfAssetSyncResult, ...]:
    """按 manifest 顺序同步整个 pack, 每个文件独立原子收敛。"""

    results: list[PdfAssetSyncResult] = []
    selected_assets = assets if assets is not None else manifest.assets
    for asset in selected_assets:
        destination = pdf_asset_path(pack_path, asset)
        try:
            if is_pdf_asset_valid(destination, asset):
                results.append(
                    PdfAssetSyncResult(
                        asset_id=asset.asset_id,
                        path=destination,
                        downloaded=False,
                    )
                )
                continue
        except OSError as error:
            raise PdfAssetSyncError(f"无法校验现有 PDF 资源: {destination}") from error
        url = pdf_asset_download_url(manifest, asset, base_url=base_url)
        downloaded = sync_pdf_asset(
            asset,
            url=url,
            destination=destination,
            timeout=timeout,
        )
        results.append(
            PdfAssetSyncResult(
                asset_id=asset.asset_id,
                path=destination,
                downloaded=downloaded,
            )
        )
    return tuple(results)


def _stream_asset(response: BinaryIO, target: BinaryIO, asset: PdfAsset) -> None:
    """边下载边校验 size 与 SHA-256, 超长或不完整内容立即失败。"""

    digest = hashlib.sha256()
    size = 0
    while chunk := response.read(CHUNK_SIZE):
        target.write(chunk)
        digest.update(chunk)
        size += len(chunk)
        if size > asset.size_bytes:
            raise PdfAssetSyncError(
                f"PDF 资源超过 manifest size: expected={asset.size_bytes}, actual>{size}"
            )
    if size != asset.size_bytes:
        raise PdfAssetSyncError(
            f"PDF 资源 size 校验失败: expected={asset.size_bytes}, actual={size}"
        )
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != asset.sha256:
        raise PdfAssetSyncError(
            f"PDF 资源 SHA-256 校验失败: expected={asset.sha256}, actual={actual_sha256}"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析显式资源包同步参数。"""

    parser = argparse.ArgumentParser(
        description="下载版本化 PDF 资源包, 并在原子落盘前校验 size 与 SHA-256。"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="资源包 manifest 路径, 默认使用仓库内固定版本。",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="PageFerry app-data 根目录, 省略时使用当前平台默认目录。",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PAGEFERRY_PDF_ASSET_BASE_URL"),
        help="覆盖 manifest 默认下载 base URL, 适用于自有 CDN 或镜像。",
    )
    parser.add_argument(
        "--asset",
        action="append",
        default=[],
        help="只同步指定 asset ID, 可重复传入。",
    )
    parser.add_argument(
        "--pack",
        action="append",
        default=[],
        help="同步指定 pack group, 可重复传入; 未选择时默认只同步 layout。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="单次 HTTP 请求超时秒数, 默认 120。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行一次资源包同步, 并以稳定退出码报告结果。"""

    args = parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("error: --timeout 必须是大于 0 的有限数字", file=sys.stderr)
        return 2
    try:
        manifest = load_pdf_asset_manifest(args.manifest.expanduser().resolve())
        pack_path = resolve_pack_destination(manifest, args.data_dir)
        selected_assets = select_pdf_assets(
            manifest,
            asset_ids=tuple(args.asset),
            packs=tuple(args.pack) if args.pack or args.asset else ("layout",),
        )
        results = sync_pdf_asset_pack(
            manifest,
            pack_path,
            assets=selected_assets,
            base_url=args.base_url,
            timeout=args.timeout,
        )
    except (PdfAssetManifestError, PdfAssetSyncError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    downloaded = sum(result.downloaded for result in results)
    reused = len(results) - downloaded
    print(f"PDF 资源包同步完成: downloaded={downloaded}, reused={reused}, path={pack_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
