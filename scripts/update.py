"""命令行更新：把新版本覆盖到旧版本上。

用法（在项目根目录）：

    python scripts/update.py 新版本.zip
    python scripts/update.py D:\\下载\\Somnia·眠        # 也可以直接给解压好的目录

真正干活的逻辑在 `somnia/updater.py` —— 面板上那个「↓ 新版本」用的是同一套。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import updater as U

# 兼容老测试/老脚本的引用
read_version = U.current_version
looks_like_project = U.looks_like_project
pick_root = U.pick_root
plan = U.plan
backup_data = U.backup_data
apply_update = U.apply_update
parse_version = U.parse_version
is_newer = U.is_newer
check = U.check
split_urls = U.split_urls
current_version = U.current_version
extract = U.extract
download = U.download
update_from_zip = U.update_from_zip
update_from_url = U.update_from_url


def main(argv=None, dst=None, assume_yes=False) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        print("没给新版本。把新版 zip 拖到 update.bat 上，或者：")
        print("    python scripts/update.py 新版本.zip")
        return 2

    src_arg = Path(argv[0]).expanduser()
    dst = Path(dst) if dst else U.PROJECT_ROOT

    if not src_arg.exists():
        print(f"找不到：{src_arg}")
        return 2
    if not U.looks_like_project(dst):
        print(f"当前目录不像项目根目录：{dst}")
        return 2

    tmp = None
    try:
        if src_arg.is_file() and src_arg.suffix.lower() == ".zip":
            print(f"解压 {src_arg.name} …")
            tmp = U.extract(src_arg)
            base = Path(tmp.name)
        else:
            base = src_arg

        try:
            src = U.pick_root(base)
        except ValueError as e:
            print(e)
            return 2

        old, new = U.current_version(dst), U.current_version(src)
        items, skipped = U.plan(src, dst)
        n_files = sum(1 for item, _ in items if item.is_file())

        print(f"版本：{old} → {new}")
        print(f"要覆盖 {n_files} 个文件，跳过 {skipped} 个你自己的")
        print(f"目标：{dst}")

        if not assume_yes:
            try:
                ans = input("确认覆盖？(y/N) ").strip().lower()
            except EOFError:
                ans = ""
            if ans not in ("y", "yes"):
                print("已取消，什么都没改。")
                return 1

        bak = U.backup_data(dst)
        if any(bak.iterdir()):
            print(f"已备份 data 到 {bak}")

        stats = U.apply_update(src, dst)
        print(f"完成：覆盖 {stats['copied']} 个文件。")
        print("双击 start.bat 启动新版本；如果刚才是开着的，先 stop.bat。")
        return 0
    finally:
        if tmp is not None:
            tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
