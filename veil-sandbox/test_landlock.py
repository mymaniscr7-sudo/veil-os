"""Landlock in-process tests. Run: python3 test_landlock.py
Proves the verified path only: restrict_self in-process, workspace
read works, host files blocked. Exec-after-restrict is NOT tested
here — it is unreliable on this host, see landlock.py.
Exit 0 = all pass. Runs in a subprocess so the restriction doesn't
leak into the test runner.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

CHILD = (
    "import sys\n"
    f"sys.path.insert(0, {str(HERE)!r})\n"
    "from landlock import apply_landlock\n"
    f"ws = {str(HERE / 'test-workspace')!r}\n"
    "apply_landlock([(ws, 'rwx'), ('/usr', 'rx'), ('/tmp', 'rwx')])\n"
    "results = []\n"
    "try:\n"
    "    results.append(('workspace-read', 'hello' in open(ws + '/notes.md').read()))\n"
    "except Exception:\n"
    "    results.append(('workspace-read', False))\n"
    "for name, p in [('etc-passwd', '/etc/passwd'), ('memory', '/home/hatch/MEMORY.md')]:\n"
    "    try:\n"
    "        open(p).read()\n"
    "        results.append((name + '-blocked', False))\n"
    "    except PermissionError:\n"
    "        results.append((name + '-blocked', True))\n"
    "    except Exception:\n"
    "        results.append((name + '-blocked', False))\n"
    "for name, passed in results:\n"
    "    print(name + '=' + str(passed))\n"
    "sys.exit(0 if all(v for _, v in results) else 1)\n"
)

if __name__ == "__main__":
    (HERE / "test-workspace" / "notes.md").write_text("hello from workspace\n")
    r = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True)
    print(r.stdout, end="")
    if r.stderr:
        print("stderr:", r.stderr, end="")
    print("Landlock in-process tests:", "PASS" if r.returncode == 0 else "FAIL")
    raise SystemExit(r.returncode)
