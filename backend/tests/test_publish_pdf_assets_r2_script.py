"""验证 PDF R2 发布脚本的公网校验、顺序与不可变对象行为。"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from urllib.error import HTTPError
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
from urllib.request import Request

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "publish-pdf-assets-r2.py"
PUBLIC_BASE_URL = "https://assets.example.test/pdf/2026.07.18.1/"


def _load_script() -> ModuleType:
    """把带连字符的发布脚本作为独立 module 加载。"""

    spec = importlib.util.spec_from_file_location("pageferry_publish_pdf_assets_r2", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本: {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish_r2 = _load_script()


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, bytes]]:
    """写入包含 ONNX、字体和 license 的小型发布 fixture。"""

    files = {
        "layout/model.onnx": b"model-content",
        "fonts/Test.ttf": b"font-content",
    }
    source_dir = tmp_path / "source"
    assets = []
    for index, (relative_path, content) in enumerate(files.items()):
        path = source_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        assets.append(
            {
                "asset_id": f"asset-{index}",
                "pack": "test",
                "relative_path": relative_path,
                "distribution_path": relative_path,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    manifest = {
        "schema_version": 1,
        "pack_id": "test-pack",
        "pack_revision": "2026.07.18.1",
        "default_base_url": PUBLIC_BASE_URL,
        "assets": assets,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    licenses_dir = tmp_path / "licenses"
    licenses_dir.mkdir()
    (licenses_dir / "OFL-1.1.txt").write_text("license text", encoding="utf-8")
    expected = {
        **files,
        "licenses/OFL-1.1.txt": b"license text",
        "manifest.json": manifest_path.read_bytes(),
    }
    return manifest_path, source_dir, licenses_dir, expected


class FakeResponse:
    """提供流式读取所需的最小 HTTP response。"""

    def __init__(self, status: int, content: bytes) -> None:
        """记录状态码并创建分块可读 body。"""

        self.status = status
        self._body = io.BytesIO(content)

    def read(self, size: int = -1) -> bytes:
        """读取最多指定数量的 body 字节。"""

        return self._body.read(size)

    def __enter__(self) -> FakeResponse:
        """返回当前 response。"""

        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc_value: object,
        _traceback: object,
    ) -> None:
        """关闭内存 body。"""

        self._body.close()


class FakePublicEndpoint:
    """按不含 query 的公网 URL 保存对象并模拟 HTTP GET。"""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        """用可选的已有公网对象初始化 endpoint。"""

        self.objects = dict(objects or {})
        self.calls: list[tuple[str, float]] = []

    def __call__(self, request: Request, *, timeout: float) -> FakeResponse:
        """检查 cache-bust query 后返回 200 或抛出真实形态的 404。"""

        assert isinstance(request, Request)
        parsed = urlsplit(request.full_url)
        query = parse_qs(parsed.query)
        assert len(query.get("pageferry_verify", [])) == 1
        canonical_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        self.calls.append((request.full_url, timeout))
        if canonical_url not in self.objects:
            raise HTTPError(request.full_url, 404, "Not Found", {}, None)
        return FakeResponse(200, self.objects[canonical_url])


class FakeWrangler:
    """只模拟 wrangler put, 并让上传内容出现在公网 endpoint。"""

    def __init__(self, endpoint: FakePublicEndpoint, *, publish: bool = True) -> None:
        """记录公网 endpoint 以及是否让上传立即公开。"""

        self.endpoint = endpoint
        self.publish = publish
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self,
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """执行 put 并拒绝测试中出现任何 wrangler get。"""

        self.calls.append((command, kwargs))
        assert kwargs == {
            "check": False,
            "capture_output": True,
            "text": True,
            "shell": False,
        }
        operation = command[3]
        assert operation == "put"
        object_ref = command[4]
        if self.publish:
            source = Path(command[command.index("--file") + 1])
            marker = "/pdf/2026.07.18.1/"
            relative_path = object_ref.split(marker, maxsplit=1)[1]
            self.endpoint.objects[urljoin(PUBLIC_BASE_URL, relative_path)] = source.read_bytes()
        return subprocess.CompletedProcess(command, 0, stdout="uploaded", stderr="")


def _public_objects(expected: dict[str, bytes]) -> dict[str, bytes]:
    """把 fixture 相对路径映射为 canonical 公网 URL。"""

    return {urljoin(PUBLIC_BASE_URL, path): content for path, content in expected.items()}


def test_404_objects_upload_in_fixed_order_then_pass_public_200_check(tmp_path: Path) -> None:
    """404 对象必须按固定顺序上传, manifest 最后且上传后通过公网 200 校验。"""

    manifest_path, source_dir, licenses_dir, expected = _write_fixture(tmp_path)
    endpoint = FakePublicEndpoint()
    wrangler = FakeWrangler(endpoint)

    results = publish_r2.publish_pdf_assets(
        "test-bucket",
        manifest_path,
        source_dir,
        licenses_dir,
        wrangler_command=("fake-wrangler",),
        runner=wrangler,
        fetcher=endpoint,
        timeout=12.0,
    )

    prefix = "test-bucket/pdf/2026.07.18.1/"
    put_calls = [command for command, _kwargs in wrangler.calls]
    assert [command[4] for command in put_calls] == [
        f"{prefix}layout/model.onnx",
        f"{prefix}fonts/Test.ttf",
        f"{prefix}licenses/OFL-1.1.txt",
        f"{prefix}manifest.json",
    ]
    assert [command[command.index("--content-type") + 1] for command in put_calls] == [
        "application/octet-stream",
        "font/ttf",
        "text/plain; charset=utf-8",
        "application/json; charset=utf-8",
    ]
    assert all(
        command[command.index("--cache-control") + 1] == publish_r2.R2_CACHE_CONTROL
        for command in put_calls
    )
    assert all(result.uploaded for result in results)
    assert endpoint.objects == _public_objects(expected)
    assert len(endpoint.calls) == len(expected) * 2
    assert len({url for url, _timeout in endpoint.calls}) == len(endpoint.calls)
    assert all(timeout == 12.0 for _url, timeout in endpoint.calls)


def test_public_200_objects_are_streamed_and_reused_without_put(tmp_path: Path) -> None:
    """公网 200 对象的 size 与 SHA-256 匹配时不得调用 wrangler。"""

    manifest_path, source_dir, licenses_dir, expected = _write_fixture(tmp_path)
    endpoint = FakePublicEndpoint(_public_objects(expected))
    wrangler = FakeWrangler(endpoint)

    results = publish_r2.publish_pdf_assets(
        "test-bucket",
        manifest_path,
        source_dir,
        licenses_dir,
        wrangler_command=("fake-wrangler",),
        runner=wrangler,
        fetcher=endpoint,
    )

    assert all(not result.uploaded for result in results)
    assert wrangler.calls == []
    assert len(endpoint.calls) == len(expected)


def test_truncated_public_body_is_retried_before_declaring_conflict(tmp_path: Path) -> None:
    """第一次公网传输被截断时应重新读取, 不能把完整远端对象误判成冲突。"""

    manifest_path, source_dir, licenses_dir, expected = _write_fixture(tmp_path)
    endpoint = FakePublicEndpoint(_public_objects(expected))
    wrangler = FakeWrangler(endpoint)
    first_url = urljoin(PUBLIC_BASE_URL, "layout/model.onnx")
    truncated = True

    def flaky_fetcher(request: Request, *, timeout: float) -> FakeResponse:
        """只截断目标对象的第一次传输, 后续委托给完整公网 endpoint。"""

        nonlocal truncated
        parsed = urlsplit(request.full_url)
        canonical_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        if canonical_url == first_url and truncated:
            truncated = False
            return FakeResponse(200, b"model")
        return endpoint(request, timeout=timeout)

    results = publish_r2.publish_pdf_assets(
        "test-bucket",
        manifest_path,
        source_dir,
        licenses_dir,
        wrangler_command=("fake-wrangler",),
        runner=wrangler,
        fetcher=flaky_fetcher,
    )

    assert all(not result.uploaded for result in results)
    assert truncated is False
    assert wrangler.calls == []


def test_public_200_conflict_rejects_immutable_object(tmp_path: Path) -> None:
    """公网 200 返回不同内容时必须失败, 不能用 wrangler 覆盖。"""

    manifest_path, source_dir, licenses_dir, _expected = _write_fixture(tmp_path)
    endpoint = FakePublicEndpoint({urljoin(PUBLIC_BASE_URL, "layout/model.onnx"): b"wrong"})
    wrangler = FakeWrangler(endpoint)

    with pytest.raises(publish_r2.R2PublishError, match="不可变对象内容冲突"):
        publish_r2.publish_pdf_assets(
            "test-bucket",
            manifest_path,
            source_dir,
            licenses_dir,
            wrangler_command=("fake-wrangler",),
            runner=wrangler,
            fetcher=endpoint,
        )

    assert wrangler.calls == []


def test_upload_must_be_visible_as_public_200(tmp_path: Path) -> None:
    """wrangler 成功但上传后公网仍为 404 时必须失败。"""

    manifest_path, source_dir, licenses_dir, _expected = _write_fixture(tmp_path)
    endpoint = FakePublicEndpoint()
    wrangler = FakeWrangler(endpoint, publish=False)

    with pytest.raises(publish_r2.R2PublishError, match="上传后公网仍返回 404"):
        publish_r2.publish_pdf_assets(
            "test-bucket",
            manifest_path,
            source_dir,
            licenses_dir,
            wrangler_command=("fake-wrangler",),
            runner=wrangler,
            fetcher=endpoint,
        )

    assert len(wrangler.calls) == 1
    assert len(endpoint.calls) == 2


def test_invalid_local_asset_stops_before_any_remote_call(tmp_path: Path) -> None:
    """任一本地资源校验失败时必须在第一次公网或 wrangler 调用前终止。"""

    manifest_path, source_dir, licenses_dir, _expected = _write_fixture(tmp_path)
    (source_dir / "fonts" / "Test.ttf").write_bytes(b"tampered-font")
    endpoint = FakePublicEndpoint()
    wrangler = FakeWrangler(endpoint)

    with pytest.raises(publish_r2.R2PublishError, match="校验失败"):
        publish_r2.publish_pdf_assets(
            "test-bucket",
            manifest_path,
            source_dir,
            licenses_dir,
            wrangler_command=("fake-wrangler",),
            runner=wrangler,
            fetcher=endpoint,
        )

    assert endpoint.calls == []
    assert wrangler.calls == []


@pytest.mark.parametrize(
    "default_base_url",
    [None, "https://assets.example.test/pdf/2026.07.17.9/"],
)
def test_invalid_manifest_base_url_stops_before_any_remote_call(
    tmp_path: Path,
    default_base_url: str | None,
) -> None:
    """空地址或错误 revision 不能成为远端发布完成标志。"""

    manifest_path, source_dir, licenses_dir, _expected = _write_fixture(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["default_base_url"] = default_base_url
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    endpoint = FakePublicEndpoint()
    wrangler = FakeWrangler(endpoint)

    with pytest.raises(publish_r2.R2PublishError, match="default_base_url"):
        publish_r2.publish_pdf_assets(
            "test-bucket",
            manifest_path,
            source_dir,
            licenses_dir,
            wrangler_command=("fake-wrangler",),
            runner=wrangler,
            fetcher=endpoint,
        )

    assert endpoint.calls == []
    assert wrangler.calls == []


def test_wrangler_put_failure_is_not_treated_as_uploaded(tmp_path: Path) -> None:
    """公网 404 后 wrangler 认证失败必须直接报告上传失败。"""

    manifest_path, source_dir, licenses_dir, _expected = _write_fixture(tmp_path)
    endpoint = FakePublicEndpoint()

    def failing_runner(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """模拟 wrangler 身份认证失败。"""

        return subprocess.CompletedProcess(command, 1, stdout="", stderr="Authentication failed")

    with pytest.raises(publish_r2.R2PublishError, match="上传 R2 对象失败"):
        publish_r2.publish_pdf_assets(
            "test-bucket",
            manifest_path,
            source_dir,
            licenses_dir,
            wrangler_command=("fake-wrangler",),
            runner=failing_runner,
            fetcher=endpoint,
        )

    assert len(endpoint.calls) == 1


def test_non_200_or_404_public_status_fails_without_put(tmp_path: Path) -> None:
    """公网返回 200 与 404 之外的状态时必须失败。"""

    manifest_path, source_dir, licenses_dir, _expected = _write_fixture(tmp_path)
    wrangler_calls: list[list[str]] = []

    def unavailable_fetcher(_request: Request, *, timeout: float) -> FakeResponse:
        """返回不能当成存在或缺失处理的 503。"""

        assert timeout == publish_r2.DEFAULT_FETCH_TIMEOUT
        return FakeResponse(503, b"")

    def recording_runner(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """记录任何意外的 wrangler 调用。"""

        wrangler_calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(publish_r2.R2PublishError, match="HTTP 503"):
        publish_r2.publish_pdf_assets(
            "test-bucket",
            manifest_path,
            source_dir,
            licenses_dir,
            wrangler_command=("fake-wrangler",),
            runner=recording_runner,
            fetcher=unavailable_fetcher,
        )

    assert wrangler_calls == []


def test_bucket_name_cannot_be_interpreted_as_wrangler_option(tmp_path: Path) -> None:
    """bucket 名称必须先验证, 不能把 option 形式的内容交给 wrangler。"""

    manifest_path, source_dir, licenses_dir, _expected = _write_fixture(tmp_path)
    endpoint = FakePublicEndpoint()
    wrangler = FakeWrangler(endpoint)

    with pytest.raises(publish_r2.R2PublishError, match="bucket 名称"):
        publish_r2.publish_pdf_assets(
            "--config",
            manifest_path,
            source_dir,
            licenses_dir,
            wrangler_command=("fake-wrangler",),
            runner=wrangler,
            fetcher=endpoint,
        )

    assert endpoint.calls == []
    assert wrangler.calls == []
