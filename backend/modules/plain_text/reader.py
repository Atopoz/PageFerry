"""读取 TXT/Markdown 的编码、换行风格与末尾换行状态。"""

import codecs
from pathlib import Path

from modules.plain_text.models import TextReadResult


class TextFileReader:
    """读取 v0.1 支持的文本编码, 并识别原始换行风格。"""

    encodings = ("utf-8", "gb18030")

    def read(self, path: Path) -> TextReadResult:
        """按 BOM、UTF-8、GB18030 顺序解码一个文本文件。"""

        payload = path.read_bytes()
        if payload.startswith(codecs.BOM_UTF8):
            text = payload.decode("utf-8-sig")
            return TextReadResult(text, "utf-8-sig", self._line_ending(text))

        for encoding in self.encodings:
            try:
                text = payload.decode(encoding)
            except UnicodeDecodeError:
                continue
            return TextReadResult(text, encoding, self._line_ending(text))
        raise ValueError("unsupported_text_encoding")

    @staticmethod
    def _line_ending(text: str) -> str:
        """返回文档实际使用的第一类换行符。"""

        if "\r\n" in text:
            return "\r\n"
        if "\r" in text:
            return "\r"
        return "\n"
