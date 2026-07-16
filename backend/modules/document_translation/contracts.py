from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

DocumentKind = Literal["docx", "pptx", "pdf"]


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    source_path: Path
    output_dir: Path
    source_language: str | None
    target_language: str
    provider_id: str
    model_id: str


@dataclass(frozen=True, slots=True)
class TranslationResult:
    output_path: Path
    document_kind: DocumentKind


class DocumentPipeline(Protocol):
    """Small boundary shared by the format-specific pipeline adapters."""

    document_kind: DocumentKind

    def translate(self, request: TranslationRequest) -> TranslationResult: ...
