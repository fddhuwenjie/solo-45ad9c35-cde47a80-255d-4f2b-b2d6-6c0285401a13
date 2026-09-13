#!/usr/bin/env python3
"""重新生成冻结旧库二进制快照（tests/fixtures/*.db）。

用法：.venv/bin/python tests/fixtures/build_fixtures.py

快照是升级前的 user_version=0 历史库，测试既可以直接复制这些冻结文件，
也可以调用 legacy_fixtures.build_fixture 在临时目录即时重建。修改夹具
DDL/数据后必须重跑本脚本，保证二进制快照与定义一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixtures.legacy_fixtures import FIXTURE_NAMES, build_fixture  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent


def main() -> None:
    for name in FIXTURE_NAMES:
        out = FIXTURE_DIR / f"{name}.db"
        if out.exists():
            out.unlink()
        build_fixture(str(out), name)
        print(f"built {out.relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
