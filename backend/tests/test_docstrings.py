"""把文件头与 class/function docstring 作为可执行的项目约束。"""

import ast
from pathlib import Path


def test_python_modules_classes_and_functions_have_docstrings() -> None:
    """扫描 backend 源码和 tests, 报出所有缺少直接说明的定义。"""

    backend_root = Path(__file__).parents[1]
    missing: list[str] = []
    for path in sorted(backend_root.rglob("*.py")):
        if ".venv" in path.parts:
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
