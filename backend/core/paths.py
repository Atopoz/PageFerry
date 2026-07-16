from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path


@dataclass(frozen=True, slots=True)
class AppPaths:
    """All writable paths owned by one PageFerry installation."""

    root: Path
    database: Path
    workspace: Path
    outputs: Path
    models: Path
    cache: Path
    logs: Path

    def ensure(self) -> None:
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
