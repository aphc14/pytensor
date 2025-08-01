import logging
import sys
import warnings
from collections.abc import Callable, Iterable, Sequence
from itertools import chain, groupby
from typing import cast, overload

import numpy as np

import pytensor
from pytensor import scalar as ps
from pytensor.configdefaults import config
from pytensor.gradient import DisconnectedType
from pytensor.graph.basic import Apply, Constant, Variable
from pytensor.graph.op import Op
from pytensor.graph.replace import _vectorize_node
from pytensor.graph.type import Type
from pytensor.graph.utils import MethodNotDefined
from pytensor.link.c.op import COp
from pytensor.link.c.params_type import ParamsType
from pytensor.npy_2_compat import numpy_version, using_numpy_2
from pytensor.printing import Printer, pprint, set_precedence
from pytensor.scalar.basic import ScalarConstant, ScalarVariable
from pytensor.tensor import (
    TensorLike,
    _get_vector_length,
    as_tensor_variable,
    get_vector_length,
)
from pytensor.tensor.basic import (
    ScalarFromTensor,
    alloc,
    get_scalar_constant_value,
    nonzero,
)
from pytensor.tensor.basic import (
    constant as tensor_constant,
)
from pytensor.tensor.blockwise import vectorize_node_fallback
from pytensor.tensor.elemwise import DimShuffle
from pytensor.tensor.exceptions import AdvancedIndexingError, NotScalarConstantError
from pytensor.tensor.math import clip
from pytensor.tensor.shape import Reshape, Shape_i, specify_broadcastable
from pytensor.tensor.type import (
    TensorType,
    bscalar,
    complex_dtypes,
    cscalar,
    discrete_dtypes,
    dscalar,
    fscalar,
    integer_dtypes,
    iscalar,
    lscalar,
    tensor,
    ubscalar,
    uiscalar,
    ulscalar,
    uwscalar,
    wscalar,
    zscalar,
)
from pytensor.tensor.type_other import (
    MakeSlice,
    NoneConst,
    NoneTypeT,
    SliceConstant,
    SliceType,
    make_slice,
)
from pytensor.tensor.variable import TensorConstant, TensorVariable


_logger = logging.getLogger("pytensor.tensor.subtensor")

invalid_scal_types = (ps.float64, ps.float32, ps.float16)
scal_types = (
    ps.int64,
    ps.int32,
    ps.int16,
    ps.int8,
    ps.uint64,
    ps.uint32,
    ps.uint16,
    ps.uint8,
)
tensor_types = (
    lscalar,
    iscalar,
    wscalar,
    bscalar,
    ulscalar,
    uiscalar,
    uwscalar,
    ubscalar,
)
invalid_tensor_types = (
    fscalar,
    dscalar,
    cscalar,
    zscalar,
)


def indices_from_subtensor(
    op_indices: Iterable[ScalarConstant],
    idx_list: list[Type | slice | Variable] | None,
) -> tuple[slice | Variable, ...]:
    """Recreate the index tuple from which a ``*Subtensor**`` ``Op`` was created.

    Parameters
    ==========
    op_indices
        The flattened indices obtained from ``x.inputs``, when ``x`` is a
        ``*Subtensor*`` node.
    idx_list
        The values describing the types of each dimension's index.  This is
        obtained from ``op.idx_list``, when ``op`` is a ``*Subtensor*``
        ``Op``.

    Example
    =======
        array, *op_indices = subtensor_node.inputs
        idx_list = getattr(subtensor_node.op, "idx_list", None)
        indices = indices_from_subtensor(op_indices, idx_list)

    """

    def convert_indices(indices, entry):
        """Reconstruct ``*Subtensor*`` index input parameter entries."""
        if indices and isinstance(entry, Type):
            rval = indices.pop(0)
            return rval
        elif isinstance(entry, slice):
            return slice(
                convert_indices(indices, entry.start),
                convert_indices(indices, entry.stop),
                convert_indices(indices, entry.step),
            )
        else:
            return entry

    op_indices = list(op_indices)

    return (
        tuple(convert_indices(op_indices, idx) for idx in idx_list)
        if idx_list
        else tuple(op_indices)
    )


def as_index_constant(
    a: slice | int | np.integer | Variable | None | TensorLike,
) -> Variable | slice | None:
    r"""Convert Python literals to PyTensor constants--when possible--in `Subtensor` arguments.

    This will leave `Variable`\s untouched.
    """
    if a is None:
        return a
    elif isinstance(a, slice):
        return slice(
            as_index_constant(a.start),
            as_index_constant(a.stop),
            as_index_constant(a.step),
        )
    elif isinstance(a, int | np.integer):
        return ps.ScalarConstant(ps.int64, a)
    elif isinstance(a, Variable):
        return a
    return as_tensor_variable(a)


@overload
def as_index_literal(idx: int | np.integer) -> int | np.integer: ...


@overload
def as_index_literal(idx: None) -> None: ...


@overload
def as_index_literal(idx: slice | SliceConstant) -> slice: ...


@overload
def as_index_literal(idx: ScalarConstant | TensorConstant) -> int | np.integer: ...


@overload
def as_index_literal(idx: Variable): ...


def as_index_literal(
    idx: None
    | int
    | np.integer
    | slice
    | SliceConstant
    | ScalarConstant
    | TensorConstant
    | Variable,
) -> int | np.integer | slice | None:
    """Convert a symbolic index element to its Python equivalent.

    This is like the inverse of `as_index_constant`

    Raises
    ------
    NotScalarConstantError
    """
    if idx is None or isinstance(idx, int | np.integer):
        return idx

    if isinstance(idx, slice):
        return slice(
            as_index_literal(idx.start),
            as_index_literal(idx.stop),
            as_index_literal(idx.step),
        )

    if not isinstance(idx, Variable):
        raise TypeError(f"Not an index element: {idx}")

    if isinstance(idx.type, NoneTypeT):
        return None

    if isinstance(idx, ScalarConstant):
        return cast(int, idx.data)

    if (
        isinstance(idx.type, ps.ScalarType)
        and idx.owner
        and isinstance(idx.owner.op, ScalarFromTensor)
    ):
        return cast(int | np.integer, as_index_literal(idx.owner.inputs[0]))

    if isinstance(idx, TensorConstant):
        return cast(int, idx.data.item())

    if isinstance(idx, SliceConstant):
        return cast(slice, idx.data)

    if isinstance(idx.type, SliceType):
        assert idx.owner is not None
        return slice(*map(as_index_literal, idx.owner.inputs))

    # Other kinds of variables are not supported
    raise NotScalarConstantError()


def get_idx_list(inputs, idx_list):
    return indices_from_subtensor(inputs[1:], idx_list)


@overload
def get_canonical_form_slice(
    theslice: slice,
    length: int | np.integer | ScalarVariable | TensorVariable,
) -> tuple[slice, int | TensorVariable]: ...


@overload
def get_canonical_form_slice(
    theslice: int | np.integer | ScalarVariable | TensorVariable,
    length: int | np.integer | ScalarVariable | TensorVariable,
) -> tuple[TensorVariable, int]: ...


def get_canonical_form_slice(
    theslice: slice | int | np.integer | ScalarVariable | TensorVariable,
    length: int | np.integer | ScalarVariable | TensorVariable,
) -> tuple[slice | TensorVariable, int | TensorVariable]:
    """Convert indices or slices to canonical form.

    Scalar integer indices or python Slices with Scalar/None attributes
    used in basic Subtensor Ops are supported.
    Symbolic slices (of SliceType) or vector indices
    used in advanced Subtensor Ops are not supported.

    Given a slice [start:stop:step] transform it into a canonical form
    that respects the conventions imposed by python and numpy.

    In a canonical form a slice is represented by a canonical form slice,
    in which 0 <= start <= stop <= length and step > 0, and a flag which says
    if the resulting set of numbers needs to be reversed or not.

    Given a scalar index `idx` that may or not be negative, convert it to
    a certainly positive form `idx if idx >= 0 else length + idx`.

    Returns
    -------
    slc
        Canonical form slice or scalar variable.
    direction
        Direction to iterate the resulting elements in. (-1 or 1). May be symbolic.
    """
    from pytensor.tensor import ge, lt, sign, switch

    def undo_scalarization(x):
        """Undo scalarization of a variable.

        PyTensor Basic index operations use ScalarVariables for the indices/slice arguments.
        But reasoning symbolically about the result of multiple indexing operations, we usually
        want to work on TensorVariables, since rewrites work on those and not ScalarVariables.

        This function undoes ScalarFromTensor operation or converts ScalarConstants to TensorConstants.
        """
        if isinstance(x, ScalarVariable):
            if isinstance(x, ScalarConstant):
                return tensor_constant(x.data, dtype=x.dtype)
            elif x.owner is not None and isinstance(x.owner.op, ScalarFromTensor):
                return x.owner.inputs[0]
            else:
                return as_tensor_variable(x)
        return x

    def analyze(x):
        try:
            x_constant = as_index_literal(x)
            is_constant = True
        except NotScalarConstantError:
            x_constant = undo_scalarization(x)
            is_constant = False
        return x_constant, is_constant

    length, is_length_constant = analyze(length)

    # Other non-slice types are the scalar indexing case
    if not isinstance(theslice, slice):
        if not (
            isinstance(theslice, int | np.integer | ScalarVariable)
            or (isinstance(theslice, TensorVariable) and theslice.ndim == 0)
        ):
            raise ValueError(f"Slice {theslice} is not a supported slice type.")

        idx, is_index_constant = analyze(theslice)
        if is_index_constant:
            if idx >= 0:
                return idx, 1
            else:
                return idx + length, 1
        else:
            return switch(lt(idx, 0), idx + length, idx), 1

    # At this point we have a slice object. Possibly with symbolic inputs.
    start, is_start_constant = analyze(theslice.start)
    stop, is_stop_constant = analyze(theslice.stop)
    step, is_step_constant = analyze(theslice.step)

    if (
        is_start_constant
        and is_stop_constant
        and is_step_constant
        and is_length_constant
    ):
        assert isinstance(length, int | np.integer)
        _start, _stop, _step = slice(start, stop, step).indices(length)
        if _start <= _stop and _step >= 1:
            return slice(_start, _stop, _step), 1

    if step is None:
        step = 1
        is_step_constant = True

    # First handle the easier and common case where `step` is 1 and
    # either `start` or `stop` is a range boundary. More specializations
    # could be added later. This makes the resulting graph smaller than
    # in the generic case below.
    if step == 1:
        is_start_0 = (
            start is None
            or start == 0
            or (
                is_start_constant
                and is_length_constant
                and start < 0
                and start + length <= 0
            )
        )
        is_stop_length = (
            stop is None
            or stop in [length, sys.maxsize]
            or (is_stop_constant and is_length_constant and stop >= length)
        )
        if is_start_0:
            # 0:stop:1
            if is_stop_length:
                # Full slice.
                return slice(0, length, 1), 1
            if is_stop_constant and stop >= 0:
                return (slice(0, switch(lt(stop, length), stop, length), 1), 1)
            stop_plus_len = stop + length
            stop = switch(
                lt(stop, 0),
                # stop < 0
                switch(
                    lt(stop_plus_len, 0),
                    # stop + len < 0
                    0,
                    # stop + len >= 0
                    stop_plus_len,
                ),
                # stop >= 0: use min(stop, length)
                switch(lt(stop, length), stop, length),
            )
            return slice(0, stop, 1), 1
        elif is_stop_length:
            # start:length:1
            if is_start_constant and start >= 0:
                return slice(switch(lt(start, length), start, length), length, 1), 1
            start_plus_len = start + length
            start = switch(
                lt(start, 0),
                # start < 0
                switch(
                    lt(start_plus_len, 0),
                    # start + len < 0
                    0,
                    # start + len >= 0
                    start_plus_len,
                ),
                # start >= 0: use min(start, length)
                switch(lt(start, length), start, length),
            )
            return slice(start, length, 1), 1

    # This is the generic case.

    if is_step_constant:
        # When we know the sign of `step`, the graph can be made simpler.
        assert step != 0
        if step > 0:

            def switch_neg_step(a, b):
                return b

            abs_step = step
            sgn_step = 1
        else:

            def switch_neg_step(a, b):
                return a

            abs_step = -step
            sgn_step = -1
    else:
        is_step_neg = lt(step, 0)

        def switch_neg_step(a, b):
            return switch(is_step_neg, a, b)

        abs_step = abs(step)
        sgn_step = sign(step)

    defstart = switch_neg_step(length - 1, 0)
    defstop = switch_neg_step(-1, length)
    if start is None:
        start = defstart
    else:
        start = switch(lt(start, 0), start + length, start)
        start = switch(lt(start, 0), switch_neg_step(-1, 0), start)
        start = switch(ge(start, length), switch_neg_step(length - 1, length), start)
    if stop is None or stop == sys.maxsize:
        # The special "maxsize" case is probably not needed here,
        # as slices containing maxsize are not generated by
        # __getslice__ anymore.
        stop = defstop
    else:
        stop = switch(lt(stop, 0), stop + length, stop)
        stop = switch(lt(stop, 0), -1, stop)
        stop = switch(ge(stop, length), length, stop)

    nw_stop = switch_neg_step(start + 1, stop)
    slice_len = (start - stop - 1) // abs_step + 1
    slice_len = switch(lt(slice_len, 0), 0, slice_len)
    neg_start = nw_stop - (slice_len - 1) * abs_step - 1
    neg_start = switch(lt(neg_start, 0), (nw_stop - 1), neg_start)
    nw_start = switch_neg_step(neg_start, start)
    nw_start = switch(lt(nw_start, 0), 0, nw_start)
    nw_stop = switch(lt(nw_stop, 0), 0, nw_stop)
    # Ensure start <= stop.
    nw_start = switch(lt(nw_start, nw_stop), nw_start, nw_stop)

    nw_step = abs_step
    if step != 1:
        reverse = sgn_step
        return slice(nw_start, nw_stop, nw_step), reverse
    else:
        return slice(nw_start, nw_stop, nw_step), 1


def range_len(slc):
    """Length of a `range` object.

    Adapted from CPython.

    """
    from pytensor.tensor import and_, gt, lt, switch

    start, stop, step = tuple(
        as_index_constant(a) for a in [slc.start, slc.stop, slc.step]
    )
    return switch(
        and_(gt(step, 0), lt(start, stop)),
        1 + (stop - 1 - start) // step,
        switch(
            and_(lt(step, 0), gt(start, stop)),
            1 + (start - 1 - stop) // (-step),
            ps.ScalarConstant(ps.int64, 0),
        ),
    )


def slice_len(slc, n):
    """Compute the length of a slice for an array of a given length.

    We're essentially computing `len(range(*slc.indices(n)))`.

    """
    # TODO: Do we need to do this or should we expect `slc` to
    # already be canonicalized?
    canon_slc, _ = get_canonical_form_slice(slc, n)
    return range_len(canon_slc)


def is_basic_idx(idx):
    """Determine if an index is of the NumPy basic type.

    XXX: This only checks a single index, so an integer is *not* considered a
    basic index, because--depending on the other indices its used with--an
    integer can indicate advanced indexing.

    """
    return isinstance(idx, slice | type(None)) or isinstance(
        getattr(idx, "type", None), SliceType | NoneTypeT
    )


def basic_shape(shape, indices):
    r"""Computes the shape resulting from basic NumPy indexing.

    Basic indices are either ``slice``\s or ``None``\s.  ``Ellipsis`` are not
    supported here; convert them to ``slice``\s first.

    Parameters
    ----------
    shape: Tuple[int, ...]
        The shape of the array being indexed
    indices: Sequence[Or[slice, NoneType]]
        A sequence of basic indices used to index an array.

    """
    res_shape = ()
    for n, idx in zip(shape[: len(indices)], indices, strict=True):
        if isinstance(idx, slice):
            res_shape += (slice_len(idx, n),)
        elif isinstance(getattr(idx, "type", None), SliceType):
            if idx.owner is None:
                if not isinstance(idx, Constant):
                    # This is an input slice, we can't reason symbolically on it.
                    # We don't even know if we will get None entries or integers
                    res_shape += (None,)
                    continue
                else:
                    sl: slice = idx.data
                    slice_inputs = (sl.start, sl.stop, sl.step)
            elif isinstance(idx.owner.op, MakeSlice):
                slice_inputs = idx.owner.inputs
            else:
                raise ValueError(f"Unexpected Slice producing Op {idx.owner.op}")
            res_shape += (slice_len(slice(*slice_inputs), n),)
        elif idx is None:
            res_shape += (ps.ScalarConstant(ps.int64, 1),)
        elif isinstance(getattr(idx, "type", None), NoneTypeT):
            res_shape += (ps.ScalarConstant(ps.int64, 1),)
        else:
            raise ValueError(f"Invalid index type: {idx}")
    return res_shape


def group_indices(indices):
    """Group indices sequentially by whether or not they're basic or advanced.

    Returns
    -------
    Tuple[Boolean, List[Tuple[Integer, Any]]]
        The boolean indicates whether or not the group is a set of basic
        indices.  The list contains the contiguous set of indices paired with their
        corresponding dimension number in the array being indexed.
    """
    idx_groups = []
    dim_num = -1
    for basic, grp_indices in groupby(indices, key=is_basic_idx):
        enum_grp_indices = []
        for idx in grp_indices:
            # We "zip" the dimension number to each index, which means we can't
            # count indices that add new axes
            if (idx is not None) and not isinstance(
                getattr(idx, "type", None), NoneTypeT
            ):
                dim_num += 1

            enum_grp_indices.append((dim_num, idx))

        idx_groups.append((basic, enum_grp_indices))

    return idx_groups


def _non_consecutive_adv_indexing(indices) -> bool:
    """Check if the advanced indexing is non-consecutive (i.e., split by basic indexing)."""
    idx_groups = group_indices(indices)
    # This means that there are at least two groups of advanced indexing separated by basic indexing
    return len(idx_groups) > 3 or (len(idx_groups) == 3 and not idx_groups[0][0])


def indexed_result_shape(array_shape, indices, indices_are_shapes=False):
    """Compute the symbolic shape resulting from `a[indices]` for `a.shape == array_shape`.

    This function uses NumPy's basic and advanced indexing logic.  It can also
    handle combinations of advanced and basic indices.

    Parameters
    ----------
    array_shape: Tuple[Variable, ...]
        Shape of the array being indexed.
    indices: Sequence[Union[TensorVariable, Tuple[Union[None, slice, Variable], ...]]]
        Either the indices themselves or the shapes of each index--depending
        on the value of `indices_are_shapes`.
    indices_are_shapes: bool (Optional)
        Indicates whether or not the `indices` contains shape tuples instead of
        the actual index arrays.  If you use this approach, make sure that the
        broadcastable dimensions are (scalar) constants with the value `1`, or `1`
        exactly.
    """
    res_shape = ()

    remaining_dims = range(pytensor.tensor.basic.get_vector_length(array_shape))
    idx_groups = group_indices(indices)

    if _non_consecutive_adv_indexing(indices):
        # In this case NumPy places the advanced index groups in the front of the array
        # https://numpy.org/devdocs/user/basics.indexing.html#combining-advanced-and-basic-indexing
        idx_groups = sorted(idx_groups, key=lambda x: x[0])
        idx_groups = groupby(
            chain.from_iterable(d_idx for _, d_idx in idx_groups),
            key=lambda x: is_basic_idx(x[1]),
        )

    for basic, grp_dim_indices in idx_groups:
        dim_nums, grp_indices = zip(*grp_dim_indices, strict=True)
        remaining_dims = tuple(dim for dim in remaining_dims if dim not in dim_nums)

        if basic:
            grp_shapes = tuple(array_shape[dim] for dim in dim_nums)
            res_shape += basic_shape(grp_shapes, grp_indices)
        else:
            from pytensor.tensor.extra_ops import broadcast_shape

            res_shape += broadcast_shape(
                *grp_indices,
                arrays_are_shapes=indices_are_shapes,
                # The AdvancedIndexing Op relies on the Numpy implementation which allows runtime broadcasting.
                # As long as that is true, the shape inference has to respect that this is not an error.
                allow_runtime_broadcast=True,
            )

    res_shape += tuple(array_shape[dim] for dim in remaining_dims)

    return res_shape


def get_slice_elements(
    idxs: Sequence,
    cond: Callable = lambda x: isinstance(x, Variable),
) -> list:
    """Extract slice elements conditional on a given predicate function.

    Parameters
    ----------
    idxs : a list of indices or slices.
    cond : a callable that returns a bool

    Returns
    -------
    list
        idxs, with the slices flattened out into a list.
        If cond is true for an entry, does not flatten it.

    """
    ret = []

    def helper(entry):
        if cond(entry):
            ret.append(entry)
        elif isinstance(entry, slice):
            helper(entry.start)
            helper(entry.stop)
            helper(entry.step)

    for idx in idxs:
        helper(idx)

    return ret


def index_vars_to_types(entry, slice_ok=True):
    r"""Change references to `Variable`s into references to `Type`s.

    The `Subtensor.idx_list` field is unique to each `Subtensor` instance.  It
    is not unique to each `Apply` node, so it should not refer to specific
    `Variable`s.

    TODO WRITEME: This function also accepts an `entry` already being a `Type`;
    when would that happen?

    """
    if (
        isinstance(entry, np.ndarray | Variable)
        and hasattr(entry, "dtype")
        and entry.dtype == "bool"
    ):
        raise AdvancedIndexingError("Invalid index type or slice for Subtensor")

    if isinstance(entry, Variable) and (
        entry.type in invalid_scal_types or entry.type in invalid_tensor_types
    ):
        raise TypeError("Expected an integer")

    if isinstance(entry, Variable) and entry.type in scal_types:
        return entry.type
    elif isinstance(entry, Type) and entry in scal_types:
        return entry

    if (
        isinstance(entry, Variable)
        and entry.type in tensor_types
        and all(entry.type.broadcastable)
    ):
        return ps.get_scalar_type(entry.type.dtype)
    elif isinstance(entry, Type) and entry in tensor_types and all(entry.broadcastable):
        return ps.get_scalar_type(entry.dtype)
    elif slice_ok and isinstance(entry, slice):
        a = entry.start
        b = entry.stop
        c = entry.step

        if a is not None:
            slice_a = index_vars_to_types(a, False)
        else:
            slice_a = None

        if b is not None and b != sys.maxsize:
            # The special "maxsize" case is probably not needed here,
            # as slices containing maxsize are not generated by
            # __getslice__ anymore.
            slice_b = index_vars_to_types(b, False)
        else:
            slice_b = None

        if c is not None:
            slice_c = index_vars_to_types(c, False)
        else:
            slice_c = None

        return slice(slice_a, slice_b, slice_c)
    elif isinstance(entry, int | np.integer):
        raise TypeError()
    else:
        raise AdvancedIndexingError("Invalid index type or slice for Subtensor")


def get_constant_idx(
    idx_list, inputs, allow_partial=False, only_process_constants=False, elemwise=True
):
    r"""Return an `idx_list` with its constant inputs replaced by their Python scalar equivalents.

    May raise `NotScalarConstantError` if the indices contain non-constant entries.

    If `allow_partial` is ``True``, then entries that are not constant will
    stay as their input variable rather than raising an exception.

    ``None`` entries are always left as-is.

    Parameters
    ----------
    only_process_constants
        If ``True``, we only attempt to obtain the value of an index/slice if
        it's directly constant and don't try to dig through `DimShuffle`\s,
        fills, `Alloc`\s, and other to figure out its value.

    Examples
    --------
    Example usage where `v` and `a` are appropriately typed PyTensor variables :
    >>> from pytensor.scalar import int64
    >>> from pytensor.tensor import matrix
    >>> import numpy as np
    >>>
    >>> v = int64("v")
    >>> a = matrix("a")
    >>> b = a[v, 1:3]
    >>> b.owner.op.idx_list
    (ScalarType(int64), slice(ScalarType(int64), ScalarType(int64), None))
    >>> get_constant_idx(b.owner.op.idx_list, b.owner.inputs, allow_partial=True)
    [v, slice(np.int64(1), np.int64(3), None)]
    >>> get_constant_idx(b.owner.op.idx_list, b.owner.inputs)
    Traceback (most recent call last):
    pytensor.tensor.exceptions.NotScalarConstantError

    """
    real_idx = get_idx_list(inputs, idx_list)

    # TODO: Combine this with `as_index_literal`
    def conv(val):
        if val is None:
            return None
        elif isinstance(val, slice):
            return slice(conv(val.start), conv(val.stop), conv(val.step))
        else:
            try:
                return get_scalar_constant_value(
                    val,
                    only_process_constants=only_process_constants,
                    elemwise=elemwise,
                )
            except NotScalarConstantError:
                if allow_partial:
                    return val
                else:
                    raise

    return list(map(conv, real_idx))


def as_nontensor_scalar(a: Variable) -> ps.ScalarVariable:
    """Convert a value to a `ScalarType` variable."""
    # Since ps.as_scalar does not know about tensor types (it would
    # create a circular import) , this method converts either a
    # TensorVariable or a ScalarVariable to a scalar.
    if isinstance(a, Variable) and isinstance(a.type, TensorType):
        return pytensor.tensor.scalar_from_tensor(a)
    else:
        return ps.as_scalar(a)


class Subtensor(COp):
    """Basic NumPy indexing operator."""

    check_input = False
    view_map = {0: [0]}
    _f16_ok = True
    __props__ = ("idx_list",)

    def __init__(self, idx_list):
        # TODO: Provide the type of `self.idx_list`
        self.idx_list = tuple(map(index_vars_to_types, idx_list))

    def make_node(self, x, *inputs):
        """
        Parameters
        ----------
        x
            The tensor to take a subtensor of.
        inputs
            A list of pytensor Scalars.

        """
        x = as_tensor_variable(x)
        inputs = tuple(as_nontensor_scalar(a) for a in inputs)

        idx_list = list(self.idx_list)
        if len(idx_list) > x.type.ndim:
            raise IndexError("too many indices for array")

        input_types = get_slice_elements(
            idx_list, lambda entry: isinstance(entry, Type)
        )

        assert len(inputs) == len(input_types)

        for input, expected_type in zip(inputs, input_types, strict=True):
            if not expected_type.is_super(input.type):
                raise TypeError(
                    f"Incompatible types for Subtensor template. Expected {input.type}, got {expected_type}."
                )

        padded = [
            *get_idx_list((None, *inputs), self.idx_list),
            *[slice(None, None, None)] * (x.type.ndim - len(idx_list)),
        ]

        out_shape = []

        def extract_const(value):
            if value is None:
                return value, True
            try:
                value = get_scalar_constant_value(value)
                return value, True
            except NotScalarConstantError:
                return value, False

        for the_slice, length in zip(padded, x.type.shape, strict=True):
            if not isinstance(the_slice, slice):
                continue

            if length is None:
                out_shape.append(None)
                continue

            start = the_slice.start
            stop = the_slice.stop
            step = the_slice.step

            is_slice_const = True

            start, is_const = extract_const(start)
            is_slice_const = is_slice_const and is_const

            stop, is_const = extract_const(stop)
            is_slice_const = is_slice_const and is_const

            step, is_const = extract_const(step)
            is_slice_const = is_slice_const and is_const

            if not is_slice_const:
                out_shape.append(None)
                continue

            slice_length = len(range(*slice(start, stop, step).indices(length)))
            out_shape.append(slice_length)

        return Apply(
            self,
            (x, *inputs),
            [tensor(dtype=x.type.dtype, shape=out_shape)],
        )

    def perform(self, node, inputs, out_):
        (out,) = out_
        x = inputs[0]

        cdata = get_idx_list(inputs, self.idx_list)
        if len(cdata) == 1:
            cdata = cdata[0]

        out[0] = np.asarray(x.__getitem__(cdata))

    def infer_shape(self, fgraph, node, shapes):
        xshp = shapes[0]
        assert len(xshp) == node.inputs[0].ndim
        outshp = []
        actual_idx_list = list(get_idx_list(node.inputs, self.idx_list))
        padded = actual_idx_list + [slice(None, None, None)] * (
            len(xshp) - len(self.idx_list)
        )
        i = 0
        for idx, xl in zip(padded, xshp, strict=True):
            if isinstance(idx, slice):
                # If it is the default (None, None, None) slice, or a variant,
                # the shape will be xl
                if (
                    (idx.start in [None, 0])
                    and (idx.stop in [None, sys.maxsize])
                    and (idx.step is None or idx.step == 1)
                ):
                    outshp.append(xl)
                else:
                    cnf = get_canonical_form_slice(idx, xl)[0]
                    if cnf.step == 1:
                        length = cnf.stop - cnf.start
                    else:
                        length = (cnf.stop - cnf.start - 1) // cnf.step + 1
                    outshp.append(length)
                i += 1
            else:
                # That dimension is dropped
                pass
        assert i == node.outputs[0].ndim
        assert len(outshp) == node.outputs[0].ndim
        return [outshp]

    def grad(self, inputs, grads):
        (gz,) = grads
        x = inputs[0]
        rest = inputs[1:]
        if x.dtype in discrete_dtypes:
            first = x.zeros_like(dtype=config.floatX)
        else:
            # For best optimization, we let this as an inc.
            # This allow the opt local_IncSubtensor_serialize to apply first.
            # We have an optimization that will convert this to a
            # set subtensor here at:
            # pytensor/tensor/opt.py:local_incsubtensor_of_zeros_to_setsubtensor()
            first = IncSubtensor(self.idx_list)(x.zeros_like(), gz, *rest)
        return [first] + [DisconnectedType()()] * len(rest)

    def connection_pattern(self, node):
        rval = [[True], *([False] for _ in node.inputs[1:])]

        return rval

    def __hash__(self):
        msg = []
        for entry in self.idx_list:
            if isinstance(entry, slice):
                msg += [(entry.start, entry.stop, entry.step)]
            else:
                msg += [entry]

        idx_list = tuple(msg)
        # backport
        # idx_list = tuple((entry.start, entry.stop, entry.step)
        #                 if isinstance(entry, slice)
        #                 else entry
        #                 for entry in self.idx_list)
        return hash(idx_list)

    @staticmethod
    def str_from_slice(entry):
        if entry.step:
            return ":".join(
                (
                    "start" if entry.start else "",
                    "stop" if entry.stop else "",
                    "step",
                )
            )
        if entry.stop:
            return f"{'start' if entry.start else ''}:stop"
        if entry.start:
            return "start:"
        return ":"

    @staticmethod
    def str_from_indices(idx_list):
        indices = []
        letter_indexes = 0
        for entry in idx_list:
            if isinstance(entry, slice):
                indices.append(Subtensor.str_from_slice(entry))
            else:
                indices.append("ijk"[letter_indexes % 3] * (letter_indexes // 3 + 1))
                letter_indexes += 1
        return ", ".join(indices)

    def __str__(self):
        return f"{self.__class__.__name__}{{{self.str_from_indices(self.idx_list)}}}"

    @staticmethod
    def default_helper_c_code_args():
        """
        Returns a dictionary of default arguments to helper_c_code.

        """

        return {"c_prefix": "PyArray", "strides_mul": 1}

    @staticmethod
    def helper_c_code(
        node,
        name,
        inputs,
        outputs,
        sub,
        idx_list,
        view_ndim,
        c_prefix=None,
        strides_mul=None,
    ):
        """
        The parameters c_prefix are there to allow reusing this
        function on PyArray object.

        This fct take as input the x.

        """

        default_args = Subtensor.default_helper_c_code_args()

        if strides_mul is None:
            strides_mul = default_args["strides_mul"]

        if c_prefix is None:
            c_prefix = default_args["c_prefix"]

        #
        # two arrays are created in C code:
        # is_slice: len == ndim, 0 means int, 1 means slice
        # subtensor_spec: len = n_ints + 3 * n_slices
        #
        fail = sub["fail"]
        init_cmds = []  # initialization for subtensor_spec
        is_slice = []
        # TODO: change that, it might lead to unexpected results,
        # see assembla-#767
        NONE_CODE = sys.maxsize - 1

        pos = [0, 1]  # annoying version of global variable for init_entry

        def inc_spec_pos(amt):
            pos[0] += amt

        def inc_input_pos(amt):
            pos[1] += amt

        def spec_pos():
            return pos[0]

        def input_pos():
            return pos[1]

        def init_entry(entry, depth=0):
            if isinstance(entry, np.integer | int):
                init_cmds.append(f"subtensor_spec[{spec_pos()}] = {entry};")
                inc_spec_pos(1)
                if depth == 0:
                    is_slice.append(0)
            elif isinstance(entry, Type):
                init_cmds.append(
                    f"subtensor_spec[{spec_pos()}] = {inputs[input_pos()]};"
                )
                inc_spec_pos(1)
                inc_input_pos(1)
                if depth == 0:
                    is_slice.append(0)
            elif entry is None:
                init_cmds.append(f"subtensor_spec[{spec_pos()}] = {NONE_CODE};")
                inc_spec_pos(1)
                if depth == 0:
                    is_slice.append(0)
            elif depth == 0 and isinstance(entry, slice):
                init_entry(entry.start, depth + 1)
                init_entry(entry.stop, depth + 1)
                init_entry(entry.step, depth + 1)
                is_slice.append(1)
            else:
                assert 0, entry

        for entry in idx_list:
            init_entry(entry)
        # make sure we used all inputs
        assert input_pos() == len(inputs), input_pos()
        assert len(is_slice) <= node.inputs[0].ndim, node.inputs[0].ndim

        len_is_slice = len(is_slice)

        len_subtensor_spec = spec_pos()
        subensor_spec = f"npy_intp subtensor_spec[{len_subtensor_spec}];"
        if len_subtensor_spec == 0:
            subensor_spec = "npy_intp * subtensor_spec = NULL;"

        if is_slice:
            is_slice_init = (
                "int is_slice[] = {" + ",".join(str(s) for s in is_slice) + "};"
            )
        else:
            is_slice_init = "int* is_slice = NULL;"
        subtensor_init = "\n".join(init_cmds)

        (x,) = inputs[:1]
        (z,) = outputs

        if view_ndim:
            rval = f"""
        // Argument of the view
        npy_intp xview_dims[{view_ndim}];
        npy_intp xview_strides[{view_ndim}];

        """
        else:
            rval = """
        // Argument of the view
        npy_intp* xview_dims = NULL;
        npy_intp* xview_strides = NULL;

        """

        rval += f"""
        // One more argument of the view
        npy_intp xview_offset = 0;

        // The subtensor is created by iterating over the dimensions
        // and updating stride, shape, and data pointers

        {is_slice_init}
        {subensor_spec}
        {subtensor_init};
        int spec_pos = 0; //position in subtensor_spec
        int inner_ii = 0; // the current dimension of zview
        int outer_ii = 0; // current dimension of z


        for (; outer_ii < {len_is_slice}; ++outer_ii)
        {{
            if (is_slice[outer_ii])
            {{
                npy_intp length = {c_prefix}_DIMS({x})[outer_ii];
                npy_intp slicelength;
                npy_intp start = subtensor_spec[spec_pos+0];
                npy_intp stop  = subtensor_spec[spec_pos+1];
                npy_intp step  = subtensor_spec[spec_pos+2];
                if (step == {NONE_CODE}) step = 1;

                npy_intp defstart = step < 0 ? length-1 : 0;
                npy_intp defstop = step < 0 ? -1 : length;

                // logic adapted from
                // PySlice_GetIndicesEx in python source
                if (!step)
                {{
                    PyErr_Format(PyExc_ValueError,
                                 "slice step cannot be zero");
                    {fail};
                }}

                if (start == {NONE_CODE})
                {{
                    start = defstart;
                }}
                else
                {{
                    if (start < 0) start += length;
                    if (start < 0) start = (step < 0) ? -1 : 0;
                    if (start >= length)
                        start = (step < 0) ? length - 1 : length;
                }}

                if (stop == {NONE_CODE})
                {{
                    stop = defstop;
                }}
                else
                {{
                    if (stop < 0) stop += length;
                    if (stop < 0) stop = (step < 0) ? -1 : 0;
                    if (stop >= length)
                        stop = (step < 0) ? length - 1 : length;
                }}

                if ((step < 0 && stop >= start)
                    || (step > 0 && start >= stop)) {{
                    slicelength = 0;
                }}
                else if (step < 0) {{
                    slicelength = (stop-start+1)/step+1;
                }}
                else {{
                    slicelength = (stop-start-1)/step+1;
                }}

                if (0){{
                    fprintf(stdout, "start %zi\\n", start);
                    fprintf(stdout, "stop %zi\\n", stop);
                    fprintf(stdout, "step %zi\\n", step);
                    fprintf(stdout, "length %zi\\n", length);
                    fprintf(stdout, "slicelength %zi\\n", slicelength);
                }}

                assert (slicelength <= length);

                xview_offset += (npy_intp){c_prefix}_STRIDES({x})[outer_ii]
                    * start * {strides_mul};
                xview_dims[inner_ii] = slicelength;
                xview_strides[inner_ii] = (npy_intp){c_prefix}_STRIDES({x})[outer_ii] * step;

                inner_ii += 1;
                spec_pos += 3;
            }}
            else // tuple coord `outer_ii` is an int
            {{
                int idx = subtensor_spec[spec_pos];
                if (idx < 0) idx += {c_prefix}_DIMS({x})[outer_ii];
                if (idx >= 0)
                {{
                    if (idx < {c_prefix}_DIMS({x})[outer_ii])
                    {{
                        xview_offset += (npy_intp){c_prefix}_STRIDES({x})[outer_ii] * idx *
                               {strides_mul};
                    }}
                    else
                    {{
                        PyErr_Format(PyExc_IndexError,"index out of bounds");
                        {fail};
                    }}
                }}
                else
                {{
                    PyErr_Format(PyExc_IndexError,"index out of bounds");
                    {fail};
                }}

                spec_pos += 1;
            }}
        }}
        assert (inner_ii <= {view_ndim});
        while (inner_ii < {view_ndim})
        {{
            assert (outer_ii < {c_prefix}_NDIM({x}));
            xview_dims[inner_ii] = {c_prefix}_DIMS({x})[outer_ii];
            xview_strides[inner_ii] = {c_prefix}_STRIDES({x})[outer_ii];

            inner_ii += 1;
            outer_ii += 1;
        }}
        """
        # print rval
        return rval

    @staticmethod
    def helper_c_code_cache_version():
        return (9,)

    def c_code(self, node, name, inputs, outputs, sub):  # DEBUG
        if not isinstance(node.inputs[0].type, TensorType):
            raise NotImplementedError()

        x = inputs[0]
        (z,) = outputs
        ndim = node.inputs[0].ndim
        view_ndim = node.outputs[0].ndim
        fail = sub["fail"]

        decl = "PyArrayObject * xview = NULL;"

        checkNDim = f"""
        if (PyArray_NDIM({x}) != {ndim}){{
            PyErr_SetString(PyExc_ValueError,
                                     "Expected {ndim} dimensions input"
                                        );
            {fail}
        }}
        """

        get_xview = self.helper_c_code(
            node, name, inputs, outputs, sub, self.idx_list, view_ndim
        )
        build_view = f"""
        //TODO: give this Op a second output so that this view can be cached
        //TODO: alternatively, fix the memory leak on failure
        Py_INCREF(PyArray_DESCR({x}));
        xview = (PyArrayObject*)PyArray_NewFromDescr(
                &PyArray_Type,
                PyArray_DESCR({x}),
                {view_ndim},
                xview_dims,
                xview_strides,
                PyArray_BYTES({x}) + xview_offset,
                PyArray_FLAGS({x}),
                NULL);
        assert (PyArray_NDIM(xview) == {view_ndim});
        if (!xview)
        {{
            {fail};
        }}
        """

        finish_view = f"""
        Py_XDECREF({z});
        Py_INCREF(py_{x});
        PyArray_SetBaseObject(xview, py_{x});
        assert(py_{x} == (PyObject*){x});
        {z} = xview;
        """

        return decl + checkNDim + "{" + get_xview + build_view + finish_view + "}"

    def c_code_cache_version(self):
        hv = self.helper_c_code_cache_version()
        # If `helper_c_code_cache_version` is not versioned we do not want to
        # have a versioned version of this op's C code.
        if len(hv) == 0:
            return ()
        return (4, hv)

    def R_op(self, inputs, eval_points):
        # Subtensor is not differentiable wrt to its indices, therefore we
        # do not even need to consider the eval_points provided for those
        # (they should be defaulted to zeros_like by the global R_op)
        if eval_points[0] is None:
            return [None]
        return self(eval_points[0], *inputs[1:], return_list=True)


class SubtensorPrinter(Printer):
    def process(self, r, pstate):
        return self._process(r.owner.op.idx_list, r.owner.inputs, pstate)

    def _process(self, idxs, op_inputs, pstate):
        inputs = list(op_inputs)
        input = inputs.pop(0)
        sidxs = []
        getattr(pstate, "precedence", None)
        for entry in idxs:
            if isinstance(entry, ps.ScalarType):
                with set_precedence(pstate):
                    sidxs.append(pstate.pprinter.process(inputs.pop()))
            elif isinstance(entry, slice):
                if entry.start is None or entry.start == 0:
                    msg1 = ""
                else:
                    msg1 = entry.start

                if entry.stop is None or entry.stop == sys.maxsize:
                    msg2 = ""
                else:
                    msg2 = entry.stop

                if entry.step is None:
                    msg3 = ""
                else:
                    msg3 = f":{entry.step}"

                sidxs.append(f"{msg1}:{msg2}{msg3}")

        with set_precedence(pstate, 1000):
            sub = pstate.pprinter.process(input, pstate)

        return f"{sub}[{', '.join(sidxs)}]"


pprint.assign(Subtensor, SubtensorPrinter())


# TODO: Implement similar vectorize for Inc/SetSubtensor
@_vectorize_node.register(Subtensor)
def vectorize_subtensor(op: Subtensor, node, batch_x, *batch_idxs):
    """Rewrite subtensor with non-batched indexes as another Subtensor with prepended empty slices."""

    # TODO: Vectorize Subtensor with non-slice batched indexes as AdvancedSubtensor
    if any(batch_inp.type.ndim > 0 for batch_inp in batch_idxs):
        return vectorize_node_fallback(op, node, batch_x, *batch_idxs)

    old_x, *_ = node.inputs
    batch_ndims = batch_x.type.ndim - old_x.type.ndim
    new_idx_list = (slice(None),) * batch_ndims + op.idx_list
    return Subtensor(new_idx_list).make_node(batch_x, *batch_idxs)


def set_subtensor(x, y, inplace=False, tolerate_inplace_aliasing=False):
    """
    Return x with the given subtensor overwritten by y.

    Parameters
    ----------
    x
        Symbolic variable for the lvalue of = operation.
    y
        Symbolic variable for the rvalue of = operation.
    tolerate_inplace_aliasing
        See inc_subtensor for documentation.

    Examples
    --------
    To replicate the numpy expression ``r[10:] = 5``, type

    .. code-block:: python

        from pytensor.tensor import set_subtensor, vector

        r = vector("r")
        new_r = set_subtensor(r[10:], 5)

    Consider using :meth:`pytensor.tensor.variable.TensorVariable.set` instead.

    """
    return inc_subtensor(
        x,
        y,
        inplace,
        set_instead_of_inc=True,
        tolerate_inplace_aliasing=tolerate_inplace_aliasing,
    )


def inc_subtensor(
    x,
    y,
    inplace=False,
    set_instead_of_inc=False,
    tolerate_inplace_aliasing=False,
    ignore_duplicates=False,
):
    """Update the value of an indexed array by a given amount.

    This is equivalent to ``x[indices] += y`` or ``np.add.at(x, indices, y)``,
    depending on the value of `ignore_duplicates`.

    Parameters
    ----------
    x
        The symbolic result of a Subtensor operation.
    y
        The amount by which to increment the array.
    inplace
        Don't use. PyTensor will do in-place operations itself, when possible.
    set_instead_of_inc
        If True, do a set_subtensor instead.
    tolerate_inplace_aliasing:
        Allow `x` and `y` to be views of a single underlying array even while
        working in-place. For correct results, `x` and `y` must not be overlapping
        views; if they overlap, the result of this `Op` will generally be
        incorrect. This value has no effect if ``inplace=False``.
    ignore_duplicates
        This determines whether ``x[indices] += y`` is used or
        ``np.add.at(x, indices, y)``.

    Examples
    --------
    To replicate the expression ``r[10:] += 5``:

    .. code-block:: python

        from pytensor.tensor import ivector, inc_subtensor

        r = ivector("r")
        new_r = inc_subtensor(r[10:], 5)

    To replicate the expression ``r[[0, 1, 0]] += 5``:

    .. code-block:: python

        r = ivector("r")
        new_r = inc_subtensor(r[[0, 1, 0]], 5, ignore_duplicates=True)

    Consider using :meth:`pytensor.tensor.variable.TensorVariable.inc` instead.

    """
    # First of all, y cannot have a higher dimension than x,
    # nor have non-broadcastable dimensions where x is broadcastable.

    x = as_tensor_variable(x)
    y = as_tensor_variable(y)

    if y.ndim > x.ndim:
        raise TypeError(
            f"Trying to increment a {int(x.ndim)}-dimensional "
            f"subtensor with a {int(y.ndim)}-dimensional value."
        )

    dim_offset = x.ndim - y.ndim
    for dim in range(y.ndim):
        if x.broadcastable[dim + dim_offset] and not y.broadcastable[dim]:
            # It is acceptable to try to increment a subtensor with a
            # broadcastable dim with a tensor that is not broadcastable
            # on that dimension. However, its length must then be 1.
            # We insert a SpecifyShape Op to make sure it is the case.
            y = specify_broadcastable(y, dim)

    if x.owner is None:
        raise TypeError("x must be the result of a subtensor operation")

    # retrieve idx_list from x.owner
    if isinstance(x.owner.op, Subtensor):
        if tolerate_inplace_aliasing:
            destroyhandler_tolerate_aliased = [[0, 1]]
        else:
            destroyhandler_tolerate_aliased = []
        the_op = IncSubtensor(
            x.owner.op.idx_list,
            inplace,
            set_instead_of_inc,
            destroyhandler_tolerate_aliased=destroyhandler_tolerate_aliased,
        )
        real_x = x.owner.inputs[0]
        real_idxargs = x.owner.inputs[1:]
        return the_op(real_x, y, *real_idxargs)
    elif isinstance(x.owner.op, AdvancedSubtensor1):
        real_x = x.owner.inputs[0]
        ilist = x.owner.inputs[1]
        if ignore_duplicates:
            the_op = AdvancedIncSubtensor(
                inplace, set_instead_of_inc=set_instead_of_inc, ignore_duplicates=True
            )
        else:
            the_op = AdvancedIncSubtensor1(
                inplace, set_instead_of_inc=set_instead_of_inc
            )
        return the_op(real_x, y, ilist)
    elif isinstance(x.owner.op, AdvancedSubtensor):
        real_x = x.owner.inputs[0]
        ilist = x.owner.inputs[1:]
        the_op = AdvancedIncSubtensor(
            inplace,
            set_instead_of_inc=set_instead_of_inc,
            ignore_duplicates=ignore_duplicates,
        )
        return the_op(real_x, y, *ilist)
    elif isinstance(x.owner.op, DimShuffle):
        inner_x = x.owner.inputs[0]
        # In the dimshuffle case, there are in fact two dimshuffles:
        # one to make the indexed dimension the last one,
        # and one to put it back where it was. So, in the case where we have
        # inc_subtensor(x[:,i], y), the graph is actually
        # inc_subtensor((x.T)[i].T, y).
        # We could get all the way to x, and then get rid of the dimshuffles
        # completely, but the problem is that advanced_inc_subtensor1 can only
        # work on the first (outer-most, left-most) dimension of x,
        # just like advanced_subtensor1.
        # So we call advanced_inc_subtensor1(x.T, i, y.T) (as we also need to
        # transpose y if it is not a scalar or a vector), but then we need to
        # return something that has the same shape as x, not as x.T (inner_x).
        # So re-apply the outer dimshuffle on the new inc_subtensor,
        # and return advanced_inc_subtensor1(x.T, i, y.T).T.

        # Get the dimshuffle pattern to apply to y.
        x_order = x.owner.op.new_order
        y_order = ["x"] * x.ndim
        for i, v in enumerate(x_order):
            if v != "x" and (v - dim_offset) >= 0:
                y_order[v - dim_offset] = i

        inner_incsubtensor = inc_subtensor(
            inner_x,
            y.dimshuffle(y_order),
            inplace=inplace,
            set_instead_of_inc=set_instead_of_inc,
            tolerate_inplace_aliasing=tolerate_inplace_aliasing,
            ignore_duplicates=ignore_duplicates,
        )
        # The broadcastable pattern of inner_x may not be the same as
        # the one of x, so we have to build a new dimshuffle here,
        # instead of reusing x.owner.op().
        return inner_incsubtensor.dimshuffle(x.owner.op.new_order)

    elif isinstance(x.owner.op, Reshape):
        # This case happens when the indices are not arranged as a vector, but
        # as a higher-dimensional array. This is handled by the subtensor
        # by flattening this list, taking the subtensor, then reshaping the
        # result.
        inner_x = x.owner.inputs[0]
        # Try to apply inc_subtensor on inner_x.
        # If it works, there is no need to reshape, as the inc_subtensor
        # will have the same shape as inner_x, which is what we want.
        # We also explicitly duplicate y to its broadcasted shape
        # before we partially flatten it to inner_x dimension. This is
        # not strictly needed in all cases, but it is easier this way.
        if y.ndim > 0:
            # This if is needed to prevent some useless warning about
            # old code bug.
            expanded_y = alloc(y, *[x.shape[i] for i in range(x.ndim)])
            flattened_y = expanded_y.reshape(inner_x.shape)
        else:
            flattened_y = y

        inner_incsubtensor = inc_subtensor(
            inner_x,
            flattened_y,
            inplace=inplace,
            set_instead_of_inc=set_instead_of_inc,
            tolerate_inplace_aliasing=tolerate_inplace_aliasing,
            ignore_duplicates=ignore_duplicates,
        )
        return inner_incsubtensor
    else:
        raise TypeError("x must be the result of a subtensor operation")


class IncSubtensor(COp):
    """
    Increment a subtensor.

    This is like numpy's

        x[i,j,k] += y

    It is used internally to implement the gradient on SubTensor.

    Parameters
    ----------
    set_instead_of_inc
        If True set the subtensor to the value instead of incrementing it by
        that value.

    """

    check_input = False
    __props__ = ("idx_list", "inplace", "set_instead_of_inc")

    def __init__(
        self,
        idx_list,
        inplace=False,
        set_instead_of_inc=False,
        destroyhandler_tolerate_aliased=None,
    ):
        if destroyhandler_tolerate_aliased is None:
            destroyhandler_tolerate_aliased = []
        self.idx_list = list(map(index_vars_to_types, idx_list))
        self.inplace = inplace
        if inplace:
            self.destroy_map = {0: [0]}
        self.destroyhandler_tolerate_aliased = list(destroyhandler_tolerate_aliased)
        self.set_instead_of_inc = set_instead_of_inc

    def __hash__(self):
        idx_list = tuple(
            (entry.start, entry.stop, entry.step) if isinstance(entry, slice) else entry
            for entry in self.idx_list
        )
        return hash((type(self), idx_list, self.inplace, self.set_instead_of_inc))

    def __str__(self):
        name = "SetSubtensor" if self.set_instead_of_inc else "IncSubtensor"
        return f"{name}{{{Subtensor.str_from_indices(self.idx_list)}}}"

    def make_node(self, x, y, *inputs):
        """
        Parameters
        ----------
        x
            The tensor to increment.
        y
            The value to increment by.
        inputs: TODO WRITEME

        """
        x, y = map(as_tensor_variable, [x, y])
        if y.ndim > x.ndim:
            raise ValueError(
                f"Trying to increment a {int(x.ndim)}-dimensional "
                f"subtensor with a {int(y.ndim)}-dimensional value."
            )
        inputs = tuple(map(as_nontensor_scalar, inputs))

        idx_list = list(self.idx_list)
        if len(idx_list) > x.type.ndim:
            raise IndexError("too many indices for array")

        input_types = get_slice_elements(
            idx_list, lambda entry: isinstance(entry, Type)
        )
        if len(inputs) != len(input_types):
            raise IndexError(
                "Not enough inputs to fill in the Subtensor template.", inputs, idx_list
            )
        for input, expected_type in zip(inputs, input_types, strict=True):
            if not expected_type.is_super(input.type):
                raise TypeError(
                    f"Wrong type for Subtensor template. Expected {input.type}, got {expected_type}."
                )

        return Apply(self, (x, y, *inputs), [x.type()])

    def decl_view(self):
        return "PyArrayObject * zview = NULL;"

    def perform(self, node, inputs, out_):
        (out,) = out_
        x, y = inputs[:2]
        indices = list(reversed(inputs[2:]))

        def _convert(entry):
            if isinstance(entry, Type):
                return indices.pop()
            elif isinstance(entry, slice):
                return slice(
                    _convert(entry.start), _convert(entry.stop), _convert(entry.step)
                )
            else:
                return entry

        cdata = tuple(map(_convert, self.idx_list))
        if len(cdata) == 1:
            cdata = cdata[0]
        if not self.inplace:
            x = x.copy()
        sub_x = x.__getitem__(cdata)
        if sub_x.shape:
            # we've sliced out an N-D tensor with N > 0
            if not self.set_instead_of_inc:
                sub_x += y
            else:
                # sub_x += -sub_x + y
                x.__setitem__(cdata, y)
        else:
            # scalar case
            if not self.set_instead_of_inc:
                x.__setitem__(cdata, sub_x + y)
            else:
                x.__setitem__(cdata, y)
        out[0] = x

    def c_code(self, node, name, inputs, outputs, sub):
        # This method delegates much of the work to helper
        # methods. This method implements the main logic
        # but subclasses may override the helper methods
        # to change the particulars.

        self.do_type_checking(node)

        if self.inplace:  # convert bool to int
            inplace = 1
        else:
            inplace = 0
        x = inputs[0]
        y = inputs[1]
        (z,) = outputs
        if self.set_instead_of_inc:  # convert bool to int
            op_is_set = 1
        else:
            op_is_set = 0
        fail = sub["fail"]
        view_ndim = node.inputs[0].ndim - sum(
            not isinstance(idx, slice) for idx in self.idx_list
        )

        copy_of_x = self.copy_of_x(x)

        copy_input_if_necessary = f"""
        if ({inplace})
        {{
            if ({x} != {z})
            {{
                Py_XDECREF({z});
                Py_INCREF({x});
                {z} = {x};
            }}
        }}
        else
        {{
            Py_XDECREF({z});
            {z} = {copy_of_x};
            if (!{z}) {{
                // Exception already set
                {fail}
            }}
        }}
        """

        # get info needed to make zview: a view of %(z)s
        helper_args = self.get_helper_c_code_args()

        get_zview = Subtensor.helper_c_code(
            node=node,
            name=name,
            inputs=outputs[:1] + inputs[2:],
            outputs=outputs,
            sub=sub,
            idx_list=self.idx_list,
            view_ndim=view_ndim,
            **helper_args,
        )

        # Make a view on the output, as we will write into it.
        alloc_zview = self.make_view_array(z, view_ndim)

        build_view = f"""
        //TODO: give this Op a second output so that this view can be cached
        //TODO: alternatively, fix the memory leak on failure
        {alloc_zview};
        if (!zview)
        {{
            {fail};
        }}
        """

        copy_into = self.copy_into("zview", y)

        add_to_zview = self.add_to_zview(name, y, fail)

        make_modification = f"""
        if ({op_is_set})
        {{
            if ({copy_into}) // does broadcasting
            {{
                Py_DECREF(zview);
                {fail};
            }}
        }}
        else
        {{
            {add_to_zview}
        }}
        """
        return (
            self.decl_view()
            + copy_input_if_necessary
            + "{"
            + get_zview
            + build_view
            + make_modification
            + "Py_DECREF(zview);"
            + "}"
        )

    def do_type_checking(self, node):
        """
        Should raise NotImplementedError if c_code does not support
        the types involved in this node.

        """

        if not isinstance(node.inputs[0].type, TensorType):
            raise NotImplementedError()

    def c_code_cache_version(self):
        hv = Subtensor.helper_c_code_cache_version()
        if hv:
            return (3, hv)
        else:
            return ()

    def copy_of_x(self, x):
        """
        Parameters
        ----------
        x
            A string giving the name of a C variable pointing to an array.

        Returns
        -------
        object
            C code expression to make a copy of x.

        Base class uses PyArrayObject *, subclasses may override for
        different types of arrays.

        """
        # Parameters of PyArray_FromAny are:
        # array
        # dtype: we pass NULL to say any dtype is acceptable, so the existing
        #        dtype will be copied
        # min_depth: we pass 0 to have this parameter ignored
        # max_depth: we pass 0 to have this parameter ignored
        # requirements: here we pass NPY_ARRAY_ENSURECOPY to force a copy
        # context: this is almost always NULL, I'm not sure what it's used for
        return f"""(PyArrayObject*)PyArray_FromAny(py_{x}, NULL, 0, 0,
                NPY_ARRAY_ENSURECOPY, NULL)"""

    def make_view_array(self, x, view_ndim):
        """
        Parameters
        ----------
        x
            A string identifying an array to be viewed.
        view_ndim
            A string specifying the number of dimensions to have in the view.

        This doesn't need to actually set up the view with the right indexing;
        we'll do that manually later.

        """

        return f"""Py_INCREF(PyArray_DESCR({x}));
        zview = (PyArrayObject*)PyArray_NewFromDescr(
                &PyArray_Type,
                PyArray_DESCR({x}),
                {view_ndim},
                xview_dims, //PyArray_DIMS({x}),
                xview_strides, //PyArray_STRIDES({x}),
                PyArray_BYTES({x}) + xview_offset, //PyArray_DATA({x}),
                PyArray_FLAGS({x}),
                NULL);
        """

    def get_helper_c_code_args(self):
        """
        Return a dictionary of arguments to pass to helper_c_code.

        """
        return Subtensor.default_helper_c_code_args()

    def copy_into(self, view, source):
        """
        Parameters
        ----------
        view : string
            C code expression for an array.
        source : string
            C code expression for an array.

        Returns
        -------
        object
            C code expression to copy source into view, and 0 on success.

        """
        return f"""PyArray_CopyInto({view}, {source})"""

    def add_to_zview(self, name, x, fail):
        """
        Return C code to add x to zview. Should DECREF zview if the
        add fails.

        """

        return f"""
            PyArrayObject * add_rval = (PyArrayObject*)PyNumber_InPlaceAdd(
                    (PyObject*)zview, py_{x});
            if (add_rval)
            {{
                assert (PyArray_Check((PyObject*)add_rval));
                assert (PyArray_DATA(add_rval) == PyArray_DATA(zview));
                Py_DECREF(add_rval);
            }}
            else
            {{
                Py_DECREF(zview);
                {fail};
            }}"""

    def infer_shape(self, fgraph, node, shapes):
        return [shapes[0]]

    def R_op(self, inputs, eval_points):
        if eval_points[0] is None or eval_points[1] is None:
            return [None]
        # Again we ignore eval points for indices because incsubtensor is
        # not differentiable wrt to those
        return self(eval_points[0], eval_points[1], *inputs[2:], return_list=True)

    def connection_pattern(self, node):
        rval = [[True], [True], *([False] for _ in node.inputs[2:])]

        return rval

    def grad(self, inputs, grads):
        (g_output,) = grads
        x, y = inputs[:2]
        idx_list = inputs[2:]

        if x.dtype in discrete_dtypes:
            # The output dtype is the same as x
            gx = x.zeros_like(dtype=config.floatX)
            if y.dtype in discrete_dtypes:
                gy = y.zeros_like(dtype=config.floatX)
            else:
                gy = y.zeros_like()
        elif x.dtype in complex_dtypes:
            raise NotImplementedError("No support for complex grad yet")
        else:
            if self.set_instead_of_inc:
                gx = set_subtensor(
                    Subtensor(idx_list=self.idx_list)(g_output, *idx_list),
                    pytensor.tensor.zeros_like(y),
                )
            else:
                gx = g_output
            gy = Subtensor(idx_list=self.idx_list)(g_output, *idx_list)
            gy = _sum_grad_over_bcasted_dims(y, gy)

        return [gx, gy] + [DisconnectedType()()] * len(idx_list)


class IncSubtensorPrinter(SubtensorPrinter):
    def process(self, r, pstate):
        x, y, *idx_args = r.owner.inputs

        res = self._process(r.owner.op.idx_list, [x, *idx_args], pstate)

        with set_precedence(pstate, 1000):
            y_str = pstate.pprinter.process(r.owner.inputs[1], pstate)

        if r.owner.op.set_instead_of_inc:
            res = f"set_subtensor({res}, {y_str})"
        else:
            res = f"inc_subtensor({res}, {y_str})"
        return res


pprint.assign(IncSubtensor, IncSubtensorPrinter())


def _sum_grad_over_bcasted_dims(x, gx):
    """
    Sum of gx over dimensions to reproduce x.broadcastable.

    This is useful to sum gradients over certain dimensions when
    x has been broadcasted, and we need to sum the gradient contributions
    over all duplications.

    """
    if gx.broadcastable != x.broadcastable:
        x_dim_added = gx.ndim - x.ndim
        x_broad = (True,) * x_dim_added + x.broadcastable
        axis_to_sum = []
        for i in range(gx.ndim):
            if gx.broadcastable[i] is False and x_broad[i] is True:
                axis_to_sum.append(i)
            elif gx.broadcastable[i] is True and x_broad[i] is False:
                # This means that PyTensor was able to infer that
                # gx.shape[i] is 1, so x.shape[i] is 1, but we
                # didn't know it. It is fine.
                pass
            else:
                assert gx.broadcastable[i] == x_broad[i]
        gx = gx.sum(axis=axis_to_sum, keepdims=True)
        if gx.ndim != x.ndim:
            assert gx.ndim > x.ndim
            for i in range(x_dim_added):
                assert gx.broadcastable[i]
            gx = gx.dimshuffle(*range(x_dim_added, gx.ndim))
        # Broadcastable flags of gx can be the same or more specific than x.
        # Only unallowed case is x_dim_b == True and gx_dim_b == False.
        assert not any(
            x_dim_b and not gx_dim_b
            for x_dim_b, gx_dim_b in zip(
                x.type.broadcastable, gx.type.broadcastable, strict=True
            )
        ), (x.type, gx.type)
    return gx


class AdvancedSubtensor1(COp):
    """
    Implement x[ilist] where ilist is a vector of integers.

    """

    # sparse_grad doesn't go in here since it only affects the output
    # of the grad() method.
    __props__ = ()
    _f16_ok = True
    check_input = False

    def __init__(self, sparse_grad=False):
        self.sparse_grad = sparse_grad

    def make_node(self, x, ilist):
        x_ = as_tensor_variable(x)
        ilist_ = as_tensor_variable(ilist)
        if ilist_.type.dtype not in integer_dtypes:
            raise TypeError("index must be integers")
        if ilist_.type.ndim != 1:
            raise TypeError("index must be vector")
        if x_.type.ndim == 0:
            raise TypeError("cannot index into a scalar")
        out_shape = (ilist_.type.shape[0], *x_.type.shape[1:])
        return Apply(self, [x_, ilist_], [TensorType(dtype=x.dtype, shape=out_shape)()])

    def perform(self, node, inp, output_storage):
        x, i = inp

        # Numpy take is always slower when out is provided
        # https://github.com/numpy/numpy/issues/28636
        output_storage[0][0] = x.take(i, axis=0, out=None)

    def connection_pattern(self, node):
        rval = [[True], *([False] for _ in node.inputs[1:])]

        return rval

    def grad(self, inputs, grads):
        x, ilist = inputs
        (gz,) = grads
        assert len(inputs) == 2
        if self.sparse_grad:
            if x.type.ndim != 2:
                raise TypeError(
                    "AdvancedSubtensor1: you can't take the sparse grad"
                    " from a tensor with ndim != 2. ndim is " + str(x.type.ndim)
                )

            rval1 = [pytensor.sparse.construct_sparse_from_list(x, gz, ilist)]
        else:
            if x.dtype in discrete_dtypes:
                # The output dtype is the same as x
                gx = x.zeros_like(dtype=config.floatX)
            elif x.dtype in complex_dtypes:
                raise NotImplementedError("No support for complex grad yet")
            else:
                gx = x.zeros_like()
            rval1 = [advanced_inc_subtensor1(gx, gz, ilist)]
        return rval1 + [DisconnectedType()()] * (len(inputs) - 1)

    def R_op(self, inputs, eval_points):
        if eval_points[0] is None:
            return [None]
        return self.make_node(eval_points[0], *inputs[1:]).outputs

    def infer_shape(self, fgraph, node, ishapes):
        x, ilist = ishapes
        return [ilist + x[1:]]

    def c_code(self, node, name, input_names, output_names, sub):
        if self.__class__ is not AdvancedSubtensor1:
            raise MethodNotDefined(
                "c_code defined for AdvancedSubtensor1, not for child class",
                type(self),
            )
        x, idxs = node.inputs
        if self._idx_may_be_invalid(x, idxs):
            mode = "NPY_RAISE"
        else:
            # We can know ahead of time that all indices are valid, so we can use a faster mode
            mode = "NPY_WRAP"  # This seems to be faster than NPY_CLIP

        a_name, i_name = input_names[0], input_names[1]
        output_name = output_names[0]
        fail = sub["fail"]
        if mode == "NPY_RAISE":
            # numpy_take always makes an intermediate copy if NPY_RAISE which is slower than just allocating a new buffer
            # We can remove this special case after https://github.com/numpy/numpy/issues/28636
            manage_pre_allocated_out = f"""
                if ({output_name} != NULL) {{
                    // Numpy TakeFrom is always slower when copying
                    // https://github.com/numpy/numpy/issues/28636
                    Py_CLEAR({output_name});
                }}
            """
        else:
            manage_pre_allocated_out = f"""
                if ({output_name} != NULL) {{
                    npy_intp nd = PyArray_NDIM({a_name}) + PyArray_NDIM({i_name}) - 1;
                    if (PyArray_NDIM({output_name}) != nd) {{
                        Py_CLEAR({output_name});
                    }}
                    else {{
                        int i;
                        npy_intp* shape = PyArray_DIMS({output_name});
                        for (i = 0; i < PyArray_NDIM({i_name}); i++) {{
                            if (shape[i] != PyArray_DIMS({i_name})[i]) {{
                                Py_CLEAR({output_name});
                                break;
                            }}
                        }}
                        if ({output_name} != NULL) {{
                            for (; i < nd; i++) {{
                                if (shape[i] != PyArray_DIMS({a_name})[i-PyArray_NDIM({i_name})+1]) {{
                                    Py_CLEAR({output_name});
                                    break;
                                }}
                            }}
                        }}
                    }}
                }}
            """

        return f"""
            {manage_pre_allocated_out}
            {output_name} = (PyArrayObject*)PyArray_TakeFrom(
                        {a_name}, (PyObject*){i_name}, 0, {output_name}, {mode});
            if ({output_name} == NULL) {fail};
        """

    def c_code_cache_version(self):
        return (5,)

    @staticmethod
    def _idx_may_be_invalid(x, idx) -> bool:
        if idx.type.shape[0] == 0:
            # Empty index is always valid
            return False

        if x.type.shape[0] is None:
            # We can't know if in index is valid if we don't know the length of x
            return True

        if not isinstance(idx, Constant):
            # This is conservative, but we don't try to infer lower/upper bound symbolically
            return True

        shape0 = x.type.shape[0]
        min_idx, max_idx = idx.data.min(), idx.data.max()
        return not (min_idx >= 0 or min_idx >= -shape0) and (
            max_idx < 0 or max_idx < shape0
        )


advanced_subtensor1 = AdvancedSubtensor1()


class AdvancedIncSubtensor1(COp):
    """
    Increments a subtensor using advanced slicing (list of index).

    """

    __props__ = ("inplace", "set_instead_of_inc")
    check_input = False
    params_type = ParamsType(inplace=ps.bool, set_instead_of_inc=ps.bool)

    _runtime_broadcast_error_msg = (
        "Runtime broadcasting not allowed. "
        "AdvancedIncSubtensor1 was asked to broadcast the second input (y) along a dimension that was not marked as broadcastable. "
        "If broadcasting was intended, use `specify_broadcastable` on the relevant dimension(s)."
    )

    def __init__(self, inplace=False, set_instead_of_inc=False):
        self.inplace = bool(inplace)
        self.set_instead_of_inc = bool(set_instead_of_inc)
        if inplace:
            self.destroy_map = {0: [0]}

    def clone_inplace(self):
        return self.__class__(inplace=True, set_instead_of_inc=self.set_instead_of_inc)

    def __str__(self):
        if self.inplace:
            msg = "inplace"
        else:
            msg = "no_inplace"
        if self.set_instead_of_inc:
            msg += ",set"
        else:
            msg += ",inc"

        return self.__class__.__name__ + f"{{{msg}}}"

    def make_node(self, x, y, ilist):
        x_ = as_tensor_variable(x)
        y_ = as_tensor_variable(y)
        ilist_ = as_tensor_variable(ilist)

        if ilist_.type.dtype not in integer_dtypes:
            raise TypeError("index must be integers")
        if ilist_.type.ndim != 1:
            raise TypeError("index must be vector")
        if x_.type.ndim == 0:
            raise TypeError("cannot index into a scalar")
        if y_.type.ndim > x_.type.ndim:
            if self.set_instead_of_inc:
                opname = "set"
            else:
                opname = "increment"
            raise TypeError(
                f"cannot {opname} x subtensor with ndim={x_.type.ndim} by y with ndim={y_.type.ndim}."
            )

        return Apply(self, [x_, y_, ilist_], [x_.type()])

    def copy_of_x(self, x):
        """
        Parameters
        ----------
        x : string
            Gives the name of a C variable pointing to an array.

        Returns
        -------
        object
            C code expression to make a copy of x.

        Base class uses PyArrayObject *, subclasses may override for
        different types of arrays.

        """
        # Parameters of PyArray_FromAny are:
        # array
        # dtype: we pass NULL to say any dtype is acceptable, so the existing
        #        dtype will be copied
        # min_depth: we pass 0 to have this parameter ignored
        # max_depth: we pass 0 to have this parameter ignored
        # requirements: here we pass NPY_ARRAY_ENSURECOPY to force a copy
        # context: this is almost always NULL, I'm not sure what it's used for
        return f"""(PyArrayObject*)PyArray_FromAny(py_{x}, NULL, 0, 0,
                NPY_ARRAY_ENSURECOPY, NULL)"""

    def c_support_code(self, **kwargs):
        if numpy_version < "1.8.0" or using_numpy_2:
            return None

        types = [
            "npy_" + t
            for t in [
                "int8",
                "int16",
                "int32",
                "int64",
                "uint8",
                "uint16",
                "uint32",
                "uint64",
                "float16",
                "float32",
                "float64",
            ]
        ]

        complex_types = ["npy_" + t for t in ("complex32", "complex64", "complex128")]

        inplace_map_template = """
        #if defined(%(typen)s)
        static void %(type)s_inplace_add(PyArrayMapIterObject *mit,
                                        PyArrayIterObject *it, int inc_or_set)
        {
            int index = mit->size;
            while (index--) {
                %(op)s

                PyArray_MapIterNext(mit);
                PyArray_ITER_NEXT(it);
            }
        }
        #endif
        """

        floatadd = (
            "((%(type)s*)mit->dataptr)[0] = "
            "(inc_or_set ? ((%(type)s*)mit->dataptr)[0] : 0)"
            " + ((%(type)s*)it->dataptr)[0];"
        )
        complexadd = """
        ((%(type)s*)mit->dataptr)[0].real =
            (inc_or_set ? ((%(type)s*)mit->dataptr)[0].real : 0)
            + ((%(type)s*)it->dataptr)[0].real;
        ((%(type)s*)mit->dataptr)[0].imag =
            (inc_or_set ? ((%(type)s*)mit->dataptr)[0].imag : 0)
            + ((%(type)s*)it->dataptr)[0].imag;
        """

        fns = "".join(
            [
                inplace_map_template
                % {"type": t, "typen": t.upper(), "op": floatadd % {"type": t}}
                for t in types
            ]
            + [
                inplace_map_template
                % {"type": t, "typen": t.upper(), "op": complexadd % {"type": t}}
                for t in complex_types
            ]
        )

        def gen_binop(type, typen):
            return f"""
    #if defined({typen})
    {type}_inplace_add,
    #endif
    """

        fn_array = (
            "static inplace_map_binop addition_funcs[] = {"
            + "".join(gen_binop(type=t, typen=t.upper()) for t in types + complex_types)
            + "NULL};\n"
        )

        def gen_num(typen):
            return f"""
    #if defined({typen})
    {typen},
    #endif
    """

        type_number_array = (
            "static int type_numbers[] = {"
            + "".join(gen_num(typen=t.upper()) for t in types + complex_types)
            + "-1000};"
        )

        code = (
            """
            typedef void (*inplace_map_binop)(PyArrayMapIterObject *,
                                            PyArrayIterObject *, int inc_or_set);
            """
            + fns
            + fn_array
            + type_number_array
            + """
    static int
    map_increment(PyArrayMapIterObject *mit, PyArrayObject *op,
                inplace_map_binop add_inplace, int inc_or_set)
    {
        PyArrayObject *arr = NULL;
        PyArrayIterObject *it;
        PyArray_Descr *descr;
        if (mit->ait == NULL) {
            return -1;
        }
        descr = PyArray_DESCR(mit->ait->ao);
        Py_INCREF(descr);
        arr = (PyArrayObject *)PyArray_FromAny((PyObject *)op, descr,
                                    0, 0, NPY_ARRAY_FORCECAST, NULL);
        if (arr == NULL) {
            return -1;
        }
        if ((mit->subspace != NULL) && (mit->consec)) {
            PyArray_MapIterSwapAxes(mit, (PyArrayObject **)&arr, 0);
            if (arr == NULL) {
                return -1;
            }
        }
        it = (PyArrayIterObject*)
                PyArray_BroadcastToShape((PyObject*)arr, mit->dimensions, mit->nd);
        if (it  == NULL) {
            Py_DECREF(arr);
            return -1;
        }

        (*add_inplace)(mit, it, inc_or_set);

        Py_DECREF(arr);
        Py_DECREF(it);
        return 0;
    }


    static int
    inplace_increment(PyArrayObject *a, PyObject *index, PyArrayObject *inc,
                    int inc_or_set)
    {
        inplace_map_binop add_inplace = NULL;
        int type_number = -1;
        int i = 0;
        PyArrayMapIterObject * mit;

        if (PyArray_FailUnlessWriteable(a, "input/output array") < 0) {
            return -1;
        }

        if (PyArray_NDIM(a) == 0) {
            PyErr_SetString(PyExc_IndexError, "0-d arrays can't be indexed.");
            return -1;
        }
        type_number = PyArray_TYPE(a);

        while (type_numbers[i] >= 0 && addition_funcs[i] != NULL){
            if (type_number == type_numbers[i]) {
                add_inplace = addition_funcs[i];
                break;
            }
            i++ ;
        }

        if (add_inplace == NULL) {
            PyErr_SetString(PyExc_TypeError, "unsupported type for a");
            return -1;
        }
        mit = (PyArrayMapIterObject *) PyArray_MapIterArray(a, index);
        if (mit == NULL) {
            goto fail;
        }
        if (map_increment(mit, inc, add_inplace, inc_or_set) != 0) {
            goto fail;
        }

        Py_DECREF(mit);

        Py_INCREF(Py_None);
        return 0;

    fail:
        Py_XDECREF(mit);

        return -1;
    }
    """
        )

        return code

    def c_code(self, node, name, input_names, output_names, sub):
        x, y, idx = input_names
        [out] = output_names
        copy_of_x = self.copy_of_x(x)
        params = sub["params"]
        fail = sub["fail"]

        x_, y_, idx_ = node.inputs
        y_cdtype = y_.type.dtype_specs()[1]
        idx_cdtype = idx_.type.dtype_specs()[1]
        out_cdtype = node.outputs[0].type.dtype_specs()[1]
        y_bcast = y_.type.broadcastable != idx_.type.broadcastable
        if (
            x_.type.ndim == 1
            and y_.type.ndim == 1
            and not y_bcast
            and x_.type.dtype not in complex_dtypes
            and y_.type.dtype not in complex_dtypes
        ):
            # Simple implementation for vector x, y cases
            idx_may_be_neg = not (
                # Empty idx needs no negative checks
                idx_.type.shape[0] == 0
                or (isinstance(idx_, Constant) and idx_.data.min() >= 0)
            )
            idx_may_be_invalid = AdvancedSubtensor1._idx_may_be_invalid(x_, idx_)
            shape0 = x_.type.shape[0]
            # This is used to make sure that when we trust the indices to be valid
            # we are not fooled by a wrong static shape
            # We mention x to the user in error messages but we work (and make checks) on out,
            # which should be x or a copy of it
            unexpected_shape0 = (
                f"PyArray_SHAPE({out})[0] != {shape0}" if shape0 is not None else "0"
            )

            op = "=" if self.set_instead_of_inc else "+="
            code = f"""
            if ({params}->inplace)
            {{
                if ({x} != {out})
                {{
                    Py_XDECREF({out});
                    Py_INCREF({x});
                    {out} = {x};
                }}
            }}
            else
            {{
                Py_XDECREF({out});
                {out} = {copy_of_x};
                if (!{out}) {{
                    // Exception already set
                    {fail}
                }}
            }}

            if (PyArray_NDIM({out}) != 1) {{
                PyErr_Format(PyExc_ValueError, "AdvancedIncSubtensor1: first input (x) ndim should be 1, got %d", PyArray_NDIM({out}));
                {fail}
            }}
            if ({unexpected_shape0}) {{
                PyErr_Format(PyExc_ValueError, "AdvancedIncSubtensor1: first input (x) shape should be {shape0}, got %d", PyArray_SHAPE({out})[0]);
                {fail}
            }}
            if (PyArray_NDIM({idx}) != 1) {{
                PyErr_Format(PyExc_ValueError, "AdvancedIncSubtensor1: indices ndim should be 1, got %d", PyArray_NDIM({idx}));
                {fail}
            }}
            if (PyArray_NDIM({y}) != 1) {{
                PyErr_Format(PyExc_ValueError, "AdvancedIncSubtensor1: second input (y) ndim should be 1, got %d", PyArray_NDIM({y}));
                {fail}
            }}
            if (PyArray_SHAPE({y})[0] != PyArray_SHAPE({idx})[0]) {{
                if ((PyArray_NDIM({y}) == 1) && (PyArray_SHAPE({y})[0] == 1)){{
                    PyErr_Format(PyExc_ValueError, "{self._runtime_broadcast_error_msg}");
                }} else {{
                    PyErr_Format(PyExc_ValueError,
                    "AdvancedIncSubtensor1: Shapes of second input (y) and indices do not match: %d, %d",
                    PyArray_SHAPE({y})[0], PyArray_SHAPE({idx})[0]);
                }}
                {fail}
            }}

            {{
                npy_intp out_shape0 = PyArray_SHAPE({out})[0];
                {out_cdtype}* out_data = ({out_cdtype}*)PyArray_DATA({out});
                {y_cdtype}* y_data = ({y_cdtype}*)PyArray_DATA({y});
                {idx_cdtype}* idx_data = ({idx_cdtype}*)PyArray_DATA({idx});
                npy_intp n = PyArray_SHAPE({idx})[0];
                npy_intp out_jump = PyArray_STRIDES({out})[0] / PyArray_ITEMSIZE({out});
                npy_intp y_jump = PyArray_STRIDES({y})[0] / PyArray_ITEMSIZE({y});
                npy_intp idx_jump = PyArray_STRIDES({idx})[0] / PyArray_ITEMSIZE({idx});

                for(int i = 0; i < n; i++){{
                    {idx_cdtype} idx = idx_data[i * idx_jump];
                    if ({int(idx_may_be_neg)}){{
                        if (idx < 0) {{
                            idx += out_shape0;
                        }}
                    }}
                    if ({int(idx_may_be_invalid)}){{
                        if ((idx < 0) || (idx >= out_shape0)) {{
                            PyErr_Format(PyExc_IndexError,"index %d out of bounds for array with shape %d", idx_data[i * idx_jump], out_shape0);
                            {fail}
                        }}
                    }}
                    out_data[idx * out_jump] {op} y_data[i * y_jump];
                }}

            }}
            """
            return code

        if numpy_version < "1.8.0" or using_numpy_2:
            raise NotImplementedError

        return f"""
        PyObject* rval = NULL;
        if ({params}->inplace)
        {{
            if ({x} != {out})
            {{
                Py_XDECREF({out});
                Py_INCREF({x});
                {out} = {x};
            }}
        }}
        else
        {{
            Py_XDECREF({out});
            {out} = {copy_of_x};
            if (!{out}) {{
                // Exception already set
                {fail}
            }}
        }}
        if (inplace_increment({out}, (PyObject *){idx}, {y}, (1 - {params}->set_instead_of_inc))) {{
            {fail};
        }}
        Py_XDECREF(rval);
        """

    def c_code_cache_version(self):
        return (10,)

    def _check_runtime_broadcasting(
        self, node: Apply, x: np.ndarray, y: np.ndarray, idx: np.ndarray
    ) -> None:
        if y.ndim > 0:
            y_pt_bcast = node.inputs[1].broadcastable  # type: ignore

            if not y_pt_bcast[0] and y.shape[0] == 1 and y.shape[0] != idx.shape[0]:
                # Attempting to broadcast with index
                raise ValueError(self._runtime_broadcast_error_msg)
            if any(
                not y_bcast and y_dim == 1 and y_dim != x_dim
                for y_bcast, y_dim, x_dim in zip(
                    reversed(y_pt_bcast),
                    reversed(y.shape),
                    reversed(x.shape),
                    strict=False,
                )
            ):
                # Attempting to broadcast with buffer
                raise ValueError(self._runtime_broadcast_error_msg)

    def perform(self, node, inputs, output_storage):
        x, y, idx = inputs

        if not self.inplace:
            x = x.copy()

        self._check_runtime_broadcasting(node, x, y, idx)

        if self.set_instead_of_inc:
            x[idx] = y
        else:
            # In Numpy, `x[idx] += y` doesn't work if the same index is present
            # many times: it does it only once.
            np.add.at(x, idx, y)

        output_storage[0][0] = x

    def infer_shape(self, fgraph, node, ishapes):
        x, y, ilist = ishapes
        return [x]

    def R_op(self, inputs, eval_points):
        if None in eval_points[:2]:
            return [None]
        return self.make_node(eval_points[0], eval_points[1], *inputs[2:]).outputs

    def connection_pattern(self, node):
        rval = [[True], [True], [False]]
        return rval

    def grad(self, inputs, grads):
        (g_output,) = grads
        x, y, idx_list = inputs
        if x.dtype in discrete_dtypes:
            # The output dtype is the same as x
            gx = x.zeros_like(dtype=config.floatX)
            if y.dtype in discrete_dtypes:
                gy = y.zeros_like(dtype=config.floatX)
            else:
                gy = y.zeros_like()
        elif x.dtype in complex_dtypes:
            raise NotImplementedError("No support for complex grad yet")
        else:
            if self.set_instead_of_inc:
                gx = advanced_set_subtensor1(g_output, y.zeros_like(), idx_list)
            else:
                gx = g_output
            gy = advanced_subtensor1(g_output, idx_list)
            gy = _sum_grad_over_bcasted_dims(y, gy)

        return [gx, gy, DisconnectedType()()]


advanced_inc_subtensor1 = AdvancedIncSubtensor1()
advanced_set_subtensor1 = AdvancedIncSubtensor1(set_instead_of_inc=True)


def as_index_variable(idx):
    if idx is None:
        return NoneConst.clone()
    if isinstance(idx, slice):
        return make_slice(idx)
    if isinstance(idx, Variable) and isinstance(idx.type, SliceType):
        return idx
    if isinstance(idx, Variable) and isinstance(idx.type, NoneTypeT):
        return idx
    idx = as_tensor_variable(idx)
    if idx.type.dtype not in discrete_dtypes:
        raise TypeError("index must be integers or a boolean mask")
    if idx.type.dtype == "bool" and idx.type.ndim == 0:
        raise NotImplementedError(
            "Boolean scalar indexing not implemented. "
            "Open an issue in https://github.com/pymc-devs/pytensor/issues if you need this behavior."
        )
    return idx


def check_advanced_indexing_dimensions(input, idx_list):
    """
    This function checks if the index list in idx_list is correct.
    If there are any boolean masks, we check if the mask has the
    same shape as the input. This is enforced in NumPy 0.13.0 and
    newer, but not by earlier versions. If the size is not the same,
    this method raises an IndexError.
    """
    dim_seen = 0
    for index in idx_list:
        if index is np.newaxis:
            # skip, does not count as an input dimension
            pass
        elif isinstance(index, np.ndarray) and index.dtype == "bool":
            for i in range(index.ndim):
                if index.shape[i] != input.shape[dim_seen + i]:
                    raise IndexError(
                        "boolean index did not match indexed array "
                        f"along dimension {int(dim_seen + i)}; dimension is "
                        f"{int(input.shape[dim_seen + i])} but "
                        f"corresponding boolean dimension is {index.shape[i]}"
                    )
            dim_seen += index.ndim
        else:
            dim_seen += 1


class AdvancedSubtensor(Op):
    """Implements NumPy's advanced indexing."""

    __props__ = ()

    def make_node(self, x, *index):
        x = as_tensor_variable(x)
        index = tuple(map(as_index_variable, index))

        # We create a fake symbolic shape tuple and identify the broadcast
        # dimensions from the shape result of this entire subtensor operation.
        with config.change_flags(compute_test_value="off"):
            fake_shape = tuple(
                tensor(dtype="int64", shape=()) if s != 1 else 1 for s in x.type.shape
            )

            fake_index = tuple(
                chain.from_iterable(
                    pytensor.tensor.basic.nonzero(idx)
                    if getattr(idx, "ndim", 0) > 0
                    and getattr(idx, "dtype", None) == "bool"
                    else (idx,)
                    for idx in index
                )
            )

            out_shape = tuple(
                i.value if isinstance(i, Constant) else None
                for i in indexed_result_shape(fake_shape, fake_index)
            )

        return Apply(
            self,
            (x, *index),
            [tensor(dtype=x.type.dtype, shape=out_shape)],
        )

    def R_op(self, inputs, eval_points):
        if eval_points[0] is None:
            return [None]
        return self.make_node(eval_points[0], *inputs[1:]).outputs

    def infer_shape(self, fgraph, node, ishapes):
        def is_bool_index(idx):
            return (
                isinstance(idx, np.bool_ | bool)
                or getattr(idx, "dtype", None) == "bool"
            )

        indices = node.inputs[1:]
        index_shapes = []
        for idx, ishape in zip(indices, ishapes[1:], strict=True):
            # Mixed bool indexes are converted to nonzero entries
            shape0_op = Shape_i(0)
            if is_bool_index(idx):
                index_shapes.extend((shape0_op(nz_dim),) for nz_dim in nonzero(idx))
            # The `ishapes` entries for `SliceType`s will be None, and
            # we need to give `indexed_result_shape` the actual slices.
            elif isinstance(getattr(idx, "type", None), SliceType):
                index_shapes.append(idx)
            else:
                index_shapes.append(ishape)

        res_shape = list(
            indexed_result_shape(ishapes[0], index_shapes, indices_are_shapes=True)
        )
        for i, res_dim_length in enumerate(res_shape):
            if res_dim_length is None:
                # This can happen when we have a Slice provided by the user (not a constant nor the result of MakeSlice)
                # We must compute the Op to find its shape
                res_shape[i] = Shape_i(i)(node.out)

        adv_indices = [idx for idx in indices if not is_basic_idx(idx)]
        bool_indices = [idx for idx in adv_indices if is_bool_index(idx)]

        # Special logic when the only advanced index group is of bool type.
        # We can replace the nonzeros by a sum of the whole bool variable.
        if len(bool_indices) == 1 and len(adv_indices) == 1:
            [bool_index] = bool_indices
            # Find the output dim associated with the bool index group
            # Because there are no more advanced index groups, there is exactly
            # one output dim per index variable up to the bool group.
            # Note: Scalar integer indexing counts as advanced indexing.
            start_dim = indices.index(bool_index)
            res_shape[start_dim] = bool_index.sum()

        assert node.outputs[0].ndim == len(res_shape)
        return [res_shape]

    def perform(self, node, inputs, out_):
        (out,) = out_
        check_advanced_indexing_dimensions(inputs[0], inputs[1:])
        rval = inputs[0].__getitem__(tuple(inputs[1:]))
        # When there are no arrays, we are not actually doing advanced
        # indexing, so __getitem__ will not return a copy.
        # Since no view_map is set, we need to copy the returned value
        if not any(
            isinstance(v.type, TensorType) and v.ndim > 0 for v in node.inputs[1:]
        ):
            rval = rval.copy()
        out[0] = rval

    def connection_pattern(self, node):
        rval = [[True], *([False] for _ in node.inputs[1:])]

        return rval

    def grad(self, inputs, grads):
        (gz,) = grads
        x = inputs[0]
        if x.dtype in discrete_dtypes:
            # The output dtype is the same as x
            gx = x.zeros_like(dtype=config.floatX)
        elif x.dtype in complex_dtypes:
            raise NotImplementedError("No support for complex grad yet")
        else:
            gx = x.zeros_like()
        rest = inputs[1:]
        return [advanced_inc_subtensor(gx, gz, *rest)] + [DisconnectedType()()] * len(
            rest
        )

    @staticmethod
    def non_contiguous_adv_indexing(node: Apply) -> bool:
        warnings.warn(
            "Method was renamed to `non_consecutive_adv_indexing`", FutureWarning
        )
        return AdvancedSubtensor.non_consecutive_adv_indexing(node)

    @staticmethod
    def non_consecutive_adv_indexing(node: Apply) -> bool:
        """
        Check if the advanced indexing is non-consecutive (i.e. interrupted by basic indexing).

        This function checks if the advanced indexing is non-consecutive,
        in which case the advanced index dimensions are placed on the left of the
        output array, regardless of their opriginal position.

        See: https://numpy.org/doc/stable/user/basics.indexing.html#combining-advanced-and-basic-indexing


        Parameters
        ----------
        node : Apply
            The node of the AdvancedSubtensor operation.

        Returns
        -------
        bool
            True if the advanced indexing is non-consecutive, False otherwise.
        """
        _, *idxs = node.inputs
        return _non_consecutive_adv_indexing(idxs)


advanced_subtensor = AdvancedSubtensor()


@_vectorize_node.register(AdvancedSubtensor)
def vectorize_advanced_subtensor(op: AdvancedSubtensor, node, *batch_inputs):
    x, *idxs = node.inputs
    batch_x, *batch_idxs = batch_inputs

    x_is_batched = x.type.ndim < batch_x.type.ndim
    idxs_are_batched = any(
        batch_idx.type.ndim > idx.type.ndim
        for batch_idx, idx in zip(batch_idxs, idxs, strict=True)
        if isinstance(batch_idx, TensorVariable)
    )

    if idxs_are_batched or (x_is_batched and op.non_consecutive_adv_indexing(node)):
        # Fallback to Blockwise if idxs are batched or if we have non contiguous advanced indexing
        # which would put the indexed results to the left of the batch dimensions!
        # TODO: Not all cases must be handled by Blockwise, but the logic is complex

        # Blockwise doesn't accept None or Slices types so we raise informative error here
        # TODO: Implement these internally, so Blockwise is always a safe fallback
        if any(not isinstance(idx, TensorVariable) for idx in idxs):
            raise NotImplementedError(
                "Vectorized AdvancedSubtensor with batched indexes or non-consecutive advanced indexing "
                "and slices or newaxis is currently not supported."
            )
        else:
            return vectorize_node_fallback(op, node, batch_x, *batch_idxs)

    # Otherwise we just need to add None slices for every new batch dim
    x_batch_ndim = batch_x.type.ndim - x.type.ndim
    empty_slices = (slice(None),) * x_batch_ndim
    return op.make_node(batch_x, *empty_slices, *batch_idxs)


class AdvancedIncSubtensor(Op):
    """Increments a subtensor using advanced indexing."""

    __props__ = ("inplace", "set_instead_of_inc", "ignore_duplicates")

    def __init__(
        self, inplace=False, set_instead_of_inc=False, ignore_duplicates=False
    ):
        self.set_instead_of_inc = set_instead_of_inc
        self.inplace = inplace
        if inplace:
            self.destroy_map = {0: [0]}
        self.ignore_duplicates = ignore_duplicates

    def __str__(self):
        return (
            "AdvancedSetSubtensor"
            if self.set_instead_of_inc
            else "AdvancedIncSubtensor"
        )

    def make_node(self, x, y, *inputs):
        x = as_tensor_variable(x)
        y = as_tensor_variable(y)

        new_inputs = []
        for inp in inputs:
            if isinstance(inp, list | tuple):
                inp = as_tensor_variable(inp)
            new_inputs.append(inp)
        return Apply(
            self,
            (x, y, *new_inputs),
            [x.type()],
        )

    def perform(self, node, inputs, out_):
        x, y, *indices = inputs

        check_advanced_indexing_dimensions(x, indices)

        (out,) = out_
        if not self.inplace:
            out[0] = x.copy()
        else:
            out[0] = x

        if self.set_instead_of_inc:
            out[0][tuple(indices)] = y
        elif self.ignore_duplicates:
            out[0][tuple(indices)] += y
        else:
            np.add.at(out[0], tuple(indices), y)

    def infer_shape(self, fgraph, node, ishapes):
        return [ishapes[0]]

    def connection_pattern(self, node):
        rval = [[True], [True], *([False] for _ in node.inputs[2:])]

        return rval

    def R_op(self, inputs, eval_points):
        if None in eval_points[:2]:
            return [None]
        return self.make_node(eval_points[0], eval_points[1], *inputs[2:]).outputs

    def grad(self, inpt, output_gradients):
        x, y = inpt[:2]
        idxs = inpt[2:]
        (outgrad,) = output_gradients
        if x.dtype in discrete_dtypes:
            # The output dtype is the same as x
            gx = x.zeros_like(dtype=config.floatX)
            if y.dtype in discrete_dtypes:
                gy = y.zeros_like(dtype=config.floatX)
            else:
                gy = y.zeros_like()
        elif x.dtype in complex_dtypes:
            raise NotImplementedError("No support for complex grad yet")
        else:
            if self.set_instead_of_inc:
                gx = advanced_set_subtensor(outgrad, y.zeros_like(), *idxs)
            else:
                gx = outgrad
            gy = advanced_subtensor(outgrad, *idxs)
            # Make sure to sum gy over the dimensions of y that have been
            # added or broadcasted
            gy = _sum_grad_over_bcasted_dims(y, gy)
        return [gx, gy] + [DisconnectedType()() for _ in idxs]

    @staticmethod
    def non_contiguous_adv_indexing(node: Apply) -> bool:
        warnings.warn(
            "Method was renamed to `non_consecutive_adv_indexing`", FutureWarning
        )
        return AdvancedIncSubtensor.non_consecutive_adv_indexing(node)

    @staticmethod
    def non_consecutive_adv_indexing(node: Apply) -> bool:
        """
        Check if the advanced indexing is non-consecutive (i.e. interrupted by basic indexing).

        This function checks if the advanced indexing is non-consecutive,
        in which case the advanced index dimensions are placed on the left of the
        output array, regardless of their opriginal position.

        See: https://numpy.org/doc/stable/user/basics.indexing.html#combining-advanced-and-basic-indexing


        Parameters
        ----------
        node : Apply
            The node of the AdvancedSubtensor operation.

        Returns
        -------
        bool
            True if the advanced indexing is non-consecutive, False otherwise.
        """
        _, _, *idxs = node.inputs
        return _non_consecutive_adv_indexing(idxs)


advanced_inc_subtensor = AdvancedIncSubtensor()
advanced_set_subtensor = AdvancedIncSubtensor(set_instead_of_inc=True)
advanced_inc_subtensor_nodup = AdvancedIncSubtensor(ignore_duplicates=True)
advanced_set_subtensor_nodup = AdvancedIncSubtensor(
    set_instead_of_inc=True, ignore_duplicates=True
)


def take(a, indices, axis=None, mode="raise"):
    """Take elements from an array along an axis.

    When axis is not None, this function does the same thing as "fancy"
    indexing (indexing arrays using arrays); however, it can be easier to use
    if you need elements along a given axis. A call such as
    ``np.take(arr, indices, axis=3)`` is equivalent to
    ``arr[:,:,:,indices,...]``.

    See `np.take`

    Parameters
    ----------
    a : TensorVariable
        The source array.
    indices : TensorVariable, ndarray, list, tuple
        The indices of the values to extract.
    axis : int, optional
        The axis over which to select values. By default, the flattened
        input array is used.

    """
    a = as_tensor_variable(a)
    indices = as_tensor_variable(indices)

    if not isinstance(axis, int | type(None)):
        raise TypeError("`axis` must be an integer or None")

    if axis is None:
        return advanced_subtensor(a.flatten(), indices)
    elif axis < 0:
        axis += a.ndim

    if mode == "clip":
        indices = clip(indices, 0, a.shape[axis] - 1)
    elif mode == "wrap":
        indices = indices % a.shape[axis]

    full_indices = (slice(None),) * axis + (indices,)

    return a[full_indices]


@_get_vector_length.register(Subtensor)  # type: ignore
def _get_vector_length_Subtensor(op, var):
    # If we take a slice, we know how many elements it will result in
    # TODO: We can cover more `*Subtensor` cases.
    try:
        indices = pytensor.tensor.subtensor.get_idx_list(
            var.owner.inputs, var.owner.op.idx_list
        )
        start = (
            None
            if indices[0].start is None
            else get_scalar_constant_value(indices[0].start)
        )
        stop = (
            None
            if indices[0].stop is None
            else get_scalar_constant_value(indices[0].stop)
        )
        step = (
            None
            if indices[0].step is None
            else get_scalar_constant_value(indices[0].step)
        )

        if start == stop:
            return 0

        arg_len = get_vector_length(var.owner.inputs[0])
        return len(range(*slice(start, stop, step).indices(arg_len)))
    except (ValueError, NotScalarConstantError):
        raise ValueError(f"Length of {var} cannot be determined")


def slice_at_axis(sl: slice, axis: int) -> tuple[slice, ...]:
    """
    Construct tuple of slices to slice an array in the given dimension.

    Copied from numpy.lib.arraypad._slice_at_axis
    https://github.com/numpy/numpy/blob/300096d384046eee479b0c7a70f79e308da52bff/numpy/lib/_arraypad_impl.py#L33

    Parameters
    ----------
    sl : slice
        The slice for the given dimension.
    axis : int
        The axis to which `sl` is applied. All other dimensions are left
        "unsliced".

    Returns
    -------
    sl : tuple of slices
        A tuple with slices matching `shape` in length.

    Examples
    --------

    .. testcode::

        import pytensor.tensor as pt

        s = pt.slice_at_axis(slice(None, 1), 1)
        print(s)

    .. testoutput::

        (slice(None, None, None), slice(None, 1, None), Ellipsis)

    .. testcode::

        x = pt.tensor('x', shape=(None, None, None))
        x_sliced = x[s]

        f = pytensor.function([x], x_sliced)
        x = np.arange(27).reshape(3, 3, 3)
        print(f(x))

    .. testoutput::
        [[[ 0.  1.  2.]]

         [[ 9. 10. 11.]]

         [[18. 19. 20.]]]

    """
    if axis >= 0:
        return (slice(None),) * axis + (sl,) + (...,)  # type: ignore
    else:
        # If axis = -1 we want zero right padding (and so on), so subtract one
        axis = abs(axis) - 1
        return (...,) + (sl,) + (slice(None),) * axis  # type: ignore


def flip(
    arr: TensorVariable, axis: int | tuple[int] | TensorVariable | None = None
) -> TensorVariable:
    """
    Reverse the order of elements in an tensor along the given axis.

    Parameters
    ----------
    arr: TensorVariable
        Input tensor.

    axis: int | tuple[int] | TensorVariable, optional
        Axis or axes along which to flip over. The default is to flip over all of the axes of the input tensor.

    Returns
    -------
    arr: TensorVariable
        A view of `arr` with the entries of axis reversed.

    Examples
    --------

    .. testcode::

        import pytensor
        import pytensor.tensor as pt

        x = pt.tensor('x', shape=(None, None))
        x_flipped = pt.flip(x, axis=0)

        f = pytensor.function([x], x_flipped)
        x = [[1, 2], [3, 4]]
        print(f(x))

    .. testoutput::
        [[3. 4.]
         [1. 2.]]

    """
    if axis is None:
        index = ((slice(None, None, -1)),) * arr.ndim
    else:
        if isinstance(axis, int):
            axis = (axis,)
        index = tuple(
            [
                slice(None, None, -1) if i in axis else slice(None, None, None)
                for i in range(arr.ndim)
            ]
        )

    return cast(TensorVariable, arr[index])


__all__ = [
    "take",
    "flip",
    "slice_at_axis",
    "inc_subtensor",
    "set_subtensor",
]
