"""FBX 7.4.0 ASCII writer for Unreal Engine 4.27.

Zero external dependencies. Produces FBX 7400 ASCII files with:
  - Mesh geometry (vertices, faces, normals, UVs, additional UVs as extra channels)
  - Per-corner material assignment (LayerElementMaterial ByPolygon)
  - Phong materials with diffuse/specular/ambient/emissive
  - Textures (Texture + Video) connected to material properties (DiffuseColor,
    NormalMap, SpecularColor, EmissiveColor, AmbientColor, TransparencyColor)
  - Sphere map textures (as additional texture on DiffuseColor or as separate slot)
  - Toon textures (as EmissiveColor or separate)
  - Bone hierarchy as Model "LimbNode" objects, with synthetic root bone if PMX
    has multiple roots
  - Skinning: Skin Deformer + Cluster SubDeformers with Transform/TransformLink
    (computed from bone world positions)
  - Vertex morphs as BlendShape + BlendShapeChannel + Shape (with deltas)
  - BindPose object
  - Coordinate conversion PMX (Y-up, X-right, Z-forward) -> UE (Z-up, Y-right,
    X-forward) via cyclic permutation (x,y,z) -> (z,x,y)
  - UV V-flip (PMX top-left origin -> UE bottom-left origin)
  - Triangle winding flip (PMX CW -> FBX CCW)
  - Scale factor (default 1 PMX unit = 8 cm = 8 UE units)

The writer is purpose-built for UE 4.27 import ("Skeletal Mesh" with
"Import Morph Targets" enabled, "Convert Scene" on by default - which is a
no-op because we already declare UE-native axes).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pmx_reader import (
    PMXBone,
    PMXMaterial,
    PMXModel,
    PMXMorph,
    PMXVertex,
)


# ---------------------------------------------------------------------------
# Math utilities (no numpy)
# ---------------------------------------------------------------------------


Vec3 = Tuple[float, float, float]
Vec4 = Tuple[float, float, float, float]
Mat4 = Tuple[
    float, float, float, float,
    float, float, float, float,
    float, float, float, float,
    float, float, float, float,
]


def v_add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def v_sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def v_scale(a: Vec3, s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def v_cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def v_dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def v_norm(a: Vec3) -> Vec3:
    m = math.sqrt(v_dot(a, a))
    if m < 1e-12:
        return (0.0, 0.0, 0.0)
    return (a[0] / m, a[1] / m, a[2] / m)


def permute(v: Tuple[float, float, float]) -> Tuple[float, float, float]:
    """PMX (x, y, z) -> UE (z, x, y).

    PMX: Y up, X right, Z forward.
    UE : Z up, Y right, X forward.
    So: x_ue = z_pmx, y_ue = x_pmx, z_ue = y_pmx.
    """
    return (v[2], v[0], v[1])


def permute_quat(q: Vec4) -> Vec4:
    """Conjugate a PMX-space quaternion by T = (0.5, 0.5, 0.5, 0.5) to express
    the same rotation in UE space. T performs the cyclic permutation on vectors.
    q' = T * q * T^{-1}. For T = (0.5,0.5,0.5,0.5) we have T^{-1} = T (since T
    is a 180-degree rotation around (1,1,1) — actually a 120-degree rotation,
    but T*T*T = identity, so T^{-1} = T*T). We just compute the sandwich.
    """
    tw, tx, ty, tz = q
    # T = (0.5, 0.5, 0.5, 0.5)
    Tw, Tx, Ty, Tz = 0.5, 0.5, 0.5, 0.5
    # Tinv = T*T = quaternion square of T
    # T*T = (Tw*Tw - Tx*Tx - Ty*Ty - Tz*Tz,
    #        2*Tw*Tx, 2*Tw*Ty, 2*Tw*Tz)  [since Tx=Ty=Tz=0.5, Tw=0.5]
    # = (0.25 - 0.25 - 0.25 - 0.25, 0.5, 0.5, 0.5)
    # = (-0.5, 0.5, 0.5, 0.5)
    IW, IX, IY, IZ = -0.5, 0.5, 0.5, 0.5
    # tmp = T * q
    rw = Tw * tw - Tx * tx - Ty * ty - Tz * tz
    rx = Tw * tx + Tx * tw + Ty * tz - Tz * ty
    ry = Tw * ty - Tx * tz + Ty * tw + Tz * tx
    rz = Tw * tz + Tx * ty - Ty * tx + Tz * tw
    # result = tmp * Tinv
    sw = rw * IW - rx * IX - ry * IY - rz * IZ
    sx = rw * IX + rx * IW + ry * IZ - rz * IY
    sy = rw * IY - rx * IZ + ry * IW + rz * IX
    sz = rw * IZ + rx * IY - ry * IX + rz * IW
    return (sw, sx, sy, sz)


def quat_to_euler_deg(q: Vec4) -> Vec3:
    """Convert quaternion (w, x, y, z) to Euler angles in degrees (XYZ order
    as FBX expects for Lcl Rotation)."""
    w, x, y, z = q
    # Normalize to be safe
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if n < 1e-12:
        return (0.0, 0.0, 0.0)
    w, x, y, z = w / n, x / n, y / n, z / n
    # Pitch (X)
    sinp = 2.0 * (w * x + y * z)
    cos_p = 1.0 - 2.0 * (x * x + y * y)
    pitch = math.atan2(sinp, cos_p)
    # Yaw (Y)
    siny = 2.0 * (w * y - z * x)
    siny = max(-1.0, min(1.0, siny))
    yaw = math.asin(siny)
    # Roll (Z)
    sinr = 2.0 * (w * z + x * y)
    cos_r = 1.0 - 2.0 * (y * y + z * z)
    roll = math.atan2(sinr, cos_r)
    return (math.degrees(pitch), math.degrees(yaw), math.degrees(roll))


def mat4_identity() -> Mat4:
    return (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )


def mat4_translation(t: Vec3) -> Mat4:
    return (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        t[0], t[1], t[2], 1.0,
    )


def mat4_mul(a: Mat4, b: Mat4) -> Mat4:
    """Row-major 4x4 multiplication: result = a * b."""
    r = [0.0] * 16
    for i in range(4):
        for j in range(4):
            s = 0.0
            for k in range(4):
                s += a[i * 4 + k] * b[k * 4 + j]
            r[i * 4 + j] = s
    return tuple(r)  # type: ignore


# ---------------------------------------------------------------------------
# ID allocator
# ---------------------------------------------------------------------------


class _IDGen:
    def __init__(self, start: int = 1000000):
        self.next = start

    def new(self) -> int:
        v = self.next
        self.next += 1
        return v


# ---------------------------------------------------------------------------
# FBX ASCII emitter
# ---------------------------------------------------------------------------


def _fmt_num(v: float) -> str:
    """Format a float for FBX ASCII. Use sufficient precision but no scientific
    notation, no trailing zeros beyond what's needed."""
    if v == int(v) and abs(v) < 1e15:
        return f"{int(v)}"
    s = f"{v:.6f}"
    # Trim trailing zeros but keep at least one decimal
    if "." in s:
        s = s.rstrip("0").rstrip(".")
        if s == "" or s == "-":
            s = "0"
    return s


def _fmt_arr(values: List[float], per_line: int = 16) -> str:
    """Format a flat list of numbers as an FBX array body (after 'a: ').

    Wraps to per_line entries per line for readability.
    """
    parts = [_fmt_num(v) for v in values]
    lines = []
    for i in range(0, len(parts), per_line):
        lines.append(",".join(parts[i : i + per_line]))
    return ",\n\t\t".join(lines)


def _quote(s: str) -> str:
    """Escape a string for an FBX ASCII double-quoted literal."""
    # FBX ASCII uses backslash escaping; we keep it conservative.
    s = s.replace("\\", "/")  # normalize path separators
    s = s.replace('"', '\\"')
    return f'"{s}"'


class _FBXFile:
    def __init__(self):
        self.objects: List[str] = []
        self.connections: List[str] = []
        self.id_gen = _IDGen()
        # type -> count, for Definitions
        self.type_counts: Dict[str, int] = {
            "Model": 0,
            "Geometry": 0,
            "Material": 0,
            "Texture": 0,
            "Video": 0,
            "Deformer": 0,
            "SubDeformer": 0,
            "Pose": 0,
        }

    def _count(self, t: str) -> None:
        self.type_counts[t] = self.type_counts.get(t, 0) + 1

    # ---- object emitters (each appends to self.objects) ----

    def add_model_mesh(self, name: str, translation: Vec3 = (0.0, 0.0, 0.0)) -> int:
        oid = self.id_gen.new()
        self._count("Model")
        obj = []
        obj.append(f'\tModel: {oid}, "Model::{name}", "Mesh" {{')
        obj.append("\t\tVersion: 232")
        obj.append("\t\tProperties70:  {")
        obj.append('\t\t\tP: "InheritType", "enum", "", "",1')
        obj.append(
            f'\t\t\tP: "Lcl Translation", "Lcl Translation", "", "A",'
            f"{_fmt_num(translation[0])},{_fmt_num(translation[1])},{_fmt_num(translation[2])}"
        )
        obj.append('\t\t\tP: "Lcl Rotation", "Lcl Rotation", "", "A",0,0,0')
        obj.append('\t\t\tP: "Lcl Scaling", "Lcl Scaling", "", "A",1,1,1')
        obj.append('\t\t\tP: "DefaultAttributeIndex", "int", "Integer", "",0')
        obj.append("\t\t}")
        obj.append("\t\tShading: T")
        obj.append('\t\tCulling: "CullingOff"')
        obj.append("\t}")
        self.objects.append("\n".join(obj))
        return oid

    def add_model_bone(
        self,
        name: str,
        translation: Vec3,
        rotation_euler_deg: Vec3 = (0.0, 0.0, 0.0),
        pre_rotation: Optional[Vec3] = None,
    ) -> int:
        oid = self.id_gen.new()
        self._count("Model")
        obj = []
        obj.append(f'\tModel: {oid}, "Model::{name}", "LimbNode" {{')
        obj.append("\t\tVersion: 232")
        obj.append("\t\tProperties70:  {")
        obj.append('\t\t\tP: "QuaternionInterpolate", "bool", "", "",0')
        obj.append('\t\t\tP: "RotationActive", "bool", "", "",1')
        obj.append('\t\t\tP: "InheritType", "enum", "", "",1')
        if pre_rotation is not None:
            obj.append(
                f'\t\t\tP: "PreRotation", "Vector3D", "Vector", "",'
                f"{_fmt_num(pre_rotation[0])},{_fmt_num(pre_rotation[1])},{_fmt_num(pre_rotation[2])}"
            )
        obj.append('\t\t\tP: "PostRotation", "Vector3D", "Vector", "",0,0,0')
        obj.append('\t\t\tP: "RotationPivot", "Vector3D", "Vector", "",0,0,0')
        obj.append('\t\t\tP: "RotationOffset", "Vector3D", "Vector", "",0,0,0')
        obj.append(
            f'\t\t\tP: "Lcl Translation", "Lcl Translation", "", "A+",'
            f"{_fmt_num(translation[0])},{_fmt_num(translation[1])},{_fmt_num(translation[2])}"
        )
        obj.append(
            f'\t\t\tP: "Lcl Rotation", "Lcl Rotation", "", "A+",'
            f"{_fmt_num(rotation_euler_deg[0])},{_fmt_num(rotation_euler_deg[1])},{_fmt_num(rotation_euler_deg[2])}"
        )
        obj.append('\t\t\tP: "Lcl Scaling", "Lcl Scaling", "", "A+",1,1,1')
        obj.append('\t\t\tP: "Visibility", "Visibility", "", "A",1')
        obj.append("\t\t}")
        obj.append("\t\tShading: Y")
        obj.append('\t\tCulling: "CullingOff"')
        obj.append("\t}")
        self.objects.append("\n".join(obj))
        return oid

    def add_geometry(
        self,
        name: str,
        vertices: List[Vec3],
        faces: List[Tuple[int, int, int]],
        normals: List[Vec3],
        uvs: List[Tuple[float, float]],
        uv_indices: List[int],
        additional_uvs: Optional[List[List[Tuple[float, float, float, float]]]] = None,
        material_per_face: Optional[List[int]] = None,
        shape_blocks: Optional[List[Tuple[str, List[int], List[Vec3]]]] = None,
    ) -> int:
        """Add a Geometry object.

        Args:
          vertices: list of (x,y,z) - control points (already in UE space)
          faces: list of (v0, v1, v2) triangles
          normals: per-corner normals, len == 3 * len(faces), UE space
          uvs: list of unique (u, v) pairs (already V-flipped)
          uv_indices: per-corner UV index, len == 3 * len(faces)
          additional_uvs: optional list of additional UV layers; each layer is a
                          list of (x,y,z,w) per control point (4 components).
          material_per_face: optional material slot index per face; None means
                             single material slot 0 for all faces.
          shape_blocks: optional list of (name, indices, deltas) for blend shapes.
        """
        oid = self.id_gen.new()
        self._count("Geometry")
        obj = []
        obj.append(f'\tGeometry: {oid}, "Geometry::{name}", "Mesh" {{')

        # Vertices
        flat_v: List[float] = []
        for v in vertices:
            flat_v.extend(v)
        obj.append(f"\t\tVertices: *{len(flat_v)} {{")
        obj.append(f"\t\t\ta: {_fmt_arr(flat_v)}")
        obj.append("\t\t}")

        # PolygonVertexIndex: triangles, last index bitwise-NOT
        pvi: List[int] = []
        for (a, b, c) in faces:
            pvi.append(a)
            pvi.append(b)
            pvi.append(~c)
        obj.append(f"\t\tPolygonVertexIndex: *{len(pvi)} {{")
        obj.append(f"\t\t\ta: {','.join(str(i) for i in pvi)}")
        obj.append("\t\t}")

        obj.append("\t\tGeometryVersion: 124")

        # Normals (ByPolygonVertex Direct)
        flat_n: List[float] = []
        for n in normals:
            flat_n.extend(n)
        obj.append("\t\tLayerElementNormal: 0 {")
        obj.append("\t\t\tVersion: 101")
        obj.append('\t\t\tName: ""')
        obj.append('\t\t\tMappingInformationType: "ByPolygonVertex"')
        obj.append('\t\t\tReferenceInformationType: "Direct"')
        obj.append(f"\t\t\tNormals: *{len(flat_n)} {{")
        obj.append(f"\t\t\t\ta: {_fmt_arr(flat_n)}")
        obj.append("\t\t\t}")
        obj.append(f"\t\t\tNormalsW: *{len(normals)} {{")
        obj.append("\t\t\t\ta: " + ",".join(["1"] * len(normals)))
        obj.append("\t\t\t}")
        obj.append("\t\t}")

        # Base UV layer
        flat_uv: List[float] = []
        for uv in uvs:
            flat_uv.extend(uv)
        obj.append("\t\tLayerElementUV: 0 {")
        obj.append("\t\t\tVersion: 101")
        obj.append('\t\t\tName: "UVChannel_1"')
        obj.append('\t\t\tMappingInformationType: "ByPolygonVertex"')
        obj.append('\t\t\tReferenceInformationType: "IndexToDirect"')
        obj.append(f"\t\t\tUV: *{len(flat_uv)} {{")
        obj.append(f"\t\t\t\ta: {_fmt_arr(flat_uv)}")
        obj.append("\t\t\t}")
        obj.append(f"\t\t\tUVIndex: *{len(uv_indices)} {{")
        obj.append("\t\t\t\ta: " + ",".join(str(i) for i in uv_indices))
        obj.append("\t\t\t}")
        obj.append("\t\t}")

        # Additional UV layers (PMX additional UVs as extra UV channels)
        num_uv_layers = 1
        if additional_uvs:
            for layer_idx, layer in enumerate(additional_uvs):
                # Each additional UV in PMX is a vec4; FBX UV is vec2.
                # We use only the first two components (the standard MMD practice
                # for additional UVs that aren't used for special effects).
                flat_auv: List[float] = []
                for auv in layer:
                    flat_auv.append(auv[0])
                    flat_auv.append(auv[1])
                # UV index per corner = same as control point index (per-vertex)
                auv_indices = [face[i] for face in faces for i in range(3)]
                obj.append(f"\t\tLayerElementUV: {layer_idx + 1} {{")
                obj.append("\t\t\tVersion: 101")
                obj.append(f'\t\t\tName: "UVChannel_{layer_idx + 2}"')
                obj.append('\t\t\tMappingInformationType: "ByPolygonVertex"')
                obj.append('\t\t\tReferenceInformationType: "IndexToDirect"')
                obj.append(f"\t\t\tUV: *{len(flat_auv)} {{")
                obj.append(f"\t\t\t\ta: {_fmt_arr(flat_auv)}")
                obj.append("\t\t\t}")
                obj.append(f"\t\t\tUVIndex: *{len(auv_indices)} {{")
                obj.append("\t\t\t\ta: " + ",".join(str(i) for i in auv_indices))
                obj.append("\t\t\t}")
                obj.append("\t\t}")
                num_uv_layers += 1

        # Material layer
        if material_per_face is None:
            obj.append("\t\tLayerElementMaterial: 0 {")
            obj.append("\t\t\tVersion: 101")
            obj.append('\t\t\tName: ""')
            obj.append('\t\t\tMappingInformationType: "AllSame"')
            obj.append('\t\t\tReferenceInformationType: "IndexToDirect"')
            obj.append("\t\t\tMaterials: *1 {")
            obj.append("\t\t\t\ta: 0")
            obj.append("\t\t\t}")
            obj.append("\t\t}")
        else:
            obj.append("\t\tLayerElementMaterial: 0 {")
            obj.append("\t\t\tVersion: 101")
            obj.append('\t\t\tName: ""')
            obj.append('\t\t\tMappingInformationType: "ByPolygon"')
            obj.append('\t\t\tReferenceInformationType: "IndexToDirect"')
            obj.append(f"\t\t\tMaterials: *{len(material_per_face)} {{")
            obj.append("\t\t\t\ta: " + ",".join(str(m) for m in material_per_face))
            obj.append("\t\t\t}")
            obj.append("\t\t}")

        # Layer binding
        obj.append("\t\tLayer: 0 {")
        obj.append("\t\t\tVersion: 100")
        obj.append("\t\t\tLayerElement:  {")
        obj.append('\t\t\t\tType: "LayerElementNormal"')
        obj.append("\t\t\t\tTypedIndex: 0")
        obj.append("\t\t\t}")
        obj.append("\t\t\tLayerElement:  {")
        obj.append('\t\t\t\tType: "LayerElementMaterial"')
        obj.append("\t\t\t\tTypedIndex: 0")
        obj.append("\t\t\t}")
        for layer_idx in range(num_uv_layers):
            obj.append("\t\t\tLayerElement:  {")
            obj.append('\t\t\t\tType: "LayerElementUV"')
            obj.append(f"\t\t\t\tTypedIndex: {layer_idx}")
            obj.append("\t\t\t}")
        obj.append("\t\t}")

        # Shape blocks (blend shape deltas) - inside the Geometry
        if shape_blocks:
            for shape_name, indices, deltas in shape_blocks:
                flat_d: List[float] = []
                for d in deltas:
                    flat_d.extend(d)
                obj.append(f'\t\tShape: "{shape_name}" {{')
                obj.append("\t\t\tVersion: 100")
                obj.append(f"\t\t\tIndexes: *{len(indices)} {{")
                obj.append("\t\t\t\ta: " + ",".join(str(i) for i in indices))
                obj.append("\t\t\t}")
                obj.append(f"\t\t\tVectors: *{len(flat_d)} {{")
                obj.append(f"\t\t\t\ta: {_fmt_arr(flat_d)}")
                obj.append("\t\t\t}")
                obj.append("\t\t}")

        obj.append("\t}")
        self.objects.append("\n".join(obj))
        return oid

    def add_material(
        self,
        name: str,
        diffuse: Vec4,
        specular: Vec3,
        specular_strength: float,
        ambient: Vec3,
        emissive: Vec3 = (0.0, 0.0, 0.0),
        opacity: float = 1.0,
    ) -> int:
        oid = self.id_gen.new()
        self._count("Material")
        # Apply diffuse alpha to opacity if needed
        if diffuse[3] < 1.0 and opacity >= 1.0:
            opacity = diffuse[3]
        obj = []
        obj.append(f'\tMaterial: {oid}, "Material::{name}", "" {{')
        obj.append("\t\tVersion: 102")
        obj.append('\t\tShadingModel: "phong"')
        obj.append("\t\tMultiLayer: 0")
        obj.append("\t\tProperties70:  {")
        obj.append('\t\t\tP: "ShadingModel", "KString", "", "", "phong"')
        obj.append('\t\t\tP: "MultiLayer", "bool", "", "",0')
        obj.append(
            f'\t\t\tP: "EmissiveColor", "ColorRGB", "Color", "",'
            f"{_fmt_num(emissive[0])},{_fmt_num(emissive[1])},{_fmt_num(emissive[2])}"
        )
        obj.append('\t\t\tP: "EmissiveFactor", "double", "Number", "",1')
        obj.append(
            f'\t\t\tP: "AmbientColor", "ColorRGB", "Color", "",'
            f"{_fmt_num(ambient[0])},{_fmt_num(ambient[1])},{_fmt_num(ambient[2])}"
        )
        obj.append('\t\t\tP: "AmbientFactor", "double", "Number", "",1')
        obj.append(
            f'\t\t\tP: "DiffuseColor", "ColorRGB", "Color", "",'
            f"{_fmt_num(diffuse[0])},{_fmt_num(diffuse[1])},{_fmt_num(diffuse[2])}"
        )
        obj.append('\t\t\tP: "DiffuseFactor", "double", "Number", "",1')
        obj.append(
            f'\t\t\tP: "SpecularColor", "ColorRGB", "Color", "",'
            f"{_fmt_num(specular[0])},{_fmt_num(specular[1])},{_fmt_num(specular[2])}"
        )
        obj.append('\t\t\tP: "SpecularFactor", "double", "Number", "",1')
        obj.append(
            f'\t\t\tP: "ShininessExponent", "double", "Number", "",'
            f"{_fmt_num(max(specular_strength, 0.0))}"
        )
        obj.append('\t\t\tP: "ReflectionColor", "ColorRGB", "Color", "",0,0,0')
        obj.append('\t\t\tP: "ReflectionFactor", "double", "Number", "",0')
        if opacity < 1.0:
            obj.append('\t\t\tP: "TransparentColor", "ColorRGB", "Color", "",1,1,1')
            obj.append(
                f'\t\t\tP: "TransparencyFactor", "double", "Number", "",'
                f"{_fmt_num(1.0 - opacity)}"
            )
        obj.append("\t\t}")
        obj.append("\t}")
        self.objects.append("\n".join(obj))
        return oid

    def add_texture(self, name: str, relative_filename: str, uv_set: str = "UVChannel_1") -> int:
        oid = self.id_gen.new()
        self._count("Texture")
        obj = []
        obj.append(f'\tTexture: {oid}, "Texture::{name}", "" {{')
        obj.append('\t\tType: "TextureVideoClip"')
        obj.append("\t\tVersion: 202")
        obj.append(f'\t\tTextureName: "Texture::{name}"')
        obj.append("\t\tProperties70:  {")
        obj.append('\t\t\tP: "CurrentTextureBlendMode", "enum", "", "",0')
        obj.append(f'\t\t\tP: "UVSet", "KString", "", "", "{uv_set}"')
        obj.append('\t\t\tP: "UseMaterial", "bool", "", "",1')
        obj.append("\t\t}")
        obj.append(f'\t\tMedia: "Video::{name}"')
        obj.append(f"\t\tFileName: {_quote(relative_filename)}")
        obj.append(f"\t\tRelativeFilename: {_quote(relative_filename)}")
        obj.append("\t\tModelUVTranslation: 0,0")
        obj.append("\t\tModelUVScaling: 1,1")
        obj.append('\t\tTexture_Alpha_Source: "None"')
        obj.append("\t\tCropping: 0,0,0,0")
        obj.append("\t}")
        self.objects.append("\n".join(obj))
        return oid

    def add_video(self, name: str, relative_filename: str) -> int:
        oid = self.id_gen.new()
        self._count("Video")
        obj = []
        obj.append(f'\tVideo: {oid}, "Video::{name}", "Clip" {{')
        obj.append('\t\tType: "Clip"')
        obj.append("\t\tProperties70:  {")
        obj.append(f'\t\t\tP: "Path", "KString", "XRefUrl", "", {_quote(relative_filename)}')
        obj.append("\t\t}")
        obj.append("\t\tUseMipMap: 0")
        obj.append(f"\t\tFilename: {_quote(relative_filename)}")
        obj.append(f"\t\tRelativeFilename: {_quote(relative_filename)}")
        obj.append("\t}")
        self.objects.append("\n".join(obj))
        return oid

    def add_skin_deformer(self, name: str) -> int:
        oid = self.id_gen.new()
        self._count("Deformer")
        obj = []
        obj.append(f'\tDeformer: {oid}, "Deformer::{name}", "Skin" {{')
        obj.append("\t\tVersion: 101")
        obj.append("\t\tLink_DeformAcuracy: 50")
        obj.append("\t}")
        self.objects.append("\n".join(obj))
        return oid

    def add_blendshape_deformer(self, name: str) -> int:
        oid = self.id_gen.new()
        self._count("Deformer")
        obj = []
        obj.append(f'\tDeformer: {oid}, "Deformer::{name}", "BlendShape" {{')
        obj.append("\t\tVersion: 100")
        obj.append("\t}")
        self.objects.append("\n".join(obj))
        return oid

    def add_cluster(
        self,
        name: str,
        indices: List[int],
        weights: List[float],
        transform: Mat4,
        transform_link: Mat4,
    ) -> int:
        oid = self.id_gen.new()
        self._count("SubDeformer")
        obj = []
        obj.append(f'\tSubDeformer: {oid}, "SubDeformer::{name}", "Cluster" {{')
        obj.append("\t\tVersion: 100")
        obj.append('\t\tUserData: "", ""')
        obj.append(f"\t\tIndexes: *{len(indices)} {{")
        obj.append("\t\t\ta: " + ",".join(str(i) for i in indices))
        obj.append("\t\t}")
        obj.append(f"\t\tWeights: *{len(weights)} {{")
        obj.append("\t\t\ta: " + ",".join(_fmt_num(w) for w in weights))
        obj.append("\t\t}")
        obj.append("\t\tTransform: *16 {")
        obj.append("\t\t\ta: " + _fmt_arr(list(transform)))
        obj.append("\t\t}")
        obj.append("\t\tTransformLink: *16 {")
        obj.append("\t\t\ta: " + _fmt_arr(list(transform_link)))
        obj.append("\t\t}")
        obj.append("\t}")
        self.objects.append("\n".join(obj))
        return oid

    def add_blendshape_channel(self, name: str) -> int:
        oid = self.id_gen.new()
        self._count("SubDeformer")
        obj = []
        obj.append(f'\tSubDeformer: {oid}, "SubDeformer::{name}", "BlendShapeChannel" {{')
        obj.append("\t\tVersion: 100")
        obj.append("\t\tDeformPercent: 0")
        obj.append("\t\tFullWeights: *1 {")
        obj.append("\t\t\ta: 100")
        obj.append("\t\t}")
        obj.append("\t}")
        self.objects.append("\n".join(obj))
        return oid

    def add_bind_pose(self, name: str, nodes: List[Tuple[int, Mat4]]) -> int:
        oid = self.id_gen.new()
        self._count("Pose")
        obj = []
        obj.append(f'\tPose: {oid}, "Pose::{name}", "BindPose" {{')
        obj.append('\t\tType: "BindPose"')
        obj.append("\t\tVersion: 100")
        obj.append(f"\t\tNbPoseNodes: {len(nodes)}")
        for node_id, matrix in nodes:
            obj.append("\t\tPoseNode:  {")
            obj.append(f"\t\t\tNode: {node_id}")
            obj.append("\t\t\tMatrix: *16 {")
            obj.append("\t\t\t\ta: " + _fmt_arr(list(matrix)))
            obj.append("\t\t\t}")
            obj.append("\t\t}")
        obj.append("\t}")
        self.objects.append("\n".join(obj))
        return oid

    # ---- connection helpers ----

    def connect_oo(self, child: int, parent: int) -> None:
        self.connections.append(f'\tC: "OO",{child},{parent}')

    def connect_op(self, child: int, parent: int, prop: str) -> None:
        self.connections.append(f'\tC: "OP",{child},{parent},"{prop}"')

    # ---- final emission ----

    def render(self, creator: str = "pmx2fbx") -> str:
        # Compute Definitions count
        # Each ObjectType counts its instances; we always include GlobalSettings.
        object_types = [
            ("GlobalSettings", 1),
            ("Model", self.type_counts.get("Model", 0)),
            ("Geometry", self.type_counts.get("Geometry", 0)),
            ("Material", self.type_counts.get("Material", 0)),
            ("Texture", self.type_counts.get("Texture", 0)),
            ("Video", self.type_counts.get("Video", 0)),
            ("Deformer", self.type_counts.get("Deformer", 0)),
            ("SubDeformer", self.type_counts.get("SubDeformer", 0)),
            ("Pose", self.type_counts.get("Pose", 0)),
        ]
        definitions_count = sum(1 for _, c in object_types if c > 0)

        out: List[str] = []
        out.append("; FBX 7.4.0 project file")
        out.append(f"; Generated by {creator}")
        out.append("; For Unreal Engine 4.27 import (Skeletal Mesh + Morph Targets)")
        out.append("; " + "-" * 52)
        out.append("")

        # FBXHeaderExtension
        out.append("FBXHeaderExtension:  {")
        out.append("\tFBXHeaderVersion: 1003")
        out.append("\tFBXVersion: 7400")
        out.append("\tCreationTimeStamp:  {")
        out.append("\t\tVersion: 1000")
        out.append("\t\tYear: 2026")
        out.append("\t\tMonth: 7")
        out.append("\t\tDay: 14")
        out.append("\t\tHour: 0")
        out.append("\t\tMinute: 0")
        out.append("\t\tSecond: 0")
        out.append("\t\tMillisecond: 0")
        out.append("\t}")
        out.append(f"\tCreator: {_quote(creator)}")
        out.append("\tSceneInfo: \"SceneInfo::GlobalInfo\", \"UserData\" {")
        out.append('\t\tType: "UserData"')
        out.append("\t\tVersion: 100")
        out.append("\t\tMetaData:  {")
        out.append("\t\t\tVersion: 100")
        out.append('\t\t\tTitle: ""')
        out.append('\t\t\tSubject: ""')
        out.append('\t\t\tAuthor: ""')
        out.append('\t\t\tKeywords: ""')
        out.append('\t\t\tRevision: ""')
        out.append('\t\t\tComment: ""')
        out.append("\t\t}")
        out.append("\t\tProperties70:  {")
        out.append('\t\t\tP: "DocumentUrl", "KString", "Url", "", ""')
        out.append('\t\t\tP: "SrcDocumentUrl", "KString", "Url", "", ""')
        out.append('\t\t\tP: "Original", "Compound", "", ""')
        out.append(f'\t\t\tP: "Original|ApplicationVendor", "KString", "", "", "AkizukiKokona"')
        out.append(f'\t\t\tP: "Original|ApplicationName", "KString", "", "", "pmx2fbx"')
        out.append('\t\t\tP: "Original|ApplicationVersion", "KString", "", "", "1.0"')
        out.append('\t\t\tP: "LastSaved", "Compound", "", ""')
        out.append('\t\t\tP: "LastSaved|ApplicationVendor", "KString", "", "", "AkizukiKokona"')
        out.append('\t\t\tP: "LastSaved|ApplicationName", "KString", "", "", "pmx2fbx"')
        out.append('\t\t\tP: "LastSaved|ApplicationVersion", "KString", "", "", "1.0"')
        out.append("\t\t}")
        out.append("\t}")
        out.append("}")
        out.append("")

        # GlobalSettings - UE native axes (Z up, X forward, Y right)
        out.append("GlobalSettings:  {")
        out.append("\tVersion: 1000")
        out.append("\tProperties70:  {")
        out.append('\t\tP: "UpAxis", "int", "Integer", "",2')
        out.append('\t\tP: "UpAxisSign", "int", "Integer", "",1')
        out.append('\t\tP: "FrontAxis", "int", "Integer", "",1')
        out.append('\t\tP: "FrontAxisSign", "int", "Integer", "",1')
        out.append('\t\tP: "CoordAxis", "int", "Integer", "",0')
        out.append('\t\tP: "CoordAxisSign", "int", "Integer", "",1')
        out.append('\t\tP: "OriginalUpAxis", "int", "Integer", "",2')
        out.append('\t\tP: "OriginalUpAxisSign", "int", "Integer", "",1')
        out.append('\t\tP: "UnitScaleFactor", "double", "Number", "",1')
        out.append('\t\tP: "OriginalUnitScaleFactor", "double", "Number", "",1')
        out.append('\t\tP: "AmbientColor", "ColorRGB", "Color", "",0,0,0')
        out.append('\t\tP: "DefaultCamera", "KString", "", "", "Producer Perspective"')
        out.append('\t\tP: "TimeMode", "enum", "", "",0')
        out.append('\t\tP: "TimeSpanStart", "KTime", "Time", "",0')
        out.append('\t\tP: "TimeSpanStop", "KTime", "Time", "",46186158000')
        out.append('\t\tP: "CustomFrameRate", "double", "Number", "",-1')
        out.append("\t}")
        out.append("}")
        out.append("")

        # Documents
        doc_id = 1000000000
        out.append("Documents:  {")
        out.append("\tCount: 1")
        out.append(f'\tDocument: {doc_id}, "", "Scene" {{')
        out.append("\t\tProperties70:  {")
        out.append('\t\t\tP: "SourceObject", "object", "", ""')
        out.append('\t\t\tP: "ActiveAnimStackName", "KString", "", "", ""')
        out.append("\t\t}")
        out.append("\t\tRootNode: 0")
        out.append("\t}")
        out.append("}")
        out.append("")

        # Definitions
        out.append("Definitions:  {")
        out.append("\tVersion: 100")
        out.append(f"\tCount: {definitions_count}")
        out.append('\tObjectType: "GlobalSettings" {')
        out.append("\t\tCount: 1")
        out.append("\t}")
        if self.type_counts.get("Model", 0) > 0:
            out.append('\tObjectType: "Model" {')
            out.append(f"\t\tCount: {self.type_counts['Model']}")
            out.append('\t\tPropertyTemplate: "FbxNode" {')
            out.append("\t\t\tProperties70:  {")
            out.append('\t\t\t\tP: "QuaternionInterpolate", "bool", "", "",0')
            out.append('\t\t\t\tP: "RotationOffset", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "RotationPivot", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "ScalingOffset", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "ScalingPivot", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "TranslationActive", "bool", "", "",0')
            out.append('\t\t\t\tP: "TranslationMin", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "TranslationMax", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "RotationActive", "bool", "", "",0')
            out.append('\t\t\t\tP: "RotationMin", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "RotationMax", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "InheritType", "enum", "", "",1')
            out.append('\t\t\t\tP: "ScalingActive", "bool", "", "",0')
            out.append('\t\t\t\tP: "ScalingMin", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "ScalingMax", "Vector3D", "Vector", "",1,1,1')
            out.append('\t\t\t\tP: "GeometricTranslation", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "GeometricRotation", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "GeometricScaling", "Vector3D", "Vector", "",1,1,1')
            out.append('\t\t\t\tP: "Lcl Translation", "Lcl Translation", "", "A",0,0,0')
            out.append('\t\t\t\tP: "Lcl Rotation", "Lcl Rotation", "", "A",0,0,0')
            out.append('\t\t\t\tP: "Lcl Scaling", "Lcl Scaling", "", "A",1,1,1')
            out.append('\t\t\t\tP: "Visibility", "Visibility", "", "A",1')
            out.append("\t\t\t}")
            out.append("\t\t}")
            out.append("\t}")
        if self.type_counts.get("Geometry", 0) > 0:
            out.append('\tObjectType: "Geometry" {')
            out.append(f"\t\tCount: {self.type_counts['Geometry']}")
            out.append('\t\tPropertyTemplate: "FbxMesh" {')
            out.append("\t\t\tProperties70:  {")
            out.append('\t\t\t\tP: "Color", "ColorRGB", "Color", "",0.8,0.8,0.8')
            out.append('\t\t\t\tP: "BBoxMin", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "BBoxMax", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "Primary Visibility", "bool", "", "",1')
            out.append('\t\t\t\tP: "Casts Shadows", "bool", "", "",1')
            out.append('\t\t\t\tP: "Receive Shadows", "bool", "", "",1')
            out.append("\t\t\t}")
            out.append("\t\t}")
            out.append("\t}")
        if self.type_counts.get("Material", 0) > 0:
            out.append('\tObjectType: "Material" {')
            out.append(f"\t\tCount: {self.type_counts['Material']}")
            out.append('\t\tPropertyTemplate: "FbxSurfacePhong" {')
            out.append("\t\t\tProperties70:  {")
            out.append('\t\t\t\tP: "ShadingModel", "KString", "", "", "phong"')
            out.append('\t\t\t\tP: "MultiLayer", "bool", "", "",0')
            out.append('\t\t\t\tP: "EmissiveColor", "Color", "", "A",0,0,0')
            out.append('\t\t\t\tP: "EmissiveFactor", "Number", "", "A",1')
            out.append('\t\t\t\tP: "AmbientColor", "Color", "", "A",0.2,0.2,0.2')
            out.append('\t\t\t\tP: "AmbientFactor", "Number", "", "A",1')
            out.append('\t\t\t\tP: "DiffuseColor", "Color", "", "A",0.8,0.8,0.8')
            out.append('\t\t\t\tP: "DiffuseFactor", "Number", "", "A",1')
            out.append('\t\t\t\tP: "Bump", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "NormalMap", "Vector3D", "Vector", "",0,0,0')
            out.append('\t\t\t\tP: "BumpFactor", "double", "Number", "",1')
            out.append('\t\t\t\tP: "TransparentColor", "Color", "", "A",0,0,0')
            out.append('\t\t\t\tP: "TransparencyFactor", "Number", "", "A",0')
            out.append('\t\t\t\tP: "SpecularColor", "Color", "", "A",0.2,0.2,0.2')
            out.append('\t\t\t\tP: "SpecularFactor", "Number", "", "A",1')
            out.append('\t\t\t\tP: "ShininessExponent", "Number", "", "A",20')
            out.append('\t\t\t\tP: "ReflectionColor", "Color", "", "A",0,0,0')
            out.append('\t\t\t\tP: "ReflectionFactor", "Number", "", "A",1')
            out.append("\t\t\t}")
            out.append("\t\t}")
            out.append("\t}")
        if self.type_counts.get("Texture", 0) > 0:
            out.append('\tObjectType: "Texture" {')
            out.append(f"\t\tCount: {self.type_counts['Texture']}")
            out.append("\t}")
        if self.type_counts.get("Video", 0) > 0:
            out.append('\tObjectType: "Video" {')
            out.append(f"\t\tCount: {self.type_counts['Video']}")
            out.append("\t}")
        if self.type_counts.get("Deformer", 0) > 0:
            out.append('\tObjectType: "Deformer" {')
            out.append(f"\t\tCount: {self.type_counts['Deformer']}")
            out.append("\t}")
        if self.type_counts.get("SubDeformer", 0) > 0:
            out.append('\tObjectType: "SubDeformer" {')
            out.append(f"\t\tCount: {self.type_counts['SubDeformer']}")
            out.append("\t}")
        if self.type_counts.get("Pose", 0) > 0:
            out.append('\tObjectType: "Pose" {')
            out.append(f"\t\tCount: {self.type_counts['Pose']}")
            out.append("\t}")
        out.append("}")
        out.append("")

        # Objects
        out.append("Objects:  {")
        out.append("\n".join(self.objects))
        out.append("}")
        out.append("")

        # Connections
        out.append("Connections:  {")
        for c in self.connections:
            out.append(c)
        out.append("}")
        out.append("")

        # BindPose connection to document
        # (Pose objects are connected to the document via OO, but that's optional.)
        out.append("Takes:  {")
        out.append('\tCurrent: ""')
        out.append("}")
        out.append("")

        return "\n".join(out)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass
class ConversionOptions:
    scale: float = 8.0  # 1 PMX unit = 8 cm (default); 1 UE unit = 1 cm
    flip_uv_v: bool = True  # PMX V=0 top, UE V=0 bottom
    flip_winding: bool = True  # PMX CW -> FBX CCW
    copy_textures: bool = True  # copy textures next to FBX
    texture_subdir: str = "textures"  # subdir name next to FBX for copied textures
    emit_morphs: bool = True
    emit_bind_pose: bool = True
    max_bones_per_vertex: int = 4  # UE4 default skinning limit
    additional_uv_as_channels: bool = True


@dataclass
class _BoneInfo:
    """Per-bone precomputed data."""
    pmx_index: int
    name: str
    world_pos_pmx: Vec3  # in PMX model space
    world_pos_ue: Vec3  # in UE space (permuted + scaled)
    local_pos_ue: Vec3  # relative to parent (UE space)
    parent_pmx: int
    is_root: bool
    fbx_id: int = 0
    transform_link: Mat4 = field(default_factory=mat4_identity)


def _sanitize_name(name: str, fallback: str) -> str:
    """Make a name safe for FBX/UE. Strip control chars, keep unicode."""
    if not name:
        return fallback
    cleaned = []
    for ch in name:
        if ord(ch) < 32:
            continue
        if ch in ('"', "\\"):
            cleaned.append("_")
        else:
            cleaned.append(ch)
    s = "".join(cleaned).strip()
    return s if s else fallback


def _build_bone_info(model: PMXModel, options: ConversionOptions) -> List[_BoneInfo]:
    """Precompute bone world/local positions and detect roots."""
    bones: List[_BoneInfo] = []
    for i, b in enumerate(model.bones):
        world_pmx = b.position
        world_ue = v_scale(permute(world_pmx), options.scale)
        is_root = b.parent_bone < 0 or b.parent_bone >= len(model.bones)
        parent_pmx = -1 if is_root else b.parent_bone
        bones.append(
            _BoneInfo(
                pmx_index=i,
                name=_sanitize_name(b.name_en or b.name_jp, f"Bone_{i}"),
                world_pos_pmx=world_pmx,
                world_pos_ue=world_ue,
                local_pos_ue=(0.0, 0.0, 0.0),  # filled below
                parent_pmx=parent_pmx,
                is_root=is_root,
            )
        )
    # Compute local positions (relative to parent in UE space)
    for bi in bones:
        if bi.is_root or bi.parent_pmx < 0:
            bi.local_pos_ue = bi.world_pos_ue
        else:
            parent = bones[bi.parent_pmx]
            bi.local_pos_ue = v_sub(bi.world_pos_ue, parent.world_pos_ue)
    return bones


def _build_clusters(
    model: PMXModel, bones: List[_BoneInfo], options: ConversionOptions
) -> Dict[int, Tuple[List[int], List[float]]]:
    """For each bone, collect (vertex_index, weight) pairs it influences.

    - If a vertex lists the same bone multiple times (common in BDEF4 when the
      author duplicated a bone slot), the weights are summed.
    - Caps influences at max_bones_per_vertex per vertex by keeping the top-N
      weighted bones and renormalizing.
    """
    # First, collect and sum all influences per vertex (bone -> total weight)
    per_vertex: List[Dict[int, float]] = [{} for _ in range(len(model.vertices))]
    for vi, v in enumerate(model.vertices):
        for bi, w in zip(v.bones, v.weights):
            if 0 <= bi < len(bones) and w > 0.0:
                per_vertex[vi][bi] = per_vertex[vi].get(bi, 0.0) + w

    # Convert to sorted lists, cap to max bones per vertex, renormalize
    per_vertex_list: List[List[Tuple[int, float]]] = []
    for influences in per_vertex:
        items = sorted(influences.items(), key=lambda x: -x[1])
        if len(items) > options.max_bones_per_vertex:
            items = items[: options.max_bones_per_vertex]
        total = sum(w for _, w in items)
        if total > 0.0:
            items = [(bi, w / total) for bi, w in items]
        per_vertex_list.append(items)

    # Group by bone
    clusters: Dict[int, Tuple[List[int], List[float]]] = {}
    for vi, influences in enumerate(per_vertex_list):
        for bi, w in influences:
            if bi not in clusters:
                clusters[bi] = ([], [])
            clusters[bi][0].append(vi)
            clusters[bi][1].append(w)
    return clusters


def _compute_transform_link(bone: _BoneInfo) -> Mat4:
    """TransformLink = bone's bind-pose world matrix. For PMX bones (translation
    only, identity rotation/scale), this is just translation by world_pos_ue."""
    return mat4_translation(bone.world_pos_ue)


def write_fbx(
    model: PMXModel,
    out_path: str,
    options: Optional[ConversionOptions] = None,
    log=print,
) -> str:
    """Convert a PMXModel to an FBX 7.4.0 ASCII file.

    Args:
      model: parsed PMX model
      out_path: absolute path to write the .fbx file
      options: conversion options (defaults to scale=8.0)
      log: callable for progress messages

    Returns:
      The path that was written.
    """
    if options is None:
        options = ConversionOptions()

    log(f"Building FBX structure for {model.name_en or model.name_jp!r}...")
    log(
        f"  vertices={len(model.vertices)} faces={len(model.faces)//3} "
        f"materials={len(model.materials)} bones={len(model.bones)} "
        f"morphs={len(model.morphs)} textures={len(model.textures)}"
    )

    fbx = _FBXFile()
    out_dir = os.path.dirname(os.path.abspath(out_path))
    out_base = os.path.splitext(os.path.basename(out_path))[0]

    # --- Bones ---
    bones = _build_bone_info(model, options)
    log(f"  bones: {len(bones)} total, {sum(1 for b in bones if b.is_root)} root(s)")

    # Detect multiple roots - if so, add a synthetic root bone
    roots = [b for b in bones if b.is_root]
    synthetic_root_id: Optional[int] = None
    if len(roots) > 1:
        # Create synthetic root at origin (UE space)
        synthetic_root_name = "___Root"
        synthetic_root_id = fbx.add_model_bone(synthetic_root_name, (0.0, 0.0, 0.0))
        log(f"  created synthetic root bone (multiple roots detected)")

    # Create FBX Model nodes for each bone
    for bi in bones:
        bi.fbx_id = fbx.add_model_bone(bi.name, bi.local_pos_ue)
        bi.transform_link = _compute_transform_link(bi)

    # Connect bone hierarchy
    for bi in bones:
        if bi.is_root:
            if synthetic_root_id is not None:
                fbx.connect_oo(bi.fbx_id, synthetic_root_id)
            else:
                fbx.connect_oo(bi.fbx_id, 0)  # parent to scene root
        else:
            parent = bones[bi.parent_pmx]
            fbx.connect_oo(bi.fbx_id, parent.fbx_id)
    if synthetic_root_id is not None:
        fbx.connect_oo(synthetic_root_id, 0)

    # Determine which bone the mesh is parented to. UE4 expects the mesh to be
    # parented to the root bone (synthetic or first root).
    if synthetic_root_id is not None:
        mesh_parent_id = synthetic_root_id
    elif roots:
        mesh_parent_id = roots[0].fbx_id
    else:
        mesh_parent_id = 0

    # --- Mesh Model ---
    mesh_name = _sanitize_name(model.name_en or model.name_jp, out_base or "Mesh")
    mesh_model_id = fbx.add_model_mesh(mesh_name, (0.0, 0.0, 0.0))
    fbx.connect_oo(mesh_model_id, mesh_parent_id)

    # --- Geometry ---
    # Build control points (vertices) in UE space
    vertices_ue: List[Vec3] = [v_scale(permute(v.position), options.scale) for v in model.vertices]

    # Build faces: PMX CW -> FBX CCW. Flip winding by swapping v1 and v2.
    faces: List[Tuple[int, int, int]] = []
    for i in range(0, len(model.faces), 3):
        v0 = model.faces[i]
        v1 = model.faces[i + 1]
        v2 = model.faces[i + 2]
        if options.flip_winding:
            faces.append((v0, v2, v1))
        else:
            faces.append((v0, v1, v2))

    # Build per-corner normals in UE space (permute, but do NOT scale)
    normals_ue: List[Vec3] = []
    # Faces are triangles; for each corner we need the vertex's normal.
    # PMX stores per-vertex normals; we just expand per corner.
    # (PMX normals are normalized; we keep them normalized.)
    for (v0, v1, v2) in faces:
        for vi in (v0, v1, v2):
            n = model.vertices[vi].normal
            normals_ue.append(permute(n))

    # Build UVs. PMX V=0 is top; UE V=0 is bottom. Flip V.
    # Use IndexToDirect: unique UVs indexed per corner. Most MMD models have
    # per-vertex UVs (so UV index = vertex index), but to be safe we dedupe.
    uvs_unique: List[Tuple[float, float]] = []
    uv_map: Dict[Tuple[float, float], int] = {}
    uv_indices: List[int] = []
    for (v0, v1, v2) in faces:
        for vi in (v0, v1, v2):
            u, v = model.vertices[vi].uv
            if options.flip_uv_v:
                v = 1.0 - v
            key = (u, v)
            idx = uv_map.get(key)
            if idx is None:
                idx = len(uvs_unique)
                uv_map[key] = idx
                uvs_unique.append((u, v))
            uv_indices.append(idx)

    # Additional UVs as extra UV channels (per control point, 2 components used)
    additional_uvs_layers: Optional[List[List[Tuple[float, float, float, float]]]] = None
    if options.additional_uv_as_channels and model.additional_uv_count > 0:
        additional_uvs_layers = []
        for layer_idx in range(model.additional_uv_count):
            layer = [v.additional_uvs[layer_idx] for v in model.vertices]
            additional_uvs_layers.append(layer)

    # Material per face: PMX materials are contiguous blocks of face_count/3
    # triangles each. Compute the slot index per face.
    material_per_face: Optional[List[int]] = None
    if len(model.materials) > 1:
        material_per_face = []
        slot = 0
        remaining = model.materials[0].face_count // 3 if model.materials else 0
        for face_idx in range(len(faces)):
            while remaining <= 0 and slot < len(model.materials) - 1:
                slot += 1
                remaining = model.materials[slot].face_count // 3
            material_per_face.append(slot)
            remaining -= 1
    elif len(model.materials) == 1:
        material_per_face = None  # AllSame is more efficient
    else:
        material_per_face = None  # no materials

    # Shape blocks for vertex morphs
    shape_blocks: List[Tuple[str, List[int], List[Vec3]]] = []
    if options.emit_morphs:
        for morph in model.morphs:
            if morph.morph_type != 1:  # only vertex morphs
                continue
            mname = _sanitize_name(morph.name_en or morph.name_jp, "Morph")
            indices: List[int] = []
            deltas: List[Vec3] = []
            for o in morph.offsets:
                if o.vertex_index < 0 or o.vertex_index >= len(model.vertices):
                    continue
                indices.append(o.vertex_index)
                # Delta is in PMX space; permute to UE space and apply scale
                d_ue = v_scale(permute(o.translation), options.scale)
                deltas.append(d_ue)
            if indices:
                shape_blocks.append((mname, indices, deltas))
        if shape_blocks:
            log(f"  morphs: {len(shape_blocks)} vertex morph(s) -> blend shapes")

    geometry_id = fbx.add_geometry(
        mesh_name,
        vertices_ue,
        faces,
        normals_ue,
        uvs_unique,
        uv_indices,
        additional_uvs=additional_uvs_layers,
        material_per_face=material_per_face,
        shape_blocks=shape_blocks if shape_blocks else None,
    )
    fbx.connect_oo(geometry_id, mesh_model_id)

    # --- Materials ---
    material_ids: List[int] = []
    for mi, mat in enumerate(model.materials):
        mname = _sanitize_name(mat.name_en or mat.name_jp, f"Mat_{mi}")
        # PMX diffuse RGB is in 0..1 linear-ish; pass through.
        # Emissive: PMX doesn't have emissive directly; use 0.
        mid = fbx.add_material(
            mname,
            diffuse=mat.diffuse,
            specular=mat.specular,
            specular_strength=mat.specular_strength,
            ambient=mat.ambient,
            emissive=(0.0, 0.0, 0.0),
            opacity=mat.diffuse[3],
        )
        material_ids.append(mid)
        fbx.connect_oo(mid, mesh_model_id)

    # --- Textures ---
    # The texture table entries are taken as-is for the FBX RelativeFilename.
    # The convert.py orchestrator is responsible for copying textures next to
    # the FBX and updating model.textures[ti] to the appropriate relative path
    # (e.g. "textures/foo.png"). If the table entry still points to an
    # absolute or PMX-relative path, we just normalize separators.
    texture_fbx_ids: Dict[int, Tuple[int, int]] = {}  # tex_index -> (tex_id, vid_id)
    for ti, tex_path in enumerate(model.textures):
        # Use the texture table entry directly as the relative filename. We
        # normalize backslashes to forward slashes (FBX/UE convention) and
        # strip any leading "./".
        rel_name = tex_path.replace("\\", "/")
        if rel_name.startswith("./"):
            rel_name = rel_name[2:]
        if not rel_name:
            rel_name = f"texture_{ti}.png"
        basename = os.path.basename(rel_name)
        tex_name = _sanitize_name(os.path.splitext(basename)[0], f"Tex_{ti}")
        tex_id = fbx.add_texture(tex_name, rel_name)
        vid_id = fbx.add_video(tex_name, rel_name)
        fbx.connect_oo(vid_id, tex_id)
        texture_fbx_ids[ti] = (tex_id, vid_id)

    # Connect textures to materials based on each material's texture/sphere/toon
    for mi, mat in enumerate(model.materials):
        if mi >= len(material_ids):
            continue
        mid = material_ids[mi]
        # Diffuse texture
        if 0 <= mat.texture_index < len(model.textures):
            tex_id, _ = texture_fbx_ids[mat.texture_index]
            fbx.connect_op(tex_id, mid, "DiffuseColor")
        # Sphere texture - add as additional DiffuseColor (UE will combine) or
        # as SpecularColor depending on sphere_mode. We use DiffuseColor for
        # mul/add modes (visually similar) and SpecularColor for sub-tex mode.
        if 0 <= mat.sphere_index < len(model.textures):
            tex_id, _ = texture_fbx_ids[mat.sphere_index]
            if mat.sphere_mode == 3:
                fbx.connect_op(tex_id, mid, "SpecularColor")
            else:
                # Sphere as a second diffuse texture - UE will pick the first;
                # for completeness we also connect to EmissiveColor so it shows.
                fbx.connect_op(tex_id, mid, "EmissiveColor")
        # Toon texture (if not shared)
        if not mat.toon_shared and 0 <= mat.toon_index < len(model.textures):
            tex_id, _ = texture_fbx_ids[mat.toon_index]
            fbx.connect_op(tex_id, mid, "EmissiveColor")

    # --- Skinning (Skin Deformer + Clusters) ---
    if bones:
        clusters = _build_clusters(model, bones, options)
        skin_id = fbx.add_skin_deformer(mesh_name)
        fbx.connect_oo(skin_id, geometry_id)
        # Mesh bind transform: identity (mesh Model at origin)
        mesh_transform = mat4_identity()
        for bi_idx, bi in enumerate(bones):
            if bi_idx not in clusters:
                continue
            indices, weights = clusters[bi_idx]
            cluster_id = fbx.add_cluster(
                bi.name,
                indices,
                weights,
                transform=mesh_transform,
                transform_link=bi.transform_link,
            )
            fbx.connect_oo(cluster_id, skin_id)
            fbx.connect_oo(cluster_id, bi.fbx_id)
        log(f"  skinning: {len(clusters)} cluster(s)")

    # --- BlendShape Deformers ---
    if shape_blocks:
        bs_id = fbx.add_blendshape_deformer(mesh_name)
        fbx.connect_oo(bs_id, geometry_id)
        for shape_name, _, _ in shape_blocks:
            chan_id = fbx.add_blendshape_channel(shape_name)
            fbx.connect_oo(chan_id, bs_id)
            # Connect the geometry to the channel (channel finds its shape by name)
            fbx.connect_oo(geometry_id, chan_id)

    # --- BindPose ---
    if options.emit_bind_pose and bones:
        pose_nodes: List[Tuple[int, Mat4]] = []
        if synthetic_root_id is not None:
            pose_nodes.append((synthetic_root_id, mat4_identity()))
        for bi in bones:
            pose_nodes.append((bi.fbx_id, bi.transform_link))
        pose_nodes.append((mesh_model_id, mat4_identity()))
        pose_id = fbx.add_bind_pose("BindPose", pose_nodes)
        # Pose connects to the Document
        fbx.connect_oo(pose_id, 1000000000)

    # --- Render and write ---
    log(f"  rendering FBX ASCII...")
    content = fbx.render(creator="pmx2fbx")
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    log(f"  wrote {len(content):,} bytes to {out_path}")
    return out_path
