"""LoadType, LoadStats and LayerInfo.
"""
import logging
from enum import Enum

from ....components.experimental.monitor import monitor
from ....layers.base import Layer
from ....utils.config import octree_config
from ._request import ChunkRequest
from ._utils import LayerRef, StatWindow

LOGGER = logging.getLogger("napari.async")


class LoadCounts:
    """Count statistics about loaded chunks."""

    def __init__(self):
        self.loads: int = 0
        self.chunks: int = 0
        self.bytes: int = 0


def _mbits(num_bytes, duration_ms) -> float:
    """Return Mbit/s."""
    mbits = (num_bytes * 8) / (1024 * 1024)
    seconds = duration_ms / 1000
    if seconds == 0:
        return 0
    return mbits / seconds


class LoadType(Enum):
    """How ChunkLoader should load this layer."""

    AUTO = 0  # Decide based on load speed.
    SYNC = 1  # Always load synchronously.
    ASYNC = 2  # Always load asynchronously.


class LoadInfo:
    """Info about loading one ChunkRequest.

    Parameters
    ----------
    num_bytes : int
        How many bytes were loaded.
    duration_ms : float
        How long did the load take in milliseconds.
    sync : bool
        True if the load was synchronous.
    """

    def __init__(self, num_bytes: int, duration_ms: float, sync: bool):
        self.num_bytes = num_bytes
        self.duration_ms = duration_ms
        self.sync = sync

    @property
    def mbits(self) -> float:
        """Return Mbits/second."""
        return _mbits(self.num_bytes, self.duration_ms)


class LoadStats:
    """Statistics about the recent loads for one layer.

    Attributes
    ----------
    window_ms : StatWindow
        Keeps track of the average load time over the window.
    """

    WINDOW_SIZE = 10  # Only keeps stats for this many loads.

    NUM_RECENT_LOADS = 10  # Save details on this many recent loads.

    def __init__(self):
        self.window_ms: StatWindow = StatWindow(self.WINDOW_SIZE)
        self.window_bytes: StatWindow = StatWindow(self.WINDOW_SIZE)
        self.recent_loads: list = []
        self.counts: LoadCounts = LoadCounts()

    def on_load_finished(self, request: ChunkRequest, sync: bool) -> None:
        """Record stats on this request that was just loaded.

        Parameters
        ----------
        request : ChunkRequest
            The request that was just loaded.
        sync : bool
            True if the load was synchronous.
        """
        try:
            # Use the special "ChunkRequest.load_chunks" timer to time
            # the loading of all the chunks in this request combine.
            load_ms = request.timers['ChunkRequest.load_chunks'].duration_ms

            # Update our StatWindow.
            self.window_ms.add(load_ms)
        except KeyError:
            pass  # there was no 'load_chunks" timer...

        # Record the number of loads and chunks.
        self.counts.loads += 1
        self.counts.chunks += request.num_chunks

        # Increment total bytes loaded.
        num_bytes = request.num_bytes
        self.counts.bytes += num_bytes

        # Time to load all chunks.
        load_ms = request.timers['ChunkRequest.load_chunks'].duration_ms

        # Update our StatWindows.
        self.window_bytes.add(num_bytes)
        self.window_ms.add(load_ms)

        # Add LoadInfo, keep only NUM_RECENT_LOADS of them.
        load_info = LoadInfo(num_bytes, load_ms, sync=sync)
        keep = self.NUM_RECENT_LOADS - 1
        self.recent_loads = self.recent_loads[-keep:] + [load_info]

        if monitor:
            # Send stats about this one load.
            monitor.send_message(
                {"load": {"num_bytes": num_bytes, "load_ms": load_ms}}
            )

    @property
    def mbits(self) -> float:
        """Return Mbit/second."""
        return _mbits(self.window_bytes.average, self.window_ms.average)

    @property
    def recent_load_str(self) -> str:
        """Return string describing the sync/async nature of recent loads.

        Returns
        -------
        str
            Return "sync", "async" or "mixed".
        """
        num_sync = num_async = 0
        for load in self.recent_loads:
            if load.sync:
                num_sync += 1
            else:
                num_async += 1

        if num_async == 0:
            return "sync"
        if num_sync == 0:
            return "async"
        return "mixed"


class LayerInfo:
    """Information about one layer the ChunkLoader is tracking.

    Parameters
    ----------
    layer : Layer
        The layer we are loading chunks for.

    Attributes
    ----------
    layer_id : int
        The id of the layer.
    layer_ref : weakref
        Weak reference to the layer.
    load_type : LoadType
        Enum for whether to do auto/sync/async loads.
    auto_sync_ms : int
        If load takes longer than this many milliseconds make it async.
    stats : LoadStats
        Statistics related the loads.

    Notes
    -----
    We store a weak reference because we do not want an in-progress request
    to prevent a layer from being deleted. Meanwhile, once a request has
    finished, we can de-reference the weakref to make sure the layer was
    note deleted during the load process.
    """

    def __init__(self, layer_ref: LayerRef):
        self.layer_ref = layer_ref
        self.load_type: LoadType = LoadType.AUTO
        self.auto_sync_ms = octree_config['loader']['auto_sync_ms']

        self.stats = LoadStats()

    def get_layer(self) -> Layer:
        """Resolve our weakref to get the layer.

        Returns
        -------
        layer : Layer
            The layer for this ChunkRequest.
        """
        layer = self.layer_ref.weak_ref()
        if layer is None:
            LOGGER.debug(
                "LayerInfo.get_layer: layer %d was deleted", self.layer_id
            )
        return layer

    @property
    def loads_fast(self) -> bool:
        """Return True if this layer has been loading very fast."""
        average = self.stats.window_ms.average
        return average is not None and average <= self.auto_sync_ms
