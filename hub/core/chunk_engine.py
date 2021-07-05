from hub.core.compression import decompress_array
from math import ceil
from typing import Optional, Sequence, Union, Tuple
from hub.util.exceptions import (
    CorruptedMetaError,
    DynamicTensorNumpyError,
)
from hub.core.meta.tensor_meta import TensorMeta
from hub.core.index.index import Index
from hub.util.keys import (
    get_chunk_key,
    get_chunk_id_encoder_key,
    get_tensor_meta_key,
)
from hub.core.sample import Sample
from hub.constants import DEFAULT_MAX_CHUNK_SIZE, UNCOMPRESSED

import numpy as np

from hub.core.storage.lru_cache import LRUCache

from hub.core.chunk import Chunk

from hub.core.meta.encode.chunk_id import ChunkIdEncoder


SampleValue = Union[np.ndarray, int, float, bool, Sample]


def is_uniform_sequence(samples):
    if len(set(map(type, samples))) != 1:
        # Cannot vectorize sequence with inconsistent types
        return False
    elif any(isinstance(s, np.ndarray) for s in samples):
        # Numpy arrays will only be vectorized if they have the same shape
        return len(set(s.shape for s in samples)) == 1
    elif any(isinstance(s, Sample) for s in samples):
        # Sample objects will not be vectorized
        return False
    else:
        # Scalar samples can be vectorized
        return True


class ChunkEngine:
    def __init__(
        self, key: str, cache: LRUCache, max_chunk_size: int = DEFAULT_MAX_CHUNK_SIZE
    ):
        """Handles creating `Chunk`s and filling them with incoming samples.

        Data delegation:
            All samples must live inside a chunk. No chunks may contain partial samples, only 1 chunk per sample.
            A chunk holds the dynamic information for the samples they contain (like shape and byte ranges).
            For more information on the `Chunk` format, check out the `Chunk` class.

        ChunkIdEncoder:
            The `ChunkIdEncoder` is used to keep track of # TODO: finish docstring

        Args:
            key (str): Tensor key.
            cache (LRUCache): Cache for which chunks and the meta information are stored.
            max_chunk_size (int): Chunks generated by this instance will never exceed this size. Defaults to DEFAULT_MAX_CHUNK_SIZE.

        Raises:
            ValueError: If invalid max chunk size.
        """

        # TODO: elaborate further in docstring (including our encoders)
        # TODO: add examples

        self.key = key
        self.cache = cache

        if max_chunk_size <= 2:
            raise ValueError("Max chunk size should be > 2 bytes.")

        self.max_chunk_size = max_chunk_size
        self.min_chunk_size_target = self.max_chunk_size // 2

    @property
    def chunk_id_encoder(self) -> ChunkIdEncoder:
        """Gets the chunk id encoder from cache, if one is not found it creates a blank encoder.
        For more information on what `ChunkIdEncoder` is used for, see the `__init__` docstring.

        Raises:
            CorruptedMetaError: If chunk id encoding was corrupted.

        Returns:
            ChunkIdEncoder: The chunk ID encoder handles the mapping between sample indices
                and their corresponding chunks.
        """

        key = get_chunk_id_encoder_key(self.key)
        if not self.chunk_id_encoder_exists:

            # 1 because we always update the meta information before writing the samples (to account for potentially corrupted data in the future)
            if self.tensor_meta.length > 1:
                raise CorruptedMetaError(
                    f"Tensor length is {self.tensor_meta.length}, but could not find the chunk id encoder."
                )

            enc = ChunkIdEncoder()
            self.cache[key] = enc
            return enc

        enc = self.cache.get_cachable(key, ChunkIdEncoder)
        return enc

    @property
    def chunk_id_encoder_exists(self) -> bool:
        return get_chunk_id_encoder_key(self.key) in self.cache

    @property
    def num_chunks(self) -> int:
        if not self.chunk_id_encoder_exists:
            return 0
        return self.chunk_id_encoder.num_chunks

    @property
    def num_samples(self) -> int:
        if not self.chunk_id_encoder_exists:
            return 0
        return self.chunk_id_encoder.num_samples

    @property
    def last_chunk(self) -> Optional[Chunk]:
        if self.num_chunks == 0:
            return None

        last_chunk_name = self.chunk_id_encoder.get_name_for_chunk(-1)
        last_chunk_key = get_chunk_key(self.key, last_chunk_name)
        return self.cache.get_cachable(last_chunk_key, Chunk)

    @property
    def tensor_meta(self):
        tensor_meta_key = get_tensor_meta_key(self.key)
        return self.cache.get_cachable(tensor_meta_key, TensorMeta)

    def _append_bytes(self, buffer: memoryview, shape: Tuple[int], dtype: np.dtype):
        """Treat `buffer` as a single sample and place them into `Chunk`s. This function implements the algorithm for
        determining which chunks contain which parts of `buffer`.

        Args:
            buffer (memoryview): Buffer that represents a single sample that may or may not be compressed.
            shape (Tuple[int]): Shape for the sample that `buffer` represents.
            dtype (np.dtype): Data type for the sample that `buffer` represents.
        """

        # TODO mention that buffer could be of length 0 (further, we should add a check that the shape should contain a 0 if the length is 0)

        # num samples is always 1 when appending
        num_samples = 1

        # update tensor meta first because erroneous meta information is better than un-accounted for data.
        self.tensor_meta.check_compatibility(shape, dtype)
        self.tensor_meta.update(shape, dtype, num_samples)

        successfully_appended_to_last_chunk = self._try_appending_to_last_chunk(buffer)
        if not successfully_appended_to_last_chunk:
            self._append_to_new_chunk(buffer)

        self._synchronize_last_chunk(num_samples, len(buffer), shape)

    def _try_appending_to_last_chunk(self, buffer: memoryview) -> bool:
        """Will store `buffer` inside of the last chunk if it can.
        It can be stored in the last chunk if it exists and has space for `buffer`.

        Args:
            buffer (memoryview): Data to store. This can represent any number of samples.

        Returns:
            bool: True if `buffer` was successfully written to the last chunk, otherwise False.
        """

        last_chunk = self.last_chunk
        if last_chunk is None:
            return False

        incoming_num_bytes = len(buffer)

        if last_chunk.is_under_min_space(self.min_chunk_size_target):
            last_chunk_size = last_chunk.num_data_bytes
            chunk_ct_content = _min_chunk_ct_for_data_size(
                self.max_chunk_size, incoming_num_bytes
            )

            extra_bytes = min(incoming_num_bytes, self.max_chunk_size - last_chunk_size)
            combined_chunk_ct = _min_chunk_ct_for_data_size(
                self.max_chunk_size, incoming_num_bytes + last_chunk_size
            )

            # combine if count is same
            if combined_chunk_ct == chunk_ct_content:
                last_chunk.append_sample(buffer[:extra_bytes], self.max_chunk_size)
                return True

        return False

    def _append_to_new_chunk(self, buffer: memoryview):
        """Will create a new chunk and store `buffer` inside of it. Assumes that `buffer`'s length is < max chunk size.
        This should be called if `buffer` could not be added to the last chunk.

        Args:
            buffer (memoryview): Data to store. This can represent any number of samples.
        """

        # check if `last_chunk_extended` to handle empty samples
        new_chunk = self._create_new_chunk()
        new_chunk.append_sample(buffer, self.max_chunk_size)

    def _synchronize_last_chunk(
        self, num_new_samples: int, num_new_bytes: int, shape: Tuple[int]
    ):
        """For the last chunk, registers samples with the chunk ID encoder and updates the headers.
        This should be called every time new sample(s) get put into the last chunk.

        Args:
            num_new_samples (int): Samples that have already been added to the last chunk.
            num_new_bytes (int): The length of the buffer added to the last chunk.
            shape (Tuple[int]): Shape of the samples (requires all samples to have the same shape for this sync).
        """

        self.chunk_id_encoder.register_samples_to_last_chunk_id(num_new_samples)
        self.last_chunk.update_headers(num_new_bytes, num_new_samples, shape)

    def _create_new_chunk(self):
        """Creates and returns a new `Chunk`. Automatically creates an ID for it and puts a reference in the cache."""

        chunk_id = self.chunk_id_encoder.generate_chunk_id()
        chunk = Chunk()
        chunk_name = ChunkIdEncoder.name_from_id(chunk_id)
        chunk_key = get_chunk_key(self.key, chunk_name)
        self.cache[chunk_key] = chunk
        return chunk

    def extend(self, samples: Union[np.ndarray, Sequence[SampleValue]]):
        """Formats a batch of `samples` and feeds them into the chunking algorithm."""

        if isinstance(samples, np.ndarray):
            compression = self.tensor_meta.sample_compression
            if compression == UNCOMPRESSED:
                buffers = []

                # before adding any data, we need to check all sample sizes
                for sample in samples:
                    buffer = memoryview(sample.tobytes())
                    self._check_sample_size(len(buffer))
                    buffers.append(buffer)

                for buffer in buffers:
                    self._append_bytes(buffer, sample.shape, sample.dtype)
            else:
                sample_objects = []
                compression = self.tensor_meta.sample_compression

                # before adding any data, we need to check all sample sizes
                for sample in samples:
                    sample_object = Sample(array=sample)
                    sample_objects.append(sample_object)
                    num_bytes = len(sample_object.compressed_bytes(compression))
                    self._check_sample_size(num_bytes)

                for sample_object in sample_objects:
                    self.append(sample_object)

        elif isinstance(samples, Sequence):
            if is_uniform_sequence(samples):
                self.extend(np.array(samples))
            else:
                for sample in samples:
                    self.append(sample)
        else:
            raise TypeError(f"Unsupported type for extending. Got: {type(samples)}")

        self.cache.maybe_flush()

    def append(self, sample: SampleValue):
        """Formats a single `sample` (compresseses/decompresses if applicable) and feeds it into the chunking algorithm."""

        if isinstance(sample, Sample):
            # has to decompress to read the array's shape and dtype
            # might be able to optimize this away
            compression = self.tensor_meta.sample_compression
            data = memoryview(sample.compressed_bytes(compression))
            self._check_sample_size(len(data))
            self._append_bytes(data, sample.shape, sample.dtype)
        else:
            return self.append(Sample(array=np.array(sample)))

        self.cache.maybe_flush()

    def numpy(
        self, index: Index, aslist: bool = False
    ) -> Union[np.ndarray, Sequence[np.ndarray]]:
        """Reads samples from chunks and returns as numpy arrays. If `aslist=True`, returns a sequence of numpy arrays."""

        length = self.num_samples
        enc = self.chunk_id_encoder
        last_shape = None
        samples = []

        for global_sample_index in index.values[0].indices(length):
            chunk_id = enc[global_sample_index]
            chunk_name = ChunkIdEncoder.name_from_id(chunk_id)
            chunk_key = get_chunk_key(self.key, chunk_name)
            chunk = self.cache.get_cachable(chunk_key, Chunk)
            sample = self.read_sample_from_chunk(global_sample_index, chunk)
            shape = sample.shape

            if not aslist and last_shape is not None:
                if shape != last_shape:
                    raise DynamicTensorNumpyError(self.key, index, "shape")

            samples.append(sample)
            last_shape = shape

        return _format_samples(samples, index, aslist)

    def read_sample_from_chunk(
        self, global_sample_index: int, chunk: Chunk
    ) -> np.ndarray:
        """Read a sample from a chunk, converts the global index into a local index. Handles decompressing if applicable."""

        tensor_meta = self.tensor_meta
        expect_compressed = tensor_meta.sample_compression != UNCOMPRESSED
        dtype = tensor_meta.dtype

        enc = self.chunk_id_encoder

        buffer = chunk.memoryview_data
        local_sample_index = enc.get_local_sample_index(global_sample_index)
        shape = chunk.shapes_encoder[local_sample_index]
        sb, eb = chunk.byte_positions_encoder[local_sample_index]

        buffer = buffer[sb:eb]
        if expect_compressed:
            sample = decompress_array(buffer, shape)
        else:
            sample = np.frombuffer(buffer, dtype=dtype).reshape(shape)

        return sample

    def _check_sample_size(self, num_bytes: int):
        if num_bytes > self.min_chunk_size_target:
            msg = f"Sorry, samples that exceed minimum chunk size ({self.min_chunk_size_target} bytes) are not supported yet (coming soon!). Got: {num_bytes} bytes."

            if self.tensor_meta.sample_compression == UNCOMPRESSED:
                msg += "\nYour data is actually uncompressed, so setting the `sample_compression` variable in `Datset.create_tensor` could help here!"

            raise NotImplementedError(msg)


def _format_samples(
    samples: Sequence[np.array], index: Index, aslist: bool
) -> Union[np.ndarray, Sequence[np.ndarray]]:
    """Helper function for preparing `samples` read from the chunk engine in the way the format the user expects."""

    samples = index.apply(samples)

    if aslist and all(map(np.isscalar, samples)):
        samples = list(arr.item() for arr in samples)

    samples = index.apply_squeeze(samples)

    if aslist:
        return samples
    else:
        return np.array(samples)


def _min_chunk_ct_for_data_size(chunk_max_data_bytes: int, size: int) -> int:
    """Calculates the minimum number of chunks in which data of given size can be fit."""
    return ceil(size / chunk_max_data_bytes)
