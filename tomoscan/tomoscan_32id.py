"""Software for tomography scanning with EPICS at APS beamline 32-ID

   Classes
   -------
   TomoScan32ID
     Derived class for tomography scanning with EPICS at APS beamline 32-ID
"""
import time
import os
import h5py 
import sys
import traceback
import numpy as np
from epics import PV
import pvaccess as pva
import threading
from pathlib import Path

from tomoscan import data_management as dm
from tomoscan.tomoscan_pso import TomoScanPSO
from tomoscan import log

EPSILON = .001
class SampleXError(Exception):
    '''Exception raised when SampleX is not equal to SampleInX
    '''
    
class TomoScan32ID(TomoScanPSO):
    """Derived class used for tomography scanning with EPICS at APS beamline 32-ID

    Parameters
    ----------
    pv_files : list of str
        List of files containing EPICS pvNames to be used.
    macros : dict
        Dictionary of macro definitions to be substituted when
        reading the pv_files
    """

    def __init__(self, pv_files, macros):
        super().__init__(pv_files, macros)
        # Set the detector running in FreeRun mode
        # self.set_trigger_mode('FreeRun', 1)
        # self.epics_pvs['CamAcquire'].put('Acquire') ###
        # self.wait_pv(self.epics_pvs['CamAcquire'], 1) ###
        
        # set TomoScan xml files
        self.epics_pvs['CamNDAttributesFile'].put('TomoScanDetectorAttributes.xml')
        self.epics_pvs['FPXMLFileName'].put('TomoScanLayout.xml')
        macro = 'DET=' + self.pv_prefixes['Camera'] + ',' + 'TS=' + self.epics_pvs['Testing'].__dict__['pvname'].replace('Testing', '', 1)
        self.control_pvs['CamNDAttributesMacros'].put(macro)

        # Enable auto-increment on file writer
        self.epics_pvs['FPAutoIncrement'].put('Yes')

        # Set standard file template on file writer
        self.epics_pvs['FPFileTemplate'].put("%s%s_%3.3d.h5", wait=True)
        # Disable over writing warning
        self.epics_pvs['OverwriteWarning'].put('Yes')

        # TXMOptics
        prefix = self.pv_prefixes['TxmOptics']
        self.epics_pvs['TXMEnergySet'] = PV(prefix+'EnergySet')
        self.epics_pvs['TXMEnergy'] = PV(prefix+'Energy')
        self.epics_pvs['TXMMoveAllOut'] = PV(prefix+'MoveAllOut')
        self.epics_pvs['TXMMoveAllIn'] = PV(prefix+'MoveAllIn')        
        # pva type channel that contains projection and metadata
        prefix = self.pv_prefixes['Image']
        self.epics_pvs['PvaPImage']          = pva.Channel(prefix + 'Image')
        self.epics_pvs['PvaPDataType_RBV']   = pva.Channel(prefix + 'DataType_RBV')
        
        # energy scan
        self.epics_pvs['EnergySet'].put(0)
        self.epics_pvs['EnergySet'].add_callback(self.pv_callback_32id)

        self.epics_pvs['SampleXSet'] = PV(self.control_pvs['SampleX'].pvname + '.SET')
        self.epics_pvs['SampleYSet'] = PV(self.control_pvs['SampleY'].pvname + '.SET')

        log.setup_custom_logger("./tomoscan.log")
    
    def pv_callback_32id(self, pvname=None, value=None, char_value=None, **kw):
        """Callback function that is called by pyEpics when certain EPICS PVs are changed
        
        """
        log.debug('pv_callback_32id pvName=%s, value=%s, char_value=%s', pvname, value, char_value)       
        if (pvname.find('EnergySet') != -1) and value==1:
            thread = threading.Thread(target=self.energy_change, args=())
            thread.start()
            
    def open_frontend_shutter(self):
        """Opens the shutters to collect flat fields or projections.

        This does the following:

        - Checks if we are in testing mode. If we are, do nothing else opens the 2-BM-A front-end shutter.

        """
        if self.epics_pvs['Testing'].get():
            log.warning('In testing mode, so not opening shutters.')
        else:
            # Open front-end shutter
            if not self.epics_pvs['OpenShutter'] is None:
                pv = self.epics_pvs['OpenShutter']
                value = self.epics_pvs['OpenShutterValue'].get(as_string=True)
                status = self.epics_pvs['ShutterStatus'].get(as_string=True)
                log.info('shutter status: %s', status)
                log.info('open shutter: %s, value: %s', pv, value)
                self.epics_pvs['OpenShutter'].put(value, wait=True)
                self.wait_frontend_shutter_open()
                # self.wait_pv(self.epics_pvs['ShutterStatus'], 1)
                status = self.epics_pvs['ShutterStatus'].get(as_string=True)
                log.info('shutter status: %s', status)

    def open_shutter(self):
        """Opens the shutters to collect flat fields or projections.

        This does the following:

        - Opens the 32-ID-C fast shutter.
        """
        # Open 32-ID-C fast shutter
        if not self.epics_pvs['OpenFastShutter'] is None:
           pv = self.epics_pvs['OpenFastShutter']
           value = self.epics_pvs['OpenFastShutterValue'].get(as_string=True)
           log.info('open fast shutter: %s, value: %s', pv, value)
           self.epics_pvs['OpenFastShutter'].put(value, wait=True)

    def close_frontend_shutter(self):
        """Closes the shutters to collect dark fields.
        This does the following:

        - Closes the 32-ID-C front-end shutter.

        """
        if self.epics_pvs['Testing'].get():
            log.warning('In testing mode, so not opening shutters.')
        else:
            # Close  front-end shutter
            if not self.epics_pvs['CloseShutter'] is None:
                pv = self.epics_pvs['CloseShutter']
                value = self.epics_pvs['CloseShutterValue'].get(as_string=True)
                status = self.epics_pvs['ShutterStatus'].get(as_string=True)
                log.info('shutter status: %s', status)
                log.info('close shutter: %s, value: %s', pv, value)
                self.epics_pvs['CloseShutter'].put(value, wait=True)
                self.wait_pv(self.epics_pvs['ShutterStatus'], 0)
                status = self.epics_pvs['ShutterStatus'].get(as_string=True)
                log.info('shutter status: %s', status)

    def close_shutter(self):
        """Closes the shutters to collect dark fields.
        This does the following:

        - Closes the 32-ID-C fast shutter.
        """

        
	    # Close 32-ID-C fast shutter
        if not self.epics_pvs['CloseFastShutter'] is None:
           pv = self.epics_pvs['CloseFastShutter']
           value = self.epics_pvs['CloseFastShutterValue'].get(as_string=True)
           log.info('close fast shutter: %s, value: %s', pv, value)
           self.epics_pvs['CloseFastShutter'].put(value, wait=True)
        
    def fly_scan(self):
        """Control of Sample X position
        """
        
         
        super().fly_scan()
    
    def set_trigger_mode(self, trigger_mode, num_images):
        """Sets the trigger mode SIS3820 and the camera.

        Parameters
        ----------
        trigger_mode : str
            Choices are: "FreeRun", "Internal", or "PSOExternal"

        num_images : int
            Number of images to collect.  Ignored if trigger_mode="FreeRun".
            This is used to set the ``NumImages`` PV of the camera.
        """
        camera_model = self.epics_pvs['CamModel'].get(as_string=True)
        if(camera_model=='Grasshopper3 GS3-U3-51S5M'):        
            self.set_trigger_mode_grasshopper(trigger_mode, num_images)
        elif(camera_model=='Blackfly S BFS-PGE-161S7M'):        
            self.set_trigger_mode_grasshopper(trigger_mode, num_images)
        else:
            log.error('Camera is not supported')
            exit(1)

    def set_trigger_mode_grasshopper(self, trigger_mode, num_images):
        self.epics_pvs['CamAcquire'].put('Done') ###
        self.wait_pv(self.epics_pvs['CamAcquire'], 0) ###
        
        log.info('set trigger mode: %s', trigger_mode)
        if trigger_mode == 'FreeRun':
            self.epics_pvs['CamImageMode'].put('Continuous', wait=True)
            self.epics_pvs['CamTriggerMode'].put('Off', wait=True)
            self.wait_pv(self.epics_pvs['CamTriggerMode'], 0)
            # self.epics_pvs['CamAcquire'].put('Acquire')
        elif trigger_mode == 'Internal':
            self.epics_pvs['CamTriggerMode'].put('Off', wait=True)
            self.wait_pv(self.epics_pvs['CamTriggerMode'], 0)
            self.epics_pvs['CamImageMode'].put('Multiple')            
            self.epics_pvs['CamNumImages'].put(num_images, wait=True)
        else: # set camera to external triggering
            # These are just in case the scan aborted with the camera in another state 
            self.epics_pvs['CamTriggerMode'].put('On', wait=True)     # VN: For PG we need to switch to On to be able to switch to readout overlap mode                                                               
            self.epics_pvs['CamTriggerSource'].put('Line2', wait=True)
            self.epics_pvs['CamTriggerOverlap'].put('ReadOut', wait=True)
            self.epics_pvs['CamExposureMode'].put('Timed', wait=True)
            self.epics_pvs['CamImageMode'].put('Multiple')            
            self.epics_pvs['CamArrayCallbacks'].put('Enable')
            self.epics_pvs['CamFrameRateEnable'].put(0)

            self.epics_pvs['CamNumImages'].put(self.num_angles, wait=True)
            self.epics_pvs['CamTriggerMode'].put('On', wait=True)
            self.wait_pv(self.epics_pvs['CamTriggerMode'], 1)

    def begin_scan(self):
        """Performs the operations needed at the very start of a scan.

        This does the following:

        - Set data directory.

        - Set the TomoScan xml files

        - Calls the base class method.
        
        - Opens the front-end shutter.

        - Sets the PSO controller.

        - Creates theta array using list from PSO. 

        - Turns on data capture.
        """
        log.info('begin scan')
        self.epics_pvs['SampleXSet'].put(1, wait=True)
        self.epics_pvs['SampleYSet'].put(1, wait=True)
        self.epics_pvs['SampleX'].put(0, wait=True)
        self.epics_pvs['SampleY'].put(0, wait=True)
        self.epics_pvs['SampleXSet'].put(0, wait=True)
        self.epics_pvs['SampleYSet'].put(0, wait=True)
        
        # if(abs(self.epics_pvs['SampleInX'].value-self.epics_pvs['SampleX'].value)>1e-4) or abs(self.epics_pvs['SampleInY'].value-self.epics_pvs['SampleY'].value)>1e-4:
        #     log.error('SampleInX/SampleInZ is not the same as current SampleTopX/SampleTopZ')            
        #     self.epics_pvs['ScanStatus'].put('Sample position error')
        #     self.epics_pvs['StartScan'].put(0)        
        #     return
        # Set data directory
        file_path = self.epics_pvs['DetectorTopDir'].get(as_string=True) + self.epics_pvs['ExperimentYearMonth'].get(as_string=True) + os.path.sep + self.epics_pvs['UserLastName'].get(as_string=True) + os.path.sep
        self.epics_pvs['FilePath'].put(file_path, wait=True)

        # Call the base class method
        super().begin_scan()
         
        # Opens the front-end shutter
        self.open_frontend_shutter()
        
    
    def energy_change(self):
        """Change energy trhough TXMOptics"""
        energy = self.epics_pvs['Energy'].get() 
        self.epics_pvs['TXMEnergy'].put(energy,wait=True)
        self.epics_pvs['TXMEnergySet'].put(1,wait=True)        
        time.sleep(1)
        self.epics_pvs['EnergySet'].put(0)
        
    def end_scan(self):
        """Performs the operations needed at the very end of a scan.

        This does the following:

        - Calls ``save_configuration()``.

        - Put the camera back in "FreeRun" mode and acquiring so the user sees live images.

        - Sets the speed of the rotation stage back to the maximum value.

        - Calls ``move_sample_in()``.

        - Calls the base class method.

        - Closes shutter.  

        - Add theta to the raw data file. 

        - Copy raw data to data analysis computer.      
        """

        if self.return_rotation == 'Yes':
            # Reset rotation position by mod 360 , the actual return 
            # to start position is handled by super().end_scan()
            log.info('wait until the stage is stopped')
            time.sleep(self.epics_pvs['RotationAccelTime'].get()*1.2)                        
            ang = self.epics_pvs['RotationRBV'].get()
            current_angle = np.sign(ang)*(np.abs(ang)%360)
            self.epics_pvs['RotationSet'].put('Set', wait=True)
            self.epics_pvs['Rotation'].put(current_angle, wait=True)
            self.epics_pvs['RotationSet'].put('Use', wait=True)
        # Call the base class method
        super().end_scan()
        
        # Stop the file plugin
        self.epics_pvs['FPCapture'].put('Done')
        self.wait_pv(self.epics_pvs['FPCaptureRBV'], 0)
        # Add theta in the hdf file
        self.add_theta()
                
                
        if self.epics_pvs['CollectMicroCTdata'].get(as_string=True)=='Yes':
            try:
                log.warning('take microCT projection')
                self.epics_pvs['TXMMoveAllOut'].put(1,wait=True)
                self.epics_pvs['CamAcquire'].put(1)
                log.info(f"write to {self.epics_pvs['FPFullFileName'].get(as_string=True)}")
                time.sleep(0.5)
                with h5py.File(self.epics_pvs['FPFullFileName'].get(as_string=True),'r+') as fid:
                    pva_image_data = self.epics_pvs['PvaPImage'].get('') 
                    width = pva_image_data['dimension'][0]['size']
                    height = pva_image_data['dimension'][1]['size']
                    datatype_list = self.epics_pvs['PvaPDataType_RBV'].get()['value']   
                    type_dict = {
                        'uint8': 'ubyteValue',
                        'float32': 'floatValue',
                        'uint16' : 'ushortValue'
                    }
                    datatype = type_dict[datatype_list['choices'][datatype_list['index']].lower()]                        
                    data = pva_image_data['value'][0][datatype]            
                    fid.create_dataset("exchange/data2", data = data.reshape([height,width]))                        
                self.epics_pvs['CamAcquire'].put(0)
            except:
                log.warning('microCT projection was not taken')
            #self.epics_pvs['TXMMoveAllIn'].put(1,wait=True)
            
        # Close shutter
        self.close_shutter()    
        
        # Copy raw data to data analysis computer    
        log.info('Automatic data trasfer to data analysis computer is enabled.')
        full_file_name = self.epics_pvs['FPFullFileName'].get(as_string=True)
        remote_analysis_dir = self.epics_pvs['RemoteAnalysisDir'].get(as_string=True)
        copy_to_analysis_dir = self.epics_pvs['CopyToAnalysisDir'].get()
        if copy_to_analysis_dir == 1:
            log.info('Using FDT')
            dm.fdt_scp(full_file_name, remote_analysis_dir, Path(self.epics_pvs['DetectorTopDir'].get()))
            self.epics_pvs['ScanStatus'].put('fdt file transfer complete')
        elif copy_to_analysis_dir == 2:
            log.info('Using scp')
            dm.scp(full_file_name, remote_analysis_dir)
            self.epics_pvs['ScanStatus'].put('scp file transfer complete')
        else:
            log.warning('Automatic data trasfer to data analysis computer is disabled.')

    def add_theta(self):
        """Add theta at the end of a scan.
        """
        log.info('add theta')

        full_file_name = self.epics_pvs['FPFullFileName'].get(as_string=True)
        if os.path.exists(full_file_name):
            try:                
                with h5py.File(full_file_name, "a") as f:
                    if self.theta is not None:                        
                        unique_ids = f['/defaults/NDArrayUniqueId']
                        hdf_location = f['/defaults/HDF5FrameLocation']
                        total_dark_fields = self.num_dark_fields * ((self.dark_field_mode in ('Start', 'Both')) + (self.dark_field_mode in ('End', 'Both')))
                        total_flat_fields = self.num_flat_fields * ((self.flat_field_mode in ('Start', 'Both')) + (self.flat_field_mode in ('End', 'Both')))                        
                        
                        proj_ids = unique_ids[hdf_location[:] == b'/exchange/data']
                        flat_ids = unique_ids[hdf_location[:] == b'/exchange/data_white']
                        dark_ids = unique_ids[hdf_location[:] == b'/exchange/data_dark']

                        # create theta dataset in hdf5 file
                        if len(proj_ids) > 0:
                            theta_ds = f.create_dataset('/exchange/theta', (len(proj_ids),))
                            theta_ds[:] = self.theta[proj_ids - proj_ids[0]]

                        # warnings that data is missing
                        if len(proj_ids) != len(self.theta):
                            log.warning(f'There are {len(self.theta) - len(proj_ids)} missing data frames')
                            missed_ids = [ele for ele in range(len(self.theta)) if ele not in proj_ids-proj_ids[0]]
                            missed_theta = self.theta[missed_ids]
                            # log.warning(f'Missed ids: {list(missed_ids)}')
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

    def wait_pv(self, epics_pv, wait_val, timeout=-1):
        """Wait on a pv to be a value until max_timeout (default forever)
           delay for pv to change
        """

        time.sleep(.01)
        start_time = time.time()
        while True:
            pv_val = epics_pv.get()
            if isinstance(pv_val, float):
                if abs(pv_val - wait_val) < EPSILON:
                    return True
            if pv_val != wait_val:
                if timeout > -1:
                    current_time = time.time()
                    diff_time = current_time - start_time
                    if diff_time >= timeout:
                        log.error('  *** ERROR: DROPPED IMAGES ***')
                        log.error('  *** wait_pv(%s, %d, %5.2f reached max timeout. Return False',
                                      epics_pv.pvname, wait_val, timeout)
                        return False
                time.sleep(.01)
            else:
                return True

    def wait_frontend_shutter_open(self, timeout=-1):
        """Waits for the front end shutter to open, or for ``abort_scan()`` to be called.

        While waiting this method periodically tries to open the shutter..

        Parameters
        ----------
        timeout : float
            The maximum number of seconds to wait before raising a ShutterTimeoutError exception.

        Raises
        ------
        ScanAbortError
            If ``abort_scan()`` is called
        ShutterTimeoutError
            If the open shutter has not completed within timeout value.
        """

        start_time = time.time()
        pv = self.epics_pvs['OpenShutter']
        value = self.epics_pvs['OpenShutterValue'].get(as_string = True)
        log.info('open shutter: %s, value: %s', pv, value)
        elapsed_time = 0
        while True:
            if self.epics_pvs['ShutterStatus'].get() == int(value):
                log.warning("Shutter is open in %f s", elapsed_time)
                return
            if not self.scan_is_running:
                exit()
            value = self.epics_pvs['OpenShutterValue'].get()
            time.sleep(1.0)
            current_time = time.time()
            elapsed_time = current_time - start_time
            log.warning("Waiting on shutter to open: %f s", elapsed_time)
            self.epics_pvs['OpenShutter'].put(value, wait=True)
            if timeout > 0:
                if elapsed_time >= timeout:
                   exit()   
