"""验证 PDF 资源包同步脚本的镜像、复用与原子落盘行为。"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from urllib.request import Request

import pytest

from modules.pdf.assets import load_pdf_asset_manifest, pdf_asset_path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "sync-pdf-assets.py"


def _load_script() -> ModuleType:
    """把带连字符的同步脚本作为独立 module 加载。"""

    spec = importlib.util.spec_from_file_location("pageferry_sync_pdf_assets", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本: {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync_pdf_assets = _load_script()


def _write_manifest(tmp_path: Path, expected: bytes) -> Path:
    """写入使用小型 fixture 的资源包 manifest。"""

    payload = {
        "schema_version": 1,
        "pack_id": "test-pack",
        "pack_revision": "3",
        "default_base_url": "https://origin.example.test/pdf/3/",
        "assets": [
            {
                "asset_id": "layout-model",
                "pack": "layout",
                "relative_path": "layout/inference.onnx",
                "distribution_path": "layout/inference.onnx",
                "size_bytes": len(expected),
                "sha256": hashlib.sha256(expected).hexdigest(),
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_sync_pack_skips_network_for_existing_valid_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """版本目录内已有有效文件时必须直接复用。"""

    expected = b"already-valid"
    manifest_path = _write_manifest(tmp_path, expected)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["default_base_url"] = None
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    manifest = load_pdf_asset_manifest(manifest_path)
    pack_path = sync_pdf_assets.resolve_pack_destination(manifest, tmp_path / "app-data")
    destination = pdf_asset_path(pack_path, manifest.assets[0])
    destination.parent.mkdir(parents=True)
    destination.write_bytes(expected)

    def fail_urlopen(*_args: object, **_kwargs: object) -> None:
        """检测到意外网络请求时立即失败。"""

        raise AssertionError("有效资源不应访问网络")

    monkeypatch.setattr(sync_pdf_assets, "urlopen", fail_urlopen)

    results = sync_pdf_assets.sync_pdf_asset_pack(manifest, pack_path)

    assert results[0].downloaded is False
    assert destination.read_bytes() == expected


def test_sync_pack_uses_base_override_and_atomically_replaces_invalid_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """镜像地址下载的有效内容应从同目录临时文件原子替换。"""

    expected = b"replacement-asset"
    manifest = load_pdf_asset_manifest(_write_manifest(tmp_path, expected))
    pack_path = sync_pdf_assets.resolve_pack_destination(manifest, tmp_path / "app-data")
    destination = pdf_asset_path(pack_path, manifest.assets[0])
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old")
    replace_calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def fake_urlopen(request: object, timeout: float) -> io.BytesIO:
        """确认 override URL 与 timeout 后返回有效内容。"""

        assert isinstance(request, Request)
        assert request.full_url == ("https://mirror.example.test/releases/3/layout/inference.onnx")
        assert timeout == 9.0
        return io.BytesIO(expected)

    def recording_replace(source: str | Path, target: str | Path) -> None:
        """记录原子替换路径并执行真实替换。"""

        replace_calls.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(sync_pdf_assets, "urlopen", fake_urlopen)
    monkeypatch.setattr(sync_pdf_assets.os, "replace", recording_replace)

    results = sync_pdf_assets.sync_pdf_asset_pack(
        manifest,
        pack_path,
        base_url="https://mirror.example.test/releases/3",
        timeout=9.0,
    )

    assert results[0].downloaded is True
    assert destination.read_bytes() == expected
    assert replace_calls[0][0].parent == destination.parent
    assert replace_calls[0][1] == destination
    assert not list(destination.parent.glob("*.download"))


def test_sync_pack_keeps_existing_file_when_hash_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """镜像返回错误内容时必须保留旧文件并清理临时下载。"""

    expected = b"good-asset"
    received = b"evil-asset"
    manifest = load_pdf_asset_manifest(_write_manifest(tmp_path, expected))
    pack_path = sync_pdf_assets.resolve_pack_destination(manifest, tmp_path / "app-data")
    destination = pdf_asset_path(pack_path, manifest.assets[0])
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old-asset")

    def fake_urlopen(_request: object, timeout: float) -> io.BytesIO:
        """返回 size 相同但 SHA-256 不同的内容。"""

        assert timeout == 120.0
        return io.BytesIO(received)

    monkeypatch.setattr(sync_pdf_assets, "urlopen", fake_urlopen)

    with pytest.raises(sync_pdf_assets.PdfAssetSyncError, match="SHA-256 校验失败"):
        sync_pdf_assets.sync_pdf_asset_pack(manifest, pack_path)

    assert destination.read_bytes() == b"old-asset"
    assert not list(destination.parent.glob("*.download"))


def test_default_destination_uses_versioned_platform_app_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未指定 data-dir 时应落到平台 app-data 的版本化 pack 目录。"""

    manifest = load_pdf_asset_manifest(_write_manifest(tmp_path, b"asset"))
    app_data = tmp_path / "PageFerry"

    def fake_user_data_path(*_args: object, **_kwargs: object) -> Path:
        """为默认路径解析返回隔离目录。"""

        return app_data

    monkeypatch.setattr(sync_pdf_assets, "user_data_path", fake_user_data_path)

    assert sync_pdf_assets.resolve_pack_destination(manifest, None) == (app_data / "pdf" / "3")
