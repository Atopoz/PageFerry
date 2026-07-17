"""管理 provider 连接、模型发现、Keychain 凭据与可启用 model inventory."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from ipaddress import ip_address
from threading import Lock
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx2 as httpx

from modules.model_catalog import ProviderDefinition, load_bundled_catalog
from modules.model_catalog.provider_repository import (
    CustomProviderRecord,
    ProviderConfigRecord,
    ProviderConfigRepositoryError,
    ProviderInventoryMergeResult,
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
from modules.translation.model_runtime import (
    DEFAULT_GLOBAL_CONCURRENCY,
    DEFAULT_PER_JOB_CONCURRENCY,
    MAX_MODEL_CONCURRENCY,
    ModelConcurrencyRegistry,
)

DEEPSEEK_PROVIDER_ID = "deepseek"
CUSTOM_PROVIDER_ID_PREFIX = "custom-"
CUSTOM_CHAT_PATH = "/chat/completions"
CUSTOM_MODELS_PATH = "/models"
MAX_PROVIDER_DISPLAY_NAME_LENGTH = 80
MAX_PROVIDER_BASE_URL_LENGTH = 2048
MAX_DISCOVERED_MODELS = 1000
MAX_MODEL_ID_LENGTH = 256
MAX_MODEL_DISPLAY_NAME_LENGTH = 120
REASONING_POLICIES = frozenset({"provider_default", "off", "on", "low", "medium", "high", "max"})
_MISSING = object()

HttpClientFactory = Callable[[], httpx.AsyncClient]


class ProviderPublicErrorCode(StrEnum):
    """可安全返回 frontend 的稳定 provider error 分类."""

    KEY = "key"
    ENDPOINT = "endpoint"
    MODEL = "model"
    MODEL_REQUIRED = "model_required"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    PROTOCOL = "protocol"
    CONFLICT = "conflict"


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
    available: bool
    probe_status: str
    probe_error_code: str | None
    latency_ms: int | None
    last_probed_at: str | None
    reasoning_policy: str | None
    reasoning_policy_override: str | None
    supported_reasoning_policies: tuple[str, ...]
    per_job_concurrency: int
    per_job_concurrency_override: int | None
    global_concurrency: int
    global_concurrency_override: int | None


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
    active: bool
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


@dataclass(frozen=True, slots=True)
class ProviderProbeResult:
    """一次不持久化任何配置的最小 inference probe 结果。"""

    provider_id: str
    model_id: str
    display_name: str
    latency_ms: int


@dataclass(frozen=True, slots=True)
class ProviderInventorySyncResult:
    """一次已存凭据 inventory sync 的公开增量结果。"""

    provider_id: str
    added: int
    restored: int
    unavailable: int
    unchanged: int
    last_synced_at: str
    models: tuple[ProviderModelStatus, ...]


@dataclass(frozen=True, slots=True)
class _CatalogModel:
    """一个 catalog model 的显示、上游身份与 reasoning contract。"""

    upstream_model_id: str
    display_name: str
    enabled_by_default: bool
    supported_reasoning_policies: tuple[str, ...]
    default_reasoning_policy: str | None


class ProviderConfigService:
    """协调 provider discovery、inference probe、Keychain 与 metadata 持久化."""

    def __init__(
        self,
        repository: SQLiteProviderConfigRepository,
        secret_store: SecretStore,
        *,
        http_client_factory: HttpClientFactory | None = None,
        concurrency_registry: ModelConcurrencyRegistry | None = None,
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
        self._catalog_models: dict[str, dict[str, _CatalogModel]] = {
            provider.id: {} for provider in providers
        }
        for mapping in catalog.provider_models:
            if mapping.provider_id in self._catalog_models:
                self._catalog_models[mapping.provider_id][mapping.model_id] = _CatalogModel(
                    upstream_model_id=mapping.upstream_model_id,
                    display_name=display_names.get(mapping.model_id, mapping.model_id),
                    enabled_by_default=mapping.enabled_by_default,
                    supported_reasoning_policies=tuple(mapping.supported_reasoning_policies),
                    default_reasoning_policy=mapping.default_reasoning_policy,
                )

        self._repository = repository
        self._secret_store = secret_store
        self._http_client_factory = http_client_factory
        self._concurrency_registry = concurrency_registry or ModelConcurrencyRegistry()
        # provider record、Keychain secret、model settings 与共享 limiter 共同组成
        # 一份 runtime snapshot。短临界区统一串行, 防止旧 URL 与新 Key 被拼在一起。
        self._provider_state_lock = Lock()

    def list_statuses(self) -> tuple[ProviderConfigStatus, ...]:
        """先列出全部 preset, 再追加所有自定义 provider 的安全状态。"""

        with self._provider_state_lock:
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

    def get_api_key(self, provider_id: str) -> str:
        """只为一个已配置 provider 读取 Keychain 中的完整 API Key。"""

        with self._provider_state_lock:
            self._provider(provider_id)
            record = self._provider_record(provider_id)
            if record is None or record.probe_status != "succeeded":
                raise ProviderConfigError(
                    ProviderPublicErrorCode.KEY,
                    "The provider has not been configured.",
                    status_code=409,
                )
            api_key = self._get_secret(record.secret_ref)
            if api_key is None or not api_key.strip():
                raise ProviderConfigError(
                    ProviderPublicErrorCode.KEY,
                    "The provider credential is missing from system storage.",
                    status_code=409,
                )
            return api_key

    def create_custom_provider(
        self,
        *,
        display_name: str,
        base_url: str,
    ) -> ProviderConfigStatus:
        """创建未配置凭据的 OpenAI-compatible provider 并返回公开状态。"""

        with self._provider_state_lock:
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

        with self._provider_state_lock:
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
        with self._provider_state_lock:
            inventory = self._merge_manual_inventory(
                inventory,
                self._stored_models_locked(provider_id),
            )
        return ProviderDiscoveryResult(
            provider_id=provider_id,
            models=tuple(
                self._model_status_from_inventory(definition.id, item) for item in inventory
            ),
        )

    async def probe(
        self,
        provider_id: str,
        *,
        api_key: str | None,
        base_url: str | None = None,
        base_url_was_provided: bool = False,
        model_id: str | None = None,
    ) -> ProviderProbeResult:
        """用临时配置执行一次 inference probe, 且不写入任何本地状态。"""

        with self._provider_state_lock:
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
            stored_models = self._stored_models_locked(provider_id)
        if key is None:
            raise ProviderConfigError(
                ProviderPublicErrorCode.KEY,
                "An API key is required for this provider.",
                status_code=401,
            )

        discovery_error: ProviderConfigError | None = None
        try:
            inventory = await self._discover_inventory(effective_definition, key)
        except ProviderConfigError as error:
            # `/models` 不是 inference 能力的权威判断。失败时仍允许 catalog 与用户
            # 明确登记的 manual model 完成真实 probe, remote-only 候选则不会被猜测。
            discovery_error = error
            inventory = self._catalog_inventory(provider_id)
        inventory = self._merge_manual_inventory(inventory, stored_models)
        if not inventory:
            if discovery_error is not None:
                raise discovery_error
            raise ProviderConfigError(
                ProviderPublicErrorCode.MODEL,
                "At least one model is required for the probe.",
                status_code=400,
            )

        selected_model_id = self._select_default_model(
            provider_id,
            inventory,
            requested=model_id,
            existing=existing,
        )
        if selected_model_id is None:
            raise ProviderConfigError(
                ProviderPublicErrorCode.MODEL,
                "At least one model is required for the probe.",
                status_code=400,
            )
        selected = next(item for item in inventory if item.model_id == selected_model_id)
        stored_model = next(
            (item for item in stored_models if item.model_id == selected_model_id),
            None,
        )
        runtime_settings = (
            self._model_status_from_record(stored_model)
            if stored_model is not None
            else self._model_status_from_inventory(provider_id, selected)
        )
        adapter = self._adapter(
            effective_definition,
            key,
            selected,
            runtime_settings=runtime_settings,
        )
        try:
            result = await adapter.probe_model()
        except ProviderRequestError as error:
            raise _map_provider_error(error, phase="inference") from None
        return ProviderProbeResult(
            provider_id=provider_id,
            model_id=selected.model_id,
            display_name=selected.display_name,
            latency_ms=result.latency_ms,
        )

    async def sync_inventory(self, provider_id: str) -> ProviderInventorySyncResult:
        """仅用已存 Key 拉取远端 inventory, 并执行不破坏选择的幂等 merge。"""

        with self._provider_state_lock:
            definition = self._provider(provider_id)
            if definition.models_path is None:
                raise ProviderConfigError(
                    ProviderPublicErrorCode.MODEL,
                    "This provider does not support remote model synchronization.",
                    status_code=400,
                )
            existing = self._provider_record(provider_id)
            if existing is None or existing.probe_status != "succeeded":
                raise ProviderConfigError(
                    ProviderPublicErrorCode.KEY,
                    "The provider must be configured before model synchronization.",
                    status_code=409,
                )
            api_key = self._get_secret(existing.secret_ref)
            if api_key is None:
                raise ProviderConfigError(
                    ProviderPublicErrorCode.KEY,
                    "The provider credential is missing from system storage.",
                    status_code=401,
                )
            effective_definition = self._effective_definition(definition, existing)
        inventory = await self._discover_inventory(
            effective_definition,
            api_key,
        )
        with self._provider_state_lock:
            self._assert_provider_snapshot_unchanged(
                provider_id,
                expected_record=existing,
                expected_secret=api_key,
            )
            try:
                merged = self._repository.merge_inventory(
                    provider_id=provider_id,
                    inventory=inventory,
                    synced_at=_utc_now(),
                )
            except ProviderConfigRepositoryError as error:
                raise _repository_public_error() from error
        return self._sync_result(definition.id, merged)

    def add_manual_model(
        self,
        provider_id: str,
        *,
        model_id: str,
        display_name: str | None = None,
    ) -> ProviderModelStatus:
        """登记一个待 probe 的手动 model, 不提前把它标成 runtime enabled。"""

        normalized_model_id = _normalize_manual_model_id(model_id)
        normalized_display_name = _normalize_manual_model_display_name(
            display_name,
            fallback=normalized_model_id,
        )
        with self._provider_state_lock:
            self._provider(provider_id)
            catalog_models = self._catalog_models.get(provider_id, {})
            if normalized_model_id in catalog_models or any(
                model.upstream_model_id == normalized_model_id for model in catalog_models.values()
            ):
                raise ProviderConfigError(
                    ProviderPublicErrorCode.MODEL,
                    "The model already exists in this provider inventory.",
                    status_code=409,
                )
            try:
                stored_models = self._repository.list_models(provider_id)
            except ProviderConfigRepositoryError as error:
                raise _repository_public_error() from error
            matches = {
                model.model_id: model
                for model in stored_models
                if model.model_id == normalized_model_id
                or model.upstream_model_id == normalized_model_id
            }
            if len(matches) > 1:
                raise ProviderConfigError(
                    ProviderPublicErrorCode.MODEL,
                    "The model already exists in this provider inventory.",
                    status_code=409,
                )
            if matches:
                existing_model = next(iter(matches.values()))
                if existing_model.source != "remote":
                    raise ProviderConfigError(
                        ProviderPublicErrorCode.MODEL,
                        "The model already exists in this provider inventory.",
                        status_code=409,
                    )
                try:
                    promoted = self._repository.promote_remote_model_to_manual(
                        provider_id=provider_id,
                        model_id=existing_model.model_id,
                        display_name=normalized_display_name,
                        updated_at=_utc_now(),
                    )
                except ProviderConfigRepositoryError as error:
                    raise _repository_public_error() from error
                if promoted is None:
                    raise _provider_state_conflict()
                return self._model_status_from_record(promoted)
            try:
                created = self._repository.create_manual_model(
                    provider_id=provider_id,
                    model_id=normalized_model_id,
                    display_name=normalized_display_name,
                    created_at=_utc_now(),
                )
            except ProviderConfigRepositoryError as error:
                raise _repository_public_error() from error
        return self._model_status_from_record(created)

    def update_model_settings(
        self,
        provider_id: str,
        model_id: str,
        *,
        reasoning_policy_override: str | None | object = _MISSING,
        per_job_concurrency_override: int | None | object = _MISSING,
        global_concurrency_override: int | None | object = _MISSING,
    ) -> ProviderModelStatus:
        """按缺省保留、NULL 恢复的语义更新一个已启用 model。"""

        with self._provider_state_lock:
            return self._update_model_settings_locked(
                provider_id,
                model_id,
                reasoning_policy_override=reasoning_policy_override,
                per_job_concurrency_override=per_job_concurrency_override,
                global_concurrency_override=global_concurrency_override,
            )

    def set_active(self, provider_id: str, *, active: bool) -> ProviderConfigStatus:
        """非破坏切换 provider active 状态, 启用前校验本地 runtime 前提。"""

        with self._provider_state_lock:
            definition = self._provider(provider_id)
            record = self._provider_record(provider_id)
            if record is None or record.probe_status != "succeeded":
                raise ProviderConfigError(
                    ProviderPublicErrorCode.CONFLICT,
                    "The provider must be configured before changing its active state.",
                    status_code=409,
                )
            models = self._stored_models_locked(provider_id)
            if active:
                secret = self._get_secret(record.secret_ref)
                if secret is None or not secret.strip():
                    raise ProviderConfigError(
                        ProviderPublicErrorCode.KEY,
                        "The provider credential is missing from system storage.",
                        status_code=409,
                    )
                if not any(_is_runnable_model(model) for model in models):
                    raise ProviderConfigError(
                        ProviderPublicErrorCode.MODEL_REQUIRED,
                        "At least one enabled and verified model is required.",
                        status_code=409,
                    )
            if record.active == active:
                return self._status_from_record(definition, record, models)
            try:
                updated = self._repository.set_active(
                    provider_id=provider_id,
                    active=active,
                    updated_at=_utc_now(),
                )
            except ProviderConfigRepositoryError as error:
                raise _repository_public_error() from error
            if updated is None:
                raise _provider_state_conflict()
            return self._status_from_record(definition, updated, models)

    async def set_model_enabled(
        self,
        provider_id: str,
        model_id: str,
        *,
        enabled: bool,
    ) -> ProviderConfigStatus:
        """切换单个 model, 启用先 probe, 停用只执行本地原子更新。"""

        if not enabled:
            with self._provider_state_lock:
                return self._disable_model_locked(provider_id, model_id)

        with self._provider_state_lock:
            definition = self._provider(provider_id)
            record = self._provider_record(provider_id)
            if record is None or record.probe_status != "succeeded":
                raise ProviderConfigError(
                    ProviderPublicErrorCode.CONFLICT,
                    "The provider must be configured before enabling a model.",
                    status_code=409,
                )
            try:
                model = self._repository.get_model(provider_id, model_id)
            except ProviderConfigRepositoryError as error:
                raise _repository_public_error() from error
            if model is None:
                catalog_model = self._catalog_models.get(provider_id, {}).get(model_id)
                if catalog_model is None:
                    raise ProviderConfigError(
                        ProviderPublicErrorCode.MODEL,
                        "The requested model does not exist.",
                        status_code=404,
                    )
                item = _InventoryItem(
                    model_id=model_id,
                    upstream_model_id=catalog_model.upstream_model_id,
                    display_name=catalog_model.display_name,
                    source="catalog",
                )
                runtime_settings = self._model_status_from_inventory(provider_id, item)
            else:
                if not model.available:
                    raise ProviderConfigError(
                        ProviderPublicErrorCode.MODEL,
                        "An unavailable model cannot be enabled.",
                        status_code=400,
                    )
                if _is_runnable_model(model):
                    models = self._stored_models_locked(provider_id)
                    return self._status_from_record(definition, record, models)
                runtime_settings = self._model_status_from_record(model)
                item = _InventoryItem(
                    model_id=model.model_id,
                    upstream_model_id=model.upstream_model_id,
                    display_name=model.display_name,
                    source=model.source,
                )
            api_key = self._get_secret(record.secret_ref)
            if api_key is None or not api_key.strip():
                raise ProviderConfigError(
                    ProviderPublicErrorCode.KEY,
                    "The provider credential is missing from system storage.",
                    status_code=409,
                )
            effective_definition = self._effective_definition(definition, record)

        adapter = self._adapter(
            effective_definition,
            api_key,
            item,
            runtime_settings=runtime_settings,
        )
        try:
            probe = await adapter.probe_model()
        except ProviderRequestError as error:
            raise _map_provider_error(error, phase="inference") from None

        probed_at = _utc_now()
        with self._provider_state_lock:
            self._provider(provider_id)
            self._assert_provider_snapshot_unchanged(
                provider_id,
                expected_record=record,
                expected_secret=api_key,
            )
            self._assert_model_snapshots_unchanged(
                provider_id,
                expected_models={model_id: model},
            )
            try:
                snapshot = self._repository.enable_model_after_probe(
                    provider_id=provider_id,
                    model_id=model_id,
                    latency_ms=probe.latency_ms,
                    probed_at=probed_at,
                    materialize_model=item if model is None else None,
                )
            except ProviderConfigRepositoryError as error:
                raise _repository_public_error() from error
            if snapshot is None:
                raise _provider_state_conflict()
            updated_record, models = snapshot
            return self._status_from_record(definition, updated_record, models)

    def _disable_model_locked(
        self,
        provider_id: str,
        model_id: str,
    ) -> ProviderConfigStatus:
        """在 state lock 内停用 model, 并在需要时原子切换 default。"""

        definition = self._provider(provider_id)
        record = self._provider_record(provider_id)
        if record is None or record.probe_status != "succeeded":
            raise ProviderConfigError(
                ProviderPublicErrorCode.CONFLICT,
                "The provider must be configured before disabling a model.",
                status_code=409,
            )
        models = self._stored_models_locked(provider_id)
        model = next((candidate for candidate in models if candidate.model_id == model_id), None)
        if model is None:
            raise ProviderConfigError(
                ProviderPublicErrorCode.MODEL,
                "The requested model does not exist.",
                status_code=404,
            )
        if not model.enabled:
            return self._status_from_record(definition, record, models)
        enabled_models = [candidate for candidate in models if candidate.enabled]
        if len(enabled_models) <= 1:
            raise ProviderConfigError(
                ProviderPublicErrorCode.MODEL_REQUIRED,
                "The last enabled model cannot be disabled.",
                status_code=409,
            )
        remaining_enabled = [candidate for candidate in enabled_models if candidate != model]
        if record.active and not any(
            candidate.available and candidate.probe_status == "succeeded"
            for candidate in remaining_enabled
        ):
            raise ProviderConfigError(
                ProviderPublicErrorCode.MODEL_REQUIRED,
                "An active provider must keep an available and verified model enabled.",
                status_code=409,
            )

        replacement: ProviderModelRecord | None = None
        if record.default_model_id == model_id:
            model_index = enabled_models.index(model)
            ordered_candidates = enabled_models[model_index + 1 :] + enabled_models[:model_index]
            replacement = next(
                (
                    candidate
                    for candidate in ordered_candidates
                    if candidate.available and candidate.probe_status == "succeeded"
                ),
                None,
            )
            if replacement is None:
                raise ProviderConfigError(
                    ProviderPublicErrorCode.MODEL_REQUIRED,
                    "A verified default model replacement is required.",
                    status_code=409,
                )
        try:
            snapshot = self._repository.disable_model(
                provider_id=provider_id,
                model_id=model_id,
                replacement_default_model_id=(
                    replacement.model_id if replacement is not None else None
                ),
                replacement_latency_ms=(
                    replacement.latency_ms if replacement is not None else None
                ),
                updated_at=_utc_now(),
            )
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error
        if snapshot is None:
            raise _provider_state_conflict()
        updated_record, updated_models = snapshot
        return self._status_from_record(definition, updated_record, updated_models)

    def _update_model_settings_locked(
        self,
        provider_id: str,
        model_id: str,
        *,
        reasoning_policy_override: str | None | object,
        per_job_concurrency_override: int | None | object,
        global_concurrency_override: int | None | object,
    ) -> ProviderModelStatus:
        """在 app-scoped lock 内完成 model settings 的 read-modify-write。"""

        self._provider(provider_id)
        try:
            current = self._repository.get_model(provider_id, model_id)
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error
        if current is None or not current.enabled:
            raise ProviderConfigError(
                ProviderPublicErrorCode.MODEL,
                "Runtime settings can only be changed for an enabled model.",
                status_code=400,
            )

        catalog_model = self._catalog_model_for_record(current)
        supported_policies = (
            catalog_model.supported_reasoning_policies if catalog_model is not None else ()
        )
        reasoning_override = (
            current.reasoning_policy_override
            if reasoning_policy_override is _MISSING
            else _normalize_reasoning_override(reasoning_policy_override)
        )
        if reasoning_policy_override is not _MISSING and (
            reasoning_override is not None and reasoning_override not in supported_policies
        ):
            raise ProviderConfigError(
                ProviderPublicErrorCode.MODEL,
                "The selected reasoning policy is not supported by this model.",
                status_code=400,
            )
        per_job_override = (
            current.per_job_concurrency_override
            if per_job_concurrency_override is _MISSING
            else _normalize_concurrency_override(per_job_concurrency_override)
        )
        global_override = (
            current.global_concurrency_override
            if global_concurrency_override is _MISSING
            else _normalize_concurrency_override(global_concurrency_override)
        )
        effective_per_job = per_job_override or DEFAULT_PER_JOB_CONCURRENCY
        effective_global = global_override or DEFAULT_GLOBAL_CONCURRENCY
        if effective_per_job > effective_global:
            raise ProviderConfigError(
                ProviderPublicErrorCode.MODEL,
                "Per-job concurrency cannot exceed global concurrency.",
                status_code=400,
            )

        try:
            updated = self._repository.update_model_runtime_settings(
                provider_id=provider_id,
                model_id=model_id,
                reasoning_policy_override=(
                    reasoning_override if isinstance(reasoning_override, str) else None
                ),
                per_job_concurrency_override=(
                    per_job_override if isinstance(per_job_override, int) else None
                ),
                global_concurrency_override=(
                    global_override if isinstance(global_override, int) else None
                ),
                updated_at=_utc_now(),
            )
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error
        if updated is None:
            raise ProviderConfigError(
                ProviderPublicErrorCode.MODEL,
                "Runtime settings can only be changed for an enabled model.",
                status_code=400,
            )
        status = self._model_status_from_record(updated)
        self._concurrency_registry.configure(
            (updated.provider_id, updated.upstream_model_id),
            status.global_concurrency,
        )
        return status

    async def configure(
        self,
        *,
        api_key: str | None,
        provider_id: str = DEEPSEEK_PROVIDER_ID,
        enabled_model_ids: Sequence[str] | None = None,
        default_model_id: str | None = None,
        model_id: str | None = None,
        enable_all_models: bool = False,
        base_url: str | None = None,
        base_url_was_provided: bool = False,
    ) -> ProviderConfigStatus:
        """发现并 probe 所有启用 model, 成功后才保存凭据与 metadata."""

        with self._provider_state_lock:
            definition = self._provider(provider_id)
            existing = self._provider_record(provider_id)
            previous_secret = (
                self._get_secret(existing.secret_ref) if existing is not None else None
            )
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
        if enable_all_models and enabled_model_ids:
            raise ProviderConfigError(
                ProviderPublicErrorCode.MODEL,
                "enable_all_models cannot be combined with enabled_model_ids.",
                status_code=400,
            )
        if not enable_all_models:
            if requested_default is None and provider_id == DEEPSEEK_PROVIDER_ID:
                requested_default = DEFAULT_MODEL_ID
            requested_enabled = _normalize_enabled_ids(enabled_model_ids, requested_default)
            if requested_default is None or requested_default not in requested_enabled:
                raise ProviderConfigError(
                    ProviderPublicErrorCode.MODEL,
                    "A default model must be included in the enabled models.",
                    status_code=400,
                )
        else:
            requested_enabled = ()

        normalized_key = api_key if api_key and api_key.strip() else None
        secret_ref = existing.secret_ref if existing is not None else _secret_ref(provider_id)
        probe_key = normalized_key or previous_secret
        if probe_key is None:
            raise ProviderConfigError(
                ProviderPublicErrorCode.KEY,
                "An API key is required for this provider.",
                status_code=401,
            )

        discovery_error: ProviderConfigError | None = None
        try:
            inventory = await self._discover_inventory(effective_definition, probe_key)
        except ProviderConfigError as error:
            discovery_error = error
            inventory = self._catalog_inventory(provider_id)

        with self._provider_state_lock:
            # discovery 与 inference 都不持锁。这里冻结完整 manual inventory; 最终保存会
            # 重写该 provider 的所有 model row, 因此不能只对已勾选行做 CAS。
            self._provider(provider_id)
            stored_models = self._stored_models_locked(provider_id)
            manual_model_snapshots = {
                model.model_id: model for model in stored_models if model.source == "manual"
            }
            if discovery_error is not None and not enable_all_models:
                fallback_ids = {item.model_id for item in inventory} | set(manual_model_snapshots)
                if not set(requested_enabled).issubset(fallback_ids):
                    raise discovery_error
                # `/models` 只补充账号 inventory; catalog/manual model 仍以真实 inference 为准。
            inventory = self._merge_manual_inventory(inventory, stored_models)
            inventory_by_id = {item.model_id: item for item in inventory}
            if enable_all_models:
                if discovery_error is not None and not inventory:
                    raise discovery_error
                requested_enabled = tuple(inventory_by_id)
                requested_default = self._select_default_model(
                    provider_id,
                    inventory,
                    requested=requested_default,
                    existing=existing,
                )
                if not requested_enabled or requested_default is None:
                    raise ProviderConfigError(
                        ProviderPublicErrorCode.MODEL,
                        "At least one model must be available for configuration.",
                        status_code=400,
                    )
            missing = [model for model in requested_enabled if model not in inventory_by_id]
            if missing:
                raise ProviderConfigError(
                    ProviderPublicErrorCode.MODEL,
                    "One or more selected models are not available.",
                    status_code=400,
                )
            stored_by_id = {model.model_id: model for model in stored_models}
            stored_model_snapshots = {
                selected_model_id: stored_by_id.get(selected_model_id)
                for selected_model_id in requested_enabled
            }
        probe_latencies: dict[str, int] = {}
        for selected_model_id in requested_enabled:
            inventory_item = inventory_by_id[selected_model_id]
            stored_model = stored_model_snapshots[selected_model_id]
            runtime_settings = (
                self._model_status_from_record(stored_model)
                if stored_model is not None
                else self._model_status_from_inventory(provider_id, inventory_item)
            )
            # 每个 model 都用自己的 effective reasoning policy 做真实 probe.
            # 不能复用 default model 的 adapter, 否则多模型配置会验证错 payload。
            adapter = self._adapter(
                effective_definition,
                probe_key,
                inventory_item,
                runtime_settings=runtime_settings,
            )
            try:
                probe = await adapter.probe_model()
            except ProviderRequestError as error:
                raise _map_provider_error(error, phase="inference") from None
            probe_latencies[selected_model_id] = probe.latency_ms

        probed_at = _utc_now()
        secret_changed = normalized_key is not None and normalized_key != previous_secret
        with self._provider_state_lock:
            # 网络 probe 不持锁; 提交前同时检查 provider、secret、所选 runtime
            # 与完整 manual inventory, 拒绝任何会覆盖较新状态的迟到结果。
            self._provider(provider_id)
            self._assert_provider_snapshot_unchanged(
                provider_id,
                expected_record=existing,
                expected_secret=previous_secret,
            )
            self._assert_model_snapshots_unchanged(
                provider_id,
                expected_models=stored_model_snapshots,
            )
            self._assert_manual_inventory_snapshot_unchanged(
                provider_id,
                expected_models=manual_model_snapshots,
            )
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

        with self._provider_state_lock:
            definition = self._provider(provider_id)
            existing = self._provider_record(provider_id)
            if existing is None or existing.probe_status != "succeeded":
                raise ProviderConfigError(
                    ProviderPublicErrorCode.KEY,
                    "The provider has not been configured and verified.",
                    status_code=409,
                )
            if not existing.active:
                raise ProviderConfigError(
                    ProviderPublicErrorCode.CONFLICT,
                    "The provider is inactive.",
                    status_code=409,
                )
            try:
                model = self._repository.get_model(provider_id, model_id)
            except ProviderConfigRepositoryError as error:
                raise _repository_public_error() from error
            if (
                model is None
                or not model.enabled
                or not model.available
                or model.probe_status != "succeeded"
            ):
                raise ProviderConfigError(
                    ProviderPublicErrorCode.MODEL,
                    "The requested model is not enabled and verified.",
                    status_code=400,
                )
            runtime_settings = self._model_status_from_record(model)
            self._concurrency_registry.configure(
                (provider_id, model.upstream_model_id),
                runtime_settings.global_concurrency,
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
            return self._adapter(
                self._effective_definition(definition, existing),
                api_key,
                item,
                runtime_settings=runtime_settings,
            )

    def delete(self, provider_id: str = DEEPSEEK_PROVIDER_ID) -> bool:
        """清除 preset 配置; custom 还会删除其持久化定义。"""

        with self._provider_state_lock:
            return self._delete_locked(provider_id)

    def _delete_locked(self, provider_id: str) -> bool:
        """在 provider state lock 内删除 metadata 与 secret。"""

        definition = self._provider(provider_id)
        is_custom = definition.id not in self._providers
        try:
            existing = self._repository.get(provider_id)
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error
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
        """以 catalog 为稳定 baseline, 并为有 models_path 的 provider 合并远端结果。"""

        catalog_models = self._catalog_models.get(definition.id, {})
        catalog_inventory = self._catalog_inventory(definition.id)
        if definition.models_path is None:
            if not catalog_inventory:
                raise ProviderConfigError(
                    ProviderPublicErrorCode.MODEL,
                    "This provider has no verified catalog models.",
                    status_code=400,
                )
            return catalog_inventory
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
            model.upstream_model_id: model_id for model_id, model in catalog_models.items()
        }
        inventory = list(catalog_inventory)
        seen_upstream_ids = {item.upstream_model_id for item in inventory}
        seen_model_ids = {item.model_id for item in inventory}
        for discovered in result.models:
            upstream_id = discovered.model_id
            if len(upstream_id) > MAX_MODEL_ID_LENGTH:
                raise ProviderConfigError(
                    ProviderPublicErrorCode.PROTOCOL,
                    "The provider returned an invalid model id.",
                    status_code=502,
                )
            if upstream_id in seen_upstream_ids:
                continue
            model_id = by_upstream.get(upstream_id, upstream_id)
            if model_id in seen_model_ids:
                continue
            inventory.append(
                _InventoryItem(
                    model_id=model_id,
                    upstream_model_id=upstream_id,
                    display_name=upstream_id,
                    source="remote",
                )
            )
            seen_upstream_ids.add(upstream_id)
            seen_model_ids.add(model_id)
        return tuple(inventory)

    def _catalog_inventory(self, provider_id: str) -> tuple[_InventoryItem, ...]:
        """返回一个 provider 随应用发版且无需远端 discovery 的 baseline。"""

        return tuple(
            _InventoryItem(
                model_id=model_id,
                upstream_model_id=model.upstream_model_id,
                display_name=model.display_name,
                source="catalog",
            )
            for model_id, model in self._catalog_models.get(provider_id, {}).items()
        )

    def _stored_models_locked(self, provider_id: str) -> tuple[ProviderModelRecord, ...]:
        """在 state lock 内读取完整 model inventory, 并统一 repository 错误。"""

        try:
            return self._repository.list_models(provider_id)
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error

    def _select_default_model(
        self,
        provider_id: str,
        inventory: Sequence[_InventoryItem],
        *,
        requested: str | None,
        existing: ProviderConfigRecord | None,
    ) -> str | None:
        """为 enable-all 选择显式、既有、catalog 默认或首个候选。"""

        inventory_ids = {item.model_id for item in inventory}
        if requested is not None:
            normalized = requested.strip()
            if not normalized or len(normalized) > MAX_MODEL_ID_LENGTH:
                raise ProviderConfigError(
                    ProviderPublicErrorCode.MODEL,
                    "A selected model id is invalid.",
                    status_code=400,
                )
            if normalized not in inventory_ids:
                raise ProviderConfigError(
                    ProviderPublicErrorCode.MODEL,
                    "The default model is not available.",
                    status_code=400,
                )
            return normalized
        if existing is not None and existing.default_model_id in inventory_ids:
            return existing.default_model_id
        catalog_models = self._catalog_models.get(provider_id, {})
        for item in inventory:
            catalog_model = catalog_models.get(item.model_id)
            if catalog_model is not None and catalog_model.enabled_by_default:
                return item.model_id
        return inventory[0].model_id if inventory else None

    def _merge_manual_inventory(
        self,
        inventory: Sequence[_InventoryItem],
        stored_models: Sequence[ProviderModelRecord],
    ) -> tuple[_InventoryItem, ...]:
        """把手动 model 合进候选; catalog 可接管身份, remote 不覆盖用户显示名。"""

        merged = list(inventory)
        by_model_id = {item.model_id: index for index, item in enumerate(merged)}
        by_upstream_id = {item.upstream_model_id: index for index, item in enumerate(merged)}
        for model in stored_models:
            if model.source != "manual":
                continue
            manual_item = _InventoryItem(
                model_id=model.model_id,
                upstream_model_id=model.upstream_model_id,
                display_name=model.display_name,
                source="manual",
            )
            existing_index = by_upstream_id.get(model.upstream_model_id)
            if existing_index is None:
                existing_index = by_model_id.get(model.model_id)
            if existing_index is None:
                by_model_id[model.model_id] = len(merged)
                by_upstream_id[model.upstream_model_id] = len(merged)
                merged.append(manual_item)
                continue
            if merged[existing_index].source == "catalog":
                # 应用 catalog 是经过版本发布的稳定 contract, 优先于旧的手动登记。
                continue
            merged[existing_index] = manual_item
            by_model_id[model.model_id] = existing_index
            by_upstream_id[model.upstream_model_id] = existing_index
        return tuple(merged)

    def _adapter(
        self,
        definition: ProviderDefinition,
        api_key: str,
        model: _InventoryItem,
        *,
        runtime_settings: ProviderModelStatus | None = None,
    ) -> OpenAICompatibleProvider:
        """为 provider 构造 adapter, translator 额外接入 model runtime settings。"""

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
        if runtime_settings is not None:
            kwargs.update(
                {
                    "provider_id": definition.id,
                    # catalog 外 model 不获得私有 reasoning 字段。DeepSeek adapter
                    # 自身仍保留 direct-use 的旧默认, service 必须显式覆盖它。
                    "reasoning_policy": (runtime_settings.reasoning_policy or "provider_default"),
                    "per_job_concurrency": runtime_settings.per_job_concurrency,
                    "global_concurrency": runtime_settings.global_concurrency,
                    "concurrency_registry": self._concurrency_registry,
                }
            )
        if definition.id == DEEPSEEK_PROVIDER_ID:
            return DeepSeekProvider(api_key, **kwargs)
        return OpenAICompatibleProvider(api_key, **kwargs)

    def _provider_record(self, provider_id: str) -> ProviderConfigRecord | None:
        """读取一个 provider 的已存配置并统一脱敏 repository 错误。"""

        try:
            return self._repository.get(provider_id)
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error

    def _assert_provider_snapshot_unchanged(
        self,
        provider_id: str,
        *,
        expected_record: ProviderConfigRecord | None,
        expected_secret: str | None,
    ) -> None:
        """在 state lock 内用 record 与 secret 做提交前 CAS 检查。"""

        current = self._provider_record(provider_id)
        if current != expected_record:
            raise _provider_state_conflict()
        current_secret = self._get_secret(current.secret_ref) if current is not None else None
        if current_secret != expected_secret:
            raise _provider_state_conflict()

    def _assert_model_snapshots_unchanged(
        self,
        provider_id: str,
        *,
        expected_models: Mapping[str, ProviderModelRecord | None],
    ) -> None:
        """在 state lock 内确认 probe 使用的 model runtime rows 仍未变化。"""

        try:
            for model_id, expected in expected_models.items():
                if self._repository.get_model(provider_id, model_id) != expected:
                    raise _provider_state_conflict()
        except ProviderConfigRepositoryError as error:
            raise _repository_public_error() from error

    def _assert_manual_inventory_snapshot_unchanged(
        self,
        provider_id: str,
        *,
        expected_models: Mapping[str, ProviderModelRecord],
    ) -> None:
        """确认 probe 期间没有新增、删除或修改任何手动 model row。"""

        current_models = {
            model.model_id: model
            for model in self._stored_models_locked(provider_id)
            if model.source == "manual"
        }
        if current_models != expected_models:
            raise _provider_state_conflict()

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
        models = self._overlay_catalog_models(definition.id, models)
        model_statuses = tuple(self._model_status_from_record(model) for model in models)
        enabled_ids = tuple(model.id for model in model_statuses if model.enabled)
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
            active=record.active if record is not None else False,
            probe_status=record.probe_status if record is not None else "not_configured",
            probe_error_code=record.probe_error_code if record is not None else None,
            latency_ms=record.latency_ms if record is not None else None,
            model_id=record.default_model_id if record is not None else None,
            default_model_id=record.default_model_id if record is not None else None,
            enabled_model_ids=enabled_ids,
            models=model_statuses,
            supports_model_sync=definition.models_path is not None,
            last_probed_at=record.last_probed_at if record is not None else None,
            last_synced_at=record.last_synced_at if record is not None else None,
        )

    def _overlay_catalog_models(
        self,
        provider_id: str,
        models: Sequence[ProviderModelRecord],
    ) -> tuple[ProviderModelRecord, ...]:
        """只读叠加 bundled baseline, 让 catalog 升级不依赖重新保存 provider。"""

        catalog_models = self._catalog_models.get(provider_id, {})
        by_upstream = {
            catalog_model.upstream_model_id: (model_id, catalog_model)
            for model_id, catalog_model in catalog_models.items()
        }
        overlaid: list[ProviderModelRecord] = []
        seen_catalog_ids: set[str] = set()
        for model in models:
            catalog_id = model.model_id if model.model_id in catalog_models else None
            catalog_model = catalog_models.get(model.model_id)
            if catalog_model is None:
                matched = by_upstream.get(model.upstream_model_id)
                if matched is not None:
                    catalog_id, catalog_model = matched
            if catalog_model is None:
                overlaid.append(model)
                continue
            seen_catalog_ids.add(catalog_id or model.model_id)
            overlaid.append(
                replace(
                    model,
                    display_name=catalog_model.display_name,
                    source="catalog",
                    available=True,
                )
            )

        for model_id, catalog_model in catalog_models.items():
            if model_id in seen_catalog_ids:
                continue
            overlaid.append(
                ProviderModelRecord(
                    provider_id=provider_id,
                    model_id=model_id,
                    upstream_model_id=catalog_model.upstream_model_id,
                    display_name=catalog_model.display_name,
                    source="catalog",
                    enabled=False,
                    available=True,
                    probe_status="not_tested",
                    probe_error_code=None,
                    latency_ms=None,
                    last_seen_at=None,
                    last_probed_at=None,
                    reasoning_policy_override=None,
                    per_job_concurrency_override=None,
                    global_concurrency_override=None,
                )
            )
        return tuple(overlaid)

    def _catalog_model_for_record(
        self,
        model: ProviderModelRecord,
    ) -> _CatalogModel | None:
        """按稳定 model id 查 catalog, 并识别相同 upstream 的旧远端记录。"""

        catalog_models = self._catalog_models.get(model.provider_id, {})
        catalog_model = catalog_models.get(model.model_id)
        if catalog_model is not None:
            return catalog_model
        return next(
            (
                candidate
                for candidate in catalog_models.values()
                if candidate.upstream_model_id == model.upstream_model_id
            ),
            None,
        )

    def _model_status_from_record(self, model: ProviderModelRecord) -> ProviderModelStatus:
        """把持久化 model 与 catalog defaults 合成 effective runtime settings。"""

        catalog_model = self._catalog_model_for_record(model)
        supported_policies = (
            catalog_model.supported_reasoning_policies if catalog_model is not None else ()
        )
        visible_reasoning_override = (
            model.reasoning_policy_override
            if model.reasoning_policy_override in supported_policies
            else None
        )
        default_reasoning = (
            catalog_model.default_reasoning_policy if catalog_model is not None else None
        )
        return ProviderModelStatus(
            id=model.model_id,
            display_name=model.display_name,
            source=model.source,
            enabled=model.enabled,
            available=model.available,
            probe_status=model.probe_status,
            probe_error_code=model.probe_error_code,
            latency_ms=model.latency_ms,
            last_probed_at=model.last_probed_at,
            reasoning_policy=visible_reasoning_override or default_reasoning,
            reasoning_policy_override=visible_reasoning_override,
            supported_reasoning_policies=supported_policies,
            per_job_concurrency=(model.per_job_concurrency_override or DEFAULT_PER_JOB_CONCURRENCY),
            per_job_concurrency_override=model.per_job_concurrency_override,
            global_concurrency=(model.global_concurrency_override or DEFAULT_GLOBAL_CONCURRENCY),
            global_concurrency_override=model.global_concurrency_override,
        )

    def _model_status_from_inventory(
        self,
        provider_id: str,
        model: _InventoryItem,
    ) -> ProviderModelStatus:
        """为只读 discovery item 补齐 catalog defaults 与应用并发默认值。"""

        catalog_model = self._catalog_models.get(provider_id, {}).get(model.model_id)
        return ProviderModelStatus(
            id=model.model_id,
            display_name=model.display_name,
            source=model.source,
            enabled=False,
            available=True,
            probe_status="not_tested",
            probe_error_code=None,
            latency_ms=None,
            last_probed_at=None,
            reasoning_policy=(
                catalog_model.default_reasoning_policy if catalog_model is not None else None
            ),
            reasoning_policy_override=None,
            supported_reasoning_policies=(
                catalog_model.supported_reasoning_policies if catalog_model is not None else ()
            ),
            per_job_concurrency=DEFAULT_PER_JOB_CONCURRENCY,
            per_job_concurrency_override=None,
            global_concurrency=DEFAULT_GLOBAL_CONCURRENCY,
            global_concurrency_override=None,
        )

    def _sync_result(
        self,
        provider_id: str,
        merged: ProviderInventoryMergeResult,
    ) -> ProviderInventorySyncResult:
        """把 repository merge snapshot 投影为保留 unavailable 项的 API 结果。"""

        return ProviderInventorySyncResult(
            provider_id=provider_id,
            added=merged.added,
            restored=merged.restored,
            unavailable=merged.unavailable,
            unchanged=merged.unchanged,
            last_synced_at=merged.synced_at,
            models=tuple(self._model_status_from_record(model) for model in merged.models),
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


def _is_runnable_model(model: ProviderModelRecord) -> bool:
    """判断持久化 model 是否能被 active provider 立即用于翻译。"""

    return model.enabled and model.available and model.probe_status == "succeeded"


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


def _normalize_manual_model_id(value: str) -> str:
    """规范化手动输入的 upstream model id, 并拒绝空白或控制字符。"""

    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > MAX_MODEL_ID_LENGTH
        or any(character.isspace() or ord(character) < 32 for character in normalized)
        or "\x7f" in normalized
    ):
        raise ProviderConfigError(
            ProviderPublicErrorCode.MODEL,
            "The manual model id is invalid.",
            status_code=400,
        )
    return normalized


def _normalize_manual_model_display_name(
    value: str | None,
    *,
    fallback: str,
) -> str:
    """清理可选显示名; 空值沿用 model id, 避免库存出现空标签。"""

    normalized = value.strip() if value is not None else ""
    if not normalized:
        return fallback
    if len(normalized) > MAX_MODEL_DISPLAY_NAME_LENGTH or any(
        ord(character) < 32 or character == "\x7f" for character in normalized
    ):
        raise ProviderConfigError(
            ProviderPublicErrorCode.MODEL,
            "The manual model display name is invalid.",
            status_code=400,
        )
    return normalized


def _normalize_reasoning_override(value: object) -> str | None:
    """规范化 reasoning override, 并拒绝 catalog contract 之外的值。"""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ProviderConfigError(
            ProviderPublicErrorCode.MODEL,
            "The reasoning policy is invalid.",
            status_code=400,
        )
    normalized = value.strip().lower()
    if normalized not in REASONING_POLICIES:
        raise ProviderConfigError(
            ProviderPublicErrorCode.MODEL,
            "The reasoning policy is invalid.",
            status_code=400,
        )
    return normalized


def _normalize_concurrency_override(value: object) -> int | None:
    """把 model concurrency override 限制在 runtime registry 支持范围。"""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderConfigError(
            ProviderPublicErrorCode.MODEL,
            "The model concurrency limit is invalid.",
            status_code=400,
        )
    if not 1 <= value <= MAX_MODEL_CONCURRENCY:
        raise ProviderConfigError(
            ProviderPublicErrorCode.MODEL,
            f"The model concurrency limit must be between 1 and {MAX_MODEL_CONCURRENCY}.",
            status_code=400,
        )
    return value


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


def _provider_state_conflict() -> ProviderConfigError:
    """拒绝把过期 probe 或 sync 结果提交到已经变化的 provider。"""

    return ProviderConfigError(
        ProviderPublicErrorCode.CONFLICT,
        "The provider configuration changed while the operation was running.",
        status_code=409,
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
