"""集中解析 PageFerry 自有数据目录, 防止业务代码散落平台路径判断。"""

from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path


@dataclass(frozen=True, slots=True)
class AppPaths:
    """列出一个 PageFerry 安装实例拥有的所有可写路径。"""

    root: Path
    database: Path
    workspace: Path
    outputs: Path
    models: Path
    cache: Path
    logs: Path

    def ensure(self) -> None:
        """创建 root 与各用途子目录, 已存在时保持幂等。"""

        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (
            self.workspace,
            self.outputs,
            self.models,
            self.cache,
            self.logs,
        ):
            directory.mkdir(parents=True, exist_ok=True)


def resolve_app_paths(data_dir: Path | None = None) -> AppPaths:
    """优先使用显式测试目录, 否则解析当前平台的用户数据目录。"""

    root = (
        data_dir.expanduser().resolve()
        if data_dir is not None
        else Path(user_data_path("PageFerry", appauthor=False, roaming=False))
    )
    return AppPaths(
        root=root,
        database=root / "pageferry.sqlite3",
        workspace=root / "workspace",
        outputs=root / "outputs",
        models=root / "models",
        cache=root / "cache",
        logs=root / "logs",
    )
