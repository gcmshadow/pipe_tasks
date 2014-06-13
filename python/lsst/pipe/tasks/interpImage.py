from __future__ import division, absolute_import
#
# LSST Data Management System
# Copyright 2008, 2009, 2010, 2011, 2012 LSST Corporation.
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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <http://www.lsstcorp.org/LegalNotices/>.
#
import lsst.pex.config as pexConfig
import lsst.afw.math as afwMath
import lsst.meas.algorithms as measAlg
import lsst.pipe.base as pipeBase
import lsst.ip.isr as ipIsr

__all__ = ["InterpImageConfig", "InterpImageTask"]

class InterpImageConfig(pexConfig.Config):
    """Config for InterpImageTask
    """
    modelPsf = measAlg.GaussianPsfFactory.makeField(doc = "Model Psf factory")

    useFallbackValueAtEdge = pexConfig.Field(
        dtype = bool,
        doc = "Smoothly taper (on the PSF scale) to the fallback value at the edge of the image?",
        default = True,
    )

class InterpImageTask(pipeBase.Task):
    """Interpolate over bad image pixels
    """
    ConfigClass = InterpImageConfig
    _DefaultName = "interpImage"

    @pipeBase.timeMethod
    def run(self, maskedImage, planeName, fwhmPixels=None, fallbackValue=None):
        """Interpolate in place over the pixels in a maskedImage which are marked bad by a mask plane

        Note that the interpolation code in meas_algorithms currently
        doesn't use the input PSF (though it's a required argument),
        so it's not important to set the input PSF parameters exactly.

        @param[in,out] maskedImage: MaskedImage over which to interpolate over edge pixels
        @param[in] planeName: mask plane over which to interpolate
        @param[in] fwhmPixels: FWHM of core star (pixels); if None then the default is used
        @param[in] fallbackValue Pixel value to use when all else fails (if None, use median)
        """
        return self.interpolateOnePlane(maskedImage, planeName, fwhmPixels, fallbackValue)

    @pipeBase.timeMethod
    def interpolateOnePlane(self, maskedImage, planeName, fwhmPixels=None, fallbackValue=None):
        """Interpolate in place over the pixels in a maskedImage which are marked bad by a mask plane

        Note that the interpolation code in meas_algorithms currently
        doesn't use the input PSF (though it's a required argument),
        so it's not important to set the input PSF parameters exactly.

        @param[in,out] maskedImage: MaskedImage over which to interpolate over edge pixels
        @param[in] planeName: mask plane over which to interpolate
        @param[in] fwhmPixels: FWHM of core star (pixels); if None then the default is used
        @param[in] fallbackValue Pixel value to use when all else fails (if None, use median)
        """
        self.log.info("Interpolate over %s pixels" % (planeName,))
        psfModel = self.config.modelPsf.apply(fwhm=fwhmPixels)


        if fallbackValue is None:
            fallbackValue = afwMath.makeStatistics(maskedImage, afwMath.MEDIAN).getValue()

        nanDefectList = ipIsr.getDefectListFromMask(maskedImage, planeName, growFootprints=0)
        measAlg.interpolateOverDefects(maskedImage, psfModel, nanDefectList, fallbackValue,
                                       self.config.useFallbackValueAtEdge)
