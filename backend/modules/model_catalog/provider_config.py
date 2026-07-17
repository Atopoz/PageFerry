"""管理 provider 连接、模型发现、Keychain 凭据与可启用 model inventory."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from ipaddress import ip_address
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx2 as httpx

from modules.model_catalog import ProviderDefinition, load_bundled_catalog
from modules.model_catalog.provider_repository import (
    CustomProviderRecord,
    ProviderConfigRecord,
    ProviderConfigRepositoryError,
    ProviderModelRecord,
    ProviderSecretCandidateRecord,
    SQLiteProviderConfigRepository,
)
from modules.model_catalog.provider_repository import (
    ProviderInventoryItem as _InventoryItem,
)
from modules.model_catalog.providers import (
    DeepSeekProvider,
    OpenAICompatibleProvider,
    ProviderErrorCode,
    ProviderRequestError,
)
from modules.model_catalog.providers.deepseek import DEFAULT_MODEL_ID
from modules.model_catalog.secrets import SecretStore, SecretStoreError
from modules.translation.contracts import BatchTranslator

DEEPSEEK_PROVIDER_ID = "deepseek"
CUSTOM_PROVIDER_ID_PREFIX = "custom-"
CUSTOM_CHAT_PATH = "/chat/completions"
CUSTOM_MODELS_PATH = "/models"
MAX_PROVIDER_DISPLAY_NAME_LENGTH = 80
MAX_PROVIDER_BASE_URL_LENGTH = 2048
MAX_DISCOVERED_MODELS = 1000
MAX_MODEL_ID_LENGTH = 256

HttpClientFactory = Callable[[], httpx.AsyncClient]


class ProviderPublicErrorCode(StrEnum):
    """可安全返回 frontend 的稳定 provider error 分类."""

    KEY = "key"
    ENDPOINT = "endpoint"
    MODEL = "model"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    PROTOCOL = "protocol"


class ProviderConfigError(RuntimeError):
    """带脱敏公开 payload 的 provider 配置错误."""

    def __init__(
        self,
        code: ProviderPublicErrorCode,
        message: str,
        *,
        status_code: int,
    ) -> None:
        """只保留 frontend 可见信息, 不保存上游异常或 response body."""

        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ProviderModelStatus:
    """API 可见的安全 model 摘要."""

    id: str
    display_name: str
    source: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class ProviderConfigStatus:
    """API 暴露的 provider 状态, 不含 secret reference 或凭据。"""

    provider_id: str
    display_name: str
    protocol: str
    is_custom: bool
    base_url: str
    base_url_overridden: bool
    base_url_editable: bool
    deletable: bool
    available: bool
    configured: bool
    probe_status: str
    probe_error_code: str | None
    latency_ms: int | None
    model_id: str | None
    default_model_id: str | None
    enabled_model_ids: tuple[str, ...]
    models: tuple[ProviderModelStatus, ...]
    supports_model_sync: bool
    last_probed_at: str | None
    last_synced_at: str | None


@dataclass(frozen=True, slots=True)
class ProviderDiscoveryResult:
    """一次不持久化凭据的 model discovery 结果."""

    provider_id: str
    models: tuple[ProviderModelStatus, ...]


class ProviderConfigService:
    """协调 provider discovery、inference probe、Keychain 与 metadata 持久化."""

    def __init__(
        self,
        repository: SQLiteProviderConfigRepository,
        secret_store: SecretStore,
        *,
        http_client_factory: HttpClientFactory | None = None,
    ) -> None:
        """从内置 catalog 构造已实现 provider 的 runtime registry."""

        catalog = load_bundled_catalog()
        providers = tuple(provider for provider in catalog.providers if provider.available)
        for provider in providers:
            if (
                provider.protocol != "openai"
                or provider.default_base_url is None
                or provider.chat_path is None
            ):
                raise RuntimeError(f"Bundled provider definition is incomplete: {provider.id}")

        display_names = {model.id: model.display_name for model in catalog.models}
        self._providers = {provider.id: provider for provider in providers}
        self._catalog_version = catalog.catalog_version
        self._catalog_models: dict[str, dict[str, tuple[str, str]]] = {
            provider.id: {} for provider in providers
        }
        for mapping in catalog.provider_models:
            if mapping.provider_id in self._catalog_models:
                self._catalog_models[mapping.provider_id][mapping.model_id] = (
                    mapping.upstream_model_id,
                    display_names.get(mapping.model_id, mapping.model_id),
                )

        self._repository = repository
        self._secret_store = secret_store
        self._http_client_factory = http_client_factory

    def list_statuses(self) -> tuple[ProviderConfigStatus, ...]:
        """先列出全部 preset, 再追加所有自定义 provider 的安全状态。"""

        try:
            custom_records = self._repository.list_custom_providers()
            records = {record.provider_id: record for record in self._repository.list()}
            definitions = tuple(self._providers.values()) + tuple(
                _custom_definition(record) for record in custom_records
            )
            stored_models = {
                definition.id: self._repository.list_models(definition.id)
                for definition in definitions
            }
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error
        return tuple(
            self._status_from_record(
                definition,
                records.get(definition.id),
                stored_models[definition.id],
            )
            for definition in definitions
        )

    def create_custom_provider(
        self,
        *,
        display_name: str,
        base_url: str,
    ) -> ProviderConfigStatus:
        """创建未配置凭据的 OpenAI-compatible provider 并返回公开状态。"""

        normalized_name = display_name.strip()
        if not normalized_name or len(normalized_name) > MAX_PROVIDER_DISPLAY_NAME_LENGTH:
            raise ProviderConfigError(
                ProviderPublicErrorCode.ENDPOINT,
                "The custom provider display name is invalid.",
                status_code=400,
            )
        normalized_base_url = _normalize_custom_base_url(base_url)
        provider_id = f"{CUSTOM_PROVIDER_ID_PREFIX}{uuid4().hex}"
        created_at = _utc_now()
        try:
            record = self._repository.create_custom_provider(
                provider_id=provider_id,
                display_name=normalized_name,
                base_url=normalized_base_url,
                created_at=created_at,
            )
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error
        return self._status_from_record(_custom_definition(record), None, ())

    def reconcile_legacy_secret_references(self) -> None:
        """接管 0012 去重遗留的可用 Keychain reference, 并清理其余候选。"""

        try:
            candidates = self._repository.list_secret_reconciliation_candidates()
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error

        candidates_by_provider: dict[str, list[ProviderSecretCandidateRecord]] = {}
        for candidate in candidates:
            candidates_by_provider.setdefault(candidate.provider_id, []).append(candidate)
        for provider_id, provider_candidates in candidates_by_provider.items():
            self._reconcile_provider_secret_references(provider_id, provider_candidates)

    async def discover(
        self,
        provider_id: str,
        *,
        api_key: str | None,
        base_url: str | None = None,
        base_url_was_provided: bool = False,
    ) -> ProviderDiscoveryResult:
        """使用临时 Key 与 URL 获取 model, 不写 Keychain 或 SQLite."""

        definition = self._provider(provider_id)
        existing = self._provider_record(provider_id)
        temporary_override = self._resolve_base_url_override(
            definition,
            existing,
            base_url=base_url,
            was_provided=base_url_was_provided,
        )
        effective_definition = self._effective_definition(
            definition,
            existing,
            override=temporary_override,
            override_was_resolved=True,
        )
        key = self._resolve_probe_key(existing, api_key)
        inventory = await self._discover_inventory(effective_definition, key)
        return ProviderDiscoveryResult(
            provider_id=provider_id,
            models=tuple(
                ProviderModelStatus(
                    id=item.model_id,
                    display_name=item.display_name,
                    source=item.source,
                    enabled=False,
                )
                for item in inventory
            ),
        )

    async def configure(
        self,
        *,
        api_key: str | None,
        provider_id: str = DEEPSEEK_PROVIDER_ID,
        enabled_model_ids: Sequence[str] | None = None,
        default_model_id: str | None = None,
        model_id: str | None = None,
        base_url: str | None = None,
        base_url_was_provided: bool = False,
    ) -> ProviderConfigStatus:
        """发现并 probe 所有启用 model, 成功后才保存凭据与 metadata."""

        definition = self._provider(provider_id)
        existing = self._provider_record(provider_id)
        base_url_override = self._resolve_base_url_override(
            definition,
            existing,
            base_url=base_url,
            was_provided=base_url_was_provided,
        )
        effective_definition = self._effective_definition(
            definition,
            existing,
            override=base_url_override,
            override_was_resolved=True,
        )
        requested_default = default_model_id or model_id
        if requested_default is None and provider_id == DEEPSEEK_PROVIDER_ID:
            requested_default = DEFAULT_MODEL_ID
        requested_enabled = _normalize_enabled_ids(enabled_model_ids, requested_default)
        if requested_default is None or requested_default not in requested_enabled:
            raise ProviderConfigError(
                ProviderPublicErrorCode.MODEL,
                "A default model must be included in the enabled models.",
                status_code=400,
            )

        normalized_key = api_key if api_key and api_key.strip() else None
        secret_ref = existing.secret_ref if existing is not None else _secret_ref(provider_id)
        previous_secret = self._get_secret(secret_ref) if existing is not None else None
        probe_key = normalized_key or previous_secret
        if probe_key is None:
            raise ProviderConfigError(
                ProviderPublicErrorCode.KEY,
                "An API key is required for this provider.",
                status_code=401,
            )

        inventory = await self._discover_inventory(effective_definition, probe_key)
        inventory_by_id = {item.model_id: item for item in inventory}
        missing = [model for model in requested_enabled if model not in inventory_by_id]
        if missing:
            raise ProviderConfigError(
                ProviderPublicErrorCode.MODEL,
                "One or more selected models are not available.",
                status_code=400,
            )

        adapter = self._adapter(
            effective_definition,
            probe_key,
            inventory_by_id[requested_default],
        )
        probe_latencies: dict[str, int] = {}
        for selected_model_id in requested_enabled:
            try:
                probe = await adapter.probe_model(
                    inventory_by_id[selected_model_id].upstream_model_id
                )
            except ProviderRequestError as error:
                raise _map_provider_error(error, phase="inference") from None
            probe_latencies[selected_model_id] = probe.latency_ms

        probed_at = _utc_now()
        secret_changed = normalized_key is not None and normalized_key != previous_secret
        if secret_changed:
            # 只有 discovery 与启用 model 的 inference 全部成功后才更新 Keychain。
            self._set_secret(secret_ref, probe_key)
        try:
            record, models = self._repository.save_successful_probe(
                provider_id=provider_id,
                default_model_id=requested_default,
                base_url=effective_definition.default_base_url or "",
                base_url_override=base_url_override,
                catalog_version=self._catalog_version,
                secret_ref=secret_ref,
                probe_latencies=probe_latencies,
                inventory=inventory,
                probed_at=probed_at,
            )
        except ProviderConfigRepositoryError as error:
            if secret_changed:
                self._restore_secret(secret_ref, previous_secret)
            raise _repository_public_error() from error
        return self._status_from_record(definition, record, models)

    def build_translator(self, provider_id: str, model_id: str) -> BatchTranslator:
        """只把已验证且已启用的 provider-model 解析为 pipeline translator."""

        definition = self._provider(provider_id)
        existing = self._provider_record(provider_id)
        try:
            model = self._repository.get_model(provider_id, model_id)
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error
        if existing is None or existing.probe_status != "succeeded":
            raise ProviderConfigError(
                ProviderPublicErrorCode.KEY,
                "The provider has not been configured and verified.",
                status_code=409,
            )
        if model is None or not model.enabled or model.probe_status != "succeeded":
            raise ProviderConfigError(
                ProviderPublicErrorCode.MODEL,
                "The requested model is not enabled and verified.",
                status_code=400,
            )
        api_key = self._get_secret(existing.secret_ref)
        if api_key is None:
            raise ProviderConfigError(
                ProviderPublicErrorCode.KEY,
                "The provider credential is missing from system storage.",
                status_code=401,
            )
        item = _InventoryItem(
            model_id=model.model_id,
            upstream_model_id=model.upstream_model_id,
            display_name=model.display_name,
            source=model.source,
        )
        return self._adapter(self._effective_definition(definition, existing), api_key, item)

    def delete(self, provider_id: str = DEEPSEEK_PROVIDER_ID) -> bool:
        """清除 preset 配置; custom 还会删除其持久化定义。"""

        definition = self._provider(provider_id)
        is_custom = definition.id not in self._providers
        try:
            existing = self._repository.get(provider_id)
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error
        if existing is None and not is_custom:
            return False

        previous_secret = self._get_secret(existing.secret_ref) if existing is not None else None
        secret_deleted = self._delete_secret(existing.secret_ref) if existing is not None else False
        try:
            deleted = (
                self._repository.delete_custom_provider(provider_id)
                if is_custom
                else self._repository.delete(provider_id)
            )
        except ProviderConfigRepositoryError as error:
            if secret_deleted and previous_secret is not None and existing is not None:
                self._set_secret(existing.secret_ref, previous_secret)
            raise _repository_public_error() from error
        return deleted

    def _provider(self, provider_id: str) -> ProviderDefinition:
        """返回可用 preset 或已持久化的自定义 provider 定义。"""

        provider = self._providers.get(provider_id)
        if provider is not None:
            return provider
        try:
            custom = self._repository.get_custom_provider(provider_id)
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error
        if custom is not None:
            return _custom_definition(custom)
        raise ProviderConfigError(
            ProviderPublicErrorCode.ENDPOINT,
            "The requested provider is not available.",
            status_code=404,
        )

    def _reconcile_provider_secret_references(
        self,
        provider_id: str,
        candidates: Sequence[ProviderSecretCandidateRecord],
    ) -> None:
        """为一个去重后的 provider 选择可用 reference, 并完成幂等 cleanup。"""

        try:
            current = self._repository.get(provider_id)
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error

        retained_reference: str | None = None
        if current is not None:
            current_secret = self._get_secret(current.secret_ref)
            if current_secret is not None and current_secret.strip():
                retained_reference = current.secret_ref
            else:
                # winner 没有可用 Key 时按 0012 去重顺序接管第一个可用 loser。
                for candidate in candidates:
                    if candidate.secret_ref == current.secret_ref:
                        continue
                    candidate_secret = self._get_secret(candidate.secret_ref)
                    if candidate_secret is None or not candidate_secret.strip():
                        continue
                    try:
                        replaced = self._repository.replace_secret_reference(
                            provider_id=provider_id,
                            expected_reference=current.secret_ref,
                            replacement_reference=candidate.secret_ref,
                        )
                    except ProviderConfigRepositoryError as error:
                        raise _repository_public_error() from error
                    if not replaced:
                        raise _repository_public_error()
                    retained_reference = candidate.secret_ref
                    break

        for candidate in candidates:
            if candidate.secret_ref == retained_reference:
                continue
            try:
                still_in_use = self._repository.secret_reference_is_in_use(candidate.secret_ref)
            except ProviderConfigRepositoryError as error:
                raise _repository_public_error() from error
            if not still_in_use:
                self._delete_secret(candidate.secret_ref)

        try:
            # 只有 reference 接管和所有 Keychain cleanup 都完成后才清 staging。
            self._repository.clear_secret_reconciliation_candidates(provider_id)
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error

    async def _discover_inventory(
        self,
        definition: ProviderDefinition,
        api_key: str | None,
    ) -> tuple[_InventoryItem, ...]:
        """对有 models_path 的 provider 调用远端, 否则返回 catalog fallback."""

        catalog_models = self._catalog_models.get(definition.id, {})
        if definition.models_path is None:
            if not catalog_models:
                raise ProviderConfigError(
                    ProviderPublicErrorCode.MODEL,
                    "This provider has no verified catalog models.",
                    status_code=400,
                )
            return tuple(
                _InventoryItem(
                    model_id=model_id,
                    upstream_model_id=upstream_id,
                    display_name=display_name,
                    source="catalog",
                )
                for model_id, (upstream_id, display_name) in catalog_models.items()
            )
        if api_key is None:
            raise ProviderConfigError(
                ProviderPublicErrorCode.KEY,
                "An API key is required to discover provider models.",
                status_code=401,
            )

        placeholder = _InventoryItem("discovery", "discovery", "discovery", "remote")
        adapter = self._adapter(definition, api_key, placeholder)
        try:
            result = await adapter.discover_models()
        except ProviderRequestError as error:
            raise _map_provider_error(error, phase="discovery") from None
        if len(result.models) > MAX_DISCOVERED_MODELS:
            raise ProviderConfigError(
                ProviderPublicErrorCode.PROTOCOL,
                "The provider returned too many models.",
                status_code=502,
            )

        by_upstream = {
            upstream_id: (model_id, display_name)
            for model_id, (upstream_id, display_name) in catalog_models.items()
        }
        inventory: list[_InventoryItem] = []
        for discovered in result.models:
            upstream_id = discovered.model_id
            if len(upstream_id) > MAX_MODEL_ID_LENGTH:
                raise ProviderConfigError(
                    ProviderPublicErrorCode.PROTOCOL,
                    "The provider returned an invalid model id.",
                    status_code=502,
                )
            model_id, display_name = by_upstream.get(
                upstream_id,
                (upstream_id, upstream_id),
            )
            inventory.append(
                _InventoryItem(
                    model_id=model_id,
                    upstream_model_id=upstream_id,
                    display_name=display_name,
                    source="remote",
                )
            )
        return tuple(inventory)

    def _adapter(
        self,
        definition: ProviderDefinition,
        api_key: str,
        model: _InventoryItem,
    ) -> OpenAICompatibleProvider:
        """为 provider 构造 adapter, 只给 DeepSeek 注入 thinking 关闭字段."""

        is_custom = definition.id not in self._providers
        base_url = (
            _normalize_custom_base_url(definition.default_base_url or "")
            if is_custom
            else _normalize_preset_base_url(definition.default_base_url or "")
        )
        parsed_url = urlsplit(base_url)
        loopback_http = parsed_url.scheme == "http"
        kwargs = {
            "base_url": base_url,
            "chat_path": definition.chat_path or "",
            "models_path": definition.models_path,
            "model_id": model.upstream_model_id,
            "client_factory": self._http_client_factory,
            # 安全校验只会放行 loopback HTTP; 它必须绕过 proxy, 避免 Bearer Key 离机。
            "trust_env": not loopback_http,
            "max_tokens_field": (
                "max_completion_tokens" if definition.id in {"minimax", "mimo"} else "max_tokens"
            ),
        }
        if definition.id == DEEPSEEK_PROVIDER_ID:
            return DeepSeekProvider(api_key, **kwargs)
        return OpenAICompatibleProvider(api_key, **kwargs)

    def _provider_record(self, provider_id: str) -> ProviderConfigRecord | None:
        """读取一个 provider 的已存配置并统一脱敏 repository 错误。"""

        try:
            return self._repository.get(provider_id)
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error

    def _resolve_probe_key(
        self,
        record: ProviderConfigRecord | None,
        api_key: str | None,
    ) -> str | None:
        """优先使用当次输入, 空值时才读取已存 Keychain 凭据."""

        normalized = api_key if api_key and api_key.strip() else None
        if normalized is not None:
            return normalized
        return self._get_secret(record.secret_ref) if record is not None else None

    def _resolve_base_url_override(
        self,
        definition: ProviderDefinition,
        record: ProviderConfigRecord | None,
        *,
        base_url: str | None,
        was_provided: bool,
    ) -> str | None:
        """按 PUT 字段语义解析 preset override, 并拒绝伪装成可编辑的 custom。"""

        if definition.id not in self._providers:
            if was_provided:
                raise _custom_provider_base_url_not_editable()
            return None
        if not was_provided:
            return record.base_url_override if record is not None else None
        if base_url is None or not base_url.strip():
            return None
        return _normalize_preset_base_url(base_url)

    def _effective_definition(
        self,
        definition: ProviderDefinition,
        record: ProviderConfigRecord | None,
        *,
        override: str | None = None,
        override_was_resolved: bool = False,
    ) -> ProviderDefinition:
        """将已存或当次 preset override 投影到真正发请求的 runtime 定义。"""

        if definition.id not in self._providers:
            normalized_url = _normalize_custom_base_url(definition.default_base_url or "")
        else:
            stored_override = record.base_url_override if record is not None else None
            # 显式 null/blank 的含义是恢复 catalog 默认值, 不能被旧 override 覆盖。
            selected_url = override if override_was_resolved else stored_override
            normalized_url = _normalize_preset_base_url(
                selected_url or definition.default_base_url or ""
            )
        return definition.model_copy(update={"default_base_url": normalized_url})

    def _status_from_record(
        self,
        definition: ProviderDefinition,
        record: ProviderConfigRecord | None,
        models: Sequence[ProviderModelRecord],
    ) -> ProviderConfigStatus:
        """把内部 metadata 投影为 frontend 需要的 provider 状态."""

        effective_definition = self._effective_definition(definition, record)
        is_custom = definition.id not in self._providers
        if not models:
            models = tuple(
                ProviderModelRecord(
                    provider_id=definition.id,
                    model_id=model_id,
                    upstream_model_id=upstream_id,
                    display_name=display_name,
                    source="catalog",
                    enabled=False,
                    available=True,
                    probe_status="not_tested",
                    probe_error_code=None,
                    latency_ms=None,
                    last_seen_at=None,
                    last_probed_at=None,
                )
                for model_id, (upstream_id, display_name) in self._catalog_models.get(
                    definition.id, {}
                ).items()
            )
        visible_models = tuple(
            ProviderModelStatus(
                id=model.model_id,
                display_name=model.display_name,
                source=model.source,
                enabled=model.enabled,
            )
            for model in models
            if model.available
        )
        enabled_ids = tuple(model.id for model in visible_models if model.enabled)
        return ProviderConfigStatus(
            provider_id=definition.id,
            display_name=definition.display_name,
            protocol=definition.protocol,
            is_custom=is_custom,
            base_url=effective_definition.default_base_url or "",
            base_url_overridden=(
                not is_custom and record is not None and record.base_url_override is not None
            ),
            base_url_editable=not is_custom,
            deletable=is_custom,
            available=True,
            configured=record is not None and record.probe_status == "succeeded",
            probe_status=record.probe_status if record is not None else "not_configured",
            probe_error_code=record.probe_error_code if record is not None else None,
            latency_ms=record.latency_ms if record is not None else None,
            model_id=record.default_model_id if record is not None else None,
            default_model_id=record.default_model_id if record is not None else None,
            enabled_model_ids=enabled_ids,
            models=visible_models,
            supports_model_sync=definition.models_path is not None,
            last_probed_at=record.last_probed_at if record is not None else None,
            last_synced_at=record.last_synced_at if record is not None else None,
        )

    def _get_secret(self, reference: str) -> str | None:
        """通过 secret 边界读取凭据, 并把失败脱敏."""

        try:
            return self._secret_store.get_secret(reference)
        except SecretStoreError:
            raise _secret_store_public_error() from None

    def _set_secret(self, reference: str, secret: str) -> None:
        """通过 secret 边界写入凭据, 并把失败脱敏."""

        try:
            self._secret_store.set_secret(reference, secret)
        except SecretStoreError:
            raise _secret_store_public_error() from None

    def _delete_secret(self, reference: str) -> bool:
        """通过 secret 边界删除凭据, 并把失败脱敏."""

        try:
            return self._secret_store.delete_secret(reference)
        except SecretStoreError:
            raise _secret_store_public_error() from None

    def _restore_secret(self, reference: str, previous_secret: str | None) -> None:
        """补偿失败的 metadata 写入, 且不暴露凭据内容."""

        try:
            if previous_secret is None:
                self._secret_store.delete_secret(reference)
            else:
                self._secret_store.set_secret(reference, previous_secret)
        except SecretStoreError:
            raise ProviderConfigError(
                ProviderPublicErrorCode.PROTOCOL,
                "Provider configuration could not be saved safely.",
                status_code=500,
            ) from None


def _custom_definition(record: CustomProviderRecord) -> ProviderDefinition:
    """把 SQLite 中的自定义记录还原成统一的 OpenAI provider 定义。"""

    return ProviderDefinition(
        id=record.provider_id,
        display_name=record.display_name,
        protocol="openai",
        available=True,
        default_base_url=record.base_url,
        # 当前没有修改定义的 endpoint; 只能删除后重建, 因此不能向 UI 宣称可编辑。
        base_url_editable=False,
        chat_path=CUSTOM_CHAT_PATH,
        models_path=CUSTOM_MODELS_PATH,
    )


def _normalize_custom_base_url(value: str) -> str:
    """校验并规范化会接收 API Key 的自定义 provider base URL。"""

    return _normalize_provider_base_url(value, error_factory=_invalid_custom_base_url)


def _normalize_preset_base_url(value: str) -> str:
    """校验并规范化 preset 的 catalog 地址或用户 override。"""

    return _normalize_provider_base_url(value, error_factory=_invalid_provider_base_url)


def _normalize_provider_base_url(
    value: str,
    *,
    error_factory: Callable[[], ProviderConfigError],
) -> str:
    """执行所有会携带 Bearer Key 的 Base URL 共用安全校验。"""

    candidate = value.strip()
    if not candidate or len(candidate) > MAX_PROVIDER_BASE_URL_LENGTH:
        raise error_factory()
    if (
        any(character.isspace() for character in candidate)
        or "\\" in candidate
        or "?" in candidate
        or "#" in candidate
    ):
        raise error_factory()

    try:
        parsed = urlsplit(candidate)
        # 访问 port 会让 urllib 对非数字或越界端口立即报错, 避免异常 netloc 混入。
        _ = parsed.port
    except ValueError:
        raise error_factory() from None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise error_factory()
    if parsed.scheme.lower() == "http" and not _is_explicit_loopback(parsed.hostname):
        raise error_factory()

    # 保留 loopback 与显式端口以支持本地 runtime. LAN/公网只接受 HTTPS.
    normalized_path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, normalized_path, "", ""))


def _is_explicit_loopback(hostname: str) -> bool:
    """仅识别不会经公网或 LAN 明文传输 Bearer Key 的 loopback 主机。"""

    normalized = hostname.casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    if normalized == "::1":
        return True
    try:
        address = ip_address(normalized)
    except ValueError:
        return False
    return address.version == 4 and address.is_loopback


def _invalid_custom_base_url() -> ProviderConfigError:
    """返回不包含输入原文的自定义 endpoint 校验错误。"""

    return ProviderConfigError(
        ProviderPublicErrorCode.ENDPOINT,
        "The custom provider base URL is invalid.",
        status_code=400,
    )


def _invalid_provider_base_url() -> ProviderConfigError:
    """返回不包含输入原文的 preset endpoint 校验错误。"""

    return ProviderConfigError(
        ProviderPublicErrorCode.ENDPOINT,
        "The provider base URL is invalid.",
        status_code=400,
    )


def _custom_provider_base_url_not_editable() -> ProviderConfigError:
    """说明 custom 定义首版只能删除后重建, 不回显当次 URL。"""

    return ProviderConfigError(
        ProviderPublicErrorCode.ENDPOINT,
        "The custom provider base URL cannot be changed.",
        status_code=400,
    )


def _normalize_enabled_ids(
    values: Sequence[str] | None,
    default_model_id: str | None,
) -> tuple[str, ...]:
    """去重并校验 enabled model id, 旧 contract 仅传 model_id 时自动补成单元组."""

    source = (
        tuple(values)
        if values is not None
        else (() if default_model_id is None else (default_model_id,))
    )
    normalized: list[str] = []
    for value in source:
        model_id = value.strip()
        if not model_id or len(model_id) > MAX_MODEL_ID_LENGTH:
            raise ProviderConfigError(
                ProviderPublicErrorCode.MODEL,
                "A selected model id is invalid.",
                status_code=400,
            )
        if model_id not in normalized:
            normalized.append(model_id)
    if not normalized:
        raise ProviderConfigError(
            ProviderPublicErrorCode.MODEL,
            "At least one model must be enabled.",
            status_code=400,
        )
    return tuple(normalized)


def _secret_ref(provider_id: str) -> str:
    """为预置 provider 构造稳定且不含凭据的 Keychain reference."""

    return f"keychain:provider/{provider_id}"


def _map_provider_error(
    error: ProviderRequestError,
    *,
    phase: str,
) -> ProviderConfigError:
    """把 provider 细节归并为固定分类, 不转发上游文本."""

    code = error.detail.code
    if code in {
        ProviderErrorCode.MISSING_CREDENTIALS,
        ProviderErrorCode.AUTHENTICATION_FAILED,
        ProviderErrorCode.INSUFFICIENT_BALANCE,
    }:
        return ProviderConfigError(
            ProviderPublicErrorCode.KEY,
            "The provider credential could not run the probe.",
            status_code=401,
        )
    if code is ProviderErrorCode.MODEL_NOT_FOUND:
        return ProviderConfigError(
            (
                ProviderPublicErrorCode.ENDPOINT
                if phase == "discovery"
                else ProviderPublicErrorCode.MODEL
            ),
            "The provider endpoint or selected model was not found.",
            status_code=502 if phase == "discovery" else 400,
        )
    if code is ProviderErrorCode.RATE_LIMITED:
        return ProviderConfigError(
            ProviderPublicErrorCode.RATE_LIMIT,
            "The provider rate-limited the probe.",
            status_code=429,
        )
    if code in {
        ProviderErrorCode.TIMEOUT,
        ProviderErrorCode.NETWORK_ERROR,
        ProviderErrorCode.UPSTREAM_UNAVAILABLE,
    }:
        return ProviderConfigError(
            ProviderPublicErrorCode.NETWORK,
            "The provider could not be reached for the probe.",
            status_code=503,
        )
    if code is ProviderErrorCode.INVALID_REQUEST and phase == "discovery":
        return ProviderConfigError(
            ProviderPublicErrorCode.ENDPOINT,
            "The provider model-list endpoint rejected the request.",
            status_code=502,
        )
    return ProviderConfigError(
        ProviderPublicErrorCode.PROTOCOL,
        "The provider returned an invalid probe response.",
        status_code=502,
    )


def _repository_public_error() -> ProviderConfigError:
    """创建不含 SQLite 细节的稳定内部存储错误."""

    return ProviderConfigError(
        ProviderPublicErrorCode.PROTOCOL,
        "Provider configuration storage is unavailable.",
        status_code=500,
    )


def _secret_store_public_error() -> ProviderConfigError:
    """创建不含 Keychain 细节的稳定凭据存储错误."""

    return ProviderConfigError(
        ProviderPublicErrorCode.KEY,
        "System credential storage is unavailable.",
        status_code=503,
    )


def _utc_now() -> str:
    """为持久化 probe metadata 返回可排序的 UTC 时间戳."""

    return datetime.now(UTC).isoformat()
