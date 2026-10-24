"""版本号三处必须一致：代码、pyproject、version.json。

跑法：python tests/test_version.py
"""

import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import somnia
from somnia import updater

ROOT = Path(__file__).resolve().parent.parent


def test_code_version_matches_pyproject():
    with open(ROOT / "pyproject.toml", "rb") as f:
        project = tomllib.load(f)["project"]["version"]
    assert somnia.__version__ == project, (
        f"__init__.py 是 {somnia.__version__}，pyproject 是 {project}")


def test_version_json_matches_and_points_at_right_tag():
    data = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
    assert data["version"] == somnia.__version__
    assert f"/v{data['version']}.zip" in data["zip"], "zip 地址里的 tag 要和版本号一致"
    assert data.get("notes")


def test_updater_reads_this_repo():
    info = updater.check((ROOT / "version.json").as_uri(), root=ROOT)
    assert info["latest"] == somnia.__version__
    assert info["has_new"] is False        # 自己跟自己比，不该说有新版


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
