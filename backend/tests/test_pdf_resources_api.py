"""验证 PDF 资源状态、安装与取消 endpoint 的稳定公开 contract。"""

from pathlib import Path

from fastapi.testclient import TestClient

from core.settings import Settings
from main import create_app
from modules.pdf.resource_installer import (
    PdfResourcePackStatus,
    PdfResourcesNotReadyError,
    PdfResourceStatus,
)


class _FakePdfResourceInstaller:
    """记录 API 编排调用且不创建真实下载线程。"""

    def __init__(self) -> None:
        """建立 missing 初态与生命周期调用计数。"""

        self.initialize_calls = 0
        self.install_calls = 0
        self.cancel_calls = 0
        self.close_calls = 0
        self.ensure_ready_calls = 0
        self.reject_ready_check = False
        self.current = PdfResourceStatus(
            pack_revision="test-v1",
            state="missing",
            total_bytes=30,
            completed_bytes=10,
            current_asset_id=None,
            error_code=None,
            resources=(
                PdfResourcePackStatus(
                    pack="layout",
                    size_bytes=10,
                    completed_bytes=10,
                    ready=True,
                ),
                PdfResourcePackStatus(
                    pack="fonts-common-zh-cn",
                    size_bytes=20,
                    completed_bytes=0,
                    ready=False,
                ),
            ),
        )

    def initialize(self) -> PdfResourceStatus:
        """记录 lifespan 初始化并返回当前状态。"""

        self.initialize_calls += 1
        return self.current

    def status(self) -> PdfResourceStatus:
        """返回当前 fake 状态。"""

        return self.current

    def ensure_ready(self) -> PdfResourceStatus:
        """满足 create_app 注入给 PDF runtime 的 fail-closed gate。"""

        self.ensure_ready_calls += 1
        if self.reject_ready_check:
            raise PdfResourcesNotReadyError(("layout",))
        return self.current

    def start_install(self) -> PdfResourceStatus:
        """记录显式安装并切换为 downloading。"""

        self.install_calls += 1
        self.current = PdfResourceStatus(
            pack_revision="test-v1",
            state="downloading",
            total_bytes=30,
            completed_bytes=12,
            current_asset_id="font-regular",
            error_code=None,
            resources=(
                PdfResourcePackStatus(
                    pack="layout",
                    size_bytes=10,
                    completed_bytes=10,
                    ready=True,
                ),
                PdfResourcePackStatus(
                    pack="fonts-common-zh-cn",
                    size_bytes=20,
                    completed_bytes=2,
                    ready=False,
                ),
            ),
        )
        return self.current

    def cancel(self) -> PdfResourceStatus:
        """记录取消请求, 模拟 worker 已在安全边界收敛。"""

        self.cancel_calls += 1
        self.current = PdfResourceStatus(
            pack_revision="test-v1",
            state="cancelled",
            total_bytes=30,
            completed_bytes=10,
            current_asset_id=None,
            error_code=None,
            resources=(
                PdfResourcePackStatus(
                    pack="layout",
                    size_bytes=10,
                    completed_bytes=10,
                    ready=True,
                ),
                PdfResourcePackStatus(
                    pack="fonts-common-zh-cn",
                    size_bytes=20,
                    completed_bytes=0,
                    ready=False,
                ),
            ),
        )
        return self.current

    def close(self) -> None:
        """记录 FastAPI lifespan 是否释放 app-scoped installer。"""

        self.close_calls += 1


def _test_app(tmp_path: Path, installer: _FakePdfResourceInstaller, *, token: str | None = None):
    """构造注入 fake installer 的隔离应用。"""

    return create_app(
        Settings(data_dir=tmp_path, boot_token=token),
        pdf_resource_installer=installer,  # type: ignore[arg-type]
    )


def test_get_pdf_resources_returns_only_public_progress_contract(tmp_path: Path) -> None:
    """GET 只返回 pack 进度字段, 不能泄露本地路径或下载 URL。"""

    installer = _FakePdfResourceInstaller()
    app = _test_app(tmp_path, installer)

    with TestClient(app) as client:
        response = client.get("/api/v1/pdf-resources")

    assert response.status_code == 200
    assert response.json() == {
        "code": "success",
        "data": {
            "pack_revision": "test-v1",
            "state": "missing",
            "total_bytes": 30,
            "completed_bytes": 10,
            "current_asset_id": None,
            "error_code": None,
            "resources": [
                {
                    "pack": "layout",
                    "size_bytes": 10,
                    "completed_bytes": 10,
                    "ready": True,
                },
                {
                    "pack": "fonts-common-zh-cn",
                    "size_bytes": 20,
                    "completed_bytes": 0,
                    "ready": False,
                },
            ],
        },
    }
    serialized = response.text.lower()
    assert "path" not in serialized
    assert "url" not in serialized
    assert installer.initialize_calls == 1
    assert installer.close_calls == 1


def test_install_and_cancel_require_boot_token_and_only_orchestrate_service(
    tmp_path: Path,
) -> None:
    """两个 mutation endpoint 都复用 boot token, API 本身不执行下载。"""

    installer = _FakePdfResourceInstaller()
    app = _test_app(tmp_path, installer, token="pdf-resource-token")

    with TestClient(app) as client:
        unauthorized_install = client.post("/api/v1/pdf-resources/install")
        install = client.post(
            "/api/v1/pdf-resources/install",
            headers={"X-PageFerry-Boot-Token": "pdf-resource-token"},
        )
        unauthorized_cancel = client.post("/api/v1/pdf-resources/cancel")
        cancel = client.post(
            "/api/v1/pdf-resources/cancel",
            headers={"X-PageFerry-Boot-Token": "pdf-resource-token"},
        )

    assert unauthorized_install.status_code == 401
    assert unauthorized_cancel.status_code == 401
    assert install.status_code == 202
    assert install.json()["data"]["state"] == "downloading"
    assert cancel.status_code == 200
    assert cancel.json()["data"]["state"] == "cancelled"
    assert installer.install_calls == 1
    assert installer.cancel_calls == 1


def test_default_app_reports_layout_and_common_chinese_font_packs(tmp_path: Path) -> None:
    """默认 app 只把 layout 与简体中文公共字体列为首次 required resources。"""

    app = create_app(Settings(data_dir=tmp_path))

    with TestClient(app) as client:
        payload = client.get("/api/v1/pdf-resources").json()["data"]

    assert payload["pack_revision"] == "2026.07.18.2"
    assert payload["state"] == "missing"
    assert payload["completed_bytes"] == 0
    assert [resource["pack"] for resource in payload["resources"]] == [
        "layout",
        "fonts-common-zh-cn",
    ]
    assert payload["total_bytes"] == sum(
        resource["size_bytes"] for resource in payload["resources"]
    )


def test_app_wires_resource_gate_before_lazy_layout_runtime(tmp_path: Path) -> None:
    """首个 PDF 操作必须经过 app-scoped installer 校验, 失败时不创建 ONNX runtime。"""

    from modules.pdf.layout import LayoutModelError

    installer = _FakePdfResourceInstaller()
    installer.reject_ready_check = True
    app = _test_app(tmp_path, installer)

    try:
        app.state.layout_detector.ensure_model_available()
    except LayoutModelError as error:
        assert str(error) == "pdf_resources_not_ready"
    else:
        raise AssertionError("PDF resource gate must fail closed")

    assert installer.ensure_ready_calls == 1
