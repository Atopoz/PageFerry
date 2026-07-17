"""兼容旧命令名, 使用 canonical PDF resource-pack 同步 layout asset。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYNC_PDF_ASSETS_SCRIPT = PROJECT_ROOT / "scripts" / "sync-pdf-assets.py"


def _load_sync_pdf_assets() -> ModuleType:
    """加载新的 resource-pack 同步入口, 避免复制 manifest 与下载实现。"""

    spec = importlib.util.spec_from_file_location(
        "pageferry_sync_pdf_assets_compat",
        SYNC_PDF_ASSETS_SCRIPT,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载同步脚本: {SYNC_PDF_ASSETS_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_sync_pdf_assets = _load_sync_pdf_assets()
DEFAULT_MANIFEST = _sync_pdf_assets.DEFAULT_MANIFEST


def main(argv: list[str] | None = None) -> int:
    """默认选择 layout asset, 其余参数原样交给 resource-pack 同步器。"""

    forwarded = list(sys.argv[1:] if argv is None else argv)
    has_selector = any(
        argument in {"--asset", "--pack"}
        or argument.startswith("--asset=")
        or argument.startswith("--pack=")
        for argument in forwarded
    )
    if not has_selector:
        forwarded.extend(("--asset", "pp-doclayout-v3-onnx"))
    return _sync_pdf_assets.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
