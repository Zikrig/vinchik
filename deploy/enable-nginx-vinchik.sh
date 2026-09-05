#!/bin/bash
# Вставляет include сниппета vinchik в TLS-vhost nginx на этом хосте.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/deploy/nginx-vinchik.snippet.conf"
SNIPPET=/etc/nginx/snippets/vinchik.conf
INCLUDE_LINE='    include /etc/nginx/snippets/vinchik.conf;'

if [[ ! -f "$SRC" ]]; then
    echo "нет $SRC" >&2
    exit 1
fi

install -d /etc/nginx/snippets
cp "$SRC" "$SNIPPET"

python3 - "$SNIPPET" "$INCLUDE_LINE" << 'PY'
import sys
from pathlib import Path

snippet, include_line = sys.argv[1], sys.argv[2]
search_dirs = [
    Path("/etc/nginx/sites-enabled"),
    Path("/etc/nginx/conf.d"),
    Path("/etc/nginx/sites-available"),
]
files = []
for d in search_dirs:
    if d.is_dir():
        files.extend(p for p in d.iterdir() if p.is_file() or p.is_symlink())

def score(path: Path) -> tuple:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return (-1, str(path))
    s = 0
    if "nginx" in path.name and "hostkey" in text:
        s += 50
    if "hostkey.in" in text:
        s += 40
    if "Let's Encrypt certificate successfully installed" in text:
        s += 30
    if "listen" in text and "443" in text:
        s += 10
    if "server_name" in text:
        s += 5
    return (s, str(path))

ranked = sorted((score(p) for p in files), reverse=True)
if not ranked or ranked[0][0] < 0:
    sys.exit("не найден nginx vhost")
target = Path(ranked[0][1])
text = target.read_text(encoding="utf-8", errors="ignore")
changed = False
if "snippets/vinchik.conf" not in text:
    idx = text.rfind("}")
    if idx < 0:
        sys.exit(f"нет закрывающей скобки в {target}")
    text = text[:idx] + include_line + "\n" + text[idx:]
    changed = True
    print(f"добавлен include в {target}")
else:
    print(f"include уже есть: {target}")

# Hostkey: server-level `return 200` runs before locations and shadows /vinchik.
out = []
i = 0
lines = text.splitlines(True)
while i < len(lines):
    line = lines[i]
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]
    is_le_return = stripped.startswith("return 200") and "Let's Encrypt" in stripped.replace("\\'", "'")
    prev = out[-1] if out else ""
    already_in_location = "location" in prev
    if is_le_return and not already_in_location:
        out.append(f"{indent}location = / {{\n")
        out.append(f"{indent}    default_type text/plain;\n")
        out.append(f"{indent}    {stripped}")
        if not stripped.endswith("\n"):
            out.append("\n")
        out.append(f"{indent}}}\n")
        changed = True
        print("return 200 Let's Encrypt убран с уровня server → location = /")
        i += 1
        continue
    out.append(line)
    i += 1
text = "".join(out)
if changed:
    target.write_text(text, encoding="utf-8")
PY

nginx -t
systemctl reload nginx

echo "--- проверка ---"
ss -tlnp | grep -E ':8180|:8181' || true
curl -sI http://127.0.0.1:8180/login | head -15 || true
curl -sI -o /dev/null -w "https /vinchik -> %{http_code} redirect=%{redirect_url}\n" https://127.0.0.1/vinchik -k --resolve "nginx125717.hostkey.in:443:127.0.0.1" || true
curl -sI https://127.0.0.1/vinchik/ -k --resolve "nginx125717.hostkey.in:443:127.0.0.1" | head -20 || true
