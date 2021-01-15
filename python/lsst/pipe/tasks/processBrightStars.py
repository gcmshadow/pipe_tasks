# This file is part of pipe_tasks.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
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
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Extract small cutouts around bright stars, normalize and warp them to the
same arbitrary pixel grid.
"""

__all__ = ["ProcessBrightStarsTask"]

import numpy as np
import astropy.units as u

from lsst import geom
from lsst.afw import math as afwMath
from lsst.afw import image as afwImage
from lsst.afw import cameraGeom as cg
from lsst.afw.geom import transformFactory as tFactory
import lsst.pex.config as pexConfig
from lsst.pipe import base as pipeBase
from lsst.pipe.base import connectionTypes as cT
from lsst.pex.exceptions import InvalidParameterError
from lsst.meas.algorithms.loadIndexedReferenceObjects import LoadIndexedReferenceObjectsTask
from lsst.meas.algorithms import ReferenceObjectLoader
from lsst.meas.algorithms import brightStarStamps as bSS


class ProcessBrightStarsConnections(pipeBase.PipelineTaskConnections, dimensions=("visit", "detector")):
    inputExposure = cT.Input(
        doc="Input exposure from which to extract bright star stamps",
        name="calexp",
        storageClass="ExposureF",
        dimensions=("visit", "detector")
    )
    refCat = cT.PrerequisiteInput(
        doc="Reference catalog that contains bright star positions",
        name="gaia_dr2_20200414",
        storageClass="SimpleCatalog",
        dimensions=("skypix",),
        multiple=True,
        deferLoad=True
    )
    brightStarStamps = cT.Output(
        doc="Set of preprocessed postage stamps, each centered on a single bright star.",
        name="brightStarStamps",
        storageClass="BrightStarStamps",
        dimensions=("visit", "detector")
    )


class ProcessBrightStarsConfig(pipeBase.PipelineTaskConfig,
                               pipelineConnections=ProcessBrightStarsConnections):
    """Configuration parameters for ProcessBrightStarsTask
    """
    magLimit = pexConfig.Field(
        dtype=float,
        doc="Magnitude limit, in Gaia G; all stars brighter than this value will be processed",
        default=18
    )
    stampSize = pexConfig.ListField(
        dtype=int,
        doc="Size of the stamps to be extracted, in pixels",
        default=(250, 250)
    )
    modelStampBuffer = pexConfig.Field(
        dtype=float,
        doc="'Buffer' factor to be applied to determine the size of the stamp the processed stars will "
            "be saved in. This will also be the size of the extended PSF model.",
        default=1.1
    )
    warpingKernelName = pexConfig.ChoiceField(
        dtype=str,
        doc="Warping kernel",
        default="lanczos5",
        allowed={
            "bilinear": "bilinear interpolation",
            "lanczos3": "Lanczos kernel of order 3",
            "lanczos4": "Lanczos kernel of order 4",
            "lanczos5": "Lanczos kernel of order 5",
        }
    )
    annularFluxRadii = pexConfig.ListField(
        dtype=int,
        doc="Inner and outer radii of the annulus used to compute the AnnularFlux for normalization, "
            "in pixels.",
        default=(40, 50)
    )
    annularFluxStatistic = pexConfig.ChoiceField(
        dtype=str,
        doc="Type of statistic to use to compute annular flux.",
        default="MEANCLIP",
        allowed={
            "MEAN": "mean",
            "MEDIAN": "median",
            "MEANCLIP": "clipped mean",
        }
    )
    numSigmaClip = pexConfig.Field(
        dtype=float,
        doc="Sigma for outlier rejection; ignored if annularFluxStatistic != 'MEANCLIP'.",
        default=4
    )
    numIter = pexConfig.Field(
        dtype=int,
        doc="Number of iterations of outlier rejection; ignored if annularFluxStatistic != 'MEANCLIP'.",
        default=3
    )
    badMaskPlanes = pexConfig.ListField(
        dtype=str,
        doc="Mask planes that, if set, lead to associated pixels not being included in the computation of the"
            " annular flux.",
        default=('BAD', 'CR', 'CROSSTALK', 'EDGE', 'NO_DATA', 'SAT', 'SUSPECT', 'UNMASKEDNAN')
    )
    minPixelsWithinFrame = pexConfig.Field(
        dtype=int,
        doc="When a bright star is beyond exposure boundary, what is the minimal number of pixels that still "
            "fall within the stamp required for it to be saved",
        default=50
    )
    refObjLoader = pexConfig.ConfigurableField(
        target=LoadIndexedReferenceObjectsTask,
        doc="reference object loader for astrometric calibration",
    )

    def setDefaults(self):
        self.refObjLoader.ref_dataset_name = "gaia_dr2_20200414"


class ProcessBrightStarsTask(pipeBase.PipelineTask, pipeBase.CmdLineTask):
    """The description of the parameters for this Task are detailed in
    :lsst-task:`~lsst.pipe.base.PipelineTask`.

    Notes
    -----
    `ProcessBrightStarsTask` is used to extract, process, and store small
    image cut-outs (or "postage stamps") around bright stars. It relies on
    three methods, called in succession:

    `extractStamps`
        Find bright stars within the exposure using a reference catalog and
        extract a stamp centered on each.
    `warpStamps`
        Shift and warp each stamp to remove optical distortions and sample all
        stars on the same pixel grid.
    `measureAndNormalize`
        Compute the flux of an object in an annulus and normalize it. This is
        required to normalize each bright star stamp as their central pixels
        are likely saturated and/or contain ghosts, and cannot be used.
    """
    ConfigClass = ProcessBrightStarsConfig
    _DefaultName = "processBrightStars"
    RunnerClass = pipeBase.ButlerInitializedTaskRunner

    def __init__(self, butler=None, initInputs=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Compute (model) stamp size depending on provided "buffer" value
        self.modelStampSize = (int(self.config.stampSize[0]*self.config.modelStampBuffer),
                               int(self.config.stampSize[1]*self.config.modelStampBuffer))
        # force it to be odd-sized so we have a central pixel
        if not self.modelStampSize[0] % 2:
            self.modelStampSize[0] += 1
        if not self.modelStampSize[1] % 2:
            self.modelStampSize[1] += 1
        # central pixel
        self.modelCenter = self.modelStampSize[0]//2, self.modelStampSize[1]//2
        # configure Gaia refcat
        if butler is not None:
            self.makeSubtask('refObjLoader', butler=butler)

    def extractStamps(self, inputExposure, refObjLoader=None):
        """ Read position of bright stars within `inputExposure` from refCat
        and extract them.

        Parameters
        ----------
        inputExposure : `afwImage.exposure.exposure.ExposureF`
            The image from which bright star stamps should be extracted.
        refObjLoader : `LoadIndexedReferenceObjectsTask`, optional
            Loader to find objects within a reference catalog.

        Returns
        -------
        result : `lsst.pipe.base.Struct`
            Result struct with components:

            - ``starIms``: `list` of stamps
            - ``pixCenters``: `list` of corresponding coordinates to each
                star's center, in pixels.
            - ``GMags``: `list` of corresponding (Gaia) G magnitudes.
            - ``gaiaIds``: `np.ndarray` of corresponding unique Gaia
                identifiers.
        """
        if refObjLoader is None:
            refObjLoader = self.refObjLoader
        starIms = []
        pixCenters = []
        GMags = []
        ids = []
        wcs = inputExposure.getWcs()
        bmp = inputExposure.mask.getMaskPlaneDict()
        # select stars within, or close enough to input exposure from refcat
        dilatationExtent = geom.Extent2I(np.array(self.config.stampSize) - self.config.minPixelsWithinFrame)
        bbox = inputExposure.getBBox().dilatedBy(dilatationExtent)
        withinCalexp = refObjLoader.loadPixelBox(bbox, wcs, filterName="phot_g_mean")
        refCat = withinCalexp.refCat
        # keep bright objects
        fluxLimit = ((self.config.magLimit*u.ABmag).to(u.nJy)).to_value()
        GFluxes = np.array(refCat['phot_g_mean_flux'])
        bright = GFluxes > fluxLimit
        # convert to AB magnitudes
        allGMags = [((gFlux*u.nJy).to(u.ABmag)).to_value() for gFlux in GFluxes[bright]]
        allIds = refCat.columns.extract("id", where=bright)["id"]
        selectedColumns = refCat.columns.extract('coord_ra', 'coord_dec', where=bright)
        for j, (ra, dec) in enumerate(zip(selectedColumns["coord_ra"], selectedColumns["coord_dec"])):
            sp = geom.SpherePoint(ra, dec, geom.radians)
            cpix = wcs.skyToPixel(sp)
            try:
                starIm = inputExposure.getCutout(sp, geom.Extent2I(self.config.stampSize))
            except InvalidParameterError:
                # star is beyond boundary
                bboxCorner = np.array(cpix) - np.array(self.config.stampSize)/2
                bboxCorner = geom.Point2I(bboxCorner)
                # compute bbox as it would be otherwise
                idealBBox = geom.Box2I(geom.Point2I(bboxCorner), geom.Extent2I(self.config.stampSize))
                bottomLeft = idealBBox.getBegin()
                topRight = idealBBox.getEnd()
                # can any pixel be salvaged?
                if np.any(np.array(bottomLeft) > np.array(inputExposure.getDimensions())) or \
                   np.any(np.array(topRight) < 0):
                    continue
                # create smaller subBBox containing overlapping pixels
                subBBoxCorner = bottomLeft.clone()
                subBBoxExtent = idealBBox.getDimensions()
                beyondX = topRight[0] - inputExposure.getDimensions()[0]
                beyondY = topRight[1] - inputExposure.getDimensions()[1]
                if bottomLeft[0] < 0:
                    subBBoxCorner[0] = 0
                    subBBoxExtent[0] -= np.abs(bottomLeft[0])
                elif beyondX > 0:
                    subBBoxExtent[0] -= beyondX
                if bottomLeft[1] < 0:
                    subBBoxCorner[1] = 0
                    subBBoxExtent[1] -= np.abs(bottomLeft[1])
                elif beyondY > 0:
                    subBBoxExtent[1] -= beyondY
                subBBox = geom.Box2I(subBBoxCorner, subBBoxExtent)
                # extract pixels if any
                subSp = wcs.pixelToSky(subBBox.getCenter())
                subStarIm = inputExposure.getCutout(subSp, subBBoxExtent)
                # and create full-sized stamp with NO_DATA elsewhere
                starIm = afwImage.ExposureF(idealBBox)
                starIm.setDetector(inputExposure.getDetector())
                starIm.setWcs(inputExposure.getWcs())
                starIm.image.array[:] = np.nan
                starIm.mask.array[:] = 2**bmp['NO_DATA']
                starIm.image[subBBox] = subStarIm.image
                starIm.mask[subBBox] = subStarIm.mask
            starIms.append(starIm)
            pixCenters.append(cpix)
            GMags.append(allGMags[j])
            ids.append(allIds[j])
        return pipeBase.Struct(starIms=starIms,
                               pixCenters=pixCenters,
                               GMags=GMags,
                               gaiaIds=ids)

    def warpStamps(self, stamps, pixCenters):
        """Warps and shifts all given stamps so they are sampled on the same
        pixel grid and centered on the central pixel. This includes rotating
        the stamp depending on detector orientation.

        Parameters
        ----------
        stamps : `collections.abc.Sequence`
                     [`afwImage.exposure.exposure.ExposureF`]
            Image cutouts centered on a single object.
        pixCenters : `collections.abc.Sequence` [`geom.Point2D`]
            Positions of each object's center (as obtained from the refCat),
            in pixels.

        Returns
        -------
        warpedStars : `list` [`afwImage.maskedImage.maskedImage.MaskedImage`]
        """
        # warping control; only contains shiftingALg provided in config
        warpCont = afwMath.WarpingControl(self.config.warpingKernelName)
        # Compare model to star stamp sizes
        bufferPix = (self.modelStampSize[0] - self.config.stampSize[0],
                     self.modelStampSize[1] - self.config.stampSize[1])
        # Initialize detector instance (note all stars were extracted from an
        # exposure from the same detector)
        det = stamps[0].getDetector()
        # Define correction for optical distortions
        pixToTan = det.getTransform(cg.PIXELS, cg.TAN_PIXELS)
        # Array of all possible rotations for detector orientation:
        possibleRots = np.array([k*np.pi/2 for k in range(4)])
        # determine how many, if any, rotations are required
        yaw = det.getOrientation().getYaw()
        nb90Rots = np.argmin(np.abs(possibleRots - float(yaw)))

        # apply transformation to each star
        warpedStars = []
        for star, cent in zip(stamps, pixCenters):
            # (re)create empty destination image
            destImage = afwImage.MaskedImageF(*self.modelStampSize)
            bottomLeft = geom.Point2D(star.getImage().getXY0())
            newBottomLeft = pixToTan.applyForward(bottomLeft)
            newBottomLeft.setX(newBottomLeft.getX() - bufferPix[0]/2)
            newBottomLeft.setY(newBottomLeft.getY() - bufferPix[1]/2)
            # Convert to int
            newBottomLeft = geom.Point2I(newBottomLeft)
            # Set origin
            destImage.setXY0(newBottomLeft)

            # Define linear shifting to recenter stamps
            newCenter = pixToTan.applyForward(cent)  # center of warped star
            shift = self.modelCenter[0] + newBottomLeft[0] - newCenter[0],\
                self.modelCenter[1] + newBottomLeft[1] - newCenter[1]
            affineShift = geom.AffineTransform(shift)
            shiftTransform = tFactory.makeTransform(affineShift)

            # Define full transform (warp and shift)
            starWarper = pixToTan.then(shiftTransform)

            # Apply it
            goodPix = afwMath.warpImage(destImage, star.getMaskedImage(),
                                        starWarper, warpCont)
            if not goodPix:
                self.log.debug("Warping of a star failed: no good pixel in output")

            # Arbitrarily set origin of shifted star to 0
            destImage.setXY0(0, 0)

            # Apply rotation if apropriate
            if nb90Rots:
                destImage = afwMath.rotateImageBy90(destImage, nb90Rots)
            warpedStars.append(destImage.clone())
        return warpedStars

    @pipeBase.timeMethod
    def run(self, inputExposure, refObjLoader=None, dataId=None):
        """Identify bright stars within an exposure using a reference catalog,
        extract stamps around each, then preprocess them. The preprocessing
        steps are: shifting, warping and potentially rotating them to the same
        pixel grid; computing their annular flux and normalizing them.

        Parameters
        ----------
        inputExposure : `afwImage.exposure.exposure.ExposureF`
            The image from which bright star stamps should be extracted.
        refObjLoader : `LoadIndexedReferenceObjectsTask`, optional
            Loader to find objects within a reference catalog.
        dataId : `dict` or `lsst.daf.butler.DataCoordinate`
            The dataId of the exposure (and detector) bright stars should be
            extracted from.

        Returns
        -------
        result :  `lsst.pipe.base.Struct`
            Result struct with component:

            - ``brightStarStamps``: ``bSS.BrightStarStamps``
        """
        self.log.info("Extracting bright stars from exposure %s", dataId)
        # Extract stamps around bright stars
        extractedStamps = self.extractStamps(inputExposure, refObjLoader=refObjLoader)
        # Warp (and shift, and potentially rotate) them
        self.log.info("Applying warp to %i star stamps from exposure %s",
                      len(extractedStamps.starIms), dataId)
        warpedStars = self.warpStamps(extractedStamps.starIms, extractedStamps.pixCenters)
        brightStarList = [bSS.BrightStarStamp(stamp_im=warp,
                                              gaiaGMag=extractedStamps.GMags[j],
                                              gaiaId=extractedStamps.gaiaIds[j])
                          for j, warp in enumerate(warpedStars)]
        # Compute annularFlux and normalize
        self.log.info("Computing annular flux and normalizing %i bright stars from exposure %s",
                      len(warpedStars), dataId)
        # annularFlux statistic set-up, excluding mask planes
        statsControl = afwMath.StatisticsControl()
        statsControl.setNumSigmaClip(self.config.numSigmaClip)
        statsControl.setNumIter(self.config.numIter)
        innerRadius, outerRadius = self.config.annularFluxRadii
        statsFlag = afwMath.stringToStatisticsProperty(self.config.annularFluxStatistic)
        brightStarStamps = bSS.BrightStarStamps.initAndNormalize(brightStarList,
                                                                 innerRadius=innerRadius,
                                                                 outerRadius=outerRadius,
                                                                 imCenter=self.modelCenter,
                                                                 statsControl=statsControl,
                                                                 statsFlag=statsFlag,
                                                                 badMaskPlanes=self.config.badMaskPlanes)
        return pipeBase.Struct(brightStarStamps=brightStarStamps)

    def runDataRef(self, dataRef):
        """ Read in required calexp, extract and process stamps around bright
        stars and write them to disk.

        Parameters
        ----------
        dataRef : `lsst.daf.persistence.butlerSubset.ButlerDataRef`
            Data reference to the calexp to extract bright stars from.
        """
        calexp = dataRef.get("calexp")
        output = self.run(calexp, dataId=dataRef.dataId)
        # Save processed bright star stamps
        dataRef.put(output.brightStarStamps, "brightStarStamps")
        return pipeBase.Struct(brightStarStamps=output.brightStarStamps)

    def runQuantum(self, butlerQC, inputRefs, outputRefs):
        inputs = butlerQC.get(inputRefs)
        inputs['dataId'] = str(butlerQC.quantum.dataId)
        # TODO (DM-27262): remove workaround and load refcat in gen3
        self.log.info("Gaia refcat is not yet available in gen3; as a temporary fix, "
                      "reading it in from a gen2 butler")
        refObjLoader = ReferenceObjectLoader(dataIds=[ref.datasetRef.dataId
                                                      for ref in inputRefs.refCat],
                                             refCats=inputs.pop("refCat"),
                                             config=self.config.refObjLoader)
        output = self.run(**inputs, refObjLoader=refObjLoader)
        butlerQC.put(output, outputRefs)
