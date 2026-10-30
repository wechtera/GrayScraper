"""
scripts/blender_render_mesh.py - Headless Blender STL Studio Thumbnail Renderer.
Supports 6-view cardinal & axial rendering with locked spinal vertical axis:
  - front_left (baseline azimuth 12°, elevation 75°, roll -20°)
  - front_right (+90° turntable around spinal axis)
  - back_right (+180° turntable around spinal axis)
  - back_left (+270° turntable around spinal axis)
  - top_down (looking down -Z with chest facing frame top)
  - bottom_up (looking up +Z with chest facing frame top and active under-fill light)

Usage inside Blender:
  blender -b -P scripts/blender_render_mesh.py -- --input <stl> --output <jpg> --view <view_name> [options]
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

    # Dark slate studio backdrop (#121217)
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

    # Neutral gray resin tone (#737880 -> sRGB ~0.451, 0.471, 0.502)
    bsdf.inputs["Base Color"].default_value = (0.451, 0.471, 0.502, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.35
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.5
    elif "Specular" in bsdf.inputs:
        bsdf.inputs["Specular"].default_value = 0.5

    links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
    return mat


def setup_studio_lighting():
    # 1. Key Light (Top-front-right of the baseline camera)
    key = bpy.data.objects.new("KeyLight", bpy.data.lights.new("KeyLight", "SUN"))
    key.data.energy = 3.6
    key.data.color = (1.0, 0.98, 0.95)
    key.rotation_euler = (math.radians(45), math.radians(0), math.radians(35))
    bpy.context.collection.objects.link(key)

    # 2. Fill Light (Soft ambient top-front-left)
    fill = bpy.data.objects.new("FillLight", bpy.data.lights.new("FillLight", "SUN"))
    fill.data.energy = 1.8
    fill.data.color = (0.86, 0.92, 1.0)
    fill.rotation_euler = (math.radians(55), math.radians(0), math.radians(-45))
    bpy.context.collection.objects.link(fill)

    # 3. Rim Light (High-angle back kicker for silhouette separation)
    rim = bpy.data.objects.new("RimLight", bpy.data.lights.new("RimLight", "SUN"))
    rim.data.energy = 2.8
    rim.data.color = (1.0, 1.0, 1.0)
    rim.rotation_euler = (math.radians(-35), math.radians(0), math.radians(160))
    bpy.context.collection.objects.link(rim)

    # 4. Under-Fill Light (Soft upward kicker for bottom-up and lower overhangs)
    under = bpy.data.objects.new("UnderLight", bpy.data.lights.new("UnderLight", "SUN"))
    under.data.energy = 2.2
    under.data.color = (0.9, 0.95, 1.0)
    under.rotation_euler = (math.radians(-120), math.radians(0), math.radians(30))
    bpy.context.collection.objects.link(under)


def import_and_align_mesh_to_spine(stl_path, rot_x=0.0, rot_y=0.0, rot_z=0.0):
    """
    Imports STL and applies the validated spinal alignment algorithm:
      - Head -> +Z (Vertical spine)
      - Chest -> +Y (Facing baseline camera)
      - Right side -> +X (Right paw)
    Centers bounding box exactly to (0,0,0) and applies smooth shading.
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

    # 1. Apply any manual fine-tune rotations before spinal alignment
    if rot_x != 0.0 or rot_y != 0.0 or rot_z != 0.0:
        obj.rotation_euler.x += math.radians(rot_x)
        obj.rotation_euler.y += math.radians(rot_y)
        obj.rotation_euler.z += math.radians(rot_z)
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)

    # 2. Initial center of geometry
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    obj.location = (0, 0, 0)
    bpy.ops.object.transform_apply(location=True, rotation=False, scale=False)
    bpy.context.view_layer.update()

    # 3. Derive rotation matrix R from baseline 08_elev75_az12_alt (azimuth 12°, elevation 75°, roll -20°)
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

    # Apply transform directly to mesh vertex coordinates
    obj.data.transform(R.to_4x4())
    obj.data.update()

    # 4. Re-center bounding box to ensure model is centered at (0, 0, 0)
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    obj.location = (0, 0, 0)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    bpy.context.view_layer.update()

    # Smooth shading
    mesh = obj.data
    mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))
    mesh.update()

    # Apply matte resin material
    mat = create_resin_material()
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)

    return obj


def main():
    argv = sys.argv
    args_list = argv[argv.index("--") + 1 :] if "--" in argv else []

    parser = argparse.ArgumentParser(description="Headless Blender STL Renderer")
    parser.add_argument("--input", required=True, help="Path to input STL file")
    parser.add_argument("--output", required=True, help="Path to output image file")
    parser.add_argument(
        "--view",
        default="front_left",
        choices=["front_left", "front_right", "back_right", "back_left", "top_down", "bottom_up"],
        help="Target view angle",
    )
    parser.add_argument("--rot-x", type=float, default=0.0, help="Additional X rotation in degrees")
    parser.add_argument("--rot-y", type=float, default=0.0, help="Additional Y rotation in degrees")
    parser.add_argument("--rot-z", type=float, default=0.0, help="Additional Z rotation in degrees")
    parser.add_argument("--resolution", type=int, default=800, help="Render resolution in px (default 800)")

    args = parser.parse_args(args_list)

    setup_scene()
    setup_studio_lighting()

    mesh_obj = import_and_align_mesh_to_spine(
        args.input,
        rot_x=args.rot_x,
        rot_y=args.rot_y,
        rot_z=args.rot_z,
    )

    # Frame camera distance based on normalized upright mesh dimensions
    dims = mesh_obj.dimensions
    max_dim = max(dims.x, dims.y, dims.z, 0.01)
    distance = (max_dim / 0.8) * 1.55

    cam_data = bpy.data.cameras.new("StudioCam")
    cam_data.lens = 85.0
    cam_obj = bpy.data.objects.new("StudioCam", cam_data)
    bpy.context.collection.objects.link(cam_obj)
    bpy.context.scene.camera = cam_obj

    view = args.view.lower()

    if view in ("front_left", "front_right", "back_right", "back_left"):
        angle_map = {
            "front_left": 0.0,
            "front_right": 90.0,
            "back_right": 180.0,
            "back_left": 270.0,
        }
        angle_deg = angle_map[view]
        mesh_obj.rotation_euler = (0.0, 0.0, math.radians(angle_deg))
        cam_obj.location = (0.0, distance, 0.0)
        direction = mathutils.Vector((0.0, 0.0, 0.0)) - cam_obj.location
        cam_obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()

    elif view == "top_down":
        mesh_obj.rotation_euler = (0.0, 0.0, 0.0)
        cam_obj.location = (0.0, 0.0, distance)
        # Looking down -Z, with Screen-Up pointing toward chest (+Y)
        cam_obj.rotation_euler = mathutils.Vector((0.0, 0.0, -1.0)).to_track_quat("-Z", "Y").to_euler()

    elif view == "bottom_up":
        mesh_obj.rotation_euler = (0.0, 0.0, 0.0)
        cam_obj.location = (0.0, 0.0, -distance)
        # Looking up +Z, with Screen-Up pointing toward chest (+Y)
        cam_obj.rotation_euler = mathutils.Vector((0.0, 0.0, 1.0)).to_track_quat("-Z", "Y").to_euler()

    bpy.context.view_layer.update()

    scene = bpy.context.scene
    scene.render.resolution_x = args.resolution
    scene.render.resolution_y = args.resolution
    scene.render.resolution_percentage = 100
    scene.render.filepath = args.output

    if args.output.lower().endswith(".png"):
        scene.render.image_settings.file_format = "PNG"
    else:
        scene.render.image_settings.file_format = "JPEG"
        scene.render.image_settings.quality = 92

    bpy.ops.render.render(write_still=True)


if __name__ == "__main__":
    main()
