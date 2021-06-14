r"""The ``images`` module provides the core functionality of MPoL via :class:`mpol.images.ImageCube`."""

import numpy as np
import torch
import torch.fft  # to avoid conflicts with old torch.fft *function*
import torchkbnufft
from torch import nn

from . import utils
from .coordinates import GridCoords
from .gridding import _setup_coords


class FourierCube(nn.Module):
    r"""
    A layer holding the cube corresponding to the FFT of ImageCube.

    Args:
        cell_size (float): the width of an image-plane pixel [arcseconds]
        npix (int): the number of pixels per image side
        coords (GridCoords): an object already instantiated from the GridCoords class. If providing this, cannot provide ``cell_size`` or ``npix``.
    """

    def __init__(self, cell_size=None, npix=None, coords=None):

        super().__init__()

        # we don't want to bother with the nchan argument here, so
        # we don't use the convenience method _setup_coords
        # and just do it manually
        if coords:
            assert (
                npix is None and cell_size is None
            ), "npix and cell_size must be empty if precomputed GridCoords are supplied."
            self.coords = coords

        elif npix or cell_size:
            assert (
                coords is None
            ), "GridCoords must be empty if npix and cell_size are supplied."

            self.coords = GridCoords(cell_size=cell_size, npix=npix)

    def forward(self, cube):
        """
        Perform the FFT of the image cube for each channel.

        Args:
            cube (torch.double tensor, of shape ``(nchan, npix, npix)``): a prepacked image cube, for example, from ImageCube.forward()

        Returns:
            (torch.complex tensor, of shape ``(nchan, npix, npix)``): the FFT of the image cube, in packed format.
        """

        # make sure the cube is 3D
        assert cube.dim() == 3, "cube must be 3D"

        # the self.cell_size prefactor (in arcsec) is to obtain the correct output units
        # since it needs to correct for the spacing of the input grid.
        # See MPoL documentation and/or TMS Eqn A8.18 for more information.
        self.vis = self.coords.cell_size ** 2 * torch.fft.fftn(cube, dim=(1, 2))

        return self.vis

    @property
    def ground_vis(self):
        r"""
        The visibility cube in ground format cube fftshifted for plotting with ``imshow``.

        Returns:
            (torch.complex tensor, of shape ``(nchan, npix, npix)``): the FFT of the image cube, in sky plane format.
        """

        return utils.packed_cube_to_ground_cube(self.vis)

    @property
    def ground_amp(self):
        r"""
        The amplitude of the cube, arranged in unpacked format corresponding to the FFT of the sky_cube. Array dimensions for plotting given by ``self.coords.vis_ext``.

        Returns:
            torch.double : 3D amplitude cube of shape ``(nchan, npix, npix)``
        """
        return torch.abs(self.ground_vis)

    @property
    def ground_phase(self):
        r"""
        The phase of the cube, arranged in unpacked format corresponding to the FFT of the sky_cube. Array dimensions for plotting given by ``self.coords.vis_ext``.

        Returns:
            torch.double : 3D phase cube of shape ``(nchan, npix, npix)``
        """
        return torch.angle(self.ground_vis)


class NuFFTNarrow(nn.Module):
    r"""
    A layer that translates an ImageCube to loose samples of the Fourier plane,
    given :math:`u,v` locations.

    Args:
        cell_size (float): the width of an image-plane pixel [arcseconds]
        npix (int): the number of pixels per image side
        coords (GridCoords): an object already instantiated from the GridCoords class. If providing this, cannot provide ``cell_size`` or ``npix``.
    """

    def __init__(self, cell_size=None, npix=None, coords=None, uu=None, vv=None):

        super().__init__()

        # we don't want to bother with the nchan argument here, so
        # we don't use the convenience method _setup_coords
        # and just do it manually
        if coords:
            assert (
                npix is None and cell_size is None
            ), "npix and cell_size must be empty if precomputed GridCoords are supplied."
            self.coords = coords

        elif npix or cell_size:
            assert (
                coords is None
            ), "GridCoords must be empty if npix and cell_size are supplied."

            self.coords = GridCoords(cell_size=cell_size, npix=npix)

        # initialize the non-uniform FFT object
        self.nufft_ob = torchkbnufft.KbNufft(
            im_size=(self.coords.npix, self.coords.npix)
        )

        if (uu is not None) and (vv is not None):
            self.k_traj = self._assemble_ktraj(uu, vv)

    def _klambda_to_radpix(self, klambda):
        """Convert a spatial frequency in units of klambda to radians/pixel.

        Args:
            klambda (float): spatial frequency in units of kilolambda
            cell_size (float): the size of a pixel in units of arcsec
        """

        # cycles per sky radian
        u_lam = klambda * 1e3  # [lambda, or cycles/radian]

        # radians per sky radian
        u_rad_per_rad = u_lam * 2 * np.pi  # [radians / sky radian]

        # size of pixel in radians
        # self.coords.dl  # [sky radians/pixel]

        # radians per pixel
        u_rad_per_pix = u_rad_per_rad * self.coords.dl  # [radians / pixel]

        return u_rad_per_pix

    def _assemble_ktraj(self, uu, vv):
        r"""
        Convert a series of :math:`u, v` coordinates into a k-trajectory vector for the torchkbnufft routines.

        Args:
            uu (numpy array): u (East-West) spatial frequency coordinate [klambda]
            vv (numpy array): v (North-South) spatial frequency coordinate [klambda]
        """

        uu_radpix = self._klambda_to_radpix(uu)
        vv_radpix = self._klambda_to_radpix(vv)

        # k-trajectory needs to be packed the way the image is packed (y,x), so
        # the trajectory needs to be packed (v, u)
        k_traj = torch.tensor([vv_radpix, uu_radpix])

        return k_traj

    def forward(self, cube, uu=None, vv=None):
        """
        Perform the FFT of the image cube for each channel and interpolate to the uv points.

        Args:
            cube (torch.double tensor, of shape ``(nchan, npix, npix)``): a prepacked image cube, for example, from ImageCube.forward()
            uu (numpy array): u (East-West) spatial frequency coordinate [klambda]
            vv (numpy array): v (North-South) spatial frequency coordinate [klambda]

        Returns:
            (torch.complex tensor, of shape ``(nchan, nvis)``): the Fourier samples at the locations uu, vv.

        ..note::

            This routine assumes that uu and vv are *the same* for all channels. This is not strictly true.
            For spectral line imaging, it might be true enough.

        """

        if (uu is not None) and (vv is not None):
            k_traj = self._assemble_ktraj(uu, vv)
        else:
            k_traj = self.k_traj

        # "unpack" the cube, but leave it flipped
        shifted = torch.fft.fftshift(cube, dim=(1, 2))

        # convert the cube to an imaginary value
        complexed = shifted.type(torch.complex128)

        # expand the cube to include a batch dimension
        expanded = complexed.unsqueeze(0)
        # now [1, nchan, npix, npix] shape

        # send this through the object
        output = self.coords.cell_size ** 2 * self.nufft_ob(expanded, k_traj)
        # output is shape [1, nchan, ntraj]

        # remove the batch dimension
        return output[0]


# class NuFFTWide(NuFFTNarrow):
#     r"""
#     A layer that translates an ImageCube to loose samples of the Fourier plane,
#     given :math:`u,v` locations.

#     Args:
#         cell_size (float): the width of an image-plane pixel [arcseconds]
#         npix (int): the number of pixels per image side
#         coords (GridCoords): an object already instantiated from the GridCoords class. If providing this, cannot provide ``cell_size`` or ``npix``.
#     """

#     def __init__(self, cell_size=None, npix=None, coords=None, uu=None, vv=None):
