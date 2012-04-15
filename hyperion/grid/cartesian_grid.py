from __future__ import print_function, division

import hashlib

import h5py
import numpy as np

from ..util.meshgrid import meshgrid_nd
from ..util.functions import FreezableClass, is_numpy_array, monotonically_increasing, link_or_copy
from ..util.logger import logger
from ..grid.grid_helpers import single_grid_dims


class CartesianGrid(FreezableClass):

    def __init__(self, *args):

        self.shape = None

        self.x_wall = None
        self.y_wall = None
        self.z_wall = None

        self.x = None
        self.y = None
        self.z = None

        self.gx = None
        self.gy = None
        self.gz = None

        self.volumes = None
        self.areas = None
        self.widths = None

        self.quantities = {}

        self._freeze()

        if len(args) > 0:
            self.set_walls(*args)

    def set_walls(self, x_wall, y_wall, z_wall):

        if type(x_wall) in [list, tuple]:
            x_wall = np.array(x_wall)
        if type(y_wall) in [list, tuple]:
            y_wall = np.array(y_wall)
        if type(z_wall) in [list, tuple]:
            z_wall = np.array(z_wall)

        if not is_numpy_array(x_wall) or x_wall.ndim != 1:
            raise ValueError("x_wall should be a 1-D sequence")
        if not is_numpy_array(y_wall) or y_wall.ndim != 1:
            raise ValueError("y_wall should be a 1-D sequence")
        if not is_numpy_array(z_wall) or z_wall.ndim != 1:
            raise ValueError("z_wall should be a 1-D sequence")

        if not monotonically_increasing(x_wall):
            raise ValueError("x_wall should be monotonically increasing")
        if not monotonically_increasing(y_wall):
            raise ValueError("y_wall should be monotonically increasing")
        if not monotonically_increasing(z_wall):
            raise ValueError("z_wall should be monotonically increasing")

        # Find grid shape
        self.shape = (len(z_wall) - 1, len(y_wall) - 1, len(x_wall) - 1)

        # Store wall positions
        self.x_wall = x_wall
        self.y_wall = y_wall
        self.z_wall = z_wall

        # Compute cell centers
        self.x = (x_wall[:-1] + x_wall[1:]) / 2.
        self.y = (y_wall[:-1] + y_wall[1:]) / 2.
        self.z = (z_wall[:-1] + z_wall[1:]) / 2.

        # Generate 3D versions of r, t, p
        #(each array is 3D and defined in every cell)
        self.gx, self.gy, self.gz = meshgrid_nd(self.x, self.y, self.z)

        # Generate 3D versions of the inner and outer wall positions respectively
        gx_wall_min, gy_wall_min, gz_wall_min = \
                    meshgrid_nd(x_wall[:-1], y_wall[:-1], z_wall[:-1])
        gx_wall_max, gy_wall_max, gz_wall_max = \
                    meshgrid_nd(x_wall[1:], y_wall[1:], z_wall[1:])

        # USEFUL QUANTITIES

        dx = gx_wall_max - gx_wall_min
        dy = gy_wall_max - gy_wall_min
        dz = gz_wall_max - gz_wall_min

        # CELL VOLUMES

        self.volumes = dx * dy * dz

        # WALL AREAS

        self.areas = np.zeros((6,) + self.shape)

        # X walls:

        self.areas[0, :, :, :] = dy * dz
        self.areas[1, :, :, :] = dy * dz

        # Y walls:

        self.areas[2, :, :, :] = dx * dz
        self.areas[3, :, :, :] = dx * dz

        # Z walls:

        self.areas[4, :, :, :] = dx * dy
        self.areas[5, :, :, :] = dx * dy

        # CELL WIDTHS

        self.widths = np.zeros((3,) + self.shape)

        # X direction:

        self.widths[0, :, :, :] = dx

        # Y direction:

        self.widths[1, :, :, :] = dy

        # Z direction:

        self.widths[2, :, :, :] = dz

    def __getattr__(self, attribute):
        if attribute == 'n_dust':
            n_dust = None
            for quantity in self.quantities:
                n_dust_q, shape_q = single_grid_dims(self.quantities[quantity])
                if n_dust is None:
                    n_dust = n_dust_q
                else:
                    if n_dust != n_dust_q:
                        raise ValueError("Not all dust lists in the grid have the same size")
            return n_dust
        else:
            return FreezableClass.__getattribute__(self, attribute)

    def _check_array_dimensions(self, array=None):
        '''
        Check that a grid's array dimensions agree with this grid's metadata

        Parameters
        ----------
        array: np.ndarray or list of np.ndarray, optional
            The array for which to test the dimensions. If this is not
            specified, this method performs a self-consistency check of array
            dimensions and meta-data.
        '''

        for quantity in self.quantities:

            n_pop, shape = single_grid_dims(self.quantities[quantity])

            if shape != self.shape:
                raise ValueError("Quantity arrays do not have the right "
                                 "dimensions: %s instead of %s"
                                 % (shape, self.shape))

    def read(self, group, quantities='all'):
        '''
        Read in a cartesian grid

        Parameters
        ----------
        group: h5py.Group
            The HDF5 group to read the grid from
        quantities: 'all' or list
            Which physical quantities to read in. Use 'all' to read in all
            quantities or a list of strings to read only specific quantities.
        '''

        # Extract HDF5 groups for geometry and physics

        g_geometry = group['Geometry']
        g_quantities = group['Quantities']

        # Read in geometry

        if g_geometry.attrs['grid_type'].decode('utf-8') != 'car':
            raise ValueError("Grid is not cartesian")

        self.set_walls(g_geometry['walls_1']['x'],
                       g_geometry['walls_2']['y'],
                       g_geometry['walls_3']['z'])

        # Read in physical quantities
        if quantities is not None:
            for quantity in g_quantities:
                if quantities == 'all' or quantity in quantities:
                    array = np.array(g_quantities[quantity])
                    if array.ndim == 4:  # if array is 4D, it is a list of 3D arrays
                        self.quantities[quantity] = [array[i] for i in range(array.shape[0])]
                    else:
                        self.quantities[quantity] = array

        # Check that advertised hash matches real hash
        if g_geometry.attrs['geometry'].decode('utf-8') != self.get_geometry_id():
            raise Exception("Calculated geometry hash does not match hash in file")

        # Self-consistently check geometry and physical quantities
        self._check_array_dimensions()

    def write(self, group, quantities='all', copy=True, absolute_paths=False, compression=True, wall_dtype=float, physics_dtype=float):
        '''
        Write out the cartesian grid

        Parameters
        ----------
        group: h5py.Group
            The HDF5 group to write the grid to
        quantities: 'all' or list
            Which physical quantities to write out. Use 'all' to write out all
            quantities or a list of strings to write only specific quantities.
        compression: bool
            Whether to compress the arrays in the HDF5 file
        wall_dtype: type
            The datatype to use to write the wall positions
        physics_dtype: type
            The datatype to use to write the physical quantities
        '''

        # Create HDF5 groups if needed

        if 'Geometry' not in group:
            g_geometry = group.create_group('Geometry')
        else:
            g_geometry = group['Geometry']

        if 'Quantities' not in group:
            g_quantities = group.create_group('Quantities')
        else:
            g_quantities = group['Quantities']

        # Write out geometry

        g_geometry.attrs['grid_type'] = 'car'.encode('utf-8')
        g_geometry.attrs['geometry'] = self.get_geometry_id().encode('utf-8')

        dset = g_geometry.create_dataset("walls_1", data=np.array(list(zip(self.x_wall)), dtype=[('x', wall_dtype)]), compression=compression)
        dset.attrs['Unit'] = 'cm'.encode('utf-8')

        dset = g_geometry.create_dataset("walls_2", data=np.array(list(zip(self.y_wall)), dtype=[('y', wall_dtype)]), compression=compression)
        dset.attrs['Unit'] = 'cm'.encode('utf-8')

        dset = g_geometry.create_dataset("walls_3", data=np.array(list(zip(self.z_wall)), dtype=[('z', wall_dtype)]), compression=compression)
        dset.attrs['Unit'] = 'cm'.encode('utf-8')

        # Self-consistently check geometry and physical quantities
        self._check_array_dimensions()

        # Write out physical quantities

        for quantity in self.quantities:
            if quantities == 'all' or quantity in quantities:
                if isinstance(self.quantities[quantity], h5py.ExternalLink):
                    link_or_copy(g_quantities, quantity, self.quantities[quantity], copy, absolute_paths=absolute_paths)
                else:
                    dset = g_quantities.create_dataset(quantity, data=self.quantities[quantity],
                                                    compression=compression,
                                                    dtype=physics_dtype)
                    dset.attrs['geometry'] = self.get_geometry_id().encode('utf-8')

    def get_geometry_id(self):
        geo_hash = hashlib.md5()
        geo_hash.update(self.x_wall)
        geo_hash.update(self.y_wall)
        geo_hash.update(self.z_wall)
        return geo_hash.hexdigest()

    def __getitem__(self, item):
        return CartesianGridView(self, item)

    def __setitem__(self, item, value):
        if isinstance(value, CartesianGridView):
            if self.x_wall is None and self.y_wall is None and self.z_wall is None:
                logger.warn("No geometry in target grid - copying from original grid")
                self.set_walls(value.x_wall, value.y_wall, value.z_wall)
            self.quantities[item] = value.quantities[value.viewed_quantity]
        elif isinstance(value, h5py.ExternalLink):
            self.quantities[item] = value
        elif value == []:
            self.quantities[item] = []
        else:
            raise ValueError('value should be an empty list, and ExternalLink, or a CartesianGridView instance')

    def __contains__(self, item):
        return self.quantities.__contains__(item)

    def reset_quantities(self):
        self.quantities = {}


class CartesianGridView(CartesianGrid):

    def __init__(self, grid, quantity):
        self.viewed_quantity = quantity
        CartesianGrid.__init__(self)
        self.set_walls(grid.x_wall, grid.y_wall, grid.z_wall)
        self.quantities = {quantity: grid.quantities[quantity]}

    def append(self, grid):
        '''
        Used to append quantities from another grid

        Parameters
        ----------
        grid: 3D Numpy array or CartesianGridView instance
            The grid to copy the quantity from
        '''
        if isinstance(grid, CartesianGridView):
            self.quantities[self.viewed_quantity].append(grid.quantities[grid.viewed_quantity])
        elif type(grid) is np.ndarray:
            self.quantities[self.viewed_quantity].append(grid)
        else:
            raise ValueError("grid should be a Numpy array or a CartesianGridView object")
