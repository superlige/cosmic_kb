#!/usr/bin/env python3
"""
Project understanding entrypoint for Cosmic/KD BOS repositories.

This script wraps local query scripts as read-only evidence tools and adds
small project-scanning helpers. It intentionally does not generate Java code
or load template-driven workflows.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable

if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SKILL_ROOT / ".cosmic-understand" / "config.json"
DEFAULT_CONFIG_EXAMPLE = SKILL_ROOT / ".cosmic-understand" / "config.example.json"


def _refs_root() -> Path:
    """references/rules 已下沉进 cosmic_kb 包（docs/设计方案/分发与多agent接入方案.md §2，单一源）。

    优先从已安装的 `cosmic_kb.semantics` 取（含 references/ 与 rules/ 两子目录）；取不到
    （未装包）才回退 skill 同级目录（老布局）。注意：本路由表里的 rel 形如 `references/...`、
    `rules/...`，正好以这两目录的**父目录**为根拼接。
    """
    try:
        from importlib.resources import files

        return Path(str(files("cosmic_kb.semantics")))
    except Exception:
        return SKILL_ROOT


REFS_ROOT = _refs_root()

REFERENCE_TOPICS = {
    "plugin-bill": "references/base/plugin/plugin-bill.md",
    "plugin-botp": "references/base/plugin/plugin-botp.md",
    "plugin-form": "references/base/plugin/plugin-form.md",
    "plugin-list": "references/base/plugin/plugin-list.md",
    "plugin-operation": "references/base/plugin/plugin-operation.md",
    "plugin-workflow": "references/base/plugin/plugin-workflow.md",
    "sdk-dynamic-object": "references/base/sdk/sdk-dynamic-object.md",
    "sdk-entity-model": "references/base/sdk/sdk-entity-model.md",
    "sdk-orm-access": "references/base/sdk/sdk-orm-access.md",
}

REQUIRED_LOCAL_ASSETS = [
    "scripts/cosmic-api-knowledge.py",
    "scripts/cosmic-form-metadata.py",
    "scripts/config_loader.py",
    "references/base/plugin/plugin-form.md",
    "references/base/plugin/plugin-list.md",
    "references/base/plugin/plugin-operation.md",
    "references/base/sdk/sdk-dynamic-object.md",
    "references/base/sdk/sdk-entity-model.md",
    "references/base/sdk/sdk-orm-access.md",
    "rules/anti-patterns.md",
    ".cosmic-understand/ok-cosmic.json",
]


def load_config(path: str | None) -> dict:
    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.append(DEFAULT_CONFIG)
    candidates.append(DEFAULT_CONFIG_EXAMPLE)

    for candidate in candidates:
        if candidate.is_file():
            with candidate.open("r", encoding="utf-8") as fh:
                return json.load(fh)
    return {}


def ok_cosmic_config(config: dict) -> str | None:
    value = config.get("okCosmicConfig") or os.environ.get("CQKD_OK_COSMIC_CONFIG")
    if value:
        path = Path(value)
        if not path.is_absolute():
            path = SKILL_ROOT / path
        return str(path)
    fallback = SKILL_ROOT / ".cosmic-understand" / "ok-cosmic.json"
    if fallback.is_file():
        return str(fallback)
    setup_fallback = SKILL_ROOT / "setup" / "ok-cosmic.json"
    return str(setup_fallback) if setup_fallback.is_file() else None


def run_local_script(script_name: str, script_args: list[str], config_path: str | None) -> int:
    script = SKILL_ROOT / "scripts" / script_name
    if not script.is_file():
        print(f"ERROR: local script not found: {script}", file=sys.stderr)
        print("Move the required script from ok-cosmic into this skill first.", file=sys.stderr)
        return 2

    cmd = [sys.executable, str(script)]
    if config_path and "--config" not in script_args:
        cmd.extend(["--config", config_path])
    cmd.extend(script_args)
    return subprocess.call(cmd)


def iter_scan_files(root: Path, include_exts: set[str], exclude_dirs: set[str]) -> Iterable[Path]:
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for name in files:
            path = Path(current) / name
            if path.suffix.lower() in include_exts:
                yield path


def scan_field(project_root: Path, field_key: str, include_exts: set[str], exclude_dirs: set[str], limit: int) -> int:
    if not project_root.is_dir():
        print(f"ERROR: project root not found: {project_root}", file=sys.stderr)
        return 2

    terms = [field_key]
    lower_terms = [t.lower() for t in terms if t]
    count = 0

    print(f"# Field scan: {field_key}")
    print(f"# Project: {project_root}")
    print("")

    for path in iter_scan_files(project_root, include_exts, exclude_dirs):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for line_no, line in enumerate(lines, start=1):
            low = line.lower()
            if any(term in low for term in lower_terms):
                rel = path.relative_to(project_root)
                kind = classify_field_hit(line, field_key)
                print(f"{rel}:{line_no}: [{kind}] {line.strip()}")
                count += 1
                if count >= limit:
                    print(f"\n# Hit limit reached: {limit}")
                    return 0

    if count == 0:
        print("No direct field-key hits found.")
    else:
        print(f"\n# Total hits: {count}")
    return 0


def classify_field_hit(line: str, field_key: str) -> str:
    low = line.lower()
    key = field_key.lower()
    write_markers = ("setvalue", ".set(", "put(", "setfield", "setdefaultvalue")
    read_markers = ("getvalue", ".get(", "getstring", "getlong", "getdate", "getboolean")
    filter_markers = ("qfilter", "where", "filter", "select", "query")

    if key not in low:
        return "context"
    if any(marker in low for marker in write_markers):
        return "possible-write"
    if any(marker in low for marker in read_markers):
        return "possible-read"
    if any(marker in low for marker in filter_markers):
        return "possible-filter"
    return "string-hit"


def refs_list() -> int:
    for topic, path in sorted(REFERENCE_TOPICS.items()):
        print(f"{topic}\t{path}")
    return 0


def refs_read(root: Path, topic: str) -> int:
    rel = REFERENCE_TOPICS.get(topic)
    if not rel:
        print(f"ERROR: unknown topic: {topic}", file=sys.stderr)
        print("Use: refs list", file=sys.stderr)
        return 2
    path = root / rel
    if not path.is_file():
        print(f"ERROR: reference not found: {path}", file=sys.stderr)
        return 2
    print(path.read_text(encoding="utf-8", errors="replace"))
    return 0


def doctor() -> int:
    missing = []
    print(f"# Skill root: {SKILL_ROOT}")
    print(f"# Refs root:  {REFS_ROOT}")
    for rel in REQUIRED_LOCAL_ASSETS:
        # references/ 与 rules/ 已下沉进包，按 REFS_ROOT 解析；scripts/.cosmic-understand 仍在 skill 下。
        base = REFS_ROOT if rel.startswith(("references/", "rules/")) else SKILL_ROOT
        path = base / rel
        status = "OK" if path.exists() else "MISSING"
        print(f"{status}\t{rel}")
        if not path.exists():
            missing.append(rel)

    print("")
    if missing:
        print(f"Missing assets: {len(missing)}")
        print("See MOVE_FROM_OK_COSMIC.md for the manual move list.")
        return 1
    print("All required local assets are present.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CQKD Cosmic project understanding helper")
    parser.add_argument("--config", help="Path to .cosmic-understand config JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    api_parser = subparsers.add_parser("api", help="Pass through to cosmic-api-knowledge.py")
    api_parser.add_argument("args", nargs=argparse.REMAINDER)

    meta_parser = subparsers.add_parser("meta", help="Pass through to cosmic-form-metadata.py")
    meta_parser.add_argument("args", nargs=argparse.REMAINDER)

    refs_parser = subparsers.add_parser("refs", help="Read local references by topic")
    refs_sub = refs_parser.add_subparsers(dest="refs_command", required=True)
    refs_sub.add_parser("list")
    refs_read_parser = refs_sub.add_parser("read")
    refs_read_parser.add_argument("topic")

    subparsers.add_parser("doctor", help="Check whether required local skill assets have been moved in")

    scan_parser = subparsers.add_parser("scan-field", help="Scan project source for direct field-key hits")
    scan_parser.add_argument("--project-root")
    scan_parser.add_argument("--field-key", required=True)
    scan_parser.add_argument("--limit", type=int, default=200)

    args = parser.parse_args()
    config = load_config(args.config)
    cosmic_config = ok_cosmic_config(config)

    if args.command == "api":
        return run_local_script("cosmic-api-knowledge.py", args.args, cosmic_config)
    if args.command == "meta":
        return run_local_script("cosmic-form-metadata.py", args.args, cosmic_config)
    if args.command == "refs":
        if args.refs_command == "list":
            return refs_list()
        return refs_read(REFS_ROOT, args.topic)
    if args.command == "doctor":
        return doctor()
    if args.command == "scan-field":
        scan_cfg = config.get("scan", {})
        default_project = config.get("defaultProjectRoot") or "."
        project_root = Path(args.project_root or default_project)
        include_exts = {ext.lower() for ext in scan_cfg.get("includeExtensions", [".java", ".xml", ".dym", ".json"])}
        exclude_dirs = set(scan_cfg.get("excludeDirs", [".git", ".gradle", "build", "target", "out"]))
        return scan_field(project_root, args.field_key, include_exts, exclude_dirs, args.limit)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
