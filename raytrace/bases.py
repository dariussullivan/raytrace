#    Copyright 2009, Teraview Ltd.
#
#    This file is part of Raytrace.
#
#    Raytrace is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.


from enthought.traits.api import HasTraits, Array, Float, Complex,\
            Property, List, Instance, Range, Any,\
            Tuple, Event, cached_property, Set, Int, Trait, Button,\
            self, Str, Bool, PythonValue, Enum
from enthought.traits.ui.api import View, Item, ListEditor, VSplit,\
            RangeEditor, ScrubberEditor, HSplit, VGroup, TextEditor,\
            TupleEditor, VGroup, HGroup, TreeEditor, TreeNode, TitleEditor,\
            ShellEditor
            
from enthought.traits.ui.file_dialog import save_file
            
from enthought.tvtk.api import tvtk
import numpy
import threading, os, itertools
import wx
from itertools import chain, izip, islice, count
from raytrace.rays import RayCollection, collectRays
from raytrace.constraints import BaseConstraint
from raytrace.has_queue import HasQueue, on_trait_change
from raytrace.faces import Face
from raytrace.utils import normaliseVector, transformNormals, transformPoints,\
        transformVectors, dotprod

Vector = Array(shape=(3,))

NumEditor = TextEditor(auto_set=False, enter_set=True, evaluate=float)
ComplexEditor = TextEditor(auto_set=False, enter_set=True, 
                           evaluate=float)

ROField = TextEditor(auto_set=False, enter_set=True, evaluate=float)
VectorEditor = TupleEditor(labels=['x','y','z'], auto_set=False, enter_set=True)

counter = count()

class Direction(HasTraits):
    x = Float
    y = Float
    z = Float


class Renderable(HasQueue):
    display = Enum("shaded", "wireframe", "hidden")
    
    actors = Instance(tvtk.ActorCollection, (), transient=True)
    render = Event() #request rerendering (but not necessarily re-tracing)
    
    def _display_changed(self, vnew):
        if vnew=="shaded":
            for actor in self.actors:
                actor.visibility = True
                actor.property.representation = "surface"
        elif vnew=="wireframe":
            for actor in self.actors:
                actor.visibility = True
                actor.property.representation = "wireframe"
        else:
            for actor in self.actors:
                actor.visibility = False
        self.render = True
    
    def get_actors(self, scene):
        return self.actors


class ModelObject(Renderable):
    name = Str("A traceable component")
    
    centre = Tuple(0.,0.,0.) #position
    
    _orientation = Tuple(float, float)
    
    orientation = Property(Range(-180.0,180.0), depends_on="_orientation")
    elevation = Property(Range(-180.,180.), depends_on="_orientation")
    
    rotation = Range(-180.0,180.0, value=0.0) #rotation around orientation axis
    
    direction_btn = Button("set")
    direction = Property(Tuple(float,float,float),depends_on="_orientation")
    
    x_axis = Property(Tuple(float,float,float),depends_on="_orientation, rotation")
    
    dir_x = Property(depends_on="direction")
    dir_y = Property(depends_on="direction")
    dir_z = Property(depends_on="direction")
    
    transform = Instance(tvtk.Transform, (), transient=True)
    
    def _direction_btn_changed(self):
        d = self.direction
        D = Direction(x=d[0],y=d[1],z=d[2])
        D.edit_traits(kind='modal')
        self.direction = D.x, D.y, D.z
    
    def _get_orientation(self): return self._orientation[0]
    
    def _set_orientation(self, v): self._orientation = (v, self._orientation[1])
    
    def _get_elevation(self): return self._orientation[1]
    
    def _set_elevation(self, v): self._orientation = self._orientation[0], v
    
    def _get_dir_x(self): return self.direction[0]
    
    def _get_dir_y(self): return self.direction[1]
    
    def _get_dir_z(self): return self.direction[2]
    
    @on_trait_change("_orientation, centre, rotation")
    def on_position(self):
        trans = self.transform
        trans.identity()
        trans.translate(*self.centre)
        o,e = self._orientation
        trans.rotate_z(o)
        trans.rotate_x(e)
        trans.rotate_z(self.rotation)
        #print "set transform", self._orientation
        self.update = True
        
    def _get_x_axis(self):
        temp = tvtk.Transform()
        o,e = self._orientation
        temp.rotate_z(o)
        temp.rotate_x(e)
        temp.rotate_z(self.rotation)
        direct = temp.transform_point(1,0,0)
        return direct
        
    @cached_property
    def _get_direction(self):
        temp = tvtk.Transform()
        o,e = self._orientation
        temp.rotate_z(o)
        temp.rotate_x(e)
        temp.rotate_z(self.rotation)
        direct = temp.transform_point(0,0,1)
        #print "get direction", direct, o, e
        return direct
    
    def _set_direction(self, d):
        x,y,z = normaliseVector(d)
        Theta = numpy.arccos(z)
        theta = 180*Theta/numpy.pi
        phi = 180*numpy.arctan2(x,y)/numpy.pi
        #print "set direction", -phi, -theta
        self._orientation = -phi, -theta
        
    def make_step_shape(self):
        """Creates an OpenCascade BRep Shape
        representation of the object, which can be
        exported to STEP format"""
        return False, None
        
        
class Probe(Renderable):
    pass
        
    
class Traceable(ModelObject):
    vtkproperty = Instance(tvtk.Property, transient=True)

    update = Event() #request re-tracing
    
    intersections = List([])
    
    faces = List(Face, desc="list of traceable faces (Face instances)",
                 transient = True)
    
    #all Traceables have a pipeline to generate a VTK visualisation of themselves
    pipeline = Any(transient=True) #a tvtk.DataSetAlgorithm ?
    
    polydata = Property(depends_on=['update', ])
    
    def _faces_changed(self, vnew):
        for face in vnew:
            face.transform = self.tranform
    
    def _actors_default(self):
        pipeline = self.pipeline
        
        map = tvtk.PolyDataMapper(input=pipeline.output)
        act = tvtk.Actor(mapper=map)
        act.property = self.vtkproperty
        actors = tvtk.ActorCollection()
        actors.append(act)
        return actors
    
    def _get_polydata(self):
        self.pipeline.update()
        pd = self.pipeline.output
        #print pd
        return pd
    
    def trace_rays(self, rays):
        """traces a RayCollection.
        
        @param rays: a RayCollection instance
        @param face_id: id of the face to trace. If None, trace all faces
        
        returns - a recarray of intersetions with the same size as rays
                  dtype=(('length','d'),('face','O'),('point','d',[3,]))
                  
                  'length' is the physical length from the start of the ray
                  to the intersection
                  
                  'cell' is the ID of the intersecting face
                  
                  'point' is the position coord of the intersection (in world coords)
        """
        max_length = rays.max_length
        p1 = rays.origin
        p2 = p1 + max_length*rays.direction
        return self.intersect(p1, p2, max_length)
        
    def intersect(self, p1, p2, max_length):
        t = self.transform
        inv_t = t.linear_inverse
        P1 = transformPoints(inv_t, p1)
        P2 = transformPoints(inv_t, p2)
        
        faces = self.faces
        if len(faces)>1:
            traces = numpy.column_stack([f.intersect(P1, P2, max_length) for f in faces])   
            nearest = numpy.argmin(traces['length'], axis=1)
            ar = numpy.arange(traces.shape[0])
            shortest = traces[ar,nearest]
        else:
            shortest = faces[0].intersect(P1, P2, max_length)
            
        t_points = shortest['point']
        points = transformPoints(t, t_points)
        shortest['point'] = points
        return shortest
    
    def intersect_line(self, p1, p2):
        """Find the nearest intersection of a single line defined by two points
        p1 and p2
        
        returns a tuple (L, F, P), where L is the scalar length from p1 to the
        intersection point, F is the intersecting face and P is the intersection
        point (a 3-vector).
        """
        p1 = numpy.asarray(p1).reshape(-1,1)
        p2 = numpy.asarray(p2).reshape(-1,1)
        max_length = ((p1-p2)**2).sum(axis=0)[0]
        nearest = self.intersect(p1, p2, max_length)[0]
        return nearest['length'], nearest['cell'], nearest['point']
    
    def update_complete(self):
        pass


Traceable.uigroup = VGroup(
                   Item('name', editor=TitleEditor(), springy=False,
                        show_label=False),
                   Item('display'),
                   VGroup(
                   Item('orientation', editor=ScrubberEditor()),
                   Item('elevation', editor=ScrubberEditor()),
                   Item('rotation', editor=ScrubberEditor()),
                   ),
                   HGroup(Item('centre', 
                               show_label=False, 
                               editor=VectorEditor,
                               springy=True), 
                          show_border=True,
                          label="Centre"),
                   HGroup(
                          VGroup(
                          Item("dir_x", style="readonly", label="x"),
                          Item("dir_y", style="readonly", label="y"),
                          Item("dir_z", style="readonly", label="z"),
                          springy=True
                          ),
                          Item('direction_btn', show_label=False, width=-60),
                          show_border=True,
                          label="Direction"
                          ),
                    )

    
class Optic(Traceable):
    n_inside = Complex(1.0+0.0j) #refractive
    n_outside = Complex(1.0+0.0j)
    
    all_rays = Bool(False, desc="trace all reflected rays")
    
    vtkproperty = tvtk.Property(opacity = 0.4,
                             color = (0.8,0.8,1.0))
    
    def calc_refractive_index(self, wavelengths):
        """
        Evaluates an array of (complex) refractive indices.
        @param wavelengths: a shape=(N,1) array of wavelengths
        @returns: a 2-tuple representing the inside and outside
        refractive indices respectively. The items in the tuple can be
        either 1) an arrays with the same shape as wavelengths and with
                    dtype=numpy.complex128
            or 2) a complex scalar
        """
        return self.n_inside, self.n_outside
    
    @on_trait_change("n_inside, n_outside")
    def n_changed(self):
        self.update = True
    
### The following functions have been moved to the Face subclasses
#
#    def compute_normal(self, points, cell_ids):
#        """
#        Evaluate the surface normal in the world frame-of-reference
#        @param points: flaot64 ndarray of shape (n,3) giving intersection points
#        @param cell_ids: Int ndaray of shape (n,) with cell ids
#        """
#        raise NotImplementedError
#    
#    def eval_children(self, rays, points, cells, mask=slice(None,None,None)):
#        """
#        actually calculates the new ray-segments. Physics here
#        for Fresnel reflections.
#        
#        rays - a RayCollection object
#        points - a (Nx3) array of intersection coordinates
#        cells - a length N array of cell ids (a.k.a. face ids), or an int
#        mask - a bool array selecting items for this Optic
#        """
#        raise Exception("depreciated!")
#        points = points[mask]
#        if isinstance(cells, int):
#            normal = self.compute_normal(points, cells)
#            cells = numpy.ones(points.shape[0], numpy.int) * cells
#        else:
#            cells = cells[mask] ###reshape not necessary
#            normal = self.compute_normal(points, cells)
#        input_v = rays.direction[mask]
#        
#        parent_ids = numpy.arange(mask.shape[0])[mask]
#        optic = numpy.repeat([self,], points.shape[0] )
#        
#        S_amp, P_amp, S_vec, P_vec = Convert_to_SP(input_v, 
#                                                   normal, 
#                                                   rays.E_vector[mask], 
#                                                   rays.E1_amp[mask], 
#                                                   rays.E2_amp[mask])
#
#        #this is cos(theta), where theta is the angle between the
#        #normal and the incident ray
#        cosTheta = dotprod(normal, input_v)
#        
#        origin = points
#            
#        fromoutside = cosTheta < 0
#        n1 = numpy.where(fromoutside, self.n_outside.real, self.n_inside.real)
#        n2 = numpy.where(fromoutside, self.n_inside.real, self.n_outside.real)
#        flip = numpy.where(fromoutside, 1, -1)
#            
#        abscosTheta = numpy.abs(cosTheta)
#        
#        N2 = (n2/n1)**2
#        N2cosTheta = N2*abscosTheta
#        
#        #if this is less than zero, we have Total Internal Reflection
#        N2_sin2 = abscosTheta**2 + (N2 - 1)
#        
#        TIR = N2_sin2 < 0.0
#        sqrt = numpy.sqrt
#        
#        cosThetaNormal = cosTheta*normal
#        reflected = input_v - 2*cosThetaNormal
#        sqrtN2sin2 = numpy.where(TIR, 1.0j*sqrt(-N2_sin2), sqrt(N2_sin2))
#        #print "root n2.sin2", sqrtN2sin2
#        
#        #Fresnel equations for reflection
#        R_p = (N2cosTheta - sqrtN2sin2) / (N2cosTheta + sqrtN2sin2)
#        R_s = (abscosTheta - sqrtN2sin2) / (abscosTheta + sqrtN2sin2)
#        #print "R_s", R_s, "R_p", R_p
#        
#        ###Now calculate transmitted rays
#        d1 = input_v
#        tangent = d1 - cosThetaNormal
#        
#        tan_mag_sq = ((n1*tangent/n2)**2).sum(axis=1).reshape(-1,1)        
#        
#        c2 = numpy.sqrt(1 - tan_mag_sq)
#        transmitted = tangent*(n1/n2) - c2*normal*flip 
#        #print d1, normal, tangent, transmitted, "T"
#        
#        cos1 = abscosTheta
#        #cos of angle between outgoing ray and normal
#        cos2 = abs(dotprod(transmitted, normal))
#        
#        Two_n1_cos1 = (2*n1)*cos1
#        
#        aspect = sqrt(cos2/cos1) * Two_n1_cos1
#        
#        #Fresnel equations for transmission
#        T_p = aspect / ( n2*cos1 + n1*cos2 )
#        T_s = aspect / ( n2*cos2 + n1*cos1 )
#        #print "T_s", T_s, "T_p", T_p
#        
#        if self.all_rays:
#            refl_rays = RayCollection(origin=origin,
#                                       direction = reflected,
#                                       max_length = rays.max_length,
#                                       E_vector = S_vec,
#                                       E1_amp = S_amp*R_s,
#                                       E2_amp = P_amp*R_p,
#                                       parent_ids = parent_ids,
#                                       optic = optic,
#                                       face_id = cells,
#                                       refractive_index=n1)
#            
#            trans_rays = RayCollection(origin=origin,
#                                       direction = transmitted,
#                                       max_length = rays.max_length,
#                                       E_vector = S_vec,
#                                       E1_amp = S_amp*T_s,
#                                       E2_amp = P_amp*T_p,
#                                       parent_ids = parent_ids,
#                                       optic = optic,
#                                       face_id = cells,
#                                       refractive_index=n2)
#            
#            allrays = collectRays(refl_rays, trans_rays)
#            allrays.parent = rays
#            return allrays
#        else:
#            TIR.shape=-1,1
#            tir = TIR*numpy.ones(3)
#            direction = numpy.where(tir, reflected,transmitted)
#            E1_amp = S_amp*numpy.where(TIR, R_s, T_s)
#            E2_amp = P_amp*numpy.where(TIR, R_p, T_p)
#            refractive_index = numpy.where(TIR, n1, n2)
#            
#            return RayCollection(origin=origin,
#                               direction = direction,
#                               max_length = rays.max_length,
#                               E_vector = S_vec,
#                               E1_amp = E1_amp,
#                               E2_amp = E2_amp,
#                               parent_ids = parent_ids,
#                               optic = optic,
#                               face_id = cells,
#                               refractive_index=refractive_index) 
    
    
class VTKOptic(Optic):
    """Polygonal optics using a vtkOBBTree to do the ray intersections"""
    data_source = Instance(tvtk.ProgrammableSource, (), transient=True)
    
    obb = Instance(tvtk.OBBTree, (), transient=True)
    
    def __getstate__(self):
        d = super(VTKOptic, self).__getstate__()
        bad = ['polydata','pipeline','data_source','obb', 'transform']
        for b in bad:
            d.pop(b, None)
        return d
    
    def __init__(self, *args, **kwds):
        super(VTKOptic, self).__init__(*args, **kwds)
        self.on_trait_change(self.on__polydata_changed, "_polydata")
        self.on__polydata_changed()
        
    def on__polydata_changed(self):
        self.data_source.modified()
        self.update = True
    
    def _polydata_changed(self):
        obb = self.obb
        obb.free_search_structure()
        obb.data_set = self.polydata
        obb.build_locator()
        self.data_source.modified()
        
    def _pipeline_default(self):
        source = self.data_source
        def execute():
            polydata = self._polydata
            output = source.poly_data_output
            output.shallow_copy(polydata)
        source.set_execute_method(execute)
        t = self.transform
        transf = tvtk.TransformFilter(input=source.output, transform=t)
        tri = tvtk.TriangleFilter(input=transf.output)
        return tri
        
    def trace_segment(self, seg, last_optic=None, last_cell=None):
        """
        Finds the intersection of the given ray-segment with the 
        object geometry data
        """
        p1 = seg.origin
        p2 = p1 + seg.MAX_RAY_LENGTH*seg.direction
        pts = tvtk.Points()
        ids = tvtk.IdList()
        ret = self.obb.intersect_with_line(p1, p2, pts, ids)
        sqrt = numpy.sqrt
        array = numpy.array
        if ret==0:
            return None
        if self is not last_optic:
            last_cell = None
        data = [ (sqrt(((array(p) - p1)**2).sum()), p, Id)
                    for p, Id in izip(pts, ids) 
                    if Id is not last_cell ]
        if not data:
            return None
        short = min(data, key=lambda a: a[0])
        return short[0], short[1], short[2], self
    