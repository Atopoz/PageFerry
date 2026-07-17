"""验证 secret store contract 与 Keychain adapter."""

from collections.abc import MutableMapping

import pytest

from modules.model_catalog.secrets import (
    KeyringSecretStore,
    SecretStore,
    SecretStoreOperationError,
    SecretStoreUnavailableError,
    keyring_store,
)


class MemorySecretStore:
    """用于结构化 Protocol 检查的小型内存 fake."""

    def __init__(self) -> None:
        """创建空 fake store."""

        self.secrets: dict[str, str] = {}

    def set_secret(self, reference: str, secret: str) -> None:
        """在内存中保存 secret."""

        self.secrets[reference] = secret

    def get_secret(self, reference: str) -> str | None:
        """从内存读取 secret."""

        return self.secrets.get(reference)

    def delete_secret(self, reference: str) -> bool:
        """从内存删除 secret."""

        return self.secrets.pop(reference, None) is not None


class FakeKeyring:
    """模拟 adapter 使用的 keyring module 子集."""

    def __init__(self) -> None:
        """创建空的 namespaced password storage."""

        self.values: MutableMapping[tuple[str, str], str] = {}

    def set_password(self, service_name: str, username: str, password: str) -> None:
        """保存 fake password."""

        self.values[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        """读取 fake password."""

        return self.values.get((service_name, username))

    def delete_password(self, service_name: str, username: str) -> None:
        """删除 fake password."""

        del self.values[(service_name, username)]


def test_memory_fake_satisfies_secret_store_contract() -> None:
    """测试 fake 符合 runtime-checkable SecretStore Protocol."""

    store = MemorySecretStore()

    assert isinstance(store, SecretStore)
    store.set_secret("provider/deepseek", "test-secret")
    assert store.get_secret("provider/deepseek") == "test-secret"
    assert store.delete_secret("provider/deepseek") is True
    assert store.delete_secret("provider/deepseek") is False


def test_keyring_is_loaded_lazily_and_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keyring 在首次操作时 import, 随后复用."""

    fake_keyring = FakeKeyring()
    imported_modules: list[str] = []

    def fake_import(module_name: str):
        """记录并满足 adapter 的 lazy import."""

        imported_modules.append(module_name)
        return fake_keyring

    monkeypatch.setattr(keyring_store, "import_module", fake_import)
    store = KeyringSecretStore(service_name="pageferry-tests")

    assert imported_modules == []
    store.set_secret("provider/deepseek", "test-secret")
    assert imported_modules == ["keyring"]
    assert fake_keyring.values == {("pageferry-tests", "provider/deepseek"): "test-secret"}
    assert store.get_secret("provider/deepseek") == "test-secret"
    assert store.delete_secret("provider/deepseek") is True
    assert store.delete_secret("provider/deepseek") is False
    assert imported_modules == ["keyring"]


def test_default_keyring_service_name_preserves_production_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认 store 继续写入稳定的 production service namespace。"""

    fake_keyring = FakeKeyring()
    monkeypatch.setattr(keyring_store, "import_module", lambda _: fake_keyring)

    KeyringSecretStore().set_secret("provider/deepseek", "test-secret")

    assert fake_keyring.values == {
        ("com.pageferry.provider-secrets", "provider/deepseek"): "test-secret"
    }


def test_missing_keyring_dependency_has_safe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少 keyring 依赖时抛出脱敏后的可用性错误."""

    import_detail = "dependency error with private detail"

    def missing_keyring(module_name: str):
        """模拟不可用的可选依赖."""

        raise ModuleNotFoundError(import_detail)

    monkeypatch.setattr(keyring_store, "import_module", missing_keyring)

    with pytest.raises(SecretStoreUnavailableError) as error_info:
        KeyringSecretStore().get_secret("provider/deepseek")

    assert import_detail not in str(error_info.value)


def test_keyring_operation_error_does_not_expose_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend 操作失败不包含凭据值."""

    secret = "sensitive-test-secret"

    class FailingKeyring(FakeKeyring):
        """模拟会通过原始异常泄漏输入的 backend."""

        def set_password(self, service_name: str, username: str, password: str) -> None:
            """携带 secret 抛错, 验证 adapter 会脱敏."""

            raise RuntimeError(password)

    monkeypatch.setattr(keyring_store, "import_module", lambda _: FailingKeyring())

    with pytest.raises(SecretStoreOperationError) as error_info:
        KeyringSecretStore().set_secret("provider/deepseek", secret)

    assert secret not in str(error_info.value)
