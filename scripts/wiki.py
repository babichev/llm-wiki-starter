#!/usr/bin/env python3
"""Portable local Wiki bookkeeping. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
import uuid

VERSION = "1.1.0"
START = "<!-- GENERATED:START -->"
END = "<!-- GENERATED:END -->"
EXCLUDED_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", "writer.lock"}
EXCLUDED_NAMES = {".DS_Store", ".env", "credentials.json", "token.json", "tokens.json"}
REQUIRED_META = {"id", "title", "type", "status", "version", "created", "updated", "closed", "replaces", "replaced_by", "owner", "access", "source_refs"}


class WikiError(Exception):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(value=None):
    return (value or now()).isoformat(timespec="seconds")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def no_symlinks(path):
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    for item in [path, *path.parents]:
        if item.is_symlink():
            raise WikiError(f"Symlink is not allowed: {item}")
    return path


def is_within(path, root):
    return path == root or root in path.parents


def safe_child(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise WikiError("Invalid relative path")
    p = PurePosixPath(relative)
    if p.is_absolute() or any(part in {"..", ""} for part in p.parts):
        raise WikiError(f"Unsafe relative path: {relative}")
    target = no_symlinks(root.joinpath(*p.parts))
    if not is_within(target, root):
        raise WikiError(f"Path escapes root: {relative}")
    return target


def read_json(path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise WikiError(f"Cannot read JSON {path}: {exc}") from exc


def atomic_write(path, data):
    no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".wiki-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data if isinstance(data, bytes) else data.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def state_path(vault, name):
    return safe_child(vault, "system/state/" + name)


def check_vault(vault):
    if not vault.is_dir() or not (vault / "second-brain-home.md").is_file():
        raise WikiError(f"Not an initialized vault: {vault}")
    no_symlinks(vault / "system/state")


def get_lease(vault):
    lock = state_path(vault, "writer.lock")
    if not lock.exists():
        return None
    value = read_json(lock / "lease.json")
    if not isinstance(value, dict):
        raise WikiError("Incomplete writer lock. Verify no writer is active before manual recovery.")
    return value


def require_lease(vault, token, allow_expired=False):
    value = get_lease(vault)
    if not token or not value or value.get("run_id") != token:
        raise WikiError("A matching writer lease --run is required")
    try:
        expired = dt.datetime.fromisoformat(value["expires_at"]) <= now()
    except (ValueError, KeyError, TypeError) as exc:
        raise WikiError("Invalid lease metadata; recover manually after checking the writer") from exc
    if expired and not allow_expired:
        raise WikiError("Writer lease expired. Finish the owned run and start a new one; no automatic takeover.")
    return value


def cmd_init(args):
    kit = Path(__file__).resolve().parents[1]
    template = no_symlinks(kit / "vault")
    if not template.is_dir():
        raise WikiError("init must be run from the starter kit scripts/wiki.py")
    vault = args.vault
    if is_within(vault, kit) or is_within(kit, vault):
        raise WikiError("Vault and starter kit must be separate, non-nested directories")
    planned = []
    for base, prefix in [(template, ""), (kit / "skills", ".agents/skills/")]:
        if not base.is_dir():
            continue
        no_symlinks(base)
        for source in sorted(base.rglob("*")):
            if source.is_symlink():
                raise WikiError(f"Template contains symlink: {source}")
            if source.is_file():
                rel = prefix + source.relative_to(base).as_posix()
                planned.append((source, safe_child(vault, rel)))
    planned.append((Path(__file__).resolve(), safe_child(vault, "system/scripts/wiki.py")))
    conflicts = [str(dst) for src, dst in planned if dst.exists() and (not dst.is_file() or dst.read_bytes() != src.read_bytes())]
    if conflicts:
        raise WikiError("Refusing to overwrite existing files: " + ", ".join(conflicts[:8]))
    for _, dst in planned:
        for parent in dst.parents:
            if parent == vault.parent:
                break
            if parent.exists() and not parent.is_dir():
                raise WikiError(f"Parent is not a directory: {parent}")
    copied = 0
    for src, dst in planned:
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open("xb") as out:
            out.write(src.read_bytes())
        copied += 1
    for directory in ("inbox", "Clippings", "raw", "artifacts", "archive", "system/state", "system/runs"):
        safe_child(vault, directory).mkdir(parents=True, exist_ok=True)
    return {"vault": str(vault), "copied": copied, "existing_equal": len(planned) - copied, "automations_created": 0}


def cmd_start(args):
    lock = state_path(args.vault, "writer.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise WikiError("Writer lock already exists, including if expired. Inspect status; never silently take over.") from exc
    started = now()
    value = {"run_id": str(uuid.uuid4()), "owner": args.owner, "started_at": iso(started), "expires_at": iso(started + dt.timedelta(minutes=args.ttl_minutes))}
    try:
        write_json(lock / "lease.json", value)
    except Exception:
        shutil.rmtree(lock)
        raise
    return value


def cmd_renew(args):
    value = require_lease(args.vault, args.run)
    value["expires_at"] = iso(now() + dt.timedelta(minutes=args.ttl_minutes))
    write_json(state_path(args.vault, "writer.lock/lease.json"), value)
    return value


def cmd_finish(args):
    value = require_lease(args.vault, args.run, allow_expired=True)
    lock = state_path(args.vault, "writer.lock")
    (lock / "lease.json").unlink()
    lock.rmdir()
    return {"finished": value["run_id"]}


def registry(vault):
    result = read_json(state_path(vault, "sources.json"), {"version": 1, "sources": {}})
    if not isinstance(result, dict) or not isinstance(result.get("sources"), dict):
        raise WikiError("Unsupported source registry schema")
    return result


def cmd_status(args):
    data = registry(args.vault)
    counts = {}
    for item in data["sources"].values():
        state = item.get("state", "unknown")
        counts[state] = counts.get(state, 0) + 1
    return {"vault": str(args.vault), "version": VERSION, "lease": get_lease(args.vault), "source_counts": counts, "note": "Local state only; no sync or publication implied."}


def verify_source_snapshot(vault, source):
    """Refuse to treat registry state as proof of an intact saved source."""
    digest = source.get("current_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise WikiError("Source registry has an invalid current SHA-256")
    rel = source.get("snapshot")
    if not isinstance(rel, str) or not rel.startswith("raw/"):
        raise WikiError("Source registry has an invalid snapshot path")
    snapshot = safe_child(vault, rel)
    if not snapshot.is_file():
        raise WikiError(f"Current immutable snapshot is missing: {rel}; recover it before processing")
    if sha(snapshot.read_bytes()) != digest:
        raise WikiError(f"Current immutable snapshot is corrupt: {rel}; recover it before processing")
    return snapshot


def cmd_ingest(args):
    require_lease(args.vault, args.run)
    if len(args.key) > 1000 or not args.key.strip():
        raise WikiError("Source key must be nonempty and at most 1000 characters")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", args.kind):
        raise WikiError("kind must contain lowercase letters, digits or hyphens")
    if not args.evidence_kind.strip():
        raise WikiError("evidence-kind must be nonempty")
    content = Path(args.content_file).read_bytes() if args.content_file else args.content.encode("utf-8")
    try:
        content.decode("utf-8")
    except UnicodeError as exc:
        raise WikiError("ingest accepts UTF-8 text; keep binary artifacts separately") from exc
    data = registry(args.vault)
    source = data["sources"].get(args.key)
    if source and source.get("kind") != args.kind:
        raise WikiError("Source kind is immutable for a stable key; use its original kind")
    content_sha = sha(content)
    key_sha = sha(args.key.encode())
    rel = f"raw/{args.kind}/{key_sha}/{content_sha}.txt"
    target = safe_child(args.vault, rel)
    metadata = {"title": args.title, "url": args.url, "source_updated_at": args.source_updated_at,
                "coverage": args.coverage, "evidence_kind": args.evidence_kind, "access": args.access}
    same_content = bool(source and source.get("current_sha256") == content_sha)
    if same_content:
        verify_source_snapshot(args.vault, source)
    if target.exists() and (not target.is_file() or target.read_bytes() != content):
        raise WikiError("Immutable snapshot content mismatch")
    metadata_changed = bool(source and any(source.get(field) != value for field, value in metadata.items()))
    if same_content and not metadata_changed:
        return {"key": args.key, "changed": False, "content_changed": False, "metadata_changed": False,
                "state": source["state"], "snapshot": source["snapshot"], "sha256": content_sha}
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as out:
            out.write(content)
    versions = list(source.get("versions", [])) if source else []
    version = {"sha256": content_sha, "snapshot": rel, "captured_at": iso(), **metadata}
    # Metadata revisions keep their provenance while reusing the immutable content file.
    versions.append(version)
    item = {"kind": args.kind, "key": args.key, "current_sha256": content_sha, "snapshot": rel,
            "state": "pending", "versions": versions, "links": source.get("links", []) if source else [],
            "updated_at": iso(), **metadata}
    data["sources"][args.key] = item
    write_json(state_path(args.vault, "sources.json"), data)
    return {"key": args.key, "changed": True, "content_changed": not same_content,
            "metadata_changed": metadata_changed, "state": "pending", "snapshot": rel, "sha256": content_sha}


def cmd_mark(args):
    require_lease(args.vault, args.run)
    data = registry(args.vault)
    source = data["sources"].get(args.key)
    if source is None:
        raise WikiError("Unknown source key")
    if args.expected_sha256 != source.get("current_sha256"):
        raise WikiError("Stale source SHA-256: reread the current snapshot and its metadata before marking")
    verify_source_snapshot(args.vault, source)
    links = []
    for rel in args.links:
        path = safe_child(args.vault, rel)
        if not path.is_file() or path.suffix.lower() != ".md" or path.parts[len(args.vault.parts)] not in {"wiki", "artifacts", "system"}:
            raise WikiError(f"Canonical Markdown link does not exist: {rel}")
        links.append(path.relative_to(args.vault).as_posix())
    if not links:
        raise WikiError("mark requires at least one existing canonical link")
    source["state"] = args.state
    source["links"] = sorted(set(source.get("links", []) + links))
    source["marked_at"] = iso()
    source["marked_by"] = args.run
    source["marked_sha256"] = args.expected_sha256
    source["mark_reason"] = args.reason
    write_json(state_path(args.vault, "sources.json"), data)
    return {"key": args.key, "state": args.state, "links": source["links"], "sha256": args.expected_sha256,
            "note": "Bookkeeping reflects agent review; script cannot validate semantic correctness."}


def parse_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    result = {}
    for line in lines[1:end]:
        match = re.match(r"^([a-zA-Z_][a-zA-Z0-9_-]*):\s*(.*)$", line)
        if match:
            result[match[1]] = match[2].strip().strip('"\'')
    return result


def markdown_files(vault):
    result = []
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault)
        if any(p in EXCLUDED_DIRS or p == ".agents" for p in rel.parts) or path.is_symlink():
            continue
        no_symlinks(path)
        result.append(path)
    return result


def markdown_body(text):
    text = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return text


def cmd_audit(args):
    files = markdown_files(args.vault)
    stems = {}
    ids = {}
    errors, warnings = [], []
    for path in files:
        stems.setdefault(path.stem, []).append(path)
    for path in files:
        rel = path.relative_to(args.vault).as_posix()
        text = path.read_text(encoding="utf-8")
        front = parse_frontmatter(text)
        substantive = rel == "second-brain-home.md" or rel.startswith(("wiki/", "playbooks/", "system/")) and "/scripts/" not in rel
        if substantive:
            if front is None:
                errors.append({"file": rel, "error": "missing YAML frontmatter"})
            else:
                missing = sorted(REQUIRED_META - front.keys())
                if missing:
                    errors.append({"file": rel, "error": "missing metadata: " + ", ".join(missing)})
                identity = front.get("id")
                if identity:
                    if identity in ids:
                        errors.append({"file": rel, "error": f"duplicate id with {ids[identity]}"})
                    ids[identity] = rel
                review = front.get("review_after")
                if review and review not in {"null", "~", ""}:
                    try:
                        if dt.date.fromisoformat(review) < now().date():
                            warnings.append({"file": rel, "warning": "review_after is overdue"})
                    except ValueError:
                        warnings.append({"file": rel, "warning": "review_after is not an ISO date"})
        body = markdown_body(text)
        for target in re.findall(r"\[\[([^\]]+)\]\]", body):
            target = target.split("|", 1)[0].split("#", 1)[0]
            if not target:
                continue
            candidate = target if Path(target).suffix else target + ".md"
            options = [path.parent / candidate, args.vault / candidate]
            if "/" not in target and Path(target).stem in stems:
                options.extend(stems[Path(target).stem])
            if not any(p.is_file() and is_within(p.resolve(), args.vault) for p in options):
                errors.append({"file": rel, "error": f"broken wikilink: {target}"})
        for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", body):
            target = target.split(' "', 1)[0].strip("<>").split("#", 1)[0]
            if not target or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                continue
            from urllib.parse import unquote
            resolved = (path.parent / unquote(target)).resolve()
            if not is_within(resolved, args.vault) or not resolved.exists():
                errors.append({"file": rel, "error": f"broken or outside relative link: {target}"})
    return {"files_checked": len(files), "errors": errors, "warnings": warnings, "ok": not errors, "limits": "Basic flat YAML keys and file targets only; no full YAML parser, anchor check, external URL check or factual validation."}


def cmd_index(args):
    require_lease(args.vault, args.run)
    target = safe_child(args.vault, "wiki/index.md")
    text = target.read_text(encoding="utf-8")
    if text.count(START) != 1 or text.count(END) != 1 or text.index(START) >= text.index(END):
        raise WikiError("wiki/index.md must contain exactly one ordered generated marker pair")
    entries = []
    for path in markdown_files(args.vault):
        rel = path.relative_to(args.vault)
        if not rel.as_posix().startswith("wiki/") or path == target:
            continue
        front = parse_frontmatter(path.read_text(encoding="utf-8")) or {}
        title = front.get("title", path.stem).replace("[", "(").replace("]", ")")
        from urllib.parse import quote
        url = quote(path.relative_to(target.parent).as_posix())
        entries.append(f"- [{title}]({url})")
    generated = "\n" + "\n".join(entries) + "\n"
    new_text = text[:text.index(START) + len(START)] + generated + text[text.index(END):]
    if new_text != text:
        atomic_write(target, new_text)
    return {"entries": len(entries), "changed": new_text != text, "manual_content_preserved": True}


def walk_regular(root):
    files, skipped = [], []
    for base, dirs, names in os.walk(root, followlinks=False):
        base = Path(base)
        kept = []
        for name in dirs:
            item = base / name
            rel = item.relative_to(root).as_posix()
            if name in EXCLUDED_DIRS or item.is_symlink():
                skipped.append(rel)
            else:
                kept.append(name)
        dirs[:] = sorted(kept)
        for name in sorted(names):
            item = base / name
            rel = item.relative_to(root).as_posix()
            if name in EXCLUDED_NAMES or name.startswith(".env.") or item.is_symlink() or not item.is_file():
                skipped.append(rel)
                continue
            files.append((rel, item))
    return files, sorted(skipped)


def cmd_backup(args):
    vault = args.vault
    lease = get_lease(vault)
    if lease:
        require_lease(vault, args.run)
    destination = no_symlinks(args.destination)
    if is_within(destination, vault) or is_within(vault, destination):
        raise WikiError("Backup destination must be outside and not an ancestor of the vault")
    if destination.exists():
        raise WikiError("Backup destination must not exist")
    original, skipped = walk_regular(vault)
    destination.mkdir(parents=True)
    (destination / "files").mkdir()
    manifest = {"version": 1, "created_at": iso(), "files": {}, "skipped": skipped}
    try:
        for rel, source in original:
            no_symlinks(source)
            content = source.read_bytes()
            out = safe_child(destination / "files", rel)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(content)
            manifest["files"][rel] = {"sha256": sha(content), "bytes": len(content)}
        after, _ = walk_regular(vault)
        if [p for p, _ in after] != [p for p, _ in original] or any(sha(p.read_bytes()) != manifest["files"][rel]["sha256"] for rel, p in after):
            raise WikiError("Vault changed while copying. Incomplete backup retained with no manifest; retry under writer lease.")
        write_json(destination / "manifest.json", manifest)
    except Exception:
        # Incomplete output is deliberately not presented as a verified snapshot.
        raise
    return {"snapshot": str(destination), "files": len(manifest["files"]), "skipped": skipped, "manifest_sha256": sha((destination / "manifest.json").read_bytes()), "encrypted": False}


def verify_snapshot(snapshot, expected_hash=None):
    snapshot = no_symlinks(snapshot)
    manifest_path = no_symlinks(snapshot / "manifest.json")
    if expected_hash and sha(manifest_path.read_bytes()) != expected_hash:
        raise WikiError("Manifest SHA-256 does not match trusted receipt")
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
        raise WikiError("Invalid backup manifest")
    if not (snapshot / "files").is_dir():
        raise WikiError("Missing backup files directory")
    for rel, spec in manifest["files"].items():
        path = safe_child(snapshot / "files", rel)
        if not isinstance(spec, dict) or not path.is_file():
            raise WikiError(f"Missing or invalid snapshot entry: {rel}")
        content = path.read_bytes()
        if sha(content) != spec.get("sha256") or len(content) != spec.get("bytes"):
            raise WikiError(f"Snapshot checksum mismatch: {rel}")
    actual, skipped = walk_regular(snapshot / "files")
    if skipped or set(rel for rel, _ in actual) != set(manifest["files"]):
        raise WikiError("Snapshot contains unlisted files, skipped objects or symlinks")
    return manifest


def cmd_verify(args):
    manifest = verify_snapshot(args.snapshot, args.manifest_sha256)
    return {"verified": True, "files": len(manifest["files"]), "authenticity": "Checked against supplied receipt" if args.manifest_sha256 else "Checksums only; provide --manifest-sha256 from trusted receipt for manifest integrity."}


def cmd_restore(args):
    snapshot = no_symlinks(args.snapshot)
    target = no_symlinks(args.target)
    if is_within(target, snapshot) or is_within(snapshot, target):
        raise WikiError("Restore target must be separate from snapshot")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise WikiError("Restore target must be a new or empty directory")
    manifest = verify_snapshot(snapshot, args.manifest_sha256)
    # Validate every target before making the first restored file.
    outputs = [(rel, safe_child(target, rel)) for rel in manifest["files"]]
    target.mkdir(parents=True, exist_ok=True)
    for rel, out in outputs:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("xb") as stream:
            stream.write(safe_child(snapshot / "files", rel).read_bytes())
        if sha(out.read_bytes()) != manifest["files"][rel]["sha256"]:
            raise WikiError(f"Restored readback failed: {rel}")
    return {"restored": len(outputs), "target": str(target), "note": "Review this restored copy before replacing any active vault. Credentials excluded."}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vault", help="Vault root; defaults to current vault when running its installed script")
    sub = p.add_subparsers(dest="command", required=True)
    def command(name, fn):
        q = sub.add_parser(name)
        q.add_argument("--vault", default=argparse.SUPPRESS)
        q.set_defaults(fn=fn)
        return q
    command("init", cmd_init)
    q = command("start-run", cmd_start)
    q.add_argument("--owner", default="local-agent")
    q.add_argument("--ttl-minutes", type=int, choices=range(1, 1441), default=60, metavar="1..1440")
    q = command("renew-run", cmd_renew)
    q.add_argument("--run", required=True)
    q.add_argument("--ttl-minutes", type=int, choices=range(1, 1441), default=60, metavar="1..1440")
    q = command("finish-run", cmd_finish)
    q.add_argument("--run", required=True)
    command("status", cmd_status)
    q = command("ingest", cmd_ingest)
    for name in ("run", "kind", "key", "title"):
        q.add_argument("--" + name, required=True)
    content = q.add_mutually_exclusive_group(required=True)
    content.add_argument("--content-file")
    content.add_argument("--content")
    q.add_argument("--url")
    q.add_argument("--source-updated-at")
    q.add_argument("--coverage", choices=("full", "partial", "unknown"), default="unknown")
    q.add_argument("--access", choices=("private", "confidential", "internal", "public"), default="private")
    q.add_argument("--evidence-kind", default="unknown")
    q = command("mark", cmd_mark)
    q.add_argument("--run", required=True)
    q.add_argument("--key", required=True)
    q.add_argument("--expected-sha256", required=True)
    q.add_argument("--reason")
    q.add_argument("--state", choices=("processed", "needs-review"), required=True)
    q.add_argument("--links", nargs="+", required=True)
    q = command("index", cmd_index)
    q.add_argument("--run", required=True)
    command("audit", cmd_audit)
    q = command("backup", cmd_backup)
    q.add_argument("--destination", required=True)
    q.add_argument("--run")
    q = command("verify-backup", cmd_verify)
    q.add_argument("--snapshot", required=True)
    q.add_argument("--manifest-sha256")
    q = command("restore", cmd_restore)
    q.add_argument("--snapshot", required=True)
    q.add_argument("--target", required=True)
    q.add_argument("--manifest-sha256")
    return p


def main():
    args = parser().parse_args()
    try:
        if not args.vault:
            script = Path(__file__).resolve()
            args.vault = str(script.parents[2]) if script.parent.name == "scripts" and script.parent.parent.name == "system" else os.getcwd()
        args.vault = no_symlinks(args.vault)
        if args.command not in {"init", "verify-backup", "restore"}:
            check_vault(args.vault)
        result = args.fn(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.command == "audit" and not result["ok"] else 0
    except (WikiError, OSError, UnicodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
