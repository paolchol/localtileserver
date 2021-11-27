import logging
import pathlib
import threading
from typing import Union

import requests
from werkzeug.serving import make_server

from localtileserver.utilities import add_query_parameters, is_valid_palette, save_file_from_request

_LIVE_SERVERS = {}


logger = logging.getLogger(__name__)


class TileServerThread(threading.Thread):
    """This is for internal use only."""

    class ServerDownError(Exception):
        """Raised when a TileServerThread is down."""

        pass

    def __init__(self, port: int = 0, debug: bool = False, start: bool = True):
        if not isinstance(port, int):
            raise ValueError(f"Port must be an int, not {type(port)}")

        threading.Thread.__init__(self)

        from localtileserver.application import app

        if not debug:
            logging.getLogger("werkzeug").setLevel(logging.ERROR)
            logging.getLogger("gdal").setLevel(logging.ERROR)
            logging.getLogger("large_image").setLevel(logging.ERROR)
        else:
            app.config["DEBUG"] = True
            logging.getLogger("werkzeug").setLevel(logging.DEBUG)
            logging.getLogger("gdal").setLevel(logging.DEBUG)
            logging.getLogger("large_image").setLevel(logging.DEBUG)

        self.daemon = True  # CRITICAL for safe exit
        self.srv = make_server("localhost", port, app)
        self.ctx = app.app_context()
        self.ctx.push()
        if start:
            self.start()

    def run(self):
        self.srv.serve_forever()

    def shutdown(self):
        if self.is_alive():
            self.srv.shutdown()

    def __del__(self):
        self.shutdown()

    @property
    def port(self):
        return self.srv.port

    @property
    def host(self):
        return self.srv.host


def is_server_live(key: Union[int, str]):
    return key in _LIVE_SERVERS and _LIVE_SERVERS[key].is_alive()


def launch_server(port: Union[int, str] = "default", debug: bool = False):
    if is_server_live(port):
        return port
    if port == "default":
        server = TileServerThread(0, debug)
    else:
        server = TileServerThread(port, debug)
        if port == 0:
            # Get reallocated port
            port = server.port
    _LIVE_SERVERS[port] = server
    return port


def shutdown_server(key: int, force: bool = False):
    if not force and key == "default":
        # We do not shut down the default server
        return
    try:
        server = _LIVE_SERVERS.pop(key)
        server.shutdown()
        del server
    except KeyError:
        logger.error(f"Server for key ({key}) not found.")


class TileClient:
    """Serve tiles from a local raster file in a background thread.

    Parameters
    ----------
    path : pathlib.Path
        The path on disk to use as the source raster for the tiles.
    port : int
        The port on your host machine to use for the tile server. This defaults
        to getting an available port.
    debug : bool
        Run the tile server in debug mode.

    """

    def __init__(
        self,
        filename: pathlib.Path,
        port: Union[int, str] = "default",
        debug: bool = False,
    ):
        path = pathlib.Path(filename).expanduser().absolute()
        if not path.exists():
            raise OSError(f"Source file path does not exist: {path}")
        self._filename = path
        self._key = launch_server(port, debug)
        # Store actual port just in case
        self._port = _LIVE_SERVERS[self._key].srv.port

    def __del__(self):
        self.shutdown()

    @property
    def filename(self):
        return self._filename

    @property
    def server(self):
        try:
            return _LIVE_SERVERS[self._key]
        except KeyError:
            raise TileServerThread.ServerDownError("Tile server for this source has been shutdown.")

    @property
    def port(self):
        return self.server.port

    @property
    def base_url(self):
        return f"http://{self.server.host}:{self.port}"

    def shutdown(self, force: bool = False):
        shutdown_server(self._key, force=force)

    def _produce_url(self, base: str):
        return add_query_parameters(base, {"filename": self._filename})

    def create_url(self, path: str):
        return self._produce_url(f"{self.base_url}/{path.lstrip('/')}")

    def get_tile_url(
        self,
        projection: str = "EPSG:3857",
        band: int = None,
        palette: str = None,
        vmin: Union[float, int] = None,
        vmax: Union[float, int] = None,
        nodata: Union[float, int] = None,
    ):
        """

        Parameters
        ----------
        projection : str
            The Proj projection to use for the tile layer. Default is `EPSG:3857`.
        band : int
            The band of the source raster to use (default in None to show RGB if
            available). Band indexing starts at 1.
        palette : str
            The name of the color palette from `palettable` to use when plotting
            a single band. Default is greyscale.
        vmin : float
            The minimum value to use when colormapping the palette when plotting
            a single band.
        vmax : float
            The maximized value to use when colormapping the palette when plotting
            a single band.
        nodata : float
            The value from the band to use to interpret as not valid data.

        """
        # First handle query parameters to check for errors
        params = {}
        if band is not None:
            params["band"] = band
        if palette is not None:
            if not is_valid_palette(palette):
                raise ValueError(
                    f"Palette choice of {palette} is invalid. Check available palettes in the `palettable` package."
                )
            params["palette"] = palette
        if vmin is not None:
            params["min"] = vmin
        if vmax is not None:
            params["max"] = vmax
        if nodata is not None:
            params["nodata"] = nodata
        if projection is not None:
            params["projection"] = projection
        # `{z}/{x}/{y}`` is reformatted by `furl` so do this hackery with `__localtileserver_path__`
        url = add_query_parameters(self.create_url("__localtileserver_path__"), params)
        return url.replace("__localtileserver_path__", "tiles/{z}/{x}/{y}.png")

    def extract_roi(
        self,
        left: float,
        right: float,
        bottom: float,
        top: float,
        units: str = "EPSG:4326",
        encoding: str = "TILED",
        output_path: pathlib.Path = None,
    ):
        """Extract ROI in world coordinates."""
        path = f"/region/world/{left}/{right}/{bottom}/{top}/region.tif?units={units}&encoding={encoding}"
        r = requests.get(self.create_url(path))
        r.raise_for_status()
        return save_file_from_request(r, output_path)

    def extract_roi_pixel(
        self,
        left: int,
        right: int,
        bottom: int,
        top: int,
        encoding: str = "TILED",
        output_path: pathlib.Path = None,
    ):
        """Extract ROI in world coordinates."""
        path = f"/region/pixel/{left}/{right}/{bottom}/{top}/region.tif?encoding={encoding}"
        r = requests.get(self.create_url(path))
        r.raise_for_status()
        return save_file_from_request(r, output_path)

    def metadata(self):
        r = requests.get(self.create_url("/metadata"))
        r.raise_for_status()
        return r.json()

    def bounds(self, projection: str = "EPSG:4326"):
        """Get bounds in form of (ymin, ymax, xmin, xmax)."""
        r = requests.get(self.create_url(f"/bounds?projection={projection}"))
        r.raise_for_status()
        bounds = r.json()
        return (bounds["ymin"], bounds["ymax"], bounds["xmin"], bounds["xmax"])

    def center(self, projection: str = "EPSG:4326"):
        """Get center in the form of (y <lat>, x <lon>)."""
        bounds = self.bounds(projection=projection)
        return (
            (bounds[1] - bounds[0]) / 2 + bounds[0],
            (bounds[3] - bounds[2]) / 2 + bounds[2],
        )


def get_or_create_tile_client(
    source: Union[pathlib.Path, TileClient],
    port: Union[int, str] = "default",
    debug: bool = False,
):
    """A helper to safely get a TileClient from a path on disk.

    To Do
    -----
    There should eventually be a check to see if a TileClient instance exists
    for the given filename. For now, it is not really a big deal because the
    default is for all TileClient's to share a single server.

    """
    _internally_created = False
    # Launch tile server if file path is given
    if not isinstance(source, TileClient):
        source = TileClient(source, port, debug)
        _internally_created = True
    # Check that the tile source is valid and no server errors
    try:
        r = requests.get(source.create_url("metadata"))
        r.raise_for_status()
    except requests.HTTPError as e:
        # Make sure to destroy the server and its thread if internally created.
        if _internally_created:
            source.shutdown()
            del source
        raise e
    return source, _internally_created