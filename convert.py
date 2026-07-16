"""PMX -> FBX conversion orchestrator.

Reads a PMX file, resolves/copies textures, and writes an FBX 7.4.0 binary
file via the fbx_writer module. Textures are embedded into the FBX binary
(Video.Content) so UE4 imports them automatically without external files.
Also writes a small sidecar JSON describing data that cannot be represented
in FBX (IK chains, rigid bodies, joints, display frames, non-vertex morphs,
SDEF parameters) so the user can inspect what was preserved and what was
dropped.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Dict, List, Optional, Tuple

from fbx_writer import ConversionOptions, write_fbx
from pmx_reader import PMXModel, read_pmx
from texture_utils import copy_texture, find_all_textures, read_texture_bytes, resolve_texture_path


def convert_pmx_to_fbx(
    pmx_path: str,
    out_path: Optional[str] = None,
    options: Optional[ConversionOptions] = None,
    log=print,
) -> str:
    """Convert a .pmx file to an .fbx file.

    Args:
      pmx_path: path to the source .pmx file.
      out_path: path to the output .fbx file. If None, derived from pmx_path
        by replacing the extension with .fbx in the same directory.
      options: ConversionOptions. If None, defaults are used (scale=8.0).
      log: callable for progress messages.

    Returns:
      The absolute path of the written .fbx file.
    """
    if options is None:
        options = ConversionOptions()

    pmx_path = os.path.abspath(pmx_path)
    if not os.path.isfile(pmx_path):
        raise FileNotFoundError(f"PMX file not found: {pmx_path}")

    if out_path is None:
        out_path = os.path.splitext(pmx_path)[0] + ".fbx"
    out_path = os.path.abspath(out_path)
    out_dir = os.path.dirname(out_path)
    os.makedirs(out_dir, exist_ok=True)

    # UE5.x Interchange FBX parser uses fopen(char*) on Windows, which encodes
    # paths using the system ANSI code page (CP932 on Japanese Windows,
    # CP936/GBK on Chinese Windows). CJK characters that don't exist in the
    # active code page cause fopen() to fail with "cannot load FBX file".
    # If the output path contains non-ASCII characters, automatically redirect
    # to an ASCII-safe filename in the same directory.
    original_out_path = out_path
    try:
        out_path.encode("ascii")
    except UnicodeEncodeError:
        # Generate an ASCII-safe output path in the same directory
        out_dir = os.path.dirname(original_out_path)
        # Use a fixed ASCII name to avoid any encoding issues
        ascii_name = "pmx2fbx_output.fbx"
        ascii_path = os.path.join(out_dir, ascii_name)
        # If the directory itself contains non-ASCII chars, fall back to a
        # temp directory near the PMX file's drive root
        try:
            ascii_path.encode("ascii")
        except UnicodeEncodeError:
            import tempfile
            # Use a temp dir on the same drive as the PMX file (Windows)
            # or /tmp on Linux/Mac
            drive = os.path.splitdrive(pmx_path)[0]
            temp_base = os.path.join(drive + os.sep, "pmx2fbx_temp") if drive else tempfile.gettempdir()
            os.makedirs(temp_base, exist_ok=True)
            ascii_path = os.path.join(temp_base, ascii_name)
        out_path = os.path.abspath(ascii_path)
        log("")
        log("⚠ 输出路径包含非ASCII字符，已自动切换到英文路径以兼容 UE5.x:")
        log(f"  原路径: {original_out_path}")
        log(f"  新路径: {out_path}")
        log("  (UE5.x Interchange 的 fopen() 在日文/中文 Windows 上无法处理 CJK 路径)")
        log("")

    log(f"Reading PMX: {pmx_path}")
    model = read_pmx(pmx_path)
    log(
        f"  parsed: PMX v{model.version:.1f}, "
        f"{len(model.vertices)} verts, {len(model.faces)//3} tris, "
        f"{len(model.materials)} mats, {len(model.bones)} bones, "
        f"{len(model.morphs)} morphs, {len(model.textures)} textures, "
        f"{len(model.rigid_bodies)} rigid bodies, {len(model.joints)} joints"
    )

    # Resolve and copy textures
    log("Resolving textures...")
    material_refs: List[Tuple[int, int, int]] = []
    for mat in model.materials:
        toon_tex = -1 if mat.toon_shared else mat.toon_index
        material_refs.append((mat.texture_index, mat.sphere_index, toon_tex))

    resolved = find_all_textures(pmx_path, model.textures, material_refs, log=log)

    # Read texture bytes for embedding into the FBX binary. This is independent
    # of copy_textures: even if we also copy files next to the FBX, the
    # embedded bytes ensure UE4 finds the textures without external files.
    texture_bytes: Dict[int, bytes] = {}
    if options.embed_textures:
        log("Reading texture bytes for embedding...")
        for ti, _orig, abs_path in resolved:
            if abs_path is None:
                continue
            data = read_texture_bytes(abs_path)
            if data is not None:
                texture_bytes[ti] = data
        log(f"  read {len(texture_bytes)} texture(s) for embedding")

    if options.copy_textures:
        log(f"Copying textures next to FBX (subdir: {options.texture_subdir}/)...")
        name_map: dict = {}
        copied_count = 0
        for ti, orig, abs_path in resolved:
            if abs_path is None:
                continue
            rel = copy_texture(
                abs_path,
                out_dir,
                subdir=options.texture_subdir,
                name_map=name_map,
            )
            # Update the texture table so the FBX writer emits the new relative
            # path (e.g. "textures/foo.png"). The writer uses the table entry
            # verbatim as RelativeFilename.
            model.textures[ti] = rel
            copied_count += 1
        log(f"  copied {copied_count} texture file(s)")

    # Write FBX
    log(f"Writing FBX: {out_path}")
    write_fbx(model, out_path, options=options, texture_bytes=texture_bytes, log=log)

    # Write sidecar JSON describing non-transferrable data
    sidecar_path = os.path.splitext(out_path)[0] + ".pmx_meta.json"
    try:
        _write_sidecar(model, sidecar_path)
        log(f"  wrote metadata sidecar: {sidecar_path}")
    except Exception as e:
        log(f"  WARN: could not write sidecar: {e}")

    log("Done.")
    return out_path


def _write_sidecar(model: PMXModel, path: str) -> None:
    """Write a JSON sidecar describing data that FBX cannot represent."""
    data = {
        "model_name_jp": model.name_jp,
        "model_name_en": model.name_en,
        "comment_jp": model.comment_jp[:500],
        "comment_en": model.comment_en[:500],
        "pmx_version": model.version,
        "stats": {
            "vertices": len(model.vertices),
            "triangles": len(model.faces) // 3,
            "materials": len(model.materials),
            "bones": len(model.bones),
            "morphs": len(model.morphs),
            "textures": len(model.textures),
            "rigid_bodies": len(model.rigid_bodies),
            "joints": len(model.joints),
            "display_frames": len(model.display_frames),
        },
        "weight_types": {
            "BDEF1": sum(1 for v in model.vertices if v.weight_type == 0),
            "BDEF2": sum(1 for v in model.vertices if v.weight_type == 1),
            "BDEF4": sum(1 for v in model.vertices if v.weight_type == 2),
            "SDEF": sum(1 for v in model.vertices if v.weight_type == 3),
            "QDEF": sum(1 for v in model.vertices if v.weight_type == 4),
        },
        "ik_bones": [
            {
                "name": b.name_en or b.name_jp,
                "target_bone_index": b.ik_target_bone,
                "loop_count": b.ik_loop_count,
                "limit_radian": b.ik_limit_radian,
                "links": [
                    {
                        "bone_index": lk.bone_index,
                        "has_limits": lk.has_limits,
                        "limit_min": list(lk.limit_min),
                        "limit_max": list(lk.limit_max),
                    }
                    for lk in b.ik_links
                ],
            }
            for b in model.bones if b.is_ik
        ],
        "rigid_bodies": [
            {
                "name": rb.name_en or rb.name_jp,
                "bone_index": rb.bone_index,
                "shape": rb.shape,
                "size": list(rb.size),
                "position": list(rb.position),
                "rotation": list(rb.rotation),
                "mass": rb.mass,
                "physics_mode": rb.physics_mode,
            }
            for rb in model.rigid_bodies
        ],
        "joints": [
            {
                "name": j.name_en or j.name_jp,
                "type": j.joint_type,
                "rigid_a": j.rigid_a,
                "rigid_b": j.rigid_b,
                "position": list(j.position),
                "rotation": list(j.rotation),
            }
            for j in model.joints
        ],
        "display_frames": [
            {
                "name": df.name_en or df.name_jp,
                "special": df.special,
                "entries": df.entries,
            }
            for df in model.display_frames
        ],
        "morphs_summary": [
            {
                "name": m.name_en or m.name_jp,
                "type": m.morph_type,
                "type_name": _morph_type_name(m.morph_type),
                "offset_count": len(m.offsets),
                "transferred_to_fbx": m.morph_type == 1,  # only vertex morphs
            }
            for m in model.morphs
        ],
        "notes": [
            "SDEF weights approximated as BDEF2 (linear).",
            "QDEF weights approximated as BDEF4 (linear).",
            "IK constraints are not represented in FBX; only the bone "
            "hierarchy is preserved. UE4 has its own IK system.",
            "Rigid bodies and joints (Bullet physics) are not represented "
            "in FBX; UE4 uses its own physics.",
            "Display frames are not represented in FBX; recorded here for "
            "reference only.",
            "Only vertex morphs (type 1) become FBX blend shapes / UE morph "
            "targets. Bone, UV, material, group, flip and impulse morphs "
            "have no FBX equivalent and are recorded here for reference.",
            "Coordinate system converted: PMX (Y-up, X-right, Z-forward) "
            "-> UE (Z-up, Y-right, X-forward) via cyclic permutation.",
            "UV V axis flipped (PMX top-left origin -> UE bottom-left origin).",
            "Triangle winding flipped (PMX CW -> FBX CCW).",
            "Scale factor: 1 PMX unit = 8 cm (default; configurable).",
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def _morph_type_name(t: int) -> str:
    return {
        0: "Group",
        1: "Vertex",
        2: "Bone",
        3: "UV",
        4: "UV1",
        5: "UV2",
        6: "UV3",
        7: "UV4",
        8: "Material",
        9: "Flip",
        10: "Impulse",
    }.get(t, f"Unknown({t})")
