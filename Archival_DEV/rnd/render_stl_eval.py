"""
rnd/render_stl_eval.py - Headless Blender Script for Thumbnail Quality Evaluation.
Supports camera elevation, azimuth, 2D camera roll, and 3D mesh rotation.
"""

import argparse
import math
import sys
import bpy
import mathutils


def reset_scene():
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
    key = bpy.data.objects.new("KeyLight", bpy.data.lights.new("KeyLight", "SUN"))
    key.data.energy = 3.6
    key.data.color = (1.0, 0.98, 0.95)
    key.rotation_euler = (math.radians(48), math.radians(12), math.radians(40))
    bpy.context.collection.objects.link(key)

    fill = bpy.data.objects.new("FillLight", bpy.data.lights.new("FillLight", "SUN"))
    fill.data.energy = 1.8
    fill.data.color = (0.86, 0.92, 1.0)
    fill.rotation_euler = (math.radians(55), math.radians(-20), math.radians(-60))
    bpy.context.collection.objects.link(fill)

    rim = bpy.data.objects.new("RimLight", bpy.data.lights.new("RimLight", "SUN"))
    rim.data.energy = 2.8
    rim.data.color = (1.0, 1.0, 1.0)
    rim.rotation_euler = (math.radians(-35), math.radians(25), math.radians(170))
    bpy.context.collection.objects.link(rim)


def import_and_orient_mesh(stl_path, up_axis="Z", rot_x=0.0, rot_y=0.0, rot_z=0.0):
    if hasattr(bpy.ops.wm, "stl_import"):
        bpy.ops.wm.stl_import(filepath=str(stl_path))
    else:
        bpy.ops.import_mesh.stl(filepath=str(stl_path))

    meshes = [obj for obj in bpy.context.selected_objects if obj.type == "MESH"]
    if not meshes:
        raise RuntimeError(f"No mesh data found in {stl_path}")

    obj = meshes[0]
    bpy.context.view_layer.objects.active = obj

    up = up_axis.upper()
    if up == "Y":
        obj.rotation_euler = (math.radians(90), 0, 0)
    elif up == "-Y":
        obj.rotation_euler = (math.radians(-90), 0, 0)
    elif up == "-Z":
        obj.rotation_euler = (math.radians(180), 0, 0)
    elif up == "X":
        obj.rotation_euler = (0, math.radians(-90), 0)
    elif up == "-X":
        obj.rotation_euler = (0, math.radians(90), 0)

    obj.rotation_euler.x += math.radians(rot_x)
    obj.rotation_euler.y += math.radians(rot_y)
    obj.rotation_euler.z += math.radians(rot_z)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)

    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    obj.location = (0, 0, 0)

    mesh = obj.data
    mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))
    mesh.update()

    mat = create_resin_material()
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)

    return obj


def setup_camera(obj, azimuth=35.0, elevation=70.0, roll=0.0):
    cam = bpy.data.objects.new("StudioCam", bpy.data.cameras.new("StudioCam"))
    cam.data.lens = 85.0
    bpy.context.collection.objects.link(cam)
    bpy.context.scene.camera = cam

    dims = obj.dimensions
    max_dim = max(dims.x, dims.y, dims.z, 0.01)
    distance = (max_dim / 0.8) * 1.55

    rad_az = math.radians(azimuth)
    rad_el = math.radians(elevation)

    cam.location = (
        distance * math.cos(rad_el) * math.sin(rad_az),
        -distance * math.cos(rad_el) * math.cos(rad_az),
        distance * math.sin(rad_el),
    )

    direction = obj.location - cam.location
    rot_quat = direction.to_track_quat("-Z", "Y")

    if roll != 0.0:
        q_roll = mathutils.Quaternion((0.0, 0.0, 1.0), math.radians(roll))
        rot_quat = rot_quat @ q_roll

    cam.rotation_euler = rot_quat.to_euler()


def main():
    argv = sys.argv
    args_list = argv[argv.index("--") + 1 :] if "--" in argv else []

    parser = argparse.ArgumentParser(description="STL Thumbnail Evaluator")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--up-axis", default="Z", choices=["Z", "-Z", "Y", "-Y", "X", "-X"])
    parser.add_argument("--rot-x", type=float, default=0.0)
    parser.add_argument("--rot-y", type=float, default=0.0)
    parser.add_argument("--rot-z", type=float, default=0.0)
    parser.add_argument("--azimuth", type=float, default=35.0)
    parser.add_argument("--elevation", type=float, default=70.0)
    parser.add_argument("--roll", type=float, default=0.0)
    parser.add_argument("--resolution", type=int, default=800)

    args = parser.parse_args(args_list)

    reset_scene()
    setup_studio_lighting()
    mesh = import_and_orient_mesh(
        args.input,
        up_axis=args.up_axis,
        rot_x=args.rot_x,
        rot_y=args.rot_y,
        rot_z=args.rot_z,
    )
    setup_camera(mesh, azimuth=args.azimuth, elevation=args.elevation, roll=args.roll)

    scene = bpy.context.scene
    scene.render.resolution_x = args.resolution
    scene.render.resolution_y = args.resolution
    scene.render.resolution_percentage = 100
    scene.render.filepath = args.output
    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.quality = 92

    bpy.ops.render.render(write_still=True)


if __name__ == "__main__":
    main()
