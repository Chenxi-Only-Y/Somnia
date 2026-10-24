"""跑全部测试，并报一个准确的数：通过多少、失败多少。

    python tests/run_all.py

为什么需要它：一个个手动跑的时候，某个文件崩了只会看到最后一行，
总数会"看起来变少"（其实是有文件没跑完）。
"""

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Windows 控制台是 GBK，子进程输出里有乱码字符时会直接抛 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    files = sorted(HERE.glob("test_*.py"))
    total_pass = 0
    failed = []

    for f in files:
        proc = subprocess.run([sys.executable, str(f)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        out = (proc.stdout or "") + (proc.stderr or "")
        m = re.search(r"(\d+)\s+passed", out)
        n = int(m.group(1)) if m else 0
        if proc.returncode != 0 or not m:
            failed.append((f.name, n, proc.returncode, out.strip().splitlines()[-6:]))
            total_pass += n
            print(f"  FAIL  {f.name:26s} {n:3d} passed")
        else:
            total_pass += n
            print(f"  ok    {f.name:26s} {n:3d}")

    print()
    if failed:
        for name, _n, code, tail in failed:
            print(f"--- {name} (exit={code}) ---")
            for line in tail:
                print("   ", line)
        print(f"\n{total_pass} passed, {len(failed)} file(s) failed")
        return 1
    print(f"{total_pass} passed, 全部通过（{len(files)} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
