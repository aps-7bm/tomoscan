"""Software for tomography scanning with EPICS at APS beamline 7-BM-B

   Classes
   -------
   TomoScan7BM
     Derived class for tomography scanning with EPICS at APS beamline 7-BM-B
"""
import time
import os
import threading
import math
import h5py 
from pathlib import Path
import numpy as np
from epics import PV

from tomoscan import data_management as dm
from tomoscan.tomoscan import TomoScan
from tomoscan.tomoscan_helical import TomoScanHelical
from tomoscan import log

EPSILON = .001

class TomoScan7BM(TomoScanHelical):
    """Derived class used for tomography scanning with EPICS at APS beamline 7-BM-B

    Parameters
    ----------
    pv_files : list of str
        List of files containing EPICS pvNames to be used.
    macros : dict
        Dictionary of macro definitions to be substituted when
        reading the pv_files
    """
    def __init__(self, pv_files, macros):
        log.setup_custom_logger(lfname=Path.home().joinpath('logs','TomoScan_7BM.log'), stream_to_console=True)
        super().__init__(pv_files, macros)
        
        # set TomoScan xml files
        self.epics_pvs['CamNDAttributesFile'].put('TomoScanDetectorAttributes.xml')
        self.epics_pvs['FPXMLFileName'].put('TomoScanLayout.xml')
        macro = 'DET=' + self.pv_prefixes['Camera'] + ',' + 'TC=' + self.epics_pvs['Testing'].__dict__['pvname'].replace('Testing', '', 1)
        self.control_pvs['CamNDAttributesMacros'].put(macro)

        # Set the detector running in FreeRun mode
        self.set_trigger_mode('FreeRun', 1)
        
        # Set data directory
        self.epics_pvs['UpdateFilePath'].add_callback(self.pv_callback)
        self.default_file_path()

        # Enable auto-increment on file writer
        self.epics_pvs['FPAutoIncrement'].put('Yes')

        # Enable over-writing warning
        self.epics_pvs['OverwriteWarning'].put('Yes')


    def pv_callback(self, pvname=None, value=None, char_value=None, **kw):
        """Callback function that is called by pyEpics when certain EPICS PVs are changed
            Overload from base class to add file path refresh button.
        The PVs that are handled are:

        - ``UpdateFilePath`` : Calls ``default_file_path()``
        """
        super().pv_callback(pvname, value, char_value, **kw)
        if (pvname.find('UpdateFilePath') != -1) and (value == 1):
            thread = threading.Thread(target=self.default_file_path, args=())
            thread.start()


    def default_file_path(self):
        """Change the file path to one derived from the experiment data PVs.
        """
        file_path = Path(self.epics_pvs['DetectorTopDir'].get(as_string=True))
        file_path = file_path.joinpath(self.epics_pvs['ExperimentYearMonth'].get(as_string=True) + '-'
                                       + self.epics_pvs['UserLastName'].get(as_string=True) + '-'
                                       + self.epics_pvs['ProposalNumber'].get(as_string=True)) 
        self.epics_pvs['FilePath'].put(str(file_path), wait=True)
        self.epics_pvs['UpdateFilePath'].put('Done')
        
    def fly_scan(self):
        """Overrides fly_scan in super class to catch a bad file path.
        """
        if self.epics_pvs['FilePathExists'].get() == 1:
            log.info('file path for file writer exists')
            super(TomoScanHelical, self).fly_scan()
        else:
            log.info('file path for file writer not found')
            self.epics_pvs['ScanStatus'].put('Abort: Bad File Path')
            self.epics_pvs['StartScan'].put(0)
            self.scan_is_running = False


    def collect_static_frame(self):
        """Collects num_frames images in "InternalSingle" trigger mode for gains

        """
        # This is called when collecting dark fields or flat fields

        log.info('collect static frame')
        self.set_trigger_mode('InternalSingle', 1)
        self.epics_pvs['CamAcquire'].put('Acquire')
        # Wait for detector and file plugin to be ready
        time.sleep(0.5)
        frame_time = self.compute_frame_time()
        self.wait_camera_done(frame_time + 5.0)


    def open_shutter(self):
        """Opens the shutter to collect flat fields or projections.

        This does the following:

        - Checks if we are in testing mode.  If we are, do nothing.

        - Opens the front end shutter, waiting for it to indicate it is open.
            This is copied from the 2-BM implementation 9/2020

        - Opens the 7-BM-B fast shutter.
        """
        if self.epics_pvs['Testing'].get():
            log.warning('In testing mode, so not opening shutters.')
            return
        # Open the front end shutter
        if not self.epics_pvs['OpenShutter'] is None:
            pv = self.epics_pvs['OpenShutter']
            value = self.epics_pvs['OpenShutterValue'].get(as_string=True)
            status = self.epics_pvs['ShutterStatus'].get(as_string=True)
            log.info('shutter status: %s', status)
            log.info('open shutter: %s, value: %s', pv, value)
            self.epics_pvs['OpenShutter'].put(value, wait=True)
            self.wait_pv(self.epics_pvs['ShutterStatus'], 0)
            status = self.epics_pvs['ShutterStatus'].get(as_string=True)
            log.info('shutter status: %s', status)
            time.sleep(1.0)
        # Open 7-BM-B fast shutter
        if not self.epics_pvs['OpenFastShutter'] is None:
            pv = self.epics_pvs['OpenFastShutter']
            value = self.epics_pvs['OpenFastShutterValue'].get(as_string=True)
            log.info('open fast shutter: %s, value: %s', pv, value)
            self.epics_pvs['OpenFastShutter'].put(value, wait=True)


    def close_shutter(self):
        """Closes the shutter to collect dark fields and at the end of a scan
        This does the following:

        - Checks if we are in testing mode.  If we are, do nothing

        - Closes the 7-BM-B fast shutter.

        - Closes the beamline shutter.
       """
        if self.epics_pvs['Testing'].get():
            log.warning('In testing mode, so not closing shutters.')
            return
        # Close 7-BM-B fast shutter; don't wait for it
        if not self.epics_pvs['CloseFastShutter'] is None:
            pv = self.epics_pvs['CloseFastShutter']
            value = self.epics_pvs['CloseFastShutterValue'].get(as_string=True)
            log.info('close fast shutter: %s, value: %s', pv, value)
            self.epics_pvs['CloseFastShutter'].put(value, wait=False)
        # Call the base class method
        if not self.epics_pvs['CloseShutter'] is None:
            pv = self.epics_pvs['CloseShutter']
            value = self.epics_pvs['CloseShutterValue'].get(as_string=True)
            status = self.epics_pvs['ShutterStatus'].get(as_string=True)
            log.info('shutter status: %s', status)
            log.info('close shutter: %s, value: %s', pv, value)
            self.epics_pvs['CloseShutter'].put(value, wait=True)
            self.wait_pv(self.epics_pvs['ShutterStatus'], 1)
            status = self.epics_pvs['ShutterStatus'].get(as_string=True)
            log.info('shutter status: %s', status)


    def set_trigger_mode(self, trigger_mode, num_images):
        """Sets the trigger mode for the camera.

        Parameters
        ----------
        trigger_mode : str
            Choices are: "FreeRun", "Internal", or "PSOExternal"

        num_images : int
            Number of images to collect.  Ignored if trigger_mode="FreeRun".
            This is used to set the ``NumImages`` PV of the camera.
        """
        if trigger_mode == 'FreeRun':
            self.epics_pvs['CamImageMode'].put('Continuous', wait=True)
            self.epics_pvs['CamTriggerMode'].put('Off', wait=True)
            self.epics_pvs['CamAcquire'].put('Acquire')
        elif trigger_mode == 'Internal':
            self.epics_pvs['CamTriggerMode'].put('Off', wait=True)
            self.epics_pvs['CamImageMode'].put('Multiple', wait=True)
            self.epics_pvs['CamNumImages'].put(num_images, wait=True)
        elif trigger_mode == 'InternalSingle':
            self.epics_pvs['CamTriggerMode'].put('Off', wait=True)
            self.epics_pvs['CamImageMode'].put('Single')
        else: # set camera to external triggering
            self.epics_pvs['CamTriggerMode'].put('On', wait=True)
            ext_source = str(self.epics_pvs['ExternalTriggerSource'].get())
            self.epics_pvs['CamTriggerSource'].put(ext_source, wait=True)
            self.epics_pvs['CamTriggerOverlap'].put('ReadOut', wait=True)
            self.epics_pvs['CamExposureMode'].put('Timed', wait=True)

            self.epics_pvs['CamImageMode'].put('Multiple')
            self.epics_pvs['CamArrayCallbacks'].put('Enable')
            self.epics_pvs['CamFrameRateEnable'].put(0)

            self.epics_pvs['CamNumImages'].put(self.num_angles, wait=True)

            self.epics_pvs['CamTriggerMode'].put('On', wait=True)
            self.wait_pv(self.epics_pvs['CamTriggerMode'], 1)


    def collect_flat_fields(self):
        """Collects flat field data

        This overrides the super class's method to allow for camera gain
        images to be acquired.
        
        - Performs the normal flat corrections

        - Set the FrameType to the appropriate value

        - For exposure times from 0 to the flat exposure time:  
    
            - Change exposure time to the appropriate value

            - Take a single frame
        """
        super().collect_flat_fields()
        if self.num_gain_fields:
            self.epics_pvs['ScanStatus'].put('Collecting camera gains')
            log.info('collecting camera gains')
            self.epics_pvs['HDF5Location'].put(self.epics_pvs['HDF5GainsLocation'].value)
            self.epics_pvs['FrameType'].put('Gains')
            self.gain_exp_times = np.linspace(0, self.epics_pvs['FlatExposureTime'].value, self.num_gain_fields)
            for gain_time in self.gain_exp_times:
                log.info('image at exposure time = {0:6.4f}'.format(gain_time))
                self.control_pvs['CamAcquireTime'].put(gain_time, wait=True)
                self.collect_static_frame() 


    def begin_scan(self):
        """Performs the operations needed at the very start of a scan.

        This is an override of the begin_scan in TomoScanPSO to account for
        the extra frames due to saving camera gains.
        This does the following:

        - Calls the base class method.
        - Sets the speed of the rotation motor
        - Computes the delta theta, start and stop motor positions for the scan
        - Programs the Aerotech driver to provide pulses at the right positions
        """
        log.info('begin scan')
        # Call the base class method from TomoScan
        TomoScan.begin_scan(self)
 
        time.sleep(0.1)

        # Program the stage driver to provide PSO pulses
        self.compute_positions_PSO()
        self.program_PSO()
        
        # Insert the number of gain images
        self.num_gain_fields = self.epics_pvs['NumGainFields'].value
        if self.num_gain_fields:
            if self.flat_field_mode != 'None':
                self.total_images += self.num_gain_fields
            if self.flat_field_mode == 'Both':
                self.total_images += self.num_gain_fields

        self.epics_pvs['FPNumCapture'].put(self.total_images, wait=True)
        self.epics_pvs['FPCapture'].put('Capture')


    def end_scan(self):
        """Performs the operations needed at the very end of a scan.

        This does the following:

        - Add theta to the raw data file. 

        - Close the shutter

        - Calls the base class method.
        """
        log.info('end scan')

        # Close the shutter
        self.close_shutter()

        # Stop the file plugin, though it should be done already
        log.info('stop the file plugin')
        self.epics_pvs['FPCapture'].put('Done')
        log.info('Check the status of the plugin')
        self.wait_pv(self.epics_pvs['FPCaptureRBV'], 0)

        # Add theta in the hdf file
        self.add_theta()

        # Add the exposure times for the gain images
        self.add_gain_exp_times()

        # Copy file to the analysis computer, if desired
        self.auto_copy_data()

        # Call the base class method
        super().end_scan()


    def add_theta(self):
        """Add theta at the end of a scan.
        Taken from tomoscan_2BM.py function.  This gives the correct theta for scans with missing frames
        """
        log.info('add theta')
        time.sleep(1.0)

        if self.theta is None:
            log.warning('no theta to add')
            return

        full_file_name = self.epics_pvs['FPFullFileName'].get(as_string=True)
        if os.path.exists(full_file_name):
            try:                
                with h5py.File(full_file_name, "a") as f:
                    unique_ids = f['/defaults/NDArrayUniqueId']
                    hdf_location = f['/defaults/HDF5FrameLocation']
                    total_dark_fields = self.num_dark_fields * ((self.dark_field_mode in ('Start', 'Both')) + (self.dark_field_mode in ('End', 'Both')))
                    total_flat_fields = self.num_flat_fields * ((self.flat_field_mode in ('Start', 'Both')) + (self.flat_field_mode in ('End', 'Both')))                        
                    
                    proj_ids = unique_ids[hdf_location[:] == b'/exchange/data']
                    flat_ids = unique_ids[hdf_location[:] == b'/exchange/data_white']
                    dark_ids = unique_ids[hdf_location[:] == b'/exchange/data_dark']

                    # create theta dataset in hdf5 file
                    if len(proj_ids) == 0 or len(self.theta) == 0:
                        log.error('No value theta values')
                        log.error('Abort adding theta')
                        return

                    if len(proj_ids) > 0:
                        theta_ds = f.create_dataset('/exchange/theta', (len(proj_ids),))
                        theta_ds[:] = self.theta[proj_ids - proj_ids[0]]
                        theta_ds.attrs['units'] = 'degrees'

                    # warnings that data is missing
                    if len(proj_ids) != len(self.theta):
                        log.warning(f'There are {len(self.theta) - len(proj_ids)} missing data frames')
                        missed_ids = [ele for ele in range(len(self.theta)) if ele not in proj_ids-proj_ids[0]]
                        missed_theta = self.theta[missed_ids]
                        log.warning(f'Missed theta: {list(missed_theta)}')
                    if len(flat_ids) != total_flat_fields:
                        log.warning(f'There are {total_flat_fields - len(flat_ids)} missing flat field frames')
                    if (len(dark_ids) != total_dark_fields):
                        log.warning(f'There are {total_dark_fields - len(dark_ids)} missing dark field frames')
            except:
                log.error('Add theta: Failed accessing: %s', full_file_name)
                traceback.print_exc(file=sys.stdout)
        else:
            log.error('Failed adding theta. %s file does not exist', full_file_name)


    def add_gain_exp_times(self):
        """Add gain exposure times to the output file.
        """
        log.info('add gain exposure times')
        time.sleep(1.0)

        if self.gain_exp_times is None:
            log.warning('no gain exposure times to add')
            return

        full_file_name = self.epics_pvs['FPFullFileName'].get(as_string=True)
        if os.path.exists(full_file_name):
            try:                
                with h5py.File(full_file_name, "a") as f:
                    gain_exp_times_ds = f.create_dataset(
                                                        '/exchange/gain_exp_times',
                                                        data = self.gain_exp_times,
                                                        )
                    gain_exp_times_ds.attrs['units'] = 's'
            except:
                log.error('Add gain exposure times: Failed accessing: %s', full_file_name)
                traceback.print_exc(file=sys.stdout)
        else:
            log.error('Failed adding gain exposure times. %s file does not exist', full_file_name)


    def wait_pv(self, epics_pv, wait_val, timeout=np.inf, delta_t=0.01):
        """Wait on a pv to be a value until max_timeout (default forever)
           delay for pv to change
        """
        log.info('wait_pv') 
        time.sleep(delta_t)
        start_time = time.time()
        while time.time() - start_time < timeout:
            pv_val = epics_pv.get()
            if isinstance(pv_val, float):
                if abs(pv_val - wait_val) < EPSILON:
                    return True
            if pv_val == wait_val:
                return True
            time.sleep(delta_t)
        else:
            log.error('  *** ERROR: PV TIMEOUT ***')
            log.error('  *** wait_pv(%s, %d, %5.2f reached max timeout. Return False',
                          epics_pv.pvname, wait_val, timeout)
            return False

    def auto_copy_data(self):
        '''Copies data from detector computer to analysis computer.
        '''
        # Copy raw data to data analysis computer    
        if self.epics_pvs['CopyToAnalysisDir'].get():
            log.info('Automatic data trasfer to data analysis computer is enabled.')
            self.epics_pvs['ScanStatus'].put('Auto File Transfer')
            full_file_name = self.epics_pvs['FPFullFileName'].get(as_string=True)
            remote_analysis_dir = self.epics_pvs['RemoteAnalysisDir'].get(as_string=True)
            dm.fdt_scp(full_file_name, remote_analysis_dir, Path(self.epics_pvs['DetectorTopDir'].get()))
            self.epics_pvs['ScanStatus'].put('File Transfer Complete')
            #dm.scp(full_file_name, remote_analysis_dir)
        else:
            log.warning('Automatic data trasfer to data analysis computer is disabled.')
