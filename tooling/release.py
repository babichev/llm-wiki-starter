#!/usr/bin/env python3
"""Build only the public starter files; never recursively package a user vault."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {'.gitignore', 'README.md', 'LICENSE', 'VERSION', 'CONTRIBUTING.md', 'CODE_OF_CONDUCT.md', 'SECURITY.md', 'SUPPORT.md'}
DIRECTORIES = {'.github', 'docs', 'prompts', 'skills', 'vault', 'scripts', 'tests', 'tooling'}
SKIP_DIRS = {'__pycache__', '.git', '.pytest_cache', 'node_modules'}
SECRET_PATTERNS = [
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    re.compile(rb'\bgithub_pat_[A-Za-z0-9_]{30,}\b'),
    re.compile(rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b'),
    re.compile(rb'\bAKIA[A-Z0-9]{16}\b'),
]

def files():
    found = []
    for path in sorted(ROOT.rglob('*')):
        rel = path.relative_to(ROOT)
        if any(p in SKIP_DIRS for p in rel.parts) or path.suffix == '.pyc':
            continue
        if rel.parts[0] not in DIRECTORIES and rel.as_posix() not in ROOT_FILES:
            continue
        if path.is_symlink():
            raise ValueError(f'Symlink is not distributable: {rel}')
        if not path.is_file():
            continue
        if '.env' in path.name or path.suffix in {'.pem', '.key'} or 'credentials' in path.name.lower():
            raise ValueError(f'Credential-like filename is not distributable: {rel}')
        if len(rel.parts) > 2 and rel.parts[:2] == ('vault', 'system') and rel.parts[2] == 'state':
            raise ValueError(f'Runtime state is not distributable: {rel}')
        found.append(path)
    if not (ROOT/'prompts/START-HERE.ru.md').is_file():
        raise ValueError('Master prompt is missing')
    return found

def scan(paths, forbidden):
    errors = []
    for path in paths:
        data = path.read_bytes()
        rel = path.relative_to(ROOT).as_posix()
        if any(pattern.search(data) for pattern in SECRET_PATTERNS):
            errors.append(f'Potential secret: {rel}')
        if any(term.encode('utf-8').lower() in data.lower() for term in forbidden):
            errors.append(f'Forbidden private marker: {rel}')
    if errors:
        raise ValueError('\n'.join(errors))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['scan', 'build'])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--forbid', action='append', default=[])
    args = parser.parse_args()
    paths = files()
    scan(paths, args.forbid)
    if args.command == 'scan':
        print(json.dumps({'public_files':len(paths),'result':'passed','limits':'Pattern scan plus explicit markers; semantic review still required.'}))
        return
    if args.output is None:
        parser.error('build requires --output')
    output = args.output.expanduser().absolute()
    if output == ROOT or ROOT in output.parents:
        raise ValueError('Write release outside the source kit')
    if output.exists():
        raise ValueError('Refusing to overwrite release')
    for parent in [output, *output.parents]:
        if parent.is_symlink():
            raise ValueError('Output symlink is not allowed')
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {'version':(ROOT/'VERSION').read_text().strip(),'files':[]}
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            data = path.read_bytes()
            rel = path.relative_to(ROOT).as_posix()
            archive.writestr('llm-wiki-starter/'+rel, data)
            manifest['files'].append({'path':rel,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        archive.writestr('llm-wiki-starter/release-manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+'.sha256').write_text(digest+'  '+output.name+'\n')
    print(json.dumps({'archive':str(output),'files':len(paths),'sha256':digest}))

if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as exc:
        print(str(exc),file=sys.stderr)
        sys.exit(1)
