"""更新脚本：拷代码、保数据、先备份。

跑法：python tests/test_update.py
"""

import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import update as U


def _old_project():
    """造一个"装好并用了一阵"的旧版本。"""
    root = Path(tempfile.mkdtemp()) / "Somnia·眠"
    (root / "somnia").mkdir(parents=True)
    (root / "somnia" / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (root / "somnia" / "app.py").write_text("旧代码\n", encoding="utf-8")
    (root / "somnia.toml").write_text('name = "旧"\n', encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "somnia.db").write_bytes(b"OLD-DB")
    (root / "data" / "settings.json").write_text('{"api_key": "sk-old"}', encoding="utf-8")
    (root / "memory" / "archive").mkdir(parents=True)
    (root / "memory" / "archive" / "2026-10-20.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "persona").mkdir()
    (root / "persona" / "persona.md").write_text("我的人设\n", encoding="utf-8")
    return root


def _new_zip(with_extra_dir=True):
    """造一个新版本 zip（多套一层目录，像真打包那样）。"""
    tmp = Path(tempfile.mkdtemp())
    name = "Somnia·眠-0.2.0" if with_extra_dir else "."
    new = tmp / name
    (new / "somnia").mkdir(parents=True)
    (new / "somnia" / "__init__.py").write_text('__version__ = "0.2.0"\n', encoding="utf-8")
    (new / "somnia" / "app.py").write_text("新代码\n", encoding="utf-8")
    (new / "somnia.toml").write_text('name = "新"\n', encoding="utf-8")
    (new / "README.md").write_text("新说明\n", encoding="utf-8")
    (new / "persona").mkdir()
    (new / "persona" / "persona.md").write_text("出厂人设\n", encoding="utf-8")
    (new / "data").mkdir()
    (new / "data" / "settings.json").write_text('{"api_key": "sk-factory"}', encoding="utf-8")

    zpath = tmp / "new.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for p in new.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(tmp))
    return zpath


def test_pick_root_finds_nested_folder():
    z = _new_zip()
    with zipfile.ZipFile(z) as zf:
        d = Path(tempfile.mkdtemp())
        zf.extractall(d)
    root = U.pick_root(d)
    assert U.looks_like_project(root)


def test_update_replaces_code_keeps_data():
    old = _old_project()
    rc = U.main([str(_new_zip())], dst=old, assume_yes=True)
    assert rc == 0

    # 代码更新了
    assert "新代码" in (old / "somnia" / "app.py").read_text(encoding="utf-8")
    assert (old / "README.md").exists()
    assert U.read_version(old) == "0.2.0"
    # 你的东西没动
    assert (old / "data" / "somnia.db").read_bytes() == b"OLD-DB"
    assert "sk-old" in (old / "data" / "settings.json").read_text(encoding="utf-8")
    assert (old / "memory" / "archive" / "2026-10-20.jsonl").exists()
    assert "我的人设" in (old / "persona" / "persona.md").read_text(encoding="utf-8")


def test_update_backs_up_before_touching():
    old = _old_project()
    U.main([str(_new_zip())], dst=old, assume_yes=True)
    baks = list((old / "data" / "backup").glob("pre_update_*"))
    assert baks, "应当留下备份"
    names = {p.name for p in baks[0].iterdir()}
    assert "somnia.db" in names and "settings.json" in names


def test_bad_source_changes_nothing():
    old = _old_project()
    bad = Path(tempfile.mkdtemp())
    (bad / "随便").mkdir()
    before = sorted(p.name for p in old.iterdir())
    try:
        U.main([str(bad)], dst=old, assume_yes=True)
    except SystemExit:
        pass
    after = sorted(p.name for p in old.iterdir())
    assert before == after
    assert "旧代码" in (old / "somnia" / "app.py").read_text(encoding="utf-8")


def test_cancel_does_nothing():
    old = _old_project()
    rc = U.main([str(_new_zip())], dst=old, assume_yes=False)
    assert rc == 1                      # 输入流结束 → 当作取消
    assert "旧代码" in (old / "somnia" / "app.py").read_text(encoding="utf-8")


def test_parse_version_and_newer():
    assert U.parse_version("0.2.1") == (0, 2, 1)
    assert U.parse_version("v1.0") == (1, 0)
    assert U.is_newer("0.2.1", "0.2.0")
    assert not U.is_newer("0.2.0", "0.2.0")
    assert not U.is_newer("0.1.9", "0.2.0")
    assert U.is_newer("1.0", "0.9.9")


def test_check_reads_manifest():
    z = _new_zip()
    manifest = Path(tempfile.mkdtemp()) / "version.json"
    manifest.write_text(
        '{"version": "9.9.9", "zip": "%s", "notes": "测试版"}' % z.as_posix(),
        encoding="utf-8")
    info = U.check(manifest.as_uri())
    assert info["has_new"] and info["latest"] == "9.9.9"
    assert info["notes"] == "测试版"


def test_check_without_url_is_quiet():
    info = U.check("")
    assert info["has_new"] is False
    assert info["error"]


def test_check_bad_manifest_does_not_raise():
    bad = Path(tempfile.mkdtemp()) / "v.json"
    bad.write_text("不是 json", encoding="utf-8")
    info = U.check(bad.as_uri())
    assert info["has_new"] is False and info["error"]


def test_update_from_url_into_temp_project():
    """整条链路：manifest → zip → 覆盖，但只作用在临时目录。"""
    old = _old_project()
    z = _new_zip()
    manifest = Path(tempfile.mkdtemp()) / "version.json"
    manifest.write_text('{"version": "0.2.0", "zip": "%s"}' % z.as_uri(),
                        encoding="utf-8")
    info = U.check(manifest.as_uri(), root=old)
    assert info["has_new"]

    r = U.update_from_url(info["zip"], dst=old)
    assert r["new"] == "0.2.0"
    assert (old / "data" / "somnia.db").read_bytes() == b"OLD-DB"


def test_stickers_merge_instead_of_skip():
    """表情包：新版加的图要能收到，用户自己加的不覆盖。"""
    old = _old_project()
    (old / "stickers").mkdir()
    (old / "stickers" / "我的.gif").write_bytes(b"GIF89a")
    (old / "stickers" / "notes.json").write_text('{"我的.gif": "我的"}', encoding="utf-8")

    new = Path(tempfile.mkdtemp()) / "new"
    (new / "somnia").mkdir(parents=True)
    (new / "somnia" / "__init__.py").write_text('__version__ = "0.2.0"\n', encoding="utf-8")
    (new / "somnia.toml").write_text('name = "新"\n', encoding="utf-8")
    (new / "stickers").mkdir()
    (new / "stickers" / "新图.gif").write_bytes(b"GIF89a")
    (new / "stickers" / "notes.json").write_text('{"新图.gif": "新"}', encoding="utf-8")

    U.apply_update(new, old)
    names = {p.name for p in (old / "stickers").iterdir()}
    assert "新图.gif" in names, "新版的表情包应该进来"
    assert "我的.gif" in names, "用户自己的图不能被删"
    notes = (old / "stickers" / "notes.json").read_text(encoding="utf-8")
    assert "新图" in notes, "随包备注应该更新"


def test_split_urls():
    got = U.split_urls(" https://a/x.json , https://b/y.json  https://a/x.json ")
    assert got == ["https://a/x.json", "https://b/y.json"]


def test_check_falls_back_to_second_url():
    z = _new_zip()
    good = Path(tempfile.mkdtemp()) / "ok.json"
    good.write_text('{"version": "9.9.9", "zip": "%s"}' % z.as_uri(), encoding="utf-8")
    bad = (Path(tempfile.mkdtemp()) / "nope.json").as_uri()
    info = U.check(f"{bad} {good.as_uri()}")
    assert info["has_new"] and info["latest"] == "9.9.9"


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
