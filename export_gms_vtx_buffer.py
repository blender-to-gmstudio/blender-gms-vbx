import bpy
#import shutil  # for image file copy
import os
import conversions
from os import (
    path,
    makedirs,
    )
from os.path import (
    splitext,
    split,
    )
from struct import (
    pack,
    calcsize,
    )

def triangulated_mesh(obj):
    import bmesh
    mod_tri = obj.modifiers.new('triangulate_for_export','TRIANGULATE')
    mesh = obj.to_mesh(preserve_all_data_layers=True,depsgraph=bpy.context.evaluated_depsgraph_get())
    obj.modifiers.remove(mod_tri)
    return mesh


def write_object_ba(scene,obj,desc,ba,frame,reverse_loop,apply_transforms):
    """Traverse the object's mesh data at the given frame and write to the
    appropriate bytearray in ba using the description data structure provided"""
    desc, vertex_format_bytesize = desc
    
    def fetch_attribs(desc,node,ba,byte_pos,frame):
        """"Fetch the attribute values from the given node and place in ba at byte_pos"""
        id = node.bl_rna.identifier
        if id in desc:
            for prop, occurences in desc[id].items():                   # Property name and occurences in bytedata
                for offset, attr_blen, fmt, index, func in occurences:  # Each occurence's data (tuple assignment!)
                    ind = byte_pos+offset
                    val = getattr(node,prop)
                    if func != None: val = func(val)
                    val_bin = pack(fmt,val) if len(fmt) == 1 else pack(fmt,*val)
                    ba[frame-index][ind:ind+attr_blen] = val_bin
    
    mod_tri = obj.modifiers.new('triangulate_for_export','TRIANGULATE')
    #m = obj.to_mesh(bpy.context.scene,True,'RENDER')
    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval = obj.evaluated_get(depsgraph)
    m = obj_eval.to_mesh()
    obj.modifiers.remove(mod_tri)
    if apply_transforms:
        m.transform(obj.matrix_world)
    
    ba_pos = 0
    for poly in m.polygons:
        iter = reversed(poly.loop_indices) if reverse_loop else poly.loop_indices
        for li in iter:
            fetch_attribs(desc,scene,ba,ba_pos,frame)
            fetch_attribs(desc,obj,ba,ba_pos,frame)
            
            fetch_attribs(desc,poly,ba,ba_pos,frame)
            
            if (len(m.materials) > 0):
                mat = m.materials[poly.material_index]
                if not mat.use_nodes:
                    fetch_attribs(desc,mat,ba,ba_pos,frame)
            
            loop = m.loops[li]
            fetch_attribs(desc,loop,ba,ba_pos,frame)
            
            if (len(m.uv_layers) > 0):
                uvs = m.uv_layers.active.data
                uv = uvs[loop.index]                                # Use active uv layer
                fetch_attribs(desc,uv,ba,ba_pos,frame)
            
            vertex = m.vertices[loop.vertex_index]
            fetch_attribs(desc,vertex,ba,ba_pos,frame)
            
            # We wrote a full vertex, so we can now increment the bytearray position by the vertex format size
            ba_pos += vertex_format_bytesize
    
    obj.to_mesh_clear()


def construct_ds(obj,attr):
    """Constructs the data structure required to move through the attributes of a given object"""
    desc, offset = {}, 0
    
    for a in attr:
        ident, atn, format, fo, func = a
        
        if ident not in desc:
            desc[ident] = {}
        dct_obj = desc[ident]
        
        if atn not in dct_obj:
            dct_obj[atn] = []
        lst_attr = dct_obj[atn]
        
        prop_rna = getattr(bpy.types,ident).bl_rna.properties[atn]
        attrib_bytesize = calcsize(format)
        
        lst_attr.append((offset,attrib_bytesize,format,fo,func))
        offset += attrib_bytesize
        
    return (desc, offset)


def construct_ba(obj,desc,frame_count):
    """Construct the required bytearrays to store vertex data for the given object for the given number of frames"""
    mod_tri = obj.modifiers.new('triangulate_for_export','TRIANGULATE')
    #m = obj.to_mesh(bpy.context.scene,True,'RENDER')
    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval = obj.evaluated_get(depsgraph)
    m = obj_eval.to_mesh(preserve_all_data_layers=True,depsgraph=bpy.context.evaluated_depsgraph_get())
    obj.modifiers.remove(mod_tri)
    no_verts = len(m.polygons) * 3
    obj.to_mesh_clear()                                   # Any easier way to get number of vertices??
    desc, vertex_format_bytesize = desc
    ba = [bytearray([0] * no_verts * vertex_format_bytesize) for i in range(0,frame_count)]
    return ba, no_verts


def object_to_json(obj):
    """Returns the data of the object in a json-compatible form"""
    result = {}
    rna = obj.bl_rna
    for prop in rna.properties:
        prop_id = prop.identifier
        prop_ins = getattr(obj,prop_id)
        prop_rna = rna.properties[prop_id]
        type = rna.properties[prop_id].type
        #print(prop_id,prop_ins,type)
        if type == 'STRING':
            result[prop_id] = prop_ins
        elif type == 'ENUM':
            result[prop_id] = [flag for flag in prop_ins] if prop_rna.is_enum_flag else prop_ins
        elif type == 'POINTER':
            result[prop_id] = getattr(prop_ins,'name','') if prop_ins != None else ''
        elif type == 'COLLECTION':
            # Enter collections up to encountering a PointerProperty
            result[prop_id] = [object_to_json(prop_item) for prop_item in prop_ins if prop_item != None]
            pass
        else:
            # 'Simple' attribute types: int, float, boolean
            if prop_rna.is_array:
                # Sometimes the bl_rna indicates a number of array items, but the actual number is less
                # That's because items are stored in an additional object, e.g. a matrix consists of 4 vectors
                len_expected, len_actual = prop_rna.array_length, len(prop_ins)
                if len_expected > len_actual:
                    result[prop_id] = []
                    for item in prop_ins: result[prop_id].extend(item[:])
                else:
                    result[prop_id] = prop_ins[:]
            else:
                result[prop_id] = prop_ins
    return result


def export(self, context):
    """Main entry point for export"""
    # TODO Get rid of context in this function
    
    # Prepare a bit
    root, ext = splitext(self.filepath)
    base, fname = split(self.filepath)
    fn = splitext(fname)[0]
    scene = context.scene
    frame_count = scene.frame_end-scene.frame_start+1 if self.frame_option == 'all' else 1
    mesh_selection = [obj for obj in context.selected_objects if obj.type == 'MESH']
    for i, obj in enumerate(mesh_selection): obj.batch_index = i   # Guarantee a predictable batch index
    
    if self.export_mesh_data:
    # Join step
        if self.join_into_active:
            bpy.ops.object.join()
        
        # TODO: transformation and axes step
        
        
        # Split by material
        if self.split_by_material:
            bpy.ops.mesh.separate(type='MATERIAL')
        
        attribs = [(i.datapath[0].node,i.datapath[1].node,i.fmt,i.int,None if i.func == "none" else getattr(conversions,i.func)) for i in self.vertex_format]
        #print(attribs)
        
        # << Prepare a structure to map vertex attributes to the actual contents >>
        ba_per_object = {}
        no_verts_per_object = {}
        desc_per_object = {}
        for obj in mesh_selection:
            desc_per_object[obj] = construct_ds(obj,attribs)
            ba_per_object[obj], no_verts_per_object[obj] = construct_ba(obj,desc_per_object[obj],frame_count)
        
        # << End of preparation of structure >>
        
        # << Now execute >>
        
        # Loop through scene frames
        for i in range(frame_count):
            # First set the current frame
            scene.frame_set(scene.frame_start+i)
            
            # Now add frame vertex data for the current object
            for obj in mesh_selection:
                write_object_ba(scene,obj,desc_per_object[obj],ba_per_object[obj],i,self.reverse_loop,self.apply_transforms)
        
        # Final step: write all bytearrays to one or more file(s)
        # in one or more directories
        f = open(root + ".vbx","wb")
        
        offset = {}
        for obj in mesh_selection:
            ba = ba_per_object[obj]
            offset[obj] = f.tell()
            for b in ba:
                f.write(b)
        
        f.close()
    
    # Create JSON description file
    if self.export_json_data:
        ctx, data = {}, {}
        json_data = {
            "bpy":{
                "context":ctx,
                "data":data
            }
        }
        
        # Export bpy.context
        ctx["selected_objects"] = [object_to_json(obj) for obj in bpy.context.selected_objects]
        #ctx["scene"] = {"view_layers":{"layers":[{layer.name:[i for i in layer.layer_collection]} for layer in context.scene.view_layers]}}
        
        # Export bpy.data
        data_to_export = self.object_types_to_export
        for datatype in data_to_export:
            #data[datatype] = [object_to_json(obj) for obj in getattr(bpy.data,datatype)]
            data[datatype] = {obj.name:object_to_json(obj) for obj in getattr(bpy.data,datatype)}
        
        # Export additional info that might be useful
        json_data["blmod"] = {
            "mesh_data":{
                "location":fn + ".vbx",
                "format":[{"type":x.datapath[0].node,"attr":x.datapath[1].node,"fmt":x.fmt} for x in self.vertex_format],
                "ranges":{obj.name:{"no_verts":no_verts_per_object[obj],"offset":offset[obj]} for obj in mesh_selection},
            },
            "settings":{"apply_transforms":self.apply_transforms},
            "no_frames":frame_count,
            "blender_version":bpy.app.version[:],
            #"version":bl_info["version"],
        }
        
        import json
        f_desc = open(root + ".json","w")
        json.dump(json_data,f_desc)
        f_desc.close()
    
    # Save textures (TODO: clean this up!)
    if self.export_textures:
        for obj in mesh_selection:                              # Only mesh objects have texture slots
            tex_slot = None
            for ms in obj.material_slots:
                mat = ms.material
                if not mat.use_nodes:
                    tex_slot = mat.texture_slots[0]
            if tex_slot != None:
                image = tex_slot.texture.image
                image.save_render(base + '/' + image.name,context.scene)
    
    # Cleanup: remove dynamic property from class
    del bpy.types.Object.batch_index
    
    return {'FINISHED'}