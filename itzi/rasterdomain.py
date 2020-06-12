# coding=utf8
"""
Copyright (C) 2016-2020 Laurent Courty

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
"""

from __future__ import division
from __future__ import absolute_import
from datetime import datetime
import numpy as np

import itzi.flow as flow
import itzi.messenger as msgr


class TimedArray():
    """A container for np.ndarray with time informations.
    Update the array value according to the simulation time.
    array is accessed via get()
    """
    def __init__(self, mkey, igis, f_arr_def):
        assert isinstance(mkey, str), u"not a string!"
        assert hasattr(f_arr_def, '__call__'), u"not a function!"
        self.mkey = mkey
        self.igis = igis  # GIS interface
        # A function to generate a default array
        self.f_arr_def = f_arr_def
        # default values for start and end
        # intended to trigger update when is_valid() is first called
        self.a_start = datetime(1, 1, 2)
        self.a_end = datetime(1, 1, 1)

    def get(self, sim_time):
        """Return a numpy array valid for the given time
        If the array stored is not valid, update the values of the object
        """
        assert isinstance(sim_time, datetime), "not a datetime object!"
        if not self.is_valid(sim_time):
            self.update_values_from_gis(sim_time)
        return self.arr

    def is_valid(self, sim_time):
        """input being a time in datetime
        If the current stored array is within the range of the map,
        return True
        If not return False
        """
        return bool(self.a_start <= sim_time <= self.a_end)

    def update_values_from_gis(self, sim_time):
        """Update array, start_time and end_time from GIS
        if GIS return None, set array to default value
        """
        # Retrieve values
        arr, arr_start, arr_end = self.igis.get_array(self.mkey, sim_time)
        # set to default if no array retrieved
        if not isinstance(arr, np.ndarray):
            arr = self.f_arr_def()
        # check retrieved values
        assert isinstance(arr_start, datetime), "not a datetime object!"
        assert isinstance(arr_end, datetime), "not a datetime object!"
        assert arr_start <= sim_time <= arr_end, "wrong time retrieved!"
        # update object values
        self.a_start = arr_start
        self.a_end = arr_end
        self.arr = arr
        return self


class RasterDomain():
    """Group all rasters for the raster domain.
    Store them as np.ndarray with validity information (TimedArray)
    Include management of the masking and unmasking of arrays.
    """
    def __init__(self, dtype, arr_mask, cell_shape, output_maps):
        # data type
        self.dtype = dtype
        # geographical data
        self.shape = arr_mask.shape
        self.dx, self.dy = cell_shape
        self.cell_surf = self.dx * self.dy
        self.mask = arr_mask

        # conversion factor between mm/h and m/s
        self.mmh_to_ms = 1000. * 3600.

        # number of cells in a row must be a multiple of that number
        byte_num = 256 / 8  # AVX2
        itemsize = np.dtype(self.dtype).itemsize
        self.row_mul = int(byte_num / itemsize)

        # slice for a simple padding (allow stencil calculation on boundary)
        self.simple_pad = (slice(1, -1), slice(1, -1))

        # input and output map names (GIS names)
        self.out_map_names = output_maps
        # all keys that will be used for the arrays
        self.k_input = ['dem', 'friction', 'h', 'y',
                        'effective_porosity', 'capillary_pressure',
                        'hydraulic_conductivity', 'in_inf',
                        'losses', 'rain', 'inflow',
                        'bcval', 'bctype']
        self.k_internal = ['inf', 'hmax', 'ext', 'y', 'hfe', 'hfs',
                           'qe', 'qs', 'qe_new', 'qs_new', 'etp',
                           'ue', 'us', 'v', 'vdir', 'vmax', 'fr',
                           'n_drain', 'capped_losses', 'dire', 'dirs']
        # arrays gathering the cumulated water depth from corresponding array
        self.k_stats = ['st_bound', 'st_inf', 'st_rain', 'st_etp',
                        'st_inflow', 'st_losses', 'st_ndrain', 'st_herr']
        self.stats_corresp = {'inf': 'st_inf', 'rain': 'st_rain',
                              'inflow': 'st_inflow', 'capped_losses': 'st_losses',
                              'n_drain': 'st_ndrain'}
        self.k_all = self.k_input + self.k_internal + self.k_stats
        # last update of statistical map entry
        self.stats_update_time = dict.fromkeys(self.k_stats)

        # boolean dict that indicate if an array has been updated
        self.isnew = dict.fromkeys(self.k_all, True)
        self.isnew['n_drain'] = False

        # Instantiate arrays and padded arrays filled with zeros
        self.arr = dict.fromkeys(self.k_all)
        self.arrp = dict.fromkeys(self.k_all)
        self.create_arrays()

    def water_volume(self):
        """get current water volume in the domain"""
        return self.asum('h') * self.cell_surf

    def inf_vol(self, sim_time):
        self.populate_stat_array('inf', sim_time)
        return self.asum('st_inf') * self.cell_surf

    def rain_vol(self, sim_time):
        self.populate_stat_array('rain', sim_time)
        return self.asum('st_rain') * self.cell_surf

    def inflow_vol(self, sim_time):
        self.populate_stat_array('inflow', sim_time)
        return self.asum('st_inflow') * self.cell_surf

    def losses_vol(self, sim_time):
        self.populate_stat_array('capped_losses', sim_time)
        return self.asum('st_losses') * self.cell_surf

    def ndrain_vol(self, sim_time):
        self.populate_stat_array('n_drain', sim_time)
        return self.asum('st_ndrain') * self.cell_surf

    def boundary_vol(self):
        return self.asum('st_bound') * self.cell_surf

    def err_vol(self):
        return self.asum('st_herr') * self.cell_surf

    def zeros_array(self):
        """return a np array of the domain dimension, filled with zeros.
        dtype is set to object's dtype.
        Intended to be used as default for the input model maps.
        """
        return np.zeros(shape=self.shape, dtype=self.dtype)

    def pad_array(self, arr):
        """Return the original input array
        as a slice of a larger padded array with one cell
        """
        arr_p = np.pad(arr, 1, 'edge')
        arr = arr_p[self.simple_pad]
        return arr, arr_p

    def create_arrays(self):
        """Instantiate masked arrays and padded arrays
        the unpadded arrays are a slice of the padded ones
        """
        for k in self.arr.keys():
            self.arr[k], self.arrp[k] = self.pad_array(self.zeros_array())
        return self

    def update_mask(self, arr):
        '''Create a mask array by marking NULL values from arr as True.
        '''
        pass
        # self.mask[:] = np.isnan(arr)
        return self

    def mask_array(self, arr, default_value):
        '''Replace NULL values in the input array by the default_value
        '''
        mask = np.logical_or(np.isnan(arr), self.mask)
        arr[mask] = default_value
        assert not np.any(np.isnan(arr))
        return self

    def unmask_array(self, arr):
        '''Replace values in the input array by NULL values from mask
        '''
        unmasked_array = np.copy(arr)
        unmasked_array[self.mask] = np.nan
        return unmasked_array

    def populate_stat_array(self, k, sim_time):
        """given an input array key,
        populate the corresponding statistic array.
        If it's the first update, only check in the time.
        Should be called before updating the array
        """
        sk = self.stats_corresp[k]
        update_time = self.stats_update_time[sk]
        # make sure everything is in m/s
        if k in ['rain', 'inf', 'capped_losses']:
            conv_factor = 1 / self.mmh_to_ms
        else:
            conv_factor = 1.

        if self.stats_update_time[sk] is None:
            self.stats_update_time[sk] = sim_time
        else:
            msgr.debug(u"{}: Populating array <{}>".format(sim_time, sk))
            time_diff = (sim_time - update_time).total_seconds()
            flow.populate_stat_array(self.arr[k], self.arr[sk],
                                     conv_factor, time_diff)
            self.stats_update_time[sk] = sim_time
        return None

    def update_ext_array(self):
        """If one of the external input array has been updated,
        combine them into a unique array 'ext' in m/s.
        in_q and n_drain in m/s.
        This applies for inputs that are needed to be taken into account,
         at every timestep, like inflows from user or drainage.
        """
        if any([self.isnew[k] for k in ('inflow', 'n_drain')]):
            flow.set_ext_array(self.arr['inflow'], self.arr['n_drain'],
                               self.arr['ext'])
            self.isnew['ext'] = True
        else:
            self.isnew['ext'] = False
        return self

    def get_output_arrays(self, interval_s, sim_time):
        """Returns a dict of unmasked arrays to be written to the disk
        """
        out_arrays = {}
        if self.out_map_names['h'] is not None:
            out_arrays['h'] = self.get_unmasked('h')
        if self.out_map_names['wse'] is not None:
            out_arrays['wse'] = self.get_unmasked('h') + self.get_array('dem')
        if self.out_map_names['v'] is not None:
            out_arrays['v'] = self.get_unmasked('v')
        if self.out_map_names['vdir'] is not None:
            out_arrays['vdir'] = self.get_unmasked('vdir')
        if self.out_map_names['fr'] is not None:
            out_arrays['fr'] = self.get_unmasked('fr')
        if self.out_map_names['qx'] is not None:
            out_arrays['qx'] = self.get_unmasked('qe_new') * self.dy
        if self.out_map_names['qy'] is not None:
            out_arrays['qy'] = self.get_unmasked('qs_new') * self.dx
        # statistics (average of last interval)
        if interval_s:
            if self.out_map_names['boundaries'] is not None:
                out_arrays['boundaries'] = self.get_unmasked('st_bound') / interval_s
            if self.out_map_names['inflow'] is not None:
                self.populate_stat_array('inflow', sim_time)
                out_arrays['inflow'] = self.get_unmasked('st_inflow') / interval_s
            if self.out_map_names['losses'] is not None:
                self.populate_stat_array('capped_losses', sim_time)
                out_arrays['losses'] = self.get_unmasked('st_losses') / interval_s
            if self.out_map_names['drainage_stats'] is not None:
                self.populate_stat_array('n_drain', sim_time)
                out_arrays['drainage_stats'] = self.get_unmasked('st_ndrain') / interval_s
            if self.out_map_names['infiltration'] is not None:
                self.populate_stat_array('inf', sim_time)
                out_arrays['infiltration'] = (self.get_unmasked('st_inf') /
                                              interval_s) * self.mmh_to_ms
            if self.out_map_names['rainfall'] is not None:
                self.populate_stat_array('rain', sim_time)
                out_arrays['rainfall'] = (self.get_unmasked('st_rain') /
                                          interval_s) * self.mmh_to_ms
        # Created volume (total since last record)
        if self.out_map_names['verror'] is not None:
            self.populate_stat_array('capped_losses', sim_time)  # This is weird
            out_arrays['verror'] = self.get_unmasked('st_herr') * self.cell_surf
        return out_arrays

    def swap_arrays(self, k1, k2):
        """swap values of two arrays
        """
        self.arr[k1], self.arr[k2] = self.arr[k2], self.arr[k1]
        self.arrp[k1], self.arrp[k2] = self.arrp[k2], self.arrp[k1]
        return self

    def update_array(self, k, arr):
        """Update the values of an array with those of a given array.
        """
        if arr.shape != self.shape:
            return ValueError
        if k == 'dem':
            # note: must run update_flow_dir() in SurfaceFlowSimulation
            self.update_mask(arr)
            fill_value = np.finfo(self.dtype).max
        elif k == 'friction':
            fill_value = 1
        else:
            fill_value = 0
        self.mask_array(arr, fill_value)
        self.arr[k][:] = arr
        return self

    def get_array(self, k):
        """return the unpadded, masked array of key 'k'
        """
        return self.arr[k]

    def get_padded(self, k):
        """return the padded, masked array of key 'k'
        """
        return self.arrp[k]

    def get_unmasked(self, k):
        """return unpadded array with NaN
        """
        return self.unmask_array(self.arr[k])

    def amax(self, k):
        """return maximum value of an unpadded array
        """
        return np.amax(self.arr[k])

    def asum(self, k):
        """return the sum of an unpadded array
        values outside the proper domain are the defaults values
        """
        return flow.arr_sum(self.arr[k])

    def reset_stats(self, sim_time):
        """Set stats arrays to zeros and the update time to current time
        """
        for k in self.k_stats:
            self.arr[k][:] = 0.
            self.stats_update_time[k] = sim_time
        return self
