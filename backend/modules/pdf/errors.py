"""定义 PDF pipeline 可安全暴露而不携带正文的稳定错误。"""

from modules.translation.contracts import DocumentPipelineError


class PdfPipelineError(DocumentPipelineError):
    """表示可由 UI 映射成人话的 PDF 输入或 runtime 错误。"""
