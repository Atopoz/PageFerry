"""实现只在凭据操作时加载的系统 Keychain store."""

from importlib import import_module
from typing import Protocol, cast

from modules.model_catalog.secrets.contracts import (
    SecretStoreOperationError,
    SecretStoreUnavailableError,
)

DEFAULT_SERVICE_NAME = "com.pageferry.provider-secrets"


class _KeyringModule(Protocol):
    """PageFerry 调用 keyring module 时使用的最小接口。"""

    def set_password(self, service_name: str, username: str, password: str) -> None:
        """在平台 backend 中保存 password."""

        ...

    def get_password(self, service_name: str, username: str) -> str | None:
        """从平台 backend 读取 password."""

        ...

    def delete_password(self, service_name: str, username: str) -> None:
        """从平台 backend 删除 password."""

        ...


class KeyringSecretStore:
    """在操作系统 secret backend 中保存 provider 凭据."""

    def __init__(self, service_name: str = DEFAULT_SERVICE_NAME) -> None:
        """创建带 namespace 的 store, 此时不 import keyring."""

        if not service_name or not service_name.strip():
            raise ValueError("service name must not be empty")
        self._service_name = service_name
        self._keyring: _KeyringModule | None = None

    def set_secret(self, reference: str, secret: str) -> None:
        """在 opaque reference 下保存非空凭据."""

        _validate_reference(reference)
        if not secret or not secret.strip():
            raise SecretStoreOperationError("Secret value must not be empty.")
        keyring = self._load_keyring()
        try:
            keyring.set_password(self._service_name, reference, secret)
        except Exception:
            raise SecretStoreOperationError("Could not save the secret.") from None

    def get_secret(self, reference: str) -> str | None:
        """读取凭据, 且不暴露 backend 异常."""

        _validate_reference(reference)
        keyring = self._load_keyring()
        try:
            secret = keyring.get_password(self._service_name, reference)
        except Exception:
            raise SecretStoreOperationError("Could not read the secret.") from None
        if secret is not None and not isinstance(secret, str):
            raise SecretStoreOperationError("The secret store returned an invalid value.")
        return secret

    def delete_secret(self, reference: str) -> bool:
        """以幂等方式删除凭据."""

        _validate_reference(reference)
        keyring = self._load_keyring()
        try:
            if keyring.get_password(self._service_name, reference) is None:
                return False
            keyring.delete_password(self._service_name, reference)
        except Exception:
            raise SecretStoreOperationError("Could not delete the secret.") from None
        return True

    def _load_keyring(self) -> _KeyringModule:
        """首次使用时才 import keyring, 避免应用启动绑定具体 backend."""

        if self._keyring is None:
            # Lazy loading 避免仅 import API 就触发平台授权提示.
            try:
                module = import_module("keyring")
            except ImportError:
                raise SecretStoreUnavailableError(
                    "The system keyring integration is unavailable."
                ) from None
            self._keyring = cast(_KeyringModule, module)
        return self._keyring


def _validate_reference(reference: str) -> None:
    """进入平台 backend 前拒绝空 reference."""

    if not reference or not reference.strip():
        raise SecretStoreOperationError("Secret reference must not be empty.")
