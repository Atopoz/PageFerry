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


def _add_fallback_url(manifest_path: Path, fallback_url: str) -> None:
    """给测试 manifest 的唯一资产增加 fallback 下载地址。"""

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assets = payload["assets"]
    assert isinstance(assets, list)
    asset = assets[0]
    assert isinstance(asset, dict)
    asset["fallback_urls"] = [fallback_url]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")


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


def test_sync_pack_does_not_request_fallback_after_primary_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主分发返回有效内容后不应继续请求 GitHub fallback。"""

    expected = b"primary-asset"
    manifest_path = _write_manifest(tmp_path, expected)
    _add_fallback_url(
        manifest_path,
        "https://github.com/pageferry/assets/releases/download/v3/inference.onnx",
    )
    manifest = load_pdf_asset_manifest(manifest_path)
    pack_path = sync_pdf_assets.resolve_pack_destination(manifest, tmp_path / "app-data")
    requested_urls: list[str] = []

    def fake_urlopen(request: object, timeout: float) -> io.BytesIO:
        """记录唯一一次主源请求并返回有效内容。"""

        assert isinstance(request, Request)
        assert timeout == 120.0
        requested_urls.append(request.full_url)
        return io.BytesIO(expected)

    monkeypatch.setattr(sync_pdf_assets, "urlopen", fake_urlopen)

    sync_pdf_assets.sync_pdf_asset_pack(manifest, pack_path)

    assert requested_urls == ["https://origin.example.test/pdf/3/layout/inference.onnx"]


def test_sync_pack_falls_back_to_github_after_primary_network_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主源连接失败时应继续从 GitHub fallback 下载。"""

    expected = b"github-asset"
    fallback_url = "https://github.com/pageferry/assets/releases/download/v3/inference.onnx"
    manifest_path = _write_manifest(tmp_path, expected)
    _add_fallback_url(manifest_path, fallback_url)
    manifest = load_pdf_asset_manifest(manifest_path)
    pack_path = sync_pdf_assets.resolve_pack_destination(manifest, tmp_path / "app-data")
    requested_urls: list[str] = []

    def fake_urlopen(request: object, timeout: float) -> io.BytesIO:
        """让主源连接失败, GitHub fallback 返回有效内容。"""

        assert isinstance(request, Request)
        assert timeout == 120.0
        requested_urls.append(request.full_url)
        if request.full_url != fallback_url:
            raise sync_pdf_assets.URLError("origin unavailable")
        return io.BytesIO(expected)

    monkeypatch.setattr(sync_pdf_assets, "urlopen", fake_urlopen)

    results = sync_pdf_assets.sync_pdf_asset_pack(manifest, pack_path)

    assert results[0].downloaded is True
    assert requested_urls == [
        "https://origin.example.test/pdf/3/layout/inference.onnx",
        fallback_url,
    ]
    assert results[0].path.read_bytes() == expected


def test_sync_pack_falls_back_after_primary_integrity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主源内容校验失败也应清理临时文件并继续尝试 fallback。"""

    expected = b"verified-content"
    fallback_url = "https://github.com/pageferry/assets/releases/download/v3/inference.onnx"
    manifest_path = _write_manifest(tmp_path, expected)
    _add_fallback_url(manifest_path, fallback_url)
    manifest = load_pdf_asset_manifest(manifest_path)
    pack_path = sync_pdf_assets.resolve_pack_destination(manifest, tmp_path / "app-data")
    destination = pdf_asset_path(pack_path, manifest.assets[0])

    def fake_urlopen(request: object, timeout: float) -> io.BytesIO:
        """主源返回同长度坏内容, fallback 返回通过 SHA-256 的内容。"""

        assert isinstance(request, Request)
        assert timeout == 120.0
        if request.full_url == fallback_url:
            return io.BytesIO(expected)
        return io.BytesIO(b"x" * len(expected))

    monkeypatch.setattr(sync_pdf_assets, "urlopen", fake_urlopen)

    results = sync_pdf_assets.sync_pdf_asset_pack(manifest, pack_path)

    assert results[0].downloaded is True
    assert destination.read_bytes() == expected
    assert not list(destination.parent.glob("*.download"))


def test_sync_pack_keeps_existing_file_when_hash_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """镜像返回错误内容时必须保留旧文件并清理临时下载。"""

    expected = b"good-asset"
    received = b"evil-asset"
    manifest_path = _write_manifest(tmp_path, expected)
    _add_fallback_url(
        manifest_path,
        "https://github.com/pageferry/assets/releases/download/v3/inference.onnx",
    )
    manifest = load_pdf_asset_manifest(manifest_path)
    pack_path = sync_pdf_assets.resolve_pack_destination(manifest, tmp_path / "app-data")
    destination = pdf_asset_path(pack_path, manifest.assets[0])
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old-asset")

    def fake_urlopen(_request: object, timeout: float) -> io.BytesIO:
        """返回 size 相同但 SHA-256 不同的内容。"""

        assert timeout == 120.0
        return io.BytesIO(received)

    monkeypatch.setattr(sync_pdf_assets, "urlopen", fake_urlopen)

    with pytest.raises(sync_pdf_assets.PdfAssetSyncError, match="所有下载来源均失败"):
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
