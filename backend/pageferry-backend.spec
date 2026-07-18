# -*- mode: python ; coding: utf-8 -*-
"""冻结 PageFerry sidecar，并显式带上源码外的数据和动态 import。"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules


BACKEND_DIR = Path(SPECPATH)
SIGNING_IDENTITY = os.environ.get("APPLE_SIGNING_IDENTITY")

# 这些文件通过 __file__ 在 runtime 定位，不能依赖开发仓库仍然存在。
DATAS = [
    (str(BACKEND_DIR / "db" / "migrations"), "db/migrations"),
    (str(BACKEND_DIR / "resources" / "model_catalog"), "resources/model_catalog"),
    (str(BACKEND_DIR / "resources" / "pdf_assets"), "resources/pdf_assets"),
    (str(BACKEND_DIR / "vendor" / "pdfminerex" / "cmap"), "vendor/pdfminerex/cmap"),
    (str(BACKEND_DIR / "vendor" / "pdfminerex" / "LICENSE"), "vendor/pdfminerex"),
    (
        str(BACKEND_DIR / "vendor" / "pdfminerex" / "LICENSE.Apache-2.0"),
        "vendor/pdfminerex",
    ),
    (
        str(BACKEND_DIR / "vendor" / "pdfminerex" / "LICENSE.pyHanko"),
        "vendor/pdfminerex",
    ),
    (str(BACKEND_DIR / "vendor" / "pdfminerex" / "SOURCE.md"), "vendor/pdfminerex"),
    (
        str(BACKEND_DIR / "vendor" / "pdfminerex" / "THIRD_PARTY_NOTICES.md"),
        "vendor/pdfminerex",
    ),
]

# pikepdf wheel 把 qpdf 等依赖放在私有 .dylibs 目录，Analysis 不应猜测它们是否被间接引用。
BINARIES = collect_dynamic_libs("pikepdf")

# catalog 通过 importlib.resources 字符串定位 package；其余三项也都在 runtime 动态 import。
HIDDEN_IMPORTS = ["keyring", "resources", "tiktoken_ext.openai_public"]
HIDDEN_IMPORTS += collect_submodules("fontTools.ttLib.tables")

analysis = Analysis(
    [str(BACKEND_DIR / "sidecar.py")],
    pathex=[str(BACKEND_DIR)],
    binaries=BINARIES,
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Frozen runtime 固定使用 asyncio + h11 + ws=none；开发期 reload 加速器不能拖进首启。
    excludes=["httptools", "uvloop", "watchfiles", "websockets"],
    noarchive=False,
    optimize=0,
)
python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="pageferry-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    codesign_identity=SIGNING_IDENTITY,
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="pageferry-backend",
)
