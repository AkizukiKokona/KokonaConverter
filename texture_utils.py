"""Texture path resolution for PMX -> FBX conversion.

PMX texture paths can be:
  - Relative to the .pmx file:  tex/diffuse.png
  - Absolute:                   C:\\models\\foo\\tex\\bar.png
  - With backslashes or forward slashes mixed
  - Contain Chinese / Japanese characters (the .pmx file's text encoding was
    respected at parse time, so by here they are proper Python str)

In practice MMD content typically stores paths like:
  - tex\\foo.png        (a 'tex' subfolder next to the .pmx)
  - .\\tex\\foo.png
  - foo.png
  - toon01.bmp .. toon10.bmp   (shared toon textures, NOT in the texture table)

This module resolves a texture path against several candidate locations and
(optionally) copies the found file into a subfolder next to the output FBX,
producing a clean relative path that UE4 can find at FBX import time.
"""

from __future__ import annotations

import os
import shutil
from typing import List, Optional, Tuple


# Shared toon texture file names that are built into MMD and never appear in
# the texture table. Materials reference them via toon_shared=True and an
# index 0..9. They are not real files on disk and we leave them alone.
SHARED_TOON_NAMES = [f"toon{i:02d}.bmp" for i in range(1, 11)]


def _normalize_separators(p: str) -> str:
    """Normalize path separators to the OS-native form."""
    if os.sep == "\\":
        return p.replace("/", "\\")
    return p.replace("\\", "/")


def _case_insensitive_exists(dirpath: str, name: str) -> Optional[str]:
    """Look for a file in `dirpath` matching `name` case-insensitively.
    Returns the actual path if found, else None. Useful because MMD content
    is often authored on Windows (case-insensitive) but might be unpacked on
    case-sensitive file systems.
    """
    try:
        entries = os.listdir(dirpath)
    except OSError:
        return None
    lowered = name.lower()
    for entry in entries:
        if entry.lower() == lowered:
            return os.path.join(dirpath, entry)
    return None


def _candidate_locations(pmx_dir: str, tex_path: str) -> List[str]:
    """Build a list of candidate absolute paths to try for a texture."""
    # Normalize separators first
    tex_norm = tex_path.replace("\\", "/")
    candidates: List[str] = []

    # 1. As-is if absolute
    if os.path.isabs(tex_norm):
        candidates.append(tex_norm)

    # 2. Relative to the .pmx directory
    candidates.append(os.path.join(pmx_dir, tex_norm))

    # 3. basename next to .pmx
    base = os.path.basename(tex_norm)
    if base:
        candidates.append(os.path.join(pmx_dir, base))

    # 4. In a 'tex' subfolder of the .pmx directory (case-insensitive)
    if base:
        for sub in ("tex", "Tex", "TEX", "texture", "textures", "Texture"):
            candidates.append(os.path.join(pmx_dir, sub, base))
        # Also walk one level deep looking for the basename in any subfolder.
        try:
            for entry in os.listdir(pmx_dir):
                full = os.path.join(pmx_dir, entry)
                if os.path.isdir(full):
                    candidates.append(os.path.join(full, base))
        except OSError:
            pass

    return candidates


def resolve_texture_path(pmx_path: str, tex_path: str) -> Optional[str]:
    """Find a texture file on disk.

    Args:
      pmx_path: absolute path to the source .pmx file.
      tex_path: texture path string as stored in the PMX texture table.

    Returns:
      Absolute path to the found file, or None if not found.
    """
    if not tex_path:
        return None

    pmx_dir = os.path.dirname(os.path.abspath(pmx_path))
    candidates = _candidate_locations(pmx_dir, tex_path)

    for cand in candidates:
        if os.path.isfile(cand):
            return os.path.abspath(cand)

    # Case-insensitive fallback: for each candidate directory, look for a
    # case-insensitive match on the basename.
    for cand in candidates:
        cdir = os.path.dirname(cand)
        cname = os.path.basename(cand)
        if not cname:
            continue
        hit = _case_insensitive_exists(cdir, cname)
        if hit:
            return os.path.abspath(hit)

    return None


def _safe_basename(name: str) -> str:
    """Sanitize a file basename for output. Keep unicode, drop path separators
    and characters illegal on Windows."""
    base = os.path.basename(name.replace("\\", "/"))
    if not base:
        base = "texture"
    # Replace illegal Windows filename characters
    cleaned = []
    for ch in base:
        if ch in '<>:"/\\|?*':
            cleaned.append("_")
        elif ord(ch) < 32:
            continue
        else:
            cleaned.append(ch)
    out = "".join(cleaned).strip()
    return out if out else "texture"


def copy_texture(
    src_path: str,
    out_dir: str,
    subdir: str = "textures",
    name_map: Optional[dict] = None,
) -> str:
    """Copy a texture file into `<out_dir>/<subdir>/`. If a file with the same
    basename already exists there (from a previous copy or another texture),
    a numeric suffix is added to avoid overwriting.

    Args:
      src_path: source texture file (must exist).
      out_dir: output directory (the FBX's directory).
      subdir: subdirectory name to create under out_dir.
      name_map: optional dict mapping basename -> already-used path; used to
        share renames across multiple calls.

    Returns:
      The relative path (subdir/basename) of the copied file, suitable for
      use as the FBX RelativeFilename.
    """
    target_dir = os.path.join(out_dir, subdir)
    os.makedirs(target_dir, exist_ok=True)

    src_base = _safe_basename(os.path.basename(src_path))
    if name_map is None:
        name_map = {}

    # If we already copied this exact source, reuse the previous target.
    if src_path in name_map:
        return name_map[src_path]

    # Find a non-colliding target name
    target_name = src_base
    target_full = os.path.join(target_dir, target_name)
    counter = 1
    stem, ext = os.path.splitext(src_base)
    while os.path.exists(target_full):
        target_name = f"{stem}_{counter}{ext}"
        target_full = os.path.join(target_dir, target_name)
        counter += 1

    try:
        shutil.copy2(src_path, target_full)
    except OSError:
        # If copy fails (e.g. permission), leave a placeholder; the FBX will
        # still reference the relative path and the user can drop the file in.
        pass

    rel = f"{subdir}/{target_name}"
    name_map[src_path] = rel
    return rel


def convert_texture_to_png_if_bmp(src_path: str) -> str:
    """Return the path of the texture, converting BMP to PNG if necessary.

    UE4 can import BMP, but some MMD textures are weirdly-paletted BMPs that
    UE4 mis-reads. For now we do NOT convert (UE4 handles most BMPs fine and
    conversion would require PIL which we don't want as a dependency). This
    function is a stub kept for future extension.
    """
    return src_path


def find_all_textures(
    pmx_path: str,
    texture_table: List[str],
    material_refs: List[Tuple[int, int, int]],
    log=print,
) -> List[Tuple[int, str, Optional[str]]]:
    """Resolve all textures referenced by the model.

    Args:
      pmx_path: path to the .pmx file.
      texture_table: list of texture path strings from PMX.
      material_refs: list of (texture_index, sphere_index, toon_texture_index)
        per material. -1 means "not used". Used to filter which textures we
        actually need to copy.
      log: callable for progress messages.

    Returns:
      List of (texture_index, original_path, resolved_absolute_path_or_None).
    """
    needed_indices: set = set()
    for tex_idx, sph_idx, toon_idx in material_refs:
        if tex_idx >= 0:
            needed_indices.add(tex_idx)
        if sph_idx >= 0:
            needed_indices.add(sph_idx)
        if toon_idx >= 0:
            needed_indices.add(toon_idx)

    results: List[Tuple[int, str, Optional[str]]] = []
    missing: List[int] = []
    for ti in sorted(needed_indices):
        if ti < 0 or ti >= len(texture_table):
            continue
        orig = texture_table[ti]
        resolved = resolve_texture_path(pmx_path, orig)
        if resolved is None:
            missing.append(ti)
            log(f"  WARN: texture #{ti} not found: {orig!r}")
        results.append((ti, orig, resolved))

    if missing:
        log(f"  {len(missing)} texture(s) could not be located; FBX will reference them by name only")
    return results
