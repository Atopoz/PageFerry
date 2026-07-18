"""把经过 manifest 校验的 PageFerry PDF 资源发布到 GitHub Release。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
DEFAULT_MANIFEST = BACKEND_ROOT / "resources" / "pdf_assets" / "manifest.json"
DEFAULT_LICENSES_DIR = DEFAULT_MANIFEST.parent / "licenses"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from modules.pdf.assets import (  # noqa: E402
    PdfAssetManifest,
    PdfAssetManifestError,
    hash_pdf_asset,
    load_pdf_asset_manifest,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]

_GITHUB_REPO_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
    r"/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})\Z"
)
_GITHUB_TAG_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})\Z")


class GitHubPublishError(RuntimeError):
    """表示发布前校验、GitHub API 查询或 Release 上传失败。"""


@dataclass(frozen=True, slots=True)
class GitHubPublishAsset:
    """描述 GitHub Release 中一个不可变的本地发布文件。"""

    name: str
    source: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class GitHubRemoteAsset:
    """记录 GitHub API 返回的 Release asset 完整性 metadata。"""

    name: str
    size_bytes: int
    digest: str | None


@dataclass(frozen=True, slots=True)
class GitHubReleaseState:
    """记录目标 Release 的 draft 状态与已有 assets。"""

    draft: bool
    assets: tuple[GitHubRemoteAsset, ...]


@dataclass(frozen=True, slots=True)
class GitHubPublishResult:
    """记录一个 Release asset 本次是上传还是复用。"""

    name: str
    uploaded: bool


def build_publish_plan(
    repo: str,
    tag: str,
    manifest_path: Path,
    source_dir: Path | None,
    licenses_dir: Path,
) -> tuple[PdfAssetManifest, tuple[GitHubPublishAsset, ...]]:
    """校验 URL 与全部本地文件后生成二进制、license、manifest 发布顺序。"""

    _validate_repo_and_tag(repo, tag)
    manifest_path = manifest_path.expanduser().resolve()
    licenses_dir = licenses_dir.expanduser().resolve()
    manifest = load_pdf_asset_manifest(manifest_path)
    resolved_source_dir = (
        source_dir.expanduser().resolve()
        if source_dir is not None
        else PROJECT_ROOT / ".data" / "pdf" / manifest.pack_revision
    )

    binary_assets: list[GitHubPublishAsset] = []
    seen_binary_names: set[str] = set()
    for asset in manifest.assets:
        name = Path(asset.distribution_path).name
        if name in seen_binary_names:
            raise GitHubPublishError(f"GitHub Release 二进制 basename 重复: {name}")
        seen_binary_names.add(name)

        expected_url = _release_download_url(repo, tag, name)
        if asset.fallback_urls != (expected_url,):
            raise GitHubPublishError(
                f"PDF 资源 fallback_urls 必须且只能声明目标 GitHub Release URL: "
                f"{asset.asset_id}: expected={expected_url}"
            )

        source = resolved_source_dir / asset.relative_path
        if source.name != name:
            raise GitHubPublishError(
                f"PDF 资源本地文件名与 GitHub Release basename 不一致: {asset.asset_id}"
            )
        _verify_local_file(
            source,
            expected_size=asset.size_bytes,
            expected_sha256=asset.sha256,
            label=asset.asset_id,
        )
        binary_assets.append(
            GitHubPublishAsset(
                name=name,
                source=source,
                size_bytes=asset.size_bytes,
                sha256=asset.sha256,
            )
        )

    license_assets = tuple(_local_publish_asset(path) for path in _license_files(licenses_dir))
    manifest_asset = _local_publish_asset(manifest_path)
    assets = (*binary_assets, *license_assets, manifest_asset)
    _reject_duplicate_release_names(assets)
    return manifest, assets


def publish_plan(
    repo: str,
    tag: str,
    manifest: PdfAssetManifest,
    assets: Sequence[GitHubPublishAsset],
    *,
    runner: Runner = subprocess.run,
) -> tuple[GitHubPublishResult, ...]:
    """创建或读取 Release, 复用匹配资产并只上传缺失项。"""

    _validate_repo_and_tag(repo, tag)
    release = _fetch_release(repo, tag, runner=runner)
    if release is None:
        _create_draft_release(repo, tag, manifest.pack_revision, runner=runner)
        release = _fetch_release(repo, tag, runner=runner)
        if release is None:
            raise GitHubPublishError("GitHub draft Release 创建后仍无法读取")

    existing = _remote_assets_by_name(release.assets)
    missing: list[GitHubPublishAsset] = []
    results_by_name: dict[str, GitHubPublishResult] = {}
    for asset in assets:
        remote_asset = existing.get(asset.name)
        if remote_asset is None:
            missing.append(asset)
            continue
        _verify_remote_asset(asset, remote_asset)
        results_by_name[asset.name] = GitHubPublishResult(name=asset.name, uploaded=False)

    # 先检查完所有已有名称冲突, 再对远端产生任何上传写入。
    for asset in missing:
        _verify_local_file(
            asset.source,
            expected_size=asset.size_bytes,
            expected_sha256=asset.sha256,
            label=asset.name,
        )
        _upload_release_asset(repo, tag, asset, runner=runner)
        results_by_name[asset.name] = GitHubPublishResult(name=asset.name, uploaded=True)

    confirmed_release = _fetch_release(repo, tag, runner=runner)
    if confirmed_release is None:
        raise GitHubPublishError("上传后无法读取 GitHub Release")
    _verify_complete_release(assets, confirmed_release)

    if confirmed_release.draft:
        _publish_draft_release(repo, tag, runner=runner)
        published_release = _fetch_release(repo, tag, runner=runner)
        if published_release is None or published_release.draft:
            raise GitHubPublishError("GitHub draft Release 未成功发布")
        _verify_complete_release(assets, published_release)

    return tuple(results_by_name[asset.name] for asset in assets)


def publish_pdf_assets(
    repo: str,
    tag: str,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    source_dir: Path | None = None,
    licenses_dir: Path = DEFAULT_LICENSES_DIR,
    runner: Runner = subprocess.run,
) -> tuple[GitHubPublishResult, ...]:
    """先完成全部本地 fail-fast 校验, 再发布 canonical PDF 资源。"""

    manifest, assets = build_publish_plan(
        repo,
        tag,
        manifest_path,
        source_dir,
        licenses_dir,
    )
    return publish_plan(repo, tag, manifest, assets, runner=runner)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析目标 GitHub repository、Release tag 与可选资源目录。"""

    parser = argparse.ArgumentParser(
        description="校验并发布 PDF 资源到 GitHub Release, 同名内容冲突时拒绝覆盖。"
    )
    parser.add_argument("--repo", required=True, help="目标 GitHub repository, 格式 OWNER/REPO。")
    parser.add_argument("--tag", required=True, help="目标 PDF 资源 Release tag。")
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="版本化资源根目录; 默认使用 .data/pdf/<pack_revision>。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行一次 GitHub Release 发布并报告上传与复用数量。"""

    args = parse_args(argv)
    try:
        results = publish_pdf_assets(
            args.repo,
            args.tag,
            source_dir=args.source_dir,
        )
    except (OSError, ValueError, PdfAssetManifestError, GitHubPublishError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    uploaded = sum(result.uploaded for result in results)
    reused = len(results) - uploaded
    print(
        f"PDF GitHub 资源发布完成: uploaded={uploaded}, reused={reused}, "
        f"repo={args.repo}, tag={args.tag}"
    )
    return 0


def _validate_repo_and_tag(repo: str, tag: str) -> None:
    """只接受不会被 gh 解释成 option 或 URL 路径的 repository 与 tag。"""

    if _GITHUB_REPO_RE.fullmatch(repo) is None:
        raise GitHubPublishError("GitHub repository 必须使用安全的 OWNER/REPO 格式")
    if _GITHUB_TAG_RE.fullmatch(tag) is None:
        raise GitHubPublishError("GitHub Release tag 只能包含字母、数字、点、下划线和连字符")


def _release_download_url(repo: str, tag: str, name: str) -> str:
    """生成 manifest 必须固定声明的 GitHub Release 下载 URL。"""

    return f"https://github.com/{repo}/releases/download/{tag}/{name}"


def _verify_local_file(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    label: str,
) -> None:
    """用 size 与 SHA-256 校验本地普通文件, 并拒绝符号链接。"""

    if path.is_symlink() or not path.is_file():
        raise GitHubPublishError(f"本地 PDF 发布文件不存在或不是普通文件: {label}: {path}")
    actual_sha256, actual_size = hash_pdf_asset(path)
    if actual_size != expected_size:
        raise GitHubPublishError(
            f"本地 PDF 发布文件 size 校验失败: {label}: "
            f"expected={expected_size}, actual={actual_size}"
        )
    if actual_sha256 != expected_sha256:
        raise GitHubPublishError(
            f"本地 PDF 发布文件 SHA-256 校验失败: {label}: "
            f"expected={expected_sha256}, actual={actual_sha256}"
        )


def _local_publish_asset(path: Path) -> GitHubPublishAsset:
    """为 manifest 或 license 普通文件生成 Release asset 描述。"""

    path = path.expanduser().resolve()
    if path.is_symlink() or not path.is_file():
        raise GitHubPublishError(f"GitHub Release 发布文件不存在或不是普通文件: {path}")
    sha256, size_bytes = hash_pdf_asset(path)
    return GitHubPublishAsset(
        name=path.name,
        source=path,
        size_bytes=size_bytes,
        sha256=sha256,
    )


def _license_files(licenses_dir: Path) -> tuple[Path, ...]:
    """按稳定文件名列出 license, 并拒绝空目录、子目录和符号链接。"""

    if licenses_dir.is_symlink() or not licenses_dir.is_dir():
        raise GitHubPublishError(f"PDF 资源 license 目录不存在: {licenses_dir}")
    entries = tuple(sorted(licenses_dir.iterdir(), key=lambda path: path.name))
    if not entries:
        raise GitHubPublishError(f"PDF 资源 license 目录为空: {licenses_dir}")
    invalid = [path for path in entries if path.is_symlink() or not path.is_file()]
    if invalid:
        raise GitHubPublishError(f"PDF 资源 license 目录只能包含普通文件: {invalid[0]}")
    return entries


def _reject_duplicate_release_names(assets: Sequence[GitHubPublishAsset]) -> None:
    """拒绝任何会在扁平 Release asset 命名空间中相互覆盖的文件名。"""

    seen: set[str] = set()
    for asset in assets:
        if asset.name in seen:
            raise GitHubPublishError(f"GitHub Release asset basename 重复: {asset.name}")
        seen.add(asset.name)


def _fetch_release(repo: str, tag: str, *, runner: Runner) -> GitHubReleaseState | None:
    """分页列出 Releases 并按 tag 读取 draft、asset size 与 digest。"""

    command = [
        "gh",
        "api",
        f"repos/{repo}/releases",
        "--method",
        "GET",
        "--paginate",
        "--slurp",
        "--header",
        "Accept: application/vnd.github+json",
        "--header",
        "X-GitHub-Api-Version: 2026-03-10",
    ]
    result = _run_gh(command, runner=runner)
    if result.returncode != 0:
        raise GitHubPublishError(f"读取 GitHub Release 失败: {_result_detail(result)}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise GitHubPublishError("GitHub Release API 返回了无效 JSON") from error
    if not isinstance(payload, list) or any(not isinstance(page, list) for page in payload):
        raise GitHubPublishError("GitHub Release list API response 必须是分页 array")
    matches = [
        release
        for page in payload
        for release in page
        if isinstance(release, Mapping) and release.get("tag_name") == tag
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise GitHubPublishError(f"GitHub Release tag 返回重复结果: {tag}")
    return _parse_release_payload(matches[0], expected_tag=tag)


def _parse_release_payload(payload: object, *, expected_tag: str) -> GitHubReleaseState:
    """严格解析 GitHub Release API 中发布判断所需的最小字段。"""

    if not isinstance(payload, Mapping):
        raise GitHubPublishError("GitHub Release API response 必须是 object")
    if payload.get("tag_name") != expected_tag:
        raise GitHubPublishError("GitHub Release API 返回了错误 tag")
    draft = payload.get("draft")
    if not isinstance(draft, bool):
        raise GitHubPublishError("GitHub Release API draft 字段无效")
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise GitHubPublishError("GitHub Release API assets 字段无效")

    assets: list[GitHubRemoteAsset] = []
    for index, raw_asset in enumerate(raw_assets):
        if not isinstance(raw_asset, Mapping):
            raise GitHubPublishError(f"GitHub Release API assets[{index}] 必须是 object")
        name = raw_asset.get("name")
        size_bytes = raw_asset.get("size")
        digest = raw_asset.get("digest")
        if not isinstance(name, str) or not name:
            raise GitHubPublishError(f"GitHub Release API assets[{index}].name 无效")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise GitHubPublishError(f"GitHub Release API assets[{index}].size 无效")
        if digest is not None and not isinstance(digest, str):
            raise GitHubPublishError(f"GitHub Release API assets[{index}].digest 无效")
        assets.append(
            GitHubRemoteAsset(
                name=name,
                size_bytes=size_bytes,
                digest=digest,
            )
        )
    return GitHubReleaseState(draft=draft, assets=tuple(assets))


def _remote_assets_by_name(
    assets: Sequence[GitHubRemoteAsset],
) -> dict[str, GitHubRemoteAsset]:
    """把远端 assets 建立为唯一名称索引, 重名时拒绝猜测。"""

    indexed: dict[str, GitHubRemoteAsset] = {}
    for asset in assets:
        if asset.name in indexed:
            raise GitHubPublishError(f"GitHub Release 已有重复 asset 名称: {asset.name}")
        indexed[asset.name] = asset
    return indexed


def _verify_remote_asset(local: GitHubPublishAsset, remote: GitHubRemoteAsset) -> None:
    """已有同名 asset 只有 size 与 GitHub SHA-256 digest 都匹配时才能复用。"""

    expected_digest = f"sha256:{local.sha256}"
    if remote.size_bytes != local.size_bytes or remote.digest != expected_digest:
        raise GitHubPublishError(f"GitHub Release 不可变 asset 内容冲突, 拒绝覆盖: {local.name}")


def _verify_complete_release(
    assets: Sequence[GitHubPublishAsset],
    release: GitHubReleaseState,
) -> None:
    """发布 draft 前确认计划内所有 assets 均已存在且完整性一致。"""

    remote_assets = _remote_assets_by_name(release.assets)
    for asset in assets:
        remote_asset = remote_assets.get(asset.name)
        if remote_asset is None:
            raise GitHubPublishError(f"GitHub Release 上传后仍缺少 asset: {asset.name}")
        _verify_remote_asset(asset, remote_asset)


def _create_draft_release(
    repo: str,
    tag: str,
    pack_revision: str,
    *,
    runner: Runner,
) -> None:
    """创建不会抢占应用 Latest 标记的 PDF 资源 draft Release。"""

    command = [
        "gh",
        "release",
        "create",
        tag,
        "--repo",
        repo,
        "--draft",
        "--latest=false",
        "--title",
        f"PageFerry PDF 资源 {pack_revision}",
        "--notes",
        f"PageFerry PDF runtime assets for pack revision {pack_revision}.",
    ]
    _require_gh_success(command, runner=runner, action="创建 GitHub draft Release")


def _upload_release_asset(
    repo: str,
    tag: str,
    asset: GitHubPublishAsset,
    *,
    runner: Runner,
) -> None:
    """不使用 clobber 上传单个缺失文件, 同名竞态交给 GitHub 拒绝。"""

    command = [
        "gh",
        "release",
        "upload",
        tag,
        str(asset.source),
        "--repo",
        repo,
    ]
    _require_gh_success(command, runner=runner, action=f"上传 GitHub asset {asset.name}")


def _publish_draft_release(repo: str, tag: str, *, runner: Runner) -> None:
    """仅在全部 assets 复核通过后把 draft Release 设为 published。"""

    command = [
        "gh",
        "release",
        "edit",
        tag,
        "--repo",
        repo,
        "--draft=false",
        "--latest=false",
    ]
    _require_gh_success(command, runner=runner, action="发布 GitHub draft Release")


def _run_gh(command: list[str], *, runner: Runner) -> subprocess.CompletedProcess[str]:
    """以参数数组和禁用 shell 的方式调用 gh CLI。"""

    return runner(
        command,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )


def _require_gh_success(command: list[str], *, runner: Runner, action: str) -> None:
    """执行 gh 写操作并把非零退出码转换成稳定发布错误。"""

    result = _run_gh(command, runner=runner)
    if result.returncode != 0:
        raise GitHubPublishError(f"{action}失败: {_result_detail(result)}")


def _result_detail(result: subprocess.CompletedProcess[str]) -> str:
    """提取 gh CLI 的简短错误信息, 避免把整段 response 混入日志。"""

    detail = result.stderr.strip() or result.stdout.strip() or f"exit={result.returncode}"
    return detail[:500]


if __name__ == "__main__":
    raise SystemExit(main())
