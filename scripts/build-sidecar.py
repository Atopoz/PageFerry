# ruff: noqa: RUF001, RUF002 -- 中文说明保留自然标点。
"""把当前平台的 Python backend 冻结为 Tauri resource 目录。"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
TAURI_BINARIES_DIR = PROJECT_ROOT / "tauri" / "binaries"
TAURI_BACKEND_DIR = TAURI_BINARIES_DIR / "pageferry-backend"


def _host_target_triple() -> str:
    """读取当前 Rust toolchain 的 host triple，避免复制成 Tauri 无法识别的名字。"""

    completed = subprocess.run(
        ["rustc", "--print", "host-tuple"],
        check=True,
        capture_output=True,
        text=True,
    )
    target_triple = completed.stdout.strip()
    if not target_triple or any(character.isspace() for character in target_triple):
        raise RuntimeError("rustc 返回了无效的 host target triple")
    return target_triple


def _backend_executable_name() -> str:
    """按当前平台返回 onedir 中的 backend 可执行文件名。"""

    return "pageferry-backend.exe" if sys.platform == "win32" else "pageferry-backend"


def _remove_generated_path(path: Path) -> None:
    """只清理 tauri/binaries 下已知的生成路径。"""

    if path.parent != TAURI_BINARIES_DIR:
        raise RuntimeError(f"拒绝清理 tauri/binaries 之外的路径：{path}")
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _directory_size(path: Path) -> int:
    """统计 onedir 的实际文件字节数，供构建日志核对。"""

    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def build_sidecar() -> Path:
    """运行锁定环境中的 PyInstaller，并用完整 onedir 替换 Tauri resource。"""

    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "pageferry-backend.spec",
        ],
        cwd=BACKEND_DIR,
        check=True,
    )

    source = BACKEND_DIR / "dist" / "pageferry-backend"
    source_executable = source / _backend_executable_name()
    if not source_executable.is_file():
        raise RuntimeError(f"PyInstaller 没有生成预期 onedir：{source}")

    target_triple = _host_target_triple()
    suffix = ".exe" if sys.platform == "win32" else ""
    legacy_onefile = TAURI_BINARIES_DIR / f"pageferry-backend-{target_triple}{suffix}"
    temporary = TAURI_BACKEND_DIR.with_name(f"{TAURI_BACKEND_DIR.name}.tmp")
    previous = TAURI_BACKEND_DIR.with_name(f"{TAURI_BACKEND_DIR.name}.previous")
    TAURI_BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    _remove_generated_path(temporary)
    _remove_generated_path(previous)
    shutil.copytree(source, temporary, symlinks=True)
    temporary_executable = temporary / _backend_executable_name()
    temporary_executable.chmod(
        temporary_executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )

    try:
        if TAURI_BACKEND_DIR.exists():
            TAURI_BACKEND_DIR.replace(previous)
        temporary.replace(TAURI_BACKEND_DIR)
    except Exception:
        if previous.exists() and not TAURI_BACKEND_DIR.exists():
            previous.replace(TAURI_BACKEND_DIR)
        raise

    _remove_generated_path(previous)
    legacy_onefile.unlink(missing_ok=True)
    destination_executable = TAURI_BACKEND_DIR / _backend_executable_name()
    print(
        f"sidecar: {TAURI_BACKEND_DIR} "
        f"({_directory_size(TAURI_BACKEND_DIR)} bytes, {target_triple})"
    )
    return destination_executable


def main() -> int:
    """构建当前 host 可执行文件；跨平台产物必须由对应平台分别生成。"""

    build_sidecar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
