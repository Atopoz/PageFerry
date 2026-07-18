#!/usr/bin/env python3
# ruff: noqa: RUF001, RUF002, RUF003 -- 中文说明保留自然标点。
"""清理 Tauri DMG 的卷图标，并在签名之前保留 Finder 安装布局。"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import NoReturn


def parse_args() -> argparse.Namespace:
    """解析显式 DMG 路径和可选签名身份。"""
    parser = argparse.ArgumentParser(
        description="移除 Tauri DMG 根目录的卷图标，并重新生成可校验的只读镜像。"
    )
    parser.add_argument("--dmg", required=True, help="需要原地更新的 .dmg 文件")
    parser.add_argument(
        "--sign-identity",
        help="后处理完成后用于重新签名 DMG 的 identity；本地 smoke 可传 '-'",
    )
    return parser.parse_args()


def fail(message: str) -> NoReturn:
    """用清楚的错误信息终止脚本。"""
    raise SystemExit(message)


def require_tool(name: str) -> str:
    """返回外部工具路径，缺失时立即失败。"""
    resolved = shutil.which(name)
    if resolved is None:
        fail(f"缺少必需工具：{name}")
    return resolved


def resolve_dmg(raw_path: str) -> Path:
    """把用户输入收窄为一个真实存在的 DMG 文件。"""
    dmg_path = Path(raw_path).expanduser().resolve()
    if not dmg_path.is_file():
        fail(f"DMG 不存在：{dmg_path}")
    if dmg_path.suffix.lower() != ".dmg":
        fail(f"目标不是 .dmg 文件：{dmg_path}")
    return dmg_path


def run(command: list[str], *, capture_stdout: bool = False) -> subprocess.CompletedProcess[bytes]:
    """执行一个打包命令，并让失败直接中止当前后处理。"""
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE if capture_stdout else None,
    )


def attached_device(attach_plist: bytes, mount_dir: Path) -> str:
    """从 hdiutil 的 plist 输出中找出临时挂载分区。"""
    payload = plistlib.loads(attach_plist)
    expected_mount = mount_dir.resolve()
    for entity in payload.get("system-entities", []):
        raw_mount = entity.get("mount-point")
        raw_device = entity.get("dev-entry")
        if raw_mount and raw_device and Path(raw_mount).resolve() == expected_mount:
            return str(raw_device)
    fail(f"hdiutil 未返回临时挂载点：{mount_dir}")


def detach_image(hdiutil: str, target: str) -> None:
    """正常卸载临时镜像，失败时只对这个临时目标执行强制卸载。"""
    try:
        run([hdiutil, "detach", target])
    except subprocess.CalledProcessError:
        run([hdiutil, "detach", "-force", target])


def clean_mounted_image(setfile: str, mount_dir: Path) -> None:
    """清除卷 custom icon 标记和对应文件，同时保护 Finder 布局。"""
    finder_layout = mount_dir / ".DS_Store"
    if not finder_layout.is_file():
        fail(f"DMG 缺少 Finder 布局：{finder_layout}")

    # Tauri 会无条件写入卷图标。这里只处理这个确定文件，不触碰 Finder 布局。
    (mount_dir / ".VolumeIcon.icns").unlink(missing_ok=True)
    run([setfile, "-a", "c", str(mount_dir)])

    if (mount_dir / ".VolumeIcon.icns").exists():
        fail("卷图标文件清理失败")


def finalize_dmg(dmg_path: Path, sign_identity: str | None) -> None:
    """在同目录临时空间中重建 DMG，全部校验通过后再原子替换。"""
    hdiutil = require_tool("hdiutil")
    setfile = require_tool("SetFile")
    codesign = require_tool("codesign") if sign_identity else None
    original_mode = dmg_path.stat().st_mode & 0o777

    with tempfile.TemporaryDirectory(prefix=".pageferry-dmg-", dir=dmg_path.parent) as raw_temp:
        temp_dir = Path(raw_temp)
        writable_dmg = temp_dir / "writable.dmg"
        mount_dir = temp_dir / "mount"
        final_dmg = temp_dir / "final.dmg"
        mount_dir.mkdir()

        run(
            [
                hdiutil,
                "convert",
                str(dmg_path),
                "-quiet",
                "-format",
                "UDRW",
                "-o",
                str(writable_dmg),
            ]
        )

        device: str | None = None
        attached = False
        try:
            attach_result = run(
                [
                    hdiutil,
                    "attach",
                    str(writable_dmg),
                    "-plist",
                    "-readwrite",
                    "-nobrowse",
                    "-noautoopen",
                    "-mountpoint",
                    str(mount_dir),
                ],
                capture_stdout=True,
            )
            attached = True
            device = attached_device(attach_result.stdout, mount_dir)
            clean_mounted_image(setfile, mount_dir)
            run(["sync"])
        finally:
            if device is not None:
                detach_image(hdiutil, device)
            elif attached or mount_dir.is_mount():
                # plist 无法解析时仍使用这次显式 mountpoint 回收，不能遗留可写镜像。
                detach_image(hdiutil, str(mount_dir))

        run(
            [
                hdiutil,
                "convert",
                str(writable_dmg),
                "-quiet",
                "-format",
                "UDZO",
                "-imagekey",
                "zlib-level=9",
                "-o",
                str(final_dmg),
            ]
        )
        os.chmod(final_dmg, original_mode)

        if codesign is not None and sign_identity is not None:
            run([codesign, "--force", "--sign", sign_identity, str(final_dmg)])
            run([codesign, "--verify", "--verbose=2", str(final_dmg)])

        run([hdiutil, "verify", str(final_dmg)])
        os.replace(final_dmg, dmg_path)


def main() -> None:
    """执行 DMG 清理并打印最终产物位置。"""
    args = parse_args()
    dmg_path = resolve_dmg(args.dmg)
    finalize_dmg(dmg_path, args.sign_identity)
    print(f"DMG 已完成安装界面清理：{dmg_path}")


if __name__ == "__main__":
    main()
