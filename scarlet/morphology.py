import autograd.numpy as np

from .bbox import Box
from .constraint import (
    ConstraintChain,
    L0Constraint,
    PositivityConstraint,
    MonotonicityConstraint,
    SymmetryConstraint,
    CenterOnConstraint,
    NormalizationConstraint,
)
from .frame import Frame
from .model import Model
from .parameter import Parameter, relative_step
from .psf import PSF
from .wavelet import Starlet
from . import fft
from . import interpolation


class Morphology(Model):
    """Morphology base class

    The class describes the 2D image of the spatial dependence of
    `~scarlet.FactorizedComponent`.

    Parameters
    ----------
    frame: `~scarlet.Frame`
        Characterization of the model
    parameters: list of `~scarlet.Parameter`
    bbox: `~scarlet.Box`
        2D bounding box of this model
    """

    def __init__(self, frame, *parameters, bbox=None):
        assert isinstance(frame, Frame)
        self.frame = frame

        if bbox is None:
            bbox = frame.bbox
        assert isinstance(bbox, Box)
        self.bbox = bbox

        super().__init__(*parameters)


class ImageMorphology(Morphology):
    """Morphology from an image

    The class uses an arbitrary image as non-parametric model. To allow for subpixel
    offsets, a Fourier-based shifting transformation is available.

    Parameters
    ----------
    frame: `~scarlet.Frame`
        Characterization of the model
    image: 2D array or `~scarlet.Parameter`
        Image parameter
    bbox: `~scarlet.Box`
        2D bounding box for focation of the image in `frame`
    shift: None or `~scarlet.Parameter`
        2D shift parameter (in units of image pixels)
    """

    def __init__(self, frame, image, bbox=None, shift=None):
        if isinstance(image, Parameter):
            assert image.name == "image"
        else:
            constraint = PositivityConstraint()
            image = Parameter(
                image, name="image", step=relative_step, constraint=constraint
            )

        if bbox is None:
            assert frame.bbox[1:].shape == image.shape
            bbox = Box(image.shape)
        else:
            assert bbox.shape == image.shape

        if shift is None:
            parameters = (image,)
        else:
            assert shift.shape == (2,)
            if isinstance(shift, Parameter):
                assert shift.name == "shift"
            else:
                shift = Parameter(shift, name="shift", step=1e-2)
            parameters = (image, shift)
            # fft helpers
            padding = 10
            self.fft_shape = fft._get_fft_shape(image, image, padding=padding)
            self.shifter_y, self.shifter_x = interpolation.mk_shifter(self.fft_shape)

        super().__init__(frame, *parameters, bbox=bbox)

    def get_model(self, *parameters):
        image = self.get_parameter(0, *parameters)
        shift = self.get_parameter(1, *parameters)
        return self._shift_image(shift, image)

    def _shift_image(self, shift, image):
        if shift is not None:
            X = fft.Fourier(image)
            X_fft = X.fft(self.fft_shape, (0, 1))

            # Apply shift in Fourier
            result_fft = (
                X_fft
                * np.exp(self.shifter_y[:, None] * shift[0])
                * np.exp(self.shifter_x[None, :] * shift[1])
            )

            X = fft.Fourier.from_fft(result_fft, self.fft_shape, X.shape, [0, 1])
            return np.real(X.image)
        return image


class PointSourceMorphology(Morphology):
    """Morphology from a PSF

    The class uses `frame.psf` as model, evaluated at `center`

    Parameters
    ----------
    frame: `~scarlet.Frame`
        Characterization of the model
    center: array or `~scarlet.Parameter`
        2D center parameter (in units of frame pixels)
    """

    def __init__(self, frame, center):

        assert frame.psf is not None and isinstance(frame.psf, PSF)
        self.psf = frame.psf

        # define bbox
        pixel_center = tuple(np.round(center).astype("int"))
        bottom = pixel_center[0] - self.psf.shape[1] // 2
        top = pixel_center[0] + self.psf.shape[1] // 2
        left = pixel_center[1] - self.psf.shape[2] // 2
        right = pixel_center[1] + self.psf.shape[2] // 2
        bbox = Box.from_bounds((bottom, top), (left, right))

        # morph parameters is simply 2D center
        if isinstance(center, Parameter):
            assert center.name == "center"
            self.center = center
        else:
            self.center = Parameter(center, name="center", step=1e-1)

        super().__init__(frame, self.center, bbox=bbox)

    def get_model(self, *parameters):
        center = self.get_parameter(0, *parameters)
        spec_bbox = Box((0,))
        return self.psf(*center, bbox=spec_bbox @ self.bbox)[0]


class StarletMorphology(Morphology):
    """Morphology from a starlet representation of an image

    The class uses the starlet parameterization as an overcomplete, non-parametric model.

    Parameters
    ----------
    frame: `~scarlet.Frame`
        Characterization of the model
    image: 2D array
        Initial image to construct starlet transform
    bbox: `~scarlet.Box`
        2D bounding box for focation of the image in `frame`
    threshold: float
        Lower bound on threshold for all but the last starlet scale
    """

    def __init__(self, frame, image, bbox=None, threshold=0):

        if bbox is None:
            assert frame.bbox[1:].shape == image.shape
            bbox = Box(image.shape)
        else:
            assert bbox.shape == image.shape

        # Starlet transform of morphologies (n1,n2) with 4 dimensions: (1,lvl,n1,n2), lvl = wavelet scales
        self.transform = Starlet(image)
        # The starlet transform is the model
        coeffs = self.transform.coefficients
        # wavelet-scale norm
        starlet_norm = self.transform.norm
        # One threshold per wavelet scale: thresh*norm
        thresh_array = np.zeros(coeffs.shape) + threshold
        thresh_array = (
            thresh_array * np.array([starlet_norm])[..., np.newaxis, np.newaxis]
        )
        # We don't threshold the last scale
        thresh_array[:, -1, :, :] = 0

        constraint = ConstraintChain(L0Constraint(thresh_array), PositivityConstraint())

        coeffs = Parameter(coeffs, name="coeffs", step=1e-2, constraint=constraint)
        super().__init__(frame, coeffs, bbox=bbox)

    def get_model(self, *parameters):
        # Takes the inverse transform of parameters as starlet coefficients
        coeffs = self.get_parameter(0, *parameters)
        return Starlet(coefficients=coeffs).image[0]


class ExtendedSourceMorphology(ImageMorphology):
    def __init__(
        self,
        frame,
        center,
        image,
        bbox=None,
        monotonic="angle",
        symmetric=False,
        min_grad=0,
        shifting=False,
    ):
        """Non-parametric image morphology designed for galaxies as extended sources.

        Parameters
        ----------
        frame: `~scarlet.Frame`
            The frame of the full model
        center: tuple
            Center of the source
        image: `numpy.ndarray`
            Image of the source.
        bbox: `~scarlet.Box`
            2D bounding box for focation of the image in `frame`
        monotonic: ['flat', 'angle', 'nearest'] or None
            Which version of monotonic decrease in flux from the center to enforce
        symmetric: `bool`
            Whether or not to enforce symmetry.
        min_grad: float in [0,1)
            Minimal radial decline for monotonicity (in units of reference pixel value)
        shifting: `bool`
            Whether or not a subpixel shift is added as optimization parameter
        """

        constraints = []
        # backwards compatibility: monotonic was boolean
        if monotonic is True:
            monotonic = "angle"
        elif monotonic is False:
            monotonic = None
        if monotonic is not None:
            # most astronomical sources are monotonically decreasing
            # from their center
            constraints.append(
                MonotonicityConstraint(neighbor_weight=monotonic, min_gradient=min_grad)
            )

        if symmetric:
            # have 2-fold rotation symmetry around their center ...
            constraints.append(SymmetryConstraint())

        constraints += [
            # ... and are positive emitters
            PositivityConstraint(),
            # prevent a weak source from disappearing entirely
            CenterOnConstraint(),
            # break degeneracies between sed and morphology
            NormalizationConstraint("max"),
        ]
        morph_constraint = ConstraintChain(*constraints)
        image = Parameter(image, name="image", step=1e-2, constraint=morph_constraint)

        self.pixel_center = np.round(center).astype("int")
        if shifting:
            shift = Parameter(center - self.pixel_center, name="shift", step=1e-1)
        else:
            shift = None
        self.shift = shift

        super().__init__(frame, image, bbox=bbox, shift=shift)

    @property
    def center(self):
        if self.shift is not None:
            return self.pixel_center + self.shift
        else:
            return self.pixel_center
