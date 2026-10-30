"""
rnd/render_cardinal_views.py - 6-View Cardinal & Axial Renderer.
Bakes the 08_elev75_az12_alt orientation into the mesh geometry, standing the body upright at (0,0,0).
Renders 4 turntable angles (0°, 90°, 180°, 270°) plus Top-Down and Bottom-Up.
"""

import argparse
import math
import sys
from pathlib import Path
import bpy
import mathutils


def setup_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene

    if hasattr(bpy.types, "RenderEngineEEVEENext"):
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    else:
        scene.render.engine = "BLENDER_EEVEE"

    world = bpy.data.worlds.new("StudioWorld")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.07, 0.07, 0.09, 1.0)
        bg.inputs[1].default_value = 1.0


def create_resin_material():
    mat = bpy.data.materials.new(name="Miniature_Resin_Matte")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    output = nodes.new(type="ShaderNodeOutputMaterial")
    bsdf = nodes.new(type="ShaderNodeBsdfPrincipled")

    bsdf.inputs["Base Color"].default_value = (0.45, 0.47, 0.50, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.35
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.5
    elif "Specular" in bsdf.inputs:
        bsdf.inputs["Specular"].default_value = 0.5

    links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
    return mat


def setup_studio_lighting():
    # Key Light (Top-front-right of the baseline camera)
    key = bpy.data.objects.new("KeyLight", bpy.data.lights.new("KeyLight", "SUN"))
    key.data.energy = 3.6
    key.data.color = (1.0, 0.98, 0.95)
    key.rotation_euler = (math.radians(45), math.radians(0), math.radians(35))
    bpy.context.collection.objects.link(key)

    # Fill Light (Soft ambient top-front-left)
    fill = bpy.data.objects.new("FillLight", bpy.data.lights.new("FillLight", "SUN"))
    fill.data.energy = 1.8
    fill.data.color = (0.86, 0.92, 1.0)
    fill.rotation_euler = (math.radians(55), math.radians(0), math.radians(-45))
    bpy.context.collection.objects.link(fill)

    # Rim Light (Back kicker for silhouette separation)
    rim = bpy.data.objects.new("RimLight", bpy.data.lights.new("RimLight", "SUN"))
    rim.data.energy = 2.8
    rim.data.color = (1.0, 1.0, 1.0)
    rim.rotation_euler = (math.radians(-35), math.radians(0), math.radians(160))
    bpy.context.collection.objects.link(rim)

    # Under-Fill Light (Upward kicker for bottom-up and lower overhangs)
    under = bpy.data.objects.new("UnderLight", bpy.data.lights.new("UnderLight", "SUN"))
    under.data.energy = 2.2
    under.data.color = (0.9, 0.95, 1.0)
    under.rotation_euler = (math.radians(-120), math.radians(0), math.radians(30))
    bpy.context.collection.objects.link(under)


def import_and_align_mesh_to_spine(stl_path):
    """
    Imports STL and uses the 08_elev75_az12_alt frame to align the body:
      - Head -> +Z (Up)
      - Chest -> +Y (Facing baseline camera)
      - Right paw -> +X (Right)
    """
    if hasattr(bpy.ops.wm, "stl_import"):
        bpy.ops.wm.stl_import(filepath=str(stl_path))
    else:
        bpy.ops.import_mesh.stl(filepath=str(stl_path))

    meshes = [obj for obj in bpy.context.selected_objects if obj.type == "MESH"]
    if not meshes:
        raise RuntimeError(f"No mesh data found in {stl_path}")

    obj = meshes[0]
    bpy.context.view_layer.objects.active = obj

    # 1. Initial center
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    obj.location = (0, 0, 0)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    bpy.context.view_layer.update()

    # 2. Derive rotation matrix R from 08_elev75_az12_alt (azimuth 12°, elevation 75°, roll -20°)
    rad_az = math.radians(12.0)
    rad_el = math.radians(75.0)

    cam_dir = mathutils.Vector((
        math.cos(rad_el) * math.sin(rad_az),
        -math.cos(rad_el) * math.cos(rad_az),
        math.sin(rad_el),
    )).normalized()

    view_dir = -cam_dir
    rot_quat = view_dir.to_track_quat("-Z", "Y")
    q_roll = mathutils.Quaternion((0.0, 0.0, 1.0), math.radians(-20.0))
    rot_quat = rot_quat @ q_roll

    cam_mat3 = rot_quat.to_matrix()
    v_right = mathutils.Vector((cam_mat3[0][0], cam_mat3[1][0], cam_mat3[2][0]))
    v_head = mathutils.Vector((cam_mat3[0][1], cam_mat3[1][1], cam_mat3[2][1]))
    v_chest = mathutils.Vector((cam_mat3[0][2], cam_mat3[1][2], cam_mat3[2][2]))

    # Target: v_head -> +Z, v_chest -> +Y, v_right -> +X
    R = mathutils.Matrix((
        (v_right.x, v_right.y, v_right.z),
        (v_chest.x, v_chest.y, v_chest.z),
        (v_head.x, v_head.y, v_head.z),
    ))

    # Apply transform directly to mesh geometry
    obj.data.transform(R.to_4x4())
    obj.data.update()

    # 3. Re-center bounding box to ensure model is at (0, 0, 0)
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    obj.location = (0, 0, 0)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    bpy.context.view_layer.update()

    # Smooth shading
    mesh = obj.data
    mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))
    mesh.update()

    mat = create_resin_material()
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)

    return obj


def main():
    argv = sys.argv
    args_list = argv[argv.index("--") + 1 :] if "--" in argv else []

    parser = argparse.ArgumentParser(description="6-View Turntable & Axial Renderer")
    parser.add_argument("--input", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--resolution", type=int, default=800)

    args = parser.parse_args(args_list)

    setup_scene()
    setup_studio_lighting()

    mesh_obj = import_and_align_mesh_to_spine(args.input)

    # Frame camera based on upright mesh dimensions
    dims = mesh_obj.dimensions
    max_dim = max(dims.x, dims.y, dims.z, 0.01)
    distance = (max_dim / 0.8) * 1.55

    cam_data = bpy.data.cameras.new("StudioCam")
    cam_data.lens = 85.0
    cam_obj = bpy.data.objects.new("StudioCam", cam_data)
    bpy.context.collection.objects.link(cam_obj)
    bpy.context.scene.camera = cam_obj

    # Position camera looking directly at the chest from +Y
    cam_obj.location = (0.0, distance, 0.0)
    direction = mathutils.Vector((0.0, 0.0, 0.0)) - cam_obj.location
    cam_obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()

    scene = bpy.context.scene
    scene.render.resolution_x = args.resolution
    scene.render.resolution_y = args.resolution
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.quality = 92

    out_path = Path(args.out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    bpy.context.view_layer.update()

    # -------------------------------------------------------------------------
    # Part 1: 4 Cardinal Turntable Views (Rotating the model in place around +Z)
    # -------------------------------------------------------------------------
    cardinals = [
        ("01_front_left.jpg", 0.0),
        ("02_front_right.jpg", 90.0),
        ("03_back_right.jpg", 180.0),
        ("04_back_left.jpg", 270.0),
    ]

    for filename, angle_deg in cardinals:
        mesh_obj.rotation_euler = (0.0, 0.0, math.radians(angle_deg))
        bpy.context.view_layer.update()

        scene.render.filepath = str(out_path / filename)
        bpy.ops.render.render(write_still=True)
        print(f"[+] Rendered: {filename} (Turntable: {angle_deg}°)")

    # Reset turntable angle to 0°
    mesh_obj.rotation_euler = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()

    # -------------------------------------------------------------------------
    # Part 2: Top-Down (Looking straight down the spine from +Z, chest at top)
    # -------------------------------------------------------------------------
    cam_obj.location = (0.0, 0.0, distance)
    # Looking down -Z, with Screen-Up pointing toward chest (+Y)
    cam_obj.rotation_euler = mathutils.Vector((0.0, 0.0, -1.0)).to_track_quat("-Z", "Y").to_euler()
    bpy.context.view_layer.update()

    scene.render.filepath = str(out_path / "05_top_down.jpg")
    bpy.ops.render.render(write_still=True)
    print("[+] Rendered: 05_top_down.jpg (Top-Down Zenith)")

    # -------------------------------------------------------------------------
    # Part 3: Bottom-Up (Looking straight up from beneath -Z, chest at top)
    # -------------------------------------------------------------------------
    cam_obj.location = (0.0, 0.0, -distance)
    # Looking up +Z, with Screen-Up pointing toward chest (+Y)
    cam_obj.rotation_euler = mathutils.Vector((0.0, 0.0, 1.0)).to_track_quat("-Z", "Y").to_euler()
    bpy.context.view_layer.update()

    scene.render.filepath = str(out_path / "06_bottom_up.jpg")
    bpy.ops.render.render(write_still=True)
    print("[+] Rendered: 06_bottom_up.jpg (Bottom-Up Nadir)")


if __name__ == "__main__":
    main()
