from __future__ import division

__copyright__ = "Copyright (C) 2013 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


import numpy as np
from pytools import Record, memoize_method
import pyopencl as cl
import pyopencl.array  # noqa
from pyopencl.tools import dtype_to_c_struct
from mako.template import Template
from pytools.obj_array import make_obj_array
from boxtree.fmm import TimingFuture, TimingResult


AXIS_NAMES = ("x", "y", "z", "w")


def padded_bin(i, l):
    """Format *i* as binary number, pad it to length *l*."""
    return bin(i)[2:].rjust(l, "0")


# NOTE: Order of positional args should match GappyCopyAndMapKernel.__call__()
def realloc_array(queue, allocator, new_shape, ary, zero_fill=False, wait_for=[]):
    if zero_fill:
        array_maker = cl.array.zeros
    else:
        array_maker = cl.array.empty

    new_ary = array_maker(queue, shape=new_shape, dtype=ary.dtype,
                          allocator=allocator)

    evt = cl.enqueue_copy(queue, new_ary.data, ary.data, byte_count=ary.nbytes,
                          wait_for=wait_for + new_ary.events)

    return new_ary, evt


def reverse_index_array(indices, target_size=None, result_fill_value=None,
        queue=None):
    """For an array of *indices*, return a new array *result* that satisfies
    ``result[indices] == arange(len(indices))

    :arg target_n: The length of the result, or *None* if the result is to
        have the same length as *indices*.
    :arg result_fill_value: If not *None*, fill *result* with this value
        prior to storing reversed indices.
    """

    queue = queue or indices.queue

    if target_size is None:
        target_size = len(indices)

    result = cl.array.empty(queue, target_size, indices.dtype)

    if result_fill_value is not None:
        result.fill(result_fill_value)

    cl.array.multi_put(
            [cl.array.arange(queue, len(indices), dtype=indices.dtype,
                allocator=indices.allocator)],
            indices,
            out=[result],
            queue=queue)

    return result


# {{{ particle distribution generators

def make_normal_particle_array(queue, nparticles, dims, dtype, seed=15):
    from pyopencl.clrandom import PhiloxGenerator
    rng = PhiloxGenerator(queue.context, seed=seed)

    return make_obj_array([
        rng.normal(queue, nparticles, dtype=dtype)
        for i in range(dims)])


def make_surface_particle_array(queue, nparticles, dims, dtype, seed=15):
    import loopy as lp

    if dims == 2:
        def get_2d_knl(dtype):
            knl = lp.make_kernel(
                "{[i]: 0<=i<n}",
                """
                    <> phi = 2*M_PI/n * i
                    x[i] = 0.5* (3*cos(phi) + 2*sin(3*phi))
                    y[i] = 0.5* (1*sin(phi) + 1.5*sin(2*phi))
                    """,
                [
                    lp.GlobalArg("x,y", dtype, shape=lp.auto),
                    lp.ValueArg("n", np.int32),
                    ],
                name="make_surface_dist")

            knl = lp.split_iname(knl, "i", 128, outer_tag="g.0", inner_tag="l.0")

            return knl

        evt, result = get_2d_knl(dtype)(queue, n=nparticles)

        result = [x.ravel() for x in result]

        return make_obj_array(result)
    elif dims == 3:
        n = int(nparticles**0.5)

        def get_3d_knl(dtype):
            knl = lp.make_kernel(
                "{[i,j]: 0<=i,j<n}",
                """
                    <> phi = 2*M_PI/n * i
                    <> theta = 2*M_PI/n * j
                    x[i,j] = 5*cos(phi) * (3 + cos(theta))
                    y[i,j] = 5*sin(phi) * (3 + cos(theta))
                    z[i,j] = 5*sin(theta)
                    """,
                [
                    lp.GlobalArg("x,y,z,", dtype, shape=lp.auto),
                    lp.ValueArg("n", np.int32),
                    ])

            knl = lp.split_iname(knl, "i", 16, outer_tag="g.1", inner_tag="l.1")
            knl = lp.split_iname(knl, "j", 16, outer_tag="g.0", inner_tag="l.0")

            return knl

        evt, result = get_3d_knl(dtype)(queue, n=n)

        result = [x.ravel() for x in result]

        return make_obj_array(result)
    else:
        raise NotImplementedError


def make_uniform_particle_array(queue, nparticles, dims, dtype, seed=15):
    import loopy as lp

    if dims == 2:
        n = int(nparticles**0.5)

        def get_2d_knl(dtype):
            knl = lp.make_kernel(
                "{[i,j]: 0<=i,j<n}",
                """
                    <> xx = 4*i/(n-1)
                    <> yy = 4*j/(n-1)
                    <float64> angle = 0.3
                    <> s = sin(angle)
                    <> c = cos(angle)
                    x[i,j] = c*xx + s*yy - 2
                    y[i,j] = -s*xx + c*yy - 2
                    """,
                [
                    lp.GlobalArg("x,y", dtype, shape=lp.auto),
                    lp.ValueArg("n", np.int32),
                    ], assumptions="n>0")

            knl = lp.split_iname(knl, "i", 16, outer_tag="g.1", inner_tag="l.1")
            knl = lp.split_iname(knl, "j", 16, outer_tag="g.0", inner_tag="l.0")

            return knl

        evt, result = get_2d_knl(dtype)(queue, n=n)

        result = [x.ravel() for x in result]

        return make_obj_array(result)
    elif dims == 3:
        n = int(nparticles**(1/3))

        def get_3d_knl(dtype):
            knl = lp.make_kernel(
                "{[i,j,k]: 0<=i,j,k<n}",
                """
                    <> xx = i/(n-1)
                    <> yy = j/(n-1)
                    <> zz = k/(n-1)

                    <float64> phi = 0.3
                    <> s1 = sin(phi)
                    <> c1 = cos(phi)

                    <> xxx = c1*xx + s1*yy
                    <> yyy = -s1*xx + c1*yy
                    <> zzz = zz

                    <float64> theta = 0.7
                    <> s2 = sin(theta)
                    <> c2 = cos(theta)

                    x[i,j,k] = 4 * (c2*xxx + s2*zzz) - 2
                    y[i,j,k] = 4 * yyy - 2
                    z[i,j,k] = 4 * (-s2*xxx + c2*zzz) - 2
                    """,
                [
                    lp.GlobalArg("x,y,z", dtype, shape=lp.auto),
                    lp.ValueArg("n", np.int32),
                    ], assumptions="n>0")

            knl = lp.split_iname(knl, "j", 16, outer_tag="g.1", inner_tag="l.1")
            knl = lp.split_iname(knl, "k", 16, outer_tag="g.0", inner_tag="l.0")

            return knl

        evt, result = get_3d_knl(dtype)(queue, n=n)

        result = [x.ravel() for x in result]

        return make_obj_array(result)
    else:
        raise NotImplementedError


def make_rotated_uniform_particle_array(queue, nparticles, dims, dtype, seed=15):
    raise NotImplementedError

# }}}


def particle_array_to_host(parray):
    return np.array([x.get() for x in parray], order="F").T


# {{{ host/device data storage

class DeviceDataRecord(Record):
    """A record of array-type data. Some of this data may live in
    :class:`pyopencl.array.Array` objects. :meth:`get` can then be
    called to convert all these device arrays into :mod:`numpy.ndarray`
    instances on the host.
    """

    def _transform_arrays(self, f):
        result = {}

        def transform_val(val):
            from pyopencl.algorithm import BuiltList
            if isinstance(val, np.ndarray) and val.dtype == object:
                from pytools.obj_array import with_object_array_or_scalar
                return with_object_array_or_scalar(f, val)
            elif isinstance(val, list):
                return [transform_val(i) for i in val]
            elif isinstance(val, BuiltList):
                transformed_list = {}
                for field in val.__dict__:
                    if field != 'count' and not field.startswith('_'):
                        transformed_list[field] = f(getattr(val, field))
                return BuiltList(count=val.count, **transformed_list)
            else:
                return f(val)

        for field_name in self.__class__.fields:
            try:
                attr = getattr(self, field_name)
            except AttributeError:
                pass
            else:
                result[field_name] = transform_val(attr)

        return self.copy(**result)

    def get(self, queue, **kwargs):
        """Return a copy of `self` in which all data lives on the host, i.e.
        all :class:`pyopencl.array.Array` objects are replaced by corresponding
        :class:`numpy.ndarray` instances on the host.
        """

        def try_get(attr):
            try:
                get_meth = attr.get
            except AttributeError:
                return attr

            return get_meth(queue=queue, **kwargs)

        return self._transform_arrays(try_get)

    def with_queue(self, queue):
        """Return a copy of `self` in
        all :class:`pyopencl.array.Array` objects are assigned to
        :class:`pyopencl.CommandQueue` *queue*.
        """

        def try_with_queue(attr):
            if isinstance(attr, cl.array.Array):
                attr.finish()

            try:
                wq_meth = attr.with_queue
            except AttributeError:
                return attr

            ary = wq_meth(queue)
            return ary

        return self._transform_arrays(try_with_queue)

    def to_device(self, queue):
        """ Return a copy of `self` in all :class:`numpy.ndarray` arrays are
        transferred to device memory as :class:`pyopencl.array.Array` objects.
        """

        def _to_device(attr):
            if isinstance(attr, np.ndarray):
                return cl.array.to_device(queue, attr).with_queue(None)
            else:
                return attr

        return self._transform_arrays(_to_device)

# }}}


# {{{ type mangling

def get_type_moniker(dtype):
    return "%s%d" % (dtype.kind, dtype.itemsize)

# }}}


# {{{ gappy-copy-and-map kernel

GAPPY_COPY_TPL = Template(r"""//CL//

    typedef ${dtype_to_ctype(dtype)} value_t;

    %if from_indices:
        value_t val = input_ary[from_indices[i]];
    %else:
        value_t val = input_ary[i];
    %endif

    // Optionally, noodle values through a lookup table.
    %if map_values:
        val = value_map[val];
    %endif

    %if to_indices:
        output_ary[to_indices[i]] = val;
    %else:
        output_ary[i] = val;
    %endif

""", strict_undefined=True)


class GappyCopyAndMapKernel:
    def __init__(self, context):
        self.context = context

    @memoize_method
    def _get_kernel(self, dtype, src_index_dtype, dst_index_dtype,
                    have_src_indices, have_dst_indices, map_values):
        from pyopencl.tools import VectorArg

        args = [
                VectorArg(dtype, "input_ary", with_offset=True),
                VectorArg(dtype, "output_ary", with_offset=True),
               ]

        if have_src_indices:
            args.append(VectorArg(src_index_dtype, "from_indices", with_offset=True))

        if have_dst_indices:
            args.append(VectorArg(dst_index_dtype, "to_indices", with_offset=True))

        if map_values:
            args.append(VectorArg(dtype, "value_map", with_offset=True))

        from pyopencl.tools import dtype_to_ctype
        src = GAPPY_COPY_TPL.render(
                dtype=dtype,
                dtype_to_ctype=dtype_to_ctype,
                from_dtype=src_index_dtype,
                to_dtype=dst_index_dtype,
                from_indices=have_src_indices,
                to_indices=have_dst_indices,
                map_values=map_values)

        from pyopencl.elementwise import ElementwiseKernel
        return ElementwiseKernel(self.context,
                args, str(src),
                preamble=dtype_to_c_struct(self.context.devices[0], dtype),
                name="gappy_copy_and_map")

    # NOTE: Order of positional args should match realloc_array()
    def __call__(self, queue, allocator, new_shape, ary, src_indices=None,
                 dst_indices=None, map_values=None, zero_fill=False,
                 wait_for=None, range=None, debug=False):
        """Compresses box info arrays after empty leaf pruning and, optionally,
        maps old box IDs to new box IDs (if the array being operated on contains
        box IDs).
        """

        have_src_indices = src_indices is not None
        have_dst_indices = dst_indices is not None
        have_map_values = map_values is not None

        if not (have_src_indices or have_dst_indices):
            raise ValueError("must specify at least one of src or dest indices")

        if range is None:
            if have_src_indices and have_dst_indices:
                raise ValueError(
                    "must supply range when passing both src and dest indices")
            elif have_src_indices:
                range = slice(src_indices.shape[0])
                if debug:
                    assert int(cl.array.max(src_indices).get()) < len(ary)
            elif have_dst_indices:
                range = slice(dst_indices.shape[0])
                if debug:
                    assert int(cl.array.max(dst_indices).get()) < new_shape

        if zero_fill:
            array_maker = cl.array.zeros
        else:
            array_maker = cl.array.empty

        result = array_maker(queue, new_shape, ary.dtype, allocator=allocator)

        kernel = self._get_kernel(ary.dtype,
                                  src_indices.dtype if have_src_indices else None,
                                  dst_indices.dtype if have_dst_indices else None,
                                  have_src_indices,
                                  have_dst_indices,
                                  have_map_values)

        args = (ary, result)
        args += (src_indices,) if have_src_indices else ()
        args += (dst_indices,) if have_dst_indices else ()
        args += (map_values,) if have_map_values else ()

        evt = kernel(*args, queue=queue, range=range, wait_for=wait_for)

        return result, evt

# }}}


# {{{ map values through table

from pyopencl.elementwise import ElementwiseTemplate


MAP_VALUES_TPL = ElementwiseTemplate(
    arguments="""//CL//
        dst_value_t *dst,
        src_value_t *src,
        dst_value_t *map_values
        """,
    operation=r"""//CL//
        dst[i] = map_values[src[i]];
        """,
    name="map_values")


class MapValuesKernel(object):

    def __init__(self, context):
        self.context = context

    @memoize_method
    def _get_kernel(self, dst_dtype, src_dtype):
        type_aliases = (
            ("src_value_t", src_dtype),
            ("dst_value_t", dst_dtype)
            )

        return MAP_VALUES_TPL.build(self.context, type_aliases)

    def __call__(self, map_values, src, dst=None):
        """
        Map the entries of the array `src` through the table `map_values`.
        """
        if dst is None:
            dst = src

        kernel = self._get_kernel(dst.dtype, src.dtype)
        evt = kernel(dst, src, map_values)

        return dst, evt

# }}}


# {{{ time recording tool

class DummyTimingFuture(TimingFuture):

    @classmethod
    def from_timer(cls, timer):
        return cls(wall_elapsed=timer.wall_elapsed,
                   process_elapsed=timer.process_elapsed)

    @classmethod
    def from_op_count(cls, op_count):
        return cls(ops_elapsed=op_count)

    def __init__(self, *args, **kwargs):
        self._result = TimingResult(*args, **kwargs)

    def result(self):
        return self._result

    def done(self):
        return True


def return_timing_data(wrapped):
    """A decorator for recording timing data for a function call.

    The decorated function returns a tuple (*retval*, *timing_future*)
    where *retval* is the original return value and *timing_future*
    supports the timing data future interface in :mod:`boxtree.fmm`.
    """

    from pytools import ProcessTimer

    def wrapper(*args, **kwargs):
        timer = ProcessTimer()
        retval = wrapped(*args, **kwargs)
        timer.done()

        future = DummyTimingFuture.from_timer(timer)
        return (retval, future)

    from functools import update_wrapper
    new_wrapper = update_wrapper(wrapper, wrapped)

    return new_wrapper

# }}}


# {{{ binary search

from mako.template import Template


BINARY_SEARCH_TEMPLATE = Template("""
inline size_t bsearch(__global ${idx_t} *starts, size_t len, ${idx_t} val)
{
    size_t l_idx = 0, r_idx = len - 1, my_idx;
    for (;;)
    {
        my_idx = (l_idx + r_idx) / 2;

        if (starts[my_idx] <= val && val < starts[my_idx + 1])
        {
            return my_idx;
        }

        if (starts[my_idx] > val)
        {
            r_idx = my_idx - 1;
        }
        else
        {
            l_idx = my_idx + 1;
        }
    }
}
""")


class InlineBinarySearch(object):

    def __init__(self, idx_t):
        self.idx_t = idx_t

    @memoize_method
    def __str__(self):
        return BINARY_SEARCH_TEMPLATE.render(idx_t=self.idx_t)

# }}}


# {{{ compress a masked array into a list / list of lists


MASK_LIST_COMPRESSOR_BODY = r"""
void generate(LIST_ARG_DECL USER_ARG_DECL index_type i)
{
    if (mask[i])
    {
        APPEND_output(i);
    }
}
"""


MASK_MATRIX_COMPRESSOR_BODY = r"""
void generate(LIST_ARG_DECL USER_ARG_DECL index_type i)
{
    for (int j = 0; j < ncols; ++j)
    {
        if (mask[outer_stride * i + j * inner_stride])
        {
            APPEND_output(j);
        }
    }
}
"""


class MaskCompressorKernel(object):

    def __init__(self, context):
        self.context = context

    @memoize_method
    def get_list_compressor_kernel(self, mask_dtype, list_dtype):
        from pyopencl.algorithm import ListOfListsBuilder
        from pyopencl.tools import VectorArg

        return ListOfListsBuilder(
                self.context,
                [("output", list_dtype)],
                MASK_LIST_COMPRESSOR_BODY,
                [
                    VectorArg(mask_dtype, "mask"),
                ],
                name_prefix="compress_list")

    @memoize_method
    def get_matrix_compressor_kernel(self, mask_dtype, list_dtype):
        from pyopencl.algorithm import ListOfListsBuilder
        from pyopencl.tools import VectorArg, ScalarArg

        return ListOfListsBuilder(
                self.context,
                [("output", list_dtype)],
                MASK_MATRIX_COMPRESSOR_BODY,
                [
                    ScalarArg(np.int32, "ncols"),
                    ScalarArg(np.int32, "outer_stride"),
                    ScalarArg(np.int32, "inner_stride"),
                    VectorArg(mask_dtype, "mask"),
                ],
                name_prefix="compress_matrix")

    def __call__(self, queue, mask, list_dtype=None):
        """Convert a mask to a list in :ref:`csr` format.

        :arg mask: Either a 1D or 2D array.
            * If *mask* is 1D, it should represent a masked list, where
              *mask[i]* is true if and only if *i* is in the list.
            * If *mask* is 2D, it should represent a list of masked lists,
              so that *mask[i,j]* is true if and only if *j* is in list *i*.

        :arg list_dtype: The dtype for the output list(s). Defaults to the mask
            dtype.

        :returns: The return value depends on the type of the input.
            * If mask* is 1D, returns a tuple *(list, evt)*.
            * If *mask* is 2D, returns a tuple *(starts, lists, event)*, as a
              :ref:`csr` list.
        """
        if list_dtype is None:
            list_dtype = mask.dtype

        if len(mask.shape) == 1:
            knl = self.get_list_compressor_kernel(mask.dtype, list_dtype)
            result, evt = knl(queue, mask.shape[0], mask.data)
            return (result["output"].lists, evt)
        elif len(mask.shape) == 2:
            # FIXME: This is efficient for small column sizes but may not be
            # for larger ones since the work is partitioned by row.
            knl = self.get_matrix_compressor_kernel(mask.dtype, list_dtype)
            size = mask.dtype.itemsize
            result, evt = knl(queue, mask.shape[0], mask.shape[1],
                              mask.strides[0] // size, mask.strides[1] // size,
                              mask.data)
            return (result["output"].starts, result["output"].lists, evt)
        else:
            raise ValueError("unsupported dimensionality")

# }}}


# {{{ all-reduce

class AllReduceCommPattern(object):
    """Describes a tree-like communication pattern for allreduce. Supports efficient
    allreduce between an arbitrary number of processes.
    """

    def __init__(self, rank, size):
        """
        :arg rank: My rank
        :arg size: Total number of processors
        """
        assert 0 <= rank < size
        self.rank = rank
        self.left = 0
        self.right = size
        self.midpoint = size // 2

    def sources(self):
        """Return the set of source nodes at this communication stage. The current
        process receives messages from these processes.
        """
        if self.rank < self.midpoint:
            partner = self.midpoint + (self.rank - self.left)
            if self.rank == self.midpoint - 1 and partner == self.right:
                partners = set()
            elif self.rank == self.midpoint - 1 and partner == self.right - 2:
                partners = set([partner, partner + 1])
            else:
                partners = set([partner])
        else:
            partner = self.left + (self.rank - self.midpoint)
            if self.rank == self.right - 1 and partner == self.midpoint:
                partners = set()
            elif self.rank == self.right - 1 and partner == self.midpoint - 2:
                partners = set([partner, partner + 1])
            else:
                partners = set([partner])

        return partners

    def sinks(self):
        """Return the set of sink nodes at this communication stage. The current process
        sends a message to these processes.
        """
        if self.rank < self.midpoint:
            partner = self.midpoint + (self.rank - self.left)
            if partner == self.right:
                partner -= 1
        else:
            partner = self.left + (self.rank - self.midpoint)
            if partner == self.midpoint:
                partner -= 1

        return set([partner])

    def messages(self):
        """Return the range of relevant messages to send to the sinks.  This is returned
        as a [start, end) pair. By design, it is a consecutive range.
        """
        if self.rank < self.midpoint:
            return (self.midpoint, self.right)
        else:
            return (self.left, self.midpoint)

    def advance(self):
        """Advance to the next stage in the communication pattern.
        """
        if self.done():
            raise RuntimeError("finished communicating")

        if self.rank < self.midpoint:
            self.right = self.midpoint
            self.midpoint = (self.midpoint + self.left) // 2
        else:
            self.left = self.midpoint
            self.midpoint = (self.midpoint + self.right) // 2

    def done(self):
        """Return whether this process is finished communicating.
        """
        return self.left + 1 == self.right

# }}}


# {{{ constant one wrangler

class ConstantOneExpansionWrangler(object):
    """This implements the 'analytical routines' for a Green's function that is
    constant 1 everywhere. For 'charges' of 'ones', this should get every particle
    a copy of the particle count.

    Timing results returned by this wrangler contain the field *ops_elapsed*,
    which counts approximately the number of floating-point operations required.
    """

    def __init__(self, tree):
        self.tree = tree

    def multipole_expansion_zeros(self):
        return np.zeros(self.tree.nboxes, dtype=np.float64)

    local_expansion_zeros = multipole_expansion_zeros

    def output_zeros(self):
        return np.zeros(self.tree.ntargets, dtype=np.float64)

    def _get_source_slice(self, ibox):
        pstart = self.tree.box_source_starts[ibox]
        return slice(
                pstart, pstart + self.tree.box_source_counts_nonchild[ibox])

    def _get_target_slice(self, ibox):
        pstart = self.tree.box_target_starts[ibox]
        return slice(
                pstart, pstart + self.tree.box_target_counts_nonchild[ibox])

    def reorder_sources(self, source_array):
        return source_array[self.tree.user_source_ids]

    def reorder_potentials(self, potentials):
        return potentials[self.tree.sorted_target_ids]

    @staticmethod
    def timing_future(ops):
        return DummyTimingFuture.from_op_count(ops)

    def form_multipoles(self, level_start_source_box_nrs, source_boxes, src_weights):
        mpoles = self.multipole_expansion_zeros()
        ops = 0

        for ibox in source_boxes:
            pslice = self._get_source_slice(ibox)
            mpoles[ibox] += np.sum(src_weights[pslice])
            ops += src_weights[pslice].size

        return mpoles, self.timing_future(ops)

    def coarsen_multipoles(self, level_start_source_parent_box_nrs,
            source_parent_boxes, mpoles):
        tree = self.tree
        ops = 0

        # nlevels-1 is the last valid level index
        # nlevels-2 is the last valid level that could have children
        #
        # 3 is the last relevant source_level.
        # 2 is the last relevant target_level.
        # (because no level 1 box will be well-separated from another)
        for source_level in range(tree.nlevels-1, 2, -1):
            target_level = source_level - 1
            start, stop = level_start_source_parent_box_nrs[
                            target_level:target_level+2]
            for ibox in source_parent_boxes[start:stop]:
                for child in tree.box_child_ids[:, ibox]:
                    if child:
                        mpoles[ibox] += mpoles[child]
                        ops += 1

        return mpoles, self.timing_future(ops)

    def eval_direct(self, target_boxes, neighbor_sources_starts,
            neighbor_sources_lists, src_weights):
        pot = self.output_zeros()
        ops = 0

        for itgt_box, tgt_ibox in enumerate(target_boxes):
            tgt_pslice = self._get_target_slice(tgt_ibox)

            src_sum = 0
            nsrcs = 0
            start, end = neighbor_sources_starts[itgt_box:itgt_box+2]
            #print "DIR: %s <- %s" % (tgt_ibox, neighbor_sources_lists[start:end])
            for src_ibox in neighbor_sources_lists[start:end]:
                src_pslice = self._get_source_slice(src_ibox)
                nsrcs += src_weights[src_pslice].size

                src_sum += np.sum(src_weights[src_pslice])

            pot[tgt_pslice] = src_sum
            ops += pot[tgt_pslice].size * nsrcs

        return pot, self.timing_future(ops)

    def multipole_to_local(self,
            level_start_target_or_target_parent_box_nrs,
            target_or_target_parent_boxes,
            starts, lists, mpole_exps):
        local_exps = self.local_expansion_zeros()
        ops = 0

        for itgt_box, tgt_ibox in enumerate(target_or_target_parent_boxes):
            start, end = starts[itgt_box:itgt_box+2]

            contrib = 0
            #print tgt_ibox, "<-", lists[start:end]
            for src_ibox in lists[start:end]:
                contrib += mpole_exps[src_ibox]
                ops += 1

            local_exps[tgt_ibox] += contrib

        return local_exps, self.timing_future(ops)

    def eval_multipoles(self,
            target_boxes_by_source_level, from_sep_smaller_nonsiblings_by_level,
            mpole_exps):
        pot = self.output_zeros()
        ops = 0

        for level, ssn in enumerate(from_sep_smaller_nonsiblings_by_level):
            for itgt_box, tgt_ibox in \
                    enumerate(target_boxes_by_source_level[level]):
                tgt_pslice = self._get_target_slice(tgt_ibox)

                contrib = 0

                start, end = ssn.starts[itgt_box:itgt_box+2]
                for src_ibox in ssn.lists[start:end]:
                    contrib += mpole_exps[src_ibox]

                pot[tgt_pslice] += contrib
                ops += pot[tgt_pslice].size * (end - start)

        return pot, self.timing_future(ops)

    def form_locals(self,
            level_start_target_or_target_parent_box_nrs,
            target_or_target_parent_boxes, starts, lists, src_weights):
        local_exps = self.local_expansion_zeros()
        ops = 0

        for itgt_box, tgt_ibox in enumerate(target_or_target_parent_boxes):
            start, end = starts[itgt_box:itgt_box+2]

            #print "LIST 4", tgt_ibox, "<-", lists[start:end]
            contrib = 0
            nsrcs = 0
            for src_ibox in lists[start:end]:
                src_pslice = self._get_source_slice(src_ibox)
                nsrcs += src_weights[src_pslice].size

                contrib += np.sum(src_weights[src_pslice])

            local_exps[tgt_ibox] += contrib
            ops += nsrcs

        return local_exps, self.timing_future(ops)

    def refine_locals(self, level_start_target_or_target_parent_box_nrs,
            target_or_target_parent_boxes, local_exps):
        ops = 0

        for target_lev in range(self.tree.nlevels):
            start, stop = level_start_target_or_target_parent_box_nrs[
                    target_lev:target_lev+2]
            for ibox in target_or_target_parent_boxes[start:stop]:
                local_exps[ibox] += local_exps[self.tree.box_parent_ids[ibox]]
                ops += 1

        return local_exps, self.timing_future(ops)

    def eval_locals(self, level_start_target_box_nrs, target_boxes, local_exps):
        pot = self.output_zeros()
        ops = 0

        for ibox in target_boxes:
            tgt_pslice = self._get_target_slice(ibox)
            pot[tgt_pslice] += local_exps[ibox]
            ops += pot[tgt_pslice].size

        return pot, self.timing_future(ops)

    def finalize_potentials(self, potentials):
        return potentials

# }}}

# vim: foldmethod=marker:filetype=pyopencl
