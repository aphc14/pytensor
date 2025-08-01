import re

import numpy as np
import pytest

import pytensor
from pytensor import function
from pytensor import tensor as pt
from pytensor.compile.mode import Mode
from pytensor.configdefaults import config
from pytensor.graph import rewrite_graph
from pytensor.graph.basic import Constant, applys_between, equal_computations
from pytensor.npy_2_compat import old_np_unique
from pytensor.raise_op import Assert
from pytensor.tensor import alloc
from pytensor.tensor.elemwise import DimShuffle
from pytensor.tensor.extra_ops import (
    Bartlett,
    CpuContiguous,
    CumOp,
    FillDiagonal,
    FillDiagonalOffset,
    RavelMultiIndex,
    Repeat,
    SearchsortedOp,
    Unique,
    UnravelIndex,
    bartlett,
    bincount,
    broadcast_arrays,
    broadcast_shape,
    broadcast_to,
    compress,
    cpu_contiguous,
    cumprod,
    cumsum,
    diff,
    fill_diagonal,
    fill_diagonal_offset,
    ravel_multi_index,
    repeat,
    searchsorted,
    squeeze,
    to_one_hot,
    unravel_index,
)
from pytensor.tensor.type import (
    TensorType,
    dmatrix,
    dscalar,
    dtensor3,
    fmatrix,
    fvector,
    integer_dtypes,
    iscalar,
    ivector,
    lscalar,
    matrix,
    scalar,
    tensor3,
    vector,
)
from pytensor.utils import LOCAL_BITWIDTH, PYTHON_INT_BITWIDTH
from tests import unittest_tools as utt


def test_cpu_contiguous():
    a = fmatrix("a")
    i = iscalar("i")
    a_val = np.asarray(np.random.random((4, 5)), dtype="float32")
    f = pytensor.function([a, i], cpu_contiguous(a.reshape((5, 4))[::i]))
    topo = f.maker.fgraph.toposort()
    assert any(isinstance(node.op, CpuContiguous) for node in topo)
    assert f(a_val, 1).flags["C_CONTIGUOUS"]
    assert f(a_val, 2).flags["C_CONTIGUOUS"]
    assert f(a_val, 3).flags["C_CONTIGUOUS"]
    # Test the grad:

    utt.verify_grad(cpu_contiguous, [np.random.random((5, 7, 2))])


compatible_types = ("int8", "int16", "int32")
if PYTHON_INT_BITWIDTH == 64:
    compatible_types += ("int64",)


class TestSearchsortedOp(utt.InferShapeTester):
    def setup_method(self):
        super().setup_method()
        self.op_class = SearchsortedOp
        self.op = SearchsortedOp()

        self.x = vector("x")
        self.v = tensor3("v")
        rng = np.random.default_rng(utt.fetch_seed())
        self.a = 30 * rng.random(50).astype(config.floatX)
        self.b = 30 * rng.random((8, 10, 5)).astype(config.floatX)
        self.idx_sorted = np.argsort(self.a).astype("int32")

    def test_searchsortedOp_on_sorted_input(self):
        f = pytensor.function([self.x, self.v], searchsorted(self.x, self.v))
        assert np.allclose(
            np.searchsorted(self.a[self.idx_sorted], self.b),
            f(self.a[self.idx_sorted], self.b),
        )

        sorter = vector("sorter", dtype="int32")
        f = pytensor.function(
            [self.x, self.v, sorter],
            self.x.searchsorted(self.v, sorter=sorter, side="right"),
        )
        assert np.allclose(
            self.a.searchsorted(self.b, sorter=self.idx_sorted, side="right"),
            f(self.a, self.b, self.idx_sorted),
        )

        sa = self.a[self.idx_sorted]
        f = pytensor.function(
            [self.x, self.v], self.x.searchsorted(self.v, side="right")
        )
        assert np.allclose(sa.searchsorted(self.b, side="right"), f(sa, self.b))

    def test_searchsortedOp_wrong_side_kwd(self):
        with pytest.raises(ValueError):
            searchsorted(self.x, self.v, side="asdfa")

    def test_searchsortedOp_on_no_1d_inp(self):
        no_1d = dmatrix("no_1d")
        with pytest.raises(ValueError):
            searchsorted(no_1d, self.v)
        with pytest.raises(ValueError):
            searchsorted(self.x, self.v, sorter=no_1d)

    def test_searchsortedOp_on_float_sorter(self):
        sorter = vector("sorter", dtype="float32")
        with pytest.raises(TypeError):
            searchsorted(self.x, self.v, sorter=sorter)

    @pytest.mark.parametrize("dtype", compatible_types)
    def test_searchsortedOp_on_int_sorter(self, dtype):
        sorter = vector("sorter", dtype=dtype)
        f = pytensor.function(
            [self.x, self.v, sorter],
            searchsorted(self.x, self.v, sorter=sorter),
            allow_input_downcast=True,
        )
        assert np.allclose(
            np.searchsorted(self.a, self.b, sorter=self.idx_sorted),
            f(self.a, self.b, self.idx_sorted),
        )

    def test_searchsortedOp_on_right_side(self):
        f = pytensor.function(
            [self.x, self.v], searchsorted(self.x, self.v, side="right")
        )
        assert np.allclose(
            np.searchsorted(self.a, self.b, side="right"), f(self.a, self.b)
        )

    def test_infer_shape(self):
        # Test using default parameters' value
        self._compile_and_check(
            [self.x, self.v],
            [searchsorted(self.x, self.v)],
            [self.a[self.idx_sorted], self.b],
            self.op_class,
        )

        # Test parameter ``sorter``
        sorter = vector("sorter", dtype="int32")
        self._compile_and_check(
            [self.x, self.v, sorter],
            [searchsorted(self.x, self.v, sorter=sorter)],
            [self.a, self.b, self.idx_sorted],
            self.op_class,
        )

        # Test parameter ``side``
        la = np.ones(10).astype(config.floatX)
        lb = np.ones(shape=(1, 2, 3)).astype(config.floatX)
        self._compile_and_check(
            [self.x, self.v],
            [searchsorted(self.x, self.v, side="right")],
            [la, lb],
            self.op_class,
        )

    def test_grad(self):
        rng = np.random.default_rng(utt.fetch_seed())
        utt.verify_grad(self.op, [self.a[self.idx_sorted], self.b], rng=rng)


class TestCumOp(utt.InferShapeTester):
    def setup_method(self):
        super().setup_method()
        self.op_class = CumOp
        self.op = CumOp()

    def test_cum_op(self):
        x = tensor3("x")
        a = np.random.random((3, 5, 2)).astype(config.floatX)

        # Test axis out of bounds
        with pytest.raises(ValueError):
            cumsum(x, axis=3)
        with pytest.raises(ValueError):
            cumsum(x, axis=-4)
        with pytest.raises(ValueError):
            cumprod(x, axis=3)
        with pytest.raises(ValueError):
            cumprod(x, axis=-4)

        f = pytensor.function([x], [cumsum(x), cumprod(x)])
        s, p = f(a)
        assert np.allclose(np.cumsum(a), s)  # Test axis=None
        assert np.allclose(np.cumprod(a), p)  # Test axis=None

        for axis in range(-len(a.shape), len(a.shape)):
            f = pytensor.function([x], [cumsum(x, axis=axis), cumprod(x, axis=axis)])
            s, p = f(a)
            assert np.allclose(np.cumsum(a, axis=axis), s)
            assert np.allclose(np.cumprod(a, axis=axis), p)

    def test_infer_shape(self):
        x = tensor3("x")
        a = np.random.random((3, 5, 2)).astype(config.floatX)

        # Test axis=None
        self._compile_and_check([x], [self.op(x)], [a], self.op_class)

        for axis in range(-len(a.shape), len(a.shape)):
            self._compile_and_check([x], [cumsum(x, axis=axis)], [a], self.op_class)

    def test_grad(self):
        a = np.random.random((3, 5, 2)).astype(config.floatX)

        utt.verify_grad(self.op_class(mode="add"), [a])  # Test axis=None
        utt.verify_grad(self.op_class(mode="mul"), [a])  # Test axis=None

        for axis in range(-len(a.shape), len(a.shape)):
            utt.verify_grad(self.op_class(axis=axis, mode="add"), [a], eps=4e-4)
            utt.verify_grad(self.op_class(axis=axis, mode="mul"), [a], eps=4e-4)


class TestBinCount(utt.InferShapeTester):
    @pytest.mark.parametrize(
        "dtype",
        (
            "int8",
            "int16",
            "int32",
            "int64",
            "uint8",
            "uint16",
            "uint32",
            "uint64",
        ),
    )
    def test_bincountFn(self, dtype):
        w = vector("w")

        rng = np.random.default_rng(4282)

        def ref(data, w=None, minlength=None):
            size = int(data.max() + 1)
            if minlength:
                size = max(size, minlength)
            if w is not None:
                out = np.zeros(size, dtype=w.dtype)
                for i in range(data.shape[0]):
                    out[data[i]] += w[i]
            else:
                out = np.zeros(size, dtype=a.dtype)
                for i in range(data.shape[0]):
                    out[data[i]] += 1
            return out

        x = vector("x", dtype=dtype)

        a = rng.integers(1, 51, size=(25)).astype(dtype)
        weights = rng.random((25,)).astype(config.floatX)

        f1 = pytensor.function([x], bincount(x))
        f2 = pytensor.function([x, w], bincount(x, weights=w))

        assert np.array_equal(ref(a), f1(a))
        assert np.allclose(ref(a, weights), f2(a, weights))

        f3 = pytensor.function([x], bincount(x, minlength=55))
        f4 = pytensor.function([x], bincount(x, minlength=5))

        assert np.array_equal(ref(a, minlength=55), f3(a))
        assert np.array_equal(ref(a, minlength=5), f4(a))

        # skip the following test when using unsigned ints
        if not dtype.startswith("u"):
            a[0] = -1
            f5 = pytensor.function([x], bincount(x, assert_nonneg=True))
            with pytest.raises(AssertionError):
                f5(a)


class TestDiff(utt.InferShapeTester):
    def test_basic(self):
        rng = np.random.default_rng(4282)

        x = matrix("x")

        a = rng.random((30, 50)).astype(config.floatX)

        f = pytensor.function([x], diff(x))
        assert np.allclose(np.diff(a), f(a))

    @pytest.mark.parametrize("axis", (-2, -1, 0, 1))
    @pytest.mark.parametrize("n", (0, 1, 2, 30, 31))
    def test_perform(self, axis, n):
        rng = np.random.default_rng(4282)

        x = matrix("x")

        a = rng.random((30, 50)).astype(config.floatX)

        f = pytensor.function([x], diff(x))
        assert np.allclose(np.diff(a), f(a))

        g = pytensor.function([x], diff(x, n=n, axis=axis))
        assert np.allclose(np.diff(a, n=n, axis=axis), g(a))

    @pytest.mark.parametrize(
        "x_type",
        (
            pt.TensorType("float64", shape=(None, None)),
            pt.TensorType("float64", shape=(None, 30)),
            pt.TensorType("float64", shape=(10, None)),
            pt.TensorType("float64", shape=(10, 30)),
        ),
    )
    @pytest.mark.parametrize("axis", (-2, -1, 0, 1))
    @pytest.mark.parametrize("n", (0, 1, 2, 10, 11))
    def test_output_type(self, x_type, axis, n):
        x = x_type("x")
        x_test = np.empty((10, 30))
        out = diff(x, n=n, axis=axis)
        out_test = np.diff(x_test, n=n, axis=axis)
        for i in range(2):
            if x.type.shape[i] is None:
                assert out.type.shape[i] is None
            else:
                assert out.type.shape[i] == out_test.shape[i]


class TestSqueeze(utt.InferShapeTester):
    def setup_method(self):
        super().setup_method()
        self.op = squeeze

    @pytest.mark.parametrize(
        "shape, var_shape",
        zip(
            [(1, 3), (1, 2, 3), (1, 5, 1, 1, 6)],
            [
                [1, None],
                [1, None, None],
                [1, None, 1, 1, None],
            ],
            strict=True,
        ),
    )
    def test_op(self, shape, var_shape):
        data = np.random.random(size=shape).astype(config.floatX)
        variable = TensorType(config.floatX, shape=var_shape)()

        f = pytensor.function([variable], self.op(variable))

        expected = np.squeeze(data)
        tested = f(data)

        assert tested.shape == expected.shape
        assert np.allclose(tested, expected)

    @pytest.mark.parametrize(
        "shape, var_shape",
        zip(
            [(1, 3), (1, 2, 3), (1, 5, 1, 1, 6)],
            [
                [1, None],
                [1, None, None],
                [1, None, 1, 1, None],
            ],
            strict=True,
        ),
    )
    def test_infer_shape(self, shape, var_shape):
        data = np.random.random(size=shape).astype(config.floatX)
        variable = TensorType(config.floatX, shape=var_shape)()

        self._compile_and_check(
            [variable], [self.op(variable)], [data], DimShuffle, warn=False
        )

    @pytest.mark.parametrize(
        "shape, broadcast",
        zip(
            [(1, 3), (1, 2, 3), (1, 5, 1, 1, 6)],
            [
                [True, False],
                [True, False, False],
                [True, False, True, True, False],
            ],
            strict=True,
        ),
    )
    def test_grad(self, shape, broadcast):
        data = np.random.random(size=shape).astype(config.floatX)
        utt.verify_grad(self.op, [data])

    @pytest.mark.parametrize(
        "shape, var_shape",
        zip(
            [(1, 3), (1, 2, 3), (1, 5, 1, 1, 6)],
            [
                [1, None],
                [1, None, None],
                [1, None, 1, 1, None],
            ],
            strict=True,
        ),
    )
    def test_var_interface(self, shape, var_shape):
        # same as test_op, but use a_pytensor_var.squeeze.
        data = np.random.random(size=shape).astype(config.floatX)
        variable = TensorType(config.floatX, shape=var_shape)()

        f = pytensor.function([variable], variable.squeeze())

        expected = np.squeeze(data)
        tested = f(data)

        assert tested.shape == expected.shape
        assert np.allclose(tested, expected)

    def test_axis(self):
        variable = TensorType(config.floatX, shape=(None, 1, None))()
        res = squeeze(variable, axis=1)

        assert res.broadcastable == (False, False)

        variable = TensorType(config.floatX, shape=(None, 1, None))()
        res = squeeze(variable, axis=(1,))

        assert res.broadcastable == (False, False)

        variable = TensorType(config.floatX, shape=(None, 1, None, 1))()
        res = squeeze(variable, axis=(1, 3))

        assert res.broadcastable == (False, False)

        variable = TensorType(config.floatX, shape=(1, None, 1, None, 1))()
        res = squeeze(variable, axis=(0, -1))

        assert res.broadcastable == (False, True, False)

    def test_scalar_input(self):
        x = pt.scalar("x")

        assert squeeze(x, axis=(0,)).eval({x: 5}) == 5

        with pytest.raises(
            np.exceptions.AxisError,
            match=re.escape("axis (1,) is out of bounds for array of dimension 0"),
        ):
            squeeze(x, axis=1)

    def test_invalid_input(self):
        x = pt.vector("x")
        axis = 0

        f = pytensor.function([x], pt.squeeze(x, axis))

        # Test that we allow squeezing of valid non-broadcastable dimension
        assert f([0]) == 0

        # Test that we cannot squeeze dimensions whose length is greater than 1
        with pytest.raises(ValueError):
            f([0, 1, 2])


class TestCompress(utt.InferShapeTester):
    def setup_method(self):
        super().setup_method()
        self.op = compress

    @pytest.mark.parametrize(
        "axis, cond, shape",
        zip(
            [None, -1, 0, 0, 0, 1],
            [
                [1, 0, 1, 0, 0, 1],
                [0, 1, 1, 0],
                [0, 1, 1, 0],
                [],
                [0, 0, 0, 0],
                [1, 1, 0, 1, 0],
            ],
            [(2, 3), (4, 3), (4, 3), (4, 3), (4, 3), (3, 5)],
            strict=True,
        ),
    )
    def test_op(self, axis, cond, shape):
        cond_var = ivector()
        data = np.random.random(size=shape).astype(config.floatX)
        data_var = matrix()

        f = pytensor.function(
            [cond_var, data_var], self.op(cond_var, data_var, axis=axis)
        )

        expected = np.compress(cond, data, axis=axis)
        tested = f(cond, data)

        assert tested.shape == expected.shape
        assert np.allclose(tested, expected)


class TestRepeat(utt.InferShapeTester):
    def _possible_axis(self, ndim):
        return [None] + list(range(ndim)) + [-i for i in range(ndim)]

    def setup_method(self):
        super().setup_method()
        self.op_class = Repeat
        self.op = Repeat()
        # uint64 always fails
        # int64 and uint32 also fail if python int are 32-bit
        if LOCAL_BITWIDTH == 64:
            self.numpy_unsupported_dtypes = ("uint64",)
        if LOCAL_BITWIDTH == 32:
            self.numpy_unsupported_dtypes = ("uint32", "int64", "uint64")

    @pytest.mark.parametrize("ndim", [1, 3])
    @pytest.mark.parametrize("dtype", integer_dtypes)
    def test_basic(self, ndim, dtype):
        rng = np.random.default_rng(4282)

        x = TensorType(config.floatX, (None,) * ndim)()
        a = rng.random((10,) * ndim).astype(config.floatX)

        for axis in self._possible_axis(ndim):
            r_var = scalar(dtype=dtype)
            r = np.asarray(3, dtype=dtype)
            if dtype == "uint64" or (
                dtype in self.numpy_unsupported_dtypes and r_var.ndim == 1
            ):
                with pytest.raises(TypeError):
                    repeat(x, r_var, axis=axis)
            else:
                f = pytensor.function([x, r_var], repeat(x, r_var, axis=axis))
                assert np.allclose(np.repeat(a, r, axis=axis), f(a, r))

                r_var = vector(dtype=dtype)

                if axis is None:
                    r = rng.integers(1, 6, size=a.size).astype(dtype)
                else:
                    r = rng.integers(1, 6, size=(10,)).astype(dtype)

                if dtype in self.numpy_unsupported_dtypes and r_var.ndim == 1:
                    with pytest.raises(TypeError):
                        repeat(x, r_var, axis=axis)
                else:
                    f = pytensor.function([x, r_var], repeat(x, r_var, axis=axis))
                    assert np.allclose(np.repeat(a, r, axis=axis), f(a, r))

                # check when r is a list of single integer, e.g. [3].
                r = rng.integers(1, 11, size=()).astype(dtype) + 2

                f = pytensor.function([x], repeat(x, [r], axis=axis))
                assert np.allclose(np.repeat(a, r, axis=axis), f(a))
                assert not any(
                    isinstance(n.op, Repeat) for n in f.maker.fgraph.toposort()
                )

                # check when r is  pytensor tensortype that broadcastable is (True,)
                r_var = TensorType(dtype=dtype, shape=(1,))()
                r = rng.integers(1, 6, size=(1,)).astype(dtype)
                f = pytensor.function([x, r_var], repeat(x, r_var, axis=axis))
                assert np.allclose(np.repeat(a, r[0], axis=axis), f(a, r))
                assert not any(
                    isinstance(n.op, Repeat) for n in f.maker.fgraph.toposort()
                )

    @pytest.mark.parametrize("ndim", [1, 3])
    @pytest.mark.parametrize("dtype", ["int8", "uint8", "uint64"])
    def test_infer_shape(self, ndim, dtype):
        rng = np.random.default_rng(4282)

        x = TensorType(config.floatX, shape=(None,) * ndim)()
        shp = (np.arange(ndim) + 1) * 3
        a = rng.random(shp).astype(config.floatX)

        for axis in self._possible_axis(ndim):
            if axis is not None and axis < 0:
                # Operator does not support negative axis
                continue

            r_var = scalar(dtype=dtype)
            r = np.asarray(3, dtype=dtype)
            if dtype in self.numpy_unsupported_dtypes:
                r_var = vector(dtype=dtype)
                with pytest.raises(TypeError):
                    repeat(x, r_var)
            else:
                self._compile_and_check(
                    [x, r_var],
                    [Repeat(axis=axis)(x, r_var)],
                    [a, r],
                    self.op_class,
                )

                r_var = vector(dtype=dtype)
                if axis is None:
                    r = rng.integers(1, 6, size=a.size).astype(dtype)
                elif a.size > 0:
                    r = rng.integers(1, 6, size=a.shape[axis]).astype(dtype)
                else:
                    r = rng.integers(1, 6, size=(10,)).astype(dtype)

                self._compile_and_check(
                    [x, r_var],
                    [Repeat(axis=axis)(x, r_var)],
                    [a, r],
                    self.op_class,
                )

    @pytest.mark.parametrize("x_ndim", [2, 3], ids=lambda x: f"x_ndim={x}")
    @pytest.mark.parametrize("repeats_ndim", [0, 1], ids=lambda r: f"repeats_ndim={r}")
    @pytest.mark.parametrize("axis", [None, 0, 1], ids=lambda a: f"axis={a}")
    def test_grad(self, x_ndim, repeats_ndim, axis):
        rng = np.random.default_rng(
            [653, x_ndim, 2 if axis is None else axis, repeats_ndim]
        )
        x_test = rng.normal(size=np.arange(3, 3 + x_ndim))
        if repeats_ndim == 0:
            repeats_size = ()
        else:
            repeats_size = (x_test.shape[axis] if axis is not None else x_test.size,)
        repeats = rng.integers(1, 6, size=repeats_size)
        utt.verify_grad(
            lambda x: Repeat(axis=axis)(x, repeats),
            [x_test],
        )

    def test_broadcastable(self):
        x = TensorType(config.floatX, shape=(None, 1, None))()
        r = Repeat(axis=1)(x, 2)
        assert r.broadcastable == (False, False, False)
        r = Repeat(axis=1)(x, 1)
        assert r.broadcastable == (False, True, False)
        r = Repeat(axis=0)(x, 2)
        assert r.broadcastable == (False, True, False)


class TestBartlett(utt.InferShapeTester):
    def setup_method(self):
        super().setup_method()
        self.op_class = Bartlett
        self.op = bartlett

    def test_perform(self):
        x = lscalar()
        f = function([x], self.op(x))
        M = np.random.default_rng().integers(3, 51, size=())
        assert np.allclose(f(M), np.bartlett(M))
        assert np.allclose(f(0), np.bartlett(0))
        assert np.allclose(f(-1), np.bartlett(-1))
        b = np.array([17], dtype="uint8")
        assert np.allclose(f(b[0]), np.bartlett(b[0]))

    def test_infer_shape(self):
        x = lscalar()
        self._compile_and_check(
            [x],
            [self.op(x)],
            [np.random.default_rng().integers(3, 51, size=())],
            self.op_class,
        )
        self._compile_and_check([x], [self.op(x)], [0], self.op_class)
        self._compile_and_check([x], [self.op(x)], [1], self.op_class)


class TestFillDiagonal(utt.InferShapeTester):
    def setup_method(self):
        super().setup_method()
        self.op_class = FillDiagonal
        self.op = fill_diagonal

    @pytest.mark.parametrize("shp", [(8, 8), (5, 8), (8, 5)])
    def test_perform(self, shp):
        rng = np.random.default_rng(43)

        x = matrix()
        y = scalar()
        f = function([x, y], fill_diagonal(x, y))
        a = rng.random(shp).astype(config.floatX)
        val = rng.random(dtype=config.floatX)
        out = f(a, val)
        # We can't use np.fill_diagonal as it is bugged.
        assert np.allclose(np.diag(out), val)
        assert (out == val).sum() == min(a.shape)

    def test_perform_3d(self):
        rng = np.random.default_rng(43)
        a = rng.random((3, 3, 3)).astype(config.floatX)
        x = tensor3()
        y = scalar()
        f = function([x, y], fill_diagonal(x, y))
        val = rng.random(dtype=config.floatX) + 10
        out = f(a, val)
        # We can't use np.fill_diagonal as it is bugged.
        assert out[0, 0, 0] == val
        assert out[1, 1, 1] == val
        assert out[2, 2, 2] == val
        assert (out == val).sum() == min(a.shape)

    @pytest.mark.slow
    def test_gradient(self):
        rng = np.random.default_rng(43)
        utt.verify_grad(
            fill_diagonal,
            [rng.random((5, 8)), rng.random()],
            n_tests=1,
            rng=rng,
        )
        utt.verify_grad(
            fill_diagonal,
            [rng.random((8, 5)), rng.random()],
            n_tests=1,
            rng=rng,
        )

    def test_infer_shape(self):
        rng = np.random.default_rng(43)
        z = dtensor3()
        x = dmatrix()
        y = dscalar()
        self._compile_and_check(
            [x, y],
            [self.op(x, y)],
            [rng.random((8, 5)), rng.random()],
            self.op_class,
        )
        self._compile_and_check(
            [z, y],
            [self.op(z, y)],
            # must be square when nd>2
            [rng.random((8, 8, 8)), rng.random()],
            self.op_class,
            warn=False,
        )


class TestFillDiagonalOffset(utt.InferShapeTester):
    def setup_method(self):
        super().setup_method()
        self.op_class = FillDiagonalOffset
        self.op = fill_diagonal_offset

    @pytest.mark.parametrize("test_offset", (-5, -4, -1, 0, 1, 4, 5))
    @pytest.mark.parametrize("shp", [(8, 8), (5, 8), (8, 5), (5, 5)])
    def test_perform(self, test_offset, shp):
        rng = np.random.default_rng(43)

        x = matrix()
        y = scalar()
        z = iscalar()

        f = function([x, y, z], fill_diagonal_offset(x, y, z))
        a = rng.random(shp).astype(config.floatX)
        val = rng.random(dtype=config.floatX)
        out = f(a, val, test_offset)
        # We can't use np.fill_diagonal as it is bugged.
        assert np.allclose(np.diag(out, test_offset), val)
        if test_offset >= 0:
            assert (out == val).sum() == min(min(a.shape), a.shape[1] - test_offset)
        else:
            assert (out == val).sum() == min(min(a.shape), a.shape[0] + test_offset)

    @pytest.mark.parametrize("test_offset", (-5, -4, -1, 0, 1, 4, 5))
    def test_gradient(self, test_offset):
        rng = np.random.default_rng(43)

        # input 'offset' will not be tested
        def fill_diagonal_with_fix_offset(a, val):
            return fill_diagonal_offset(a, val, test_offset)

        utt.verify_grad(
            fill_diagonal_with_fix_offset,
            [rng.random((5, 8)), rng.random()],
            n_tests=1,
            rng=rng,
        )
        utt.verify_grad(
            fill_diagonal_with_fix_offset,
            [rng.random((8, 5)), rng.random()],
            n_tests=1,
            rng=rng,
        )
        utt.verify_grad(
            fill_diagonal_with_fix_offset,
            [rng.random((5, 5)), rng.random()],
            n_tests=1,
            rng=rng,
        )

    @pytest.mark.parametrize("test_offset", (-5, -4, -1, 0, 1, 4, 5))
    def test_infer_shape(self, test_offset):
        rng = np.random.default_rng(43)
        x = dmatrix()
        y = dscalar()
        z = iscalar()
        self._compile_and_check(
            [x, y, z],
            [self.op(x, y, z)],
            [rng.random((8, 5)), rng.random(), test_offset],
            self.op_class,
        )
        self._compile_and_check(
            [x, y, z],
            [self.op(x, y, z)],
            [rng.random((5, 8)), rng.random(), test_offset],
            self.op_class,
        )


def test_to_one_hot():
    v = ivector()
    o = to_one_hot(v, 10)
    f = pytensor.function([v], o)
    out = f([1, 2, 3, 5, 6])
    assert out.dtype == config.floatX
    assert np.allclose(
        out,
        [
            [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        ],
    )

    v = ivector()
    o = to_one_hot(v, 10, dtype="int32")
    f = pytensor.function([v], o)
    out = f([1, 2, 3, 5, 6])
    assert out.dtype == "int32"
    assert np.allclose(
        out,
        [
            [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        ],
    )


class TestUnique(utt.InferShapeTester):
    def setup_method(self):
        super().setup_method()
        self.op_params = [
            (False, False, False),
            (True, False, False),
            (False, True, False),
            (True, True, False),
            (False, False, True),
            (True, False, True),
            (False, True, True),
            (True, True, True),
        ]

    @pytest.mark.parametrize(
        ("x", "inp", "axis"),
        [
            (vector(), np.asarray([2, 1, 3, 2], dtype=config.floatX), None),
            (matrix(), np.asarray([[2, 1], [3, 2], [2, 1]], dtype=config.floatX), None),
            (vector(), np.asarray([2, 1, 3, 2], dtype=config.floatX), 0),
            (matrix(), np.asarray([[2, 1], [3, 2], [2, 1]], dtype=config.floatX), 0),
            (vector(), np.asarray([2, 1, 3, 2], dtype=config.floatX), -1),
            (matrix(), np.asarray([[2, 1], [3, 2], [2, 1]], dtype=config.floatX), -1),
        ],
    )
    def test_basic_vector(self, x, inp, axis):
        list_outs_expected = [
            old_np_unique(inp, axis=axis),
            old_np_unique(inp, True, axis=axis),
            old_np_unique(inp, False, True, axis=axis),
            old_np_unique(inp, True, True, axis=axis),
            old_np_unique(inp, False, False, True, axis=axis),
            old_np_unique(inp, True, False, True, axis=axis),
            old_np_unique(inp, False, True, True, axis=axis),
            old_np_unique(inp, True, True, True, axis=axis),
        ]
        for params, outs_expected in zip(
            self.op_params, list_outs_expected, strict=True
        ):
            out = pt.unique(x, *params, axis=axis)
            f = pytensor.function(inputs=[x], outputs=out)
            outs = f(inp)
            for out, out_exp in zip(outs, outs_expected, strict=True):
                utt.assert_allclose(out, out_exp)

    @pytest.mark.parametrize(
        ("x", "inp", "axis"),
        [
            (vector(), np.asarray([2, 1, 3, 2], dtype=config.floatX), None),
            (matrix(), np.asarray([[2, 1], [3, 2], [2, 1]], dtype=config.floatX), None),
            (vector(), np.asarray([2, 1, 3, 2], dtype=config.floatX), 0),
            (matrix(), np.asarray([[2, 1], [3, 2], [2, 1]], dtype=config.floatX), 0),
            (vector(), np.asarray([2, 1, 3, 2], dtype=config.floatX), -1),
            (matrix(), np.asarray([[2, 1], [3, 2], [2, 1]], dtype=config.floatX), -1),
        ],
    )
    def test_infer_shape(self, x, inp, axis):
        for params in self.op_params:
            if not params[1]:
                continue
            if params[0]:
                f = pt.unique(x, *params, axis=axis)[2]
            else:
                f = pt.unique(x, *params, axis=axis)[1]
            self._compile_and_check(
                [x],
                [f],
                [inp],
                Unique,
            )


class TestUnravelIndex(utt.InferShapeTester):
    def test_unravel_index(self):
        def check(shape, index_ndim, order):
            indices = np.arange(np.prod(shape))
            # test with scalars and higher-dimensional indices
            if index_ndim == 0:
                indices = indices[-1]
            elif index_ndim == 2:
                indices = indices[:, np.newaxis]
            indices_symb = pytensor.shared(indices)

            # reference result
            ref = np.unravel_index(indices, shape, order=order)

            def fn(i, d):
                return function([], unravel_index(i, d, order=order))

            # shape given as a tuple
            f_array_tuple = fn(indices, shape)
            f_symb_tuple = fn(indices_symb, shape)
            np.testing.assert_equal(ref, f_array_tuple())
            np.testing.assert_equal(ref, f_symb_tuple())

            # shape given as an array
            shape_array = np.array(shape)
            f_array_array = fn(indices, shape_array)
            np.testing.assert_equal(ref, f_array_array())

            # shape given as a shared PyTensor variable with static shape
            shape_symb = pytensor.shared(
                shape_array, shape=shape_array.shape, strict=True
            )
            f_array_symb = fn(indices, shape_symb)
            np.testing.assert_equal(ref, f_array_symb())

            # shape given as a Shape op (unravel_index will use get_vector_length
            # to infer the number of dimensions)
            indexed_array = pytensor.shared(np.random.uniform(size=shape_array))
            f_array_shape = fn(indices, indexed_array.shape)
            np.testing.assert_equal(ref, f_array_shape())

            # shape testing
            self._compile_and_check(
                [],
                unravel_index(indices, shape_symb, order=order),
                [],
                UnravelIndex,
            )

        for order in ("C", "F"):
            for index_ndim in (0, 1, 2):
                check((3,), index_ndim, order)
                check((3, 4), index_ndim, order)
                check((3, 4, 5), index_ndim, order)

        # must specify ndim if length of dims is not fixed
        with pytest.raises(ValueError):
            unravel_index(ivector(), ivector())

        # must provide integers
        with pytest.raises(TypeError):
            unravel_index(fvector(), (3, 4))
        with pytest.raises(TypeError):
            unravel_index((3, 4), (3.4, 3.2))

        # dims must be a 1D sequence
        with pytest.raises(TypeError):
            unravel_index((3, 4), 3)
        with pytest.raises(TypeError):
            unravel_index((3, 4), ((3, 4),))


class TestRavelMultiIndex(utt.InferShapeTester):
    def test_ravel_multi_index(self):
        def check(shape, index_ndim, mode, order):
            multi_index = np.unravel_index(
                np.arange(np.prod(shape)), shape, order=order
            )
            # create some invalid indices to test the mode
            if mode in ("wrap", "clip"):
                multi_index = (multi_index[0] - 1,) + multi_index[1:]
            # test with scalars and higher-dimensional indices
            if index_ndim == 0:
                multi_index = tuple(i[-1] for i in multi_index)
            elif index_ndim == 2:
                multi_index = tuple(i[:, np.newaxis] for i in multi_index)
            multi_index_symb = [pytensor.shared(i) for i in multi_index]

            # reference result
            ref = np.ravel_multi_index(multi_index, shape, mode, order)

            def fn(mi, s):
                return function([], ravel_multi_index(mi, s, mode, order))

            # shape given as a tuple
            f_array_tuple = fn(multi_index, shape)
            f_symb_tuple = fn(multi_index_symb, shape)
            np.testing.assert_equal(ref, f_array_tuple())
            np.testing.assert_equal(ref, f_symb_tuple())

            # shape given as an array
            shape_array = np.array(shape)
            f_array_array = fn(multi_index, shape_array)
            np.testing.assert_equal(ref, f_array_array())

            # shape given as an PyTensor variable
            shape_symb = pytensor.shared(shape_array)
            f_array_symb = fn(multi_index, shape_symb)
            np.testing.assert_equal(ref, f_array_symb())

            # shape testing
            self._compile_and_check(
                [],
                [ravel_multi_index(multi_index, shape_symb, mode, order)],
                [],
                RavelMultiIndex,
            )

        for mode in ("raise", "wrap", "clip"):
            for order in ("C", "F"):
                for index_ndim in (0, 1, 2):
                    check((3,), index_ndim, mode, order)
                    check((3, 4), index_ndim, mode, order)
                    check((3, 4, 5), index_ndim, mode, order)

        # must provide integers
        with pytest.raises(TypeError):
            ravel_multi_index((fvector(), ivector()), (3, 4))
        with pytest.raises(TypeError):
            ravel_multi_index(((3, 4), ivector()), (3.4, 3.2))

        # dims must be a 1D sequence
        with pytest.raises(TypeError):
            ravel_multi_index(((3, 4),), ((3, 4),))


def test_broadcast_shape_basic():
    def shape_tuple(x, use_bcast=True):
        if use_bcast:
            return tuple(
                s if not bcast else 1
                for s, bcast in zip(tuple(x.shape), x.broadcastable, strict=True)
            )
        else:
            return tuple(s for s in tuple(x.shape))

    x = np.array([[1], [2], [3]])
    y = np.array([4, 5, 6])
    b = np.broadcast(x, y)
    x_at = pt.as_tensor_variable(x)
    y_at = pt.as_tensor_variable(y)
    b_at = broadcast_shape(x_at, y_at)
    assert np.array_equal([z.eval() for z in b_at], b.shape)
    # Now, we try again using shapes as the inputs
    #
    # This case also confirms that a broadcast dimension will
    # broadcast against a non-broadcast dimension when they're
    # both symbolic (i.e. we couldn't obtain constant values).
    b_at = broadcast_shape(
        shape_tuple(x_at, use_bcast=False),
        shape_tuple(y_at, use_bcast=False),
        arrays_are_shapes=True,
    )
    assert any(
        isinstance(node.op, Assert) for node in applys_between([x_at, y_at], b_at)
    )
    with pytest.raises(AssertionError):
        assert np.array_equal([z.eval() for z in b_at], b.shape)
    # But fine if we allow_runtime_broadcast
    b_at = broadcast_shape(
        shape_tuple(x_at, use_bcast=False),
        shape_tuple(y_at, use_bcast=False),
        arrays_are_shapes=True,
        allow_runtime_broadcast=True,
    )
    assert np.array_equal([z.eval() for z in b_at], b.shape)
    # Or if static bcast is known
    b_at = broadcast_shape(shape_tuple(x_at), shape_tuple(y_at), arrays_are_shapes=True)
    assert np.array_equal([z.eval() for z in b_at], b.shape)

    x = np.array([1, 2, 3])
    y = np.array([4, 5, 6])
    b = np.broadcast(x, y)
    x_at = pt.as_tensor_variable(x)
    y_at = pt.as_tensor_variable(y)
    b_at = broadcast_shape(x_at, y_at)
    assert np.array_equal([z.eval() for z in b_at], b.shape)
    b_at = broadcast_shape(shape_tuple(x_at), shape_tuple(y_at), arrays_are_shapes=True)
    assert np.array_equal([z.eval() for z in b_at], b.shape)

    x = np.empty((1, 2, 3))
    y = np.array(1)
    b = np.broadcast(x, y)
    x_at = pt.as_tensor_variable(x)
    y_at = pt.as_tensor_variable(y)
    b_at = broadcast_shape(x_at, y_at)
    assert b_at[0].value == 1
    assert np.array_equal([z.eval() for z in b_at], b.shape)
    b_at = broadcast_shape(shape_tuple(x_at), shape_tuple(y_at), arrays_are_shapes=True)
    assert np.array_equal([z.eval() for z in b_at], b.shape)

    x = np.empty((2, 1, 3))
    y = np.empty((2, 1, 1))
    b = np.broadcast(x, y)
    x_at = pt.as_tensor_variable(x)
    y_at = pt.as_tensor_variable(y)
    b_at = broadcast_shape(x_at, y_at)
    assert b_at[1].value == 1
    assert np.array_equal([z.eval() for z in b_at], b.shape)
    b_at = broadcast_shape(shape_tuple(x_at), shape_tuple(y_at), arrays_are_shapes=True)
    assert np.array_equal([z.eval() for z in b_at], b.shape)

    x1_shp_at = iscalar("x1")
    x2_shp_at = iscalar("x2")
    y1_shp_at = iscalar("y1")
    x_shapes = (1, x1_shp_at, x2_shp_at)
    x_at = pt.ones(x_shapes)
    y_shapes = (y1_shp_at, 1, x2_shp_at)
    y_at = pt.ones(y_shapes)
    b_at = broadcast_shape(x_at, y_at)
    res = pt.as_tensor(b_at).eval(
        {
            x1_shp_at: 10,
            x2_shp_at: 4,
            y1_shp_at: 2,
        }
    )
    assert np.array_equal(res, (2, 10, 4))

    y_shapes = (y1_shp_at, 1, y1_shp_at)
    y_at = pt.ones(y_shapes)
    b_at = broadcast_shape(x_at, y_at)
    assert isinstance(b_at[-1].owner.op, Assert)

    # N.B. Shared variable shape values shouldn't be treated as constants,
    # because they can change.
    s = pytensor.shared(1)
    b_at = broadcast_shape((s, 2), (2, 1), arrays_are_shapes=True)
    assert isinstance(b_at[0].owner.op, Assert)


def test_broadcast_shape_constants():
    """Make sure `broadcast_shape` uses constants when it can."""
    x1_shp_at = iscalar("x1")
    y2_shp_at = iscalar("y2")
    b_at = broadcast_shape((x1_shp_at, 2), (3, y2_shp_at), arrays_are_shapes=True)
    assert len(b_at) == 2
    assert isinstance(b_at[0].owner.op, Assert)
    assert b_at[0].owner.inputs[0].value.item() == 3
    assert isinstance(b_at[1].owner.op, Assert)
    assert b_at[1].owner.inputs[0].value.item() == 2

    b_at = broadcast_shape((1, 2), (3, 2), arrays_are_shapes=True)
    assert len(b_at) == 2
    assert all(isinstance(x, Constant) for x in b_at)
    assert b_at[0].value.item() == 3
    assert b_at[1].value.item() == 2

    b_at = broadcast_shape((1,), (1, 1), arrays_are_shapes=True)
    assert len(b_at) == 2
    assert all(isinstance(x, Constant) for x in b_at)
    assert b_at[0].value.item() == 1
    assert b_at[1].value.item() == 1

    b_at = broadcast_shape((1,), (1,), arrays_are_shapes=True)
    assert len(b_at) == 1
    assert all(isinstance(x, Constant) for x in b_at)
    assert b_at[0].value.item() == 1


@pytest.mark.parametrize(
    ("s1_vals", "s2_vals", "exp_res"),
    [
        ((2, 2), (1, 2), AssertionError),
        ((0, 2), (1, 2), AssertionError),
        ((1, 2, 1), (2, 1, 2, 1), (2, 1, 2, 1)),
    ],
)
def test_broadcast_shape_symbolic(s1_vals, s2_vals, exp_res):
    s1s = pt.lscalars(len(s1_vals))
    eval_point = {}
    for s, s_val in zip(s1s, s1_vals, strict=True):
        eval_point[s] = s_val
        s.tag.test_value = s_val

    s2s = pt.lscalars(len(s2_vals))
    for s, s_val in zip(s2s, s2_vals, strict=True):
        eval_point[s] = s_val
        s.tag.test_value = s_val

    res = broadcast_shape(s1s, s2s, arrays_are_shapes=True)
    res = pt.as_tensor(res)

    if exp_res is AssertionError:
        with pytest.raises(AssertionError):
            res.eval(eval_point)
    else:
        assert tuple(res.eval(eval_point)) == exp_res


def test_broadcast_shape_symbolic_one_symbolic():
    """Test case for a constant non-broadcast shape and a symbolic shape."""
    one_at = pt.as_tensor(1, dtype=np.int64)
    three_at = pt.as_tensor(3, dtype=np.int64)
    int_div = one_at / one_at

    assert int_div.owner.op == pt.true_div

    index_shapes = [
        (one_at, one_at, three_at),
        (one_at, int_div, one_at),
        (one_at, one_at, int_div),
    ]

    res_shape = broadcast_shape(*index_shapes, arrays_are_shapes=True)
    res_shape = rewrite_graph(res_shape)
    assert res_shape[0].data == 1
    assert res_shape[1].data == 1
    with pytest.raises(AssertionError, match="Could not broadcast dimensions"):
        # broadcast_shape doesn't treat int_div as a constant 1
        res_shape[2].eval()

    res_shape = broadcast_shape(
        *index_shapes, arrays_are_shapes=True, allow_runtime_broadcast=True
    )
    res_shape = rewrite_graph(res_shape)
    assert res_shape[0].data == 1
    assert res_shape[1].data == 1
    assert res_shape[2].data == 3


def test_broadcast_to():
    x = vector("x")
    y1 = scalar(dtype="int64")
    y2 = scalar(dtype="int64")

    assert equal_computations(
        [broadcast_to(x, (y1, y2))],
        [alloc(x, y1, y2)],
    )


def test_broadcast_arrays():
    x, y = pt.tensor(shape=(1,), dtype="float64"), pt.dmatrix()
    x_bcast, y_bcast = broadcast_arrays(x, y)

    py_mode = Mode("py", None)
    bcast_fn = function([x, y], [x_bcast, y_bcast], mode=py_mode)

    x_val = np.array([1.0], dtype=np.float64)
    y_val = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64)
    x_bcast_val, y_bcast_val = bcast_fn(x_val, y_val)
    x_bcast_exp, y_bcast_exp = np.broadcast_arrays(x_val, y_val)

    assert np.array_equal(x_bcast_val, x_bcast_exp)
    assert np.array_equal(y_bcast_val, y_bcast_exp)


@pytest.mark.parametrize(
    "op",
    ["linspace", "logspace", "geomspace"],
    ids=["linspace", "logspace", "geomspace"],
)
@pytest.mark.parametrize(
    "dtype", [None, "int64", "floatX"], ids=[None, "int64", "floatX"]
)
@pytest.mark.parametrize(
    "start, stop, num_samples, endpoint, axis",
    [
        (1, 10, 50, True, 0),
        (1, 10, 1, True, 0),
        (np.array([5, 6]), np.array([[10, 10], [10, 10]]), 25, True, 0),
        (np.array([5, 6]), np.array([[10, 10], [10, 10]]), 25, True, 1),
        (np.array([5, 6]), np.array([[10, 10], [10, 10]]), 25, False, -1),
        (1, np.array([5, 6]), 30, True, 0),
        (1, np.array([5, 6]), 30, False, -1),
    ],
)
def test_space_ops(op, dtype, start, stop, num_samples, endpoint, axis):
    pt_func = getattr(pt, op)
    np_func = getattr(np, op)
    dtype = dtype if dtype != "floatX" else config.floatX
    z = pt_func(start, stop, num_samples, endpoint=endpoint, axis=axis, dtype=dtype)

    numpy_res = np_func(
        start, stop, num=num_samples, endpoint=endpoint, dtype=dtype, axis=axis
    )
    pytensor_res = function(inputs=[], outputs=z, mode="FAST_COMPILE")()

    np.testing.assert_allclose(
        pytensor_res,
        numpy_res,
        atol=1e-6 if config.floatX.endswith("64") else 1e-4,
        rtol=1e-6 if config.floatX.endswith("64") else 1e-4,
    )
