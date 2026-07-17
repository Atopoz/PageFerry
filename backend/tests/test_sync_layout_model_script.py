"""验证旧 layout 同步命令复用 canonical PDF resource-pack 入口。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "sync-layout-model.py"
MANIFEST_PATH = PROJECT_ROOT / "backend" / "resources" / "pdf_assets" / "manifest.json"


def _load_script() -> ModuleType:
    """把兼容脚本作为独立 module 加载以测试参数转发。"""

    spec = importlib.util.spec_from_file_location("pageferry_sync_layout_model", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本: {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync_layout_model = _load_script()


def test_compat_command_uses_canonical_manifest() -> None:
    """旧命令不能保留第二份 layout size/hash manifest。"""

    assert sync_layout_model.DEFAULT_MANIFEST == MANIFEST_PATH


def test_compat_command_selects_layout_asset_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 selector 时旧命令只同步 layout asset。"""

    captured: list[str] = []

    def fake_main(argv: list[str]) -> int:
        """记录兼容入口最终传给新同步器的参数。"""

        captured.extend(argv)
        return 0

    monkeypatch.setattr(sync_layout_model._sync_pdf_assets, "main", fake_main)

    assert sync_layout_model.main(["--timeout", "7"]) == 0
    assert captured == ["--timeout", "7", "--asset", "pp-doclayout-v3-onnx"]


def test_compat_command_preserves_explicit_pack_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调用方显式选择字体 group 时不能被兼容入口改回 layout。"""

    captured: list[str] = []

    def fake_main(argv: list[str]) -> int:
        """记录显式 selector。"""

        captured.extend(argv)
        return 0

    monkeypatch.setattr(sync_layout_model._sync_pdf_assets, "main", fake_main)

    assert sync_layout_model.main(["--pack", "fonts-ja"]) == 0
    assert captured == ["--pack", "fonts-ja"]
