import datajoint as dj
import scanreader
import numpy as np
import pathlib
from datetime import datetime

from .parameter import CaimanParamSet, Suite2pParamSet
from .imaging import schema, Scan, ScanInfo, Channel, PhysicalFile
from djutils.templates import required, optional

from img_loaders import suite2p

# ===================================== Lookup =====================================


@schema
class ProcessingMethod(dj.Lookup):
    definition = """
    processing_method: char(8)
    """

    contents = zip(['suite2p', 'caiman'])


@schema
class ProcessingParamSet(dj.Lookup):
    definition = """
    -> ProcessingMethod
    paramset_idx:  smallint
    ---
    paramset_desc: varchar(128)
    """

    class Caiman(dj.Part):
        definition = """
        -> master
        ---
        -> CaimanParamSet
        """

    class Suite2p(dj.Part):
        definition = """
        -> master
        ---
        -> Suite2pParamSet
        """


@schema
class CellCompartment(dj.Lookup):
    definition = """  # cell compartments that can be imaged
    cell_compartment         : char(16)
    """
    contents = [['axon'], ['soma'], ['bouton']]


@schema
class RoiType(dj.Lookup):
    definition = """ # possible classifications for a segmented mask
    roi_type        : varchar(16)
    """
    contents = [
        ['soma'],
        ['axon'],
        ['dendrite'],
        ['neuropil'],
        ['artifact'],
        ['unknown']
    ]


# ===================================== Trigger a processing routine =====================================

@schema
class ProcessingTask(dj.Manual):
    definition = """
    -> Scan
    processing_instance: uuid
    ---
    -> ProcessingParamSet
    """


@schema
class Processing(dj.Computed):
    definition = """
    -> ProcessingTask
    ---
    start_time=null     : datetime  # execution time of this processing task (not available if analysis triggering is not required)
    completion_time     : datetime  # time of generation of this set of processed, segmented results
    curation_time=null  : datetime  # time of lastest curation (modification to the file) on this result set
    """

    class ProcessingOutputFile(dj.Part):
        definition = """
        -> master
        -> PhysicalFile
        """

    @staticmethod
    @optional
    def _get_caiman_dir(processing_task_key: dict) -> str:
        """
        Retrieve the CaImAn output directory for a given ProcessingTask
        :param processing_task_key: a dictionary of one ProcessingTask
        :return: a string for full path to the resulting CaImAn output directory
        """
        return None

    @staticmethod
    @optional
    def _get_suite2p_dir(processing_task_key: dict) -> str:
        """
        Retrieve the Suite2p output directory for a given ProcessingTask
        :param processing_task_key: a dictionary of one ProcessingTask
        :return: a string for full path to the resulting CaImAn output directory
        """
        return None

    # Run processing only on Scan with ScanInfo inserted
    @property
    def key_source(self):
        return ProcessingTask & ScanInfo

    def make(self, key):
        # ----
        # trigger suite2p or caiman here
        # ----

        method = (ProcessingMethod * ProcessingTask & key).fetch1('processing_method')

        if method == 'suite2p':
            data_dir = pathlib.Path(Processing._get_suite2p_dir(key))
            if data_dir.exists():
                s2p_loader = suite2p.Suite2p(data_dir)
                key = {**key, 'completion_time': s2p_loader.creation_time, 'curation_time': s2p_loader.curation_time}
                self.insert1(key)
                # Insert file(s)
                root = pathlib.Path(PhysicalFile._get_root_data_dir())
                files = data_dir.glob('*')  # works for Suite2p, maybe something more file-specific for CaImAn
                files = [pathlib.Path(f).relative_to(root).as_posix() for f in files if f.is_file()]

                PhysicalFile.insert(zip(files), skip_duplicates=True)
                self.ProcessingOutputFile.insert([{**key, 'file_path': f} for f in files], ignore_extra_fields=True)
            else:
                start_time = datetime.now()
                # trigger Suite2p here
                # wait for completion, then insert with "start_time", "completion_time", no "curation_time"
                return
        else:
            raise NotImplementedError('Unknown method: {}'.format(method))


# ===================================== Motion Correction =====================================

@schema
class MotionCorrection(dj.Imported):
    definition = """ 
    -> ProcessingTask
    ---
    -> Channel.proj(mc_channel='channel')              # channel used for motion correction in this processing task
    """

    class RigidMotionCorrection(dj.Part):
        definition = """ 
        -> master
        -> ScanInfo.Field
        ---
        outlier_frames                  : longblob      # mask with true for frames with outlier shifts (already corrected)
        y_shifts                        : longblob      # (pixels) y motion correction shifts
        x_shifts                        : longblob      # (pixels) x motion correction shifts
        y_std                           : float         # (pixels) standard deviation of y shifts
        x_std                           : float         # (pixels) standard deviation of x shifts
        z_drift=null                    : longblob      # z-drift over frame of this Field (plane)
        """

    class NonRigidMotionCorrection(dj.Part):
        """ Piece-wise rigid motion correction - tile the FOV into multiple 2D blocks/patches"""
        definition = """ 
        -> master
        -> ScanInfo.Field
        ---
        outlier_frames                  : longblob      # mask with true for frames with outlier shifts (already corrected)
        block_height                    : int           # (px)
        block_width                     : int           # (px)
        block_count_y                   : int           # number of blocks tiled in the y direction
        block_count_x                   : int           # number of blocks tiled in the x direction
        z_drift=null                    : longblob      # z-drift over frame of this Field (plane)
        """

    class Block(dj.Part):
        definition = """  # FOV-tiled blocks used for non-rigid motion correction
        -> master.NonRigidMotionCorrection
        block_id                        : int
        ---
        block_y                         : longblob      # (y_start, y_end) in pixel of this block
        block_x                         : longblob      # (x_start, x_end) in pixel of this block
        y_shifts                        : longblob      # (pixels) y motion correction shifts for every frame
        x_shifts                        : longblob      # (pixels) x motion correction shifts for every frame
        y_std                           : float         # (pixels) standard deviation of y shifts
        x_std                           : float         # (pixels) standard deviation of x shifts
        """

    def make(self, key):

        method = (ProcessingMethod * ProcessingTask & key).fetch1('processing_method')

        if method == 'suite2p':
            data_dir = pathlib.Path(Processing._get_suite2p_dir(key))
            s2p_loader = suite2p.Suite2p(data_dir)

            # ---- build motion correction key
            align_chn = s2p_loader.planes[0].alignment_channel
            self.insert1({**key, 'mc_channel': align_chn})

            # ---- iterate through all s2p plane outputs ----
            for plane, s2p in s2p_loader.planes.items():
                mc_key = (ScanInfo.Field * ProcessingTask & key & {'plane': plane}).fetch1('KEY')

                # -- rigid motion correction --
                rigid_mc = {'y_shifts': s2p.ops['yoff'],
                            'x_shifts': s2p.ops['xoff'],
                            'y_std': np.nanstd(s2p.ops['yoff']),
                            'x_std': np.nanstd(s2p.ops['xoff']),
                            'outlier_frames': s2p.ops['badframes']}

                self.RigidMotionCorrection.insert1({**mc_key, **rigid_mc})

                # -- non-rigid motion correction --
                if s2p.ops['nonrigid']:
                    nonrigid_mc = {'block_height': s2p.ops['block_size'][0],
                                   'block_width': s2p.ops['block_size'][1],
                                   'block_count_y': s2p.ops['nblocks'][0],
                                   'block_count_x': s2p.ops['nblocks'][1],
                                   'outlier_frames': s2p.ops['badframes']}
                    nr_blocks = [{**mc_key, 'block_id': b_id,
                                  'block_y': b_y, 'block_x': b_x,
                                  'y_shifts': bshift_y, 'x_shifts': bshift_x,
                                  'y_std': np.nanstd(bshift_y), 'x_std': np.nanstd(bshift_x)}
                                 for b_id, (b_y, b_x, bshift_y, bshift_x)
                                 in enumerate(zip(s2p.ops['xblock'], s2p.ops['yblock'],
                                                  s2p.ops['yoff1'].T, s2p.ops['xoff1'].T))]
                    self.NonRigidMotionCorrection.insert1({**mc_key, **nonrigid_mc})
                    self.Block.insert(nr_blocks)
        else:
            raise NotImplementedError('Unknown/unimplemented method: {}'.format(method))


@schema
class MotionCorrectedImages(dj.Imported):
    definition = """ # summary images for each field and channel after corrections
    -> MotionCorrection
    -> ScanInfo.Field
    ---
    ref_image                    : longblob      # image used as alignment template
    average_image                : longblob      # mean of registered frames
    correlation_image=null       : longblob      # correlation map (computed during cell detection)
    max_proj_image=null          : longblob      # max of registered frames
    """

    key_source = MotionCorrection()

    def make(self, key):
        method = (ProcessingMethod * ProcessingTask & key).fetch1('processing_method')

        if method == 'suite2p':
            data_dir = pathlib.Path(Processing._get_suite2p_dir(key))
            s2p_loader = suite2p.Suite2p(data_dir)

            # ---- iterate through all s2p plane outputs ----
            for plane, s2p in s2p_loader.items():
                mc_key = (ScanInfo.Field * ProcessingTask & key & {'plane': plane}).fetch1('KEY')
                img_dict = {'ref_image': s2p.ref_image,
                            'average_image': s2p.mean_image,
                            'correlation_image': s2p.correlation_map,
                            'max_proj_image': s2p.max_proj_image}
                self.insert1({**mc_key, **img_dict})
        else:
            raise NotImplementedError('Unknown/unimplemented method: {}'.format(method))


# ===================================== Segmentation =====================================


@schema
class Segmentation(dj.Computed):
    definition = """ # Different mask segmentations.
    -> MotionCorrection        
    ---
    -> Channel.proj(seg_channel='channel')  # channel used for the segmentation
    """

    class Mask(dj.Part):
        definition = """ # A mask produced by segmentation.
        -> master
        mask                : smallint
        ---
        -> ScanInfo.Field                   # the field this ROI comes from
        npix                : int           # number of pixels in ROIs
        center_x            : int           # center x coordinate in pixels
        center_y            : int           # center y coordinate in pixels
        xpix                : longblob      # x coordinates in pixels
        ypix                : longblob      # y coordinates in pixels        
        weights             : longblob      # weights of the mask at the indices above in column major (Fortran) order
        """

    class Cell(dj.Part):
        definition = """
        -> master.Mask
        ---
        is_cell: bool
        cell_prob: float
        """

    def make(self, key):
        method = (ProcessingMethod * ProcessingTask & key).fetch1('processing_method')

        if method == 'suite2p':
            data_dir = pathlib.Path(Processing._get_suite2p_dir(key))
            s2p_loader = suite2p.Suite2p(data_dir)

            # ---- build segmentation key
            seg_channel = s2p_loader.planes[0].segmentation_channel
            self.insert1({**key, 'seg_channel': seg_channel})

            # ---- iterate through all s2p plane outputs ----
            masks = []
            for plane, s2p in s2p_loader.planes.items():
                seg_key = (ScanInfo.Field * ProcessingTask & key & {'plane': plane}).fetch1('KEY')
                mask_count = len(masks)  # increment mask id from all "plane"
                for mask_idx, (is_cell, cell_prob, mask_stat) in enumerate(zip(s2p.iscell, s2p.cell_prob, s2p.stat)):
                    mask = {**seg_key, 'mask': mask_idx + mask_count,
                            'is_cell': bool(is_cell), 'cell_prob': cell_prob, 'npix': mask_stat['npix'],
                            'center_x':  mask_stat['med'][1], 'center_y':  mask_stat['med'][0],
                            'xpix':  mask_stat['xpix'], 'ypix':  mask_stat['ypix'], 'weights':  mask_stat['lam']}
                    masks.append(mask)

            self.Mask.insert(masks, ignore_extra_fields=True)
            self.Cell.insert(masks, ignore_extra_fields=True)
        else:
            raise NotImplementedError('Unknown/unimplemented method: {}'.format(method))


@schema
class MaskClassificationMethod(dj.Lookup):
    definition = """
    mask_classification_method: varchar(16)
    """


@schema
class MaskClassification(dj.Computed):
    definition = """
    -> Segmentation
    -> MaskClassificationMethod
    """

    class MaskType(dj.Part):
        definition = """
        -> master
        -> Segmentation.Cell
        ---
        -> RoiType        
        """


# ===================================== Activity Trace =====================================


@schema
class Fluorescence(dj.Computed):
    definition = """  # fluorescence traces before spike extraction or filtering
    -> Segmentation
    """

    class Trace(dj.Part):
        definition = """
        -> master
        -> Segmentation.Cell
        -> Channel.proj(fluo_channel='channel')  # the channel that this trace comes from 
        ---
        fluo                : longblob  # Raw fluorescence trace
        neuropil_fluo       : longblob  # Neuropil fluorescence trace
        """

    def make(self, key):
        method = (ProcessingMethod * ProcessingTask & key).fetch1('processing_method')

        if method == 'suite2p':
            data_dir = pathlib.Path(Processing._get_suite2p_dir(key))
            s2p_loader = suite2p.Suite2p(data_dir)

            self.insert1(key)

            # ---- iterate through all s2p plane outputs ----
            fluo_traces = []
            for s2p in s2p_loader.planes.values():
                mask_count = len(fluo_traces)  # increment mask id from all "plane"
                for mask_idx, (f, fneu) in enumerate(zip(s2p.F, s2p.Fneu)):
                    fluo_traces.append({**key, 'mask': mask_idx + mask_count,
                                        'fluo_channel': 0, 'fluo': f, 'neuropil_fluo': fneu})
                if len(s2p.F_chan2):
                    for mask_idx, (f2, fneu2) in enumerate(zip(s2p.F_chan2, s2p.Fneu_chan2)):
                        fluo_traces.append({**key, 'mask': mask_idx + mask_count,
                                            'fluo_channel': 1, 'fluo': f2, 'neuropil_fluo': fneu2})

            self.Trace.insert(fluo_traces)

        else:
            raise NotImplementedError('Unknown/unimplemented method: {}'.format(method))


@schema
class DeconvolutionMethod(dj.Lookup):
    definition = """
    deconvolution_method: varchar(16)
    """


@schema
class DeconvolvedCalciumActivity(dj.Computed):
    definition = """  # deconvolved calcium acitivity from fluorescence trace
    -> Fluorescence
    -> DeconvolutionMethod
    """

    class DFF(dj.Part):
        definition = """  # delta F/F
        -> master
        -> Fluorescence.Trace
        ---
        df_f                : longblob  # delta F/F - deconvolved calcium acitivity 
        """
