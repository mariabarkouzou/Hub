from hub.core.meta.tensor_meta import TensorMeta
from hub.core.index.index import Index
from hub.util.keys import get_chunk_key, get_tensor_meta_key
import numpy as np

from hub.core.storage.lru_cache import LRUCache

from hub.core.chunk import Chunk

from hub.core.meta.encode.chunk_name import ChunkNameEncoder


class ChunkEngine:
    def __init__(self, key: str, cache: LRUCache):
        if not isinstance(cache, LRUCache):
            raise ValueError(f"Expected cache to be `LRUCache`. Got '{type(cache)}'.")

        self.key = key
        self.cache = cache

        # TODO: load if it already exists
        self.index_chunk_encoder = ChunkNameEncoder()

    @property
    def tensor_meta(self):
        tensor_meta_key = get_tensor_meta_key(self.key)
        return self.cache.get_cachable(tensor_meta_key, TensorMeta)

    def extend(self, array: np.array):
        if len(array.shape) < 2:
            raise ValueError(
                f"Extending requires arrays to have a minimum dimensionality of 2 (`len(shape)`). Got {len(array.shape)}."
            )

        chunk = Chunk()

        buffer = array.tobytes()
        num_samples = array.shape[0]
        sample_shape = array.shape[1:]
        extra_chunks = chunk.extend(buffer, num_samples, sample_shape)

        self.register_new_chunks(chunk, *extra_chunks)

        raise NotImplementedError

    def append(self, array: np.array):
        self.extend(np.expand_dims(array, axis=0))

    def register_new_chunks(self, *chunks: Chunk):
        for chunk in chunks:
            connected_to_next = chunk.next_chunk is not None

            self.index_chunk_encoder.attach_samples_to_last_chunk(
                chunk.num_samples, connected_to_next=connected_to_next
            )
            chunk_name = self.index_chunk_encoder.last_chunk_name
            chunk_key = get_chunk_key(self.key, chunk_name)

            self.cache[chunk_key] = chunk

    def numpy(self, index: Index, aslist: bool = False):
        raise NotImplementedError