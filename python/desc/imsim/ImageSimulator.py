"""
Code to manage the parallel simulation of sensors using the
multiprocessing module.
"""
import os
import sys
import re
import multiprocessing
import warnings
import numpy as np
from lsst.afw.cameraGeom import WAVEFRONT, GUIDER
from lsst.sims.photUtils import BandpassDict
from lsst.sims.GalSimInterface import make_galsim_detector
from lsst.sims.GalSimInterface import make_gs_interpreter
from lsst.sims.GalSimInterface import LSSTCameraWrapper
from lsst.sims.GalSimInterface import GalSimInterpreter
from .imSim import read_config, parsePhoSimInstanceFile, add_cosmic_rays,\
    add_treering_info
from .bleed_trails import apply_channel_bleeding
from .skyModel import make_sky_model

__all__ = ['ImageSimulator']

# This is global variable is needed since instances of ImageSimulator
# contain references to unpickleable objects in the LSST Stack (e.g.,
# various cameraGeom objects), and the multiprocessing module can only
# execute pickleable callback functions. The SimulateSensor functor
# class below uses the global image_simulator variable to access its
# gs_interpreter objects, and the ImageSimulator.run method sets
# image_simulator to self so that it is available in the callbacks.
image_simulator = None

class ImageSimulator:
    """
    Class to manage the parallel simulation of sensors using the
    multiprocessing module.
    """
    def __init__(self, instcat, psf, numRows=None, config=None, seed=267,
                 outdir='fits', sensor_list=None, apply_sensor_model=True,
                 file_id=None):
        """
        Parameters
        ----------
        instcat: str
            The instance catalog for the desired visit.
        psf: lsst.sims.GalSimInterface.PSFbase subclass
            PSF to use for drawing objects.  A single instance is used
            by all of the GalSimInterpreters so that memory for any
            atmospheric screen data can be shared among the processes.
        numRows: int [None]
            The number of rows to read in from the instance catalog.
            If None, then all rows will be read in.
        config: str [None]
            Filename of config file to use.  If None, then the default
            config will be used.
        seed: int [267]
            Random number seed to pass to the GalSimInterpreter objects.
        outdir: str ['fits']
            Output directory to write the FITS images.
        sensor_list: tuple or other container [None]
            The names of sensors (e.g., "R:2,2 S:1,1") to simulate.
            If None, then all sensors in the camera will be
            considered.
        apply_sensor_model: bool [True]
            Flag to apply galsim.SiliconSensor model.
        file_id: str [None]
            string to use for the output files like the checkpoint file.
            If None, then no checkpoint file will be used
        """
        self.config = read_config(config)
        self.psf = psf
        self.outdir = outdir
        self.obs_md, self.phot_params, sources \
            = parsePhoSimInstanceFile(instcat, numRows=numRows)
        self.gs_obj_arr = sources[0]
        self.gs_obj_dict = sources[1]
        self.camera_wrapper = LSSTCameraWrapper()
        self.apply_sensor_model = apply_sensor_model
        self._make_gs_interpreters(seed, sensor_list, file_id)

    def _make_gs_interpreters(self, seed, sensor_list, file_id):
        """
        Create a separate GalSimInterpreter for each sensor so that they
        can be run in parallel and maintain separate checkpoint files.

        TODO: Find a good way to pass a different seed to each
        gs_interpreter or have them share the random number generator.
        """
        bp_dict = BandpassDict.loadTotalBandpassesFromFiles(bandpassNames=self.obs_md.bandpass)
        noise_and_background \
            = make_sky_model(self.obs_md, self.phot_params,
                             apply_sensor_model=self.apply_sensor_model)
        self.gs_interpreters = dict()
        for det in self.camera_wrapper.camera:
            det_type = det.getType()
            det_name = det.getName()
            if sensor_list is not None and det_name not in sensor_list:
                continue
            if det_type == WAVEFRONT or det_type == GUIDER:
                continue
            gs_det = make_galsim_detector(self.camera_wrapper, det_name,
                                          self.phot_params, self.obs_md)
            self.gs_interpreters[det_name] \
                = make_gs_interpreter(self.obs_md, [gs_det], bp_dict,
                                      noise_and_background,
                                      epoch=2000.0, seed=seed,
                                      apply_sensor_model=self.apply_sensor_model)
            self.gs_interpreters[det_name].sky_bg_per_pixel \
                = noise_and_background.sky_counts(det_name)
            self.gs_interpreters[det_name].setPSF(PSF=self.psf)
            if self.apply_sensor_model:
                add_treering_info(self.gs_interpreters[det_name])
            if file_id is not None:
                self.gs_interpreters[det_name].checkpoint_file \
                    = self.checkpoint_file(file_id, det_name)
                self.gs_interpreters[det_name].nobj_checkpoint \
                    = self.config['checkpointing']['nobj']
                self.gs_interpreters[det_name]\
                    .restore_checkpoint(self.camera_wrapper,
                                        self.phot_params,
                                        self.obs_md)

    @staticmethod
    def checkpoint_file(file_id, det_name):
        return '-'.join(('checkpoint', file_id,
                         re.sub('[:, ]', '_', det_name))) + '.ckpt'

    def run(self, processes=1):
        """
        Use multiprocessing module to simulate sensors in parallel.
        """
        # Set the image_simulator variable so that the SimulateSensor
        # instance can use it to access the GalSimInterpreter
        # instances to draw objects.
        global image_simulator
        if image_simulator is None:
            image_simulator = self
        elif id(image_simulator) != id(self):
            raise RuntimeError("Attempt to use more than one instance of "
                               "ImageSimulator in the same python interpreter")

        if processes == 1:
            # Don't need multiprocessing, so just run serially.
            for det_name in self.gs_interpreters:
                simulate_sensor = SimulateSensor(det_name)
                simulate_sensor(self.gs_obj_dict[det_name])
        else:
            # Use multiprocessing.
            pool = multiprocessing.Pool(processes=processes)
            results = []
            for det_name in self.gs_interpreters:
                simulate_sensor = SimulateSensor(det_name)
                gs_objects = self.gs_obj_dict[det_name]
                if len(gs_objects) > 0:
                    results.append(pool.apply_async(simulate_sensor,
                                                    (gs_objects,)))
            pool.close()
            pool.join()
            for res in results:
                res.get()


class SimulateSensor:
    """
    Functor class to serve as the callback for simulating sensors in
    parallel using the multiprocessing module.  Note that the
    image_simulator variable is defined in the global scope.
    """
    def __init__(self, sensor_name):
        """
        Parameters
        ----------
        sensor_name: str
            The name of the sensor to be simulated, e.g., "R:2,2 S:1,1"
        """
        self.sensor_name = sensor_name

    def __call__(self, gs_objects):
        """
        Draw objects using the corresponding GalSimInterpreter.

        Parameters
        ----------
        gs_objects: list of GalSimCelestialObjects
            The list of objects to draw.  This should be restricted to
            the objects for the corresponding sensor.
        """
        if len(gs_objects) == 0:
            return

        print("drawing %i objects on %s" % (len(gs_objects), self.sensor_name))
        sys.stdout.flush()

        # image_simulator must be a variable declared in the
        # outer scope and set to an ImageSimulator instance.
        gs_interpreter = image_simulator.gs_interpreters[self.sensor_name]
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', 'Automatic n_photons',
                                    UserWarning)
            for gs_obj in gs_objects:
                if gs_obj.uniqueId in gs_interpreter.drawn_objects:
                    continue
                if not np.isnan(gs_obj.flux(image_simulator.obs_md.bandpass)):
                    gs_interpreter.drawObject(gs_obj)
                gs_obj.sed.delete_sed_obj()

        # Recover the memory devoted to the GalSimCelestialObject instances.
        gs_objects.reset()

        add_cosmic_rays(gs_interpreter, image_simulator.phot_params)
        full_well = int(image_simulator.config['ccd']['full_well'])
        apply_channel_bleeding(gs_interpreter, full_well)

        outdir = image_simulator.outdir
        if not os.path.isdir(outdir):
            os.makedirs(outdir)
        prefix = image_simulator.config['persistence']['eimage_prefix']
        obsHistID = str(image_simulator.obs_md.OpsimMetaData['obshistID'])
        gs_interpreter.writeImages(nameRoot=os.path.join(outdir, prefix)
                                   + obsHistID)

#        # The image for the sensor-visit has been drawn, so delete the
#        # checkpoint file.
#        os.remove(gs_interpreter.checkpoint_file)

        # Remove reference to gs_interpreter in order to recover the
        # memory associated with that object.
        image_simulator.gs_interpreters[self.sensor_name] = None