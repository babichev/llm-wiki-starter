#!/usr/bin/env python3
"""Check relative Markdown file links in the public starter source."""
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
errors = []
checked = 0
for path in sorted(ROOT.rglob('*.md')):
    relative = path.relative_to(ROOT)
    if any(part in {'.git', '__pycache__', 'node_modules', 'dist'} for part in relative.parts):
        continue
    if path.is_symlink():
        errors.append(f'{relative}: symlink is not allowed')
        continue
    checked += 1
    text = re.sub(r'```.*?```', '', path.read_text(encoding='utf-8'), flags=re.S)
    text = re.sub(r'`[^`\n]*`', '', text)
    for target in re.findall(r'!?\[[^\]]*\]\(([^)]+)\)', text):
        target = target.split(' "', 1)[0].strip('<>')
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        destination = (path.parent / unquote(parsed.path)).resolve()
        if ROOT not in destination.parents and destination != ROOT:
            errors.append(f'{relative}: link escapes starter: {target}')
        elif not destination.exists():
            errors.append(f'{relative}: missing link: {target}')
if errors:
    print('\n'.join(errors), file=sys.stderr)
    sys.exit(1)
print(f'{checked} Markdown files: relative file links passed (anchors and external URLs excluded).')
