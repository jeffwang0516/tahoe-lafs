"""
First attempt at benchmarking uploads and downloads.

TODO Parameterization (pytest?)
- Foolscap vs not foolscap
- Number of nodes
- Data size
- Number of needed/happy/total shares.
"""

from time import time, process_time
from contextlib import contextmanager

from twisted.trial.unittest import TestCase

from allmydata.util.deferredutil import async_to_deferred
from allmydata.util.consumer import MemoryConsumer
from allmydata.test.common_system import SystemTestMixin
from allmydata.immutable.upload import Data as UData


@contextmanager
def timeit(name):
    start = time()
    start_cpu = process_time()
    try:
        yield
    finally:
        print(f"{name}: {time() - start:.3f} elapsed, {process_time() - start_cpu:.3f} CPU")


class ImmutableBenchmarks(SystemTestMixin, TestCase):
    """Benchmarks for immutables."""

    # To use HTTP, change to true:
    FORCE_FOOLSCAP_FOR_STORAGE = False

    @async_to_deferred
    async def test_upload_and_download_immutables(self):
        self.basedir = self.mktemp()

        # To test larger files, change this:
        DATA = b"Some data to upload\n" * 10

        # 1 node
        await self.set_up_nodes(1)

        # 1 share
        for c in self.clients:
            c.encoding_params["k"] = 1
            c.encoding_params["happy"] = 1
            c.encoding_params["n"] = 1

        for i in range(5):
            # 1. Upload:
            with timeit("upload"):
                uploader = self.clients[0].getServiceNamed("uploader")
                results = await uploader.upload(UData(DATA, convergence=None))

            # 2. Download:
            with timeit("download"):
                uri = results.get_uri()
                node = self.clients[1].create_node_from_uri(uri)
                mc = await node.read(MemoryConsumer(), 0, None)
                self.assertEqual(b"".join(mc.chunks), DATA)
