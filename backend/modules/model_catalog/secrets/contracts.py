"""定义 credential store contract 与脱敏错误."""

from typing import Protocol, runtime_checkable


class SecretStoreError(RuntimeError):
    """安全 secret store 错误的基类."""


class SecretStoreUnavailableError(SecretStoreError):
    """操作系统 secret backend 不可用."""


class SecretStoreOperationError(SecretStoreError):
    """Secret 读取, 写入或删除操作失败."""


@runtime_checkable
class SecretStore(Protocol):
    """通过 opaque reference 存储凭据, 不使用数据库字段."""

    def set_secret(self, reference: str, secret: str) -> None:
        """在 opaque reference 下保存一个 secret."""

        ...

    def get_secret(self, reference: str) -> str | None:
        """返回存在的 secret, 不存在时返回空值."""

        ...

    def delete_secret(self, reference: str) -> bool:
        """删除一个 secret, 并报告它是否存在."""

        ...
