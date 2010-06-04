# This file is part of the MapProxy project.
# Copyright (C) 2010 Omniscale <http://omniscale.de>
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Tile caching (creation, caching and retrieval of tiles).

.. classtree:: mapproxy.core.cache.CacheManager
.. classtree:: mapproxy.core.cache._TileCreator
.. classtree:: mapproxy.core.cache.TileSource

.. digraph:: Schematic Call Graph
    
    ranksep = 0.1;
    node [shape="box", height="0", width="0"] 

    tcache  [label="Cache",         href="<Cache>"];
    cm      [label="CacheManager",  href="<CacheManager>"];
    tc      [label="tile_creator_func", href="<_TileCreator>"];
    ts      [label="TileSource",    href="<TileSource>"];
    c       [label="Cache",         href="<Cache>"];

    {
        tcache -> cm [label="load_tile_coords"];
        cm -> tc [label="call"];
        tc -> cm  [label="is_cached"];
        cm -> c  [label="load\\nstore\\nis_cached"];
        tc -> ts [label="create_tiles"];
    }
    

"""

from __future__ import with_statement
import os
import sys
import time
import errno
import hashlib
from functools import partial

from mapproxy.core.utils import FileLock, cleanup_lockdir, ThreadedExecutor
from mapproxy.core.image import TiledImage, ImageSource, is_single_color_image
from mapproxy.core.config import base_config, abspath
from mapproxy.core.grid import NoTiles

import logging
log = logging.getLogger(__name__)

class BlankImage(Exception):
    pass
class TileCacheError(Exception):
    pass
class TileSourceError(TileCacheError):
    pass
class TooManyTilesError(TileCacheError):
    pass

class Cache(object):
    """
    Easy access to images from cached tiles.
    """
    def __init__(self, cache_mgr, grid, transparent=False):
        """
        :param cache_mgr: the cache manager
        :param grid: the grid of the tile cache
        """
        self.cache_mgr = cache_mgr
        self.grid = grid
        self.transparent = transparent
    
    def tile(self, tile_coord):
        """
        Return a single tile.
        
        :return: loaded tile or ``None``
        :rtype: `ImageSource` or ``None``
        """
        tiles = self.cache_mgr.load_tile_coords([tile_coord], with_metadata=True)
        if len(tiles) < 1:
            return None
        else:
            return tiles[0]
    
    def _tiles(self, tile_coords):
        return self.cache_mgr.load_tile_coords(tile_coords)
        
    
    def _tiled_image(self, req_bbox, req_srs, out_size):
        """
        Return a `TiledImage` with all tiles that are within the requested bbox,
        for the given out_size.
        
        :note: The parameters are just hints for the tile cache to load the right
               tiles. Usually the bbox and the size of the result is larger.
               The result will always be in the native srs of the cache.
               See `Cache.image`.
        
        :param req_bbox: the requested bbox
        :param req_srs: the srs of the req_bbox
        :param out_size: the target output size
        :rtype: `ImageSource`
        """
        try:
            src_bbox, tile_grid, affected_tile_coords = \
                self.grid.get_affected_tiles(req_bbox, out_size, req_srs=req_srs)
        except IndexError:
            raise TileCacheError('Invalid BBOX')
        except NoTiles:
            raise BlankImage()
        
        num_tiles = tile_grid[0] * tile_grid[1]
        if num_tiles >= base_config().cache.max_tile_limit:
            raise TooManyTilesError()

        tile_sources = [tile.source for tile in self._tiles(affected_tile_coords)]
        return TiledImage(tile_sources, src_bbox=src_bbox, src_srs=self.grid.srs,
                          tile_grid=tile_grid, tile_size=self.grid.tile_size,
                          transparent=self.transparent)
    
    def image(self, req_bbox, req_srs, out_size):
        """
        Return an image with the given bbox and size.
        The result will be cropped/transformed if needed.
        
        :param req_bbox: the requested bbox
        :param req_srs: the srs of the req_bbox
        :param out_size: the output size
        :rtype: `ImageSource`
        """
        tiled_image = self._tiled_image(req_bbox, req_srs, out_size)
        return tiled_image.transform(req_bbox, req_srs, out_size)
    
    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.cache_mgr, self.grid)

class TileCollection(object):
    def __init__(self, tile_coords):
        self.tiles = [_Tile(coord) for coord in tile_coords]
        self.tiles_dict = {}
        for tile in self.tiles:
            self.tiles_dict[tile.coord] = tile
    
    def __getitem__(self, idx_or_coord):
        if isinstance(idx_or_coord, int):
            return self.tiles[idx_or_coord]
        if idx_or_coord in self.tiles_dict:
            return self.tiles_dict[idx_or_coord]
        return _Tile(idx_or_coord)
    
    def __contains__(self, tile_or_coord):
        if isinstance(tile_or_coord, tuple):
            return tile_or_coord in self.tiles_dict
        if hasattr(tile_or_coord, 'coord'):
            return tile_or_coord.coord in self.tiles_dict
        return False
    
    def __len__(self):
        return len(self.tiles)
    
    def __iter__(self):
        return iter(self.tiles)
    
    def __call__(self, coord):
        return self[coord]

class CacheManager(object):
    """
    Manages tile cache and tile creation.
    """
    def __init__(self, cache, tile_source, tile_creator):
        self.cache = cache
        self.tile_source = tile_source
        self.tile_creator = tile_creator
        self._expire_timestamp = None
        
    def is_cached(self, tile):
        """
        Return True if the tile is cached.
        """
        if isinstance(tile, tuple):
            tile = _Tile(tile)
        max_mtime = self.expire_timestamp(tile)
        cached = self.cache.is_cached(tile)
        if cached and max_mtime is not None:
            stale = self.cache.timestamp_created(tile) < max_mtime
            if stale:
                cached = False
        return cached
    
    def expire_timestamp(self, tile=None):
        """
        Return the timestamp until which a tile should be accepted as up-to-date,
        or ``None`` if the tiles should not expire.
        
        :note: Returns _expire_timestamp by default.
        """
        return self._expire_timestamp
    
    def load_tile_coords(self, tile_coords, with_metadata=False):
        """
        Load all given tiles from cache. If they are not present, load them.
        
        :param tile_coords: list with tile coordinates (``None`` for out of bounds tiles)
        :return: list with `ImageSource` for all tiles (``None`` for out of bounds tiles)
        """
        tiles = TileCollection(tile_coords)
        self._load_tiles(tiles, with_metadata=with_metadata)
        
        return tiles
    
    def _load_tiles(self, tiles, with_metadata=False):
        """
        Return the given `tiles` with the `_Tile.source` set. If a tile is not cached,
        it will be created.
        """
        self._load_cached_tiles(tiles, with_metadata=with_metadata)
        self._create_tiles(tiles, with_metadata=with_metadata)
    
    def _create_tiles(self, tiles, with_metadata=False):
        """
        Create the tile data for all missing tiles. All created tiles will be added
        to the cache.
        
        :return: True if new tiles were created.
        """
        new_tiles = [tile for tile in tiles if tile.is_missing()]
        if new_tiles:
            created_tiles = self.tile_creator(new_tiles, tiles,
                                              self.tile_source, self)
            
            # load tile that were not created (e.g tiles created by another process)
            not_created = set(new_tiles).difference(created_tiles)
            if not_created:
                self._load_cached_tiles(not_created, with_metadata=with_metadata)
    
    def _load_cached_tiles(self, tiles, with_metadata=False):
        """
        Set the `_Tile.source` for all cached tiles.
        """
        for tile in tiles:
            if tile.is_missing() and self.is_cached(tile):
                self.cache.load(tile, with_metadata=with_metadata)
    def store_tiles(self, tiles):
        """
        Store the given tiles in the underlying cache.
        """
        for tile in tiles:
            self.cache.store(tile)
    
    def __repr__(self):
        return '%s(%r, %r, %r)' % (self.__class__.__name__, self.cache, self.tile_source,
                                   self.tile_creator)
    

class FileCache(object):
    """
    This class is responsible to store and load the actual tile data.
    """
    def __init__(self, cache_dir, file_ext, lock_dir=None, pre_store_filter=None,
                 link_single_color_images=False):
        """
        :param cache_dir: the path where the tile will be stored
        :param file_ext: the file extension that will be appended to
            each tile (e.g. 'png')
        :param pre_store_filter: a list with filter. each filter will be called
            with a tile before it will be stored to disc. the filter should 
            return this or a new tile object.
        """
        self.cache_dir = cache_dir
        if lock_dir is None:
            lock_dir = os.path.join(cache_dir, 'tile_locks')
        self.lock_dir = lock_dir
        self.file_ext = file_ext
        self._lock_cache_id = None
        if pre_store_filter is None:
            pre_store_filter = []
        self.pre_store_filter = pre_store_filter
        if link_single_color_images and sys.platform == 'win32':
            log.warn('link_single_color_images not supported on windows')
            link_single_color_images = False
        self.link_single_color_images = link_single_color_images
    
    def level_location(self, level):
        """
        Return the path where all tiles for `level` will be stored.
        
        >>> c = FileCache(cache_dir='/tmp/cache/', file_ext='png')
        >>> c.level_location(2)
        '/tmp/cache/02'
        """
        return os.path.join(self.cache_dir, "%02d" % level)
    
    def tile_location(self, tile, create_dir=False):
        """
        Return the location of the `tile`. Caches the result as ``location``
        property of the `tile`.
        
        :param tile: the tile object
        :param create_dir: if True, create all necessary directories
        :return: the full filename of the tile
         
        >>> c = FileCache(cache_dir='/tmp/cache/', file_ext='png')
        >>> c.tile_location(_Tile((3, 4, 2))).replace('\\\\', '/')
        '/tmp/cache/02/000/000/003/000/000/004.png'
        """
        if tile.location is None:
            x, y, z = tile.coord
            parts = (self.level_location(z),
                     "%03d" % int(x / 1000000),
                     "%03d" % (int(x / 1000) % 1000),
                     "%03d" % (int(x) % 1000),
                     "%03d" % int(y / 1000000),
                     "%03d" % (int(y / 1000) % 1000),
                     "%03d.%s" % (int(y) % 1000, self.file_ext))
            tile.location = os.path.join(*parts)
        if create_dir:
            _create_dir(tile.location)
        return tile.location
    
    def _single_color_tile_location(self, color, create_dir=False):
        """
        >>> c = FileCache(cache_dir='/tmp/cache/', file_ext='png')
        >>> c._single_color_tile_location((254, 0, 4)).replace('\\\\', '/')
        '/tmp/cache/single_color_tiles/fe0004.png'
        """
        parts = (
            self.cache_dir,
            'single_color_tiles',
            ''.join('%02x' % v for v in color) + '.' + self.file_ext
        )
        location = os.path.join(*parts)
        if create_dir:
            _create_dir(location)
        return location
    
    def timestamp_created(self, tile):
        """
        Return the timestamp of the last modification of the tile.
        """
        self._update_tile_metadata(tile)
        return tile.timestamp
    
    def _update_tile_metadata(self, tile):
        location = self.tile_location(tile)
        stats = os.lstat(location)
        tile.timestamp = stats.st_mtime
        tile.size = stats.st_size
    
    def is_cached(self, tile):
        """
        Returns ``True`` if the tile data is present.
        """
        if tile.is_missing():
            location = self.tile_location(tile)
            if os.path.exists(location):
                return True
            else:
                return False
        else:
            return True
    
    def load(self, tile, with_metadata=False):
        """
        Fills the `_Tile.source` of the `tile` if it is cached.
        If it is not cached or if the ``.coord`` is ``None``, nothing happens.
        """
        if not tile.is_missing():
            return True
        
        location = self.tile_location(tile)
        
        if os.path.exists(location):
            if with_metadata:
                self._update_tile_metadata(tile)
            tile.source = ImageSource(location)
            return True
        return False
        
    def store(self, tile):
        """
        Add the given `tile` to the file cache. Stores the `_Tile.source` to
        `FileCache.tile_location`.
        
        All ``pre_store_filter`` will be called with the tile, before
        it will be stored.
        """
        if tile.stored:
            return
        
        tile_loc = self.tile_location(tile, create_dir=True)
        
        if self.link_single_color_images:
            color = is_single_color_image(tile.source.as_image())
            if color:
                real_tile_loc = self._single_color_tile_location(color, create_dir=True)
                if not os.path.exists(real_tile_loc):
                    self._store(tile, real_tile_loc)
                
                log.debug('linking %r from %s to %s',
                          tile.coord, real_tile_loc, tile_loc)
                
                # remove any file before symlinking.
                # exists() returns False if it links to non-
                # existing file, islink() test to check that
                if os.path.exists(tile_loc) or os.path.islink(tile_loc):
                    os.unlink(tile_loc)
                
                os.symlink(real_tile_loc, tile_loc)
                return
        
        self._store(tile, tile_loc)
    
    def _store(self, tile, location):
        if os.path.islink(location):
            os.unlink(location)
        
        for img_filter in self.pre_store_filter:
            tile = img_filter(tile)
        data = tile.source.as_buffer()
        data.seek(0)
        with open(location, 'wb') as f:
            log.debug('writing %r to %s' % (tile.coord, location))
            f.write(data.read())
        tile.size = data.tell()
        tile.timestamp = time.time()
        data.seek(0)
        tile.stored = True
    
    def lock_filename(self, tile):
        if self._lock_cache_id is None:
            md5 = hashlib.md5()
            md5.update(self.cache_dir)
            self._lock_cache_id = md5.hexdigest()
        return os.path.join(self.lock_dir, self._lock_cache_id + '-' +
                            '-'.join(map(str, tile.coord)) + '.lck')
        
    def lock(self, tile):
        """
        Returns a lock object for this tile.
        """
        lock_filename = self.lock_filename(tile)
        return FileLock(lock_filename, timeout=base_config().http_client_timeout)
    
    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.cache_dir, self.file_ext)

def _create_dir(file_name):
    dir_name = os.path.dirname(file_name)
    if not os.path.exists(dir_name):
        try:
            os.makedirs(dir_name)
        except OSError, e:
            if e.errno != errno.EEXIST:
                raise e            
    

class _TileCreator(object):
    """
    Base class for the creation of new tiles.
    Subclasses can implement different strategies how multiple tiles should
    be created (e.g. threaded).
    """
    def __init__(self, tile_source, cache):
        self.tile_source = tile_source
        self.cache = cache
    def create_tiles(self, tiles):
        """
        Create the given tiles (`_Tile.source` will be set). Returns a list with all
        created tiles.
        
        :note: The returned list may contain more tiles than requested. This allows
               the `TileSource` to create multiple tiles in one pass. 
        """
        raise NotImplementedError()
    
    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.tile_source, self.cache)


class _SequentialTileCreator(_TileCreator):
    """
    This `_TileCreator` creates one requested tile after the other.
    """
    def create_tiles(self, tiles, tile_collection):
        created_tiles = []
        for tile in tiles:
            with self.tile_source.tile_lock(tile):
                if not self.cache.is_cached(tile):
                    new_tiles = self.tile_source.create_tile(tile, tile_collection)
                    self.cache.store_tiles(new_tiles)
                    created_tiles.extend(new_tiles)
        cleanup_lockdir(self.tile_source.lock_dir)
        return created_tiles

def sequential_tile_creator(tiles, tile_collection, tile_source, cache):
    """
    This tile creator creates a thread pool to create multiple tiles in parallel.
    """
    return _SequentialTileCreator(tile_source, cache).create_tiles(tiles, tile_collection)

class _ThreadedTileCreator(_TileCreator):
    """
    This `_TileCreator` creates one requested tile after the other.
    """
    def create_tiles(self, tiles, tile_collection):
        unique_meta_tiles, _ = self._sort_tiles(tiles)
        if len(unique_meta_tiles) == 1: # don't start thread pool for one tile
            new_tiles = self._create_tile(unique_meta_tiles[0], tile_collection)
            if new_tiles is None:
                return []
            cleanup_lockdir(self.tile_source.lock_dir)
            return new_tiles
        else:
            return self._create_multiple_tiles(unique_meta_tiles, tile_collection)
    
    def _create_multiple_tiles(self, tiles, tile_collection):
        pool_size = base_config().tile_creator_pool_size
        pool = ThreadedExecutor(partial(self._create_tile, 
                                        tile_collection=tile_collection), 
                                pool_size=pool_size)
        new_tiles = pool.execute(tiles)
        result = []
        for value in new_tiles:
            if value is not None:
                result.extend(value)
        
        cleanup_lockdir(self.tile_source.lock_dir)
        return result
    
    def _create_tile(self, tile, tile_collection):
        with self.tile_source.tile_lock(tile):
            if not self.cache.is_cached(tile):
                new_tiles = self.tile_source.create_tile(tile, tile_collection)
                self.cache.store_tiles(new_tiles)
                return new_tiles
    
    def _sort_tiles(self, tiles):
        unique_meta_tiles = {}
        other_tiles = []
    
        for tile in tiles:
            lock_name = self.tile_source.lock_filename(tile)
            if lock_name in unique_meta_tiles:
                other_tiles.append(tile)
            else:
                unique_meta_tiles[lock_name] = tile
        
        return unique_meta_tiles.values(), other_tiles
    
def threaded_tile_creator(tiles, tile_collection, tile_source, cache):
    """
    This tile creator creates a thread pool to create multiple tiles in parallel.
    """
    return _ThreadedTileCreator(tile_source, cache).create_tiles(tiles, tile_collection)


class TileSource(object):
    """
    Base class for tile sources.
    A ``TileSource`` knows how to get the `_Tile.source` for a given tile.
    """
    def __init__(self, lock_dir=None):
        if lock_dir is None:
            lock_dir = abspath(base_config().cache.lock_dir)
        self.lock_dir = lock_dir
        self._id = None
        
    def id(self):
        """
        Returns a unique but constant id of this TileSource used for locking.
        """
        raise NotImplementedError
    
    def tile_lock(self, tile):
        """
        Returns a lock object for the given tile.
        """
        lock_file = self.lock_filename(tile)
        # TODO use own configuration option for lock timeout
        return FileLock(lock_file, timeout=base_config().http_client_timeout)
    
    def lock_filename(self, tile):
        if self._id is None:
            md5 = hashlib.md5()
            md5.update(str(self.id()))
            self._id = md5.hexdigest()
        return os.path.join(self.lock_dir, self._id + '-' +
                                           '-'.join(map(str, tile.coord)) + '.lck')
    
    def create_tile(self, tile, tile_map):
        """
        Create the given tile and set the `_Tile.source`. It doesn't store the data on
        disk (or else where), this is up to the cache manager.
        
        :note: This method may return multiple tiles, if it is more effective for the
               ``TileSource`` to create multiple tiles in one pass.
        :rtype: list of ``Tiles``
        
        """
        raise NotImplementedError()
    
    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__)


class _Tile(object):
    """
    Internal data object for all tiles. Stores the tile-``coord`` and the tile data.
    
    :ivar source: the data of this tile
    :type source: ImageSource
    """
    def __init__(self, coord, source=None):
        self.coord = coord
        self.source = source
        self.location = None
        self.stored = False
        self.size = None
        self.timestamp = None
    
    def source_buffer(self, *args, **kw):
        if self.source is not None:
            return self.source.as_buffer(*args, **kw)
        else:
            return None
    
    def source_image(self, *args, **kw):
        if self.source is not None:
            return self.source.as_image(*args, **kw)
        else:
            return None
    
    def is_missing(self):
        """
        Returns ``True`` when the tile has no ``data``, except when the ``coord``
        is ``None``. It doesn't check if the tile exists.
        
        >>> _Tile((1, 2, 3)).is_missing()
        True
        >>> _Tile((1, 2, 3), './tmp/foo').is_missing()
        False
        >>> _Tile(None).is_missing()
        False
        """
        if self.coord is None:
            return False
        return self.source is None
    
    def __eq__(self, other):
        """
        >>> _Tile((0, 0, 1)) == _Tile((0, 0, 1))
        True
        >>> _Tile((0, 0, 1)) == _Tile((1, 0, 1))
        False
        >>> _Tile((0, 0, 1)) == None
        False
        """
        if isinstance(other, _Tile):
            return  (self.coord == other.coord and
                     self.source == other.source)
        else:
            return NotImplemented
    def __ne__(self, other):
        """
        >>> _Tile((0, 0, 1)) != _Tile((0, 0, 1))
        False
        >>> _Tile((0, 0, 1)) != _Tile((1, 0, 1))
        True
        >>> _Tile((0, 0, 1)) != None
        True
        """
        equal_result = self.__eq__(other)
        if equal_result is NotImplemented:
            return NotImplemented
        else:
            return not equal_result
    
    def __repr__(self):
        return '_Tile(%r, source=%r)' % (self.coord, self.source)

Tile = _Tile
from mapproxy.core.grid import MetaGrid
from mapproxy.core.image import TileSplitter, ImageTransformer
from mapproxy.core.client import HTTPClient, HTTPClientError

from mapproxy.core.utils import reraise_exception

class MapQuery(object):
    """
    Internal query for a map with a specific extend, size, srs, etc.
    """
    def __init__(self, bbox, size, srs, format=None, transparent=False):
        self.bbox = bbox
        self.size = size
        self.srs = srs
        self.format = format
        self.transparent = transparent
        

class MapLayer(object):
    def get_map(self, query):
        raise NotImplementedError

class ResolutionConditional(MapLayer):
    def __init__(self, one, two, resolution, srs):
        self.one = one
        self.two = two
        self.resolution = resolution
        self.srs = srs
    
    def get_map(self, query):
        bbox = query.bbox
        if query.srs != self.srs:
            bbox = query.srs.transform_bbox_to(self.srs, bbox)
        
        xres = (bbox[2] - bbox[0]) / query.size[0]
        yres = (bbox[3] - bbox[1]) / query.size[1]
        res = min(xres, yres)
        if res > self.resolution:
            return self.one.get_map(query)
        else:
            return self.two.get_map(query)

class SRSConditional(MapLayer):
    PROJECTED = 'PROJECTED'
    GEOGRAPHIC = 'GEOGRAPHIC'
    
    def __init__(self, layers):
        # TODO geographic/projected fallback
        self.srs_map = {}
        for layer, srss in layers:
            for srs in srss:
                self.srs_map[srs] = layer
    
    def get_map(self, query):
        layer = self._select_layer(query.srs)
        return self.layer.get_map(query)
    
    def _select_layer(self, query_srs):
        # srs exists
        if query_srs in self.srs_map:
            return self.srs_map[query_srs]
        
        # srs_type exists
        srs_type = self.GEOGRAPHIC if query_srs.is_latlong else self.PROJECTED
        if srs_type in self.srs_map:
            return self.srs_map[srs_type]
        
        # first with same type
        is_latlong = query_srs.is_latlong
        for srs in self.srs_map:
            if hasattr(srs, 'is_latlong') and srs.is_latlong == is_latlong:
                return self.srs_map[srs]
        
        # return first
        return self.srs_map.itervalues().next()
        

class DirectMapLayer(MapLayer):
    def __init__(self, source):
        self.source = source
    
    def get_map(self, query):
        return self.source.get(query)
    
class CacheMapLayer(MapLayer):
    def __init__(self, tile_manager, transparent=False):
        self.tile_manager = tile_manager
        self.grid = tile_manager.grid
        self.transparent = transparent
    
    def get_map(self, query):
        tiled_image = self._tiled_image(query.bbox, query.size, query.srs)
        return tiled_image.transform(query.bbox, query.srs, query.size)
    
    def _tiled_image(self, bbox, size, srs):
        try:
            src_bbox, tile_grid, affected_tile_coords = \
                self.grid.get_affected_tiles(bbox, size, req_srs=srs)
        except IndexError:
            raise TileCacheError('Invalid BBOX')
        except NoTiles:
            raise BlankImage()
        
        num_tiles = tile_grid[0] * tile_grid[1]
        if num_tiles >= base_config().cache.max_tile_limit:
            raise TooManyTilesError()
        
        tile_sources = [tile.source for tile in self.tile_manager.load_tile_coords(affected_tile_coords)]
        return TiledImage(tile_sources, src_bbox=src_bbox, src_srs=self.grid.srs,
                          tile_grid=tile_grid, tile_size=self.grid.tile_size,
                          transparent=self.transparent)
    

class TileManager(object):
    def __init__(self, grid, file_cache, sources, format,
        meta_buffer=None, meta_size=None):
        self.grid = grid
        self.file_cache = file_cache
        self.meta_grid = None
        self.format = format
        assert len(sources) == 1
        self.sources = sources
        
        if meta_buffer and meta_size is not None and \
            any(source.supports_meta_tiles for source in sources):
            self.meta_grid = MetaGrid(grid, meta_size=meta_size, meta_buffer=meta_buffer)
        
    def load_tile_coords(self, tile_coords):
        tiles = TileCollection(tile_coords)
        uncached_tiles = []
        for tile in tiles:
            # TODO cache eviction
            if self.file_cache.is_cached(tile):
                self.file_cache.load(tile)
            else:
                uncached_tiles.append(tile)
        
        if uncached_tiles:
            created_tiles = self._create_tiles(uncached_tiles)
            for created_tile in created_tiles:
                if created_tile.coord in tiles:
                    tiles[created_tile.coord].source = created_tile.source
        
        return tiles
    
    def _create_tiles(self, tiles):
        created_tiles = []
        if not self.meta_grid:
            for tile in tiles:
                created_tiles.append(self._create_tile(tile))
        else:
            meta_tiles = []
            meta_bboxes = set()
            for tile in tiles:
                meta_bbox = self.meta_grid.meta_bbox(tile.coord)
                if meta_bbox not in meta_bboxes:
                    meta_tiles.append((tile, meta_bbox))
                    meta_bboxes.add(meta_bbox)
            
            created_tiles = self._create_meta_tiles(meta_tiles)
        
        return created_tiles
            
    def _create_tile(self, tile):
        assert len(self.sources) == 1
        tile_bbox = self.grid.tile_bbox(tile.coord)
        query = MapQuery(tile_bbox, self.grid.tile_size, self.grid.srs, self.format)
        with self.file_cache.lock(tile):
            if not self.file_cache.is_cached(tile):
                try:
                    tile.source = self.sources[0].get(query)
                except HTTPClientError, e:
                    reraise_exception(TileSourceError(e.args[0]), sys.exc_info())
                self.file_cache.store(tile)
            else:
                self.file_cache.load(tile)
        return tile
    
    def _create_meta_tiles(self, meta_tiles):
        assert len(self.sources) == 1
        created_tiles = []
        for tile, meta_bbox in meta_tiles:
            tiles = list(self.meta_grid.tiles(tile.coord))
            main_tile = Tile(tiles[0][0]) # use first tile of meta grid
            created_tiles.extend(self._create_meta_tile(main_tile, meta_bbox, tiles))
        return created_tiles
    
    def _create_meta_tile(self, main_tile, meta_bbox, tiles):
        tile_size = self.meta_grid.tile_size(main_tile.coord[2])
        query = MapQuery(meta_bbox, tile_size, self.grid.srs, self.format)
        with self.file_cache.lock(main_tile):
            if not self.file_cache.is_cached(main_tile):
                try:
                    meta_tile = self.sources[0].get(query)
                except HTTPClientError, e:
                    reraise_exception(TileSourceError(e.args[0]), sys.exc_info())
                splitted_tiles = split_meta_tiles(meta_tile, tiles, tile_size)
                for splitted_tile in splitted_tiles:
                    self.file_cache.store(splitted_tile)
                return splitted_tiles
        # else
        tiles = [Tile(coord) for coord, pos in tiles]
        for tile in tiles:
            self.file_cache.load(tile)
        return tiles

def split_meta_tiles(meta_tile, tiles, tile_size):
        try:
            # TODO png8
            # if not self.transparent and format == 'png':
            #     format = 'png8'
            splitter = TileSplitter(meta_tile)
        except IOError, e:
            # TODO
            raise
        split_tiles = []
        for tile in tiles:
            tile_coord, crop_coord = tile
            data = splitter.get_tile(crop_coord, tile_size)
            new_tile = Tile(tile_coord)
            new_tile.source = data
            split_tiles.append(new_tile)
        return split_tiles

class InvalidSourceQuery(ValueError):
    pass

class Source(object):
    supports_meta_tiles = False
    def get(self, query):
        raise NotImplementedError

class WMSClient(object):
    def __init__(self, request_template, http_client=None, supported_srs=None):
        self.request_template = request_template
        self.http_client = http_client or HTTPClient()
        self.supported_srs = set(supported_srs or [])
    
    def get(self, query):
        if self.supported_srs and query.srs not in self.supported_srs:
            return self._get_transformed(query)
        resp = self._retrieve(query)
        return ImageSource(resp, self.request_template.params.format)
    
    def _get_transformed(self, query):
        dst_srs = query.srs
        src_srs = self._best_supported_srs(dst_srs)
        dst_bbox = query.bbox
        src_bbox = dst_srs.transform_bbox_to(src_srs, dst_bbox)
        
        src_query = MapQuery(src_bbox, query.size, src_srs)
        resp = self._retrieve(src_query)
        
        img = ImageSource(resp, self.request_template.params.format, size=src_query.size)
        
        img = ImageTransformer(src_srs, dst_srs).transform(img, src_bbox, 
            query.size, dst_bbox)
        
        img.format = self.request_template.params.format
        return img
    
    def _best_supported_srs(self, srs):
        latlong = srs.is_latlong
        
        for srs in self.supported_srs:
            if srs.is_latlong == latlong:
                return srs
        
        return iter(self.supported_srs).next()
    def _retrieve(self, query):
        url = self._query_url(query)
        return self.http_client.open(url)
    
    def _query_url(self, query):
        req = self.request_template.copy()
        req.params.bbox = query.bbox
        req.params.size = query.size
        req.params.srs = query.srs.srs_code
        
        return req.complete_url

class WMSSource(Source):
    supports_meta_tiles = True
    def __init__(self, client):
        Source.__init__(self)
        self.client = client
    
    def get(self, query):
        return self.client.get(query)
    
class TiledSource(Source):
    def __init__(self, grid, client):
        self.grid = grid
        self.client = client
    
    def get(self, query):
        if self.grid.tile_size != query.size:
            raise InvalidSourceQuery()
        if self.grid.srs != query.srs:
            raise InvalidSourceQuery()
        
        _bbox, grid, tiles = self.grid.get_affected_tiles(query.bbox, query.size)
        
        if grid != (1, 1):
            raise InvalidSourceQuery('bbox does not align to tile')
        
        
        return self.client.get_tile(tiles.next())
    