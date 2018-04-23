#!/usr/bin/env python
"""
This is the imSim program, used to drive GalSim to simulate the LSST.  Written
for the DESC collaboration and LSST project.  This version of the program can
read phoSim instance files as is. It leverages the LSST Sims GalSim interface
code found in sims_GalSimInterface.
"""
from __future__ import absolute_import, print_function
import os
import argparse
import warnings
import numpy as np
from lsst.afw.cameraGeom import WAVEFRONT, GUIDER
from lsst.sims.photUtils import BandpassDict
from lsst.sims.GalSimInterface import make_galsim_detector
from lsst.sims.GalSimInterface import SNRdocumentPSF
from lsst.sims.GalSimInterface import LSSTCameraWrapper
from lsst.sims.GalSimInterface import Kolmogorov_and_Gaussian_PSF
from lsst.sims.GalSimInterface import make_gs_interpreter
from desc.imsim.skyModel import make_sky_model
import desc.imsim

def main():
    """
    Drive GalSim to simulate the LSST.
    """
    # Setup a parser to take command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('file', help="The instance catalog")
    parser.add_argument('-n', '--numrows', default=None, type=int,
                        help="Read the first numrows of the file.")
    parser.add_argument('--outdir', type=str, default='fits',
                        help='Output directory for eimage file')
    parser.add_argument('--sensor', type=str, default=None,
                        help='Sensor to simulate, e.g., "R:2,2 S:1,1".' +
                        'If None, then simulate all sensors with sources on them')
    parser.add_argument('--config_file', type=str, default=None,
                        help="Config file. If None, the default config will be used.")
    parser.add_argument('--log_level', type=str,
                        choices=['DEBUG', 'INFO', 'WARN', 'ERROR', 'CRITICAL'],
                        default='INFO', help='Logging level. Default: "INFO"')
    parser.add_argument('--psf', type=str, default='Kolmogorov',
                        choices=['DoubleGaussian', 'Kolmogorov'],
                        help="PSF model to use; either the double Gaussian "
                        "from LSE-40 (equation 30) or the Kolmogorov convolved "
                        "with a Gaussian proposed by David Kirkby at the "
                        "23 March 2017 SSims telecon")
    parser.add_argument('--disable_sensor_model', default=False,
                        action='store_true',
                        help='disable sensor effects')
    parser.add_argument('--checkpoint_file', type=str, default=None,
                        help='Checkpoint file name.')
    parser.add_argument('--nobj_checkpoint', type=int, default=1000,
                        help='# objects to process between checkpoints')
    parser.add_argument('--seed', type=int, default=267,
                        help='integer used to seed random number generator')
    arguments = parser.parse_args()

    config = desc.imsim.read_config(arguments.config_file)

    logger = desc.imsim.get_logger(arguments.log_level)

    # Get the number of rows to read from the instance file.  Use
    # default if not specified.
    numRows = arguments.numrows
    if numRows is not None:
        logger.info("Reading %i rows from the instance catalog %s.",
                    numRows, arguments.file)
    else:
        logger.info("Reading all rows from the instance catalog %s.",
                    arguments.file)

    camera_wrapper = LSSTCameraWrapper()

    catalog_contents = desc.imsim.parsePhoSimInstanceFile(arguments.file,
                                                          numRows=numRows)

    obs_md = catalog_contents.obs_metadata
    phot_params = catalog_contents.phot_params
    sources = catalog_contents.sources
    gs_object_arr = sources[0]
    gs_object_dict = sources[1]

    # Sub-divide the source dataframe into stars and galaxies.
    if arguments.sensor is not None:
        detector_list = [make_galsim_detector(camera_wrapper, arguments.sensor,
                                              phot_params, obs_md)]
    else:
        detector_list = []
        for det in camera_wrapper.camera:
            det_type = det.getType()
            if det_type != WAVEFRONT and det_type != GUIDER:
                detector_list.append(make_galsim_detector(camera_wrapper, det.getName(),
                                                          phot_params, obs_md))

    apply_sensor_model = not arguments.disable_sensor_model
    noise_and_background \
        = make_sky_model(obs_md, phot_params, addNoise=True, addBackground=True,
                         apply_sensor_model=apply_sensor_model,
                         logger=logger)

    bp_dict = BandpassDict.loadTotalBandpassesFromFiles(bandpassNames=obs_md.bandpass)

    gs_interpreter = make_gs_interpreter(obs_md, detector_list, bp_dict,
                                         noise_and_background,
                                         epoch=2000.0,
                                         seed=arguments.seed,
                                         apply_sensor_model=apply_sensor_model)
    gs_interpreter.sky_bg_per_pixel \
        = noise_and_background.sky_counts(arguments.sensor)

    gs_interpreter.checkpoint_file = arguments.checkpoint_file
    gs_interpreter.nobj_checkpoint = arguments.nobj_checkpoint
    gs_interpreter.restore_checkpoint(camera_wrapper,
                                      phot_params,
                                      obs_md)

    # Add a PSF.
    if arguments.psf.lower() == "doublegaussian":
        # This one is taken from equation 30 of
        # www.astro.washington.edu/users/ivezic/Astr511/LSST_SNRdoc.pdf .
        #
        # Set seeing from self.obs_metadata.
        local_PSF = \
            SNRdocumentPSF(obs_md.OpsimMetaData['FWHMgeom'])
    elif arguments.psf.lower() == "kolmogorov":
        # This PSF was presented by David Kirkby at the 23 March 2017
        # Survey Simulations Working Group telecon
        #
        # https://confluence.slac.stanford.edu/pages/viewpage.action?spaceKey=LSSTDESC&title=SSim+2017-03-23

        # equation 3 of Krisciunas and Schaefer 1991
        airmass = 1.0/np.sqrt(1.0-0.96*(np.sin(0.5*np.pi-obs_md.OpsimMetaData['altitude']))**2)

        local_PSF = \
            Kolmogorov_and_Gaussian_PSF(airmass=airmass,
                                        rawSeeing=obs_md.OpsimMetaData['rawSeeing'],
                                        band=obs_md.bandpass)
    else:
        raise RuntimeError("Do not know what to do with psf model: "
                           "%s" % arguments.psf)

    gs_interpreter.setPSF(PSF=local_PSF)

    desc.imsim.add_treering_info(gs_interpreter)

    if arguments.sensor is not None:
        gs_objects_to_draw = gs_object_dict[arguments.sensor]
    else:
        gs_objects_to_draw = gs_object_arr

    for gs_obj in gs_objects_to_draw:
        if gs_obj.uniqueId in gs_interpreter.drawn_objects:
            continue
        gs_interpreter.drawObject(gs_obj)
        # Delete underlying .sed.sed_obj to release associated memory.
        gs_obj.sed.delete_sed_obj()

    desc.imsim.add_cosmic_rays(gs_interpreter, phot_params)

    # Write out the fits files
    outdir = arguments.outdir
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    prefix = config['persistence']['eimage_prefix']
    gs_interpreter.writeImages(nameRoot=os.path.join(outdir, prefix) +
                                        str(obs_md.OpsimMetaData['obshistID']))


if __name__ == "__main__":
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', 'Automatic n_photons', UserWarning)
        main()
