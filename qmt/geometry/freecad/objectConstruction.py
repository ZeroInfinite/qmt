# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

"""Functions that perform composite executions."""

import numpy as np
from six import iteritems, text_type

import logging
# ~ logging.getLogger().setLevel(logging.DEBUG)  # toggle debug logging for this file

import FreeCAD
import Draft

# TODO: use namespace in code
from qmt.geometry.freecad.auxiliary import *
from qmt.geometry.freecad.fileIO import exportCAD
from qmt.geometry.freecad.geomUtils import (
    extrude, copy_move, genUnion,  # make_solid,
    getBB, makeBB, makeHexFace,
    extrudeBetween, draftOffset, intersect,
    checkOverlap, subtract,
    crossSection
    )
from qmt.geometry.freecad.sketchUtils import (
    findSegments, splitSketch, extendSketch,
    findEdgeCycles
    )

from qmt.data.geometry import Geo3DData, store_serial


DBG_OUT = (logging.getLogger().level <= logging.DEBUG)


def set_params(doc, paramDict):
    # TODO: support passthrough params
    ''' Update the parameters in the modelParams spreadsheet to reflect the
        current value in the dict.
    '''
    if not paramDict:
        return
    try:
        spreadSheet = doc.modelParams
        spreadSheet.clearAll()  # clear existing spreadsheet
    except:
        # Cave: unconditional removeObject on spreadSheet breaks param dependencies.
        doc.removeObject('modelParams')  # it was not a good spreadsheet
        spreadSheet = doc.addObject('Spreadsheet::Sheet', 'modelParams')
    spreadSheet.set('A1', 'paramName')
    spreadSheet.set('B1', 'paramValue')
    spreadSheet.setColumnWidth('A', 200)
    spreadSheet.setStyle('A1:B1', 'bold', 'add')
    for i, key in enumerate(paramDict):
        paramType = paramDict[key][1]
        if paramType == 'freeCAD':
            idx = str(i + 2)
            spreadSheet.set('A' + idx, key)
            spreadSheet.set('B' + idx, str(paramDict[key][0]))
            spreadSheet.setAlias('B' + idx, str(key))
        elif paramType == 'python':
            pass
        else:
            raise ValueError('Unknown geometric parameter type.')

    doc.recompute()


class DummyInfo:
    def __init__(self):
        self.trash = []
        self.litho_setup_done = False


def build(opts):
    '''Build the 3D geometry in FreeCAD.

    :param dict opts:   Options dict in the QMT Geometry3D.__init__ input format.
    :return geo:        Geo3DData object with the built objects.
    '''
    doc = FreeCAD.ActiveDocument
    geo = Geo3DData()

    # Schedule for deletion all objects not explicitly selected by the user
    input_parts_names = [part.fc_name for part in opts['input_parts']]
    blacklist = []
    for obj in doc.Objects:
        if (obj.Name not in input_parts_names) and (obj.TypeId != 'Spreadsheet::Sheet'):
            blacklist.append(obj)

    # Update the model parameters
    if 'params' in opts:
        # Extend params dictionary to original parts schema
        fcdict = {key: (value, 'freeCAD') for (key, value) in opts['params'].items()}
        set_params(doc, fcdict)

    if 'built_part_names' not in opts:
        opts['built_part_names'] = {}
    if 'serial_stp_parts' not in opts:
        opts['serial_stp_parts'] = {}

    # Build the parts
    info_holder = DummyInfo()  # temporary workaround to support old litho code
    built_parts = []
    for input_part in opts['input_parts']:

        if input_part.directive == 'extrude':
            part = build_extrude(input_part)
        elif input_part.directive == 'SAG':
            part = build_sag(input_part)
        elif input_part.directive == 'wire':
            part = build_wire(input_part)
        elif input_part.directive == 'wire_shell':
            part = build_wire_shell(input_part)
        elif input_part.directive == 'lithography':
            part = build_lithography(input_part, opts, info_holder)
        elif input_part.directive == '3d_shape':
            part = build_pass(input_part)
        else:
            raise ValueError('Directive ' + input_part.directive +
                             ' is not a recognized directive type.')

        assert part is not None
        doc.recompute()
        built_parts.append(part)
        opts['built_part_names'][input_part.label] = part.Name  # needed for litho steps

    # Cleanup
    if not DBG_OUT:
        collect_garbage(info_holder)
        for obj in blacklist:
            delete(obj)
        doc.recompute()

    # Subtraction (removes the need for subtractlists)
    for i, part in enumerate(built_parts):
        for other_part in built_parts[0:i]:
            if checkOverlap([part, other_part]):
                cut = subtract(part, copy_move(other_part), consumeInputs=True
                                                            if not DBG_OUT else False)
                simple_copy = doc.addObject('Part::Feature', "simple_copy")
                simple_copy.Shape = cut.Shape  # no solid, just its shape (can be disjoint)
                delete(cut)
                part = simple_copy
                built_parts[i] = simple_copy

    # Update names and store the built parts
    for input_part, built_part in zip(opts['input_parts'], built_parts):
        built_part.Label = input_part.label  # here it's collision free
        input_part.serial_stp = store_serial([built_part], exportCAD, 'stp')
        input_part.built_fc_name = built_part.Name
        geo.add_part(input_part.label, input_part)

    # Store the FreeCAD document
    geo.set_data('fcdoc', doc)

    return geo


def build_pass(part):
    '''Pass a part unchanged.'''
    assert part.directive == '3d_shape'
    existing_part = FreeCAD.ActiveDocument.getObject(part.fc_name)
    assert existing_part is not None
    return existing_part


def build_extrude(part):
    '''Build an extrude part.'''
    assert part.directive == 'extrude'
    z0 = part.z0
    deltaz = part.thickness
    doc = FreeCAD.ActiveDocument
    sketch = doc.getObject(part.fc_name)
    splitSketches = splitSketch(sketch)
    extParts = []
    for sketch in splitSketches:
        extParts.append(extrudeBetween(sketch, z0, z0 + deltaz, name=part.label))
        delete(sketch)
    doc.recompute()
    return genUnion(extParts, consumeInputs=True
                              if not DBG_OUT else False)


def build_sag(part, offset=0.):
    '''Build a SAG part.'''
    assert part.directive == 'SAG'
    zBot = part.z0
    zMid = part.z_middle
    zTop = part.thickness + zBot
    tIn = part.t_in
    tOut = part.t_out
    doc = FreeCAD.ActiveDocument
    sketch = doc.getObject(part.fc_name)
    sag = makeSAG(sketch, zBot, zMid, zTop, tIn, tOut, offset=offset)
    sag.Label = part.label
    doc.recompute()
    return sag


def build_wire(part, offset=0.):
    '''Build a wire part.'''
    assert part.directive == 'wire'
    doc = FreeCAD.ActiveDocument
    zBottom = part.z0
    width = part.thickness
    sketch = doc.getObject(part.fc_name)
    wire = buildWire(sketch, zBottom, width, offset=offset)
    wire.Label = part.label
    return wire


def build_wire_shell(part, offset=0.):
    '''Build a wire shell part.'''
    assert part.directive == 'wire_shell'
    doc = FreeCAD.ActiveDocument
    zBottom = part.target_wire.z0
    radius = part.target_wire.thickness
    wireSketch = doc.getObject(part.target_wire.fc_name)
    shell_verts = part.shell_verts
    thickness = part.thickness

    if part.depo_mode == 'depo':
        depoZone = doc.getObject(part.fc_name)
        etchZone = None
    elif part.depo_mode == 'etch':
        depoZone = None
        etchZone = doc.getObject(part.fc_name)
    else:
        raise ValueError('Unknown depo_mode ' + part.depo_mode)

    shell = buildAlShell(
        wireSketch,
        zBottom,
        radius,
        shell_verts,
        thickness,
        depoZone=depoZone,
        etchZone=etchZone,
        offset=offset)
    shell.Label = part.label
    return shell


def build_lithography(part, opts, info_holder):
    """Build a lithography part."""
    assert part.directive == 'lithography'
    if not info_holder.litho_setup_done:
        initialize_lithography(info_holder, opts, fillShells=True)
        info_holder.litho_setup_done = True

    if DBG_OUT:
        FreeCAD.ActiveDocument.saveAs('tmp_after_init.fcstd')
    layerNum = part.layer_num
    returnObjs = []
    for objID in info_holder.lithoDict['layers'][layerNum]['objIDs']:
        if part.fc_name == info_holder.lithoDict['layers'][layerNum]['objIDs'][objID]['partName']:
            returnObjs.append(gen_G(info_holder, opts, layerNum, objID))

    logging.debug([o.Name for o in returnObjs])
    return genUnion(returnObjs, consumeInputs=True
                                if not DBG_OUT else False)



################################################################################


def buildWire(sketch, zBottom, width, faceOverride=None, offset=0.0):
    """Given a line segment, build a nanowire of given cross-sectional width
    with a bottom location at zBottom. Offset produces an offset with a specified
    offset.
    """
    doc = FreeCAD.ActiveDocument
    if faceOverride is None:
        face = makeHexFace(sketch, zBottom - offset, width + 2 * offset)
    else:
        face = faceOverride
    sketchForSweep = extendSketch(sketch, offset)
    mySweepTemp = doc.addObject('Part::Sweep', sketch.Name + '_wire')
    mySweepTemp.Sections = [face]
    mySweepTemp.Spine = sketchForSweep
    mySweepTemp.Solid = True
    doc.recompute()
    mySweep = copy_move(mySweepTemp)
    deepRemove(mySweepTemp)
    return mySweep


def buildAlShell(sketch, zBottom, width, verts, thickness,
                 depoZone=None, etchZone=None, offset=0.0):
    """Builds a shell on a nanowire parameterized by sketch, zBottom, and width.

    Here, verts describes the vertices that are covered, and thickness describes
    the thickness of the shell. depoZone, if given, is extruded and intersected
    with the shell (for an etch). Note that offset here *is not* a real offset -
    for simplicity we keep this a thin shell that lies cleanly on top of the
    bigger wire offset. There's no need to include the bottom portion since that's
    already taken up by the wire.
    """
    lineSegments = findSegments(sketch)[0]
    x0, y0, z0 = lineSegments[0]
    x1, y1, z1 = lineSegments[1]
    dx = x1 - x0
    dy = y1 - y0
    rAxis = np.array([-dy, dx, 0])
    # axis perpendicular to the wire in the xy plane
    rAxis /= np.sqrt(np.sum(rAxis ** 2))
    zAxis = np.array([0, 0, 1.])
    doc = FreeCAD.ActiveDocument
    shellList = []
    for vert in verts:
        # Make the original wire (including an offset if applicable)
        originalWire = buildWire(sketch, zBottom, width, offset=offset)
        # Now make the shifted wire:
        angle = vert * np.pi / 3.
        dirVec = rAxis * np.cos(angle) + zAxis * np.sin(angle)
        shiftVec = (thickness) * dirVec
        transVec = FreeCAD.Vector(tuple(shiftVec))
        face = makeHexFace(sketch, zBottom - offset, width +
                           2 * offset)  # make the bigger face
        shiftedFace = Draft.move(face, transVec, copy=False)
        extendedSketch = extendSketch(sketch, offset)
        # The shell offset is handled manually since we are using faceOverride to
        # input a shifted starting face:
        shiftedWire = buildWire(extendedSketch, zBottom,
                                width, faceOverride=shiftedFace)
        delete(extendedSketch)
        shellCut = doc.addObject(
            "Part::Cut", sketch.Name + "_cut_" + str(vert))
        shellCut.Base = shiftedWire
        shellCut.Tool = originalWire
        doc.recompute()
        shell = Draft.move(shellCut, FreeCAD.Vector(0., 0., 0.), copy=True)
        doc.recompute()
        delete(shellCut)
        delete(originalWire)
        delete(shiftedWire)
        shellList.append(shell)
    if len(shellList) > 1:
        coatingUnion = doc.addObject(
            "Part::MultiFuse", sketch.Name + "_coating")
        coatingUnion.Shapes = shellList
        doc.recompute()
        coatingUnionClone = copy_move(coatingUnion)
        doc.removeObject(coatingUnion.Name)
        for shell in shellList:
            doc.removeObject(shell.Name)
    elif len(shellList) == 1:
        coatingUnionClone = shellList[0]
    else:
        raise NameError(
            'Trying to build an empty Al shell. If no shell is desired, omit the AlVerts key from '
            'the json.')
    if (depoZone is None) and (etchZone is None):
        return coatingUnionClone

    elif depoZone is not None:
        coatingBB = getBB(coatingUnionClone)
        zMin = coatingBB[4]
        zMax = coatingBB[5]
        depoVol = extrudeBetween(depoZone, zMin, zMax)
        etchedCoatingUnionClone = intersect(
            [depoVol, coatingUnionClone], consumeInputs=True
                                          if not DBG_OUT else False)
        return etchedCoatingUnionClone
    else:  # etchZone instead
        coatingBB = getBB(coatingUnionClone)
        zMin = coatingBB[4]
        zMax = coatingBB[5]
        etchVol = extrudeBetween(etchZone, zMin, zMax)
        etchedCoatingUnionClone = subtract(
            coatingUnionClone, etchVol, consumeInputs=True
                                        if not DBG_OUT else False)
        return etchedCoatingUnionClone


def makeSAG(sketch, zBot, zMid, zTop, tIn, tOut, offset=0.):
    doc = FreeCAD.ActiveDocument
    # First, compute the geometric quantities we will need:
    a = zTop - zMid  # height of the top part
    b = tOut + tIn  # width of one of the trianglular pieces of the top
    alpha = np.abs(np.arctan(a / np.float(b)))  # lower angle of the top part
    c = a + 2 * offset  # height of the top part including the offset
    # horizontal width of the trianglular part of the top after offset
    d = c / np.tan(alpha)
    # horizontal shift in the triangular part of the top after an offset
    f = offset / np.sin(alpha)

    sketchList = splitSketch(sketch)
    returnParts = []
    for tempSketch in sketchList:
        # TODO: right now, if we try to taper the top of the SAG wire to a point, this
        # breaks, since the offset of topSketch is empty. We should detect and handle this.
        # For now, just make sure that the wire has a small flat top.
        botSketch = draftOffset(tempSketch, offset)  # the base of the wire
        midSketch = draftOffset(tempSketch, f + d - tIn)  # the base of the cap
        topSketch = draftOffset(tempSketch, -tIn + f)  # the top of the cap
        delete(tempSketch)  # remove the copied sketch part
        # Make the bottom wire:
        rectPartTemp = extrude(botSketch, zMid - zBot)
        rectPart = copy_move(rectPartTemp, moveVec=(0., 0., zBot - offset))
        delete(rectPartTemp)
        # make the cap of the wire:
        topSketchTemp = copy_move(topSketch, moveVec=(
            0., 0., zTop - zMid + 2 * offset))
        capPartTemp = doc.addObject('Part::Loft', sketch.Name + '_cap')
        capPartTemp.Sections = [midSketch, topSketchTemp]
        capPartTemp.Solid = True
        doc.recompute()
        capPart = copy_move(capPartTemp, moveVec=(0., 0., zMid - offset))
        delete(capPartTemp)
        delete(topSketchTemp)
        delete(topSketch)
        delete(midSketch)
        delete(botSketch)
        returnParts += [capPart, rectPart]
        returnPart = genUnion(returnParts, consumeInputs=True
                                           if not DBG_OUT else False)
    return returnPart


def initialize_lithography(info, opts, fillShells=True):
    doc = FreeCAD.ActiveDocument
    info.fillShells = fillShells
    # The lithography step requires some infrastructure to track things
    # throughout.
    info.lithoDict = {}  # dictionary containing objects for the lithography step
    info.lithoDict['layers'] = {}
    # Dictionary for containing the substrate. () indicates un-offset objects,
    # and subsequent tuples are offset by t_i for each index in the tuple.
    info.lithoDict['substrate'] = {(): []}

    # To start, we need to collect up all the lithography directives, and
    # organize them by layerNum and objectIDs within layers.
    baseSubstrateParts = []
    for part in opts['input_parts']:
        # If this part is a litho step
        if part.directive == 'lithography':
            layerNum = part.layer_num  # layerNum of this part
            # Add the layerNum to the layer dictionary:
            if layerNum not in info.lithoDict['layers']:
                info.lithoDict['layers'][layerNum] = {'objIDs': {}}
            layerDict = info.lithoDict['layers'][layerNum]
            # Generate the base and thickness of the layer:
            layerBase = float(part.z0)
            layerThickness = float(part.thickness)
            # All parts within a given layer number are required to have
            # identical thickness and base, so check that:
            if 'base' in layerDict:
                assert layerBase == layerDict['base']
            else:
                layerDict['base'] = layerBase
            if 'thickness' in layerDict:
                assert layerThickness == layerDict['thickness']
            else:
                layerDict['thickness'] = layerThickness
            # A given part references a base sketch. However, we need to split
            # the sketch here into possibly disjoint sub-sketches to work
            # with them:
            sketch = doc.getObject(part.fc_name)
            splitSketches = splitSketch(sketch)
            for mySplitSketch in splitSketches:
                objID = len(layerDict['objIDs'])
                objDict = {}
                objDict['partName'] = part.fc_name
                objDict['sketch'] = mySplitSketch
                info.trash.append(mySplitSketch)
                info.lithoDict['layers'][layerNum]['objIDs'][objID] = objDict
            # Add the base substrate to the appropriate dictionary
            baseSubstrateParts += part.litho_base

    # Get rid of any duplicates:
    baseSubstrateParts = list(set(baseSubstrateParts))

    # Now convert the part names for the substrate into 3D freeCAD objects, which
    # should have already been rendered.
    for baseSubstrate in baseSubstrateParts:
        try:
            built_part_name = opts['built_part_names'][baseSubstrate.label]
        except:
            raise KeyError("No substrate built for '" + str(baseSubstrate.label) + "'")
        info.lithoDict['substrate'][()] += [doc.getObject(built_part_name)]
    # ~ import sys
    # ~ sys.stderr.write(">>> litdic " + str(info.lithoDict) + "\n")

    # Now that we have ordered the primitives, we need to compute a few
    # aux quantities that we will need. First, we compute the total bounding
    # box of the lithography procedure:
    thicknesses = []
    bases = []
    for layerNum in info.lithoDict['layers'].keys():
        thicknesses.append(info.lithoDict['layers'][layerNum]['thickness'])
        bases.append(info.lithoDict['layers'][layerNum]['base'])
    bottom = min(bases)
    totalThickness = sum(thicknesses)
    assert len(info.lithoDict['substrate'][
                   ()]) > 0  # Otherwise, we don't have a reference for the lateral BB
    substrateUnion = genUnion(info.lithoDict['substrate'][()],
                              consumeInputs=False)  # total substrate
    BB = list(getBB(substrateUnion))  # bounding box
    BB[4] = min([bottom, BB[4]])
    BB[5] = max([BB[5] + totalThickness, bottom + totalThickness])
    BB = tuple(BB)
    constructionZone = makeBB(BB)  # box that encompases the whole domain.
    info.lithoDict['boundingBox'] = [BB, constructionZone]
    delete(substrateUnion)  # not needed for next steps
    delete(constructionZone)  # not needed for next steps  ... WHY?

    # Next, we add two prisms for each sketch. The first, which we denote "B",
    # is bounded by the base from the bottom and the layer thickness on the top.
    # These serve as "stencils" that would be the deposited shape if no other.
    # objects got in the way. The second set of prisms, denoted "C", covers the
    # base of the layer to the top of the entire domain box. This is used for
    # forming the volumes occupied when substrate objects are offset and
    # checking for overlaps.
    for layerNum in info.lithoDict['layers'].keys():
        base = info.lithoDict['layers'][layerNum]['base']
        thickness = info.lithoDict['layers'][layerNum]['thickness']
        for objID in info.lithoDict['layers'][layerNum]['objIDs']:
            sketch = info.lithoDict['layers'][layerNum]['objIDs'][objID]['sketch']
            B = extrudeBetween(sketch, base, base + thickness)
            C = extrudeBetween(sketch, base, BB[5])
            info.lithoDict['layers'][layerNum]['objIDs'][objID]['B'] = B
            info.lithoDict['layers'][layerNum]['objIDs'][objID]['C'] = C
            info.trash.append(B)
            info.trash.append(C)
            # In addition, add a hook for the HDict, which will contain the "H"
            # constructions for this object, but offset to thicknesses of various
            # layers, according to the keys.
            info.lithoDict['layers'][layerNum]['objIDs'][objID]['HDict'] = {}


def gen_offset(opts, obj, offsetVal):
    """Generates an offset non-destructively."""
    doc = FreeCAD.ActiveDocument
    # First, we need to check if the object needs special treatment:
    treatment = 'standard'
    try:
        partname = next(label for (label, built_name) in
                        opts['built_part_names'].iteritems() if built_name == obj.Name)
        input_part = next(input_part for input_part in
                          opts['input_parts'] if input_part.label == partname)
        treatment = input_part.directive
    except:
        pass

    if treatment == 'extrude' or treatment == 'lithography':
        treatment = 'standard'

    if treatment == 'standard':
        # Apparently the offset function is buggy for very small offsets...
        if offsetVal < 1e-5:
            offsetDupe = copy_move(obj)
        else:
            offset = doc.addObject("Part::Offset")
            offset.Source = obj
            offset.Value = offsetVal
            offset.Mode = 0
            offset.Join = 2
            doc.recompute()
            offsetDupe = copy_move(offset)
            doc.recompute()
            delete(offset)
    elif treatment == 'wire':
        offsetDupe = build_wire(input_part, offset=offsetVal)
    elif treatment == 'wire_shell':
        offsetDupe = build_wire_shell(input_part, offset=offsetVal)
    elif treatment == 'SAG':
        offsetDupe = build_sag(input_part, offset=offsetVal)
    doc.recompute()

    try:
        logging.debug("%s (%s) -> %s (%s) [from %s]", obj.Name, obj.Label,
                      offsetDupe.Name, offsetDupe.Label, input_part.label)
    except:
        logging.debug("%s (%s) -> %s (%s)", obj.Name, obj.Label,
                      offsetDupe.Name, offsetDupe.Label)

    return offsetDupe


def screened_H_union_list(info, opts, obj, m, j, offsetTuple, checkOffsetTuple):
    """Form the screened union list of obj with the layer m, objID j H object that has
    been offset according to offsetTuple. The screened union list is defined by checking
    first whether the object intersects with the components of the checkOffset version
    of the H object. Then, for each component that would intersect, we return the a list
    of the offsetTuple version of the object.
    """
    logging.debug('>>> %s (%s)', obj.Name, obj.Label)
    # First, we need to check to see if we need to compute either of the
    # underlying H obj lists:
    HDict = info.lithoDict['layers'][m]['objIDs'][j]['HDict']
    # HDict stores a collection of H object component lists for each (layerNum,objID)
    # pair. The index of this dictionary is a tuple: () indicates no
    # offset, while other indices indicate an offset by summing the thicknesses
    # from corresponding layers.
    if checkOffsetTuple not in HDict:  # If we haven't computed this yet
        HDict[checkOffsetTuple] = H_offset(info, opts, m, j,
                                           tList=list(checkOffsetTuple))  # list of H parts
        info.trash += HDict[checkOffsetTuple]
    if offsetTuple not in HDict:  # If we haven't computed this yet
        HDict[offsetTuple] = H_offset(info, opts, m, j, tList=list(offsetTuple))  # list of H parts
        info.trash += HDict[offsetTuple]
    HObjCheckList = HDict[checkOffsetTuple]
    HObjList = HDict[offsetTuple]

    returnList = []
    for i, HObjPart in enumerate(HObjCheckList):
        if checkOverlap(
                [obj, HObjPart]):  # if we need to include an overlap
            returnList.append(HObjList[i])

    # fix for multilayer intersections: make sure we really check all overlaps
    for i, HObjPart in enumerate(HObjList):
        if checkOverlap(
                [obj, HObjPart]):  # if we need to include an overlap
            returnList.append(HObjList[i])

    logging.debug('<<< %s', [o.Name + ' (' + o.Label + ')' for o in returnList])
    return returnList


def screened_A_UnionList(info, opts, obj, t, ti, offsetTuple, checkOffsetTuple):
    """Form the screened union list of obj with the substrate A that has
    been offset according to offsetTuple.
    """
    logging.debug('>>> %s (%s)', obj.Name, obj.Label)
    # First, we need to see if we have built the objects before:
    if checkOffsetTuple not in info.lithoDict['substrate']:
        info.lithoDict['substrate'][checkOffsetTuple] = []
        for A in info.lithoDict['substrate'][()]:
            AObj = gen_offset(opts, A, t)
            info.trash.append(AObj)
            info.lithoDict['substrate'][checkOffsetTuple].append(AObj)
    if offsetTuple not in info.lithoDict['substrate']:
        info.lithoDict['substrate'][offsetTuple] = []
        for A in info.lithoDict['substrate'][()]:
            AObj = gen_offset(opts, A, t + ti)
            info.trash.append(AObj)
            info.lithoDict['substrate'][offsetTuple].append(AObj)

    returnList = []
    for i, ACheck in enumerate(
            info.lithoDict['substrate'][checkOffsetTuple]):
        if checkOverlap([obj, ACheck]):
            returnList.append(info.lithoDict['substrate'][offsetTuple][i])

    logging.debug('<<< %s', [o.Name + ' (' + o.Label + ')' for o in returnList])
    return returnList


def H_offset(info, opts, layerNum, objID, tList=[]):
    """For a given layerNum=n and ObjID=i, compute the deposited object.

    ```latex
    H_{n,i}(t) = C_{n,i}(t) \cap [ B_{n,i}(t) \cup (\cup_{m<n;j} H_{m,j}(t_i+t)) \cup (\cup_k A_k(t_i + t))],
    ```
    where A_k is from the base substrate list. This is computed recursively. The list of integers
    tList determines the offset t; t = the sum of all layer thicknesses ti that appear
    in tList. For example, tList = [1,2,3] -> t = t1+t2+t3.

    Note: this object is returned as a list of objects that need to be unioned together
    in order to form the full H.
    """

    logging.debug('>>> partname %s',
                  info.lithoDict['layers'][layerNum]['objIDs'][objID]['partName'])

    # This is a tuple that encodes the check offset t:
    checkOffsetTuple = tuple(sorted(tList))
    # This is a tuple that encodes the total offset t_i+t:
    offsetTuple = tuple(sorted(tList + [layerNum]))
    # First, check if we have to do anything:
    layers = info.lithoDict['layers']
    if checkOffsetTuple in layers[layerNum]['objIDs'][
        objID]['HDict']:
        return layers[layerNum]['objIDs'][objID][
            'HDict'][checkOffsetTuple]
    # First, compute t:
    t = 0.0
    for tIndex in tList:
        t += layers[tIndex]['thickness']
    # thickness of this layer
    ti = layers[layerNum]['thickness']
    # Set the aux. thickness t:
    B = layers[layerNum]['objIDs'][objID]['B']  # B prism for this layer & objID
    C = layers[layerNum]['objIDs'][objID]['C']  # C prism for this layer & ObjID
    B_t = gen_offset(opts, B, t)  # offset the B prism
    C_t = gen_offset(opts, C, t)  # offset the C prism
    info.trash.append(B_t)
    info.trash.append(C_t)

    # Build up the substrate due to previously deposited gates
    HOffsetList = []
    for m in layers.keys():
        if m < layerNum:  # then this is a lower layer
            for j in layers[m]['objIDs'].keys():
                HOffsetList += screened_H_union_list(
                    info, opts, C_t, m, j, offsetTuple, checkOffsetTuple)
    # Next, build up the original substrate list:
    AOffsetList = []
    AOffsetList += screened_A_UnionList(info, opts, C_t, t, ti,
                                        offsetTuple, checkOffsetTuple)
    unionList = HOffsetList + AOffsetList
    returnList = [B_t]

    for obj in unionList:
        intObj = intersect([C_t, obj])
        info.trash.append(intObj)
        returnList.append(intObj)
        logging.debug('%s (%s) -> %s (%s)', obj.Name, obj.Label, intObj.Name, intObj.Label)

    layers[layerNum]['objIDs'][objID]['HDict'][checkOffsetTuple] = returnList

    logging.debug('<<< %s', [o.Name + ' (' + o.Label + ')' for o in returnList])
    return returnList


def gen_U(info, layerNum, objID):
    """For a given layerNum and objID, compute the quantity:
    ```latex
    U_{n,i}(t) = (\cup_{m<n;j} G_{m,j}) \cup (\cup_{k} A_k),
    ```
    where the inner union terms are not included if their intersection
    with B_i is empty.
    """
    layers = info.lithoDict['layers']
    B = layers[layerNum]['objIDs'][objID][
        'B']  # B prism for this layer & objID
    GList = []
    for m in layers.keys():
        if m < layerNum:  # then this is a lower layer
            for j in layers[m].keys():
                if 'G' not in layers[layerNum][
                    'objIDs'][objID]:
                    gen_G(info, m, j)
                G = layers[layerNum]['objIDs'][objID]['G']
                if checkOverlap([B, G]):
                    GList.append(G)
    AList = []
    for A in info.lithoDict['substrate'][()]:
        if checkOverlap([B, A]):
            AList.append(A)
    unionList = GList + AList
    unionObj = genUnion(unionList, consumeInputs=False)
    return unionObj


def gen_G(info, opts, layerNum, objID):
    """Generate the gate deposition for a given layerNum and objID."""

    layerobj = info.lithoDict['layers'][layerNum]['objIDs'][objID]
    logging.debug('>>> layer %d obj %d (part:%s B:%s C:%s sketch:%s)', layerNum, objID,
                  layerobj['partName'], layerobj['B'].Name, layerobj['C'].Name,
                  layerobj['sketch'].Name)

    if 'G' not in layerobj:
        if () not in layerobj['HDict']:
            layerobj['HDict'][()] = H_offset(info, opts, layerNum, objID)

        if DBG_OUT:
            FreeCAD.ActiveDocument.saveAs('tmp_after_H_offset.fcstd')
        # TODO: reuse new function
        # This block fixes multifuses for wireshells with too big offsets,
        # by forcing all participating object shells into a new solid.
        # It still needs to be coerced into handling disjoint "solids".
        # ~ solid_hlist = []
        # ~ import Part
        # ~ for obj in layerobj['HDict'][()]:
            # ~ obj.Shape.Solids
            # ~ try:

                # ~ __s__ = obj.Shape.Faces
                # ~ __s__ = Part.Solid(Part.Shell(__s__))
                # ~ __o__ = FreeCAD.ActiveDocument.addObject("Part::Feature", obj.Name + "_solid")
                # ~ __o__.Label = obj.Label + "_solid"
                # ~ __o__.Shape = __s__

            # ~ except Part.OCCError:
                #Draft.downgrade(obj,delete=True)  # doesn't work without GUI
                # ~ for solid in obj.Shape.Solids:
                    # ~ for shell in solid.Shells:
                        # ~ pass

            # ~ solid_hlist.append(__o__)
            # ~ info.trash.append(obj)
            # ~ info.trash.append(__o__)
            # ~ info.trash.append(__s__)

        # ~ layerobj['HDict'][()] = solid_hlist
        # ~ logging.debug('new HDict: %s', [o.Name + ' (' + o.Label + ')' for o in layerobj['HDict'][()]])

        H = genUnion(layerobj['HDict'][()],
                     consumeInputs=False)
        info.trash.append(H)
        if info.fillShells:
            G = copy_move(H)
        else:
            U = gen_U(info, layerNum, objID)
            G = subtract(H, U)
            delete(U)
        layerobj['G'] = G

    G = layerobj['G']
    partName = layerobj['partName']
    G.Label = partName
    logging.debug('<<< G from H: %s (%s)', G.Name, G.Label)
    return G


def collect_garbage(info):
    """Delete all the objects in trash."""
    for obj in info.trash:
        try:
            delete(obj)
        except BaseException:
            pass


def buildCrossSection(sliceInfo, passModel=None):
    """Render the 2D objects required for cross-sections."""
    if passModel is None:
        passModel = getModel()
    doc = FreeCAD.ActiveDocument

    sliceName = sliceInfo['sliceName']
    axis, distance = sliceInfo['axis'], sliceInfo['distance']
    sliceParts = {}
    for name, part in iteritems(passModel.modelDict['3DParts']):
        # loop over FreeCAD shapes corresponding to part
        polygons = {}
        for shapeName in part['fileNames'].keys():
            # slice the 3D part
            fcName = shapeName + '_section_' + sliceName
            partObj = doc.getObject(shapeName)
            section = crossSection(partObj, axis=axis, d=distance, name=fcName)

            # separate disjoint pieces
            segments, cycles = findEdgeCycles(section)
            for i, cycle in enumerate(cycles):
                points = [tuple(segments[idx, 0]) for idx in cycle]
                patchName = fcName
                patchName = '{}_{}'.format(shapeName, i)
                polygons[patchName] = points

        # store sliced part
        if polygons:
            slicePart = part.copy()
            slicePart['type'] = "domain"
            slicePart['3DPart'] = name
            slicePart['geometry'] = polygons
            sliceParts[name] = slicePart

    return sliceParts


def build2DGeo(passModel=None):
    """Construct the 2D geometry entities defined in the json file."""
    # TODO: THIS FUNCTION NEEDS TO BE UPDATED AFTER modelRevision
    # RESTRUCTURING!
    myModel = getModel() if passModel is None else passModel
    twoDObjs = {}
    doc = FreeCAD.ActiveDocument
    for fcName in myModel.modelDict['freeCADInfo']:
        keys = myModel.modelDict['freeCADInfo'][fcName].keys()
        if '2DObject' in keys:
            objType = '2DObject'
            objControlDict = myModel.modelDict['freeCADInfo'][fcName][objType]
            returnDict = {}
            returnDict.update(objControlDict['physicsProps'])
            returnDict['type'] = objControlDict['type']
            if objControlDict['type'] == 'boundary':
                points = [tuple(v.Point)
                          for v in doc.getObject(fcName).Shape.Vertexes]
                twoDObjs[fcName] = (points, returnDict)
            else:
                lineSegments, cycles = findEdgeCycles(doc.getObject(fcName))
                for i, cycle in enumerate(cycles):
                    points = [tuple(lineSegments[idx, 0]) for idx in cycle]
                    name = fcName
                    if len(cycles) > 1:
                        name = '{}_{}'.format(name, i)
                    twoDObjs[name] = (points, returnDict)
    return twoDObjs
