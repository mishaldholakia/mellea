#!/usr/bin/env python3
"""
generate-ast.py

What this does:
  1) Runs mdxify for PACKAGES (mellea, cli) to generate MDX into a STAGING folder:
        <repo-root>/docs/api/<pkg>/...
  2) Reorganizes flat mdxify output into nested folders
  3) Renames __init__.mdx -> <foldername>.mdx (dedupes if identical)
  4) Updates frontmatter (title/sidebarTitle/description) + removes empty MDX files
  5) Moves the generated API docs to your Mintlify docs root:
        <mintlify-docs-root>/api
     - If <mintlify-docs-root>/api already exists, it is deleted first.
  6) Builds the Mintlify "API Reference" nav from the *moved* files
  7) MERGES that nav into an existing docs.json by replacing ONLY:
        the tab { "tab": "API Reference", ... }

Important behavior:
  - Only TWO top-level groups in the API Reference tab: "mellea" and "cli"
  - Paths written to docs.json DO NOT include ".mdx" suffix

Usage:
  python3 tooling/docs-autogen/generate-ast.py \
    --docs-json docs/docs/docs.json \
    --docs-root docs/docs
"""

import os
import sys
import json
import glob
import re
import subprocess
import argparse
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


NAV_TAB = "API Reference"

# Script is in tooling/docs-autogen/generate-ast.py -> repo root is 2 parents up
REPO_ROOT = Path(__file__).resolve().parents[2]

# Staging output (keeps your prior convention)
STAGING_DOCS_ROOT = REPO_ROOT / "docs"
STAGING_API_DIR = STAGING_DOCS_ROOT / "api"

# Only focus on these two top-level packages in the repo
PACKAGES = ["mellea", "cli"]

# Optional: add source links in generated pages
REPO_URL = "https://github.com/generative-computing/mellea"


# -----------------------------
# Helpers
# -----------------------------
def yaml_quote(value: Optional[str]) -> str:
    """Safe YAML string quoting for Mintlify frontmatter."""
    if value is None:
        return '""'
    v = str(value)
    v = v.replace("\\", "\\\\").replace('"', '\\"')
    v = v.replace("\r\n", "\n").replace("\n", "\\n")
    return f'"{v}"'


def is_meaningful_body_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.startswith("<!--") and s.endswith("-->"):
        return False
    if s.startswith("#"):
        return False
    return True


def strip_frontmatter(lines: List[str]) -> List[str]:
    if lines and lines[0].strip() == "---":
        try:
            end_idx = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
            return lines[end_idx + 1 :]
        except StopIteration:
            return []
    return lines


def safe_read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def safe_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def find_docs_json(cli_path: Optional[str]) -> Path:
    if cli_path:
        p = Path(cli_path)
        if not p.is_absolute():
            p = (REPO_ROOT / p).resolve()
        if not p.exists():
            raise FileNotFoundError(f"--docs-json path not found: {p}")
        return p

    candidates = [
        REPO_ROOT / "docs.json",
        REPO_ROOT / "docs" / "docs.json",
        REPO_ROOT / "docs" / "docs" / "docs.json",
    ]
    for c in candidates:
        if c.exists():
            return c

    raise FileNotFoundError(
        "Could not locate docs.json. Pass --docs-json explicitly, e.g. "
        "--docs-json docs/docs/docs.json"
    )


def merge_api_reference_into_docs_json(docs_json_path: Path, api_tab_obj: Dict[str, Any]) -> None:
    data = json.loads(docs_json_path.read_text(encoding="utf-8"))

    # Mintlify docs.json commonly uses navigation.tabs
    nav = data.get("navigation") or {}
    tabs = nav.get("tabs") or []

    if not isinstance(tabs, list) or not tabs:
        raise RuntimeError("docs.json has no navigation.tabs (or it's empty)")

    replaced = False
    for i, tab in enumerate(tabs):
        if isinstance(tab, dict) and tab.get("tab") == NAV_TAB:
            tabs[i] = api_tab_obj
            replaced = True
            break

    if not replaced:
        raise RuntimeError(f'No tab named "{NAV_TAB}" found in docs.json')

    nav["tabs"] = tabs
    data["navigation"] = nav

    docs_json_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"✅ Merged API Reference tab into: {docs_json_path}", flush=True)


# -----------------------------
# Step 1: Environment setup
# -----------------------------
def setup_env() -> None:
    STAGING_API_DIR.mkdir(parents=True, exist_ok=True)

    # Ensure Python can import `mellea` and `cli` from the repo checkout
    # Repo layout: <root>/mellea/... and <root>/cli/...
    os.environ["PYTHONPATH"] = str(REPO_ROOT)

    print(f"Setting PYTHONPATH to: {os.environ['PYTHONPATH']}", flush=True)
    print(f"Staging API output: {STAGING_API_DIR}", flush=True)
    print("-" * 30, flush=True)


# -----------------------------
# Step 2: Run mdxify (generation only) - UPDATED CLI SHAPE
# -----------------------------
def run_mdxify_generation(root_module: str) -> None:
    """
    New mdxify CLI pattern (per docs):
      mdxify --all --root-module <pkg> --output-dir <dir> ...

    NOTE: Do NOT pass <pkg> as a positional argument when using --all.
    """
    output_dir = STAGING_API_DIR / root_module
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"➡️ Generating documentation for root module: {root_module} into {output_dir}", flush=True)

    cmd = [
        sys.executable,
        "-m",
        "mdxify",
        "--all",
        "--root-module",
        root_module,
        "--output-dir",
        str(output_dir),
        "--update-nav",
        "false",
        "--repo-url",
        REPO_URL,
    ]

    # If you want stable links to a non-main branch in CI, uncomment:
    # cmd += ["--branch", os.getenv("GITHUB_REF_NAME", "main")]

    try:
        subprocess.run(cmd, check=True, text=True)
        print(f"✅ Successfully generated docs for {root_module}", flush=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ Error generating docs for {root_module}: {e}", flush=True)
        sys.exit(1)


# -----------------------------
# Step 3: Reorganize mdxify flat output -> nested folders
# -----------------------------
def reorganize_to_nested_structure() -> None:
    print("-" * 30, flush=True)
    print("📁 Reorganizing MDX files into nested folder structure...", flush=True)

    all_mdx = glob.glob(str(STAGING_API_DIR / "**" / "*.mdx"), recursive=True)

    for old in all_mdx:
        old_path = Path(old)

        pkg = old_path.parent.name
        parent_dir = old_path.parent

        if pkg not in PACKAGES:
            continue

        # Only reorganize files directly under docs/api/<pkg> (flat)
        if parent_dir != (STAGING_API_DIR / pkg):
            continue

        base = old_path.stem
        prefix = f"{pkg}-"
        if not base.startswith(prefix):
            continue

        module_path_raw = base[len(prefix) :]
        if not module_path_raw:
            continue

        parts = module_path_raw.split("-")
        new_dir = STAGING_API_DIR / pkg / Path(*parts[:-1])
        new_path = new_dir / f"{parts[-1]}.mdx"

        if new_path.resolve() == old_path.resolve():
            continue

        new_dir.mkdir(parents=True, exist_ok=True)
        print(f"   Moving {old_path} -> {new_path}", flush=True)
        old_path.replace(new_path)

    print("✅ Folder reorganization complete.", flush=True)


# -----------------------------
# Step 3b: Rename __init__.mdx -> <foldername>.mdx (dedupe if identical)
# -----------------------------
def rename_init_files_to_parent() -> None:
    print("-" * 30, flush=True)
    print("📛 Renaming __init__.mdx files to folder-name.mdx (dedupe if identical)...", flush=True)

    init_files = glob.glob(str(STAGING_API_DIR / "**" / "__init__.mdx"), recursive=True)

    def normalize_text(s: str) -> str:
        return "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").split("\n")).strip()

    for old in init_files:
        old_path = Path(old)
        folder = old_path.parent.name
        new_path = old_path.parent / f"{folder}.mdx"

        if not new_path.exists():
            print(f"   Renaming {old_path} -> {new_path}", flush=True)
            old_path.rename(new_path)
            continue

        try:
            old_txt = normalize_text(safe_read_text(old_path))
            new_txt = normalize_text(safe_read_text(new_path))
        except Exception as e:
            print(f"   ⚠️ Could not compare {old_path} and {new_path}: {e}. Keeping __init__.mdx.", flush=True)
            continue

        if old_txt == new_txt:
            print(f"   🗑️ Duplicate content: removing {old_path} (same as {new_path})", flush=True)
            old_path.unlink(missing_ok=True)
        else:
            print(f"   ⚠️ Content differs: keeping {old_path} (leaving existing {new_path})", flush=True)

    print("✅ __init__.mdx rename/dedupe pass complete.", flush=True)


# -----------------------------
# Step 4: Update frontmatter (title, sidebarTitle, description)
# -----------------------------
def extract_title_and_description(body_lines: List[str]) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[int]]:
    h1_pattern = re.compile(r"^#\s+`?(.+?)`?\s*$")

    title_value = None
    h1_idx = None
    for i, line in enumerate(body_lines):
        m = h1_pattern.match(line.strip())
        if m:
            title_value = m.group(1).strip("`").strip()
            h1_idx = i
            break

    if not title_value or h1_idx is None:
        return None, None, None, None

    desc_value = None
    desc_idx = None
    for j in range(h1_idx + 1, len(body_lines)):
        s = body_lines[j].strip()
        if not s:
            continue
        if s.startswith("#"):
            break
        if s.startswith("```"):
            continue
        desc_value = s
        desc_idx = j
        break

    return title_value, desc_value, h1_idx, desc_idx


def update_frontmatter_metadata() -> None:
    print("-" * 30, flush=True)
    print("📝 Updating frontmatter title/description/sidebarTitle from content...", flush=True)

    mdx_files = glob.glob(str(STAGING_API_DIR / "**" / "*.mdx"), recursive=True)

    for p in mdx_files:
        path = Path(p)
        text = safe_read_text(path)
        lines = text.splitlines()

        if not lines or lines[0].strip() != "---":
            continue

        try:
            end_idx = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        except StopIteration:
            continue

        front_lines = lines[1:end_idx]
        body_lines = lines[end_idx + 1 :]

        title_value, desc_value, h1_idx, desc_idx = extract_title_and_description(body_lines)
        if not title_value:
            continue

        preserved = []
        for line in front_lines:
            k = line.strip()
            if k.startswith("title:") or k.startswith("sidebarTitle:") or k.startswith("description:"):
                continue
            preserved.append(line)

        cleaned_body: List[str] = []
        for idx, line in enumerate(body_lines):
            if h1_idx is not None and idx == h1_idx:
                continue
            if desc_idx is not None and idx == desc_idx:
                continue
            cleaned_body.append(line)

        new_front = ["---"]
        new_front.append(f"title: {yaml_quote(title_value)}")
        new_front.append(f"sidebarTitle: {yaml_quote(title_value)}")
        if desc_value:
            new_front.append(f"description: {yaml_quote(desc_value)}")
        new_front.extend(preserved)
        new_front.append("---")

        new_text = "\n".join(new_front + cleaned_body).rstrip() + "\n"
        safe_write_text(path, new_text)

    print("✅ Frontmatter update complete.", flush=True)


# -----------------------------
# Step 4b: Remove empty/no-content MDX
# -----------------------------
def remove_empty_mdx_files() -> None:
    print("-" * 30, flush=True)
    print("🧹 Removing empty/no-content MDX files...", flush=True)

    mdx_files = glob.glob(str(STAGING_API_DIR / "**" / "*.mdx"), recursive=True)
    removed = 0

    for p in mdx_files:
        path = Path(p)
        lines = safe_read_text(path).splitlines()
        body = strip_frontmatter(lines)

        meaningful = any(is_meaningful_body_line(line) for line in body)
        if not meaningful:
            print(f"   🗑️ Removing empty file: {path}", flush=True)
            path.unlink(missing_ok=True)
            removed += 1

    print(f"✅ Removed {removed} empty files.", flush=True)


# -----------------------------
# Step 5: Move staging api -> target docs root
# -----------------------------
def move_api_to_docs_root(target_docs_root: Path) -> Path:
    """
    Move <repo>/docs/api -> <target_docs_root>/api
    If <target_docs_root>/api exists, delete it first.

    Returns the final API dir path.
    """
    target_docs_root = target_docs_root.resolve()
    target_api_dir = target_docs_root / "api"

    print("-" * 30, flush=True)
    print(f"📦 Moving generated API docs to: {target_api_dir}", flush=True)

    if not STAGING_API_DIR.exists():
        raise RuntimeError(f"Staging API dir not found: {STAGING_API_DIR}")

    if target_api_dir.exists():
        print(f"   🧹 Deleting existing target api dir: {target_api_dir}", flush=True)
        shutil.rmtree(target_api_dir)

    target_docs_root.mkdir(parents=True, exist_ok=True)
    shutil.move(str(STAGING_API_DIR), str(target_api_dir))

    print("✅ Move complete.", flush=True)
    return target_api_dir


# -----------------------------
# Step 6: Build Mintlify navigation from moved files (NO .mdx in output)
# -----------------------------
def build_tree_from_paths(paths: List[str]) -> Dict[str, Any]:
    root: Dict[str, Any] = {}

    def insert(node: Dict[str, Any], parts: List[str], page_path: str) -> None:
        if not parts:
            node.setdefault("__pages__", []).append(page_path)
            return
        k = parts[0]
        node.setdefault(k, {})
        insert(node[k], parts[1:], page_path)

    for p in paths:
        parts = p.split("/")
        # p is "api/<pkg>/...."
        if len(parts) < 3:
            continue
        sub = parts[2:]  # after api/<pkg>
        insert(root, sub[:-1], p)

    return root


def tree_to_mintlify(node: Dict[str, Any], group_name: str) -> Dict[str, Any]:
    pages: List[Any] = []

    file_pages = node.get("__pages__", [])
    if file_pages:
        pages.extend(sorted(file_pages))

    for k in sorted(x for x in node.keys() if x != "__pages__"):
        pages.append(tree_to_mintlify(node[k], k))

    return {"group": group_name, "pages": pages}


def collect_pages_under(api_dir: Path, pkg: str, docs_root: Path) -> List[str]:
    """
    Return page paths relative to docs_root, WITHOUT .mdx suffix:
      api/<pkg>/...
    """
    base = api_dir / pkg
    files = glob.glob(str(base / "**" / "*.mdx"), recursive=True)

    out: List[str] = []
    for f in files:
        fp = Path(f)
        rel = fp.relative_to(docs_root)  # api/<pkg>/...
        out.append(rel.as_posix().removesuffix(".mdx"))
    return sorted(out)


def build_api_reference_tab_object(api_dir: Path, docs_root: Path) -> Dict[str, Any]:
    cli_pages = collect_pages_under(api_dir, "cli", docs_root)
    mellea_pages = collect_pages_under(api_dir, "mellea", docs_root)

    cli_tree = build_tree_from_paths(cli_pages)
    mellea_tree = build_tree_from_paths(mellea_pages)

    cli_nav = tree_to_mintlify(cli_tree, "cli")
    mellea_nav = tree_to_mintlify(mellea_tree, "mellea")

    # Top-level ONLY: mellea and cli (no nested mellea -> cli)
    return {
        "tab": NAV_TAB,
        "pages": [
            {"group": "mellea", "pages": mellea_nav["pages"]},
            {"group": "cli", "pages": cli_nav["pages"]},
        ],
    }


def build_and_merge_navigation(docs_json_path: Path, api_dir: Path, docs_root: Path) -> None:
    print("-" * 30, flush=True)
    print("🛠️ Building API Reference navigation and merging into docs.json...", flush=True)
    api_tab = build_api_reference_tab_object(api_dir, docs_root)
    merge_api_reference_into_docs_json(docs_json_path, api_tab)


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate MDX API docs, move to docs root, and merge nav into docs.json"
    )
    parser.add_argument("--docs-json", help="Path to docs.json to update (recommended for CI).", required=False)
    parser.add_argument(
        "--docs-root",
        help="Mintlify docs root (folder that contains docs.json, api/, snippets/, etc.). "
             "Defaults to parent directory of --docs-json.",
        required=False,
    )
    args = parser.parse_args()

    docs_json_path = find_docs_json(args.docs_json)
    docs_root = Path(args.docs_root).resolve() if args.docs_root else docs_json_path.parent.resolve()

    setup_env()

    # Generate MDX into staging
    for pkg in PACKAGES:
        run_mdxify_generation(pkg)

    # Restructure + rename init + metadata cleanup in staging
    reorganize_to_nested_structure()
    rename_init_files_to_parent()
    update_frontmatter_metadata()
    remove_empty_mdx_files()

    # Move staging api -> final docs root/api
    final_api_dir = move_api_to_docs_root(docs_root)

    # Merge nav based on final location
    build_and_merge_navigation(docs_json_path, final_api_dir, docs_root)

    # Cleanup env
    if "PYTHONPATH" in os.environ:
        del os.environ["PYTHONPATH"]

    print("-" * 30, flush=True)
    print("🎉 All tasks complete!", flush=True)


if __name__ == "__main__":
    main()
