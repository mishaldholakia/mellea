import os
import sys
import json
import glob
import re
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

# -----------------------------
# Configuration
# -----------------------------
# Repo root = directory that contains top-level "mellea/" and "cli/" folders
REPO_ROOT = Path(__file__).resolve().parents[0]

# Where to write MDX files (Mintlify docs root usually has docs/docs/; you can point docs_root there)
DOCS_ROOT = REPO_ROOT / "docs"  # adjust if your mintlify root differs
OUTPUT_BASE_DIR = DOCS_ROOT / "api"

# Only focus on top-level packages:
PACKAGES = ["mellea", "cli"]

NAV_ANCHOR = "API Reference"
NAV_OUTPUT_FILE = REPO_ROOT / "docs-generated.json"

# mdxify module name (python -m mdxify ...)
MDXIFY_MODULE = "mdxify"

# -----------------------------
# Helpers
# -----------------------------
def sh(cmd: List[str], *, env: Optional[Dict[str, str]] = None) -> None:
    print("   $ " + " ".join(cmd))
    subprocess.run(cmd, check=True, text=True, env=env)

def yaml_quote(value: str) -> str:
    """
    Quote a YAML scalar safely using double quotes.
    """
    if value is None:
        return '""'
    s = str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")
    return f'"{s}"'

def read_frontmatter(lines: List[str]) -> Tuple[Optional[Tuple[int, int]], Dict[str, str]]:
    """
    Returns ((start_idx, end_idx), kv) where end_idx is index of closing '---' line.
    If no frontmatter, returns (None, {}).
    """
    if not lines or lines[0].strip() != "---":
        return None, {}
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None, {}
    kv: Dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            k, v = line.split(":", 1)
            kv[k.strip()] = v.strip()
    return (0, end), kv

def write_frontmatter(lines: List[str], fm_range: Optional[Tuple[int, int]], new_kv: Dict[str, str]) -> List[str]:
    """
    Replace or add frontmatter. Preserves body exactly.
    """
    body_start = 0
    if fm_range:
        _, end = fm_range
        body_start = end + 1

    body = lines[body_start:]

    fm_lines = ["---"]
    # Only set these keys; keep others if they exist in original frontmatter
    # We'll rebuild from new_kv entirely (callers can merge if desired).
    for k, v in new_kv.items():
        fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")

    # Ensure a blank line after frontmatter for MDX readability
    return fm_lines + [""] + body

def find_first_h1(body_lines: List[str]) -> Optional[str]:
    h1_pattern = re.compile(r"^#\s+(.+?)\s*$")
    for line in body_lines:
        m = h1_pattern.match(line.strip())
        if m:
            return m.group(1).strip()
    return None

def find_description(body_lines: List[str]) -> Optional[str]:
    """
    Pick a safe description line:
    - after first H1
    - first non-empty plain-text line
    - skip code fences, headings, imports, etc.
    """
    h1_idx = None
    for i, line in enumerate(body_lines):
        if line.strip().startswith("# "):
            h1_idx = i
            break
    if h1_idx is None:
        return None

    for j in range(h1_idx + 1, len(body_lines)):
        s = body_lines[j].strip()
        if not s:
            continue
        if s.startswith("#"):
            break
        if s.startswith("```"):  # do not treat code fence as description
            continue
        if s.startswith("from ") or s.startswith("import "):  # avoid “description = import line”
            continue
        # avoid MDX JSX-looking lines
        if s.startswith("<") and s.endswith(">"):
            continue
        return s
    return None

# -----------------------------
# Step 1: Prepare dirs + env
# -----------------------------
def build_env() -> Dict[str, str]:
    env = dict(os.environ)
    # Ensure repo root is on PYTHONPATH so `import mellea` and `import cli` work in CI
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env

def ensure_dirs() -> None:
    OUTPUT_BASE_DIR.mkdir(parents=True, exist_ok=True)

# -----------------------------
# Step 2: Run mdxify
# -----------------------------
def run_mdxify_generation(package_name: str) -> None:
    out_dir = OUTPUT_BASE_DIR / package_name
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"➡️ Generating docs for: {package_name} -> {out_dir}")
    env = build_env()

    cmd = [
        sys.executable, "-m", MDXIFY_MODULE,
        package_name,
        "--output-dir", str(out_dir),
        "--root-module", package_name,
        "--all",
        "--no-update-nav",
        "--with-editable",  # IMPORTANT for CI/source checkouts
    ]

    try:
        sh(cmd, env=env)
        print(f"✅ mdxify ok for {package_name}")
    except subprocess.CalledProcessError as e:
        print(f"❌ mdxify failed for {package_name} (exit {e.returncode})")
        raise

# -----------------------------
# Step 3: Reorganize mdxify flat output into nested folders
# -----------------------------
def reorganize_to_nested_structure() -> None:
    print("📁 Reorganizing MDX files into nested structure...")

    all_mdx_files = glob.glob(str(OUTPUT_BASE_DIR / "**" / "*.mdx"), recursive=True)

    for old_path_str in all_mdx_files:
        old_path = Path(old_path_str)

        # expected: docs/api/<pkg>/<flatname>.mdx
        try:
            rel = old_path.relative_to(OUTPUT_BASE_DIR)
        except ValueError:
            continue

        if len(rel.parts) < 2:
            continue

        top_pkg = rel.parts[0]
        if top_pkg not in PACKAGES:
            continue

        filename = rel.parts[-1]
        stem = filename.removesuffix(".mdx")

        # mdxify naming: "<pkg>-a-b-c.mdx" for module path pkg.a.b.c
        prefix = f"{top_pkg}-"
        if not stem.startswith(prefix):
            continue

        remainder = stem[len(prefix):]
        if not remainder:
            # root module file, keep as <pkg>.mdx where it is
            continue

        module_parts = remainder.split("-")
        new_dir = OUTPUT_BASE_DIR / top_pkg / Path(*module_parts[:-1])
        new_path = new_dir / f"{module_parts[-1]}.mdx"

        if new_path.resolve() == old_path.resolve():
            continue

        new_dir.mkdir(parents=True, exist_ok=True)
        print(f"   Move {old_path} -> {new_path}")
        old_path.replace(new_path)

    print("✅ Reorg complete.")

# -----------------------------
# Step 3b: Rename __init__.mdx to folder-name.mdx (and dedupe)
# -----------------------------
def rename_init_files_to_parent() -> None:
    print("📛 Renaming __init__.mdx -> <folder>.mdx ...")

    init_files = glob.glob(str(OUTPUT_BASE_DIR / "**" / "__init__.mdx"), recursive=True)

    for old_path_str in init_files:
        old_path = Path(old_path_str)
        folder = old_path.parent.name
        new_path = old_path.parent / f"{folder}.mdx"

        if new_path.exists():
            # If the target exists already, remove __init__.mdx (keeps canonical file)
            print(f"   ⚠️ {new_path} exists; removing {old_path}")
            old_path.unlink()
            continue

        print(f"   Rename {old_path} -> {new_path}")
        old_path.rename(new_path)

    print("✅ __init__ rename complete.")

# -----------------------------
# Step 4: Update frontmatter safely (no body deletion)
# -----------------------------
def update_frontmatter_metadata() -> None:
    print("📝 Updating frontmatter safely (quoted YAML, keep body intact)...")

    mdx_files = glob.glob(str(OUTPUT_BASE_DIR / "**" / "*.mdx"), recursive=True)

    for path_str in mdx_files:
        path = Path(path_str)
        lines = path.read_text(encoding="utf-8").splitlines()

        fm_range, fm_kv = read_frontmatter(lines)

        body_start = (fm_range[1] + 1) if fm_range else 0
        body_lines = lines[body_start:]

        # Title: prefer existing frontmatter title if present, else first H1, else file stem
        raw_title = None
        if "title" in fm_kv and fm_kv["title"].strip():
            # strip quotes if present
            raw_title = fm_kv["title"].strip().strip('"').strip("'")
        else:
            h1 = find_first_h1(body_lines)
            raw_title = h1.strip("`") if h1 else path.stem

        raw_desc = find_description(body_lines)
        if not raw_desc:
            # fallback: keep existing description if present
            if "description" in fm_kv and fm_kv["description"].strip():
                raw_desc = fm_kv["description"].strip().strip('"').strip("'")

        new_fm: Dict[str, str] = {}
        new_fm["title"] = yaml_quote(raw_title)
        new_fm["sidebarTitle"] = yaml_quote(raw_title)
        if raw_desc:
            new_fm["description"] = yaml_quote(raw_desc)

        # Preserve any other existing frontmatter keys (except ones we set)
        for k, v in fm_kv.items():
            if k in ("title", "sidebarTitle", "description"):
                continue
            # Keep as-is (already YAML-ish)
            new_fm[k] = v

        new_lines = write_frontmatter(lines, fm_range, new_fm)
        path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    print("✅ Frontmatter update complete.")

# -----------------------------
# Step 4b: Remove empty MDX files (optional)
# -----------------------------
def remove_empty_mdx_files() -> None:
    print("🧹 Removing empty/no-content MDX files...")

    mdx_files = glob.glob(str(OUTPUT_BASE_DIR / "**" / "*.mdx"), recursive=True)
    removed = 0

    for path_str in mdx_files:
        path = Path(path_str)
        lines = path.read_text(encoding="utf-8").splitlines()

        fm_range, _ = read_frontmatter(lines)
        body_start = (fm_range[1] + 1) if fm_range else 0
        body = lines[body_start:]

        meaningful = False
        in_fence = False
        for line in body:
            s = line.strip()
            if s.startswith("```"):
                in_fence = not in_fence
                meaningful = True
                continue
            if not s:
                continue
            if s.startswith("#"):
                continue
            if s.startswith("<!--") and s.endswith("-->"):
                continue
            # Anything else counts
            meaningful = True
            break

        if not meaningful:
            print(f"   🗑️ Removing empty file: {path}")
            path.unlink()
            removed += 1

    print(f"✅ Removed {removed} empty files.")

# -----------------------------
# Step 5: Build navigation JSON (NO .mdx)
# -----------------------------
def build_navigation_structure() -> None:
    print("🛠️ Building navigation JSON (no .mdx in paths)...")

    all_mdx_files = glob.glob(str(OUTPUT_BASE_DIR / "**" / "*.mdx"), recursive=True)

    # We want a nested tree under:
    # api/<pkg>/<...>/<file>
    package_tree: Dict[str, Any] = {pkg: {} for pkg in PACKAGES}

    for full_path_str in all_mdx_files:
        full_path = Path(full_path_str)
        rel = full_path.relative_to(DOCS_ROOT)  # e.g. api/mellea/backends/types.mdx

        # enforce top-level packages only: api/mellea/... or api/cli/...
        if len(rel.parts) < 3:
            continue
        if rel.parts[0] != "api":
            continue

        pkg = rel.parts[1]
        if pkg not in PACKAGES:
            continue

        # path components after api/<pkg>/
        subparts = list(rel.parts[2:])
        if not subparts:
            continue

        # remove .mdx suffix from leaf
        leaf = subparts[-1]
        if leaf.endswith(".mdx"):
            subparts[-1] = leaf[:-4]

        # special: folder index file "<folder>/<folder>.mdx" should be treated as folder index
        # Here index means: if leaf == parent folder, store as __file of that node
        def insert(tree: Dict[str, Any], parts: List[str], page: str) -> None:
            if not parts:
                tree["__file"] = page
                return
            head = parts[0]
            tree.setdefault(head, {})
            insert(tree[head], parts[1:], page)

        # compute page string like "api/mellea/backends/types" (no .mdx)
        page_no_ext = "/".join(rel.parts).removesuffix(".mdx")

        if len(subparts) >= 2 and subparts[-1] == subparts[-2]:
            # treat as index file for that folder (drop the leaf)
            insert(package_tree[pkg], subparts[:-1], page_no_ext)
        else:
            insert(package_tree[pkg], subparts, page_no_ext)

    def to_mintlify(node: Dict[str, Any], name: str) -> Dict[str, Any]:
        pages: List[Any] = []
        if "__file" in node:
            pages.append(node["__file"])
        for k in sorted([x for x in node.keys() if x != "__file"]):
            pages.append(to_mintlify(node[k], k))
        return {"group": name, "pages": pages}

    final_pages = [to_mintlify(package_tree[pkg], pkg) for pkg in PACKAGES if package_tree[pkg]]

    nav = {"tab": NAV_ANCHOR, "pages": final_pages}

    NAV_OUTPUT_FILE.write_text(json.dumps(nav, indent=2) + "\n", encoding="utf-8")
    print(f"📄 Wrote: {NAV_OUTPUT_FILE}")

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    print("-" * 30)
    print(f"Repo root: {REPO_ROOT}")
    print(f"Docs root: {DOCS_ROOT}")
    print(f"Output:    {OUTPUT_BASE_DIR}")
    print("-" * 30)

    ensure_dirs()

    # Generate
    for pkg in PACKAGES:
        run_mdxify_generation(pkg)

    reorganize_to_nested_structure()
    rename_init_files_to_parent()
    update_frontmatter_metadata()
    remove_empty_mdx_files()
    build_navigation_structure()

    print("-" * 30)
    print("🎉 All tasks complete!")
