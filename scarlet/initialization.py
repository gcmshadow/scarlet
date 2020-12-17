import numpy as np
import logging

from . import operator
from .bbox import Box, overlapped_slices
from .constraint import CenterOnConstraint
from .interpolation import interpolate_observation
from .observation import Observation, LowResObservation
from .wavelet import Starlet, mad_wavelet
from . import measure
from functools import partial
from .constraint import PositivityConstraint
from .parameter import Parameter, relative_step


logger = logging.getLogger("scarlet.initialisation")


def get_best_fit_spectra(morphs, images):
    """Calculate best fitting spectra for multiple components.

    Solves min_A ||img - AS||^2 for the SED matrix A,
    assuming that the images only contain a single source.

    Parameters
    ----------
    morphs: list
        Morphology for each component in the source.
    images: array
        Observation to extract SEDs from.

    Returns
    -------
    SED: `~numpy.array`
    """
    K = len(morphs)
    _morph = morphs.reshape(K, -1)
    if isinstance(images, np.ndarray):
        images_ = images
    elif isinstance(images, Observation):
        images_ = images.images
    elif hasattr(images, "__iter__") and all(
        tuple(obs.frame == images[0].frame for obs in images)
    ):
        # all observations need to have the same frame for this mapping to work
        images_ = np.stack(tuple(obs.images for obs in images), axis=0)

    data = images_.reshape(images_.shape[0], -1)
    seds = np.dot(np.linalg.inv(np.dot(_morph, _morph.T)), np.dot(_morph, data.T))
    return seds


def get_pixel_spectrum(sky_coord, observations, correct_psf=False):
    """Get the spectrum at `sky_coord` in `observation`.

    Yields the spectrum of a single-pixel source with flux 1 in every channel,
    concatenated for all observations. If `correct_psf`, it homogenizes the PSFs of the
    observations, which yields the correct spectrum for a point source.

    Parameters
    ----------
    sky_coord: tuple
        Position in the observation
    observations: instance or list of `~scarlet.Observation`
        Observation to extract SED from.
    correct_psf: bool
        If PSF shape variations in the observations should be corrected.

    Returns
    -------
    spectrum: `~numpy.array`
    """

    if not hasattr(observations, "__iter__"):
        observations = (observations,)

    spectra = []
    for obs in observations:
        pixel = obs.frame.get_pixel(sky_coord)
        index = np.round(pixel).astype(np.int)
        spectrum = obs.images[:, index[0], index[1]].copy()

        if obs.frame.psf is not None and correct_psf:
            # image of point source in observed = obs.frame.psf
            psf_model = obs.frame.psf.get_model()
            psf_center = psf_model.max(axis=(1, 2))
            # best fit solution for the model amplitude of the center pixel
            # to yield to PSF center: (spectrum * psf_center) / psf_center**2
            # or shorter:
            spectrum /= psf_center

        spectra.append(spectrum)

    spectrum = np.concatenate(spectra).reshape(-1)

    if np.any(spectrum <= 0):
        # If the flux in all channels is  <=0,
        # the new sed will be filled with NaN values,
        # which will cause the code to crash later
        msg = "Zero or negative spectrum {} at y={}, x={}".format(spectrum, *sky_coord)
        if np.all(spectrum <= 0):
            logger.warning(msg)
        else:
            logger.info(msg)

    return spectrum


def get_psf_spectrum(sky_coord, observations):
    """Get spectrum for a point source at `sky_coord` in `observation`

    Equivalent to point source photometry for isolated sources. For extended source,
    this will underestimate the actual source flux in every channel. In case of crowding,
    the resulting photometry is likely contaminated by neighbors.

    Yields the spectrum of a PSF-homogenized source of flux 1 in every channel,
    concatenated for all observations.

    Parameters
    ----------
    sky_coord: tuple
        Position in the observation
    observations: instance or list of `~scarlet.Observation`
        Observation to extract SED from.

    Returns
    -------
    spectrum: `~numpy.array`
    """

    if not hasattr(observations, "__iter__"):
        observations = (observations,)

    spectra = []
    for obs in observations:

        pixel = obs.frame.get_pixel(sky_coord)
        index = np.round(pixel).astype(np.int)

        psf = obs.frame.psf.get_model()
        bbox = obs.frame.psf.bbox + (0, *index)
        img = bbox.extract_from(obs.images)

        # img now 0 outside of observation, psf is not:
        # restrict both to observed pixels to avoid truncation effects
        mask = img[0] > 0
        psf = psf[:, mask]  # flattens array in last two axes
        img = img[:, mask]

        # amplitude of img when projected onto psf
        # i.e. factor to multiply psf with to get img (if img looked like psf)
        spectrum = (img * psf).sum(axis=1) / (psf * psf).sum(axis=1)
        spectra.append(spectrum)

    spectrum = np.concatenate(spectra).reshape(-1)

    if np.any(spectrum <= 0):
        # If the flux in all channels is  <=0,
        # the new sed will be filled with NaN values,
        # which will cause the code to crash later
        msg = "Zero or negative spectrum {} at y={}, x={}".format(spectrum, *sky_coord)
        if np.all(spectrum <= 0):
            logger.warning(msg)
        else:
            logger.info(msg)

    return spectrum


def get_minimal_boxsize(size, min_size=15, increment=8):
    boxsize = min_size
    while boxsize < size:
        boxsize += increment  # keep box sizes quite small
    return boxsize


def trim_morphology(center_index, morph, bg_thresh=0):
    # trim morph to pixels above threshold
    mask = morph > bg_thresh
    morph[~mask] = 0

    bbox = Box.from_data(morph, min_value=0)

    # find fitting bbox
    if bbox.contains(center_index):
        size = 2 * max(
            (
                center_index[0] - bbox.start[-2],
                bbox.stop[0] - center_index[-2],
                center_index[1] - bbox.start[-1],
                bbox.stop[1] - center_index[-1],
            )
        )
    else:
        size = 0

    # define new box and cut morphology accordingly
    boxsize = get_minimal_boxsize(size)
    bottom = center_index[0] - boxsize // 2
    top = center_index[0] + boxsize // 2
    left = center_index[1] - boxsize // 2
    right = center_index[1] + boxsize // 2
    bbox = Box.from_bounds((bottom, top), (left, right))
    morph = bbox.extract_from(morph)
    return morph, bbox

def init_compact_source(
    sky_coord, frame, observations,
):
    """Initialize a source just like `init_extended_source`,
    but with the morphology of a point source.
    """

    # get PSF-corrected center pixel spectrum
    spectrum = get_pixel_spectrum(sky_coord, observations, correct_psf=True)

    # position in frame coordinates
    center = frame.get_pixel(sky_coord)
    center_index = np.round(center).astype(np.int)

    # morphology initialized as a point source
    morph_ = frame.psf.get_model().mean(axis=0)
    origin = (
        center_index[0] - (morph_.shape[0] // 2),
        center_index[1] - (morph_.shape[1] // 2),
    )
    bbox_ = Box(morph_.shape, origin=origin)

    # adjust box size to conform with extended sources
    size = max(morph_.shape)
    boxsize = get_minimal_boxsize(size)
    morph = np.zeros((boxsize, boxsize))
    origin = (
        center_index[0] - (morph.shape[0] // 2),
        center_index[1] - (morph.shape[1] // 2),
    )
    bbox = Box(morph.shape, origin=origin)

    slices = overlapped_slices(bbox, bbox_)
    morph[slices[0]] = morph_[slices[1]]

    # apply max normalization
    morph_max = morph.max()
    morph /= morph_max
    spectrum *= morph_max

    # expand to full bbox
    bbox = frame.bbox[0] @ bbox

    return spectrum, morph, bbox


def init_extended_source(
    sky_coord,
    frame,
    observations,
    coadd=None,
    coadd_rms=None,
    compact=False,
    thresh=1,
    symmetric=True,
    monotonic="flat",
    min_grad=0.1,
    spectrum = None,
    full = False,
):
    if not hasattr(observations, "__iter__"):
        observations = (observations,)

    if spectrum is None:
        # get PSF-corrected center pixel spectrum
        spectrum = get_pixel_spectrum(sky_coord, observations, correct_psf=True)

    # position in frame coordinates
    center = frame.get_pixel(sky_coord)
    center_index = np.round(center).astype(np.int)

    if coadd is None:
        # determine initial SED from peak position
        # don't correct for PSF variation: emphasize sharper bands
        spectra = [
            get_pixel_spectrum(sky_coord, obs, correct_psf=False)
            for obs in observations
        ]

        try:
            bg_rmses = np.array(
                [
                    [1 / np.sqrt(w[w > 0].mean()) for w in obs.weights]
                    for obs in observations
                ]
            )
        except:
            raise AttributeError(
                "Observation.weights missing! Please set inverse variance weights"
            )
        coadd, bg_rms = build_sed_coadd(spectra, bg_rmses, observations)
    else:
        if coadd_rms is None:
            raise AttributeError(
                "background cutoff missing! Please set argument coadd_rms"
            )
        coadd = coadd.copy()  # will likely be reused by other sources
        bg_rms = coadd_rms

    # Apply the necessary constraints
    if symmetric:
        morph = operator.prox_uncentered_symmetry(
            coadd,
            0,
            center=center_index,
            algorithm="sdss",
        )
    else:
        morph = coadd
    if monotonic:
        if monotonic is True:
            monotonic = "angle"
        # use finite thresh to remove flat bridges
        prox_monotonic = operator.prox_weighted_monotonic(
            morph.shape,
            neighbor_weight=monotonic,
            center=center_index,
            min_gradient=min_grad,
        )
        morph = prox_monotonic(morph, 0).reshape(morph.shape)

    if full == False:
        # truncate morph at thresh * bg_rms
        threshold = bg_rms * thresh
        morph, bbox = trim_morphology(center_index, morph, bg_thresh=threshold)
        bbox = frame.bbox[0] @ bbox
    else:
        bbox = None

    # normalize to unity at peak pixel for the imposed normalization
    if morph.sum() > 0:
        morph /= morph.max()

        # since the spectrum assumes a point source:
        # determine the optimal amplitude for matching morph and the model psf
        # TODO: morph is still convolved with the observed PSF, but we compute
        # amplitude correction as if it were not..
        if frame.psf is not None:
            psf = frame.psf.get_model()

            shape = (psf.shape[0], *morph.shape)
            bbox_ = Box(
                shape,
                origin=(
                    psf.shape[0] - shape[0],
                    psf.shape[1] // 2 - shape[1] // 2,
                    psf.shape[2] // 2 - shape[2] // 2,
                ),
            )
            psf = bbox_.extract_from(psf)

            # spectrum assumes the source to have point-source morphology,
            # otherwise get_pixel_spectrum is not well-defined.
            # factor corrects that by finding out how much (in terms of a scalar number)
            # morph looks like the (model) psf.
            # if model psf is constant across bands (as it should) then factor
            # is constant as well
            factor = (morph[None, :, :] * psf).sum(axis=(1, 2)) / (psf * psf).sum(
                axis=(1, 2)
            )

            # correct amplitude from point source to this morph
            spectrum /= factor

    else:
        morph = CenterOnConstraint()(morph, 0)
        msg = "No flux in morphology model for source at y={0} x={1}".format(*sky_coord)
        logger.warning(msg)

    return spectrum, morph, bbox


def init_starlet_source(
    sky_coord,
    model_frame,
    observations,
    thresh = 1,
    spectrum = None,
    full=False,
    coadd=None,
    coadd_rms=None,
    symmetric=False,
    monotonic="flat",
    min_grad=0.,
    starlet_thresh=5,
):

    # initialize as extended from observation
    if not hasattr(observations, "__iter__"):
        observations = (observations,)
    if spectrum is not None:
        constraint = PositivityConstraint(zero=1e-20)  # slightly positive values
        step = partial(relative_step, factor=1e-6)
        spectrum = Parameter(spectrum,
                             name="spectrum",
                             step=step,
                             constraint=constraint,
                             )

    spectrum, morph, bbox = init_extended_source(sky_coord,
                                                 model_frame,
                                                 observations,
                                                 spectrum = spectrum,
                                                 coadd=coadd,
                                                 thresh = thresh,
                                                 coadd_rms=coadd_rms,
                                                 symmetric=symmetric,
                                                 monotonic=monotonic,
                                                 min_grad=min_grad,
                                                 full=full,
                                                 )

    noise = []
    for obs in observations:
        noise += [
            mad_wavelet(obs.images)
            * np.sqrt(np.sum(obs._diff_kernels.image ** 2, axis=(-2, -1)))
        ]
    noise = np.concatenate(noise)

    # Threshold in units of noise on the coadd
    starlet_thresh = starlet_thresh * np.sqrt(np.sum((spectrum._data * noise) ** 2))
    return morph, starlet_thresh, bbox


def init_multicomponent_source(
    sky_coord,
    frame,
    observations,
    coadd=None,
    coadd_rms=None,
    flux_percentiles=None,
    thresh=1,
    symmetric=True,
    monotonic="flat",
    min_grad=0.1,
    obs_ref=None,
):
    """Initialize multiple components
    See `MultiComponentSource` for a description of the parameters
    """
    try:
        iter(observations)
    except TypeError:
        observations = [observations]

    if obs_ref is None:
        if len(observations) == 1:
            obs_ref = observations[0]
        else:
            # The observation that lives in the same plane as the frame
            loc = np.where([type(obs) is Observation for obs in observations])
            # If more than one element is an `Observation`, then pick the first one as a reference (arbitrary)
            obs_ref = observations[loc[0]]

    if flux_percentiles is None:
        flux_percentiles = [25]

    # Initialize the first component as an extended source
    sed, morph, bbox = init_extended_source(
        sky_coord,
        frame,
        observations,
        coadd=coadd,
        coadd_rms=coadd_rms,
        thresh=thresh,
        symmetric=symmetric,
        monotonic=monotonic,
        min_grad=min_grad,
    )
    # create a list of components from base morph by layering them on top of
    # each other so that they sum up to morph
    K = len(flux_percentiles) + 1

    Ny, Nx = morph.shape
    morphs = np.zeros((K, Ny, Nx), dtype=morph.dtype)
    morphs[0, :, :] = morph[:, :]
    max_flux = morph.max()
    percentiles_ = np.sort(flux_percentiles)
    last_thresh = 0
    for k in range(1, K):
        perc = percentiles_[k - 1]
        flux_thresh = perc * max_flux / 100
        mask_ = morph > flux_thresh
        morphs[k - 1][mask_] = flux_thresh - last_thresh
        morphs[k][mask_] = morph[mask_] - flux_thresh
        last_thresh = flux_thresh

    # renormalize morphs: initially Smax
    for k in range(K):
        if np.all(morphs[k] <= 0):
            msg = "Zero or negative morphology for component {} at y={}, x={}"
            logger.warning(msg.format(k, *sky_coord))
        morphs[k] /= morphs[k].max()

    # optimal SEDs given the morphologies, assuming img only has that source
    boxed_img = bbox.extract_from(obs_ref.images)
    spectra = get_best_fit_spectra(morphs, boxed_img)

    for k in range(K):
        if np.all(spectra[k] <= 0):
            # If the flux in all channels is  <=0,
            # the new sed will be filled with NaN values,
            # which will cause the code to crash later
            msg = "Zero or negative spectrum {} for component {} at y={}, x={}".format(
                spectra[k], k, *sky_coord
            )
            logger.warning(msg)

    # avoid using the same box for multiple components
    boxes = tuple(bbox.copy() for k in range(K))

    # # define minimal boxes (NOTE: dangerous due to box truncation)
    # morphs_ = []
    # boxes = []
    # threshold = 0
    # for k in range(K):
    #     morph, bbox = trim_morphology(sky_coord, frame, morphs[k], threshold)
    #     morphs_.append(morph)
    #     boxes.append(bbox)
    # morphs = morphs_

    return spectra, morphs, boxes


def build_sed_coadd(seds, bg_rmses, observations, obs_ref=None):
    """Build a channel weighted coadd to use for source detection

    Parameters
    ----------
    sed: array
        SED at the center of the source.
    bg_rms: array
        Background RMS in each channel in observation.
    observations: list of `~scarlet.observation.Observation`
        Observations to use for the coadd.
    obs_ref: `scarlet.Observation`
        observation to use as a reference frame.
        If set to None, the first (or only if applicable) element with type `Observation` is used.

    Returns
    -------
    detect: array
        2D image created by weighting all of the channels by SED
    bg_cutoff: float
        The minimum value in `detect` to include in detection.
    """
    if not hasattr(observations, "__iter__"):
        observations = (observations,)
        seds = (seds,)
        bg_rmses = (bg_rmses,)

    if len(observations) == 1:
        obs_ref = observations[0]

    # The observation that lives in the same plane as the frame
    if obs_ref is None:
        loc = np.where([type(obs) is Observation for obs in observations])
        obs_ref = observations[np.int(loc[0])]
    else:
        # The observation that lives in the same plane as the frame
        assert type(obs_ref) is not LowResObservation, (
            f"Reference observation should not be a `LowResObservation`. The observation, {obs_ref} "
            f"provided refers to an observation of type: {type(obs_ref)}"
        )

    positive_img = []
    positive_bgrms = []
    weights = []
    jacobian_args = []
    for i, obs in enumerate(observations):
        sed = seds[i]
        try:
            iter(sed)
        except TypeError:
            sed = [sed]
        C = len(sed)
        bg_rms = bg_rmses[i]
        try:
            iter(bg_rms)
        except TypeError:
            bg_rms = [bg_rms]
        if np.any(np.array(bg_rms) <= 0):
            raise ValueError("bg_rms must be greater than zero in all channels")

        positive = [c for c in range(C) if sed[c] > 0]
        if type(obs) is not LowResObservation:
            positive_img += [obs.images[c] for c in positive]
        else:
            positive_img += [
                interpolate_observation(obs, obs_ref.frame)[c] for c in positive
            ]
        positive_bgrms += [bg_rms[c] for c in positive]
        weights += [sed[c] / bg_rms[c] ** 2 for c in positive]
        jacobian_args += [sed[c] ** 2 / bg_rms[c] ** 2 for c in positive]

    detect = np.einsum("i,i...", np.array(weights), positive_img) / np.sum(
        jacobian_args
    )

    # thresh is multiple above the rms of detect (weighted variance across channels)
    bg_cutoff = np.sqrt(
        (np.array(weights) ** 2 * np.array(positive_bgrms) ** 2).sum()
    ) / np.sum(jacobian_args)
    return detect, bg_cutoff


def build_initialization_coadd(observations, filtered_coadd=False, obs_idx=None):
    """Build a channel weighted coadd to use for source detection

    For `LowResObservation`, images are interpolated to a reference frame

    Parameters
    ----------
    observations: `~scarlet.observation.Observation`
        Observation to use for the coadd.
    filtered_coadd: `bool`
        if set to True, images are filtered using wavelet filtering before interpolation/coadding
    obs_idx: `int`
        index of the observation in observations to use as a reference frame.
        If set to None, the first element with type `Observation` is used.

    Returns
    -------
    detect: array
        2D image created by weighting all of the channels by SED
    bg_cutoff: float
        The minimum value in `detect` to include in detection.
    """
    try:
        iter(observations)
    except TypeError:
        observations = [observations]

    if obs_idx is None:
        loc = np.where([type(obs) is Observation for obs in observations])
        obs_ref = observations[loc[0][0]]
    else:
        # The observation that lives in the same plane as the frame
        assert type(observations[obs_idx]) is Observation, (
            f"Reference observation should be an `Observation`. The observation index, {obs_idx} "
            f"provided refers to an observation of type: {type(observations[obs_idx])}"
        )
        # If more than one element is an `Observation`, then pick the first one as a reference (arbitrary)
        obs_ref = observations[obs_idx]

    coadd = 0
    jacobian = 0
    weights = 0
    for obs in observations:
        try:
            weights = np.array([w[w > 0].mean() for w in obs.weights])
        except:
            raise AttributeError(
                "Observation.weights missing! Please set inverse variance weights"
            )

        if obs is obs_ref:
            if filtered_coadd is True:
                star = Starlet(obs.images)
                # Sarlet filtering at 5 sigma
                star.filter()
                # Sets the last starlet scale to 0 to remove the wings of the profile introduced by psfs
                star.coefficients[:, -1, :, :] = 0
                # Positivity
                star.coefficients[star.coefficients < 0] = 0
                images = star.image
            else:
                images = obs.images
        else:
            # interpolate low-res to reference resolution
            images = interpolate_observation(
                obs, obs_ref.frame, wave_filter=filtered_coadd
            )
        if filtered_coadd is True:
            coadd += np.sum(
                images / np.sum(images, axis=(-2, -1))[:, None, None], axis=0
            )
        else:
            # Weighted coadd
            coadd += (images * weights[:, None, None]).sum(axis=(0))
            jacobian += weights.sum()

    if filtered_coadd is True:
        coadd /= np.max(coadd)
        bg_cutoff = 0.01
        return coadd, bg_cutoff
    coadd /= jacobian
    # thresh is multiple above the rms of detect (weighted variance across channels)
    bg_cutoff = np.sqrt((weights ** 2).sum()) / jacobian
    return coadd, bg_cutoff


def hasEdgeFlux(source, edgeDistance=1):
    """hasEdgeFlux

    Determine whether or not a source has flux within `edgeDistance`
    of the edge.

    Parameters
    ----------
    source : `scarlet.Component`
        The source to check for edge flux
    edgeDistance : int
        The distance from the edge of the image to consider
        a source an edge source. For example if `edgeDistance=3`
        then any source within 3 pixels of the edge will be
        considered to have edge flux.
        If `edgeDistance` is `None` then the edge check is ignored.

    Returns
    -------
    isEdge: `bool`
        Whether or not the source has flux on the edge.
    """
    if edgeDistance is None:
        return False

    assert edgeDistance > 0

    # Use the first band that has a non-zero SED
    flux = measure.flux(source)
    if hasattr(source, "sed"):
        band = np.min(np.where(flux > 0)[0])
    else:
        band = np.min(np.where(flux > 0)[0])
    model = source.get_model()[band]
    for edge in range(edgeDistance):
        if (
            np.any(model[edge - 1] > 0)
            or np.any(model[-edge] > 0)
            or np.any(model[:, edge - 1] > 0)
            or np.any(model[:, -edge] > 0)
        ):
            return True
    return False


def initAllSources(
    frame,
    centers,
    observation,
    symmetric=False,
    monotonic=True,
    thresh=1,
    maxComponents=1,
    edgeDistance=1,
    shifting=False,
    downgrade=True,
    fallback=True,
    minGradient=0,
):
    """Initialize all sources in a blend

    Any sources which cannot be initialized are returned as a `skipped`
    index, the index needed to reinsert them into a catalog to preserve
    their index in the output catalog.

    See `~initSources` for a description of the parameters

    Parameters
    ----------
    centers : list of tuples
        `(y, x)` center location for each source

    Returns
    -------
    sources: list
        List of intialized sources, where each source derives from the
        `~scarlet.Component` class.
    """
    # Only deblend sources that can be initialized
    sources = []
    skipped = []
    for k, center in enumerate(centers):
        source = initSource(
            frame,
            center,
            observation,
            symmetric,
            monotonic,
            thresh,
            maxComponents,
            edgeDistance,
            shifting,
            downgrade,
            fallback,
            minGradient,
        )
        if source is not None:
            sources.append(source)
        else:
            skipped.append(k)
    return sources, skipped


def initSource(
    frame,
    center,
    observation,
    symmetric=False,
    monotonic=True,
    thresh=1,
    maxComponents=1,
    edgeDistance=1,
    shifting=False,
    downgrade=True,
    fallback=True,
    minGradient=0,
):
    """Initialize a Source

    The user can specify the number of desired components
    for the modeled source. If scarlet cannot initialize a
    model with the desired number of components it continues
    to attempt initialization of one fewer component until
    it finds a model that can be initialized.
    It is possible that scarlet will be unable to initialize a
    source with the desired number of components, for example
    a two component source might have degenerate components,
    a single component source might not have enough signal in
    the joint coadd (all bands combined together into
    single signal-to-noise weighted image for initialization)
    to initialize, and a true spurious detection will not have
    enough signal to initialize as a point source.
    If all of the models fail, including a `PointSource` model,
    then this source is skipped.

    Parameters
    ----------
    frame : `LsstFrame`
        The model frame for the scene
    center : `tuple` of `float``
        `(y, x)` location for the center of the source.
    observation : `~scarlet.Observation`
        The `Observation` that contains the images, weights, and PSF
        used to generate the model.
    symmetric : `bool`
        Whether or not the object is symmetric
    monotonic : `bool`
        Whether or not the object has flux monotonically
        decreasing from its center
    thresh : `float`
        Fraction of the background to use as a threshold for
        each pixel in the initialization
    maxComponents : int
        The maximum number of components in a source.
        If `fallback` is `True` then when
        a source fails to initialize with `maxComponents` it
        will continue to subtract one from the number of components
        until it reaches zero (which fits a point source).
        If a point source cannot be fit then the source is skipped.
    edgeDistance : int
        The distance from the edge of the image to consider
        a source an edge source. For example if `edgeDistance=3`
        then any source within 3 pixels of the edge will be
        considered to have edge flux.
        If `edgeDistance` is `None` then the edge check is ignored.
    shifting : bool
        Whether or not to fit the position of a source.
        This is an expensive operation and is typically only used when
        a source is on the edge of the detector.
    downgrade : bool
        Whether or not to decrease the number of components for sources
        with small bounding boxes. For example, a source with no flux
        outside of its 16x16 box is unlikely to be resolved enough
        for multiple components, so a single source can be used.
    fallback : bool
        Whether to reduce the number of components
        if the model cannot be initialized with `maxComponents`.
        This is unlikely to be used in production
        but can be useful for troubleshooting when an error can cause
        a particular source class to fail every time.
    """
    from .source import PointSource, ExtendedSource

    while maxComponents > 1:
        try:
            source = ExtendedSource(
                frame,
                center,
                observation,
                thresh=thresh,
                shifting=shifting,
                K=maxComponents,
            )
            try:
                source.check_parameters()
                # Make sure that SED is >0 in at least 1 band
                if np.any(
                    [
                        np.all(child.children[0].get_model() <= 0)
                        for child in source.children
                    ]
                ):
                    raise ArithmeticError
            except ArithmeticError:
                msg = "Could not initialize source at {} with {} components".format(
                    center, maxComponents
                )
                logger.warning(msg)
                raise ValueError(msg)

            if downgrade and np.all(np.array(source.bbox.shape[1:]) <= 8):
                # the source is in a small box so it must be a point source
                maxComponents = 0
            elif downgrade and np.all(np.array(source.bbox.shape[1:]) <= 16):
                # if the source is in a slightly larger box
                # it is not big enough to model with 2 components
                maxComponents = 1
            elif hasEdgeFlux(source, edgeDistance):
                source.shifting = True

            break
        except Exception as e:
            if not fallback:
                raise e
            # If the MultiComponentSource failed to initialize
            # try an ExtendedSource
            maxComponents -= 1

    if maxComponents == 1:
        try:
            source = ExtendedSource(
                frame, center, observation, thresh=thresh, shifting=shifting
            )

            try:
                source.check_parameters()
                if np.all(source.children[0].get_model() <= 0):
                    raise ArithmeticError
            except ArithmeticError:
                msg = "Could not initlialize source at {} with 1 component".format(
                    center
                )
                logger.warning(msg)
                raise ValueError(msg)

            if downgrade and np.all(np.array(source.bbox.shape[1:]) <= 16):
                # the source is in a small box so it must be a point source
                maxComponents = 0
            elif hasEdgeFlux(source, edgeDistance):
                source.shifting = True
        except Exception as e:
            if not fallback:
                raise e
            # If the source is too faint for background detection,
            # initialize it as a PointSource
            maxComponents -= 1

    if maxComponents == 0:
        try:
            source = PointSource(frame, center, observation)
        except Exception:
            # None of the models worked to initialize the source,
            # so skip this source
            return None

    if hasEdgeFlux(source, edgeDistance):
        # The detection algorithm implemented in meas_algorithms
        # does not place sources within the edge mask
        # (roughly 5 pixels from the edge). This results in poor
        # deblending of the edge source, which for bright sources
        # may ruin an entire blend. So we reinitialize edge sources
        # to allow for shifting and return the result.
        if not isinstance(source, PointSource) and not shifting:
            return initSource(
                frame,
                center,
                observation,
                symmetric,
                monotonic,
                thresh,
                maxComponents,
                edgeDistance,
                shifting=True,
                downgrade=downgrade,
                fallback=fallback,
            )
        source.isEdge = True
    else:
        source.isEdge = False

    return source
