import re
import sys
import pathlib

root = pathlib.Path(__file__).parent
sys.path.insert(0, str(root))
from locales import ru

keys = set(ru.TEXTS)
used = {}
for p in root.rglob("*.py"):
    if "locales" in p.parts or p.name.startswith("_tmp"):
        continue
    src = p.read_text(encoding="utf-8")
    for m in re.finditer(r"""\bt\(\s*["']([\w]+)["']""", src):
        used.setdefault(m.group(1), set()).add(p.name)

missing = {k: sorted(v) for k, v in used.items() if k not in keys}
print("missing keys:", missing)

print("dynamic t-calls:")
for p in root.rglob("*.py"):
    if "locales" in p.parts or p.name.startswith("_tmp"):
        continue
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if re.search(r"""\bt\(\s*(f["']|key)""", line):
            print(p.name, i, line.strip())
