from __future__ import print_function
from openslide import OpenSlide
import numpy as np
import time
import cv2

from .foreground import get_foreground
from .normalize import reinhard

class Slide(object):
  """ Slide object for interfacing with Aperio SVS slides

  Upon initialization the Slide object opens the SVS file, finds tissue area,
  using Otsu thresholding, and populates a list of tiles at the requested magnification
  and size.

  ```
  slide_defaults = {
    'slide_path': None,
    'low_level_mag': 5,
    'preprocess_fn': lambda x: x,  ## ID
    'process_mag': 10,
    'process_size': 256,
    'normalize_fn': reinhard,
    'background_speed': 'fast', # One of 'fast' or 'accurate'
    'background_threshold': 210,
    'background_pct': 0.15,
    'oversample_factor': 1.25,
    'output_types': [],
    'output_res': '5x',
    'verbose': False}
  ```

  Args:
    slide_path: path to the slide
    process_mag: int for the magnification level (5, 10, 20, 40)
    process_size: int edge length of the tile.
    normalize_fn: function to apply on each individual tile before returning
    oversample_factor: float, the factor by which to oversample from the slide when 
      returning tiles
    
    # Planned
    low_level_mag: magnification to hold the reconstructed images

  Returns:
    Slide object

  https://stackoverflow.com/questions/47086599/parallelising-tf-data-dataset-from-generator
  """
  def __init__(self, **kwargs):
    slide_defaults = {
      'slide_path': None,
      'low_level_mag': 5,
      'preprocess_fn': lambda x: x,  ## ID
      'process_mag': 10,
      'process_size': 256,
      'normalize_fn': reinhard,
      'background_speed': 'fast', # One of 'fast', 'accurate' or 'mask'
      'background_image': None,
      'background_threshold': 210,
      'background_pct': 0.15,
      'oversample_factor': 1.25,
      'output_types': [],
      'output_mag': 5,
      'verbose': False}
    slide_defaults.update(kwargs)
    for key, val in slide_defaults.items():
      setattr(self, key, val)

    self.svs = self._parse_svs_info()

    ## TODO allow choice of output size by providing and output_mag
    ## Get level according to downsample for scan size --> output_mag.

    # self.low_level_index = self.get_low_level_index()
    # self.foreground = get_foreground(self.svs, low_level_index=2)
    self.foreground = get_foreground(self.svs)
    self._get_load_params()
    self.tile()

    ## Reconstruction params
    self._get_place_params()
    self.output_imgs = {}

    ## Finally check read tile
    self._check_read_tile()


  def _get_low_level_index(self):
    ## get the info to read/write the low-level image more efficiently
    ## This operation is instead of simply using the lowest resolution
    ## size to write output.
    ## Use the scanned power, and requested magnification to find the downsample factor
    pass


  def _parse_svs_info(self):
    """ Returns the OpenSlide object

    Populates a dict with:
    - scanning power
    - downsample fractions
    - low-level dimensions
    """

    svs = OpenSlide(self.slide_path)
    scan_power = int(svs.properties['aperio.AppMag'])
    level_count = svs.level_count
    high_power_dim = svs.level_dimensions[0][::-1]
    low_power_dim = svs.level_dimensions[-1][::-1]

    #if scan_power == 20 and level_count ==4:
    #  raise Exception('Malformed slide. {}'.format(self.slide_path))

    if self.verbose:
      print('Slide: %s' % self.slide_path)
      print('\t power: %d' % scan_power)
      print('\t levels: %d' % level_count)
      print('\t high_power_dim: %d %d' % (high_power_dim))
      print('\t low_power_dim: %d %d' % (low_power_dim))

    self.slide_info = {
      'scan_power': scan_power,
      'level_count': level_count,
      'high_power_dim': high_power_dim,
      'low_power_dim': low_power_dim,
      'level_dimensions': svs.level_dimensions }
    return svs


  def initialize_output(self, name, dim, mode='full'):
    """ Set up the output image to the same size as the level-0 shape

    """

    ## Initialize an image for dimensions preserving output
    if mode=='full':
      h,w = self.foreground.shape[:2]
      output_img = np.zeros((int(h), int(w), dim), dtype=np.float32)
      self.output_imgs[name] = output_img

    ## Initialize an image for one-value-per-tile output (dimensions reducing)
    elif mode=='tile':
      y = len(self.y_coord)
      x = len(self.x_coord)
      output_img = np.zeros((y, x, dim), dtype=np.float32)
      self.output_imgs[name] = output_img

    self.output_types.append(name)


  def _get_load_size(self, process_size, loading_level, downsample):
    """ Process the current slide attributes and requested image size

    Sets the attributes:
    self.ds_load_level

    Returns:
      size and loading index for openslide.read_region()
    """

    ds_load_level = int(self.svs.level_downsamples[loading_level])

    if self.verbose:
      print('Requested processing size: {}'.format(process_size))
      print('Estimated loading from level: {}'.format(loading_level))
      print('Downsample at estimated level: {}'.format(ds_load_level))

    self.ds_load_level = ds_load_level

    ## scan @ Nx ; request 10x
    ## scan @ Nx ; request 5x
    if ds_load_level == downsample:
      if self.verbose:
        print('Loading size: {} ({}x processing size)'.format(
          process_size, 1))
      return process_size, 1

    ## scan @ 40x; request 20x
    if ds_load_level < downsample:
      ratio = int(downsample / ds_load_level)
      if self.verbose:
        print('Loading size: {} ({}x processing size)'.format(
          process_size*ratio, ratio))
      return process_size*ratio, 1./ratio


  def _get_load_params(self):
    """ Translate slide params and requested `process_mag` into `read_region` args

    """

    ## Add a small number to the requested downsample because often we're off by some.
    EPS = 1e-3
    downsample = int(self.slide_info['scan_power'] / self.process_mag)
    loading_level = self.svs.get_best_level_for_downsample(downsample+EPS)
    load_level_dims = self.svs.level_dimensions[loading_level][::-1]
    loading_size, post_load_resize = self._get_load_size(self.process_size,
      loading_level, downsample)

    if self.verbose:
      print('Slide scanned at {} magnification'.format(self.slide_info['scan_power']))
      print('Requested processing at {} magnification'.format(self.process_mag))
      print('Downsample ~ {}'.format(downsample))
      print('Load from level {}'.format(loading_level))
      print('Level {} dimensions: {}'.format(loading_level, load_level_dims))

    self.downsample = downsample
    self.loading_level = loading_level
    self.load_level_dims = load_level_dims
    self.loading_size = loading_size
    self.post_load_resize = post_load_resize


  def _get_place_params(self):
    """ Logic translating processing size into reconstruct() args

    """

    ## Place w.r.t. level 0
    ## We have process downsample.. and downsample w.r.t. Last level
    ds_low_level = int(self.svs.level_downsamples[-1])
    place_downsample = self.downsample / float(ds_low_level)
    self.ds_low_level = ds_low_level
    place_size = int(self.process_size * place_downsample)
    if self.verbose:
      print('Placing size: {}'.format(place_size))

    self.place_size = place_size

    place_list = []
    for coords in self.tile_list:
      y, x = coords
      place_list.append([
        int(y*(1./ds_low_level)),
        int(x*(1./ds_low_level)) ])
    self.place_list = place_list


  def _read_region_args(self, coords):
    """ Returns the parameters needed to for _read_tile

    ! TODO This function appears unused

    return: y1, y2, x1, x2, level, downsample

    """

    y1, x1 = coords
    # y1 = int(y1 * self.post_load_resize)
    # x1 = int(x1 * self.post_load_resize)
    y1 = int(y1 / self.ds_load_level)
    x1 = int(x1 / self.ds_load_level)
    y2 = int(y1 + self.loading_size * self.post_load_resize)
    x2 = int(x1 + self.loading_size * self.post_load_resize)
    level = self.loading_level
    downsample = self.post_load_resize
    return y1, y2, x1, x2, level, downsample


  def _read_tile(self, coords, as_is=False):
    """ Call openslide.read_region on the slide

    passes in all the right settings: level, dimensions, etc.

    """

    y, x = coords
    size = (self.loading_size, self.loading_size)
    img = self.svs.read_region((x, y), self.loading_level, size)
    img = np.array(img)
    img = cv2.cvtColor(img, cv2.COLOR_RGBA2RGB)

    if not as_is:
      img = self.normalize_fn(img)

    # CV2 has issues with image dtype and range
    img = cv2.resize(img, dsize=(0,0), fx=self.post_load_resize,
      fy=self.post_load_resize)

    if not as_is:
      img = self.preprocess_fn(img)
    return img


  def _check_read_tile(self):
    print('Checking tile read function')
    coords = self.tile_list[0]
    try:
      ret = self._read_tile(coords)
    except:
      raise Exception('Read tile check failed')

    print('Passed read check')


  def generate_index(self):
    """ Returns an iterable of tile_list indices """
    for idx, _ in enumerate(self.tile_list):
      yield idx


  ## TODO add live skipping of white area
  def generator(self):
    """ Returns an iterable list of (image, index) pairs

    Example usage:
    ``` python
    svs = Slide(...)
    generator = svs.generator()
    img, idx = next(generator)
    ```
    """
    for idx, coords in enumerate(self.tile_list):
      img = self._read_tile(coords)
      yield img, idx


  def _find_all_tiles(self):
    """ Generate a list of foreground tiles

    1. Call get_foreground
    2. Estimate tiles, with/without overlap according to settings
    3. Reject background-only tiles
    
    """

    load_y, load_x = self.load_level_dims
    est_y = int(load_y / self.loading_size)
    est_x = int(load_x / self.loading_size)

    y_coord = np.linspace(0, load_y-self.loading_size,
      int(est_y*self.oversample_factor), dtype=np.int64)
    x_coord = np.linspace(0, load_x-self.loading_size,
      int(est_x*self.oversample_factor), dtype=np.int64)

    if self.verbose:
      print('Estimated w={} x h={} tiles'.format(est_x, est_y))
      print('With oversample ~ {}, split x={} x y={}'.format(
        self.oversample_factor, len(x_coord), len(y_coord) ))

    self.y_coord = y_coord
    self.x_coord = x_coord


  def _all_background(self):
    """ Keep and process the whole slide

    - Creates `Slide.tile_list`

    Useful for dumping slides as this will avoid the
    tile-cutting artifact at tissue edges.

    Pros: Fast start up
    Cons: Adds time downstream
    """
    if self.verbose:
      print('All background')

    yc, xc = self.y_coord, self.x_coord
    h,w = self.foreground.shape[:2]
    self.ds_tile_map = np.zeros((h, w), dtype=np.int) - 1

    idx = 0
    tile_list = []
    for yi, yy in enumerate(yc):
      for xi, xx in enumerate(xc):
        self.ds_tile_map[yi, xi] = idx
        tile_list.append([ yy*self.ds_load_level , xx*self.ds_load_level ])

    self.tile_list = tile_list


  def _fast_reject_background(self):
    """ Reject background tiles by amount of white space

    - Creates `Slide.tile_list`

    Fast background rejection based on the low-mag foreground.
    Pros: Fast
    Cons: Keeps mistakes made by morphological operations + flood filling
    """
    if self.verbose:
      print('Fast reject background')

    yc, xc = self.y_coord, self.x_coord
    foreground_ds = cv2.resize(self.foreground,
                   dsize=( len(xc), len(yc) ),
                   interpolation=cv2.INTER_NEAREST)

    tile_idx = 0
    tile_list = []
    self.ds_tile_map = np.zeros((len(yc), len(xc)), dtype=np.int)-1
    for yi, yy in enumerate(yc):
      for xi, xx in enumerate(xc):
        if foreground_ds[yi, xi]==1:
          self.ds_tile_map[yi, xi] = tile_idx
          tile_idx += 1
          tile_list.append(
            [yy*self.ds_load_level,
             xx*self.ds_load_level])

    if self.verbose:
      print('Started with {} candidate tiles'.format(len(yc)*len(xc)))
      print('Got {} foreground tiles'.format(len(tile_list)))

    self.tile_list = tile_list

  def _accurate_reject_background(self):
    """ Reject background by reading tiles and making indiviual decisions

    - Creates `Slide.tile_list`

    Read from the lowest mag ?
    First use the original method, then further refine the listing
    """
    if self.verbose:
      print('Accurate reject background method:')

    if self.background_image is None:
      self._fast_reject_background()
    else:
      print('Accurate background requested but image was supplied')
      self._image_reference_background()
      return

    new_tile_list = []
    new_ds_tile_map = np.zeros((len(self.y_coord), len(self.x_coord)), dtype=np.int)-1
    if self.verbose:
      print('Initial tile list: {}'.format(len(self.tile_list)))
      tstart = time.time()

    new_tile_idx = 0
    for tile_idx, tile in enumerate(self.tile_list):
      img = self._read_tile(tile, as_is=True)

      gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
      white = gray > self.background_threshold

      if (white.sum() / float(img.shape[0] * img.shape[1])) > self.background_pct:
        continue
      else:
        new_tile_list.append(tile)
        # Re-index the downsampled tile map
        new_ds_tile_map[self.ds_tile_map == tile_idx] = new_tile_idx
        new_tile_idx += 1

    if self.verbose:
      print('Finished in {:3.4f}s'.format(time.time() - tstart))
      print('Pruned tile list: {}'.format(len(new_tile_list)))


    self.tile_list = new_tile_list
    self.ds_tile_map = new_ds_tile_map

  def _image_reference_background(self):
    """ Reject background using a reference image

    Same as fast method except we use a different reference.
    By convention reference_img == 1 is the usable area.
    """
    if self.verbose:
      print('Image reference background:')

    # Catch case when image is requested but supplied image is none
    # Perform default background and exit.
    if self.background_image is None:
      print('Background image is none. Default to fast reject background')
      self._fast_reject_background()
      return

    # No fast background. We assume that's already been done:
    reference_img = np.copy(self.background_image)
    yc, xc = self.y_coord, self.x_coord
    reference_img = cv2.resize(reference_img, 
                   dsize=( len(xc), len(yc)), 
                   interpolation = cv2.INTER_NEAREST)
    reference_mask = reference_img > 0

    self.ds_tile_map = np.zeros((len(yc), len(xc)), dtype=np.int)-1
    tile_idx = 0
    tile_list = []
    for yi, yy in enumerate(yc):
      for xi, xx in enumerate(xc):
        if reference_mask[yi, xi] ==1:
          self.ds_tile_map[yi, xi] = tile_idx
          tile_idx += 1
          tile_list.append(
            [yy*self.ds_load_level,
             xx*self.ds_load_level])

    # Set attributes
    self.tile_list = tile_list

  def tile(self):
    self.tile_list = self._find_all_tiles()
    if self.background_speed == 'all':
      self._all_background()
    elif self.background_speed == 'fast':
      self._fast_reject_background()
    elif self.background_speed == 'accurate':
      self._accurate_reject_background()
    elif self.background_speed == 'image':
      self._image_reference_background()

    if self.verbose:
      print('{} tiles'.format(len(self.tile_list)))
      print('down sample tile map: ', self.ds_tile_map.shape, self.ds_tile_map.min(), self.ds_tile_map.max())

  # place x into location, doing whatever downsampling is needed
  def place(self, x, idx, name, mode='full', clobber=False):
    if mode=='full':
      place_coord = self.place_list[idx]
      y0, x0 = place_coord
      x1 = x0 + int(self.place_size)
      y1 = y0 + int(self.place_size)
      x = cv2.resize(x, dsize=(int(self.place_size), int(self.place_size)))
      if clobber:
        self.output_imgs[name][y0:y1, x0:x1, :] = x
      else:
        self.output_imgs[name][y0:y1, x0:x1, :] += x
    elif mode=='tile':
      location = self.ds_tile_map == idx
      self.output_imgs[name][location] = x

  def place_batch(self, xs, idxs, name, mode='full', clobber=False):
    for x , idx in zip(xs,idxs):
      self.place(x, idx, name, mode=mode, clobber=clobber)

  ## Valid probability distribution sums to 1.
  ## We can tell where the overlaps are by finding areas that sum > 1
  def get_overlapping_images(self, reference):
    ref_img = self.output_imgs[reference]
    ref_sum = np.sum(ref_img, axis=-1)

    self.twice_overlapping  = (ref_sum == 2).astype(np.uint8)
    self.thrice_overlapping = (ref_sum == 3).astype(np.uint8)
    self.quad_overlapping   = (ref_sum == 4).astype(np.uint8)
    # self.quint_overlapping  = (ref_sum == 5).astype(np.uint8)
    # print('Used {} ({}) for overlap reference'.format(reference, ref_img.shape))
    # print('Found {} areas with 2x coverage'.format(self.twice_overlapping.sum()))
    # print('Found {} areas with 3x coverage'.format(self.thrice_overlapping.sum()))
    # print('Found {} areas with 4x coverage'.format(self.quad_overlapping.sum()))
    # print('Found {} areas with 5x coverage'.format(self.quint_overlapping.sum()))

  # colorize, and write out; adjust for mismatching sizes between prob, and all other outputs
  def make_outputs(self, reference='prob'):
    inter = cv2.INTER_LINEAR
    self.get_overlapping_images(reference)
    for key, img in self.output_imgs.items():
      img_size = img.shape[:2][::-1]
      # print('Fixing overlaps in {} ({})'.format(key, img_size))
      overlap_2 = cv2.resize(self.twice_overlapping, dsize=img_size, 
                   interpolation=inter).astype(np.bool)
      overlap_3 = cv2.resize(self.thrice_overlapping, dsize=img_size, 
                   interpolation=inter).astype(np.bool)
      overlap_4 = cv2.resize(self.quad_overlapping, dsize=img_size, 
                   interpolation=inter).astype(np.bool)
      # overlap_5 = cv2.resize(self.quint_overlapping, dsize=img_size, 
      #            interpolation=inter).astype(np.bool)
      img[overlap_2] = img[overlap_2] / 2.
      img[overlap_3] = img[overlap_3] / 3.
      img[overlap_4] = img[overlap_4] / 4.
      # img[overlap_5] = img[overlap_5] / 5.
      self.output_imgs[key] = img

  def close(self):
    """ Close references to the slide and generated outputs """
    print('Closing slide')
    del self.foreground
    del self.output_imgs
    self.svs.close()


  def print_info(self):
    """ Prints info about the slide object """
    print('\n======================= SLIDE ======================')
    print('|')
    for key, val in sorted(self.__dict__.items()):
      if 'list' in key:
        print('|\t {}:\n|\t\t\tlength: {}'.format(key, len(val)))
        continue

      if type(val) is np.ndarray:
        print('|\t {}:\n|\t\t\tshape: {}'.format(key, val.shape))
        continue

      if key == 'output_imgs':
        try:
          for vk, vv in val.items():
            print('|\t {}:\n|\t\t\t{}: {}'.format(key, vk, vv.shape))
        except:
          print('|\t {}:\n|\t\t\tlen: {}'.format(key, len(val)))
        continue

      print('|\t {}:\n|\t\t\t{}'.format(key, val))
    print('|')
    print('======================= SLIDE ======================\n')
