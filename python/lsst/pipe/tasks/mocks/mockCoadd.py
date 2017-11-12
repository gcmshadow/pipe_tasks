#
# LSST Data Management System
# Copyright 2008-2015 AURA/LSST.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <https://www.lsstcorp.org/LegalNotices/>.
#

from __future__ import absolute_import, division, print_function
from builtins import range
import lsst.afw.image
import lsst.afw.geom
import lsst.pex.config
import lsst.afw.table
import lsst.pipe.base
from lsst.pipe.tasks.makeSkyMap import MakeSkyMapTask
from lsst.pipe.tasks.makeCoaddTempExp import MakeCoaddTempExpTask
from lsst.pipe.tasks.assembleCoadd import (AssembleCoaddTask, SafeClipAssembleCoaddTask,
                                           CompareWarpAssembleCoaddTask)
from .mockObject import MockObjectTask
from .mockObservation import MockObservationTask
from .mockSelect import MockSelectImagesTask


class MockCoaddConfig(lsst.pex.config.Config):
    makeSkyMap = lsst.pex.config.ConfigurableField(
        doc="SkyMap builder subtask",
        target=MakeSkyMapTask
    )
    mockObject = lsst.pex.config.ConfigurableField(
        doc="Subtask that generates and draws the objects/sources in the mock images",
        target=MockObjectTask
    )
    mockObservation = lsst.pex.config.ConfigurableField(
        doc="Subtask that generates the Wcs, Psf, Calib, etc. of mock images",
        target=MockObservationTask
    )
    coaddName = lsst.pex.config.Field(
        doc="Coadd name used as a prefix for other datasets",
        dtype=str,
        optional=False,
        default="deep"
    )
    nObservations = lsst.pex.config.Field(
        doc="Number of mock observations to generate.",
        dtype=int,
        optional=False,
        default=12
    )
    edgeBuffer = lsst.pex.config.Field(
        doc=("Number of pixels by which to grow object bounding boxes when determining whether they land "
             " completely on a generated image"),
        dtype=int,
        optional=False,
        default=5
    )

    def setupSkyMapPatches(self, nPatches=2, patchSize=400, pixelScale=0.2*lsst.afw.geom.arcseconds):
        """
        Set the nested [discrete] skymap config parameters such that the full tract
        has nPatches x nPatches patches of the given size and pixel scale.
        """
        self.makeSkyMap.skyMap['discrete'].patchInnerDimensions = [patchSize, patchSize]
        self.makeSkyMap.skyMap['discrete'].pixelScale = pixelScale.asArcseconds()
        # multiply by 0.5 because we want a half-width; subtract 0.49 to ensure that we get the right
        # number after skyMap.TractInfo rounds up.
        radius = (0.5 * nPatches - 0.49) * patchSize * pixelScale.asDegrees()
        self.makeSkyMap.skyMap['discrete'].radiusList = [radius]

    def setDefaults(self):
        self.makeSkyMap.skyMap.name = 'discrete'
        self.makeSkyMap.skyMap['discrete'].raList = [90.0]
        self.makeSkyMap.skyMap['discrete'].decList = [0.0]
        self.makeSkyMap.skyMap['discrete'].patchBorder = 10
        self.makeSkyMap.skyMap['discrete'].projection = "TAN"
        self.makeSkyMap.skyMap['discrete'].tractOverlap = 0.0
        self.setupSkyMapPatches()


class MockCoaddTask(lsst.pipe.base.CmdLineTask):
    """MockCoaddTask is a driver task for creating mock coadds.  As opposed to more realistic
    simulations, MockCoadd generates and uses extremely simple "toy" data that can be used to more
    rigorously test the behavior of high-level task code because the expected results are
    more easily predicted.  In particular, calexps are generated directly from the truth catalog,
    and contain only zero-noise stars that are created using the same Psf, Calib, and Wcs that will
    be attached to the mock calexp.

    In addition to creating the mock calexps and truth catalogs, MockCoadd also contains driver
    code to run the MakeSkyMap, MakeCoaddTempExp, and AssembleCoadd tasks on the mock calexps,
    and code to directly create a mock coadd image using CoaddPsf, which can be compared to the
    output of the regular coadd tasks to check that the coadd code and CoaddPsf are consistent.

    Note that aside from MakeSkyMapTask, the coadd tasks are *not* subtasks of MockCoaddTasks,
    and their configs are not part of MockCoaddConfig; these are created locally within
    MockCoaddTask methods when needed, as not all coadd task config options are appropriate
    for the mock data generated by MockCoadd.
    """

    ConfigClass = MockCoaddConfig

    _DefaultName = "MockCoadd"

    def __init__(self, **kwds):
        """Construct a MockCoaddTask and the subtasks used for generating skymaps, objects,
        and observations (i.e. calexp parameters).
        """
        lsst.pipe.base.CmdLineTask.__init__(self, **kwds)
        self.makeSubtask("makeSkyMap")
        self.makeSubtask("mockObject")
        self.makeSubtask("mockObservation")
        self.schema = lsst.afw.table.SimpleTable.makeMinimalSchema()
        self.objectIdKey = self.schema.addField("objectId", type="L", doc="foreign key to truth catalog")
        self.exposureIdKey = self.schema.addField("exposureId", type="L",
                                                  doc="foreign key to observation catalog")
        self.centroidInBBoxKey = self.schema.addField(
            "centroidInBBox", type="Flag",
            doc="set if this source's center position is inside the generated image's bbox"
        )
        self.partialOverlapKey = self.schema.addField(
            "partialOverlap", type="Flag",
            doc="set if this source was not completely inside the generated image"
        )

    def buildSkyMap(self, butler):
        """Build the skymap for the mock dataset."""
        return self.makeSkyMap.run(butler.dataRef(self.config.coaddName + "Coadd_skyMap")).skyMap

    def buildTruthCatalog(self, butler=None, skyMap=None, tract=0):
        """Create and save (if butler is not None) a truth catalog containing all the mock objects.

        Must be run after buildSkyMap.

        Most of the work is delegated to the mockObject subtask.
        """
        if skyMap is None:
            skyMap = butler.get(self.config.coaddName + "Coadd_skyMap")
        catalog = self.mockObject.run(tractInfo=skyMap[tract])
        if butler is not None:
            butler.put(catalog, "truth", tract=tract)
        return catalog

    def buildObservationCatalog(self, butler=None, skyMap=None, tract=0, camera=None):
        """Create and save (if butler is not None) an ExposureCatalog of simulated observations,
        containing the Psfs, Wcss, Calibs, etc. of the calexps to be simulated.

        Must be run after buildSkyMap.

        Most of the work is delegated to the mockObservation subtask.
        """
        if skyMap is None:
            skyMap = butler.get(self.config.coaddName + "Coadd_skyMap")
        if camera is None:
            camera = butler.get("camera")
        catalog = self.mockObservation.run(butler=butler,
                                           n=self.config.nObservations, camera=camera,
                                           tractInfo=skyMap[tract])
        if butler is not None:
            butler.put(catalog, "observations", tract=tract)
        return catalog

    def buildInputImages(self, butler, obsCatalog=None, truthCatalog=None, tract=0):
        """Use the truth catalog and observation catalog to create and save (if butler is not None)
        mock calexps and an ExposureCatalog ('simsrc') that contains information about which objects
        appear partially or fully in each exposure.

        Must be run after buildTruthCatalog and buildObservationCatalog.
        """
        if obsCatalog is None:
            obsCatalog = butler.get("observations", tract=tract)
        if truthCatalog is None:
            truthCatalog = butler.get("truth", tract=tract)
        ccdKey = obsCatalog.getSchema().find("ccd").key
        visitKey = obsCatalog.getSchema().find("visit").key
        simSrcCatalog = lsst.afw.table.SimpleCatalog(self.schema)
        for obsRecord in obsCatalog:
            ccd = obsRecord.getI(ccdKey)
            visit = obsRecord.getI(visitKey)
            self.log.info("Generating image for visit={visit}, ccd={ccd}".format(ccd=ccd, visit=visit))
            exposure = lsst.afw.image.ExposureF(obsRecord.getBBox())
            exposure.setCalib(obsRecord.getCalib())
            exposure.setWcs(obsRecord.getWcs())
            exposure.setPsf(obsRecord.getPsf())
            exposure.getInfo().setApCorrMap(obsRecord.getApCorrMap())
            for truthRecord in truthCatalog:
                status = self.mockObject.drawSource(truthRecord, exposure, buffer=self.config.edgeBuffer)
                if status:
                    simSrcRecord = simSrcCatalog.addNew()
                    simSrcRecord.setCoord(truthRecord.getCoord())
                    simSrcRecord.setL(self.objectIdKey, truthRecord.getId())
                    simSrcRecord.setL(self.exposureIdKey, obsRecord.getId())
                    simSrcRecord.setFlag(self.centroidInBBoxKey, obsRecord.contains(truthRecord.getCoord()))
                    simSrcRecord.setFlag(self.partialOverlapKey, status == 1)
                    self.log.info("  added object {id}".format(id=truthRecord.getId()))
            exposure.getMaskedImage().getVariance().set(1.0)
            if butler is not None:
                butler.put(exposure, "calexp", ccd=ccd, visit=visit)
        if butler is not None:
            butler.put(simSrcCatalog, "simsrc", tract=tract)
        return simSrcCatalog

    def buildAllInputs(self, butler):
        """Convenience function that calls buildSkyMap, buildObservationCatalog, buildTruthCatalog,
        and buildInputImages.
        """
        skyMap = self.buildSkyMap(butler)
        observations = self.buildObservationCatalog(butler, skyMap=skyMap)
        truth = self.buildTruthCatalog(butler, skyMap=skyMap)
        simSrcCatalog = self.buildInputImages(butler, obsCatalog=observations, truthCatalog=truth)

    def makeCoaddTask(self, cls, assemblePsfMatched=False):
        """Helper function to create a Coadd task with configuration appropriate for the simulations.

        MockCoaddTask does not include MakeCoaddTempExpTask or AssembleCoaddTask as subtasks, because
        we want explicit control over their configs, rather than leaving this up to the user.
        However, we have to install our own SelectImages task for both of these, so it made sense
        to have a single method that would create one of these two tasks, set the config values we
        want, and install the custom SelectImagesTask.
        """
        config = cls.ConfigClass()
        config.coaddName = self.config.coaddName
        config.select.retarget(MockSelectImagesTask)
        if cls == MakeCoaddTempExpTask:
            config.bgSubtracted = True
            config.makeDirect = True
            config.makePsfMatched = True
            config.modelPsf.defaultFwhm = 9
            config.modelPsf.addWing = False
            config.warpAndPsfMatch.psfMatch.kernel['AL'].scaleByFwhm = False
            config.warpAndPsfMatch.psfMatch.kernel['AL'].kernelSize = 25
            config.warpAndPsfMatch.psfMatch.kernel['AL'].sizeCellX = 64
            config.warpAndPsfMatch.psfMatch.kernel['AL'].sizeCellY = 64

        elif cls in [AssembleCoaddTask, SafeClipAssembleCoaddTask, CompareWarpAssembleCoaddTask]:
            if assemblePsfMatched:
                config.warpType = 'psfMatched'
            if cls != AssembleCoaddTask:
                config.doWrite = False
            if cls == CompareWarpAssembleCoaddTask:
                config.assembleStaticSkyModel.select.retarget(MockSelectImagesTask)
        return cls(config=config)

    def iterPatchRefs(self, butler, tractInfo):
        """Generator that iterates over the patches in a tract, yielding dataRefs.
        """
        nPatchX, nPatchY = tractInfo.getNumPatches()
        for iPatchX in range(nPatchX):
            for iPatchY in range(nPatchY):
                patchRef = butler.dataRef(self.config.coaddName + "Coadd",
                                          tract=tractInfo.getId(), patch="%d,%d" % (iPatchX, iPatchY),
                                          filter='r')
                yield patchRef

    def buildCoadd(self, butler, skyMap=None, tract=0):
        """Run the coadd tasks (MakeCoaddTempExp and AssembleCoadd) on the mock data.

        Must be run after buildInputImages.
        Makes both direct and PSF-matched coadds
        """
        if skyMap is None:
            skyMap = butler.get(self.config.coaddName + "Coadd_skyMap")
        tractInfo = skyMap[tract]
        makeCoaddTempExpTask = self.makeCoaddTask(MakeCoaddTempExpTask)
        directCoaddTaskList = []
        for coaddTask in [SafeClipAssembleCoaddTask, CompareWarpAssembleCoaddTask, AssembleCoaddTask]:
            directCoaddTaskList.append(self.makeCoaddTask(coaddTask))
        assemblePsfMatchedCoaddTask = self.makeCoaddTask(AssembleCoaddTask, assemblePsfMatched=True)
        for patchRef in self.iterPatchRefs(butler, tractInfo):
            makeCoaddTempExpTask.run(patchRef)
        for patchRef in self.iterPatchRefs(butler, tractInfo):
            for directCoaddTask in directCoaddTaskList:
                directCoaddTask.run(patchRef)
            assemblePsfMatchedCoaddTask.run(patchRef)

    def buildMockCoadd(self, butler, truthCatalog=None, skyMap=None, tract=0):
        """Directly create a simulation of the coadd, using the CoaddPsf (and ModelPsf)
        of the direct (and psfMatched) coadd exposure and the truth catalog.

        Must be run after buildCoadd.
        """
        if truthCatalog is None:
            truthCatalog = butler.get("truth", tract=tract)
        if skyMap is None:
            skyMap = butler.get(self.config.coaddName + "Coadd_skyMap")
        tractInfo = skyMap[tract]
        tractWcs = tractInfo.getWcs()
        for patchRef in self.iterPatchRefs(butler, tractInfo):
            # TODO: pybind11 remove `immediate=True` once DM-9112 is resolved
            for dataProduct in ["Coadd", "CoaddPsfMatched"]:
                exposure = patchRef.get(self.config.coaddName + dataProduct, immediate=True)
                exposure.getMaskedImage().getImage().set(0.0)
                coaddPsf = lsst.meas.algorithms.CoaddPsf(
                    exposure.getInfo().getCoaddInputs().ccds, exposure.getWcs()
                )
                exposure.setPsf(coaddPsf)
                for truthRecord in truthCatalog:
                    self.mockObject.drawSource(truthRecord, exposure, buffer=0)
                patchRef.put(exposure, self.config.coaddName + dataProduct + "_mock")

def run(root):
    """Convenience function to create and run MockCoaddTask with default settings.
    """
    from .simpleMapper import makeDataRepo
    butler = makeDataRepo(root=root)
    task = MockCoaddTask()
    task.buildAllInputs(butler)
    task.buildCoadd(butler)
    task.buildMockCoadd(butler)
