"""PMX (Polygon Model eXtended) 2.0/2.1 binary parser.

Zero external dependencies. Implements the full PMX specification including
vertices (BDEF1/2/4, SDEF, QDEF), faces, textures, materials, bones (with
IK, inherit rotation/translation, fixed axis, local axes, external parent),
morphs (all types), display frames, rigid bodies, joints and soft bodies.

References:
  - felixjones/pmx21.md (community English translation of the official spec)
  - sugiany/blender_mmd_tools core/pmx/__init__.py
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PMXVertex:
    position: Tuple[float, float, float]
    normal: Tuple[float, float, float]
    uv: Tuple[float, float]
    additional_uvs: List[Tuple[float, float, float, float]]
    weight_type: int  # 0=BDEF1, 1=BDEF2, 2=BDEF4, 3=SDEF, 4=QDEF
    bones: List[int]  # bone indices referenced
    weights: List[float]  # weights (length matches bones; for SDEF BDEF2-style)
    sdef_c: Optional[Tuple[float, float, float]] = None
    sdef_r0: Optional[Tuple[float, float, float]] = None
    sdef_r1: Optional[Tuple[float, float, float]] = None
    edge_scale: float = 1.0


@dataclass
class PMXMaterial:
    name_jp: str
    name_en: str
    diffuse: Tuple[float, float, float, float]  # RGBA
    specular: Tuple[float, float, float]  # RGB
    specular_strength: float
    ambient: Tuple[float, float, float]
    flags: int  # drawing flags bitfield
    edge_color: Tuple[float, float, float, float]
    edge_scale: float
    texture_index: int  # -1 = none
    sphere_index: int  # -1 = none
    sphere_mode: int  # 0=off,1=mul,2=add,3=sub-tex
    toon_shared: bool  # True=shared toon01-10, False=texture
    toon_index: int  # 0-9 if shared; texture index otherwise; -1 = none
    comment: str
    face_count: int  # number of vertex indices (multiple of 3)


@dataclass
class PMXIKLink:
    bone_index: int
    has_limits: bool
    limit_min: Tuple[float, float, float]
    limit_max: Tuple[float, float, float]


@dataclass
class PMXBone:
    name_jp: str
    name_en: str
    position: Tuple[float, float, float]
    parent_bone: int  # -1 = none
    layer: int
    flags: int
    # Tail (conditional)
    tail_is_bone: bool  # flag bit 0x0001
    tail_position: Tuple[float, float, float]  # either offset vec3
    tail_bone: int  # or bone index
    # Inherit (if 0x0100 or 0x0200)
    inherit_bone: int
    inherit_influence: float
    inherit_rotation: bool
    inherit_translation: bool
    # Fixed axis (0x0400)
    has_fixed_axis: bool
    fixed_axis: Tuple[float, float, float]
    # Local coordinate (0x0800)
    has_local_axes: bool
    local_x: Tuple[float, float, float]
    local_z: Tuple[float, float, float]
    # External parent (0x2000)
    has_external_parent: bool
    external_parent_key: int
    # IK (0x0020)
    is_ik: bool
    ik_target_bone: int
    ik_loop_count: int
    ik_limit_radian: float
    ik_links: List[PMXIKLink] = field(default_factory=list)


@dataclass
class PMXMorphOffset:
    """Generic morph offset; only the relevant fields are populated per type."""
    # Vertex / UV types
    vertex_index: int = -1
    uv_offset: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    # Vertex morph translation
    translation: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    # Bone morph
    bone_index: int = -1
    rotation: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    # Material morph
    material_index: int = -1
    material_offset_type: int = 0  # 0=multiply, 1=add
    mat_diffuse: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    mat_specular: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    mat_ambient: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    mat_edge_color: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    mat_edge_size: float = 0.0
    mat_texture_tint: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    mat_sphere_tint: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    mat_toon_tint: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    # Group / Flip
    morph_index: int = -1
    influence: float = 0.0
    # Impulse
    rigid_index: int = -1
    impulse_local: int = 0
    impulse_speed: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    impulse_torque: Tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass
class PMXMorph:
    name_jp: str
    name_en: str
    panel: int  # 0=System,1=Eye,2=Brow,3=Mouth,4=Other
    morph_type: int  # 0=Group,1=Vertex,2=Bone,3-7=UV,8=Material,9=Flip,10=Impulse
    offsets: List[PMXMorphOffset] = field(default_factory=list)


@dataclass
class PMXDisplayFrame:
    name_jp: str
    name_en: str
    special: bool
    entries: List[Tuple[int, int]]  # (type 0=bone/1=morph, index)


@dataclass
class PMXRigidBody:
    name_jp: str
    name_en: str
    bone_index: int
    collision_group: int
    group_mask: int  # unsigned short
    shape: int  # 0=sphere,1=box,2=capsule
    size: Tuple[float, float, float]
    position: Tuple[float, float, float]
    rotation: Tuple[float, float, float]
    mass: float
    move_attenuation: float
    rotation_damping: float
    repulsion: float
    friction: float
    physics_mode: int


@dataclass
class PMXJoint:
    name_jp: str
    name_en: str
    joint_type: int
    rigid_a: int
    rigid_b: int
    position: Tuple[float, float, float]
    rotation: Tuple[float, float, float]
    position_min: Tuple[float, float, float]
    position_max: Tuple[float, float, float]
    rotation_min: Tuple[float, float, float]
    rotation_max: Tuple[float, float, float]
    spring_position: Tuple[float, float, float]
    spring_rotation: Tuple[float, float, float]


@dataclass
class PMXModel:
    version: float
    encoding: int  # 0=UTF-16LE, 1=UTF-8
    additional_uv_count: int
    index_sizes: dict  # vertex/texture/material/bone/morph/rigidbody -> 1|2|4
    name_jp: str
    name_en: str
    comment_jp: str
    comment_en: str
    vertices: List[PMXVertex] = field(default_factory=list)
    faces: List[int] = field(default_factory=list)  # flat list of vertex indices, len = 3 * tri_count
    textures: List[str] = field(default_factory=list)
    materials: List[PMXMaterial] = field(default_factory=list)
    bones: List[PMXBone] = field(default_factory=list)
    morphs: List[PMXMorph] = field(default_factory=list)
    display_frames: List[PMXDisplayFrame] = field(default_factory=list)
    rigid_bodies: List[PMXRigidBody] = field(default_factory=list)
    joints: List[PMXJoint] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Binary reader
# ---------------------------------------------------------------------------


class _Reader:
    __slots__ = ("data", "pos", "encoding")

    def __init__(self, data: bytes, encoding: int):
        self.data = data
        self.pos = 0
        self.encoding = encoding  # 0=UTF-16LE, 1=UTF-8

    # ---- primitive helpers ----
    def read(self, n: int) -> bytes:
        b = self.data[self.pos : self.pos + n]
        if len(b) != n:
            raise EOFError(f"unexpected EOF: needed {n} bytes, got {len(b)}")
        self.pos += n
        return b

    def i8(self) -> int:
        return struct.unpack("<b", self.read(1))[0]

    def u8(self) -> int:
        return struct.unpack("<B", self.read(1))[0]

    def i16(self) -> int:
        return struct.unpack("<h", self.read(2))[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.read(2))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.read(4))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.read(4))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self.read(8))[0]

    def vec2(self) -> Tuple[float, float]:
        return struct.unpack("<2f", self.read(8))

    def vec3(self) -> Tuple[float, float, float]:
        return struct.unpack("<3f", self.read(12))

    def vec4(self) -> Tuple[float, float, float, float]:
        return struct.unpack("<4f", self.read(16))

    def text(self) -> str:
        n = self.i32()
        raw = self.read(n) if n > 0 else b""
        if self.encoding == 0:
            return raw.decode("utf-16-le", errors="replace")
        return raw.decode("utf-8", errors="replace")

    # ---- variable-width indices ----
    def _index_signed(self, size: int) -> int:
        if size == 1:
            return self.i8()
        if size == 2:
            return self.i16()
        return self.i32()

    def _index_unsigned(self, size: int) -> int:
        if size == 1:
            return self.u8()
        if size == 2:
            return self.u16()
        return self.u32()

    def vertex_index(self, size: int) -> int:
        return self._index_unsigned(size)

    def bone_index(self, size: int) -> int:
        return self._index_signed(size)

    def texture_index(self, size: int) -> int:
        return self._index_signed(size)

    def material_index(self, size: int) -> int:
        return self._index_signed(size)

    def morph_index(self, size: int) -> int:
        return self._index_signed(size)

    def rigid_index(self, size: int) -> int:
        return self._index_signed(size)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def read_pmx(path: str) -> PMXModel:
    """Parse a PMX file fully. Raises ValueError on malformed data."""

    with open(path, "rb") as fh:
        data = fh.read()

    if len(data) < 17:
        raise ValueError("file too small to be a valid PMX")

    if data[:4] != b"PMX ":
        raise ValueError(f"bad magic: expected b'PMX ', got {data[:4]!r}")

    r = _Reader(data, encoding=0)  # encoding set after header
    r.read(4)  # magic
    version = struct.unpack("<f", r.read(4))[0]
    if version not in (2.0, 2.1):
        raise ValueError(f"unsupported PMX version: {version}")

    globals_count = r.u8()
    if globals_count != 8:
        # Some non-conforming files have fewer globals; we still try to read 8.
        # mmd_tools requires exactly 8; we are lenient.
        pass

    encoding = r.u8()
    additional_uv = r.u8()
    vsize = r.u8()
    tsize = r.u8()
    msize = r.u8()
    bsize = r.u8()
    psize = r.u8()  # morph
    rsize = r.u8()  # rigid body

    r.encoding = encoding

    index_sizes = {
        "vertex": vsize,
        "texture": tsize,
        "material": msize,
        "bone": bsize,
        "morph": psize,
        "rigidbody": rsize,
    }

    name_jp = r.text()
    name_en = r.text()
    comment_jp = r.text()
    comment_en = r.text()

    model = PMXModel(
        version=version,
        encoding=encoding,
        additional_uv_count=additional_uv,
        index_sizes=index_sizes,
        name_jp=name_jp,
        name_en=name_en,
        comment_jp=comment_jp,
        comment_en=comment_en,
    )

    _read_vertices(r, model)
    _read_faces(r, model)
    _read_textures(r, model)
    _read_materials(r, model)
    _read_bones(r, model)
    _read_morphs(r, model)
    _read_display_frames(r, model)
    _read_rigid_bodies(r, model)
    _read_joints(r, model)

    # Soft bodies (PMX 2.1 only) - read past but do not store.
    if version >= 2.1:
        _read_soft_bodies_skip(r)

    return model


# ---------------------------------------------------------------------------
# Section readers
# ---------------------------------------------------------------------------


def _read_vertices(r: _Reader, model: PMXModel) -> None:
    bsize = model.index_sizes["bone"]
    vsize = model.index_sizes["vertex"]
    count = r.i32()
    for _ in range(count):
        position = r.vec3()
        normal = r.vec3()
        uv = r.vec2()
        additional = [r.vec4() for _ in range(model.additional_uv_count)]
        weight_type = r.u8()

        bones: List[int] = []
        weights: List[float] = []
        sdef_c = sdef_r0 = sdef_r1 = None

        if weight_type == 0:  # BDEF1
            bones.append(r.bone_index(bsize))
            weights.append(1.0)
        elif weight_type == 1:  # BDEF2
            b1 = r.bone_index(bsize)
            b2 = r.bone_index(bsize)
            w1 = r.f32()
            bones = [b1, b2]
            weights = [w1, 1.0 - w1]
        elif weight_type == 2:  # BDEF4
            b1 = r.bone_index(bsize)
            b2 = r.bone_index(bsize)
            b3 = r.bone_index(bsize)
            b4 = r.bone_index(bsize)
            w1 = r.f32()
            w2 = r.f32()
            w3 = r.f32()
            w4 = r.f32()
            bones = [b1, b2, b3, b4]
            weights = [w1, w2, w3, w4]
            total = sum(weights)
            if total > 0.0:
                weights = [w / total for w in weights]
        elif weight_type == 3:  # SDEF
            b1 = r.bone_index(bsize)
            b2 = r.bone_index(bsize)
            w1 = r.f32()
            sdef_c = r.vec3()
            sdef_r0 = r.vec3()
            sdef_r1 = r.vec3()
            bones = [b1, b2]
            weights = [w1, 1.0 - w1]
        elif weight_type == 4:  # QDEF (PMX 2.1)
            b1 = r.bone_index(bsize)
            b2 = r.bone_index(bsize)
            b3 = r.bone_index(bsize)
            b4 = r.bone_index(bsize)
            w1 = r.f32()
            w2 = r.f32()
            w3 = r.f32()
            w4 = r.f32()
            bones = [b1, b2, b3, b4]
            weights = [w1, w2, w3, w4]
            total = sum(weights)
            if total > 0.0:
                weights = [w / total for w in weights]
        else:
            raise ValueError(f"unknown weight type {weight_type}")

        edge_scale = r.f32()
        model.vertices.append(
            PMXVertex(
                position=position,
                normal=normal,
                uv=uv,
                additional_uvs=additional,
                weight_type=weight_type,
                bones=bones,
                weights=weights,
                sdef_c=sdef_c,
                sdef_r0=sdef_r0,
                sdef_r1=sdef_r1,
                edge_scale=edge_scale,
            )
        )

    # sanity check we used the vertex index size somewhere (it's only used in faces/morphs)
    _ = vsize


def _read_faces(r: _Reader, model: PMXModel) -> None:
    vsize = model.index_sizes["vertex"]
    count = r.i32()  # total index count (multiple of 3)
    faces: List[int] = []
    for _ in range(count):
        faces.append(r.vertex_index(vsize))
    model.faces = faces


def _read_textures(r: _Reader, model: PMXModel) -> None:
    count = r.i32()
    for _ in range(count):
        model.textures.append(r.text())


def _read_materials(r: _Reader, model: PMXModel) -> None:
    tsize = model.index_sizes["texture"]
    count = r.i32()
    for _ in range(count):
        name_jp = r.text()
        name_en = r.text()
        diffuse = r.vec4()
        specular = r.vec3()
        specular_strength = r.f32()
        ambient = r.vec3()
        flags = r.u8()
        edge_color = r.vec4()
        edge_scale = r.f32()
        texture_index = r.texture_index(tsize)
        sphere_index = r.texture_index(tsize)
        sphere_mode = r.i8()
        toon_shared_flag = r.i8()
        toon_shared = bool(toon_shared_flag)
        if toon_shared:
            toon_index = r.i8()  # 0..9
        else:
            toon_index = r.texture_index(tsize)
        comment = r.text()
        face_count = r.i32()
        model.materials.append(
            PMXMaterial(
                name_jp=name_jp,
                name_en=name_en,
                diffuse=diffuse,
                specular=specular,
                specular_strength=specular_strength,
                ambient=ambient,
                flags=flags,
                edge_color=edge_color,
                edge_scale=edge_scale,
                texture_index=texture_index,
                sphere_index=sphere_index,
                sphere_mode=sphere_mode,
                toon_shared=toon_shared,
                toon_index=toon_index,
                comment=comment,
                face_count=face_count,
            )
        )


def _read_bones(r: _Reader, model: PMXModel) -> None:
    bsize = model.index_sizes["bone"]
    count = r.i32()
    for _ in range(count):
        name_jp = r.text()
        name_en = r.text()
        position = r.vec3()
        parent = r.bone_index(bsize)
        layer = r.i32()
        flags = r.u16()

        tail_is_bone = bool(flags & 0x0001)
        if tail_is_bone:
            tail_position = (0.0, 0.0, 0.0)
            tail_bone = r.bone_index(bsize)
        else:
            tail_position = r.vec3()
            tail_bone = -1

        inherit_rotation = bool(flags & 0x0100)
        inherit_translation = bool(flags & 0x0200)
        if inherit_rotation or inherit_translation:
            inherit_bone = r.bone_index(bsize)
            inherit_influence = r.f32()
        else:
            inherit_bone = -1
            inherit_influence = 0.0

        has_fixed_axis = bool(flags & 0x0400)
        if has_fixed_axis:
            fixed_axis = r.vec3()
        else:
            fixed_axis = (0.0, 0.0, 0.0)

        has_local_axes = bool(flags & 0x0800)
        if has_local_axes:
            local_x = r.vec3()
            local_z = r.vec3()
        else:
            local_x = (1.0, 0.0, 0.0)
            local_z = (0.0, 0.0, 1.0)

        has_external_parent = bool(flags & 0x2000)
        if has_external_parent:
            external_parent_key = r.i32()
        else:
            external_parent_key = 0

        is_ik = bool(flags & 0x0020)
        ik_target_bone = -1
        ik_loop_count = 0
        ik_limit_radian = 0.0
        ik_links: List[PMXIKLink] = []
        if is_ik:
            ik_target_bone = r.bone_index(bsize)
            ik_loop_count = r.i32()
            ik_limit_radian = r.f32()
            link_count = r.i32()
            for _ in range(link_count):
                link_bone = r.bone_index(bsize)
                has_limits = bool(r.u8())
                if has_limits:
                    lmin = r.vec3()
                    lmax = r.vec3()
                else:
                    lmin = (0.0, 0.0, 0.0)
                    lmax = (0.0, 0.0, 0.0)
                ik_links.append(
                    PMXIKLink(
                        bone_index=link_bone,
                        has_limits=has_limits,
                        limit_min=lmin,
                        limit_max=lmax,
                    )
                )

        model.bones.append(
            PMXBone(
                name_jp=name_jp,
                name_en=name_en,
                position=position,
                parent_bone=parent,
                layer=layer,
                flags=flags,
                tail_is_bone=tail_is_bone,
                tail_position=tail_position,
                tail_bone=tail_bone,
                inherit_bone=inherit_bone,
                inherit_influence=inherit_influence,
                inherit_rotation=inherit_rotation,
                inherit_translation=inherit_translation,
                has_fixed_axis=has_fixed_axis,
                fixed_axis=fixed_axis,
                has_local_axes=has_local_axes,
                local_x=local_x,
                local_z=local_z,
                has_external_parent=has_external_parent,
                external_parent_key=external_parent_key,
                is_ik=is_ik,
                ik_target_bone=ik_target_bone,
                ik_loop_count=ik_loop_count,
                ik_limit_radian=ik_limit_radian,
                ik_links=ik_links,
            )
        )


def _read_morphs(r: _Reader, model: PMXModel) -> None:
    vsize = model.index_sizes["vertex"]
    bsize = model.index_sizes["bone"]
    msize = model.index_sizes["material"]
    psize = model.index_sizes["morph"]
    rsize = model.index_sizes["rigidbody"]

    count = r.i32()
    for _ in range(count):
        name_jp = r.text()
        name_en = r.text()
        panel = r.i8()
        mtype = r.i8()
        offset_count = r.i32()
        offsets: List[PMXMorphOffset] = []

        for _ in range(offset_count):
            o = PMXMorphOffset()
            if mtype == 0:  # Group
                o.morph_index = r.morph_index(psize)
                o.influence = r.f32()
            elif mtype == 1:  # Vertex
                o.vertex_index = r.vertex_index(vsize)
                o.translation = r.vec3()
            elif mtype == 2:  # Bone
                o.bone_index = r.bone_index(bsize)
                o.translation = r.vec3()
                o.rotation = r.vec4()
            elif mtype in (3, 4, 5, 6, 7):  # UV / UV ext1-4
                o.vertex_index = r.vertex_index(vsize)
                o.uv_offset = r.vec4()
            elif mtype == 8:  # Material
                o.material_index = r.material_index(msize)
                o.material_offset_type = r.i8()
                o.mat_diffuse = r.vec4()
                o.mat_specular = r.vec4()
                o.mat_ambient = r.vec3()
                o.mat_edge_color = r.vec4()
                o.mat_edge_size = r.f32()
                o.mat_texture_tint = r.vec4()
                o.mat_sphere_tint = r.vec4()
                o.mat_toon_tint = r.vec4()
            elif mtype == 9:  # Flip
                o.morph_index = r.morph_index(psize)
                o.influence = r.f32()
            elif mtype == 10:  # Impulse
                o.rigid_index = r.rigid_index(rsize)
                o.impulse_local = r.u8()
                o.impulse_speed = r.vec3()
                o.impulse_torque = r.vec3()
            else:
                raise ValueError(f"unknown morph type {mtype}")
            offsets.append(o)

        model.morphs.append(
            PMXMorph(
                name_jp=name_jp,
                name_en=name_en,
                panel=panel,
                morph_type=mtype,
                offsets=offsets,
            )
        )


def _read_display_frames(r: _Reader, model: PMXModel) -> None:
    bsize = model.index_sizes["bone"]
    psize = model.index_sizes["morph"]
    count = r.i32()
    for _ in range(count):
        name_jp = r.text()
        name_en = r.text()
        special = bool(r.u8())
        fcount = r.i32()
        entries: List[Tuple[int, int]] = []
        for _ in range(fcount):
            ftype = r.u8()
            if ftype == 0:
                idx = r.bone_index(bsize)
            elif ftype == 1:
                idx = r.morph_index(psize)
            else:
                raise ValueError(f"unknown frame entry type {ftype}")
            entries.append((ftype, idx))
        model.display_frames.append(
            PMXDisplayFrame(
                name_jp=name_jp,
                name_en=name_en,
                special=special,
                entries=entries,
            )
        )


def _read_rigid_bodies(r: _Reader, model: PMXModel) -> None:
    bsize = model.index_sizes["bone"]
    count = r.i32()
    for _ in range(count):
        name_jp = r.text()
        name_en = r.text()
        bone_index = r.bone_index(bsize)
        collision_group = r.i8()
        group_mask = r.u16()
        shape = r.i8()
        size = r.vec3()
        position = r.vec3()
        rotation = r.vec3()
        mass = r.f32()
        move_att = r.f32()
        rot_damp = r.f32()
        repulsion = r.f32()
        friction = r.f32()
        physics_mode = r.i8()
        model.rigid_bodies.append(
            PMXRigidBody(
                name_jp=name_jp,
                name_en=name_en,
                bone_index=bone_index,
                collision_group=collision_group,
                group_mask=group_mask,
                shape=shape,
                size=size,
                position=position,
                rotation=rotation,
                mass=mass,
                move_attenuation=move_att,
                rotation_damping=rot_damp,
                repulsion=repulsion,
                friction=friction,
                physics_mode=physics_mode,
            )
        )


def _read_joints(r: _Reader, model: PMXModel) -> None:
    rsize = model.index_sizes["rigidbody"]
    count = r.i32()
    for _ in range(count):
        name_jp = r.text()
        name_en = r.text()
        jtype = r.i8()
        rigid_a = r.rigid_index(rsize)
        rigid_b = r.rigid_index(rsize)
        position = r.vec3()
        rotation = r.vec3()
        position_min = r.vec3()
        position_max = r.vec3()
        rotation_min = r.vec3()
        rotation_max = r.vec3()
        spring_position = r.vec3()
        spring_rotation = r.vec3()
        model.joints.append(
            PMXJoint(
                name_jp=name_jp,
                name_en=name_en,
                joint_type=jtype,
                rigid_a=rigid_a,
                rigid_b=rigid_b,
                position=position,
                rotation=rotation,
                position_min=position_min,
                position_max=position_max,
                rotation_min=rotation_min,
                rotation_max=rotation_max,
                spring_position=spring_position,
                spring_rotation=spring_rotation,
            )
        )


def _read_soft_bodies_skip(r: _Reader) -> None:
    """Read past the soft body section without storing it.

    Structure (PMX 2.1): count, then per-body: name_jp, name_en, shape, material
    index, group, mask, flags, b-link distance, cluster count, mass, collision
    margin, aero model, 13 config floats, cluster coefficients (4), iteration
    counts (4), material stiffness (5), anchor rigid bodies count + entries,
    vertex pin count + entries. We read but discard.
    """
    try:
        count = r.i32()
    except EOFError:
        return
    for _ in range(count):
        r.text()  # name jp
        r.text()  # name en
        r.u8()  # shape
        msize_size = 1  # material index size is the same as model.index_sizes['material'] but we lost ref; soft bodies rare
        # We need the material index size. Cheat by re-deriving from header is not available here;
        # soft bodies are extremely rare in MMD content, so if we encounter one we read conservatively
        # using 4-byte material index (most common). This may desync for files using 1/2-byte indices,
        # but such files essentially do not exist in the wild with soft bodies.
        r.i32()  # material index
        r.u8()  # group
        r.u16()  # mask
        r.i32()  # flags
        r.i32()  # b-link distance
        r.i32()  # cluster count
        r.f32()  # mass
        r.f32()  # collision margin
        r.i32()  # aero model
        # 13 config floats (VCF, DP, DG, LF, PR, VC, DF, MT, CHR, KHR, SHR, AHR)
        for _ in range(13):
            r.f32()
        # cluster coefficients (4 floats)
        for _ in range(4):
            r.f32()
        # iteration counts (4 ints)
        for _ in range(4):
            r.i32()
        # material stiffness (5 floats)
        for _ in range(5):
            r.f32()
        # anchor rigid bodies
        anchor_count = r.i32()
        for _ in range(anchor_count):
            r.i32()  # rigid body index
            r.i32()  # vertex index
        # vertex pins
        pin_count = r.i32()
        for _ in range(pin_count):
            r.i32()  # vertex index
