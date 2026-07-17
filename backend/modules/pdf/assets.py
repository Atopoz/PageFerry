"""定义 PDF 大型资源包的 manifest、版本化路径与完整性 contract。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

PDF_ASSET_MANIFEST_SCHEMA_VERSION = 1
PDF_ASSET_DIRECTORY_NAME = "pdf"
DEFAULT_PDF_ASSET_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2] / "resources" / "pdf_assets" / "manifest.json"
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class PdfAssetManifestError(ValueError):
    """表示 PDF 资源 manifest 不满足固定下载与落盘 contract。"""


@dataclass(frozen=True, slots=True)
class PdfAsset:
    """描述资源包中一个可独立校验的大型文件。"""

    asset_id: str
    pack: str
    relative_path: Path
    distribution_path: str
    size_bytes: int
    sha256: str
    upstream_url: str | None = None


@dataclass(frozen=True, slots=True)
class PdfAssetManifest:
    """描述一个不可变、可安装到版本化目录的 PDF 资源包。"""

    pack_id: str
    pack_revision: str
    default_base_url: str | None
    assets: tuple[PdfAsset, ...]


def load_default_pdf_asset_manifest() -> PdfAssetManifest:
    """读取随 PageFerry 代码发布的唯一 canonical PDF 资源 manifest。"""

    return load_pdf_asset_manifest(DEFAULT_PDF_ASSET_MANIFEST_PATH)


def load_pdf_asset_manifest(path: Path) -> PdfAssetManifest:
    """读取并严格验证仓库内的 PDF 资源包 manifest。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PdfAssetManifestError(f"无法读取 PDF 资源 manifest: {path}") from error
    if not isinstance(payload, Mapping):
        raise PdfAssetManifestError("PDF 资源 manifest 顶层必须是 object")
    return parse_pdf_asset_manifest(payload)


def parse_pdf_asset_manifest(payload: Mapping[str, object]) -> PdfAssetManifest:
    """把已解码的 JSON object 转换为经过边界检查的 manifest。"""

    schema_version = payload.get("schema_version")
    if schema_version != PDF_ASSET_MANIFEST_SCHEMA_VERSION:
        raise PdfAssetManifestError("PDF 资源 manifest schema_version 不受支持")

    pack_id = _required_identifier(payload.get("pack_id"), "pack_id")
    pack_revision = _required_identifier(payload.get("pack_revision"), "pack_revision")
    raw_default_base_url = payload.get("default_base_url")
    default_base_url = (
        None
        if raw_default_base_url is None
        else normalize_pdf_asset_base_url(
            _required_string(raw_default_base_url, "default_base_url")
        )
    )

    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise PdfAssetManifestError("PDF 资源 manifest assets 必须是非空 array")

    assets: list[PdfAsset] = []
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    for index, raw_asset in enumerate(raw_assets):
        if not isinstance(raw_asset, Mapping):
            raise PdfAssetManifestError(f"assets[{index}] 必须是 object")
        asset = _parse_pdf_asset(raw_asset, index)
        if asset.asset_id in seen_ids:
            raise PdfAssetManifestError(f"PDF 资源 asset_id 重复: {asset.asset_id}")
        if asset.relative_path in seen_paths:
            raise PdfAssetManifestError(f"PDF 资源 relative_path 重复: {asset.relative_path}")
        seen_ids.add(asset.asset_id)
        seen_paths.add(asset.relative_path)
        assets.append(asset)

    return PdfAssetManifest(
        pack_id=pack_id,
        pack_revision=pack_revision,
        default_base_url=default_base_url,
        assets=tuple(assets),
    )


def pdf_asset_pack_path(app_data_dir: Path, manifest: PdfAssetManifest) -> Path:
    """在 app-data 下为指定 pack 解析不可变的版本化安装目录。"""

    return app_data_dir.expanduser().resolve() / PDF_ASSET_DIRECTORY_NAME / manifest.pack_revision


def pdf_asset_path(pack_path: Path, asset: PdfAsset) -> Path:
    """把已经验证过的逻辑相对路径解析到 pack 目录内。"""

    return pack_path / asset.relative_path


def pdf_asset_download_url(
    manifest: PdfAssetManifest,
    asset: PdfAsset,
    *,
    base_url: str | None = None,
) -> str:
    """使用 manifest 默认地址或显式 base URL 生成固定资产 URL。"""

    selected_base = base_url or manifest.default_base_url
    if selected_base is not None:
        normalized_base = normalize_pdf_asset_base_url(selected_base)
        return urljoin(normalized_base, asset.distribution_path)
    if asset.upstream_url is not None:
        return asset.upstream_url
    raise PdfAssetManifestError(f"PDF 资源 {asset.asset_id} 没有默认来源, 必须显式提供 base URL")


def find_pdf_asset(manifest: PdfAssetManifest, asset_id: str) -> PdfAsset:
    """按稳定 ID 查找单个资源, 未声明时明确失败。"""

    for asset in manifest.assets:
        if asset.asset_id == asset_id:
            return asset
    raise PdfAssetManifestError(f"PDF 资源 asset_id 不存在: {asset_id}")


def select_pdf_assets(
    manifest: PdfAssetManifest,
    *,
    asset_ids: Sequence[str] = (),
    packs: Sequence[str] = (),
) -> tuple[PdfAsset, ...]:
    """按 asset ID 或 pack group 选择资源, 并保持 manifest 顺序。"""

    requested_ids = set(asset_ids)
    requested_packs = set(packs)
    known_ids = {asset.asset_id for asset in manifest.assets}
    known_packs = {asset.pack for asset in manifest.assets}
    unknown_ids = requested_ids - known_ids
    unknown_packs = requested_packs - known_packs
    if unknown_ids:
        raise PdfAssetManifestError(f"PDF 资源 asset_id 不存在: {', '.join(sorted(unknown_ids))}")
    if unknown_packs:
        raise PdfAssetManifestError(f"PDF 资源 pack 不存在: {', '.join(sorted(unknown_packs))}")
    if not requested_ids and not requested_packs:
        return manifest.assets
    return tuple(
        asset
        for asset in manifest.assets
        if asset.asset_id in requested_ids or asset.pack in requested_packs
    )


def normalize_pdf_asset_base_url(base_url: str) -> str:
    """只接受不含凭据、query 或 fragment 的 HTTPS base URL。"""

    parsed = urlsplit(base_url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise PdfAssetManifestError("PDF 资源 base URL 必须是有效 HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise PdfAssetManifestError("PDF 资源 base URL 不能包含凭据")
    if parsed.query or parsed.fragment:
        raise PdfAssetManifestError("PDF 资源 base URL 不能包含 query 或 fragment")
    path = f"{parsed.path.rstrip('/')}/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def hash_pdf_asset(path: Path) -> tuple[str, int]:
    """流式计算资产 SHA-256 与字节数, 避免一次读入大型文件。"""

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def is_pdf_asset_valid(path: Path, asset: PdfAsset) -> bool:
    """检查现有文件是否与 manifest 固定的 size 和 SHA-256 一致。"""

    if not path.is_file() or path.stat().st_size != asset.size_bytes:
        return False
    digest, size = hash_pdf_asset(path)
    return size == asset.size_bytes and digest == asset.sha256


def is_pdf_asset_pack_valid(
    pack_path: Path,
    manifest: PdfAssetManifest,
    *,
    assets: Sequence[PdfAsset] | None = None,
) -> bool:
    """只有所选资产全部通过完整性检查时才把对应 pack group 视为可用。"""

    selected_assets = assets if assets is not None else manifest.assets
    return all(
        is_pdf_asset_valid(pdf_asset_path(pack_path, asset), asset) for asset in selected_assets
    )


def _parse_pdf_asset(raw_asset: Mapping[str, object], index: int) -> PdfAsset:
    """验证并构造单个 manifest asset。"""

    asset_id = _required_identifier(raw_asset.get("asset_id"), f"assets[{index}].asset_id")
    pack = _required_identifier(raw_asset.get("pack"), f"assets[{index}].pack")
    relative_path = _safe_relative_path(
        raw_asset.get("relative_path"),
        f"assets[{index}].relative_path",
    )
    distribution_path = _safe_download_path(
        raw_asset.get("distribution_path"),
        f"assets[{index}].distribution_path",
    )
    size_bytes = raw_asset.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
        raise PdfAssetManifestError(f"assets[{index}].size_bytes 必须是正整数")
    sha256 = _required_string(raw_asset.get("sha256"), f"assets[{index}].sha256").lower()
    if _SHA256_RE.fullmatch(sha256) is None:
        raise PdfAssetManifestError(f"assets[{index}].sha256 必须是 64 位十六进制字符串")
    raw_upstream_url = raw_asset.get("upstream_url")
    upstream_url = (
        None
        if raw_upstream_url is None
        else _safe_asset_url(
            _required_string(raw_upstream_url, f"assets[{index}].upstream_url"),
            f"assets[{index}].upstream_url",
        )
    )
    return PdfAsset(
        asset_id=asset_id,
        pack=pack,
        relative_path=relative_path,
        distribution_path=distribution_path,
        size_bytes=size_bytes,
        sha256=sha256,
        upstream_url=upstream_url,
    )


def _required_identifier(value: object, field: str) -> str:
    """读取适合安全目录名和稳定 ID 的短标识。"""

    text = _required_string(value, field)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise PdfAssetManifestError(f"{field} 只能包含字母、数字、点、下划线和连字符")
    return text


def _required_string(value: object, field: str) -> str:
    """读取不允许空白的字符串字段。"""

    if not isinstance(value, str) or not value.strip():
        raise PdfAssetManifestError(f"{field} 必须是非空字符串")
    return value.strip()


def _safe_relative_path(value: object, field: str) -> Path:
    """拒绝绝对路径、反斜杠与目录穿越后返回平台路径。"""

    text = _required_string(value, field)
    if "\\" in text:
        raise PdfAssetManifestError(f"{field} 必须使用正斜杠")
    parts = text.split("/")
    if text.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise PdfAssetManifestError(f"{field} 必须是安全相对路径")
    return Path(*parts)


def _safe_download_path(value: object, field: str) -> str:
    """拒绝会逃逸 base URL 或携带额外 URL 状态的下载路径。"""

    text = _required_string(value, field)
    parsed = urlsplit(text)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or text.startswith("/")
        or "\\" in text
    ):
        raise PdfAssetManifestError(f"{field} 必须是 base URL 下的相对路径")
    decoded_parts = unquote(parsed.path).split("/")
    if any(part in {"", ".", ".."} for part in decoded_parts):
        raise PdfAssetManifestError(f"{field} 必须是安全 URL 相对路径")
    return parsed.path


def _safe_asset_url(value: str, field: str) -> str:
    """验证单资产 upstream fallback URL, 不允许凭据与动态参数。"""

    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise PdfAssetManifestError(f"{field} 必须是有效 HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise PdfAssetManifestError(f"{field} 不能包含凭据")
    if parsed.query or parsed.fragment:
        raise PdfAssetManifestError(f"{field} 不能包含 query 或 fragment")
    return value
