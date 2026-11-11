"""Bounded HDF5 image I/O, without decoding or changing encoded payloads."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import h5py
import numpy as np

from pipeline.shared.core import DomainError, file_hash

# Keep only a small window of encoded images in memory, not an entire episode.
IMAGE_BATCH_FRAMES = 32
_STORAGE = ContextVar("prepared_image_storage", default="materialized")


@contextmanager
def image_storage(mode):
    if mode not in {"materialized", "reference"}:
        raise ValueError("Unknown prepared image storage")
    token = _STORAGE.set(mode)
    try:
        yield
    finally:
        _STORAGE.reset(token)


def write_images(output, key, source, indices=None):
    """Preserve the aligned schema, optionally mapping bytes without copying.

    Upload readers must still reject virtual/external datasets. Only trusted
    prepare code creates these mappings; the coordinator revalidates their
    hashes before and after conversion, before exposing any output dataset.
    """
    selected = np.arange(len(source)) if indices is None else np.asarray(indices)
    if _STORAGE.get() == "materialized":
        dataset = output.create_dataset(
            key, shape=(len(selected),), dtype=h5py.vlen_dtype(np.dtype("uint8"))
        )
        copy_images(source, dataset, indices)
        return dataset
    if selected.ndim != 1 or not np.issubdtype(selected.dtype, np.integer):
        raise DomainError("图像引用索引无效")
    if len(selected) and (selected.min() < 0 or selected.max() >= len(source)):
        raise DomainError("图像引用索引越界")
    path = Path(source.file.filename).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file():
        raise DomainError("图像引用必须指向原始普通文件")
    if source.is_virtual or source.external:
        raise DomainError("图像引用源不得再次引用外部数据")
    shape = (len(selected), *source.shape[1:])
    layout = h5py.VirtualLayout(shape=shape, dtype=source.dtype)
    virtual = h5py.VirtualSource(str(path), source.name, shape=source.shape)
    begin = 0
    for end in range(1, len(selected) + 1):
        if end < len(selected) and selected[end] == selected[end - 1] + 1:
            continue
        layout[begin:end] = virtual[int(selected[begin]) : int(selected[end - 1]) + 1]
        begin = end
    dataset = output.create_virtual_dataset(key, layout)
    if "image_source_sha256" not in output.attrs:
        output.attrs["image_source_path"] = str(path)
        output.attrs["image_source_sha256"] = file_hash(path)
    elif output.attrs["image_source_path"] != str(path):
        raise DomainError("单个对齐产物不得混合不同图像源")
    output.attrs["image_storage"] = "reference_v1"
    return dataset


def verify_image_references(root, expected_sources=None):
    """HDF5 silently fills missing VDS sources; explicitly fail closed instead."""
    checked = {}
    for path in Path(root).glob("*.hdf5"):
        with h5py.File(path, "r") as handle:
            if handle.attrs.get("image_storage") != "reference_v1":
                continue
            source = Path(handle.attrs["image_source_path"])
            expected = handle.attrs["image_source_sha256"]
            if (
                expected_sources is not None
                and expected_sources.get(source) != expected
            ):
                raise DomainError("引用图像源不属于已核验任务快照")
            if (
                any(part.is_symlink() for part in (source, *source.parents))
                or not source.is_file()
            ):
                raise DomainError("引用图像源缺失或不是普通文件")
            if source not in checked:
                checked[source] = file_hash(source)
            if checked[source] != expected:
                raise DomainError("引用图像源摘要变化；拒绝发布转换结果")
            for key in handle:
                dataset = handle[key]
                if isinstance(dataset, h5py.Dataset) and dataset.is_virtual:
                    for mapping in dataset.virtual_sources():
                        if Path(mapping.file_name) != source:
                            raise DomainError("虚拟图像映射与来源快照不一致")


def inspect_images(dataset, inspect_frame, indices=None):
    """Inspect every frame in order, decoding outside h5py's global lock.

    Only encoded NumPy rows cross into threads, never HDF5 handles. Limit both
    queued payloads and decoder concurrency; keep errors in source-frame order.
    """
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    inherited = os.environ.get("SLURM_CPUS_PER_TASK")
    if inherited and inherited.isdecimal() and int(inherited) > 0:
        cpus = min(cpus, int(inherited))
    workers = max(1, min(3, cpus))
    if workers == 1:
        for _, rows in image_batches(dataset, indices):
            yield from map(inspect_frame, rows)
        return
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="image-check"
    ) as pool:
        for _, rows in image_batches(dataset, indices):
            yield from pool.map(inspect_frame, rows)


def image_batches(dataset, indices=None):
    """Yield output offsets and encoded rows, preserving repeats and order.

    HDF5 fancy indexing requires sorted, unique indices. Read each selected
    source row once per batch, then apply the inverse map in NumPy. Nearby
    selections use a hyperslab instead of expensive HDF5 point selections.
    Both fixed-width padded byte arrays and variable-length arrays are valid.
    """
    count = len(dataset) if indices is None else len(indices)
    for start in range(0, count, IMAGE_BATCH_FRAMES):
        stop = min(count, start + IMAGE_BATCH_FRAMES)
        if indices is None:
            yield start, dataset[start:stop]
            continue
        selected, inverse = np.unique(indices[start:stop], return_inverse=True)
        lower, upper = int(selected[0]), int(selected[-1]) + 1
        if upper - lower <= IMAGE_BATCH_FRAMES * 2:
            rows = dataset[lower:upper][selected - lower]
        else:
            rows = dataset[selected.tolist()]
        yield start, rows[inverse]


def copy_images(source, destination, indices=None):
    """Copy exact encoded bytes using bounded batch reads and writes."""
    for start, rows in image_batches(source, indices):
        if rows.ndim == 1:
            converted = np.empty(len(rows), dtype=object)
            for index, row in enumerate(rows):
                converted[index] = (
                    np.asarray(row, dtype=np.uint8)
                    if isinstance(row, np.ndarray)
                    else np.frombuffer(bytes(row), dtype=np.uint8)
                )
            rows = converted
            if len({len(row) for row in rows}) == 1:
                # h5py misinterprets equal-length object arrays as a 2-D object
                # matrix. A uint8 matrix also handles one-row batches.
                rows = np.stack(rows)
        else:
            rows = np.asarray(rows, dtype=np.uint8)
        destination[start : start + len(rows)] = rows
