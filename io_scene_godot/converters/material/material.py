"""
Exports materials. For now I'm targetting the blender internal, however this
will be deprecated in Blender 2.8 in favor of EEVEE. EEVEE has PBR and
should be able to match Godot better, but unfortunately parseing a node
tree into a flat bunch of parameters is not trivial. So for someone else:"""

import logging
import os
import bpy
from .script_shader import export_script_shader
from ...structures import (
    InternalResource, ExternalResource, gamma_correct, ValidationError)


def export_image(escn_file, export_settings, image):
    """
    Saves an image as an external reference relative to the blend location
    """
    image_id = escn_file.get_external_resource(image)
    if image_id is not None:
        return image_id

    imgpath = image.filepath
    if imgpath.startswith("//"):
        imgpath = bpy.path.abspath(imgpath)

    imgpath = os.path.relpath(
        imgpath,
        os.path.dirname(export_settings['path'])
    ).replace("\\", "/")

    # Add the image to the file
    image_resource = ExternalResource(imgpath, "Image")
    image_id = escn_file.add_external_resource(image_resource, image)

    return image_id


def export_material(escn_file, export_settings, bl_object, material):
    """Exports blender internal/cycles material as best it can"""
    external_material = find_material(export_settings, material)
    if external_material is not None:
        resource_id = escn_file.get_external_resource(material)
        if resource_id is None:
            ext_mat = ExternalResource(
                external_material[0],  # Path
                external_material[1]  # Material Type
            )
            resource_id = escn_file.add_external_resource(ext_mat, material)
        return "ExtResource({})".format(resource_id)

    resource_id = generate_material_resource(
        escn_file, export_settings, bl_object, material
    )
    return "SubResource({})".format(resource_id)

from .script_shader.node_tree import find_material_output_node, topology_sort


def extract(escn_file, input):
    name = input.name
    namemap = {"Base Color": "albedo",
           "Metallic": "metallic",
           "Specular": "specular",
           "Roughness": "roughness",
           "Normal": "normal"}
    try:
        name = namemap[input.name]
    except:
        return None, None

    if input.is_linked:
        node = input.links[0].from_socket.node 
        if node.bl_idname ==  'ShaderNodeTexImage':
            name = name + "_texture"
            value = "ExtResource( {0} )".format(escn_file.get_external_resource(node.image))
        else:
            return None, None
    else:
        name = name + "_color"
        value = input.default_value
        if "bpy_prop_array" in str(type(value)):
            v = list(value)
            value = "Color( {0}, {1}, {2}, {3} )".format(*v)
    return name, value


def parse_shader_node_tree_spatial(escn_file, shader_node_tree):
    """Parse blender shader node tree"""
    mtl_output_node = find_material_output_node(shader_node_tree)
    data = {}
    if mtl_output_node is not None:
        frag_node_list = topology_sort(shader_node_tree.nodes)

        for idx, node in enumerate(frag_node_list):
            if node == mtl_output_node:
                continue

        surface_output_socket = mtl_output_node.inputs['Surface']
        if surface_output_socket.is_linked:
            surface_in_socket = surface_output_socket.links[0].from_socket
            for input in surface_in_socket.node.inputs:
                name, value = extract(escn_file, input)
                if name is not None:
                    data[name] = value

    print(data)
    return data
    

def export_as_spatial_material(escn_file, material_rsc_name, material, bl_object):
    """Export a Blender Material as Godot Spatial Material"""
    mat = InternalResource("SpatialMaterial", material_rsc_name)
    shader_node_tree = material.node_tree
    data = parse_shader_node_tree_spatial(escn_file, shader_node_tree)
    for key, value in data.items():
        mat[key] = value
    if "normal_texture" in data:
        mat["normal_enabled"] = "true"
    #mat[''] = data['albedo_color']

    return mat


def generate_material_resource(escn_file, export_settings, bl_object,
                               material):
    """Export blender material as an internal resource"""
    engine = bpy.context.scene.render.engine
    mat = None

    if export_settings['generate_external_material']:
        material_rsc_name = material.name
    else:
        # leave material_name as empty, prevent godot
        # to convert material to external file
        material_rsc_name = ''

    if (export_settings['material_mode'] == 'SCRIPT_SHADER' and
            engine in ('CYCLES', 'BLENDER_EEVEE') and
            material.node_tree is not None):
        mat = InternalResource("ShaderMaterial", material_rsc_name)
        try:
            export_script_shader(
                escn_file, export_settings, bl_object, material, mat 
            )
        except ValidationError as exception:
            # fallback to SpatialMaterial
            mat = export_as_spatial_material(escn_file, material_rsc_name, material, mat)
            logging.error(
                "%s, in material '%s'", str(exception), material.name
            )
    else:  # Spatial Material
        try:
            export_script_shader(
                    escn_file, export_settings, bl_object, material, mat, True
                )
        except ValidationError:
            pass
        mat = export_as_spatial_material(escn_file, material_rsc_name, material, bl_object)

    # make material-object tuple as an identifier, as uniforms is part of
    # material and they are binded with object
    return escn_file.add_internal_resource(mat, (bl_object, material))


# ------------------- Tools for finding existing materials -------------------
def _find_material_in_subtree(folder, material):
    """Searches for godot materials that match a blender material. If found,
    it returns (path, type) otherwise it returns None"""
    candidates = []

    material_file_name = material.name + '.tres'
    for dir_path, _subdirs, files in os.walk(folder):
        if material_file_name in files:
            candidates.append(os.path.join(dir_path, material_file_name))

    # Checks it is a material and finds out what type
    valid_candidates = []
    for candidate in candidates:
        with open(candidate) as mat_file:
            first_line = mat_file.readline()
            if "SpatialMaterial" in first_line:
                valid_candidates.append((candidate, "SpatialMaterial"))
            if "ShaderMaterial" in first_line:
                valid_candidates.append((candidate, "ShaderMaterial"))

    if not valid_candidates:
        return None
    if len(valid_candidates) > 1:
        logging.warning("Multiple materials found for %s", material.name)
    return valid_candidates[0]


def find_material(export_settings, material):
    """Searches for an existing Godot material"""
    search_type = export_settings["material_search_paths"]
    if search_type == "PROJECT_DIR":
        search_dir = export_settings["project_path_func"]()
    elif search_type == "EXPORT_DIR":
        search_dir = os.path.dirname(export_settings["path"])
    else:
        search_dir = None

    if search_dir is None:
        return None
    return _find_material_in_subtree(search_dir, material)
