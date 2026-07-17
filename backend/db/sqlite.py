"""初始化 PageFerry 的 SQLite 连接参数并执行单向 migration。"""

import sqlite3
from pathlib import Path

from db.migrations import apply_migrations


def initialize_database(database_path: Path) -> None:
    """创建数据库目录, 设置运行参数并升级到最新 schema。"""

    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        apply_migrations(connection)
