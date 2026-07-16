"""FBX 7.4.0 binary writer for Unreal Engine 4.27 with embedded textures.

Zero external dependencies (uses only Python standard library: struct, zlib,
array, math, os). Produces FBX 7400 **binary** files with:
  - Embedded texture data (Video.Content with MIME header, raw bytes)
  - Mesh geometry (vertices, faces, normals, UVs, additional UVs)
  - Per-corner material assignment (LayerElementMaterial ByPolygon)
  - Phong materials with diffuse/specular/ambient/emissive
  - Textures connected to material properties
  - Bone hierarchy as Model "LimbNode" objects, with synthetic "Root" bone
    if PMX has multiple roots
  - Skinning: Skin Deformer + Cluster SubDeformers with Transform/TransformLink
  - Vertex morphs as BlendShape + BlendShapeChannel + Shape (with deltas)
  - BindPose object
  - Coordinate conversion PMX (Y-up) -> UE (Z-up) via cyclic permutation
  - UV V-flip, triangle winding flip, scale factor

Binary FBX format reference:
  - Header: 27 bytes (23-byte magic "Kaydara FBX Binary  \\x00\\x1a\\x00" + uint32 version)
  - Node records: EndOffset/NumProps/PropListLen/NameLen/Name/Props/Children/NULL
  - Properties: type char + data (Y/C/I/F/D/L/S/R and arrays i/l/f/d/b)
  - Footer: Footer1(16) + padding(0-15) + Footer2(4 zeros) + version(4) + Footer3(120 zeros) + Footer4(16 fixed)
"""

from __future__ import annotations

import array
import math
import os
import struct
import sys
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pmx_reader import (
    PMXBone,
    PMXMaterial,
    PMXModel,
    PMXMorph,
    PMXVertex,
)


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

Vec3 = Tuple[float, float, float]
Vec4 = Tuple[float, float, float, float]
Mat4 = Tuple[float, ...]


# ---------------------------------------------------------------------------
# Math utilities (no numpy)
# ---------------------------------------------------------------------------


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
# Binary FBX node tree and serialization
# ---------------------------------------------------------------------------


class _Node:
    """A single FBX binary node: name + properties + children."""

    __slots__ = ("name", "props", "children")

    def __init__(
        self,
        name: str = "",
        props: Optional[List[Tuple[str, Any]]] = None,
        children: Optional[List["_Node"]] = None,
    ):
        self.name = name
        self.props: List[Tuple[str, Any]] = props if props is not None else []
        self.children: List[_Node] = children if children is not None else []

    def add_prop(self, type_code: str, value: Any) -> None:
        self.props.append((type_code, value))

    def add_child(self, node: "_Node") -> "_Node":
        self.children.append(node)
        return node


# Property type sizes (for the data part, excluding the 1-byte type code)
_PROP_DATA_SIZE: Dict[str, int] = {
    "Y": 2,   # int16
    "C": 1,   # bool/uint8
    "I": 4,   # int32
    "F": 4,   # float32
    "D": 8,   # float64
    "L": 8,   # int64
}

_ARRAY_ELEM_SIZE: Dict[str, int] = {
    "i": 4,   # int32 array
    "l": 8,   # int64 array
    "f": 4,   # float32 array
    "d": 8,   # float64 array
    "b": 1,   # bool array
}


def _property_size(prop: Tuple[str, Any]) -> int:
    """Compute the byte size of a serialized property (including type code)."""
    tc = prop[0]
    if tc in _PROP_DATA_SIZE:
        return 1 + _PROP_DATA_SIZE[tc]
    elif tc in ("S", "R"):
        data = prop[1]
        if isinstance(data, str):
            data = data.encode("utf-8")
        return 1 + 4 + len(data)
    elif tc in _ARRAY_ELEM_SIZE:
        data = prop[1]
        count = len(data)
        # type(1) + count(4) + encoding(4) + compressed_length(4) + data
        # We compute the actual size after compression attempt
        raw_bytes = _pack_array(tc, data)
        compressed = zlib.compress(raw_bytes, 1)
        if len(compressed) < len(raw_bytes):
            return 1 + 4 + 4 + 4 + len(compressed)
        else:
            return 1 + 4 + 4 + 4 + len(raw_bytes)
    return 0


def _pack_array(type_code: str, data: List[Any]) -> bytes:
    """Pack a Python list into raw bytes for an FBX array property."""
    if type_code == "i":
        arr = array.array("i", data)
    elif type_code == "l":
        arr = array.array("q", data)
    elif type_code == "f":
        arr = array.array("f", data)
    elif type_code == "d":
        arr = array.array("d", data)
    elif type_code == "b":
        arr = array.array("B", [1 if v else 0 for v in data])
    else:
        return b""
    # Ensure little-endian
    if arr.itemsize > 1 and sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


def _serialize_property(prop: Tuple[str, Any]) -> bytes:
    """Serialize a single property to bytes."""
    tc = prop[0]
    out = tc.encode("ascii")
    if tc == "Y":  # int16
        out += struct.pack("<h", int(prop[1]))
    elif tc == "C":  # bool
        out += struct.pack("<B", 1 if prop[1] else 0)
    elif tc == "I":  # int32
        out += struct.pack("<i", int(prop[1]))
    elif tc == "F":  # float32
        out += struct.pack("<f", float(prop[1]))
    elif tc == "D":  # float64
        out += struct.pack("<d", float(prop[1]))
    elif tc == "L":  # int64
        out += struct.pack("<q", int(prop[1]))
    elif tc == "S":  # string
        data = prop[1]
        if isinstance(data, str):
            data = data.encode("utf-8")
        out += struct.pack("<I", len(data)) + data
    elif tc == "R":  # raw bytes
        data = prop[1]
        if isinstance(data, str):
            data = data.encode("utf-8")
        out += struct.pack("<I", len(data)) + data
    elif tc in _ARRAY_ELEM_SIZE:  # arrays
        data = prop[1]
        count = len(data)
        raw_bytes = _pack_array(tc, data)
        # Try zlib compression
        compressed = zlib.compress(raw_bytes, 1)
        if len(compressed) < len(raw_bytes):
            out += struct.pack("<III", count, 1, len(compressed))
            out += compressed
        else:
            out += struct.pack("<III", count, 0, len(raw_bytes))
            out += raw_bytes
    return out


def _node_size(node: _Node) -> int:
    """Compute total byte size of a serialized node (including children + NULL)."""
    name_bytes = node.name.encode("utf-8")
    header_size = 13 + len(name_bytes)  # EndOffset(4)+NumProps(4)+PropListLen(4)+NameLen(1)+Name
    props_size = sum(_property_size(p) for p in node.props)
    children_size = 0
    if node.children:
        for child in node.children:
            children_size += _node_size(child)
        children_size += 13  # NULL record
    return header_size + props_size + children_size


def _serialize_node(node: _Node, base_offset: int) -> bytes:
    """Serialize a node and all its children to bytes.

    Args:
      node: the node to serialize.
      base_offset: absolute file offset where this node starts.
    """
    name_bytes = node.name.encode("utf-8")

    # Serialize properties
    props_bytes = b"".join(_serialize_property(p) for p in node.props)

    # Offset where children start
    children_start = base_offset + 13 + len(name_bytes) + len(props_bytes)

    # Serialize children
    children_bytes = b""
    if node.children:
        child_offset = children_start
        for child in node.children:
            child_bytes = _serialize_node(child, child_offset)
            children_bytes += child_bytes
            child_offset += len(child_bytes)
        # NULL record (13 zero bytes)
        children_bytes += b"\x00" * 13

    # EndOffset = first byte after this node
    end_offset = children_start + len(children_bytes)

    # Header
    header = struct.pack("<III", end_offset, len(node.props), len(props_bytes))
    header += struct.pack("B", len(name_bytes))
    header += name_bytes

    return header + props_bytes + children_bytes


# ---------------------------------------------------------------------------
# FBX file builder
# ---------------------------------------------------------------------------


class _FBXFile:
    """Builds an in-memory FBX binary node tree and serializes it."""

    def __init__(self):
        self.id_gen = _IDGen()
        self.type_counts: Dict[str, int] = {
            "Model": 0,
            "Geometry": 0,
            "Material": 0,
            "Texture": 0,
            "Video": 0,
            "NodeAttribute": 0,
            "Deformer": 0,
            "Pose": 0,
        }
        # Top-level nodes (order matches reference FBX files from Blender/Maya):
        # FBXHeaderExtension, FileId, CreationTime, Creator, GlobalSettings,
        # Documents, References, Definitions, Objects, Connections, Takes
        self._root = _Node()  # implicit top-level (empty name)
        self._header_ext = self._root.add_child(_Node("FBXHeaderExtension"))
        # FileId: 16-byte raw identifier (constant used by Blender/Maya reference files)
        self._root.add_child(_Node("FileId", [("R", bytes.fromhex("28b32aebb624ccc2bfc8b02aa92bfcf1"))]))
        self._root.add_child(_Node("CreationTime", [("S", "1970-01-01 10:00:00:000")]))
        self._root.add_child(_Node("Creator", [("S", "pmx2fbx")]))
        self._global_settings = self._root.add_child(_Node("GlobalSettings"))
        self._documents = self._root.add_child(_Node("Documents"))
        self._references = self._root.add_child(_Node("References"))
        self._definitions = self._root.add_child(_Node("Definitions"))
        self._objects = self._root.add_child(_Node("Objects"))
        self._connections = self._root.add_child(_Node("Connections"))
        self._takes = self._root.add_child(_Node("Takes"))

        self._init_header()
        self._init_global_settings()
        self._init_documents()

    def _count(self, t: str) -> None:
        self.type_counts[t] = self.type_counts.get(t, 0) + 1

    # ---- header / global settings / documents ----

    def _init_header(self) -> None:
        h = self._header_ext
        h.add_child(_Node("FBXHeaderVersion", [("I", 1003)]))
        h.add_child(_Node("FBXVersion", [("I", 7400)]))
        ts = h.add_child(_Node("CreationTimeStamp"))
        ts.add_child(_Node("Version", [("I", 1000)]))
        ts.add_child(_Node("Year", [("I", 2026)]))
        ts.add_child(_Node("Month", [("I", 7)]))
        ts.add_child(_Node("Day", [("I", 14)]))
        ts.add_child(_Node("Hour", [("I", 0)]))
        ts.add_child(_Node("Minute", [("I", 0)]))
        ts.add_child(_Node("Second", [("I", 0)]))
        ts.add_child(_Node("Millisecond", [("I", 0)]))
        h.add_child(_Node("Creator", [("S", "pmx2fbx")]))
        si = h.add_child(_Node("SceneInfo"))
        si.props = [("S", "SceneInfo::GlobalInfo"), ("S", "UserData")]
        si.add_child(_Node("Type", [("S", "UserData")]))
        si.add_child(_Node("Version", [("I", 100)]))
        md = si.add_child(_Node("MetaData"))
        md.add_child(_Node("Version", [("I", 100)]))
        md.add_child(_Node("Title", [("S", "")]))
        md.add_child(_Node("Subject", [("S", "")]))
        md.add_child(_Node("Author", [("S", "")]))
        md.add_child(_Node("Keywords", [("S", "")]))
        md.add_child(_Node("Revision", [("S", "")]))
        md.add_child(_Node("Comment", [("S", "")]))
        p70 = si.add_child(_Node("Properties70"))
        p70.add_child(_Node("P", [("S", "DocumentUrl"), ("S", "KString"), ("S", "Url"), ("S", ""), ("S", "")]))
        p70.add_child(_Node("P", [("S", "SrcDocumentUrl"), ("S", "KString"), ("S", "Url"), ("S", ""), ("S", "")]))
        p70.add_child(_Node("P", [("S", "Original"), ("S", "Compound"), ("S", ""), ("S", "")]))
        p70.add_child(_Node("P", [("S", "Original|ApplicationVendor"), ("S", "KString"), ("S", ""), ("S", ""), ("S", "AkizukiKokona")]))
        p70.add_child(_Node("P", [("S", "Original|ApplicationName"), ("S", "KString"), ("S", ""), ("S", ""), ("S", "pmx2fbx")]))
        p70.add_child(_Node("P", [("S", "Original|ApplicationVersion"), ("S", "KString"), ("S", ""), ("S", ""), ("S", "1.0")]))
        p70.add_child(_Node("P", [("S", "LastSaved"), ("S", "Compound"), ("S", ""), ("S", "")]))
        p70.add_child(_Node("P", [("S", "LastSaved|ApplicationVendor"), ("S", "KString"), ("S", ""), ("S", ""), ("S", "AkizukiKokona")]))
        p70.add_child(_Node("P", [("S", "LastSaved|ApplicationName"), ("S", "KString"), ("S", ""), ("S", ""), ("S", "pmx2fbx")]))
        p70.add_child(_Node("P", [("S", "LastSaved|ApplicationVersion"), ("S", "KString"), ("S", ""), ("S", ""), ("S", "1.0")]))

    def _init_global_settings(self) -> None:
        gs = self._global_settings
        gs.add_child(_Node("Version", [("I", 1000)]))
        p70 = gs.add_child(_Node("Properties70"))
        # UE native: Z up (2), X front (1), Y coord (0)
        p70.add_child(_Node("P", [("S", "UpAxis"), ("S", "int"), ("S", "Integer"), ("S", ""), ("I", 2)]))
        p70.add_child(_Node("P", [("S", "UpAxisSign"), ("S", "int"), ("S", "Integer"), ("S", ""), ("I", 1)]))
        p70.add_child(_Node("P", [("S", "FrontAxis"), ("S", "int"), ("S", "Integer"), ("S", ""), ("I", 1)]))
        p70.add_child(_Node("P", [("S", "FrontAxisSign"), ("S", "int"), ("S", "Integer"), ("S", ""), ("I", 1)]))
        p70.add_child(_Node("P", [("S", "CoordAxis"), ("S", "int"), ("S", "Integer"), ("S", ""), ("I", 0)]))
        p70.add_child(_Node("P", [("S", "CoordAxisSign"), ("S", "int"), ("S", "Integer"), ("S", ""), ("I", 1)]))
        p70.add_child(_Node("P", [("S", "OriginalUpAxis"), ("S", "int"), ("S", "Integer"), ("S", ""), ("I", 2)]))
        p70.add_child(_Node("P", [("S", "OriginalUpAxisSign"), ("S", "int"), ("S", "Integer"), ("S", ""), ("I", 1)]))
        p70.add_child(_Node("P", [("S", "UnitScaleFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 1.0)]))
        p70.add_child(_Node("P", [("S", "OriginalUnitScaleFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 1.0)]))
        p70.add_child(_Node("P", [("S", "AmbientColor"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)]))
        p70.add_child(_Node("P", [("S", "DefaultCamera"), ("S", "KString"), ("S", ""), ("S", ""), ("S", "Producer Perspective")]))
        p70.add_child(_Node("P", [("S", "TimeMode"), ("S", "enum"), ("S", ""), ("S", ""), ("I", 0)]))
        p70.add_child(_Node("P", [("S", "TimeSpanStart"), ("S", "KTime"), ("S", "Time"), ("S", ""), ("L", 0)]))
        p70.add_child(_Node("P", [("S", "TimeSpanStop"), ("S", "KTime"), ("S", "Time"), ("S", ""), ("L", 46186158000)]))
        p70.add_child(_Node("P", [("S", "CustomFrameRate"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", -1.0)]))

    def _init_documents(self) -> None:
        doc_id = 1000000000
        self._documents.add_child(_Node("Count", [("I", 1)]))
        doc = self._documents.add_child(_Node("Document", [("L", doc_id), ("S", ""), ("S", "Scene")]))
        p70 = doc.add_child(_Node("Properties70"))
        p70.add_child(_Node("P", [("S", "SourceObject"), ("S", "object"), ("S", ""), ("S", "")]))
        p70.add_child(_Node("P", [("S", "ActiveAnimStackName"), ("S", "KString"), ("S", ""), ("S", ""), ("S", "")]))
        doc.add_child(_Node("RootNode", [("L", 0)]))

    def build_definitions(self) -> None:
        """Build the Definitions section from type_counts. Call before render."""
        d = self._definitions
        d.add_child(_Node("Version", [("I", 100)]))
        object_types = [
            ("GlobalSettings", 1),
            ("Model", self.type_counts.get("Model", 0)),
            ("Geometry", self.type_counts.get("Geometry", 0)),
            ("Material", self.type_counts.get("Material", 0)),
            ("Texture", self.type_counts.get("Texture", 0)),
            ("Video", self.type_counts.get("Video", 0)),
            ("NodeAttribute", self.type_counts.get("NodeAttribute", 0)),
            ("Deformer", self.type_counts.get("Deformer", 0)),
            ("Pose", self.type_counts.get("Pose", 0)),
        ]
        count = sum(1 for _, c in object_types if c > 0)
        d.add_child(_Node("Count", [("I", count)]))

        # GlobalSettings
        ot = d.add_child(_Node("ObjectType", [("S", "GlobalSettings")]))
        ot.add_child(_Node("Count", [("I", 1)]))

        # Model
        if self.type_counts.get("Model", 0) > 0:
            ot = d.add_child(_Node("ObjectType", [("S", "Model")]))
            ot.add_child(_Node("Count", [("I", self.type_counts["Model"])]))
            pt = ot.add_child(_Node("PropertyTemplate", [("S", "FbxNode")]))
            p70 = pt.add_child(_Node("Properties70"))
            for p in _MODEL_PROPS_TEMPLATE:
                p70.add_child(_Node("P", p))

        # Geometry
        if self.type_counts.get("Geometry", 0) > 0:
            ot = d.add_child(_Node("ObjectType", [("S", "Geometry")]))
            ot.add_child(_Node("Count", [("I", self.type_counts["Geometry"])]))
            pt = ot.add_child(_Node("PropertyTemplate", [("S", "FbxMesh")]))
            p70 = pt.add_child(_Node("Properties70"))
            p70.add_child(_Node("P", [("S", "Color"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", 0.8), ("D", 0.8), ("D", 0.8)]))
            p70.add_child(_Node("P", [("S", "BBoxMin"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)]))
            p70.add_child(_Node("P", [("S", "BBoxMax"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)]))
            p70.add_child(_Node("P", [("S", "Primary Visibility"), ("S", "bool"), ("S", ""), ("S", ""), ("C", True)]))
            p70.add_child(_Node("P", [("S", "Casts Shadows"), ("S", "bool"), ("S", ""), ("S", ""), ("C", True)]))
            p70.add_child(_Node("P", [("S", "Receive Shadows"), ("S", "bool"), ("S", ""), ("S", ""), ("C", True)]))

        # Material
        if self.type_counts.get("Material", 0) > 0:
            ot = d.add_child(_Node("ObjectType", [("S", "Material")]))
            ot.add_child(_Node("Count", [("I", self.type_counts["Material"])]))
            pt = ot.add_child(_Node("PropertyTemplate", [("S", "FbxSurfacePhong")]))
            p70 = pt.add_child(_Node("Properties70"))
            for p in _MATERIAL_PROPS_TEMPLATE:
                p70.add_child(_Node("P", p))

        # NodeAttribute (LimbNode for bones)
        if self.type_counts.get("NodeAttribute", 0) > 0:
            ot = d.add_child(_Node("ObjectType", [("S", "NodeAttribute")]))
            ot.add_child(_Node("Count", [("I", self.type_counts["NodeAttribute"])]))
            pt = ot.add_child(_Node("PropertyTemplate", [("S", "FbxSkeleton")]))
            p70 = pt.add_child(_Node("Properties70"))
            p70.add_child(_Node("P", [("S", "Color"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", 0.8), ("D", 0.8), ("D", 0.8)]))
            p70.add_child(_Node("P", [("S", "Size"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 100.0)]))
            p70.add_child(_Node("P", [("S", "LimbLength"), ("S", "double"), ("S", "Number"), ("S", "H"), ("D", 1.0)]))

        # Texture, Video, Deformer, Pose
        for tname in ("Texture", "Video", "Deformer", "Pose"):
            if self.type_counts.get(tname, 0) > 0:
                ot = d.add_child(_Node("ObjectType", [("S", tname)]))
                ot.add_child(_Node("Count", [("I", self.type_counts[tname])]))

    # ---- object emitters (add nodes to self._objects) ----

    def add_model_mesh(self, name: str, translation: Vec3 = (0.0, 0.0, 0.0)) -> int:
        oid = self.id_gen.new()
        self._count("Model")
        obj = _Node("Model", [("L", oid), ("S", f"Model::{name}"), ("S", "Mesh")])
        obj.add_child(_Node("Version", [("I", 232)]))
        p70 = obj.add_child(_Node("Properties70"))
        p70.add_child(_Node("P", [("S", "InheritType"), ("S", "enum"), ("S", ""), ("S", ""), ("I", 1)]))
        p70.add_child(_Node("P", [("S", "Lcl Translation"), ("S", "Lcl Translation"), ("S", ""), ("S", "A"), ("D", translation[0]), ("D", translation[1]), ("D", translation[2])]))
        p70.add_child(_Node("P", [("S", "Lcl Rotation"), ("S", "Lcl Rotation"), ("S", ""), ("S", "A"), ("D", 0.0), ("D", 0.0), ("D", 0.0)]))
        p70.add_child(_Node("P", [("S", "Lcl Scaling"), ("S", "Lcl Scaling"), ("S", ""), ("S", "A"), ("D", 1.0), ("D", 1.0), ("D", 1.0)]))
        p70.add_child(_Node("P", [("S", "DefaultAttributeIndex"), ("S", "int"), ("S", "Integer"), ("S", ""), ("I", 0)]))
        obj.add_child(_Node("Shading", [("Y", 1)]))  # T=True in ASCII -> Y(1) in binary
        obj.add_child(_Node("Culling", [("S", "CullingOff")]))
        self._objects.add_child(obj)
        return oid

    def add_model_bone(
        self,
        name: str,
        translation: Vec3,
        rotation_euler_deg: Vec3 = (0.0, 0.0, 0.0),
    ) -> int:
        oid = self.id_gen.new()
        self._count("Model")
        obj = _Node("Model", [("L", oid), ("S", f"Model::{name}"), ("S", "LimbNode")])
        obj.add_child(_Node("Version", [("I", 232)]))
        p70 = obj.add_child(_Node("Properties70"))
        p70.add_child(_Node("P", [("S", "QuaternionInterpolate"), ("S", "bool"), ("S", ""), ("S", ""), ("C", False)]))
        p70.add_child(_Node("P", [("S", "RotationActive"), ("S", "bool"), ("S", ""), ("S", ""), ("C", True)]))
        p70.add_child(_Node("P", [("S", "InheritType"), ("S", "enum"), ("S", ""), ("S", ""), ("I", 1)]))
        p70.add_child(_Node("P", [("S", "PostRotation"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)]))
        p70.add_child(_Node("P", [("S", "RotationPivot"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)]))
        p70.add_child(_Node("P", [("S", "RotationOffset"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)]))
        p70.add_child(_Node("P", [("S", "Lcl Translation"), ("S", "Lcl Translation"), ("S", ""), ("S", "A+"), ("D", translation[0]), ("D", translation[1]), ("D", translation[2])]))
        p70.add_child(_Node("P", [("S", "Lcl Rotation"), ("S", "Lcl Rotation"), ("S", ""), ("S", "A+"), ("D", rotation_euler_deg[0]), ("D", rotation_euler_deg[1]), ("D", rotation_euler_deg[2])]))
        p70.add_child(_Node("P", [("S", "Lcl Scaling"), ("S", "Lcl Scaling"), ("S", ""), ("S", "A+"), ("D", 1.0), ("D", 1.0), ("D", 1.0)]))
        p70.add_child(_Node("P", [("S", "Visibility"), ("S", "Visibility"), ("S", ""), ("S", "A"), ("D", 1.0)]))
        obj.add_child(_Node("Shading", [("Y", 1)]))
        obj.add_child(_Node("Culling", [("S", "CullingOff")]))
        self._objects.add_child(obj)
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
    ) -> int:
        oid = self.id_gen.new()
        self._count("Geometry")
        obj = _Node("Geometry", [("L", oid), ("S", f"Geometry::{name}"), ("S", "Mesh")])

        # Vertices: flat double array
        flat_v: List[float] = []
        for v in vertices:
            flat_v.extend(v)
        obj.add_child(_Node("Vertices", [("d", flat_v)]))

        # PolygonVertexIndex: triangles, last index bitwise-NOT
        pvi: List[int] = []
        for (a, b, c) in faces:
            pvi.append(a)
            pvi.append(b)
            pvi.append(~c)
        obj.add_child(_Node("PolygonVertexIndex", [("i", pvi)]))

        obj.add_child(_Node("GeometryVersion", [("I", 124)]))

        # Normals (ByPolygonVertex Direct)
        flat_n: List[float] = []
        for n in normals:
            flat_n.extend(n)
        le_n = obj.add_child(_Node("LayerElementNormal", [("I", 0)]))
        le_n.add_child(_Node("Version", [("I", 101)]))
        le_n.add_child(_Node("Name", [("S", "")]))
        le_n.add_child(_Node("MappingInformationType", [("S", "ByPolygonVertex")]))
        le_n.add_child(_Node("ReferenceInformationType", [("S", "Direct")]))
        le_n.add_child(_Node("Normals", [("d", flat_n)]))
        le_n.add_child(_Node("NormalsW", [("d", [1.0] * len(normals))]))

        # Base UV layer
        flat_uv: List[float] = []
        for uv in uvs:
            flat_uv.extend(uv)
        le_uv = obj.add_child(_Node("LayerElementUV", [("I", 0)]))
        le_uv.add_child(_Node("Version", [("I", 101)]))
        le_uv.add_child(_Node("Name", [("S", "UVChannel_1")]))
        le_uv.add_child(_Node("MappingInformationType", [("S", "ByPolygonVertex")]))
        le_uv.add_child(_Node("ReferenceInformationType", [("S", "IndexToDirect")]))
        le_uv.add_child(_Node("UV", [("d", flat_uv)]))
        le_uv.add_child(_Node("UVIndex", [("i", uv_indices)]))

        # Additional UV layers
        num_uv_layers = 1
        if additional_uvs:
            for layer_idx, layer in enumerate(additional_uvs):
                flat_auv: List[float] = []
                for auv in layer:
                    flat_auv.append(auv[0])
                    flat_auv.append(auv[1])
                auv_indices = [face[i] for face in faces for i in range(3)]
                le = obj.add_child(_Node("LayerElementUV", [("I", layer_idx + 1)]))
                le.add_child(_Node("Version", [("I", 101)]))
                le.add_child(_Node("Name", [("S", f"UVChannel_{layer_idx + 2}")]))
                le.add_child(_Node("MappingInformationType", [("S", "ByPolygonVertex")]))
                le.add_child(_Node("ReferenceInformationType", [("S", "IndexToDirect")]))
                le.add_child(_Node("UV", [("d", flat_auv)]))
                le.add_child(_Node("UVIndex", [("i", auv_indices)]))
                num_uv_layers += 1

        # Material layer
        if material_per_face is None:
            le_mat = obj.add_child(_Node("LayerElementMaterial", [("I", 0)]))
            le_mat.add_child(_Node("Version", [("I", 101)]))
            le_mat.add_child(_Node("Name", [("S", "")]))
            le_mat.add_child(_Node("MappingInformationType", [("S", "AllSame")]))
            le_mat.add_child(_Node("ReferenceInformationType", [("S", "IndexToDirect")]))
            le_mat.add_child(_Node("Materials", [("i", [0])]))
        else:
            le_mat = obj.add_child(_Node("LayerElementMaterial", [("I", 0)]))
            le_mat.add_child(_Node("Version", [("I", 101)]))
            le_mat.add_child(_Node("Name", [("S", "")]))
            le_mat.add_child(_Node("MappingInformationType", [("S", "ByPolygon")]))
            le_mat.add_child(_Node("ReferenceInformationType", [("S", "IndexToDirect")]))
            le_mat.add_child(_Node("Materials", [("i", material_per_face)]))

        # Layer binding
        layer = obj.add_child(_Node("Layer", [("I", 0)]))
        layer.add_child(_Node("Version", [("I", 100)]))
        le1 = layer.add_child(_Node("LayerElement"))
        le1.add_child(_Node("Type", [("S", "LayerElementNormal")]))
        le1.add_child(_Node("TypedIndex", [("I", 0)]))
        le2 = layer.add_child(_Node("LayerElement"))
        le2.add_child(_Node("Type", [("S", "LayerElementMaterial")]))
        le2.add_child(_Node("TypedIndex", [("I", 0)]))
        for layer_idx in range(num_uv_layers):
            le = layer.add_child(_Node("LayerElement"))
            le.add_child(_Node("Type", [("S", "LayerElementUV")]))
            le.add_child(_Node("TypedIndex", [("I", layer_idx)]))

        self._objects.add_child(obj)
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
        obj = _Node("Material", [("L", oid), ("S", f"Material::{name}"), ("S", "")])
        obj.add_child(_Node("Version", [("I", 102)]))
        obj.add_child(_Node("ShadingModel", [("S", "phong")]))
        obj.add_child(_Node("MultiLayer", [("I", 0)]))
        p70 = obj.add_child(_Node("Properties70"))
        p70.add_child(_Node("P", [("S", "ShadingModel"), ("S", "KString"), ("S", ""), ("S", ""), ("S", "phong")]))
        p70.add_child(_Node("P", [("S", "MultiLayer"), ("S", "bool"), ("S", ""), ("S", ""), ("C", False)]))
        p70.add_child(_Node("P", [("S", "EmissiveColor"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", emissive[0]), ("D", emissive[1]), ("D", emissive[2])]))
        p70.add_child(_Node("P", [("S", "EmissiveFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 1.0)]))
        p70.add_child(_Node("P", [("S", "AmbientColor"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", ambient[0]), ("D", ambient[1]), ("D", ambient[2])]))
        p70.add_child(_Node("P", [("S", "AmbientFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 1.0)]))
        p70.add_child(_Node("P", [("S", "DiffuseColor"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", diffuse[0]), ("D", diffuse[1]), ("D", diffuse[2])]))
        p70.add_child(_Node("P", [("S", "DiffuseFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 1.0)]))
        p70.add_child(_Node("P", [("S", "SpecularColor"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", specular[0]), ("D", specular[1]), ("D", specular[2])]))
        p70.add_child(_Node("P", [("S", "SpecularFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 1.0)]))
        p70.add_child(_Node("P", [("S", "ShininessExponent"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", max(specular_strength, 0.0))]))
        p70.add_child(_Node("P", [("S", "ReflectionColor"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)]))
        p70.add_child(_Node("P", [("S", "ReflectionFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)]))
        if opacity < 1.0:
            p70.add_child(_Node("P", [("S", "TransparentColor"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", 1.0), ("D", 1.0), ("D", 1.0)]))
            p70.add_child(_Node("P", [("S", "TransparencyFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 1.0 - opacity)]))
        self._objects.add_child(obj)
        return oid

    def add_texture(self, name: str, relative_filename: str, uv_set: str = "UVChannel_1") -> int:
        oid = self.id_gen.new()
        self._count("Texture")
        obj = _Node("Texture", [("L", oid), ("S", f"Texture::{name}"), ("S", "")])
        obj.add_child(_Node("Type", [("S", "TextureVideoClip")]))
        obj.add_child(_Node("Version", [("I", 202)]))
        obj.add_child(_Node("TextureName", [("S", f"Texture::{name}")]))
        p70 = obj.add_child(_Node("Properties70"))
        p70.add_child(_Node("P", [("S", "CurrentTextureBlendMode"), ("S", "enum"), ("S", ""), ("S", ""), ("I", 0)]))
        p70.add_child(_Node("P", [("S", "UVSet"), ("S", "KString"), ("S", ""), ("S", ""), ("S", uv_set)]))
        p70.add_child(_Node("P", [("S", "UseMaterial"), ("S", "bool"), ("S", ""), ("S", ""), ("C", True)]))
        obj.add_child(_Node("Media", [("S", f"Video::{name}")]))
        fname = relative_filename.replace("\\", "/")
        obj.add_child(_Node("FileName", [("S", fname)]))
        obj.add_child(_Node("RelativeFilename", [("S", fname)]))
        obj.add_child(_Node("ModelUVTranslation", [("D", 0.0), ("D", 0.0)]))
        obj.add_child(_Node("ModelUVScaling", [("D", 1.0), ("D", 1.0)]))
        obj.add_child(_Node("Texture_Alpha_Source", [("S", "None")]))
        obj.add_child(_Node("Cropping", [("I", 0), ("I", 0), ("I", 0), ("I", 0)]))
        self._objects.add_child(obj)
        return oid

    def add_video(
        self,
        name: str,
        relative_filename: str,
        content_bytes: Optional[bytes] = None,
    ) -> int:
        oid = self.id_gen.new()
        self._count("Video")
        obj = _Node("Video", [("L", oid), ("S", f"Video::{name}"), ("S", "Clip")])
        obj.add_child(_Node("Type", [("S", "Clip")]))
        p70 = obj.add_child(_Node("Properties70"))
        fname = relative_filename.replace("\\", "/")
        p70.add_child(_Node("P", [("S", "Path"), ("S", "KString"), ("S", "XRefUrl"), ("S", ""), ("S", fname)]))
        obj.add_child(_Node("UseMipMap", [("I", 0)]))
        obj.add_child(_Node("Filename", [("S", fname)]))
        obj.add_child(_Node("RelativeFilename", [("S", fname)]))
        # Embedded content: raw bytes with MIME header
        if content_bytes is not None:
            obj.add_child(_Node("Content", [("R", content_bytes)]))
        self._objects.add_child(obj)
        return oid

    def add_skin_deformer(self, name: str) -> int:
        oid = self.id_gen.new()
        self._count("Deformer")
        obj = _Node("Deformer", [("L", oid), ("S", f"Deformer::{name}"), ("S", "Skin")])
        obj.add_child(_Node("Version", [("I", 101)]))
        obj.add_child(_Node("Link_DeformAcuracy", [("D", 50.0)]))
        self._objects.add_child(obj)
        return oid

    def add_blendshape_deformer(self, name: str) -> int:
        oid = self.id_gen.new()
        self._count("Deformer")
        obj = _Node("Deformer", [("L", oid), ("S", f"Deformer::{name}"), ("S", "BlendShape")])
        obj.add_child(_Node("Version", [("I", 100)]))
        self._objects.add_child(obj)
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
        self._count("Deformer")
        obj = _Node("Deformer", [("L", oid), ("S", f"SubDeformer::{name}"), ("S", "Cluster")])
        obj.add_child(_Node("Version", [("I", 100)]))
        obj.add_child(_Node("UserData", [("S", ""), ("S", "")]))
        obj.add_child(_Node("Indexes", [("i", indices)]))
        obj.add_child(_Node("Weights", [("d", weights)]))
        obj.add_child(_Node("Transform", [("d", list(transform))]))
        obj.add_child(_Node("TransformLink", [("d", list(transform_link))]))
        self._objects.add_child(obj)
        return oid

    def add_blendshape_channel(self, name: str) -> int:
        oid = self.id_gen.new()
        self._count("Deformer")
        obj = _Node("Deformer", [("L", oid), ("S", f"SubDeformer::{name}"), ("S", "BlendShapeChannel")])
        obj.add_child(_Node("Version", [("I", 100)]))
        obj.add_child(_Node("DeformPercent", [("D", 0.0)]))
        obj.add_child(_Node("FullWeights", [("d", [100.0])]))
        self._objects.add_child(obj)
        return oid

    def add_shape_geometry(self, name: str, indices: List[int], deltas: List[Vec3]) -> int:
        """Create a standalone Geometry node of type 'Shape' for blend shape deltas.

        ufbx/Maya expect blend shape targets as separate Geometry objects with
        sub_type 'Shape', containing Indexes (affected vertex indices) and
        Vertices (delta positions, 3 floats per affected vertex).
        """
        oid = self.id_gen.new()
        self._count("Geometry")
        obj = _Node("Geometry", [("L", oid), ("S", f"Geometry::{name}"), ("S", "Shape")])
        p70 = obj.add_child(_Node("Properties70"))
        p70.add_child(_Node("P", [("S", "LegacyStyle"), ("S", "bool"), ("S", ""), ("S", ""), ("C", False)]))
        obj.add_child(_Node("Version", [("I", 101)]))
        obj.add_child(_Node("Indexes", [("i", indices)]))
        flat_d: List[float] = []
        for d in deltas:
            flat_d.extend(d)
        obj.add_child(_Node("Vertices", [("d", flat_d)]))
        self._objects.add_child(obj)
        return oid

    def add_bind_pose(self, name: str, nodes: List[Tuple[int, Mat4]]) -> int:
        oid = self.id_gen.new()
        self._count("Pose")
        obj = _Node("Pose", [("L", oid), ("S", f"Pose::{name}"), ("S", "BindPose")])
        obj.add_child(_Node("Type", [("S", "BindPose")]))
        obj.add_child(_Node("Version", [("I", 100)]))
        obj.add_child(_Node("NbPoseNodes", [("I", len(nodes))]))
        for node_id, matrix in nodes:
            pn = obj.add_child(_Node("PoseNode"))
            pn.add_child(_Node("Node", [("L", node_id)]))
            pn.add_child(_Node("Matrix", [("d", list(matrix))]))
        self._objects.add_child(obj)
        return oid

    def add_node_attribute_bone(self, name: str) -> int:
        """Create a NodeAttribute of type 'LimbNode' for a bone Model.

        ufbx (UE5.8's FBX parser) detects bones via NodeAttribute nodes with
        sub_type 'LimbNode' for FBX version 7000+. Without this, the bone
        Model nodes are parsed as regular nodes but scene->bones.count == 0.
        Connection format (per Maya reference): C("OO", attr_id, model_id)
        i.e. NodeAttribute is the source, Model is the destination.
        """
        oid = self.id_gen.new()
        self._count("NodeAttribute")
        obj = _Node("NodeAttribute", [("L", oid), ("S", f"NodeAttribute::{name}"), ("S", "LimbNode")])
        p70 = obj.add_child(_Node("Properties70"))
        p70.add_child(_Node("P", [("S", "Size"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 1.0)]))
        obj.add_child(_Node("TypeFlags", [("S", "Skeleton")]))
        self._objects.add_child(obj)
        return oid

    # ---- connection helpers ----

    def connect_oo(self, child: int, parent: int) -> None:
        self._connections.add_child(_Node("C", [("S", "OO"), ("L", child), ("L", parent)]))

    def connect_op(self, child: int, parent: int, prop: str) -> None:
        self._connections.add_child(_Node("C", [("S", "OP"), ("L", child), ("L", parent), ("S", prop)]))

    # ---- final serialization ----

    def render(self, creator: str = "pmx2fbx") -> bytes:
        """Serialize the entire FBX binary file to bytes."""
        # Build Definitions (must be done after all objects are added)
        self.build_definitions()

        # Takes (empty — no animation; standard FBX 7.4 format)
        self._takes.add_child(_Node("Current", [("S", "")]))

        # --- Serialize ---
        # The root node is a virtual container; its children are the top-level
        # FBX nodes. We serialize them sequentially with running offsets.
        # Magic is 23 bytes: "Kaydara FBX Binary" + TWO spaces + NUL + 0x1A + NUL
        header = b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", 7400)
        body = bytearray()
        offset = len(header)  # 27
        for child in self._root.children:
            cb = _serialize_node(child, offset)
            body.extend(cb)
            offset += len(cb)
        # NULL record terminates the root (13 bytes for FBX 7.4)
        body.extend(b"\x00" * 13)

        # --- Footer (FBX 7.4 binary, 160 bytes + padding) ---
        # Reference file layout (verified against Blender/Maya output):
        #   Footer1 (16) -> padding (0-15 zeros) -> Footer2 (4 zeros) -> version (4)
        #   -> Footer3 (120 zeros) -> Footer4 (16 fixed magic)
        # ufbx stops parsing at the NULL record and never reads the footer, so the
        # exact padding does not affect parsing. Layout matches reference files.
        pos_after_null = len(header) + len(body)
        footer1 = b"\x00" * 16
        pos_after_footer1 = pos_after_null + 16
        padding_len = (16 - (pos_after_footer1 % 16)) % 16
        padding = b"\x00" * padding_len
        footer2 = b"\x00" * 4
        version_field = struct.pack("<I", 7400)
        footer3 = b"\x00" * 120
        footer4 = bytes.fromhex("f85a8c6adef5d97eece90ce3758f290b")
        footer = footer1 + padding + footer2 + version_field + footer3 + footer4
        return header + bytes(body) + footer


# ---------------------------------------------------------------------------
# Property templates (referenced by build_definitions)
# ---------------------------------------------------------------------------

_MODEL_PROPS_TEMPLATE: List[Tuple[Any, ...]] = [
    (("S", "QuaternionInterpolate"), ("S", "bool"), ("S", ""), ("S", ""), ("C", False)),
    (("S", "RotationOffset"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "RotationPivot"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "ScalingOffset"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "ScalingPivot"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "TranslationActive"), ("S", "bool"), ("S", ""), ("S", ""), ("C", False)),
    (("S", "TranslationMin"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "TranslationMax"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "RotationOrder"), ("S", "enum"), ("S", ""), ("S", ""), ("I", 0)),
    (("S", "RotationSpaceForLimitOnly"), ("S", "bool"), ("S", ""), ("S", ""), ("C", False)),
    (("S", "RotationStiffnessX"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "RotationStiffnessY"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "RotationStiffnessZ"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "AxisLen"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 10.0)),
    (("S", "PreRotation"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "PostRotation"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "RotationActive"), ("S", "bool"), ("S", ""), ("S", ""), ("C", False)),
    (("S", "RotationMin"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "RotationMax"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "InheritType"), ("S", "enum"), ("S", ""), ("S", ""), ("I", 0)),
    (("S", "ScalingActive"), ("S", "bool"), ("S", ""), ("S", ""), ("C", False)),
    (("S", "ScalingMin"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "ScalingMax"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 1.0), ("D", 1.0), ("D", 1.0)),
    (("S", "GeometricTranslation"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "GeometricRotation"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "GeometricScaling"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 1.0), ("D", 1.0), ("D", 1.0)),
    (("S", "MinDampRangeX"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "MinDampRangeY"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "MinDampRangeZ"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "MaxDampRangeX"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "MaxDampRangeY"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "MaxDampRangeZ"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "MinDampStrengthX"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "MinDampStrengthY"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "MinDampStrengthZ"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "MaxDampStrengthX"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "MaxDampStrengthY"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "MaxDampStrengthZ"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "PreferedAngleX"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "PreferedAngleY"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "PreferedAngleZ"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "LookAtProperty"), ("S", "object"), ("S", ""), ("S", "")),
    (("S", "UpVectorProperty"), ("S", "object"), ("S", ""), ("S", "")),
    (("S", "Show"), ("S", "bool"), ("S", ""), ("S", ""), ("C", True)),
    (("S", "NegativePercentShapeSupport"), ("S", "bool"), ("S", ""), ("S", ""), ("C", True)),
    (("S", "DefaultAttributeIndex"), ("S", "int"), ("S", "Integer"), ("S", ""), ("I", -1)),
    (("S", "Freeze"), ("S", "bool"), ("S", ""), ("S", ""), ("C", False)),
    (("S", "LODBox"), ("S", "bool"), ("S", ""), ("S", ""), ("C", False)),
    (("S", "Visibility"), ("S", "Visibility"), ("S", ""), ("S", "A"), ("D", 1.0)),
    (("S", "Lcl Translation"), ("S", "Lcl Translation"), ("S", ""), ("S", "A"), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "Lcl Rotation"), ("S", "Lcl Rotation"), ("S", ""), ("S", "A"), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "Lcl Scaling"), ("S", "Lcl Scaling"), ("S", ""), ("S", "A"), ("D", 1.0), ("D", 1.0), ("D", 1.0)),
]

_MATERIAL_PROPS_TEMPLATE: List[Tuple[Any, ...]] = [
    (("S", "ShadingModel"), ("S", "KString"), ("S", ""), ("S", ""), ("S", "phong")),
    (("S", "MultiLayer"), ("S", "bool"), ("S", ""), ("S", ""), ("C", False)),
    (("S", "EmissiveColor"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "EmissiveFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 1.0)),
    (("S", "AmbientColor"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", 0.2), ("D", 0.2), ("D", 0.2)),
    (("S", "AmbientFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 1.0)),
    (("S", "DiffuseColor"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", 0.8), ("D", 0.8), ("D", 0.8)),
    (("S", "DiffuseFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 1.0)),
    (("S", "Bump"), ("S", "Vector3D"), ("S", "Vector"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "BumpFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 1.0)),
    (("S", "TransparentColor"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", 1.0), ("D", 1.0), ("D", 1.0)),
    (("S", "TransparencyFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 0.0)),
    (("S", "SpecularColor"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", 0.2), ("D", 0.2), ("D", 0.2)),
    (("S", "SpecularFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 1.0)),
    (("S", "ShininessExponent"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 20.0)),
    (("S", "ReflectionColor"), ("S", "ColorRGB"), ("S", "Color"), ("S", ""), ("D", 0.0), ("D", 0.0), ("D", 0.0)),
    (("S", "ReflectionFactor"), ("S", "double"), ("S", "Number"), ("S", ""), ("D", 1.0)),
]


# ---------------------------------------------------------------------------
# Conversion options and bone-info dataclass
# ---------------------------------------------------------------------------


@dataclass
class ConversionOptions:
    """Options controlling PMX -> FBX conversion."""
    scale: float = 8.0  # 1 PMX unit = N cm
    copy_textures: bool = True  # also copy texture files next to FBX
    emit_morphs: bool = True  # emit blend shapes for vertex morphs
    emit_bind_pose: bool = True  # emit a BindPose object
    embed_textures: bool = True  # embed texture bytes inside FBX binary
    max_bones_per_vertex: int = 4  # cap (UE4 default GPU skinning)
    texture_subdir: str = "textures"
    synthetic_root_name: str = "Root"  # synthetic root bone for multi-root PMX


@dataclass
class _BoneInfo:
    """Per-bone computed info for FBX emission."""
    pmx_index: int
    name: str  # sanitized unique name
    parent_pmx_index: int  # -1 if none (or synthetic root)
    parent_fbx_index: int  # index into bone_infos (after synthetic root injection); -1 for top
    fbx_model_id: int  # the Model object id assigned by _FBXFile
    local_translation: Vec3  # in UE space, scaled
    world_translation: Vec3  # in UE space, scaled
    transform_link: Mat4  # world bind matrix for skinning


def _sanitize_name(name: str, fallback: str, used: set) -> str:
    """Return a name safe for FBX: non-empty, unique within `used`.
    FBX object long names (Model::xxx) tolerate most Unicode but we strip
    a few problematic characters and ensure uniqueness."""
    base = name.strip() if name else ""
    if not base:
        base = fallback
    # Replace characters that confuse FBX/UE
    cleaned_chars = []
    for ch in base:
        if ch in ("\x00", "\n", "\r", "\t"):
            continue
        if ch in ('"',):
            cleaned_chars.append("_")
        else:
            cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars).strip()
    if not cleaned:
        cleaned = fallback
    candidate = cleaned
    i = 1
    while candidate in used:
        i += 1
        candidate = f"{cleaned}_{i}"
    used.add(candidate)
    return candidate


def _build_bone_info(
    model: PMXModel,
    options: ConversionOptions,
    used_names: set,
) -> Tuple[List[_BoneInfo], int]:
    """Build the bone list for FBX emission.

    Detects roots (PMX parent == -1). If there is more than one root or zero
    roots, injects a synthetic "Root" bone at the world origin (after the
    PMX->UE coordinate conversion) so UE has a single skeletal root.

    Returns:
      (bone_infos, synthetic_root_index)
      bone_infos is a list in PMX bone order, except that when a synthetic
      root is injected it is inserted at index 0 and all real bones follow
      (with their parent_fbx_index shifted accordingly).
      synthetic_root_index is the index in bone_infos of the synthetic root,
      or -1 if none was created.
    """
    scale = options.scale
    n = len(model.bones)

    # Compute world position (UE space) for every PMX bone
    world_pos: List[Vec3] = []
    for b in model.bones:
        ue = permute(b.position)
        world_pos.append((ue[0] * scale, ue[1] * scale, ue[2] * scale))

    # Identify PMX root bones (parent_bone == -1)
    pmx_roots = [i for i, b in enumerate(model.bones) if b.parent_bone < 0 or b.parent_bone >= n]

    inject_synthetic = len(pmx_roots) != 1
    synthetic_root_index = -1

    bone_infos: List[_BoneInfo] = []

    if inject_synthetic:
        # Synthetic root at world origin
        synthetic_root_index = 0
        root_name = _sanitize_name(options.synthetic_root_name, "Root", used_names)
        bone_infos.append(_BoneInfo(
            pmx_index=-1,
            name=root_name,
            parent_pmx_index=-1,
            parent_fbx_index=-1,
            fbx_model_id=0,
            local_translation=(0.0, 0.0, 0.0),
            world_translation=(0.0, 0.0, 0.0),
            transform_link=mat4_identity(),
        ))

    # Map pmx_index -> bone_infos index (for parent lookup)
    pmx_to_info: Dict[int, int] = {}
    for i in range(n):
        info_idx = len(bone_infos)
        pmx_to_info[i] = info_idx
        bone_infos.append(_BoneInfo(
            pmx_index=i,
            name="",  # filled below
            parent_pmx_index=model.bones[i].parent_bone,
            parent_fbx_index=-1,  # filled below
            fbx_model_id=0,
            local_translation=(0.0, 0.0, 0.0),
            world_translation=world_pos[i],
            transform_link=mat4_identity(),
        ))

    # Fill names
    for i in range(n):
        info_idx = pmx_to_info[i]
        b = model.bones[i]
        name_src = b.name_en if b.name_en else b.name_jp
        bone_infos[info_idx].name = _sanitize_name(name_src, f"Bone_{i}", used_names)

    # Fill parent_fbx_index
    for i in range(n):
        info_idx = pmx_to_info[i]
        parent_pmx = model.bones[i].parent_bone
        if parent_pmx < 0 or parent_pmx >= n:
            bone_infos[info_idx].parent_fbx_index = synthetic_root_index  # -1 if no synth, else 0
        else:
            bone_infos[info_idx].parent_fbx_index = pmx_to_info[parent_pmx]

    # Fill local_translation relative to parent (in UE space)
    for i in range(n):
        info_idx = pmx_to_info[i]
        info = bone_infos[info_idx]
        if info.parent_fbx_index < 0:
            parent_world = (0.0, 0.0, 0.0)
        else:
            parent_world = bone_infos[info.parent_fbx_index].world_translation
        info.local_translation = v_sub(info.world_translation, parent_world)

    # Fill transform_link (world bind matrix = translation to world_pos)
    for info in bone_infos:
        info.transform_link = mat4_translation(info.world_translation)

    return bone_infos, synthetic_root_index


def _build_clusters(
    vertices: List[PMXVertex],
    bone_infos: List[_BoneInfo],
    pmx_to_info: Dict[int, int],
    max_bones_per_vertex: int,
) -> Dict[int, List[Tuple[int, float]]]:
    """Aggregate per-bone vertex assignments.

    For each vertex, sum weights of duplicate bones, keep only the top
    `max_bones_per_vertex`, renormalize, and group by bone.

    Args:
      vertices: PMX vertices.
      bone_infos: bone info list (index = info index).
      pmx_to_info: maps PMX bone index -> bone_infos index.
      max_bones_per_vertex: cap.

    Returns:
      Dict mapping bone_infos_index -> list of (vertex_index, weight).
    """
    clusters: Dict[int, List[Tuple[int, float]]] = {i: [] for i in range(len(bone_infos))}

    for vi, vtx in enumerate(vertices):
        # Sum duplicate bone weights
        per_bone: Dict[int, float] = {}
        for bidx, w in zip(vtx.bones, vtx.weights):
            if bidx < 0:
                continue
            info_idx = pmx_to_info.get(bidx)
            if info_idx is None:
                continue
            per_bone[info_idx] = per_bone.get(info_idx, 0.0) + float(w)

        if not per_bone:
            continue

        # Keep top N
        items = sorted(per_bone.items(), key=lambda kv: kv[1], reverse=True)
        if len(items) > max_bones_per_vertex:
            items = items[:max_bones_per_vertex]

        # Renormalize
        total = sum(w for _, w in items)
        if total <= 0.0:
            continue
        for info_idx, w in items:
            clusters[info_idx].append((vi, w / total))

    # Drop empty clusters
    return {k: v for k, v in clusters.items() if v}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def write_fbx(
    model: PMXModel,
    out_path: str,
    options: Optional[ConversionOptions] = None,
    texture_bytes: Optional[Dict[int, bytes]] = None,
    log=print,
) -> str:
    """Convert a parsed PMXModel to an FBX 7.4.0 binary file.

    Args:
      model: parsed PMX model.
      out_path: absolute path to write the .fbx file.
      options: ConversionOptions (defaults if None).
      texture_bytes: optional map of texture_index -> raw image bytes, for
        embedding. When present and options.embed_textures is True, the bytes
        are written into Video.Content.
      log: progress logger.

    Returns:
      The absolute path written.
    """
    if options is None:
        options = ConversionOptions()
    if texture_bytes is None:
        texture_bytes = {}

    scale = options.scale
    out_path = os.path.abspath(out_path)
    model_name = model.name_en or model.name_jp or "PMXModel"
    used_names: set = set()

    fbx = _FBXFile()

    # ---- Bones ----
    log(f"  bones: {len(model.bones)} (building hierarchy)")
    bone_infos, synth_root_idx = _build_bone_info(model, options, used_names)
    pmx_to_info: Dict[int, int] = {}
    for idx, info in enumerate(bone_infos):
        if info.pmx_index >= 0:
            pmx_to_info[info.pmx_index] = idx

    # ---- Mesh Model ----
    mesh_name = _sanitize_name(model_name, "Mesh", used_names)
    mesh_model_id = fbx.add_model_mesh(mesh_name, translation=(0.0, 0.0, 0.0))

    # ---- Bone Models ----
    # Emit in bone_infos order so parents precede children (synthetic root
    # is first, then PMX bones in their original order).
    # For each bone, also create a NodeAttribute (LimbNode) and connect it
    # to the bone Model via OO (attr->model). ufbx detects bones through
    # NodeAttribute nodes, NOT from Model sub_type, for FBX 7000+.
    for info in bone_infos:
        info.fbx_model_id = fbx.add_model_bone(
            info.name,
            translation=info.local_translation,
        )
        attr_id = fbx.add_node_attribute_bone(info.name)
        # NodeAttribute -> Model (attr is src, model is dst, per Maya format)
        fbx.connect_oo(attr_id, info.fbx_model_id)

    # Connect bone hierarchy (OO child -> parent)
    for info in bone_infos:
        if info.parent_fbx_index >= 0:
            parent_id = bone_infos[info.parent_fbx_index].fbx_model_id
            fbx.connect_oo(info.fbx_model_id, parent_id)
        else:
            # Top-level bone connects to scene root (0)
            fbx.connect_oo(info.fbx_model_id, 0)

    # Mesh model connects to scene root (0), NOT to a bone.
    # Skinning is handled via Skin/Cluster deformers, not node hierarchy.
    fbx.connect_oo(mesh_model_id, 0)

    # ---- Geometry: vertices, faces, normals, UVs ----
    log(f"  geometry: {len(model.vertices)} verts, {len(model.faces)//3} tris")
    verts_ue: List[Vec3] = []
    for v in model.vertices:
        p = permute(v.position)
        verts_ue.append((p[0] * scale, p[1] * scale, p[2] * scale))

    # Faces: PMX flat list -> list of (a,b,c). Flip winding for UE (CCW).
    faces: List[Tuple[int, int, int]] = []
    for i in range(0, len(model.faces), 3):
        a = model.faces[i]
        b = model.faces[i + 1]
        c = model.faces[i + 2]
        faces.append((a, c, b))  # flip b<->c

    # Normals: permute (no flip; winding flip handles facing)
    normals_ue: List[Vec3] = []
    for v in model.vertices:
        normals_ue.append(permute(v.normal))

    # UVs: V-flip; one UV per vertex (PMX has unique vertices)
    uvs: List[Tuple[float, float]] = []
    for v in model.vertices:
        uvs.append((v.uv[0], 1.0 - v.uv[1]))
    uv_indices: List[int] = [idx for tri in faces for idx in tri]

    # Additional UV layers
    additional_uvs: Optional[List[List[Tuple[float, float, float, float]]]] = None
    if model.additional_uv_count > 0:
        additional_uvs = []
        for layer in range(model.additional_uv_count):
            layer_uvs = [v.additional_uvs[layer] for v in model.vertices]
            additional_uvs.append(layer_uvs)

    # Material per face
    material_per_face: List[int] = []
    for mat_idx, mat in enumerate(model.materials):
        tri_count = mat.face_count // 3
        material_per_face.extend([mat_idx] * tri_count)

    # Shape blocks (vertex morphs -> blend shapes)
    shape_blocks: List[Tuple[str, List[int], List[Vec3]]] = []
    if options.emit_morphs:
        for morph in model.morphs:
            if morph.morph_type != 1:  # only vertex morphs
                continue
            indices: List[int] = []
            deltas: List[Vec3] = []
            for off in morph.offsets:
                if off.vertex_index < 0 or off.vertex_index >= len(model.vertices):
                    continue
                d = permute(off.translation)
                indices.append(off.vertex_index)
                deltas.append((d[0] * scale, d[1] * scale, d[2] * scale))
            if indices:
                mname = _sanitize_name(
                    morph.name_en or morph.name_jp,
                    f"Morph_{len(shape_blocks)}",
                    used_names,
                )
                shape_blocks.append((mname, indices, deltas))

    geom_id = fbx.add_geometry(
        mesh_name,
        verts_ue,
        faces,
        normals_ue,
        uvs,
        uv_indices,
        additional_uvs=additional_uvs,
        material_per_face=material_per_face if material_per_face else None,
    )
    # Connect geometry OO to mesh model
    fbx.connect_oo(geom_id, mesh_model_id)

    # ---- Materials ----
    log(f"  materials: {len(model.materials)}")
    mat_fbx_ids: List[int] = []
    for mi, mat in enumerate(model.materials):
        mname = _sanitize_name(
            mat.name_en or mat.name_jp,
            f"Material_{mi}",
            used_names,
        )
        # PMX diffuse is RGBA; FBX DiffuseColor is RGB + TransparencyFactor
        opacity = mat.diffuse[3]
        mid = fbx.add_material(
            mname,
            diffuse=(mat.diffuse[0], mat.diffuse[1], mat.diffuse[2], mat.diffuse[3]),
            specular=mat.specular,
            specular_strength=mat.specular_strength,
            ambient=mat.ambient,
            emissive=(0.0, 0.0, 0.0),
            opacity=opacity,
        )
        mat_fbx_ids.append(mid)
        fbx.connect_oo(mid, mesh_model_id)

    # ---- Textures + Videos (with embedded bytes) ----
    # Collect unique texture indices actually used by materials.
    used_tex: set = set()
    for mat in model.materials:
        if mat.texture_index >= 0:
            used_tex.add(mat.texture_index)
        if mat.sphere_index >= 0:
            used_tex.add(mat.sphere_index)
        if not mat.toon_shared and mat.toon_index >= 0:
            used_tex.add(mat.toon_index)

    log(f"  textures: {len(used_tex)} referenced, embedding={options.embed_textures}")
    tex_fbx_ids: Dict[int, int] = {}  # tex_index -> Texture object id
    vid_fbx_ids: Dict[int, int] = {}  # tex_index -> Video object id
    embedded_count = 0
    for ti in sorted(used_tex):
        if ti < 0 or ti >= len(model.textures):
            continue
        tex_path = model.textures[ti]
        # Use basename for the texture/video object name and relative filename
        base = os.path.basename(tex_path.replace("\\", "/"))
        if not base:
            base = f"texture_{ti}.png"
        tname = _sanitize_name(base, f"Texture_{ti}", used_names)
        # Strip extension for the object name to avoid weirdness
        tname_stem = os.path.splitext(tname)[0]

        content = texture_bytes.get(ti) if options.embed_textures else None
        vid_id = fbx.add_video(tname_stem, base, content_bytes=content)
        tex_id = fbx.add_texture(tname_stem, base)
        tex_fbx_ids[ti] = tex_id
        vid_fbx_ids[ti] = vid_id
        fbx.connect_oo(vid_id, tex_id)
        if content is not None:
            embedded_count += 1

    if options.embed_textures:
        log(f"  embedded {embedded_count} texture(s) into FBX binary")

    # Connect textures to materials per property
    for mi, mat in enumerate(model.materials):
        mid = mat_fbx_ids[mi]
        if mat.texture_index >= 0 and mat.texture_index in tex_fbx_ids:
            fbx.connect_op(tex_fbx_ids[mat.texture_index], mid, "DiffuseColor")
        if mat.sphere_index >= 0 and mat.sphere_index in tex_fbx_ids:
            # Sphere maps are commonly used as specular/rim; route to SpecularColor
            fbx.connect_op(tex_fbx_ids[mat.sphere_index], mid, "SpecularColor")
        if not mat.toon_shared and mat.toon_index >= 0 and mat.toon_index in tex_fbx_ids:
            fbx.connect_op(tex_fbx_ids[mat.toon_index], mid, "EmissiveColor")

    # ---- Skin deformer + clusters ----
    if bone_infos:
        log(f"  skinning: building clusters (max {options.max_bones_per_vertex} bones/vert)")
        skin_id = fbx.add_skin_deformer(mesh_name)
        fbx.connect_oo(skin_id, geom_id)

        clusters = _build_clusters(
            model.vertices,
            bone_infos,
            pmx_to_info,
            options.max_bones_per_vertex,
        )
        # Transform matrix = mesh bind world = identity (mesh at origin)
        transform = mat4_identity()
        for info_idx, vw_list in clusters.items():
            info = bone_infos[info_idx]
            indices = [vi for vi, _ in vw_list]
            weights = [w for _, w in vw_list]
            cid = fbx.add_cluster(
                info.name,
                indices,
                weights,
                transform,
                info.transform_link,
            )
            # Cluster -> Skin (OO)
            fbx.connect_oo(cid, skin_id)
            # Bone -> Cluster (OO, no property). ufbx expects bone as src,
            # cluster as dst, plain OO connection (NOT OP "Link").
            fbx.connect_oo(info.fbx_model_id, cid)

    # ---- Blend shapes (vertex morphs) ----
    if shape_blocks:
        log(f"  blend shapes: {len(shape_blocks)} channel(s)")
        bs_id = fbx.add_blendshape_deformer(mesh_name)
        # BlendShape -> Geometry (OO)
        fbx.connect_oo(bs_id, geom_id)
        for sb_name, sb_indices, sb_deltas in shape_blocks:
            ch_id = fbx.add_blendshape_channel(sb_name)
            # BlendShapeChannel -> BlendShape (OO)
            fbx.connect_oo(ch_id, bs_id)
            # Shape geometry (target deltas) -> BlendShapeChannel (OO)
            shape_id = fbx.add_shape_geometry(sb_name, sb_indices, sb_deltas)
            fbx.connect_oo(shape_id, ch_id)

    # ---- Bind pose ----
    if options.emit_bind_pose:
        log("  bind pose: emitting")
        pose_nodes: List[Tuple[int, Mat4]] = []
        # Mesh node (identity transform at origin)
        pose_nodes.append((mesh_model_id, mat4_identity()))
        # Bone nodes (their world bind matrices)
        for info in bone_infos:
            pose_nodes.append((info.fbx_model_id, info.transform_link))
        fbx.add_bind_pose(f"Pose::{mesh_name}", pose_nodes)

    # ---- Render + write ----
    log("  serializing FBX binary...")
    data = fbx.render()
    with open(out_path, "wb") as fh:
        fh.write(data)
    log(f"  wrote {len(data)} bytes -> {out_path}")
    return out_path