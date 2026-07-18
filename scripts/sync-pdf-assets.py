"""显式同步并校验 PageFerry PDF 大型资源包。"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO
from urllib.error import URLError as _URLError
from urllib.request import urlopen

from platformdirs import user_data_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
DEFAULT_MANIFEST = BACKEND_ROOT / "resources" / "pdf_assets" / "manifest.json"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from modules.pdf import resource_installer as resource_installer_module  # noqa: E402
from modules.pdf.assets import (  # noqa: E402
    PdfAsset,
    PdfAssetManifest,
    PdfAssetManifestError,
    load_pdf_asset_manifest,
    pdf_asset_pack_path,
    select_pdf_assets,
)

CHUNK_SIZE = resource_installer_module.PDF_RESOURCE_CHUNK_SIZE
PdfAssetSyncError = resource_installer_module.PdfAssetSyncError
PdfAssetSyncResult = resource_installer_module.PdfAssetSyncResult
URLError = _URLError


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
    """兼容旧 CLI 调用, 实际下载与校验复用 backend 安装器实现。"""

    return resource_installer_module.sync_pdf_asset(
        asset,
        url=url,
        destination=destination,
        timeout=timeout,
        open_url=urlopen,
        replace_file=os.replace,
    )


def sync_pdf_asset_pack(
    manifest: PdfAssetManifest,
    pack_path: Path,
    *,
    assets: Sequence[PdfAsset] | None = None,
    base_url: str | None = None,
    timeout: float = 120.0,
) -> tuple[PdfAssetSyncResult, ...]:
    """兼容旧 CLI contract, 按主源、fallback、upstream 顺序同步所选资源。"""

    return resource_installer_module.sync_pdf_asset_pack(
        manifest,
        pack_path,
        assets=assets,
        base_url=base_url,
        timeout=timeout,
        open_url=urlopen,
        replace_file=os.replace,
    )


def _stream_asset(response: BinaryIO, target: BinaryIO, asset: PdfAsset) -> None:
    """保留旧脚本测试入口, 复用 backend 的流式完整性校验。"""

    resource_installer_module._stream_pdf_asset(response, target, asset)


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
