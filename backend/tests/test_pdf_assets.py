"""验证 PDF 资源包 manifest、版本路径与完整性 contract。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from modules.pdf.assets import (
    PdfAssetManifestError,
    find_pdf_asset,
    is_pdf_asset_pack_valid,
    load_default_pdf_asset_manifest,
    load_pdf_asset_manifest,
    parse_pdf_asset_manifest,
    pdf_asset_download_url,
    pdf_asset_download_urls,
    pdf_asset_pack_path,
    pdf_asset_path,
    select_pdf_assets,
)

MANIFEST_PATH = Path(__file__).parents[1] / "resources" / "pdf_assets" / "manifest.json"


def _manifest_payload(expected: bytes = b"asset") -> dict[str, object]:
    """生成只供测试使用的小型资源包 manifest。"""

    return {
        "schema_version": 1,
        "pack_id": "test-pack",
        "pack_revision": "7",
        "default_base_url": "https://assets.example.test/pdf/7/",
        "assets": [
            {
                "asset_id": "layout-model",
                "pack": "layout",
                "relative_path": "layout/model.onnx",
                "distribution_path": "layout/model.onnx",
                "size_bytes": len(expected),
                "sha256": hashlib.sha256(expected).hexdigest(),
            }
        ],
    }


def test_repository_manifest_pins_versioned_layout_asset() -> None:
    """仓库 manifest 应固定 pack 版本、模型路径、size 与 SHA-256。"""

    manifest = load_pdf_asset_manifest(MANIFEST_PATH)

    assert manifest.pack_id == "pdf-runtime"
    assert manifest.pack_revision == "2026.07.18.2"
    assert manifest.default_base_url == "https://assets.pageferry.download/pdf/2026.07.18.2/"
    assert len(manifest.assets) == 18
    asset = manifest.assets[0]
    assert asset.relative_path == Path("layout/PP-DocLayoutV3/inference.onnx")
    assert asset.size_bytes == 130_502_049
    assert asset.sha256 == "45bf71750b00739a41fc209f132eb104a4d6b5bb29483c9078164d8b87cf28ba"
    font_assets = tuple(item for item in manifest.assets if item.relative_path.parts[0] == "fonts")
    assert len(font_assets) == 17
    assert sum(item.size_bytes for item in font_assets) == 75_544_872
    assert all(item.relative_path.parts[0] == "fonts" for item in font_assets)
    assert load_default_pdf_asset_manifest() == manifest


def test_asset_selection_supports_ids_and_pack_groups() -> None:
    """同步调用方可以只选择单个 asset 或 layout/fonts pack。"""

    manifest = load_pdf_asset_manifest(MANIFEST_PATH)

    assert [item.asset_id for item in select_pdf_assets(manifest, packs=("layout",))] == [
        "pp-doclayout-v3-onnx"
    ]
    assert len(select_pdf_assets(manifest, packs=("fonts-common-zh-cn",))) == 5
    assert len(select_pdf_assets(manifest, packs=("fonts-ja",))) == 2
    selected = select_pdf_assets(manifest, asset_ids=("noto-sans-regular",))
    assert selected == (find_pdf_asset(manifest, "noto-sans-regular"),)
    with pytest.raises(PdfAssetManifestError, match="pack 不存在"):
        select_pdf_assets(manifest, packs=("unknown",))


def test_pack_path_is_versioned_below_app_data(tmp_path: Path) -> None:
    """大文件必须进入 app-data 下由 pack id 与版本隔离的目录。"""

    manifest = parse_pdf_asset_manifest(_manifest_payload())
    app_data = tmp_path / "app-data"

    pack_path = pdf_asset_pack_path(app_data, manifest)

    assert pack_path == app_data / "pdf" / "7"
    assert pdf_asset_path(pack_path, manifest.assets[0]) == pack_path / "layout/model.onnx"


def test_base_url_override_keeps_manifest_download_path() -> None:
    """自有 CDN override 只能替换 base, 资产相对路径仍由 manifest 固定。"""

    manifest = parse_pdf_asset_manifest(_manifest_payload())

    url = pdf_asset_download_url(
        manifest,
        manifest.assets[0],
        base_url="https://mirror.example.test/releases/v7",
    )

    assert url == "https://mirror.example.test/releases/v7/layout/model.onnx"


def test_download_urls_keep_order_and_remove_duplicates() -> None:
    """下载候选应依次使用主分发、fallback 与 upstream, 并跳过重复 URL。"""

    payload = _manifest_payload()
    assets = payload["assets"]
    assert isinstance(assets, list)
    asset = assets[0]
    assert isinstance(asset, dict)
    asset["fallback_urls"] = [
        "https://github.com/pageferry/assets/releases/download/v7/model.onnx",
        "https://upstream.example.test/model.onnx",
        "https://github.com/pageferry/assets/releases/download/v7/model.onnx",
    ]
    asset["upstream_url"] = "https://upstream.example.test/model.onnx"
    manifest = parse_pdf_asset_manifest(payload)

    urls = pdf_asset_download_urls(manifest, manifest.assets[0])

    assert urls == (
        "https://assets.example.test/pdf/7/layout/model.onnx",
        "https://github.com/pageferry/assets/releases/download/v7/model.onnx",
        "https://upstream.example.test/model.onnx",
    )
    assert manifest.assets[0].fallback_urls == urls[1:]
    assert pdf_asset_download_url(manifest, manifest.assets[0]) == urls[0]


@pytest.mark.parametrize(
    "fallback_urls",
    [
        "https://github.com/pageferry/model.onnx",
        ["http://github.com/pageferry/model.onnx"],
        ["https://user:secret@github.com/pageferry/model.onnx"],
        ["https://github.com/pageferry/model.onnx?download=1"],
        ["https://github.com/pageferry/model.onnx#sha256"],
        ["https://github.com/pageferry/releases/"],
        ["https://github.com/pageferry/../model.onnx"],
        ["https://github.com:invalid/pageferry/model.onnx"],
    ],
)
def test_manifest_rejects_unsafe_fallback_urls(fallback_urls: object) -> None:
    """fallback_urls 必须是无凭据和动态参数的 HTTPS 文件 URL array。"""

    payload = _manifest_payload()
    assets = payload["assets"]
    assert isinstance(assets, list)
    asset = assets[0]
    assert isinstance(asset, dict)
    asset["fallback_urls"] = fallback_urls

    with pytest.raises(PdfAssetManifestError):
        parse_pdf_asset_manifest(payload)


def test_asset_without_upstream_requires_distribution_base_url() -> None:
    """没有主源、fallback 或 upstream 的资产必须 fail closed。"""

    payload = _manifest_payload()
    payload["default_base_url"] = None
    manifest = parse_pdf_asset_manifest(payload)
    asset = manifest.assets[0]

    with pytest.raises(PdfAssetManifestError, match="必须显式提供 base URL"):
        pdf_asset_download_url(manifest, asset)


@pytest.mark.parametrize(
    "field,value",
    [
        ("relative_path", "../model.onnx"),
        ("relative_path", "/model.onnx"),
        ("distribution_path", "%2e%2e/model.onnx"),
        ("distribution_path", "https://evil.example/model.onnx"),
    ],
)
def test_manifest_rejects_unsafe_asset_paths(field: str, value: str) -> None:
    """本地路径与下载路径都不能逃逸 pack 或 base URL。"""

    payload = _manifest_payload()
    assets = payload["assets"]
    assert isinstance(assets, list)
    asset = assets[0]
    assert isinstance(asset, dict)
    asset[field] = value

    with pytest.raises(PdfAssetManifestError):
        parse_pdf_asset_manifest(payload)


def test_manifest_rejects_duplicate_destination() -> None:
    """两个 asset 不能原子替换同一个 pack 文件。"""

    payload = _manifest_payload()
    assets = payload["assets"]
    assert isinstance(assets, list)
    duplicate = dict(assets[0])
    duplicate["asset_id"] = "another-model"
    assets.append(duplicate)

    with pytest.raises(PdfAssetManifestError, match="relative_path 重复"):
        parse_pdf_asset_manifest(payload)


def test_pack_is_valid_only_after_every_asset_matches(tmp_path: Path) -> None:
    """pack 中任一文件缺失或 hash 错误都不能宣称资源版本可用。"""

    expected = b"verified-asset"
    manifest = parse_pdf_asset_manifest(_manifest_payload(expected))
    pack_path = pdf_asset_pack_path(tmp_path, manifest)
    destination = pdf_asset_path(pack_path, manifest.assets[0])

    assert is_pdf_asset_pack_valid(pack_path, manifest) is False
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"wrong")
    assert is_pdf_asset_pack_valid(pack_path, manifest) is False
    destination.write_bytes(expected)
    assert is_pdf_asset_pack_valid(pack_path, manifest) is True


def test_manifest_json_remains_parseable_without_custom_loader() -> None:
    """manifest 应保持普通 JSON, 并携带发布所需的来源与许可证 metadata。"""

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assets = payload["assets"]
    assert assets[0]["source_revision"] == "46bbdf188bb0a772c08aed74882ce7e51a8f1ea6"
    assert assets[0]["license"] == "Apache-2.0"
    assert {asset["license"] for asset in assets[1:]} == {"OFL-1.1"}
    assert (MANIFEST_PATH.parent / "licenses" / "OFL-1.1.txt").is_file()
