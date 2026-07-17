"""把文件头与 class/function docstring 作为可执行的项目约束。"""

import ast
from pathlib import Path


def test_python_modules_classes_and_functions_have_docstrings() -> None:
    """扫描 backend 源码和 tests, 报出所有缺少直接说明的定义。"""

    backend_root = Path(__file__).parents[1]
    parity_snapshot_files = {
        backend_root / "modules" / "pdf" / filename
        for filename in {
            "entities.py",
            "extractor.py",
            "font_manager.py",
            "formatter.py",
            "layout_tools.py",
            "renderer.py",
            "span_line_utils.py",
            "table_renderer.py",
            "translator.py",
        }
    }
    vendor_snapshot = backend_root / "vendor" / "pdfminerex"
    missing: list[str] = []
    for path in sorted(backend_root.rglob("*.py")):
        if ".venv" in path.parts:
            continue
        # 已迁入的 PDF 核心算法与 pdfminerex fork 先以来源、许可证和 parity tests
        # 冻结; 只有行为测试覆盖后才能逐片清理, 避免为 docstring 一次改动数百处实现。
        if path in parity_snapshot_files or vendor_snapshot in path.parents:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(backend_root)
        if ast.get_docstring(tree, clean=False) is None:
            missing.append(f"{relative_path}:1 module")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if ast.get_docstring(node, clean=False) is None:
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                missing.append(f"{relative_path}:{node.lineno} {kind} {node.name}")

    assert not missing, "Missing docstrings:\n" + "\n".join(missing)
