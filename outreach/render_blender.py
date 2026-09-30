"""Photoreal product shot of the QR sign, rendered with Blender Cycles.

One card stands in a black base, a second lies flat in front of it. Instead
of rendering every business, this renders the scene ONCE with the card face
pure white and once pure black (same noise seed), and records where each
card face lands in the image. Because light on the face is linear in its
colour, any face can then be composited in afterwards:

    pixel = black + face_colour * (white - black)      (in linear light)

which is exact for the face, keeps occlusion by the base, and takes a
fraction of a second per business (outreach.py does that part).

Run either way (outreach.py picks whichever is available):
    python render_blender.py spec.json                      # with `pip install bpy`
    blender -b --factory-startup -P render_blender.py -- spec.json

spec.json: {"samples": 256, "out_dir": "data/scene"}
Writes white.npy, black.npy (linear RGB, float16, top row first) and
corners.json (pixel corners TL, TR, BR, BL of each card face).
"""

import json
import math
import os
import sys

import bpy  # must come first: the pip `bpy` module provides bmesh
import bmesh
from mathutils import Vector

MM = 0.001
CARD_W, CARD_H, CARD_T, CARD_R = 100 * MM, 140 * MM, 3 * MM, 6 * MM
BASE_W, BASE_D, BASE_H, BASE_R = 130 * MM, 40 * MM, 14 * MM, 5 * MM
SINK = 9 * MM          # how deep the standing card sits in the base's slot
LAYER = 0.2 * MM       # printed layer height, shown as faint lines on the edges

BACKDROP = (0.40, 0.355, 0.30)   # warm taupe (linear), contrasts white and black


def srgb(c):
    return tuple(((v / 255 + 0.055) / 1.055) ** 2.4 if v > 10 else v / 255 / 12.92
                 for v in c)


def rounded_outline(w, h, r, seg=12):
    pts = []
    for cx, cy, a0 in ((w / 2 - r, -h / 2 + r, -90), (w / 2 - r, h / 2 - r, 0),
                       (-w / 2 + r, h / 2 - r, 90), (-w / 2 + r, -h / 2 + r, 180)):
        for i in range(seg + 1):
            a = math.radians(a0 + 90 * i / seg)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def rounded_slab(name, w, h, t, r, bevel):
    """Rounded rectangle w x h extruded t along local Z, centred on the
    origin, front face at +Z, with a small bevel on the outer edges."""
    bm = bmesh.new()
    pts = rounded_outline(w, h, r)
    top = [bm.verts.new((x, y, t / 2)) for x, y in pts]
    bot = [bm.verts.new((x, y, -t / 2)) for x, y in pts]
    bm.faces.new(top)
    bm.faces.new(list(reversed(bot)))
    n = len(pts)
    for i in range(n):
        f = bm.faces.new((bot[i], bot[(i + 1) % n], top[(i + 1) % n], top[i]))
        f.smooth = True
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    mod = obj.modifiers.new("bevel", "BEVEL")
    mod.width = bevel
    mod.segments = 4
    mod.limit_method = "ANGLE"
    mod.angle_limit = math.radians(40)
    mod.harden_normals = False
    return obj


def pla_material(name, color, roughness=0.42, face_image=None, card=None):
    """Matte PLA with faint layer lines. With `face_image`, the image is
    printed on the local +Z face, mapped over the card's full w x h."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nodes, links = nt.nodes, nt.links
    nodes.clear()
    out = nodes.new("ShaderNodeOutputMaterial")
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.inputs["Roughness"].default_value = roughness
    # Printed PLA is fairly matte; a strong sheen would grey out dark print.
    bsdf.inputs["Specular IOR Level"].default_value = 0.25
    bsdf.inputs["Base Color"].default_value = (*color, 1)
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    tc = nodes.new("ShaderNodeTexCoord")
    sep = nodes.new("ShaderNodeSeparateXYZ")
    links.new(tc.outputs["Object"], sep.inputs["Vector"])

    # Layer lines on the sides: sin(z / layer), fading out on the flat faces.
    zmul = nodes.new("ShaderNodeMath"); zmul.operation = "MULTIPLY"
    zmul.inputs[1].default_value = 2 * math.pi / LAYER
    links.new(sep.outputs["Z"], zmul.inputs[0])
    zsin = nodes.new("ShaderNodeMath"); zsin.operation = "SINE"
    links.new(zmul.outputs[0], zsin.inputs[0])

    nsep = nodes.new("ShaderNodeSeparateXYZ")
    links.new(tc.outputs["Normal"], nsep.inputs["Vector"])
    nz = nodes.new("ShaderNodeMath"); nz.operation = "ABSOLUTE"
    links.new(nsep.outputs["Z"], nz.inputs[0])
    side = nodes.new("ShaderNodeMath"); side.operation = "LESS_THAN"
    side.inputs[1].default_value = 0.5
    links.new(nz.outputs[0], side.inputs[0])
    lines = nodes.new("ShaderNodeMath"); lines.operation = "MULTIPLY"
    links.new(zsin.outputs[0], lines.inputs[0])
    links.new(side.outputs[0], lines.inputs[1])

    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.35
    bump.inputs["Distance"].default_value = 0.00004
    links.new(lines.outputs[0], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])

    if face_image is not None:
        w, h = card
        mapping = nodes.new("ShaderNodeMapping")
        mapping.vector_type = "POINT"
        mapping.inputs["Location"].default_value = (0.5, 0.5, 0)
        mapping.inputs["Scale"].default_value = (1 / w, 1 / h, 1)
        links.new(tc.outputs["Object"], mapping.inputs["Vector"])
        tex = nodes.new("ShaderNodeTexImage")
        tex.name = "face"
        tex.image = face_image
        tex.extension = "CLIP"
        tex.interpolation = "Cubic"
        links.new(mapping.outputs["Vector"], tex.inputs["Vector"])

        front = nodes.new("ShaderNodeMath"); front.operation = "GREATER_THAN"
        front.inputs[1].default_value = 0.5
        links.new(nsep.outputs["Z"], front.inputs[0])
        fac = nodes.new("ShaderNodeMath"); fac.operation = "MULTIPLY"
        links.new(front.outputs[0], fac.inputs[0])
        links.new(tex.outputs["Alpha"], fac.inputs[1])

        mix = nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.inputs["A"].default_value = (*color, 1)
        links.new(fac.outputs[0], mix.inputs["Factor"])
        links.new(tex.outputs["Color"], mix.inputs["B"])
        links.new(mix.outputs["Result"], bsdf.inputs["Base Color"])
    return mat


def backdrop():
    """A photo-studio sweep: flat floor curving up into a back wall."""
    bm = bmesh.new()
    prof = [(y, 0.0) for y in (-1.5, -0.5, 0.0, 0.12)]
    r = 0.35
    for i in range(1, 17):
        a = math.radians(90 * i / 16)
        prof.append((0.12 + r * math.sin(a), r - r * math.cos(a)))
    prof.append((0.12 + r, 1.5))
    left = [bm.verts.new((-2.0, y, z)) for y, z in prof]
    right = [bm.verts.new((2.0, y, z)) for y, z in prof]
    for i in range(len(prof) - 1):
        f = bm.faces.new((left[i], right[i], right[i + 1], left[i + 1]))
        f.smooth = True
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new("backdrop")
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new("backdrop", mesh)
    bpy.context.collection.objects.link(obj)
    for f in obj.data.polygons:
        if f.normal.z < 0:
            obj.data.flip_normals()
            break
    mat = bpy.data.materials.new("backdrop")
    mat.use_nodes = True
    b = mat.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*BACKDROP, 1)
    b.inputs["Roughness"].default_value = 0.95
    obj.data.materials.append(mat)


def area_light(name, loc, target, size, power, color=(1, 1, 1)):
    data = bpy.data.lights.new(name, "AREA")
    data.shape = "DISK"
    data.size = size
    data.energy = power
    data.color = color
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.location = loc
    direction = Vector(target) - Vector(loc)
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def build_scene(samples):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene

    face_img = bpy.data.images.new("face", 8, 8, alpha=True)
    white = srgb((238, 236, 231))
    black = srgb((28, 28, 30))
    card_mat = pla_material("card", white, 0.62, face_img, (CARD_W, CARD_H))
    base_mat = pla_material("base", black, 0.5)

    # Standing assembly: base with a slot, card sunk into it.
    stand = bpy.data.objects.new("stand", None)
    bpy.context.collection.objects.link(stand)
    stand.location = (-0.036, 0.03, 0)
    stand.rotation_euler = (0, 0, math.radians(-20))

    base = rounded_slab("base", BASE_W, BASE_D, BASE_H, BASE_R, 1.0 * MM)
    base.parent = stand
    base.location = (0, 0, BASE_H / 2)
    base.data.materials.append(base_mat)

    bpy.ops.mesh.primitive_cube_add(size=1)
    cutter = bpy.context.active_object
    cutter.name = "slot"
    cutter.parent = stand
    cutter.scale = (CARD_W + 0.8 * MM, CARD_T + 0.8 * MM, 2 * SINK)
    cutter.location = (0, 0, BASE_H)
    cutter.hide_render = True
    cutter.hide_viewport = True
    boolean = base.modifiers.new("slot", "BOOLEAN")
    boolean.operation = "DIFFERENCE"
    boolean.object = cutter
    # Cut before bevelling so the slot edges get rounded too.
    bpy.context.view_layer.objects.active = base
    bpy.ops.object.modifier_move_to_index(modifier="slot", index=0)

    card = rounded_slab("card_standing", CARD_W, CARD_H, CARD_T, CARD_R, 0.6 * MM)
    card.parent = stand
    card.rotation_euler = (math.radians(90), 0, 0)   # local +Z (face) -> -Y
    card.location = (0, 0, BASE_H - SINK + CARD_H / 2)
    card.data.materials.append(card_mat)

    flat = rounded_slab("card_flat", CARD_W, CARD_H, CARD_T, CARD_R, 0.6 * MM)
    flat.location = (0.066, -0.068, CARD_T / 2)
    flat.rotation_euler = (0, 0, math.radians(14))
    flat.data.materials.append(card_mat)

    backdrop()

    # Soft studio light: big key from the left, fill from the right, a rim
    # from behind to separate the parts from the backdrop.
    area_light("key", (-0.42, -0.38, 0.52), (0.0, 0.0, 0.04), 0.55, 14)
    area_light("fill", (0.5, -0.28, 0.22), (0.0, 0.0, 0.05), 0.5, 3.5,
               (1.0, 0.97, 0.93))
    area_light("rim", (0.1, 0.55, 0.45), (0.0, 0.0, 0.08), 0.4, 7)

    world = bpy.data.worlds.new("world")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs["Color"].default_value = (*BACKDROP, 1)
    bg.inputs["Strength"].default_value = 0.12

    cam_data = bpy.data.cameras.new("camera")
    cam_data.lens = 58
    cam_data.dof.use_dof = True
    # Mild depth of field: composited faces are sharp, so keep blur small.
    cam_data.dof.aperture_fstop = 16
    cam = bpy.data.objects.new("camera", cam_data)
    bpy.context.collection.objects.link(cam)
    cam.location = (0.23, -0.56, 0.27)
    target = Vector((0.016, -0.012, 0.042))
    cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
    cam_data.dof.focus_distance = (Vector((-0.02, 0.02, 0.07)) - cam.location).length
    scene.camera = cam

    scene.render.engine = "CYCLES"
    scene.cycles.samples = samples
    scene.cycles.use_denoising = True
    scene.render.resolution_x = 1600
    scene.render.resolution_y = 1200
    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.quality = 92
    # AgX handles the white card without clipping; Punchy keeps brand colours.
    scene.view_settings.view_transform = "AgX"
    try:
        scene.view_settings.look = "AgX - Punchy"
    except TypeError:
        pass
    _use_gpu(scene)
    return face_img


def _use_gpu(scene):
    """Metal on Apple Silicon, CUDA/OptiX/HIP elsewhere, CPU as a fallback."""
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except KeyError:
        return
    for kind in ("METAL", "OPTIX", "CUDA", "HIP", "ONEAPI"):
        try:
            prefs.compute_device_type = kind
        except TypeError:
            continue
        prefs.get_devices()
        gpus = [d for d in prefs.devices if d.type != "CPU"]
        if gpus:
            for d in prefs.devices:
                d.use = True
            scene.cycles.device = "GPU"
            return


def face_corners(obj):
    """Pixel positions of the card's front rectangle (TL, TR, BR, BL as the
    face image is oriented: local +Y is the top)."""
    from bpy_extras.object_utils import world_to_camera_view
    scene = bpy.context.scene
    w, h = scene.render.resolution_x, scene.render.resolution_y
    bpy.context.view_layer.update()
    out = []
    for x, y in ((-CARD_W / 2, CARD_H / 2), (CARD_W / 2, CARD_H / 2),
                 (CARD_W / 2, -CARD_H / 2), (-CARD_W / 2, -CARD_H / 2)):
        co = world_to_camera_view(scene, scene.camera,
                                  obj.matrix_world @ Vector((x, y, CARD_T / 2)))
        out.append((co.x * w, (1 - co.y) * h))
    return out


def render_linear(path):
    """Renders and returns the image as linear float RGB, top row first."""
    import numpy as np
    scene = bpy.context.scene
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(path)
    w, h = img.size
    px = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    bpy.data.images.remove(img)
    os.remove(path)
    return px.reshape(h, w, 4)[::-1, :, :3]


def main(argv):
    import numpy as np
    spec = json.load(open(argv[0], encoding="utf-8"))
    out_dir = spec["out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    face_img = build_scene(spec.get("samples", 256))
    scene = bpy.context.scene
    scene.cycles.seed = 7                 # identical noise in both passes
    scene.view_settings.view_transform = "Standard"   # EXR stays linear anyway
    mat = bpy.data.materials["card"]
    for name, value in (("white", 1.0), ("black", 0.0)):
        # Big and sampled "Closest": a tiny image with cubic filtering and
        # CLIP extension fades out toward the card edges.
        solid = bpy.data.images.new(name, 64, 64, alpha=True)
        solid.pixels.foreach_set([value, value, value, 1.0] * 64 * 64)
        node = mat.node_tree.nodes["face"]
        node.image = solid
        node.interpolation = "Closest"
        rgb = render_linear(os.path.join(out_dir, f"{name}.exr"))
        np.save(os.path.join(out_dir, f"{name}.npy"), rgb.astype(np.float16))
        print(f"rendered {name}", flush=True)
    corners = {"standing": face_corners(bpy.data.objects["card_standing"]),
               "flat": face_corners(bpy.data.objects["card_flat"])}
    with open(os.path.join(out_dir, "corners.json"), "w") as f:
        json.dump(corners, f)
    del face_img


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    main(args)
