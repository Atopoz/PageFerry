"""验证 PDF GitHub Release 发布脚本的顺序、复用与不可变资产行为。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "publish-pdf-assets-github.py"
REPO = "Atopoz/PageFerry"
TAG = "pdf-assets-test.1"


def _load_script() -> ModuleType:
    """把带连字符的发布脚本作为独立 module 加载。"""

    spec = importlib.util.spec_from_file_location(
        "pageferry_publish_pdf_assets_github",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本: {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish_github = _load_script()


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path, tuple[str, ...]]:
    """写入包含两个二进制文件、两个 license 与 manifest 的发布 fixture。"""

    files = {
        "layout/model.onnx": b"model-content",
        "fonts/Test.ttf": b"font-content",
    }
    source_dir = tmp_path / "source"
    assets = []
    for index, (relative_path, content) in enumerate(files.items()):
        source = source_dir / relative_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        name = source.name
        assets.append(
            {
                "asset_id": f"asset-{index}",
                "pack": "test",
                "relative_path": relative_path,
                "distribution_path": relative_path,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "fallback_urls": [f"https://github.com/{REPO}/releases/download/{TAG}/{name}"],
            }
        )

    manifest = {
        "schema_version": 1,
        "pack_id": "test-pack",
        "pack_revision": "test.1",
        "default_base_url": "https://assets.example.test/pdf/test.1/",
        "assets": assets,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    licenses_dir = tmp_path / "licenses"
    licenses_dir.mkdir()
    (licenses_dir / "OFL-1.1.txt").write_text("ofl", encoding="utf-8")
    (licenses_dir / "Apache-2.0.txt").write_text("apache", encoding="utf-8")
    expected_order = (
        "model.onnx",
        "Test.ttf",
        "Apache-2.0.txt",
        "OFL-1.1.txt",
        "manifest.json",
    )
    return manifest_path, source_dir, licenses_dir, expected_order


class FakeGh:
    """模拟 gh API、draft 创建、asset 上传与发布操作。"""

    def __init__(
        self,
        *,
        exists: bool = False,
        draft: bool = True,
        assets: dict[str, dict[str, object]] | None = None,
    ) -> None:
        """用可选 Release 状态初始化内存中的 GitHub 远端。"""

        self.exists = exists
        self.draft = draft
        self.assets = dict(assets or {})
        self.calls: list[list[str]] = []
        self.uploaded_names: list[str] = []

    def __call__(
        self,
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """执行最小 gh 命令集合, 并断言始终禁用 shell。"""

        assert kwargs == {
            "check": False,
            "capture_output": True,
            "text": True,
            "shell": False,
        }
        self.calls.append(command)
        assert "--clobber" not in command
        if command[:2] == ["gh", "api"]:
            return self._api_response(command)
        if command[:3] == ["gh", "release", "create"]:
            self.exists = True
            self.draft = True
            return subprocess.CompletedProcess(command, 0, stdout="created", stderr="")
        if command[:3] == ["gh", "release", "upload"]:
            source = Path(command[4])
            content = source.read_bytes()
            self.assets[source.name] = {
                "name": source.name,
                "size": len(content),
                "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
            }
            self.uploaded_names.append(source.name)
            return subprocess.CompletedProcess(command, 0, stdout="uploaded", stderr="")
        if command[:3] == ["gh", "release", "edit"]:
            self.draft = False
            return subprocess.CompletedProcess(command, 0, stdout="published", stderr="")
        raise AssertionError(f"unexpected gh command: {command}")

    def _api_response(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        """返回 gh api --paginate --slurp 产生的分页 Release JSON。"""

        releases = []
        if self.exists:
            releases.append(
                {
                    "tag_name": TAG,
                    "draft": self.draft,
                    "assets": list(self.assets.values()),
                }
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps([releases]),
            stderr="",
        )


def _remote_assets(
    assets: tuple[object, ...],
) -> dict[str, dict[str, object]]:
    """把本地发布计划转换为 GitHub API asset metadata。"""

    return {
        asset.name: {
            "name": asset.name,
            "size": asset.size_bytes,
            "digest": f"sha256:{asset.sha256}",
        }
        for asset in assets
    }


def test_missing_release_uploads_in_fixed_order_then_publishes_draft(tmp_path: Path) -> None:
    """不存在的 Release 应先建 draft, manifest 最后上传并在复核后发布。"""

    manifest_path, source_dir, licenses_dir, expected_order = _write_fixture(tmp_path)
    runner = FakeGh()

    results = publish_github.publish_pdf_assets(
        REPO,
        TAG,
        manifest_path=manifest_path,
        source_dir=source_dir,
        licenses_dir=licenses_dir,
        runner=runner,
    )

    assert [result.name for result in results] == list(expected_order)
    assert all(result.uploaded for result in results)
    assert runner.uploaded_names == list(expected_order)
    assert runner.draft is False
    operations = [command[1:3] for command in runner.calls]
    assert operations == [
        ["api", f"repos/{REPO}/releases"],
        ["release", "create"],
        ["api", f"repos/{REPO}/releases"],
        ["release", "upload"],
        ["release", "upload"],
        ["release", "upload"],
        ["release", "upload"],
        ["release", "upload"],
        ["api", f"repos/{REPO}/releases"],
        ["release", "edit"],
        ["api", f"repos/{REPO}/releases"],
    ]


def test_matching_published_release_assets_are_reused_without_writes(tmp_path: Path) -> None:
    """published Release 的 size 与 digest 全匹配时只读取并复用。"""

    manifest_path, source_dir, licenses_dir, expected_order = _write_fixture(tmp_path)
    manifest, assets = publish_github.build_publish_plan(
        REPO,
        TAG,
        manifest_path,
        source_dir,
        licenses_dir,
    )
    runner = FakeGh(exists=True, draft=False, assets=_remote_assets(assets))

    results = publish_github.publish_plan(REPO, TAG, manifest, assets, runner=runner)

    assert [result.name for result in results] == list(expected_order)
    assert not any(result.uploaded for result in results)
    assert runner.uploaded_names == []
    assert all(command[:2] == ["gh", "api"] for command in runner.calls)


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("size", 1),
        ("digest", "sha256:" + "0" * 64),
        ("digest", None),
    ],
)
def test_existing_asset_conflict_fails_before_any_upload(
    tmp_path: Path,
    field: str,
    bad_value: object,
) -> None:
    """已有同名 asset 的 size 或 digest 不匹配时绝不能上传覆盖。"""

    manifest_path, source_dir, licenses_dir, _expected_order = _write_fixture(tmp_path)
    manifest, assets = publish_github.build_publish_plan(
        REPO,
        TAG,
        manifest_path,
        source_dir,
        licenses_dir,
    )
    remote = _remote_assets(assets)
    remote[assets[0].name][field] = bad_value
    runner = FakeGh(exists=True, draft=True, assets=remote)

    with pytest.raises(publish_github.GitHubPublishError, match="内容冲突"):
        publish_github.publish_plan(REPO, TAG, manifest, assets, runner=runner)

    assert runner.uploaded_names == []
    assert len(runner.calls) == 1


def test_invalid_local_asset_stops_before_any_gh_call(tmp_path: Path) -> None:
    """任一本地二进制校验失败时必须在第一次 GitHub 调用前终止。"""

    manifest_path, source_dir, licenses_dir, _expected_order = _write_fixture(tmp_path)
    (source_dir / "fonts" / "Test.ttf").write_bytes(b"tampered")
    runner = FakeGh()

    with pytest.raises(publish_github.GitHubPublishError, match="校验失败"):
        publish_github.publish_pdf_assets(
            REPO,
            TAG,
            manifest_path=manifest_path,
            source_dir=source_dir,
            licenses_dir=licenses_dir,
            runner=runner,
        )

    assert runner.calls == []


@pytest.mark.parametrize(
    "fallback_urls",
    [
        [],
        [f"https://github.com/{REPO}/releases/download/wrong/model.onnx"],
        [
            f"https://github.com/{REPO}/releases/download/{TAG}/model.onnx",
            f"https://github.com/{REPO}/releases/download/{TAG}/other.onnx",
        ],
    ],
)
def test_manifest_must_pin_exactly_one_target_release_url(
    tmp_path: Path,
    fallback_urls: list[str],
) -> None:
    """fallback_urls 缺失、tag 错误或包含其他 URL 都必须 fail closed。"""

    manifest_path, source_dir, licenses_dir, _expected_order = _write_fixture(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["assets"][0]["fallback_urls"] = fallback_urls
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(publish_github.GitHubPublishError, match="fallback_urls"):
        publish_github.build_publish_plan(
            REPO,
            TAG,
            manifest_path,
            source_dir,
            licenses_dir,
        )


@pytest.mark.parametrize(
    "repo,tag",
    [
        ("--repo", TAG),
        ("owner/repo/extra", TAG),
        (REPO, "--delete"),
        (REPO, "tag/with/slash"),
    ],
)
def test_repo_and_tag_cannot_be_interpreted_as_options_or_url_paths(
    tmp_path: Path,
    repo: str,
    tag: str,
) -> None:
    """repository 与 tag 在进入 gh 参数数组前必须通过安全字符校验。"""

    manifest_path, source_dir, licenses_dir, _expected_order = _write_fixture(tmp_path)
    runner = FakeGh()

    with pytest.raises(publish_github.GitHubPublishError):
        publish_github.publish_pdf_assets(
            repo,
            tag,
            manifest_path=manifest_path,
            source_dir=source_dir,
            licenses_dir=licenses_dir,
            runner=runner,
        )

    assert runner.calls == []


def test_duplicate_binary_basename_is_rejected(tmp_path: Path) -> None:
    """Release 扁平命名空间不能接受来自不同目录的重复二进制 basename。"""

    manifest_path, source_dir, licenses_dir, _expected_order = _write_fixture(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["assets"][1]["distribution_path"] = "other/model.onnx"
    payload["assets"][1]["fallback_urls"] = [
        f"https://github.com/{REPO}/releases/download/{TAG}/model.onnx"
    ]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(publish_github.GitHubPublishError, match="basename 重复"):
        publish_github.build_publish_plan(
            REPO,
            TAG,
            manifest_path,
            source_dir,
            licenses_dir,
        )
