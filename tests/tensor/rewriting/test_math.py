import copy
import logging
import time
from io import StringIO

import numpy as np
import pytest

import pytensor
import pytensor.scalar as ps
import pytensor.tensor as pt
from pytensor import pprint, shared
from pytensor.compile import optdb
from pytensor.compile.debugmode import DebugMode
from pytensor.compile.function import function
from pytensor.compile.mode import Mode, get_default_mode, get_mode
from pytensor.compile.ops import DeepCopyOp, deep_copy_op
from pytensor.configdefaults import config
from pytensor.graph import vectorize_graph
from pytensor.graph.basic import Apply, ancestors, equal_computations
from pytensor.graph.fg import FunctionGraph
from pytensor.graph.rewriting.basic import (
    SequentialNodeRewriter,
    WalkingGraphRewriter,
    check_stack_trace,
    in2out,
    out2in,
)
from pytensor.graph.rewriting.db import RewriteDatabaseQuery
from pytensor.graph.rewriting.utils import is_same_graph, rewrite_graph
from pytensor.printing import debugprint
from pytensor.scalar import PolyGamma, Psi, TriGamma
from pytensor.tensor import inplace
from pytensor.tensor.basic import Alloc, constant, join, second, switch
from pytensor.tensor.blas import Dot22, Gemv
from pytensor.tensor.blas_c import CGemv
from pytensor.tensor.blockwise import Blockwise
from pytensor.tensor.elemwise import CAReduce, DimShuffle, Elemwise
from pytensor.tensor.math import (
    Dot,
    Max,
    Prod,
    Sum,
    _conj,
    _matmul,
    add,
    arccosh,
    arcsinh,
    arctanh,
    bitwise_and,
    bitwise_or,
    bitwise_xor,
    cast,
    conj,
    cosh,
    deg2rad,
    dot,
    eq,
    erf,
    erfc,
    exp,
    expm1,
    floor_div,
    ge,
    gt,
    int_div,
    kv,
    le,
    log,
    log1mexp,
    log1p,
    lt,
    maximum,
    minimum,
    mul,
    neg,
    neq,
    polygamma,
    prod,
    rad2deg,
    reciprocal,
    sigmoid,
    sign,
    sinh,
    softplus,
    sqr,
    sqrt,
    sub,
    tanh,
    true_div,
    xor,
)
from pytensor.tensor.math import abs as pt_abs
from pytensor.tensor.math import all as pt_all
from pytensor.tensor.math import any as pt_any
from pytensor.tensor.math import max as pt_max
from pytensor.tensor.math import min as pt_min
from pytensor.tensor.math import pow as pt_pow
from pytensor.tensor.math import sum as pt_sum
from pytensor.tensor.rewriting.elemwise import local_dimshuffle_lift
from pytensor.tensor.rewriting.math import (
    compute_mul,
    is_1pexp,
    local_div_switch_sink,
    local_grad_log_erfc_neg,
    local_greedy_distributor,
    local_mul_canonizer,
    local_mul_switch_sink,
    local_reduce_chain,
    local_reduce_join,
    local_sum_prod_of_mul_or_div,
    mul_canonizer,
    parse_mul_tree,
    perform_sigm_times_exp,
    simplify_mul,
)
from pytensor.tensor.shape import Reshape, Shape_i, SpecifyShape, specify_shape
from pytensor.tensor.type import (
    TensorType,
    cmatrix,
    dmatrices,
    dmatrix,
    dscalar,
    dtensor3,
    dvector,
    fmatrices,
    fmatrix,
    fscalar,
    ftensor4,
    fvector,
    imatrices,
    imatrix,
    iscalar,
    ivector,
    lscalar,
    matrices,
    matrix,
    scalar,
    tensor,
    tensor3,
    tensor4,
    values_eq_approx_remove_nan,
    vector,
    vectors,
    zscalar,
)
from pytensor.tensor.variable import TensorConstant
from tests import unittest_tools as utt


rewrite_mode = config.mode
if rewrite_mode == "FAST_COMPILE":
    rewrite_mode = "FAST_RUN"
rewrite_mode = get_mode(rewrite_mode)

dimshuffle_lift = out2in(local_dimshuffle_lift)

_stabilize_rewrites = RewriteDatabaseQuery(include=["fast_run"])
_stabilize_rewrites.position_cutoff = 1.51
_stabilize_rewrites = optdb.query(_stabilize_rewrites)

_specialize_rewrites = RewriteDatabaseQuery(include=["fast_run"])
_specialize_rewrites.position_cutoff = 2.01
_specialize_rewrites = optdb.query(_specialize_rewrites)

_fast_run_rewrites = RewriteDatabaseQuery(include=["fast_run"])
_fast_run_rewrites = optdb.query(_fast_run_rewrites)


def ds(x, y):
    return x.dimshuffle(y)


def rewrite(g, level="fast_run"):
    if level == "fast_run":
        _fast_run_rewrites.rewrite(g)
    elif level == "specialize":
        _specialize_rewrites.rewrite(g)
    elif level == "stabilize":
        _stabilize_rewrites.rewrite(g)
    else:
        raise ValueError(level)
    return g


def inputs(xbc=(0, 0), ybc=(0, 0), zbc=(0, 0)):
    x = TensorType(dtype="float64", shape=xbc)("x")
    y = TensorType(dtype="float64", shape=ybc)("y")
    z = TensorType(dtype="float64", shape=zbc)("z")
    return x, y, z


def test_add_canonizer_problem0():
    n_segments = 10
    label = lscalar("label")
    segment_labels = label + np.asarray([0] * n_segments, dtype="int64")

    r = segment_labels * 5
    f = function([label], r)
    f(3)

    # This was crashing in the past.
    c0 = pt.constant([True])
    c1 = pt.constant([True])
    function([], c0 + c1)


class TestGreedyDistribute:
    def test_main(self):
        a, b, c, d, x, y, z = matrices("abcdxyz")

        # 1. ((a/x + b/y) * x * y) --> a*y + b*x
        e = (a / z + b / x) * x * z
        g = FunctionGraph([a, b, c, d, x, y, z], [e])
        mul_canonizer.rewrite(g)
        WalkingGraphRewriter(
            SequentialNodeRewriter(local_greedy_distributor), order="out_to_in"
        ).rewrite(g)
        assert str(pprint(g.outputs[0])) == "((a * x) + (b * z))"

        # 2. ((a/x + b) * x) --> a + b*x
        e = (a / x + b) * x
        g = FunctionGraph([a, b, x], [e])
        mul_canonizer.rewrite(g)
        WalkingGraphRewriter(
            SequentialNodeRewriter(local_greedy_distributor), order="out_to_in"
        ).rewrite(g)
        assert str(pprint(g.outputs[0])) == "(a + (b * x))"

    def test_kording_bug(self):
        x, y = vectors("xy")
        eps = scalar("eps")
        s = scalar("s")

        # r = mul(pt.fill(x, 2.*a), x/a , (y+z) , a)
        # r = mul((x/a+y) , a, z)
        r = mul(s - 1, eps + x / s, eps + y / s, s)

        f = function([s, eps, x, y], r**2)

        s_val = np.asarray(4, dtype=config.floatX)
        eps_val = np.asarray(1.0e-6, dtype=config.floatX)
        x_val = np.asarray([1.5, 2], dtype=config.floatX)
        y_val = np.asarray([2.3, 3.1], dtype=config.floatX)

        r0 = f(s_val, eps_val, x_val, y_val)
        r1 = f(s_val, eps_val, x_val, y_val)
        r2 = f(s_val, eps_val, x_val, y_val)

        assert np.all(r0 == r1)
        assert np.all(r0 == r2)


class TestAlgebraicCanonizer:
    x, y, z = matrices("xyz")

    @pytest.mark.parametrize(
        "e, exp_g",
        [
            # ((2.0 * x) / (2.0 * y), None),
            # ((2.0 * x) / (4.0 * y), None),
            # (x / (y / z), None),
            # ((x * y) / x, None),
            # ((x / y) * (y / z) * (z / x), None),
            # ((a / b) * (b / c) * (c / d), None),
            # ((a * b) / (b * c) / (c * d), None),
            # (2 * x / 2, None),
            # (x / y / x, None),
            # ((x / x) * (y / y), None),
            (
                (-1 * x) / y / (-2 * z),
                (pt.as_tensor([[0.5]], dtype="floatX") * x) / (y * z),
            ),
        ],
    )
    def test_muldiv(self, e, exp_g):
        g_rewritten = rewrite_graph(e, custom_rewrite=mul_canonizer)
        assert equal_computations([g_rewritten], [exp_g])

    def test_elemwise_multiple_inputs_rewrites(self):
        """Verify that the `AlgebraicCanonizer` merges sequential ``Elemwise({mul,add})``."""
        # Test with and without DimShuffle
        shp = (5, 5)
        fx, fy, fz = fmatrices("xyz")
        dx, dy, dz = dmatrices("xyz")
        # fv = fvector('r').dimshuffle('x', 0)
        # dv = dvector('s').dimshuffle('x', 0)
        fxv = np.asarray(np.random.random(shp), dtype="float32")
        fyv = np.asarray(np.random.random(shp), dtype="float32")
        fzv = np.asarray(np.random.random(shp), dtype="float32")
        # fvv = np.asarray(np.random.random((shp[0]), dtype='float32').reshape(1, shp[0])
        # dxv = np.asarray(np.random.random((*shp), dtype='float64')
        # dyv = np.asarray(np.random.random((*shp), dtype='float64')
        # dzv = np.asarray(np.random.random((*shp), dtype='float64')
        # dvv = np.asarray(np.random.random((shp[0]), dtype='float64').reshape(1, shp[0])
        cases = [
            (fx + fy, (fx, fy), (fxv, fyv), 1, "float32"),
            (fx * fy, (fx, fy), (fxv, fyv), 1, "float32"),
            (fx + fy + fz, (fx, fy, fz), (fxv, fyv, fzv), 1, "float32"),
            # (dx+dy+dz,(dx,dy,dz),(dxv,dyv,dzv),1,'float64'),
            (fx * fy * fz, (fx, fy, fz), (fxv, fyv, fzv), 1, "float32"),
            # (dx*dy*dz,(dx,dy,dz),(dxv,dyv,dzv),1,'float64'),
            # (fx*fy*(fx+fy+fz),(fx,fy,fz),(fxv,fyv,fzv),2,'float32'),
            # (dx*dy*(dx+dy+dz),(dx,dy,dz),(dxv,dyv,dzv),2,'float64'),
            # (fx*fy*(fx+fy+dz),(fx,fy,dz),(dxv,dyv,dzv),2,'float64'),  # check mixed type add
            # (dz*fy*(fx+fy),(fx,fy,dz),(dxv,dyv,dzv),2,'float64'),  # check mixed type mul
            # check with dimshuffle of constant
            (
                fx + fy + fz + 2,
                (fx, fy, fz),
                (fxv, fyv, fzv),
                1,
                {
                    "custom": "float32",
                    "numpy+floatX": config.floatX,
                    "numpy": "float64",
                },
            ),
            (
                fx * fy * fz * 2,
                (fx, fy, fz),
                (fxv, fyv, fzv),
                1,
                {
                    "custom": "float32",
                    "numpy+floatX": config.floatX,
                    "numpy": "float64",
                },
            ),
            # (2+fx+fy+fz,(fx,fy,fz),(fxv,fyv,fzv),1,'float32'),
            # (2*fx*fy*fz,(fx,fy,fz),(fxv,fyv,fzv),1,'float32'),
            (
                2 + fx + fy + fz + 2,
                (fx, fy, fz),
                (fxv, fyv, fzv),
                1,
                {
                    "custom": "float32",
                    "numpy+floatX": config.floatX,
                    "numpy": "float64",
                },
            ),
            (
                2 * fx * fy * fz * 2,
                (fx, fy, fz),
                (fxv, fyv, fzv),
                1,
                {
                    "custom": "float32",
                    "numpy+floatX": config.floatX,
                    "numpy": "float64",
                },
            ),
            # (fx*fy*2*(fx+fy+fz),(fx,fy,fz),(fxv,fyv,fzv),2,'float32'),
            # (fx*fy*(2+fx+fy+fz),(fx,fy,fz),(fxv,fyv,fzv),2,'float32'),
            (
                fx * fy * 2 * (fx + fy + fz + 2),
                (fx, fy, fz),
                (fxv, fyv, fzv),
                2,
                {
                    "custom": "float32",
                    "numpy+floatX": config.floatX,
                    "numpy": "float64",
                },
            ),
            # check with broadcast of row
            # (fx+fy+fz+fv,(fx,fy,fz,fv),(fxv,fyv,fzv,fvv),1,'float32'),
            # (fx*fy*fz*fv,(fx,fy,fz,fv),(fxv,fyv,fzv,fvv),1,'float32'),
            # (fv+fx+fy+fz,(fx,fy,fz,fv),(fxv,fyv,fzv,fvv),1,'float32'),
            # (fv*fx*fy*fz,(fx,fy,fz,fv),(fxv,fyv,fzv,fvv),1,'float32'),
            # (fx*fy*fv*(fx+fy+fz),(fx,fy,fz,fv),(fxv,fyv,fzv,fvv),2,'float32'),
            # (fx*fy*(fv+fx+fy+fz),(fx,fy,fz,fv),(fxv,fyv,fzv,fvv),2,'float32'),
            # (fx*fy*fv*(fv+fx+fy+fz),(fx,fy,fz,fv),(fxv,fyv,fzv,fvv),2,'float32'),
            # (dx+dy+dz+dv,(dx,dy,dz,dv),(dxv,dyv,dzv,dvv),1,'float64'),
            # (dx*dy*dz*dv,(dx,dy,dz,dv),(dxv,dyv,dzv,dvv),1,'float64'),
            # (dv+dx+dy+dz,(dx,dy,dz,dv),(dxv,dyv,dzv,dvv),1,'float64'),
            # (dv*dx*dy*dz,(dx,dy,dz,dv),(dxv,dyv,dzv,dvv),1,'float64'),
            # (dx*dy*dv*(dx+dy+dz),(dx,dy,dz,dv),(dxv,dyv,dzv,dvv),2,'float64'),
            # (dx*dy*(dv+dx+dy+dz),(dx,dy,dz,dv),(dxv,dyv,dzv,dvv),2,'float64'),
            # (dx*dy*dv*(dv+dx+dy+dz),(dx,dy,dz,dv),(dxv,dyv,dzv,dvv),2,'float64'),
        ]  # [10:11]
        # print cases

        # We must be sure that the `AlgebraicCanonizer` is working, but that we don't have other
        # rewrites that could hide bug in the `AlgebraicCanonizer` as `local_elemwise_fusion`
        mode = get_default_mode()
        rewrites = RewriteDatabaseQuery(["canonicalize"])
        rewrites = rewrites.excluding("local_elemwise_fusion")
        mode = mode.__class__(linker=mode.linker, optimizer=rewrites)
        for id, [g, sym_inputs, val_inputs, nb_elemwise, out_dtype] in enumerate(cases):
            if isinstance(out_dtype, dict):
                out_dtype = out_dtype[config.cast_policy]
            f = function(
                list(sym_inputs),
                g,
                mode=mode,
            )

            out = f(*val_inputs)
            assert len(f.maker.fgraph.toposort()) == nb_elemwise
            assert out_dtype == out.dtype

    @pytest.mark.skip(
        reason="Current implementation of AlgebraicCanonizer does not implement all cases."
    )
    def test_elemwise_multiple_inputs_rewrites_2(self):
        """Verify that the `AlgebraicCanonizer` merges sequential ``Elemwise({mul,add})``.

        This part are that case that should have been done, but that are not implemented.
        """

        # Test with and without `DimShuffle`
        shp = (5, 5)
        fx, fy, fz = fmatrices("xyz")
        dx, dy, dz = dmatrices("xyz")
        fv = fvector("r").dimshuffle("x", 0)
        dv = dvector("s").dimshuffle("x", 0)
        fxv = np.asarray(np.random.random(shp), dtype="float32")
        fyv = np.asarray(np.random.random(shp), dtype="float32")
        fzv = np.asarray(np.random.random(shp), dtype="float32")
        fvv = np.asarray(np.random.random(shp[0]), dtype="float32").reshape(1, shp[0])
        dxv = np.asarray(np.random.random(shp), dtype="float64")
        dyv = np.asarray(np.random.random(shp), dtype="float64")
        dzv = np.asarray(np.random.random(shp), dtype="float64")
        dvv = np.asarray(np.random.random(shp[0]), dtype="float64").reshape(1, shp[0])
        cases = [
            (fx + fy, (fx, fy), (fxv, fyv), 1, "float32"),
            (fx * fy, (fx, fy), (fxv, fyv), 1, "float32"),
            (fx + fy + fz, (fx, fy, fz), (fxv, fyv, fzv), 1, "float32"),
            (dx + dy + dz, (dx, dy, dz), (dxv, dyv, dzv), 1, "float64"),
            (fx * fy * fz, (fx, fy, fz), (fxv, fyv, fzv), 1, "float32"),
            (dx * dy * dz, (dx, dy, dz), (dxv, dyv, dzv), 1, "float64"),
            (fx * fy * (fx + fy + fz), (fx, fy, fz), (fxv, fyv, fzv), 2, "float32"),
            (dx * dy * (dx + dy + dz), (dx, dy, dz), (dxv, dyv, dzv), 2, "float64"),
            (
                fx * fy * (fx + fy + dz),
                (fx, fy, dz),
                (dxv, dyv, dzv),
                2,
                "float64",
            ),  # check mixed type add
            (
                dz * fy * (fx + fy),
                (fx, fy, dz),
                (dxv, dyv, dzv),
                2,
                "float64",
            ),  # check mixed type mul
            # check with dimshuffle of constant
            (fx + fy + fz + 2, (fx, fy, fz), (fxv, fyv, fzv), 1, "float32"),
            (fx * fy * fz * 2, (fx, fy, fz), (fxv, fyv, fzv), 1, "float32"),
            (2 + fx + fy + fz, (fx, fy, fz), (fxv, fyv, fzv), 1, "float32"),
            (2 * fx * fy * fz, (fx, fy, fz), (fxv, fyv, fzv), 1, "float32"),
            (2 + fx + fy + fz + 2, (fx, fy, fz), (fxv, fyv, fzv), 1, "float32"),
            (2 * fx * fy * fz * 2, (fx, fy, fz), (fxv, fyv, fzv), 1, "float32"),
            (fx * fy * 2 * (fx + fy + fz), (fx, fy, fz), (fxv, fyv, fzv), 2, "float32"),
            (fx * fy * (2 + fx + fy + fz), (fx, fy, fz), (fxv, fyv, fzv), 2, "float32"),
            (
                fx * fy * 2 * (fx + fy + fz + 2),
                (fx, fy, fz),
                (fxv, fyv, fzv),
                2,
                "float32",
            ),
            # check with broadcast of row
            (fx + fy + fz + fv, (fx, fy, fz, fv), (fxv, fyv, fzv, fvv), 1, "float32"),
            (fx * fy * fz * fv, (fx, fy, fz, fv), (fxv, fyv, fzv, fvv), 1, "float32"),
            (fv + fx + fy + fz, (fx, fy, fz, fv), (fxv, fyv, fzv, fvv), 1, "float32"),
            (fv * fx * fy * fz, (fx, fy, fz, fv), (fxv, fyv, fzv, fvv), 1, "float32"),
            (
                fx * fy * fv * (fx + fy + fz),
                (fx, fy, fz, fv),
                (fxv, fyv, fzv, fvv),
                2,
                "float32",
            ),
            (
                fx * fy * (fv + fx + fy + fz),
                (fx, fy, fz, fv),
                (fxv, fyv, fzv, fvv),
                2,
                "float32",
            ),
            (
                fx * fy * fv * (fv + fx + fy + fz),
                (fx, fy, fz, fv),
                (fxv, fyv, fzv, fvv),
                2,
                "float32",
            ),
            (dx + dy + dz + dv, (dx, dy, dz, dv), (dxv, dyv, dzv, dvv), 1, "float64"),
            (dx * dy * dz * dv, (dx, dy, dz, dv), (dxv, dyv, dzv, dvv), 1, "float64"),
            (dv + dx + dy + dz, (dx, dy, dz, dv), (dxv, dyv, dzv, dvv), 1, "float64"),
            (dv * dx * dy * dz, (dx, dy, dz, dv), (dxv, dyv, dzv, dvv), 1, "float64"),
            (
                dx * dy * dv * (dx + dy + dz),
                (dx, dy, dz, dv),
                (dxv, dyv, dzv, dvv),
                2,
                "float64",
            ),
            (
                dx * dy * (dv + dx + dy + dz),
                (dx, dy, dz, dv),
                (dxv, dyv, dzv, dvv),
                2,
                "float64",
            ),
            (
                dx * dy * dv * (dv + dx + dy + dz),
                (dx, dy, dz, dv),
                (dxv, dyv, dzv, dvv),
                2,
                "float64",
            ),
        ]  # [10:11]
        # print cases

        # We must be sure that the AlgebraicCanonizer is working, but that we don't have other
        # rewrites that could hide bugs in the `AlgebraicCanonizer` as `local_elemwise_fusion`
        mode = get_default_mode()
        mode._optimizer = RewriteDatabaseQuery(["canonicalize"])
        mode._optimizer = mode._optimizer.excluding("local_elemwise_fusion")
        for id, [g, sym_inputs, val_inputs, nb_elemwise, out_dtype] in enumerate(cases):
            f = function(
                list(sym_inputs),
                g,
                mode=mode,
            )

            out = f(*val_inputs)
            assert len(f.maker.fgraph.toposort()) == nb_elemwise
            assert out_dtype == out.dtype

    def test_mul_div_cases(self):
        """
        TODO

            x / x -> 1
            (x * y) / x -> y
            x / y / x -> 1 / y
            x / y / z -> x / (y * z)
            x / (y / z) -> (x * z) / y
            (a / b) * (b / c) * (c / d) -> a / d
            (2.0 * x) / (4.0 * y) -> (0.5 * x) / y
            2 * x / 2 -> x

        """
        # with and without DimShuffle
        # TODO: with DimShuffle

        shp = (3, 3)
        fx, fy, fz, fw = fmatrices("xyzw")
        dx, dy, dz, dw = dmatrices("xyzw")
        fv = fvector("r").dimshuffle("x", 0)
        dv = dvector("s").dimshuffle("x", 0)
        fxv = np.asarray(np.random.random(shp), dtype="float32")
        fyv = np.asarray(np.random.random(shp), dtype="float32")
        fzv = np.asarray(np.random.random(shp), dtype="float32")
        fwv = np.asarray(np.random.random(shp), dtype="float32")
        fvv = np.asarray(np.random.random(shp[0]), dtype="float32").reshape(1, shp[0])
        dxv = np.asarray(np.random.random(shp), dtype="float64")
        dyv = np.asarray(np.random.random(shp), dtype="float64")
        dzv = np.asarray(np.random.random(shp), dtype="float64")
        dwv = np.asarray(np.random.random(shp), dtype="float64")
        dvv = np.asarray(np.random.random(shp[0]), dtype="float64").reshape(1, shp[0])

        # We must be sure that the `AlgebraicCanonizer` is working, but that we don't have other
        # rewrites that could hide bugs in the `AlgebraicCanonizer` as `local_elemwise_fusion`
        mode = get_default_mode()

        rewrite_query = RewriteDatabaseQuery(["canonicalize"])
        rewrite_query = rewrite_query.including("ShapeOpt", "local_fill_to_alloc")
        rewrite_query = rewrite_query.excluding("local_elemwise_fusion")
        mode = mode.__class__(linker=mode.linker, optimizer=rewrite_query)
        # test x / x -> 1
        for id, (g, sym_inputs, val_inputs, out_dtype) in enumerate(
            [
                (fx / fx, [fx], [fxv], "float32"),
                (dx / dx, [dx], [dxv], "float64"),
                (fv / fv, [fv], [fvv], "float32"),
                (dv / dv, [dv], [dvv], "float64"),
            ]
        ):
            f = function(list(sym_inputs), g, mode=mode)
            out = f(*val_inputs)
            assert (out == np.ones(shp, dtype=out_dtype)).all()
            topo = f.maker.fgraph.toposort()
            if sym_inputs[0].broadcastable[0]:
                assert len(topo) == 2
                assert isinstance(topo[0].op, Shape_i)
                assert isinstance(topo[1].op, Alloc)
            else:
                assert len(topo) == 3
                assert isinstance(topo[0].op, Shape_i)
                assert isinstance(topo[1].op, Shape_i)
                assert isinstance(topo[2].op, Alloc)
            assert out_dtype == out.dtype

        # test (x * y) / x -> y
        for id, (g, sym_inputs, val_inputs, nb_elemwise, out_dtype) in enumerate(
            [
                ((dx * dy) / dx, [dx, dy], [dxv, dyv], 0, "float64"),
                ((fx * fy) / fx, [fx, fy], [fxv, fyv], 0, "float32"),
                ((dv * dy) / dv, [dv, dy], [dvv, dyv], 0, "float64"),
                ((fv * fy) / fv, [fv, fy], [fvv, fyv], 0, "float32"),
                # must broadcast as there is a dimshuffle in the computation
                ((dx * dv) / dx, [dx, dv], [dxv, dvv], 1, "float64"),
                # topo: [Elemwise{second,no_inplace}(x, <TensorType(float64, row)>)]
                ((fx * fv) / fx, [fx, fv], [fxv, fvv], 1, "float32"),
                # topo: [Elemwise{second,no_inplace}(x, <TensorType(float32, row)>)]
            ]
        ):
            f = function(list(sym_inputs), g, mode=mode)
            out = f(*val_inputs)
            assert out_dtype == out.dtype
            utt.assert_allclose(out, val_inputs[1])
            topo = f.maker.fgraph.toposort()
            assert not any(node.op == pt.true_div for node in topo)

        # test x / y / x -> 1 / y
        for id, (g, sym_inputs, val_inputs, nb_elemwise, out_dtype) in enumerate(
            [
                ((dx / dy) / dx, [dx, dy], [dxv, dyv], 1, "float64"),
                ((fx / fy) / fx, [fx, fy], [fxv, fyv], 1, "float32"),
                ((dv / dy) / dv, [dv, dy], [dvv, dyv], 1, "float64"),
                ((fv / fy) / fv, [fv, fy], [fvv, fyv], 1, "float32"),
                # must broadcast as there is a dimshuffle in the computation
                ((dx / dv) / dx, [dx, dv], [dxv, dvv], 2, "float64"),
                # topo: [Shape_i, Shape_i, Elemwise{reciprocal,no_inplace}(<TensorType(float64, row)>), Alloc]
                ((fx / fv) / fx, [fx, fv], [fxv, fvv], 2, "float32"),
                # topo: [Shape_i, Shape_i, Elemwise{reciprocal,no_inplace}(<TensorType(float32, row)>), Alloc]
            ]
        ):
            f = function(list(sym_inputs), g, mode=mode)
            out = f(*val_inputs)
            utt.assert_allclose(out, (1 / val_inputs[1]))
            topo = f.maker.fgraph.toposort()
            elem = [t for t in topo if isinstance(t.op, Elemwise)]
            assert len(elem) == nb_elemwise
            assert isinstance(elem[0].op, Elemwise)
            assert any(
                isinstance(
                    el.op.scalar_op,
                    ps.basic.Reciprocal | ps.basic.TrueDiv,
                )
                for el in elem
            )
            assert out_dtype == out.dtype

        # test (a / b) * (b / c) * (c / d) -> a / d
        for id, (g, sym_inputs, val_inputs, out_dtype) in enumerate(
            [
                (
                    (dx / dy) * (dy / dz) * (dz / dw),
                    [dx, dy, dz, dw],
                    [dxv, dyv, dzv, dwv],
                    "float64",
                ),
                (
                    (fx / fy) * (fy / fz) * (fz / fw),
                    [fx, fy, fz, fw],
                    [fxv, fyv, fzv, fwv],
                    "float32",
                ),
                (
                    (dv / dy) * (dy / dz) * (dz / dw),
                    [dv, dy, dz, dw],
                    [dvv, dyv, dzv, dwv],
                    "float64",
                ),
                (
                    (fv / fy) * (fy / fz) * (fz / fw),
                    [fv, fy, fz, fw],
                    [fvv, fyv, fzv, fwv],
                    "float32",
                ),
                (
                    (dx / dv) * (dv / dz) * (dz / dw),
                    [dx, dv, dz, dw],
                    [dxv, dvv, dzv, dwv],
                    "float64",
                ),
                (
                    (fx / fv) * (fv / fz) * (fz / fw),
                    [fx, fv, fz, fw],
                    [fxv, fvv, fzv, fwv],
                    "float32",
                ),
                (
                    (dx / dy) * (dy / dv) * (dv / dw),
                    [dx, dy, dv, dw],
                    [dxv, dyv, dvv, dwv],
                    "float64",
                ),
                (
                    (fx / fy) * (fy / fv) * (fv / fw),
                    [fx, fy, fv, fw],
                    [fxv, fyv, fvv, fwv],
                    "float32",
                ),
                (
                    (dx / dy) * (dy / dz) * (dz / dv),
                    [dx, dy, dz, dv],
                    [dxv, dyv, dzv, dvv],
                    "float64",
                ),
                (
                    (fx / fy) * (fy / fz) * (fz / fv),
                    [fx, fy, fz, fv],
                    [fxv, fyv, fzv, fvv],
                    "float32",
                ),
            ]
        ):
            f = function(list(sym_inputs), g, mode=mode)
            out = f(*val_inputs)
            utt.assert_allclose(out, (val_inputs[0] / val_inputs[3]))
            topo = f.maker.fgraph.toposort()
            assert len(topo) == 1
            assert isinstance(topo[0].op, Elemwise)
            assert isinstance(topo[0].op.scalar_op, ps.basic.TrueDiv)
            assert len(topo[0].inputs) == 2
            assert out_dtype == out.dtype

        # test (2.0 * x) / (4.0 * y) -> (0.5 * x) / y
        for id, (g, sym_inputs, val_inputs, out_dtype) in enumerate(
            [
                (((2.0 * dx) / (4.0 * dy)), [dx, dy], [dxv, dyv], "float64"),
                (
                    ((2.0 * fx) / (4.0 * fy)),
                    [fx, fy],
                    [fxv, fyv],
                    {
                        "custom": "float32",
                        "numpy+floatX": config.floatX,
                        "numpy": "float64",
                    },
                ),
                (((2.0 * dv) / (4.0 * dy)), [dv, dy], [dvv, dyv], "float64"),
                (
                    ((2.0 * fv) / (4.0 * fy)),
                    [fv, fy],
                    [fvv, fyv],
                    {
                        "custom": "float32",
                        "numpy+floatX": config.floatX,
                        "numpy": "float64",
                    },
                ),
                (((2.0 * dx) / (4.0 * dv)), [dx, dv], [dxv, dvv], "float64"),
                (
                    ((2.0 * fx) / (4.0 * fv)),
                    [fx, fv],
                    [fxv, fvv],
                    {
                        "custom": "float32",
                        "numpy+floatX": config.floatX,
                        "numpy": "float64",
                    },
                ),
            ]
        ):
            if isinstance(out_dtype, dict):
                out_dtype = out_dtype[config.cast_policy]
            f = function(list(sym_inputs), g, mode=mode)
            out = f(*val_inputs)
            utt.assert_allclose(out, (0.5 * val_inputs[0] / val_inputs[1]))
            topo = f.maker.fgraph.toposort()
            assert len(topo) == 2
            assert isinstance(topo[0].op, Elemwise)
            assert isinstance(topo[0].op.scalar_op, ps.basic.Mul)
            assert len(topo[0].inputs) == 2
            assert isinstance(topo[1].op, Elemwise)
            assert isinstance(topo[1].op.scalar_op, ps.basic.TrueDiv)
            assert len(topo[1].inputs) == 2
            assert out_dtype == out.dtype

        # test 2 * x / 2 -> x
        for id, (g, sym_inputs, val_inputs, out_dtype) in enumerate(
            [
                ((2 * dx) / 2, [dx], [dxv], "float64"),
                (
                    (2 * fx) / 2,
                    [fx],
                    [fxv],
                    {
                        "custom": "float32",
                        "numpy+floatX": config.floatX,
                        "numpy": "float64",
                    },
                ),
                ((2 * dv) / 2, [dv], [dvv], "float64"),
                (
                    (2 * fv) / 2,
                    [fv],
                    [fvv],
                    {
                        "custom": "float32",
                        "numpy+floatX": config.floatX,
                        "numpy": "float64",
                    },
                ),
            ]
        ):
            if isinstance(out_dtype, dict):
                out_dtype = out_dtype[config.cast_policy]
            f = function(list(sym_inputs), g, mode=mode)
            out = f(*val_inputs)
            utt.assert_allclose(out, val_inputs[0])
            topo = f.maker.fgraph.toposort()
            assert len(topo) == 1
            topo[0].op == deep_copy_op
            assert out_dtype == out.dtype

        # test x / abs(x) -> sign(x)
        for id, (g, sym_inputs, val_inputs, out_dtype) in enumerate(
            [
                (dx / abs(dx), [dx], [0.5 - dxv], "float64"),
                (fx / abs(fx), [fx], [0.5 - fxv], "float32"),
                (dx / abs(dx), [dx], [0.1 * dxv], "float64"),
                (fx / abs(fx), [fx], [0.1 * fxv], "float32"),
                (dv / abs(dv), [dv], [0.5 - dvv], "float64"),
                (fv / abs(fv), [fv], [0.5 - fvv], "float32"),
            ]
        ):
            f = function(list(sym_inputs), g, mode=mode)
            out = f(*val_inputs)
            assert np.all(np.isfinite(out))
            utt.assert_allclose(out, np.sign(val_inputs[0]))
            assert out_dtype == out.dtype
            assert len(f.maker.fgraph.toposort()) == 1

        # test (2*x) / (3*abs(x)) -> sign(x)
        for id, (g, sym_inputs, val_inputs, out_dtype) in enumerate(
            [
                ((2 * dx) / (3 * abs(dx)), [dx], [0.5 - dxv], "float64"),
                (
                    (2 * fx) / (3 * abs(fx)),
                    [fx],
                    [0.5 - fxv],
                    {
                        "custom": "float32",
                        "numpy+floatX": config.floatX,
                        "numpy": "float64",
                    },
                ),
                ((2 * dx) / (3 * abs(dx)), [dx], [0.1 * dxv], "float64"),
                (
                    (2 * fx) / (3 * abs(fx)),
                    [fx],
                    [0.1 * fxv],
                    {
                        "custom": "float32",
                        "numpy+floatX": config.floatX,
                        "numpy": "float64",
                    },
                ),
                ((2 * dv) / (3 * abs(dv)), [dv], [0.5 - dvv], "float64"),
                (
                    (2 * fv) / (3 * abs(fv)),
                    [fv],
                    [0.5 - fvv],
                    {
                        "custom": "float32",
                        "numpy+floatX": config.floatX,
                        "numpy": "float64",
                    },
                ),
            ]
        ):
            if isinstance(out_dtype, dict):
                out_dtype = out_dtype[config.cast_policy]
            f = function(list(sym_inputs), g, mode=mode)
            topo = f.maker.fgraph.toposort()
            out = f(*val_inputs)
            assert np.all(np.isfinite(out))
            utt.assert_allclose(out, np.sign(val_inputs[0]) * 2 / 3)
            assert out_dtype == out.dtype

    def test_abs_mul_div(self):
        """Test that ``4 * x / abs(2*x)`` gets "simplified" during canonicalization."""

        x = dscalar()
        # a = pt.pt_abs(x)

        if config.mode == "FAST_COMPILE":
            mode = get_mode("FAST_RUN").excluding("local_elemwise_fusion")
        else:
            mode = get_default_mode().excluding("local_elemwise_fusion")

        f = function([x], [(4 * x) / abs(2 * x)], mode=mode)
        f(0.1)
        f(-1)
        # Some stabilization rewrites make the output finite instead of NaN.
        # `debug_mode` will raise an error when he see NaN
        if not isinstance(mode, DebugMode):
            assert np.isfinite(f(0))

        assert len(f.maker.fgraph.toposort()) == 2
        assert f.maker.fgraph.toposort()[0].op == sign

        f = function([x], [(4 * x) / abs(x / 2)], mode=mode)
        f(0.1)
        f(-1)
        if not isinstance(mode, DebugMode):
            assert np.isfinite(f(0))

        assert len(f.maker.fgraph.toposort()) == 2
        assert f.maker.fgraph.toposort()[0].op == sign

    @pytest.mark.skip(
        reason="Current implementation of AlgebraicCanonizer does not "
        "implement all cases. Skip the corresponding test."
    )
    def test_multiple_case_that_fail(self):
        shp = (4, 4)
        fx, fy, fz = fmatrices("xyz")
        dx, dy, dz = dmatrices("xyz")
        fxv = np.asarray(np.random.random(shp), dtype="float32")
        fyv = np.asarray(np.random.random(shp), dtype="float32")
        fzv = np.asarray(np.random.random(shp), dtype="float32")
        dxv = np.asarray(np.random.random(shp), dtype="float32")
        dyv = np.asarray(np.random.random(shp), dtype="float32")
        dzv = np.asarray(np.random.random(shp), dtype="float32")
        # fvv = np.asarray(np.random.random((shp[0]), dtype='float32').reshape(1, shp[0])

        mode = get_default_mode()

        rewrites = RewriteDatabaseQuery(["canonicalize"])
        rewrites = rewrites.excluding("local_elemwise_fusion")
        mode = mode.__class__(linker=mode.linker, optimizer=rewrites)
        # test fail!
        # test x / y / z -> x / (y * z)
        for g, sym_inputs, val_inputs, out_dtype in [
            ((dx / dy) / dz, [dx, dy, dz], [dxv, dyv, dzv], "float64"),
            ((fx / fy) / fz, [fx, fy, fz], [fxv, fyv, fzv], "float32"),
        ]:
            f = function(list(sym_inputs), g, mode=mode)
            out = f(*val_inputs)
            utt.assert_allclose(out, val_inputs[0] / val_inputs[1] / val_inputs[2])
            topo = f.maker.fgraph.toposort()
            assert len(topo) == 2
            assert isinstance(topo[0].op, Elemwise)
            assert isinstance(topo[0].op.scalar_op, ps.basic.Reciprocal)
            assert len(topo[0].inputs) == 1
            assert out_dtype == out.dtype

        # test x / (y / z) -> (x * z) / y
        for g, sym_inputs, val_inputs, out_dtype in [
            (dx / (dy / dz), [dx, dy, dz], [dxv, dyv, dzv], "float64"),
            (fx / (fy / fz), [fx, fy, fz], [fxv, fyv, fzv], "float32"),
        ]:
            f = function(list(sym_inputs), g, mode=mode)
            out = f(*val_inputs)
            utt.assert_allclose(out, val_inputs[0] / (val_inputs[1] / val_inputs[2]))
            topo = f.maker.fgraph.toposort()
            assert len(topo) == 2
            assert isinstance(topo[0].op, Elemwise)
            assert isinstance(topo[0].op.scalar_op, ps.basic.Reciprocal)
            assert len(topo[0].inputs) == 1
            assert out_dtype == out.dtype

    def test_canonicalize_nan(self):
        # Regression test for bug in canonicalization of NaN values.
        # This bug caused an infinite loop which was caught by the equilibrium
        # rewriter, resulting in an error log message.

        sio = StringIO()
        handler = logging.StreamHandler(sio)
        handler.setLevel(logging.ERROR)
        logging.getLogger("pytensor.graph.rewriting.basic").addHandler(handler)
        try:
            x = vector()
            function([x], x + np.nan)
        finally:
            logging.getLogger("pytensor.graph.rewriting.basic").removeHandler(handler)
        # Ideally this test would only catch the maxed out equilibrium
        # rewriter error message, but to be safe in case this message
        # is modified in the future, we assert that there is no error
        # at all.
        assert not sio.getvalue()

    def test_mismatching_types(self):
        a = pt.as_tensor([[0.0]], dtype=np.float64)
        b = tensor(dtype="float64", shape=(None,)).dimshuffle("x", 0)
        z = add(a, b)
        # Construct a node with the wrong output `Type`
        z = Apply(
            z.owner.op, z.owner.inputs, [tensor(dtype="float64", shape=(None, None))]
        ).outputs[0]

        z_rewritten = rewrite_graph(
            z, custom_rewrite=in2out(local_mul_canonizer, name="blah")
        )
        # No rewrite was applied
        assert z_rewritten is z

    def test_shape_specified_by_constant(self):
        x = vector("x")
        const = np.full(shape=(5,), fill_value=2.0).astype(config.floatX)
        out = x * const

        new_out = rewrite_graph(
            out, custom_rewrite=in2out(local_mul_canonizer, name="test")
        )
        expected_out = np.array([2.0]).astype(config.floatX) * specify_shape(x, (5,))
        assert equal_computations([new_out], [expected_out])

    def test_broadcasted_by_constant(self):
        x = vector("x")
        const = np.full(shape=(3, 5), fill_value=2.0).astype(config.floatX)
        out = x * const

        new_out = rewrite_graph(
            out, custom_rewrite=in2out(local_mul_canonizer, name="test")
        )
        expected_out = second(const, np.array([[2.0]], dtype=config.floatX) * x)
        assert equal_computations([new_out], [expected_out])


def test_local_merge_abs():
    x, y, z = matrices("xyz")
    x_val = np.random.random((5, 5)).astype(config.floatX)
    y_val = np.random.random((5, 5)).astype(config.floatX)
    z_val = np.random.random((5, 5)).astype(config.floatX)
    mode = config.mode
    if mode == "FAST_COMPILE":
        mode = "FAST_RUN"
    mode = get_mode(mode).excluding("local_elemwise_fusion")

    f = function([y, z], (abs(y * z * -2)), mode=mode)
    f(y_val, z_val)
    assert isinstance(f.maker.fgraph.toposort()[1].op.scalar_op, ps.Abs)
    assert len(f.maker.fgraph.toposort()) == 2

    f = function([x, y], abs(x / y), mode=mode)
    f(x_val, y_val)
    assert isinstance(f.maker.fgraph.toposort()[1].op.scalar_op, ps.Abs)
    assert len(f.maker.fgraph.toposort()) == 2


def test_merge_abs_bugfix():
    """
    See https://groups.google.com/d/topic/theano-users/TaXfqXP2Mj0/discussion
    """
    input = matrix()
    # normalize on cols
    step1 = input / input.sum(0)
    # normalize on rows
    step2 = step1 / step1.sum(1)
    # get l1 norm
    l1_norm = pt_abs(step2).sum()
    function([input], pytensor.gradient.grad(l1_norm, input))


def test_mixeddiv():
    # Test that int division is preserved
    i = iscalar()
    d = dscalar()
    assert 0 == function([i, d], d * (i // (i + 1)))(3, 1.0)


def test_const_type_in_mul_canonizer():
    input = dmatrix()
    w = dmatrix()
    visb = dvector()
    hidb = dvector()
    betas = dvector()
    a = dvector()

    def sigm(x):
        return 1.0 / (1 + exp(-x))

    hid = sigm((dot(w, input) + hidb) * betas)

    vis_gauss1 = (dot(w.T, hid) + visb) * betas / (2 * a * a)
    vis_gauss2 = (dot(w.T, hid) + visb) * betas / (2.0 * a * a)

    f1 = function([input, w, visb, hidb, betas, a], vis_gauss1)
    f2 = function([input, w, visb, hidb, betas, a], vis_gauss2)

    ival = np.random.random((5, 5))
    wval = np.random.random((5, 5))
    visbval = np.random.random(5)
    hidbval = np.random.random(5)
    betaval = np.random.random(5)
    aval = np.random.random(5)

    utt.assert_allclose(
        f2(ival, wval, visbval, hidbval, betaval, aval),
        f1(ival, wval, visbval, hidbval, betaval, aval),
    )


def test_cast_in_mul_canonizer():
    x, y = vectors("xy")
    m = minimum(x, y)
    o = m.sum()
    go = pt.fill(o, 1)
    e = eq(go, x)
    o1 = (1 - e) * go
    o2 = e * go
    mode = get_default_mode().excluding("fusion").including("fast_run")
    f = function([x, y], [o1, o2], mode=mode)
    nodes = f.maker.fgraph.apply_nodes
    assert (
        len(
            [
                n
                for n in nodes
                if isinstance(getattr(n.op, "scalar_op", None), ps.Identity)
            ]
        )
        == 0
    )
    assert len([n for n in nodes if isinstance(n.op.scalar_op, ps.Cast)]) == 1
    f([1], [1])


@utt.assertFailure_fast
def test_log1p():
    m = config.mode
    if m == "FAST_COMPILE":
        m = "FAST_RUN"
    m = get_mode(m)
    m = m.excluding("fusion")
    # check some basic cases
    x = dvector()
    f = function([x], log(1 + (x)), mode=m)
    assert [node.op for node in f.maker.fgraph.toposort()] == [log1p]
    f = function([x], log(1 + (-x)), mode=m)
    assert [node.op for node in f.maker.fgraph.toposort()] == [
        neg,
        inplace.log1p_inplace,
    ]
    f = function([x], -log(1 + (-x)), mode=m)
    assert [node.op for node in f.maker.fgraph.toposort()] == [
        neg,
        inplace.log1p_inplace,
        inplace.neg_inplace,
    ]

    # check trickier cases (and use different dtype)
    y = fmatrix()
    f = function([x, y], log(pt.fill(y, 1) + (x)), mode=m)
    # the first three ops are Shape_i, Shape_i, and Dimshuffle
    topo = f.maker.fgraph.toposort()
    assert topo[-1].op == pt.alloc
    assert log1p in [node.op for node in topo]

    f = function([x, y], log(0 + (x) + pt.fill(y, 1.0)), mode=m)
    topo = f.maker.fgraph.toposort()
    assert topo[-1].op == pt.alloc
    assert log1p in [node.op for node in topo]

    f = function([x, y], log(2 + (x) - pt.fill(y, 1.0)), mode=m)
    topo = f.maker.fgraph.toposort()
    assert topo[-1].op == pt.alloc
    assert log1p in [node.op for node in topo]

    f([1e-7, 10], [[0, 0], [0, 0]])  # debugmode will verify values

    # should work for int
    z = imatrix()
    f = function([z], log(1 + (z)), mode=m)
    assert [node.op for node in f.maker.fgraph.toposort()] == [log1p]


def test_local_log_add_exp():
    m = config.mode
    if m == "FAST_COMPILE":
        m = "FAST_RUN"
    m = get_mode(m)
    m = m.excluding("fusion")
    m = copy.copy(m)
    # No need to put them back as we have a new object
    m.check_isfinite = False

    # check some basic cases
    x = dvector()
    y = dvector()
    f = function([x, y], log(exp(x) + exp(y)), mode=m)

    # test that it gives the correct result when it doesn't overflow
    f([10], [10])  # doesn't causes overflow
    utt.assert_allclose(f([10], [10]), 10 + np.log1p(1))

    assert np.isfinite(f([10000], [10000]))  # causes overflow if handled incorrectly
    utt.assert_allclose(f([10000], [10000]), 10000 + np.log1p(1))

    # test that when max = +-inf, rewritten output still works correctly
    assert f([-np.inf], [-np.inf]) == -np.inf
    assert f([np.inf], [np.inf]) == np.inf
    assert f([np.inf], [-np.inf]) == np.inf

    # test that it also works with more than two args
    x = dvector()
    y = dvector()
    f = function([x, y], log(exp(x) + exp(y) + exp(x - y) + exp(x + y)), mode=m)

    assert np.isfinite(f([10000], [10000]))  # causes overflow if handled incorrectly
    utt.assert_allclose(f([10000], [10000]), 20000)

    # TODO: test that the rewrite works in the presence of broadcasting.


def test_local_elemwise_sub_zeros():
    scal = scalar()
    vect = vector()
    mat = matrix()

    rng = np.random.default_rng(seed=utt.fetch_seed())
    scalar_val = rng.random(1).astype(config.floatX)[0]
    vect_val = rng.random(5).astype(config.floatX)
    mat_val = rng.random((3, 2)).astype(config.floatX)

    mode = (
        get_default_mode()
        .excluding(
            "canonicalize",
            "uncanonicalize",
            "ShapeOpt",
            "local_fill_to_alloc",
            "local_elemwise_alloc",
        )
        .including("local_elemwise_sub_zeros")
    )

    # Test scalar minus scalar
    f = function([scal], scal - scal, mode=mode)
    assert isinstance(f.maker.fgraph.toposort()[0].op, Elemwise)
    assert isinstance(f.maker.fgraph.toposort()[0].op.scalar_op, ps.Second)
    assert isinstance(
        f.maker.fgraph.toposort()[0].inputs[1], TensorConstant
    ) or isinstance(f.maker.fgraph.toposort()[0].inputs[1], TensorConstant)
    utt.assert_allclose(f(scalar_val), 0.0)
    assert check_stack_trace(f, ops_to_check="all")

    # Test vector minus vector
    f = function([vect], vect - vect, mode=mode)
    assert isinstance(f.maker.fgraph.toposort()[0].op, Elemwise)
    assert isinstance(f.maker.fgraph.toposort()[0].op.scalar_op, ps.Second)
    assert isinstance(
        f.maker.fgraph.toposort()[0].inputs[1], TensorConstant
    ) or isinstance(f.maker.fgraph.toposort()[0].inputs[1], TensorConstant)
    utt.assert_allclose(f(vect_val), np.zeros(vect_val.shape))
    assert check_stack_trace(f, ops_to_check="all")

    # Test vector minus vector
    f = function([mat], mat - mat, mode=mode)
    assert isinstance(f.maker.fgraph.toposort()[0].op, Elemwise)
    assert isinstance(f.maker.fgraph.toposort()[0].op.scalar_op, ps.Second)
    assert isinstance(
        f.maker.fgraph.toposort()[0].inputs[1], TensorConstant
    ) or isinstance(f.maker.fgraph.toposort()[0].inputs[1], TensorConstant)
    utt.assert_allclose(f(mat_val), np.zeros(mat_val.shape))
    assert check_stack_trace(f, ops_to_check="all")


class TestLocalUselessElemwiseComparison:
    def setup_method(self):
        self.rng = np.random.default_rng(utt.fetch_seed())

    def test_local_useless_elemwise_comparison(self):
        # TODO FIXME: This is not a real test!
        # TODO: test each case individually.
        # The following case is what made me discover those cases.
        X = matrix("X")
        Y = vector("Y")
        X_sum, updates = pytensor.scan(
            fn=lambda x: x.sum(), outputs_info=None, sequences=[X], non_sequences=None
        )
        Z = X_sum + Y
        # pytensor.printing.debugprint(Z)
        # here is the output for the debug print:
        """
        Elemwise{add,no_inplace} [id A] ''
         |for{cpu,scan_fn} [id B] ''
         | |Subtensor{int64} [id C] ''
         | | |Shape [id D] ''
         | | | |Subtensor{int64::} [id E] 'X[0:]'
         | | |   |X [id F]
         | | |   |Constant{0} [id G]
         | | |Constant{0} [id H]
         | |Subtensor{:int64:} [id I] ''
         | | |Subtensor{int64::} [id E] 'X[0:]'
         | | |ScalarFromTensor [id J] ''
         | |   |Subtensor{int64} [id C] ''
         | |Subtensor{int64} [id C] ''
         |Y [id K]

        Inner graphs:

        for{cpu,scan_fn} [id B] ''
         >Sum{acc_dtype=float64} [id L] ''
         > |X[t] [id M] -> [id I]
        """

        mode = get_default_mode().excluding("fusion")
        f = function([X, Y], Z, mode=mode)
        f(
            self.rng.random((2, 3)).astype(config.floatX),
            self.rng.random(2).astype(config.floatX),
        )
        # pytensor.printing.debugprint(f, print_type=True)
        # here is the output for the debug print:
        """
        Elemwise{Add}[(0, 0)] [id A] <TensorType(float64, vector)> ''   7
         |for{cpu,scan_fn} [id B] <TensorType(float64, vector)> ''   6
         | |Shape_i{0} [id C] <TensorType(int64, scalar)> ''   0
         | | |X [id D] <TensorType(float64, matrix)>
         | |Subtensor{int64:int64:int8} [id E] <TensorType(float64, matrix)> ''   5
         | | |X [id D] <TensorType(float64, matrix)>
         | | |ScalarFromTensor [id F] <int64> ''   4
         | | | |Elemwise{switch,no_inplace} [id G] <TensorType(int64, scalar)> ''   3
         | | |   |Elemwise{le,no_inplace} [id H] <TensorType(int8, scalar)> ''   2
         | | |   | |Shape_i{0} [id C] <TensorType(int64, scalar)> ''   0
         | | |   | |TensorConstant{0} [id I] <TensorType(int8, scalar)>
         | | |   |TensorConstant{0} [id I] <TensorType(int8, scalar)>
         | | |   |TensorConstant{0} [id J] <TensorType(int64, scalar)>
         | | |ScalarFromTensor [id K] <int64> ''   1
         | | | |Shape_i{0} [id C] <TensorType(int64, scalar)> ''   0
         | | |Constant{1} [id L] <int8>
         | |Shape_i{0} [id C] <TensorType(int64, scalar)> ''   0
         |Y [id M] <TensorType(float64, vector)>

        Inner graphs:

        for{cpu,scan_fn} [id B] <TensorType(float64, vector)> ''
         >Sum{acc_dtype=float64} [id N] <TensorType(float64, scalar)> ''
         > |X[t] [id O] <TensorType(float64, vector)> -> [id E]
        """

    def assert_eqs_const(self, f, val, op=deep_copy_op):
        topo = f.maker.fgraph.toposort()
        elem = topo[0]
        assert len(topo) == 1, topo
        assert elem.op == op, elem.op
        if op == deep_copy_op:
            assert len(elem.inputs) == 1, elem.inputs
            assert isinstance(elem.inputs[0], TensorConstant), elem
            assert pt.get_underlying_scalar_constant_value(elem.inputs[0]) == val, val
        else:
            assert len(elem.inputs) == 2, elem.inputs
            assert isinstance(elem.inputs[0], TensorConstant), elem
            assert pt.get_underlying_scalar_constant_value(elem.inputs[0]) == val, val

    def assert_identity(self, f):
        topo = f.maker.fgraph.toposort()
        assert len(topo) == 1
        assert topo[0].op == deep_copy_op
        if f.outputs[0].variable.dtype == "bool":
            x_vals = [0, 1]
        else:
            x_vals = [0, 1, 10]
        for x_val in x_vals:
            assert f(x_val) == x_val

    def test_inequality_with_self(self):
        x = scalar("x", dtype=config.floatX)
        mode = get_default_mode().including("local_useless_elemwise_comparison")

        f = function([x], lt(x, x), mode=mode)
        self.assert_eqs_const(f, 0)

        f = function([x], le(x, x), mode=mode)
        self.assert_eqs_const(f, 1)

        f = function([x], gt(x, x), mode=mode)
        self.assert_eqs_const(f, 0)

        f = function([x], ge(x, x), mode=mode)
        self.assert_eqs_const(f, 1)

        f = function([x], minimum(x, x), mode=mode)
        self.assert_identity(f)

        f = function([x], maximum(x, x), mode=mode)
        self.assert_identity(f)

    def test_shape_inequality_with_self(self):
        x = vector("x", dtype=config.floatX)
        mode = get_default_mode().including(
            "local_useless_elemwise_comparison",
            "local_shape_to_shape_i",
            "local_track_shape_i",
            "local_subtensor_make_vector",
            "local_subtensor_remove_broadcastable_index",
            "local_useless_dimshuffle_makevector",
        )
        f = function([x], lt(x.shape[0], 0), mode=mode)
        self.assert_eqs_const(f, 0)

        f = function([x], ge(x.shape[0], 0), mode=mode)
        self.assert_eqs_const(f, 1)

        f = function([x], maximum(x.shape[0], 0), mode=mode)
        topo = f.maker.fgraph.toposort()
        assert len(topo) == 1
        assert isinstance(topo[0].op, Shape_i), topo[0].op
        x_val = np.ones(100, dtype=config.floatX)
        assert f(x_val) == x_val.shape[0]

        f = function([x], maximum(0, x.shape[0]), mode=mode)
        topo = f.maker.fgraph.toposort()
        assert len(topo) == 1
        assert isinstance(topo[0].op, Shape_i), topo[0].op
        x_val = np.ones(100, dtype=config.floatX)
        assert f(x_val) == x_val.shape[0]

        f = function([x], minimum(x.shape[0], 0), mode=mode)
        self.assert_eqs_const(f, 0)
        assert f(x_val) == 0

        f = function([x], minimum(0, x.shape[0]), mode=mode)
        self.assert_eqs_const(f, 0)
        assert f(x_val) == 0
        f = function([x], minimum([0, 0], x.shape[0]), mode=mode)
        # This case isn't rewritten.
        # self.assert_eqs_const(f, 0)
        utt.assert_allclose(f(x_val), [0, 0])

    def test_shape_add_inequality(self):
        x = vector("x", dtype=config.floatX)
        mode = get_default_mode().including(
            "local_useless_elemwise_comparison",
            "local_shape_to_shape_i",
            "local_track_shape_i",
            "local_subtensor_make_vector",
        )

        y = vector("y", dtype=config.floatX)

        f = function([x, y], lt(x.shape[0] + y.shape[0], 0), mode=mode)
        self.assert_eqs_const(f, 0)

        f = function([x, y], ge(x.shape[0] + y.shape[0], 0), mode=mode)
        self.assert_eqs_const(f, 1)

    @pytest.mark.skipif(
        config.mode == "FAST_COMPILE",
        reason="This rewrite is disabled.",
    )
    def test_equality_shapes(self):
        # Test equality where one sides contain only shapes related
        # stuff.
        x = vector("x", dtype=config.floatX)
        for g in [x.shape[0], Shape_i(0)(x)]:
            f = function([x], eq(g, 0))
            assert f([3, 3]) == 0
            assert f([]) == 1

            f = function([x], eq(g, -1))
            self.assert_eqs_const(f, 0)
            assert f([3, 3]) == 0

        g = join(0, x.shape[0:], x.shape[0:1])  # todo test reshape, dimshuffle
        f = function([x], eq(g, 0))
        assert (f([3, 3]) == 0).all()
        assert (f([]) == 1).all()

        f = function([x], eq(g, -1))
        self.assert_eqs_const(f, 0, op=pt.alloc)
        assert (f([3, 3]) == 0).all()

    def test_and(self):
        # bitwise "and" with 0 should give 0 for both bool and int
        # bitwise "and" with 1 should only simplify for bool
        mode = get_default_mode().including("canonicalize")
        for dtype, zero, one in [
            ("bool", np.array(False), np.array(True)),
            ("int8", np.int8(0), np.int8(1)),
            ("int8", 0, 1),
        ]:
            x = scalar("x", dtype=dtype)

            f = function([x], bitwise_and(x, zero), mode=mode)
            self.assert_eqs_const(f, 0)

            f = function([x], bitwise_and(zero, x), mode=mode)
            self.assert_eqs_const(f, 0)

            f = function([x], bitwise_and(x, one), mode=mode)
            if dtype == "bool":
                self.assert_identity(f)

            f = function([x], bitwise_and(one, x), mode=mode)
            if dtype == "bool":
                self.assert_identity(f)

    def test_and_int(self):
        # Test that bitwise "and" is correctly computed on int constants.
        f = function([], bitwise_and(5, 6))
        assert f() == 4

    def test_or(self):
        # bitwise "or" with 0 should simplify for both bool and int
        # bitwise "or" with 1 should only give 1 for bool
        mode = get_default_mode().including("canonicalize")
        for dtype, zero, one in [
            ("bool", np.array(False), np.array(True)),
            ("int8", np.int8(0), np.int8(1)),
            ("int8", 0, 1),
        ]:
            x = scalar("x", dtype=dtype)

            f = function([x], bitwise_or(x, one), mode=mode)
            if dtype == "bool":
                self.assert_eqs_const(f, 1)

            f = function([x], bitwise_or(one, x), mode=mode)
            if dtype == "bool":
                self.assert_eqs_const(f, 1)

            f = function([x], bitwise_or(x, zero), mode=mode)
            self.assert_identity(f)

            f = function([x], bitwise_or(zero, x), mode=mode)
            self.assert_identity(f)

    def test_or_int(self):
        # Test that bitwise "or" is correctly computed on int constants.
        f = function([], bitwise_or(5, 6))
        assert f() == 7

    def test_xor(self):
        # bitwise "xor" with itself should always give 0 for both bool and int.
        mode = get_default_mode().including("canonicalize")
        for dtype in ("bool", "int8"):
            x = scalar("x", dtype=dtype)

            f = function([x], xor(x, x), mode=mode)
            self.assert_eqs_const(f, 0)

    def test_stacktrace(self):
        mode = get_default_mode().including("local_useless_elemwise_comparison")

        x = vector("x", dtype=config.floatX)
        f = function([x], gt(x, x), mode=mode)
        assert check_stack_trace(f, ops_to_check="last")

        f = function([x], le(x, x), mode=mode)
        assert check_stack_trace(f, ops_to_check="last")


def test_local_mul_specialize():
    mode = config.mode
    if mode == "FAST_COMPILE":
        mode = "FAST_RUN"
    mode = get_mode(mode)
    mode = mode.excluding("fusion")

    v = vector()
    m = vector()

    f = function([v], v * 1, mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    nodes == [deep_copy_op]

    f = function([v], v * 0, mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert nodes == [Shape_i(0), pt.alloc]

    f = function([v], v * (-1), mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert nodes == [neg]

    f = function([v, m], v * 1 * (-m), mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert nodes == [mul]

    f = function([v, m], v * 0 * (-m), mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert nodes == [Shape_i(0), pt.alloc]

    f = function([v, m], v * (-1) * (-m), mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert nodes == [mul]

    f = function([v, m], v * (-1) * m, mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert nodes == [mul]


def speed_local_pow_specialize_range():
    # TODO: This should be a benchmark test
    val = np.random.random(1e7)
    v = vector()
    mode = get_default_mode()
    mode_without_pow_rewrite = mode.excluding("local_pow_specialize")
    for i in range(500, 513):
        f1 = function([v], v**i, mode=mode)
        f2 = function([v], v**i, mode=mode_without_pow_rewrite)
        assert len(f1.maker.fgraph.toposort()) == 1
        t1 = time.perf_counter()
        f1(val)
        t2 = time.perf_counter()
        f2(val)
        t3 = time.perf_counter()
        # print(i, t2 - t1, t3 - t2, t2 - t1 < t3 - t2)
        if not t2 - t1 < t3 - t2:
            raise ValueError("WARNING WE ARE SLOWER")
    for i in range(-3, -1500, -1):
        f1 = function([v], v**i, mode=mode)
        f2 = function([v], v**i, mode=mode_without_pow_rewrite)
        assert len(f1.maker.fgraph.toposort()) == 1
        t1 = time.perf_counter()
        f1(val)
        t2 = time.perf_counter()
        f2(val)
        t3 = time.perf_counter()
        # print(i, t2 - t1, t3 - t2, t2 - t1 < t3 - t2)
        if not t2 - t1 < t3 - t2:
            raise ValueError("WARNING WE ARE SLOWER")


def test_local_pow_specialize():
    mode = config.mode
    if mode == "FAST_COMPILE":
        mode = "FAST_RUN"
    mode = get_mode(mode)
    mode = mode.excluding("fusion")

    v = vector()
    val = np.arange(10, dtype=config.floatX)
    val_no0 = np.arange(1, 10, dtype=config.floatX)

    f = function([v], v**0, mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert nodes == [Shape_i(0), pt.alloc]
    utt.assert_allclose(f(val), val**0)

    f = function([v], v**1, mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    nodes == [deep_copy_op]
    utt.assert_allclose(f(val), val**1)

    f = function([v], v ** (-1), mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert nodes == [reciprocal]
    utt.assert_allclose(f(val_no0), val_no0 ** (-1))

    f = function([v], v**2, mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert nodes == [sqr]
    utt.assert_allclose(f(val), val**2)

    f = function([v], v ** (-2), mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert len(nodes) == 2
    assert nodes[0] == sqr
    assert isinstance(nodes[1].scalar_op, ps.basic.Reciprocal)
    utt.assert_allclose(f(val_no0), val_no0 ** (-2))

    f = function([v], v ** (0.5), mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert nodes == [sqrt]
    utt.assert_allclose(f(val), val ** (0.5))

    f = function([v], v ** (-0.5), mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert len(nodes) == 2
    assert nodes[0] == sqrt
    assert isinstance(nodes[1].scalar_op, ps.basic.Reciprocal)
    utt.assert_allclose(f(val_no0), val_no0 ** (-0.5))

    twos = np.full(shape=(10,), fill_value=2.0).astype(config.floatX)
    f = function([v], v**twos, mode=mode)
    topo = f.maker.fgraph.toposort()
    assert len(topo) == 2
    # Depending on the mode the SpecifyShape is lifted or not
    if topo[0].op == sqr:
        assert isinstance(topo[1].op, SpecifyShape)
    else:
        assert isinstance(topo[0].op, SpecifyShape)
        assert topo[1].op == sqr
    utt.assert_allclose(f(val), val**twos)


def test_local_pow_to_nested_squaring():
    mode = config.mode
    if mode == "FAST_COMPILE":
        mode = "FAST_RUN"
    mode = get_mode(mode)
    mode = mode.excluding("fusion")

    v = vector()
    val = np.arange(10, dtype=config.floatX)
    val_no0 = np.arange(1, 10, dtype=config.floatX)
    f = function([v], v ** (15), mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert len(nodes) == 1
    assert len(f.maker.fgraph.toposort()[0].op.scalar_op.fgraph.apply_nodes) == 6
    assert isinstance(nodes[0].scalar_op, ps.Composite)
    utt.assert_allclose(f(val), val**15)

    f = function([v], v ** (-15), mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert len(nodes) == 2
    assert len(f.maker.fgraph.toposort()[0].op.scalar_op.fgraph.apply_nodes) == 6
    assert isinstance(nodes[0].scalar_op, ps.Composite)
    assert isinstance(nodes[-1].scalar_op, ps.basic.Reciprocal)
    utt.assert_allclose(f(val_no0), val_no0 ** (-15))

    f = function([v], v ** (16), mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert len(nodes) == 1
    assert len(f.maker.fgraph.toposort()[0].op.scalar_op.fgraph.apply_nodes) == 4
    assert isinstance(nodes[0].scalar_op, ps.Composite)
    utt.assert_allclose(f(val), val**16)

    f = function([v], v ** (-16), mode=mode)
    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert len(nodes) == 2
    assert len(f.maker.fgraph.toposort()[0].op.scalar_op.fgraph.apply_nodes) == 4
    assert isinstance(nodes[0].scalar_op, ps.Composite)
    assert isinstance(nodes[-1].scalar_op, ps.basic.Reciprocal)
    utt.assert_allclose(f(val_no0), val_no0 ** (-16))


def test_local_pow_to_nested_squaring_works_with_static_type():
    # Reported in #456

    x = vector("x", shape=(1,))
    # Create an Apply that does not have precise output shape
    node = Apply(
        op=pt_pow,
        inputs=[x, constant([2.0])],
        outputs=[tensor(shape=(None,))],
    )
    y = node.default_output()

    fn = function([x], y)

    np.testing.assert_allclose(fn([2.0]), np.array([4.0]))


class TestFuncInverse:
    def setup_method(self):
        mode = get_default_mode()
        self.mode = mode.including("local_func_inv")

    def assert_func_pair_rewritten(
        self, func1, func2, data, should_copy=True, is_complex=False
    ):
        """Check that a pair of functions are rewritten properly."""

        x = cmatrix() if is_complex else fmatrix()
        o = func2(func1(x))
        f = function([x], o, mode=self.mode)
        delta = f(data) - data
        topo = f.maker.fgraph.toposort()

        if should_copy:
            acceptable_topo_lens = [1]
        else:
            # The 2 funcs can be split apart if they are not inverses
            acceptable_topo_lens = [1, 2]

        if should_copy:
            delta_condition = np.all(delta == 0)
        else:
            delta_condition = np.all(delta != 0)

        assert len(topo) in acceptable_topo_lens
        assert delta_condition
        assert (
            isinstance(topo[0].op, DeepCopyOp) == should_copy
        ), "Inverse functions not removed!"

    def test(self):
        """Test rewrites for consecutive functional inverses."""

        dx = np.random.random((5, 4)).astype("float32")
        self.assert_func_pair_rewritten(deg2rad, rad2deg, dx)
        dx = np.random.random((5, 4)).astype("float32") * 180
        self.assert_func_pair_rewritten(rad2deg, deg2rad, dx)

        # Test the other functional inverses
        dx = np.random.random((5, 4)).astype("float32")
        self.assert_func_pair_rewritten(cosh, arccosh, dx)
        self.assert_func_pair_rewritten(arcsinh, sinh, dx)
        self.assert_func_pair_rewritten(arctanh, tanh, dx)
        self.assert_func_pair_rewritten(reciprocal, reciprocal, dx)
        self.assert_func_pair_rewritten(neg, neg, dx)
        cx = dx + complex(0, 1) * (dx + 0.01)
        self.assert_func_pair_rewritten(conj, conj, cx, is_complex=True)

        # Test that non-inverse functions are ran normally
        self.assert_func_pair_rewritten(
            conj, neg, cx, should_copy=False, is_complex=True
        )
        dx = np.random.random((5, 4)).astype("float32") + 0.01
        self.assert_func_pair_rewritten(rad2deg, rad2deg, dx, should_copy=False)
        self.assert_func_pair_rewritten(rad2deg, cosh, dx, should_copy=False)

    def test_integer_upcast(self):
        """
        All invertible methods (except for `Neg`) can upgrade their input to float.
        Here we test that the rewrite works with just one pair of methods
        """
        x = ivector("x")
        f = function([x], deg2rad(rad2deg(x)), mode=self.mode)
        topo = f.maker.fgraph.toposort()
        assert len(topo) == 1


class TestExpLog:
    def setup_method(self):
        mode = get_default_mode()
        self.mode = mode.including(
            "local_exp_log",
            "local_exp_log_nan_switch",
        ).excluding("fusion")

    def test_log_exp(self):
        # log(exp(x)) -> x
        data = np.random.random((4, 3)).astype("float32")
        x = fmatrix()
        f = function([x], log(exp(x)), mode=self.mode)
        graph = f.maker.fgraph.toposort()
        ops_graph = [
            node
            for node in graph
            if isinstance(node.op, Elemwise)
            and isinstance(node.op.scalar_op, ps.Log | ps.Exp)
        ]
        assert len(ops_graph) == 0
        np.testing.assert_array_equal(f(data), data)

    def test_log_exp_integer_upcast(self):
        x = ivector("x")
        f = function([x], log(exp(x)), mode=self.mode)
        ops_graph = [
            node
            for node in f.maker.fgraph.toposort()
            if isinstance(node.op, Elemwise)
            and isinstance(node.op.scalar_op, ps.Log | ps.Exp)
        ]
        assert len(ops_graph) == 0

    @pytest.mark.parametrize("dtype", ["float32", "int32"])
    def test_log1p_expm1(self, dtype):
        # log1p(expm1(x)) -> x
        data = (np.random.random((4, 3)) * 100).astype(dtype)
        x = matrix(dtype=dtype)
        f = function([x], log1p(expm1(x)), mode=self.mode, allow_input_downcast=True)
        graph = f.maker.fgraph.toposort()
        ops_graph = [
            node
            for node in graph
            if isinstance(node.op, Elemwise)
            and isinstance(node.op.scalar_op, ps.Log | ps.Exp | ps.Log1p | ps.Expm1)
        ]
        assert len(ops_graph) == 0
        np.testing.assert_array_equal(f(data), data)

    @pytest.mark.parametrize("exp_op", [exp, expm1])
    def test_exp_log(self, exp_op):
        # exp(log(x)) -> switch(x >= 0, x, nan)
        # expm1(log(x)) -> switch(x >= 0, x - 1, nan)
        data_valid = np.random.random((4, 3)).astype("float32")
        data_valid[0, 0] = 0  # edge case
        data_invalid = data_valid - 1

        x = fmatrix()
        f = function([x], exp_op(log(x)), mode=self.mode)
        graph = f.maker.fgraph.toposort()
        ops_graph = [
            node
            for node in graph
            if isinstance(node.op, Elemwise)
            and isinstance(node.op.scalar_op, ps.Log | ps.Log1p | ps.Exp | ps.Expm1)
        ]
        assert len(ops_graph) == 0

        if exp_op == exp:
            expected = data_valid
        else:
            expected = data_valid - 1
        np.testing.assert_almost_equal(f(data_valid), expected)
        assert np.all(np.isnan(f(data_invalid)))

    @pytest.mark.parametrize("exp_op", [exp, expm1])
    def test_exp_log1p(self, exp_op):
        # exp(log1p(x)) -> switch(x >= -1, x + 1, nan)
        # expm1(log1p(x)) -> switch(x >= -1, x, nan)
        data_valid = np.random.random((4, 3)).astype("float32") * 2 - 1
        data_valid[0, 0] = -1  # edge case
        data_invalid = data_valid - 2

        x = fmatrix()
        f = function([x], exp_op(log1p(x)), mode=self.mode)
        graph = f.maker.fgraph.toposort()
        ops_graph = [
            node
            for node in graph
            if isinstance(node.op, Elemwise)
            and isinstance(node.op.scalar_op, ps.Log | ps.Log1p | ps.Exp | ps.Expm1)
        ]
        assert len(ops_graph) == 0

        if exp_op == exp:
            expected = data_valid + 1
        else:
            expected = data_valid
        np.testing.assert_almost_equal(f(data_valid), expected)
        assert np.all(np.isnan(f(data_invalid)))

    @pytest.mark.parametrize("exp_op", [exp, expm1])
    def test_exp_log1mexp(self, exp_op):
        # exp(log1mexp(x)) -> switch(x <= 0, 1 - exp(x), nan)
        # expm1(log1mexp(x)) -> switch(x <= 0, - exp(x), nan)
        data_valid = -np.random.random((4, 3)).astype("float32")
        data_valid[0, 0] = 0  # edge case
        data_invalid = data_valid + 1

        x = fmatrix()
        f = function([x], exp_op(log1mexp(x)), mode=self.mode)
        graph = f.maker.fgraph.toposort()
        ops_graph = [
            node
            for node in graph
            if isinstance(node.op, Elemwise)
            and isinstance(
                node.op.scalar_op, ps.Log | ps.Log1p | ps.Log1mexp | ps.Expm1
            )
        ]
        assert len(ops_graph) == 0

        if exp_op == exp:
            expected = 1 - np.exp(data_valid)
        else:
            expected = -np.exp(data_valid)
        np.testing.assert_almost_equal(f(data_valid), expected)
        assert np.all(np.isnan(f(data_invalid)))

    @pytest.mark.parametrize("exp_op", [exp, expm1])
    def test_exp_softplus(self, exp_op):
        # exp(softplus(x)) -> 1 + exp(x)
        # expm1(softplus(x)) -> exp(x)
        data_valid = np.random.random((4, 3)).astype("float32") * 2 - 1

        x = fmatrix()
        f = function([x], exp_op(softplus(x)), mode=self.mode)
        graph = f.maker.fgraph.toposort()
        ops_graph = [
            node
            for node in graph
            if isinstance(node.op, Elemwise)
            and isinstance(
                node.op.scalar_op,
                ps.Log | ps.Log1p | ps.Softplus | ps.Expm1 | ps.Switch,
            )
        ]
        assert len(ops_graph) == 0

        if exp_op == exp:
            expected = 1 + np.exp(data_valid)
        else:
            expected = np.exp(data_valid)
        np.testing.assert_almost_equal(
            f(data_valid),
            expected,
            decimal=6,
        )

    @pytest.mark.parametrize(
        ["nested_expression", "expected_switches"],
        [
            (lambda x: exp(log(exp(log(exp(x))))), 0),
            (lambda x: exp(log(exp(log(x)))), 1),
        ],
    )
    def test_exp_log_nested(self, nested_expression, expected_switches):
        # Make sure nested exp-log graphs have as little `nan` switches as necessary
        x = fvector()
        f = function([x], nested_expression(x), mode=self.mode)
        graph = f.maker.fgraph.toposort()
        ops_graph = [
            node
            for node in graph
            if isinstance(node.op, Elemwise)
            and isinstance(node.op.scalar_op, ps.Switch)
        ]
        assert len(ops_graph) == expected_switches


class TestSqrSqrt:
    def setup_method(self):
        mode = get_default_mode()
        self.mode = mode.including(
            "local_sqrt_sqr",
        ).excluding("fusion")
        self.rng = np.random.default_rng()

    def test_sqr_sqrt(self):
        # sqrt(x) ** 2 -> x
        x = pt.tensor("x", shape=(None, None))
        out = sqr(sqrt(x))
        out = rewrite_graph(out, include=["canonicalize", "specialize", "stabilize"])

        assert equal_computations([out], [pt_abs(x)])

    def test_sqrt_sqr(self):
        x = pt.tensor("x", shape=(None, None))
        out = sqrt(sqr(x))
        out = rewrite_graph(out, include=["canonicalize", "specialize", "stabilize"])

        expected = switch(
            ge(x, np.zeros((1, 1), dtype="int8")),
            x,
            np.full((1, 1), np.nan, dtype=x.type.dtype),
        )

        assert equal_computations([out], [expected])

    def test_sqr_sqrt_integer_upcast(self):
        x = ivector("x")
        out = sqr(sqrt(x))
        dtype = out.type.dtype
        out = rewrite_graph(out, include=["canonicalize", "specialize", "stabilize"])

        expected = pt.cast(pt_abs(x), dtype=dtype)
        assert equal_computations([out], [expected])


class TestLocalSwitchSink:
    def setup_method(self):
        # condition values
        self.condm = np.asarray([[0.1, 0, 1, -1], [0.0, 0.0, 0.0, 0.0], [1, 1, 1, 1]])
        self.condv = np.asarray([0.1, 0, 1, -1])
        self.conds = [0.1, 0, 1, -1]

        # x values
        self.xm = np.ones((3, 4))
        self.xv = np.ones((4,))
        self.xs = 1.0

        # expected results
        self.resm = (
            [np.asarray([[1, 0, 1, 0], [0, 0, 0, 0], [1, 1, 1, 1]])] * 3
            + [np.asarray([[1, 0, 1, 0], [1, 0, 1, 0], [1, 0, 1, 0]])]
            + 2 * [np.asarray([[1, 0, 1, 0]])]
            + [[np.ones((3, 4)), np.zeros((3, 4)), np.ones((3, 4)), np.zeros((3, 4))]]
            + [[np.ones((4,)), np.zeros((4,)), np.ones((4,)), np.zeros((4,))]]
            + [[np.asarray(1.0), np.asarray(0.0), np.asarray(1.0), np.asarray(0.0)]]
        )

        self.mode = (
            get_default_mode()
            .including("canonicalize", "fast_run")
            .excluding("gpu", "fusion")
        )
        self.mode = copy.copy(self.mode)
        self.mode.check_isfinite = False

    def function_remove_nan(self, *args, **kwargs):
        """Wrapper around function for this test.

        It disables checking for NaNs removed by rewrites in `DebugMode`
        (it has false positives in that case).
        """
        f = function(*args, **kwargs)

        def wrapped_f(*args, **kwargs):
            # This is a bit ugly since it changes the global value of
            # TensorType.values_eq_approx.
            old_values_eq_approx = staticmethod(TensorType.values_eq_approx)
            TensorType.values_eq_approx = staticmethod(values_eq_approx_remove_nan)
            try:
                out = f(*args, **kwargs)
            finally:
                TensorType.values_eq_approx = old_values_eq_approx
            return out

        return wrapped_f

    def test_local_mul_switch_sink(self):
        c = dscalar()
        idx = 0
        for condition in [
            (dmatrix("cond"), self.condm),
            (dvector("cond"), self.condv),
            (dscalar("cond"), self.conds),
        ]:
            for x in [
                (dmatrix("x"), self.xm),
                (dvector("x"), self.xv),
                (dscalar("x"), self.xs),
            ]:
                y = mul(
                    pt.switch(condition[0] > 0, 1.0 * x[0], 0.0 * x[0]),
                    pt.switch(condition[0] > 0, 1.0 * x[0], log(c) * x[0]),
                )
                f = self.function_remove_nan(
                    [condition[0], x[0], c], [y], mode=self.mode
                )
                if isinstance(condition[1], list):
                    for i in range(len(condition[1])):
                        res = f(condition[1][i], x[1], -1)
                        assert (
                            res == np.asarray(self.resm[idx][i])
                        ).sum() == self.resm[idx][i].size
                else:
                    res = f(condition[1], x[1], -1)
                    assert (res == np.asarray(self.resm[idx])).sum() == self.resm[
                        idx
                    ].size
                idx += 1

        # This case prevented a rewrite from being applied in the past
        x = dscalar("x")
        y = pt.switch(x < 7, x, sqrt(x - 7))
        f = self.function_remove_nan([x], pytensor.gradient.grad(y, x), self.mode)
        assert f(5) == 1, f(5)

    def test_local_div_switch_sink(self):
        c = dscalar()
        idx = 0
        for condition in [
            (dmatrix("cond"), self.condm),
            (dvector("cond"), self.condv),
            (dscalar("cond"), self.conds),
        ]:
            for x in [
                (dmatrix("x"), self.xm),
                (dvector("x"), self.xv),
                (dscalar("x"), self.xs),
            ]:
                y = true_div(
                    pt.switch(condition[0] > 0, 1.0 * x[0], 0.0 * x[0]),
                    pt.switch(condition[0] > 0, 1.0 * x[0], log(c) * x[0]),
                )
                f = self.function_remove_nan(
                    [condition[0], x[0], c], [y], mode=self.mode
                )
                if isinstance(condition[1], list):
                    for i in range(len(condition[1])):
                        res = f(condition[1][i], x[1], -1)
                        assert (
                            res == np.asarray(self.resm[idx][i])
                        ).sum() == self.resm[idx][i].size
                else:
                    res = f(condition[1], x[1], -1)
                    assert (res == np.asarray(self.resm[idx])).sum() == self.resm[
                        idx
                    ].size
                idx += 1

    @pytest.mark.parametrize(
        "op, rewrite", [(mul, local_mul_switch_sink), (true_div, local_div_switch_sink)]
    )
    def test_local_mul_div_switch_sink_cast(self, op, rewrite):
        """Check that we don't downcast during the rewrite.

        Regression test for: https://github.com/pymc-devs/pytensor/issues/1037
        """
        cond = scalar("cond", dtype="bool")
        # The zero branch upcasts the output, so we can't ignore its dtype
        zero_branch = constant(np.array(0, dtype="float64"), name="zero_branch")
        other_branch = scalar("other_branch", dtype="float32")
        outer_var = scalar("outer_var", dtype="bool")

        out = op(switch(cond, zero_branch, other_branch), outer_var)
        fgraph = FunctionGraph(outputs=[out], clone=False)
        [new_out] = rewrite.transform(fgraph, out.owner)
        assert new_out.type.dtype == out.type.dtype

        expected_out = switch(cond, zero_branch, op(other_branch, outer_var))
        assert equal_computations([new_out], [expected_out])

    @pytest.mark.parametrize(
        "op, rewrite", [(mul, local_mul_switch_sink), (true_div, local_div_switch_sink)]
    )
    def test_local_mul_div_switch_sink_branch_order(self, op, rewrite):
        cond = scalar("cond", dtype="bool")
        zero_branch = constant(np.array(0.0, dtype="float64"), "zero_branch")
        other_branch = scalar("other_branch", dtype="float64")
        outer_var = scalar("outer_var", dtype="float64")

        left = op(switch(cond, zero_branch, other_branch), outer_var)
        right = op(switch(cond, other_branch, zero_branch), outer_var)
        fgraph = FunctionGraph(outputs=[left, right], clone=False)
        [new_left] = rewrite.transform(fgraph, left.owner)
        [new_right] = rewrite.transform(fgraph, right.owner)

        expected_left = switch(cond, zero_branch, op(other_branch, outer_var))
        expected_right = switch(cond, op(other_branch, outer_var), zero_branch)
        assert equal_computations(
            [new_left, new_right], [expected_left, expected_right]
        )


@pytest.mark.skipif(
    config.cxx == "",
    reason="erf need a c++ compiler or scipy",
)
class TestLocalErf:
    def setup_method(self):
        self.mode = (
            get_default_mode()
            .including("canonicalize", "fast_run")
            .excluding("gpu", "fusion", "inplace")
        )

    def test_local_one_plus_erf(self):
        val = np.asarray([-30, -3, -2, -1, 0, 1, 2, 3, 30], dtype=config.floatX)
        x = vector()

        f = function([x], 1 + erf(x), mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [neg, erfc]
        f(val)

        f = function([x], erf(x) + 1, mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [neg, erfc]
        f(val)

        f = function([x], erf(x) + 2, mode=self.mode)
        topo = f.maker.fgraph.toposort()
        assert len(topo) == 2
        assert topo[0].op == erf
        assert isinstance(topo[1].op, Elemwise)
        assert isinstance(topo[1].op.scalar_op, ps.Add)
        f(val)

    def test_local_one_minus_erf(self):
        val = np.asarray([-30, -3, -2, -1, 0, 1, 2, 3, 30], dtype=config.floatX)
        x = vector()

        f = function([x], 1 - erf(x), mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [erfc]
        f(val)

        f = function([x], 1 + (-erf(x)), mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [erfc]

        f = function([x], (-erf(x)) + 1, mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [erfc]

        f = function([x], (-1.0 * erf(x)) + 1, mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [erfc]

        f = function([x], 2 - erf(x), mode=self.mode)
        topo = f.maker.fgraph.toposort()
        assert len(topo) == 2
        assert topo[0].op == erf
        assert isinstance(topo[1].op, Elemwise)
        assert isinstance(topo[1].op.scalar_op, ps.Add | ps.Sub)

    def test_local_erf_minus_one(self):
        val = np.asarray([-30, -3, -2, -1, 0, 1, 2, 3, 30], dtype=config.floatX)
        x = vector()

        f = function([x], erf(x) - 1, mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [erfc, neg]
        f(val)

        f = function([x], erf(x) + (-1), mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [erfc, neg]

        f = function([x], -1 + erf(x), mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [erfc, neg]

        f = function([x], erf(x) - 2, mode=self.mode)
        topo = f.maker.fgraph.toposort()
        assert len(topo) == 2
        assert topo[0].op == erf
        assert isinstance(topo[1].op, Elemwise)
        assert isinstance(topo[1].op.scalar_op, ps.Add | ps.Sub)


@pytest.mark.skipif(
    config.cxx == "",
    reason="erf need a c++ compiler or scipy",
)
class TestLocalErfc:
    def setup_method(self):
        self.mode_fusion = (
            get_default_mode()
            .including("canonicalize", "fast_run")
            .excluding("gpu", "inplace")
        )
        self.mode = self.mode_fusion.excluding("fusion")

    def test_local_one_minus_erfc(self):
        """Test the rewrites ``1 - erfc(x) -> erf(x)`` and ``-erfc(x) + 1 -> erf(x)``."""

        val = np.asarray([-30, -3, -2, -1, 0, 1, 2, 3, 30], dtype=config.floatX)
        x = vector("x")

        f = function([x], 1 - erfc(x), mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [erf]
        f(val)

        f = function([x], (-erfc(x)) + 1, mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [erf]

        f = function([x], (-1.0 * erfc(x)) + 1, mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [erf]

        f = function([x], 2 - erfc(x), mode=self.mode)
        topo = f.maker.fgraph.toposort()
        assert len(topo) == 2
        assert topo[0].op == erfc
        assert isinstance(topo[1].op, Elemwise)
        assert isinstance(topo[1].op.scalar_op, ps.Sub)

    def test_local_erf_neg_minus_one(self):
        """Test the rewrite ``-1 + erfc(-x) -> erf(x)``."""
        val = np.asarray([-30, -3, -2, -1, 0, 1, 2, 3, 30], dtype=config.floatX)
        x = vector("x")

        f = function([x], -1 + erfc(-x), mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [erf]
        f(val)

        f = function([x], erfc(-x) - 1, mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [erf]

        f = function([x], erfc(-x) + (-1), mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [erf]

        f = function([x], erfc(-1.0 * x) + (-1), mode=self.mode)
        assert [n.op for n in f.maker.fgraph.toposort()] == [erf]

    def test_local_log_erfc(self):
        val = [-30, -27, -26, -11, -10, -3, -2, -1, 0, 1, 2, 3, 10, 11, 26, 27, 28, 30]
        if config.mode in ["DebugMode", "DEBUG_MODE", "FAST_COMPILE"]:
            # python mode doesn't like the reciprocal(0)
            val.remove(0)
        val = np.asarray(val, dtype=config.floatX)
        x = vector("x")

        # their are some `nan`s that will appear in the graph due to the logs
        # of negatives values
        mode = copy.copy(self.mode)
        mode.check_isfinite = False
        mode_fusion = copy.copy(self.mode_fusion)
        mode_fusion.check_isfinite = False

        f = function([x], log(erfc(x)), mode=mode)
        assert len(f.maker.fgraph.apply_nodes) == 22
        assert f.maker.fgraph.outputs[0].dtype == config.floatX
        assert all(np.isfinite(f(val)))

        f = function([x], log(erfc(-x)), mode=mode)
        assert len(f.maker.fgraph.apply_nodes) == 23
        assert f.maker.fgraph.outputs[0].dtype == config.floatX
        assert all(np.isfinite(f(-val)))

        f = function([x], log(erfc(x)), mode=mode_fusion)
        assert len(f.maker.fgraph.apply_nodes) == 1
        assert f.maker.fgraph.outputs[0].dtype == config.floatX
        assert len(f.maker.fgraph.toposort()[0].op.scalar_op.fgraph.apply_nodes) == 22

        # TODO: fix this problem: The python code upcast somewhere internally
        #  some value of float32 to python float for part of its computation.
        #  That makes the c and python code generate slightly different values
        if not (
            config.floatX == "float32" and config.mode in ["DebugMode", "DEBUG_MODE"]
        ):
            assert all(np.isfinite(f(val)))

    @np.errstate(divide="ignore", invalid="ignore")
    def test_local_grad_log_erfc_neg(self):
        # TODO: This evaluation is questionable; is the transform's math not
        # already established?  It doesn't look like these tests are preforming
        # a real numerical evaluation of the underlying math.  Instead, it
        # looks like they're being used as an extremely poor way of validating
        # the transform results.  It would be better to remove these numerical
        # evaluations and confirm the transform output directly and exactly.
        val = [
            -100,
            -30,
            -27,
            -26.4,
            -26.2,
            -26,
            -11,
            -10,
            -9,
            -3,
            -2,
            -1,
            0,
            1,
            2,
            3,
            9,
            10,
            11,
            27,
            26.4,
            26.2,
            26,
            28,
            30,
            100,
        ]
        val = np.asarray(val, dtype=config.floatX)
        x = vector("x")
        y = vector("y")

        # Test cases for which the requisite form isn't present
        no_matches = [
            ([x, y], exp(sqr(x)) / erfc(y)),
            ([x, y], exp(neg(x)) / erfc(y)),
            ([x, y], exp(x * 1) / erfc(y)),
            ([x, y], exp(neg(sqr(x))) / erfc(y)),
            ([x], mul(1.0, 2.0, x) / erfc(x)),
        ]
        for inputs, no_match in no_matches:
            fg = FunctionGraph(inputs, [no_match], clone=False)

            WalkingGraphRewriter(
                SequentialNodeRewriter(local_grad_log_erfc_neg), order="out_to_in"
            ).rewrite(fg)

            # Make sure that the graph hasn't been changed
            assert fg.outputs[0] is no_match

        # Some `nan`s will appear in the graph for the log of negatives values
        mode = Mode("py", self.mode.optimizer)
        mode.check_isfinite = False

        # Make sure that we catch our target graph in a way that it's naturally
        # produced
        log_erfc_grad = pytensor.gradient.grad(log(erfc(x)).sum(), x)
        f = function([x], log_erfc_grad, mode=mode)

        # The resulting graph should be `mul(switch(...), y)`
        assert f.maker.fgraph.outputs[0].owner.op == mul
        assert f.maker.fgraph.outputs[0].owner.inputs[0].owner.op == switch
        assert all(np.isfinite(f(val)))
        assert f.maker.fgraph.outputs[0].dtype == config.floatX

        # Test with a different `mul` and `constant`
        f = function([x], mul(exp(neg(sqr(x))), -10.12837917) / erfc(x), mode=mode)

        assert f.maker.fgraph.outputs[0].owner.op == mul
        assert f.maker.fgraph.outputs[0].owner.inputs[0].owner.op == switch
        assert f.maker.fgraph.outputs[0].dtype == config.floatX
        assert all(np.isfinite(f(val)))

        # Test it works without the `mul`
        f = function([x], exp(neg(sqr(x))) / erfc(x), mode=mode)

        assert f.maker.fgraph.outputs[0].owner.op == switch
        assert f.maker.fgraph.outputs[0].dtype == config.floatX
        assert all(np.isfinite(f(val)))

        # Test that it works without the `sqr` and `neg`
        f = function([x], exp(mul(-1, x, x)) / erfc(x), mode=mode)

        assert f.maker.fgraph.outputs[0].owner.op == switch
        assert f.maker.fgraph.outputs[0].dtype == config.floatX
        assert all(np.isfinite(f(val)))

        # Test that it works correctly when `x` is multiplied by a constant
        f = function([x], pytensor.gradient.grad(log(erfc(2 * x)).sum(), x), mode=mode)

        assert f.maker.fgraph.outputs[0].owner.op == mul
        assert f.maker.fgraph.outputs[0].owner.inputs[0].owner.op == switch
        assert np.isfinite(f(val)).all()
        assert f.maker.fgraph.outputs[0].dtype == config.floatX

        # I suppose this tests whether or not the transform is applied before
        # fusion?
        mode_fusion = copy.copy(self.mode_fusion)
        mode_fusion.check_isfinite = False

        f = function(
            [x], pytensor.gradient.grad(log(erfc(x)).sum(), x), mode=mode_fusion
        )

        assert len(f.maker.fgraph.apply_nodes) == 1, len(f.maker.fgraph.apply_nodes)
        assert f.maker.fgraph.outputs[0].dtype == config.floatX

    def speed_local_log_erfc(self):
        # TODO: Make this a benchmark test!
        val = np.random.random(1e6)
        x = vector()
        mode = get_mode("FAST_RUN")
        f1 = function([x], log(erfc(x)), mode=mode.excluding("local_log_erfc"))
        f2 = function([x], log(erfc(x)), mode=mode)
        # print(f1.maker.fgraph.toposort())
        # print(f2.maker.fgraph.toposort())
        # t0 = time.perf_counter()
        f1(val)
        # t1 = time.perf_counter()
        f2(val)
        # t2 = time.perf_counter()
        # print(t1 - t0, t2 - t1)


class TestLocalMergeSwitchSameCond:
    def test_elemwise(self):
        # float Ops
        mats = matrices("cabxy")
        c, a, b, x, y = mats
        s1 = pt.switch(c, a, b)
        s2 = pt.switch(c, x, y)
        for op in (
            add,
            sub,
            mul,
            true_div,
            int_div,
            floor_div,
            minimum,
            maximum,
            gt,
            lt,
            ge,
            le,
            eq,
            neq,
            pt_pow,
        ):
            g = rewrite(FunctionGraph(mats, [op(s1, s2)]))
            assert debugprint(g, file="str").count("Switch") == 1
        # integer Ops
        mats = imatrices("cabxy")
        c, a, b, x, y = mats
        s1 = pt.switch(c, a, b)
        s2 = pt.switch(c, x, y)
        for op in (
            bitwise_and,
            bitwise_or,
            bitwise_xor,
        ):
            g = rewrite(FunctionGraph(mats, [op(s1, s2)]))
            assert debugprint(g, file="str").count("Switch") == 1
        # add/mul with more than two inputs
        u, v = matrices("uv")
        s3 = pt.switch(c, u, v)
        for op in (add, mul):
            g = rewrite(FunctionGraph([*mats, u, v], [op(s1, s2, s3)]))
            assert debugprint(g, file="str").count("Switch") == 1


class TestReduceChain:
    def setup_method(self):
        self.mode = get_default_mode().including("canonicalize", "specialize")

    def test_local_sum_prod_all_to_none(self):
        a = tensor3()
        input = np.arange(3 * 4 * 5, dtype=config.floatX).reshape(3, 4, 5)
        # test sum
        f = function([a], a.sum(), mode=self.mode)
        assert len(f.maker.fgraph.apply_nodes) == 1
        utt.assert_allclose(f(input), input.sum())
        # test prod
        f = function([a], a.prod(), mode=self.mode)
        assert len(f.maker.fgraph.apply_nodes) == 1
        utt.assert_allclose(f(input), input.prod())
        # test sum
        f = function([a], a.sum([0, 1, 2]), mode=self.mode)
        assert len(f.maker.fgraph.apply_nodes) == 1
        utt.assert_allclose(f(input), input.sum())
        # test prod
        f = function([a], a.prod([0, 1, 2]), mode=self.mode)
        assert len(f.maker.fgraph.apply_nodes) == 1
        utt.assert_allclose(f(input), input.prod())

        f = function([a], a.sum(0).sum(0).sum(0), mode=self.mode)
        assert len(f.maker.fgraph.apply_nodes) == 1
        utt.assert_allclose(f(input), input.sum())

    def test_local_sum_sum_prod_prod(self):
        a = tensor3()
        input = np.arange(3 * 4 * 5, dtype=config.floatX).reshape(3, 4, 5)
        dims = [
            (0, 0),
            (1, 0),
            (2, 0),
            (0, 1),
            (1, 1),
            (2, 1),
            ((0, 1), 0),
            ((1, 2), 0),
            (0, (0, 1)),
            (1, (0, 1)),
            (2, (0, 1)),
        ]

        def my_prod(data, d, dd):
            # This prod when d or dd is a tuple of 2 dimensions.
            if not isinstance(d, tuple) and not isinstance(dd, tuple):
                return data.prod(d).prod(dd)
            if isinstance(d, tuple):
                d = sorted(d)
                return data.prod(d[1]).prod(d[0]).prod(dd)
            else:
                dd = sorted(dd)
                return data.prod(d).prod(dd[1]).prod(dd[0])

        def my_sum(data, d, dd):
            # This sum when d or dd is a tuple of 2 dimensions.
            if not isinstance(d, tuple) and not isinstance(dd, tuple):
                return data.sum(d).sum(dd)
            if isinstance(d, tuple):
                d = sorted(d)
                return data.sum(d[1]).sum(d[0]).sum(dd)
            else:
                dd = sorted(dd)
                return data.sum(d).sum(dd[1]).sum(dd[0])

        def my_sum_prod(data, d, dd):
            # This sum when d or dd is a tuple of 2 dimensions.
            if not isinstance(d, tuple) and not isinstance(dd, tuple):
                return data.sum(d).prod(dd)
            if isinstance(d, tuple):
                d = sorted(d)
                return data.sum(d[1]).sum(d[0]).prod(dd)
            else:
                dd = sorted(dd)
                return data.sum(d).prod(dd[1]).prod(dd[0])

        for d, dd in dims:
            expected = my_sum(input, d, dd)
            f = function([a], a.sum(d).sum(dd), mode=self.mode)
            utt.assert_allclose(f(input), expected)
            assert len(f.maker.fgraph.apply_nodes) == 1
        for d, dd in dims[:6]:
            f = function([a], a.sum(d).sum(dd).sum(0), mode=self.mode)
            utt.assert_allclose(f(input), input.sum(d).sum(dd).sum(0))
            assert len(f.maker.fgraph.apply_nodes) == 1
        for d in [0, 1, 2]:
            f = function([a], a.sum(d).sum(None), mode=self.mode)
            utt.assert_allclose(f(input), input.sum(d).sum())
            assert len(f.maker.fgraph.apply_nodes) == 1
        f = function([a], a.sum(None).sum(), mode=self.mode)
        utt.assert_allclose(f(input), input.sum())
        assert len(f.maker.fgraph.apply_nodes) == 1

        # test prod
        for d, dd in dims:
            expected = my_prod(input, d, dd)
            f = function([a], a.prod(d).prod(dd), mode=self.mode)
            utt.assert_allclose(f(input), expected)
            assert len(f.maker.fgraph.apply_nodes) == 1
        for d, dd in dims[:6]:
            f = function([a], a.prod(d).prod(dd).prod(0), mode=self.mode)
            utt.assert_allclose(f(input), input.prod(d).prod(dd).prod(0))
            assert len(f.maker.fgraph.apply_nodes) == 1
        for d in [0, 1, 2]:
            f = function([a], a.prod(d).prod(None), mode=self.mode)
            utt.assert_allclose(f(input), input.prod(d).prod())
            assert len(f.maker.fgraph.apply_nodes) == 1
        f = function([a], a.prod(None).prod(), mode=self.mode)
        utt.assert_allclose(f(input), input.prod())
        assert len(f.maker.fgraph.apply_nodes) == 1

        # Test that sum prod didn't get rewritten.
        for d, dd in dims:
            expected = my_sum_prod(input, d, dd)
            f = function([a], a.sum(d).prod(dd), mode=self.mode)
            utt.assert_allclose(f(input), expected)
            assert len(f.maker.fgraph.apply_nodes) == 2
        for d, dd in dims[:6]:
            f = function([a], a.sum(d).prod(dd).prod(0), mode=self.mode)
            utt.assert_allclose(f(input), input.sum(d).prod(dd).prod(0))
            assert len(f.maker.fgraph.apply_nodes) == 2
        for d in [0, 1, 2]:
            f = function([a], a.sum(d).prod(None), mode=self.mode)
            utt.assert_allclose(f(input), input.sum(d).prod())
            assert len(f.maker.fgraph.apply_nodes) == 2
        f = function([a], a.sum(None).prod(), mode=self.mode)
        utt.assert_allclose(f(input), input.sum())
        assert len(f.maker.fgraph.apply_nodes) == 1

    def test_local_sum_sum_int8(self):
        """Test that `local_sum_sum` works when combining two sums on an int8 array.

        This is a regression test for ticket gh-356.
        """

        x = tensor3(dtype="int8")
        y = x.sum(axis=0).sum(axis=1)

        with config.change_flags(on_opt_error="raise"):
            # This compilation would fail prior to fix.
            function([x], y)

    def test_local_sum_sum_dtype(self):
        """Test that `local_sum_sum` works when specifying dtypes manually."""

        x = tensor3(dtype="int8")
        y = x.sum(axis=0, dtype="int32").sum(axis=1, dtype="int64")

        with config.change_flags(on_opt_error="raise"):
            # This compilation would fail prior to fix.
            function([x], y)

    def test_all(self):
        x = tensor3(dtype=bool)
        out = x.all(axis=-1).all(axis=0)
        fg = FunctionGraph([x], [out], clone=False)
        [new_out] = local_reduce_chain.transform(fg, out.owner)
        assert equal_computations([new_out], [x.all(axis=(0, 2))])


class TestLocalSumProd:
    """Test sum/prod rewrites."""

    def setup_method(self):
        self.mode = get_default_mode().including("canonicalize", "specialize")

    def test_local_sum_prod_of_scalar_mul(self):
        # Test the rewrite `local_sum_prod_mul_by_scalar` for both Sum and
        # Prod ops in six cases each :
        # 1-the inputs to the mul contain a scalar and no non-scalar
        # 2-the inputs to the mul contain a scalar and one non-scalar
        # 3-the inputs to the mul contain a scalar and two non-scalars
        # 4-the inputs to the mul contain two scalars and no non-scalar
        # 5-the inputs to the mul contain two scalars and one non-scalar
        # 6-the inputs to the mul contain two scalars and two non-scalars
        # 7-the reduction happens across only the first of two axes

        vect = dvector()
        mat = dmatrix()
        scalar1 = dscalar()
        scalar2 = dscalar()

        v_val = np.random.random(2)
        m_val = np.random.random((2, 2))
        s1_val = np.random.random()
        s2_val = np.random.random()

        def test_reduction_rewrite(
            inputs,
            inputs_val,
            reduction_op,
            expected_output,
            nb_expected_sum_nodes,
            axis=None,
        ):
            mul_out = mul(*inputs)
            f = function(inputs, reduction_op(axis=axis)(mul_out), mode=self.mode)
            out = f(*inputs_val)
            utt.assert_allclose(out, expected_output)

            # Ensure that the rewrite has been applied properly by
            # ensuring that the rewritten graph contains the expected number
            # of apply nodes for the sum op
            prod_nodes = [
                n for n in f.maker.fgraph.toposort() if isinstance(n.op, reduction_op)
            ]
            assert len(prod_nodes) == nb_expected_sum_nodes

        # Test sum

        # Case 1
        test_reduction_rewrite([scalar1], [s1_val], Sum, s1_val, 0)

        # Case 2
        test_reduction_rewrite(
            [vect, scalar1], [v_val, s1_val], Sum, s1_val * v_val.sum(), 1
        )

        # Case 3
        test_reduction_rewrite(
            [vect, mat, scalar1],
            [v_val, m_val, s1_val],
            Sum,
            s1_val * (v_val * m_val).sum(),
            1,
        )

        # Case 4
        test_reduction_rewrite(
            [scalar1, scalar2], [s1_val, s2_val], Sum, s1_val * s2_val, 0
        )

        # Case 5
        test_reduction_rewrite(
            [vect, scalar1, scalar2],
            [v_val, s1_val, s2_val],
            Sum,
            s1_val * s2_val * v_val.sum(),
            1,
        )

        # Case 6
        test_reduction_rewrite(
            [vect, mat, scalar1, scalar2],
            [v_val, m_val, s1_val, s2_val],
            Sum,
            s1_val * s2_val * (v_val * m_val).sum(),
            1,
        )

        # Case 7
        test_reduction_rewrite(
            [mat, scalar1, scalar2],
            [m_val, s1_val, s2_val],
            Sum,
            (s1_val * s2_val * m_val).sum(0),
            1,
            axis=(0,),
        )

        # Test prod

        # Case 1
        test_reduction_rewrite([scalar1], [s1_val], Prod, s1_val, 0)

        # Case 2
        test_reduction_rewrite(
            [vect, scalar1],
            [v_val, s1_val],
            Prod,
            (s1_val * v_val).prod(),
            1,
        )

        # Case 3
        test_reduction_rewrite(
            [vect, mat, scalar1],
            [v_val, m_val, s1_val],
            Prod,
            (s1_val * v_val * m_val).prod(),
            2,
        )

        # Case 4
        test_reduction_rewrite(
            [scalar1, scalar2], [s1_val, s2_val], Prod, s1_val * s2_val, 0
        )

        # Case 5
        test_reduction_rewrite(
            [vect, scalar1, scalar2],
            [v_val, s1_val, s2_val],
            Prod,
            (s1_val * s2_val * v_val).prod(),
            1,
        )

        # Case 6
        test_reduction_rewrite(
            [vect, mat, scalar1, scalar2],
            [v_val, m_val, s1_val, s2_val],
            Prod,
            (s1_val * s2_val * v_val * m_val).prod(),
            2,
        )

        # Case 7
        test_reduction_rewrite(
            [mat, scalar1, scalar2],
            [m_val, s1_val, s2_val],
            Prod,
            (s1_val * s2_val * m_val).prod(0),
            1,
            axis=(0,),
        )

    def test_sum_of_non_scalar_mul(self):
        mode = Mode("vm", optimizer="None")
        rewrite = out2in(local_sum_prod_of_mul_or_div)

        row1 = matrix(shape=(1, None), dtype="float64")
        row2 = matrix(shape=(1, None), dtype="float64")
        col1 = matrix(shape=(None, 1), dtype="float64")
        col2 = matrix(shape=(None, 1), dtype="float64")
        mat1 = matrix(shape=(None, None), dtype="float64")
        mat2 = matrix(shape=(None, None), dtype="float64")

        inputs = [row1, row2, col1, col2, mat1, mat2]
        test_vals = [
            np.random.random((1, 2)),
            np.random.random((1, 2)),
            np.random.random((2, 1)),
            np.random.random((2, 1)),
            np.random.random((2, 2)),
            np.random.random((2, 2)),
        ]

        for out, expected_out in [
            (
                mul(row1, row2, mat1, mat2, col1, col2).sum(axis=None),
                mul(row1, row2, mat1, mat2, col1, col2).sum(axis=None),
            ),
            (
                mul(row1, row2, mat1, mat2, col1, col2).sum(axis=0),
                mul(row1.squeeze(), row2.squeeze())
                * mul(mat1, mat2, col1, col2).sum(axis=0),
            ),
            (
                mul(row1, mat1, mat2, col1, col2).sum(axis=0),
                row1.squeeze() * mul(mat1, mat2, col1, col2).sum(axis=0),
            ),
            (
                mul(row1, row2, mat1, mat2, col1, col2).sum(axis=1),
                mul(col1.squeeze(), col2.squeeze())
                * mul(row1, row2, mat1, mat2).sum(axis=1),
            ),
            (
                mul(row1, row2, mat1, mat2, col2).sum(axis=1),
                col2.squeeze() * mul(row1, row2, mat1, mat2).sum(axis=1),
            ),
            (
                mul(row1, row2).sum(axis=1),
                mul(row1, row2).sum(axis=1),
            ),
            (
                mul(row1, row2).sum(axis=0),
                mul(row1.squeeze(), row2.squeeze()),
            ),
            (
                mul(row1, col1).sum(axis=0),
                row1.squeeze() * col1.sum(axis=0),
            ),
        ]:
            out_fn = pytensor.function(inputs, out, mode=mode, on_unused_input="ignore")

            rewritten_out = rewrite_graph(out, custom_rewrite=rewrite)
            assert equal_computations([rewritten_out], [expected_out])

            rewritten_out_fn = pytensor.function(
                inputs, rewritten_out, mode=mode, on_unused_input="ignore"
            )
            np.testing.assert_allclose(
                out_fn(*test_vals),
                rewritten_out_fn(*test_vals),
            )

    def test_prod_of_non_scalar_mul(self):
        mode = Mode("vm", optimizer="None")
        rewrite = out2in(local_sum_prod_of_mul_or_div)

        scl1 = matrix(shape=(1, 1), dtype="float64")
        row1 = matrix(shape=(1, None), dtype="float64")
        row2 = matrix(shape=(1, None), dtype="float64")
        col1 = matrix(shape=(None, 1), dtype="float64")
        col2 = matrix(shape=(None, 1), dtype="float64")
        mat1 = matrix(shape=(None, None), dtype="float64")
        mat2 = matrix(shape=(None, None), dtype="float64")

        inputs = [scl1, row1, row2, col1, col2, mat1, mat2]
        test_vals = [
            np.random.random((1, 1)),
            np.random.random((1, 2)),
            np.random.random((1, 2)),
            np.random.random((2, 1)),
            np.random.random((2, 1)),
            np.random.random((2, 2)),
            np.random.random((2, 2)),
        ]

        for out, expected_out in [
            (
                mul(row1, row2, mat1, mat2, col1, col2).prod(axis=None),
                mul(row1, row2, mat1, mat2, col1, col2).prod(axis=None),
            ),
            (
                mul(row1, row2, mat1, mat2, col1, col2).prod(axis=0),
                (
                    mul(row1.squeeze(), row2.squeeze())
                    ** prod([mul(mat1, mat2, col1, col2).shape[0].astype("float64")])
                    * mul(mat1, mat2, col1, col2).prod(axis=0)
                ),
            ),
            (
                mul(row1, mat1, mat2, col1, col2).prod(axis=0),
                (
                    row1.squeeze()
                    ** prod([mul(mat1, mat2, col1, col2).shape[0].astype("float64")])
                    * mul(mat1, mat2, col1, col2).prod(axis=0)
                ),
            ),
            (
                mul(row1, row2, mat1, mat2, col1, col2).prod(axis=1),
                (
                    mul(col1.squeeze(), col2.squeeze())
                    ** prod([mul(row1, row2, mat1, mat2).shape[1].astype("float64")])
                    * mul(row1, row2, mat1, mat2).prod(axis=1)
                ),
            ),
            (
                mul(row1, row2).prod(axis=0),
                mul(row1.squeeze(), row2.squeeze()),
            ),
            (
                mul(row1, col1).prod(axis=0),
                (
                    row1.squeeze() ** prod([col1.shape[0].astype("float64")])
                    * col1.prod(axis=0)
                ),
            ),
            (
                mul(scl1, mat1, row1).prod(axis=None),
                (
                    scl1.squeeze()
                    ** prod(
                        [
                            mul(mat1, row1).shape[0].astype("float64"),
                            mul(mat1, row1).shape[1].astype("float64"),
                        ]
                    )
                    * mul(mat1, row1).prod(axis=None)
                ),
            ),
        ]:
            out_fn = pytensor.function(inputs, out, mode=mode, on_unused_input="ignore")

            rewritten_out = rewrite_graph(out, custom_rewrite=rewrite)
            assert equal_computations([rewritten_out], [expected_out])

            rewritten_out_fn = pytensor.function(
                inputs, rewritten_out, mode=mode, on_unused_input="ignore"
            )
            np.testing.assert_allclose(
                out_fn(*test_vals),
                rewritten_out_fn(*test_vals),
            )

    def test_local_sum_prod_alloc(self):
        a = dtensor3()
        input = np.asarray(np.arange(2 * 3 * 4).reshape(2, 3, 4), dtype="float64")
        mode = self.mode.including("specialize").excluding("fusion")

        for t_like, n_like, nb_nodes in [
            (pt.zeros_like, np.zeros_like, (1, 3, 3, 2)),
            (pt.ones_like, np.ones_like, (5, 5, 5, 6)),
        ]:
            # test sum
            f = function([a], t_like(a).sum(None), mode=mode)
            utt.assert_allclose(f(input), n_like(input).sum())
            assert len(f.maker.fgraph.apply_nodes) == nb_nodes[0]

            f = function([a], t_like(a).sum([0, 1, 2]), mode=mode)
            utt.assert_allclose(f(input), n_like(input).sum())
            assert len(f.maker.fgraph.apply_nodes) == nb_nodes[0]

            for d in range(3):
                f = function([a], t_like(a).sum(d), mode=mode)
                utt.assert_allclose(f(input), n_like(input).sum(d))
                assert len(f.maker.fgraph.apply_nodes) == nb_nodes[1]
                topo = f.maker.fgraph.toposort()
                assert topo[-1].op == pt.alloc
                assert not any(isinstance(node.op, Sum) for node in topo)
            for i in range(3):
                f = function([a], t_like(a).sum(i), mode=mode)
                utt.assert_allclose(f(input), n_like(input).sum(i))
                assert len(f.maker.fgraph.apply_nodes) == nb_nodes[2]
                topo = f.maker.fgraph.toposort()
                assert topo[-1].op == pt.alloc
                assert not any(isinstance(node.op, Sum) for node in topo)

            # test prod
            f = function([a], t_like(a).prod(None), mode=mode)
            utt.assert_allclose(f(input), n_like(input).prod())
            # assert len(f.maker.fgraph.apply_nodes) == nb_nodes[0]

            f = function([a], t_like(a).prod([0, 1, 2]), mode=mode)
            utt.assert_allclose(f(input), n_like(input).prod())
            # assert len(f.maker.fgraph.apply_nodes) == nb_nodes[0]

            for d in range(3):
                f = function([a], t_like(a).prod(d), mode=mode)
                utt.assert_allclose(f(input), n_like(input).prod(d))
                # assert len(f.maker.fgraph.apply_nodes) == nb_nodes[1]
                topo = f.maker.fgraph.toposort()
                assert topo[-1].op == pt.alloc
                assert not any(isinstance(node.op, Prod) for node in topo)
            for i in range(3):
                f = function([a], t_like(a).prod(i), mode=mode)
                utt.assert_allclose(f(input), n_like(input).prod(i))
                # assert len(f.maker.fgraph.apply_nodes) == nb_nodes[2]
                topo = f.maker.fgraph.toposort()
                assert topo[-1].op == pt.alloc
                assert not any(isinstance(node.op, Prod) for node in topo)

            for d, dd in [(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (2, 1)]:
                f = function([a], t_like(a).sum(d).sum(dd), mode=mode)
                utt.assert_allclose(f(input), n_like(input).sum(d).sum(dd))
                assert len(f.maker.fgraph.apply_nodes) == nb_nodes[3]
                topo = f.maker.fgraph.toposort()
                assert topo[-1].op == pt.alloc
                assert not any(isinstance(node.op, Sum) for node in topo)

    def test_local_sum_prod_mul_by_scalar_stack_trace(self):
        """Test that stack trace is copied over correctly for `local_sum_prod_mul_by_scalar`."""
        m0 = (
            get_default_mode()
            .excluding("inplace_elemwise_opt")
            .including("canonicalize", "specialize")
        )

        vect = dvector()
        mat = dmatrix()
        ds = dscalar()

        f = function([vect, ds], pt_sum(vect * ds), mode=m0)
        assert check_stack_trace(f, ops_to_check="all")

        f = function([vect], pt_sum(-vect), mode=m0)
        assert check_stack_trace(f, ops_to_check=[Sum])

        f = function([vect, ds], Prod()(vect * ds), mode=m0)
        assert check_stack_trace(f, ops_to_check=[Prod])

        f = function([vect], Prod()(-vect), mode=m0)
        assert check_stack_trace(f, ops_to_check=[Prod])

        f = function([mat, ds], pt_sum(mat * ds), mode=m0)
        assert check_stack_trace(f, ops_to_check="all")

        f = function([mat], pt_sum(-mat), mode=m0)
        assert check_stack_trace(f, ops_to_check=[Sum])

    def test_local_sum_of_div(self):
        a = matrix("a")
        b = vector("b")
        c = tensor3("c")
        d = scalar("d")
        sum = pt_sum
        sums = [
            sum(a / d),
            sum(a / d.dimshuffle("x", "x")),
            sum(a / d.dimshuffle("x", "x"), axis=0),
            sum(a / d.dimshuffle("x", "x"), axis=1),
            sum(b / d),
            sum(b / d.dimshuffle("x")),
            sum(c / d),
            sum(c / d.dimshuffle("x", "x", "x")),
            sum(c / d.dimshuffle("x", "x", "x"), axis=0),
            sum(c / d.dimshuffle("x", "x", "x"), axis=1),
            sum(c / d.dimshuffle("x", "x", "x"), axis=2),
            sum(a / b, axis=0),
            sum(a / b.dimshuffle(0, "x"), axis=1),
            sum(a.dimshuffle(0, 1) / b.dimshuffle(0, "x"), axis=1),
            sum(a.dimshuffle(1, 0) / b.dimshuffle(0, "x"), axis=1),
            sum(c / a, axis=0),
            sum(c / a.dimshuffle(1, 0), axis=0),
            sum(c / a.dimshuffle(0, "x", 1), axis=1),
            sum(c / a.dimshuffle(1, "x", 0), axis=1),
            sum(c / a.dimshuffle(0, 1, "x"), axis=2),
            sum(c / a.dimshuffle(1, 0, "x"), axis=2),
            sum(c / b, axis=0),
            sum(c / b, axis=1),
            sum(c / b, axis=(0, 1)),
            sum(c / b.dimshuffle(0, "x"), axis=0),
            sum(c / b.dimshuffle(0, "x"), axis=2),
            sum(c / b.dimshuffle(0, "x"), axis=(0, 2)),
            sum(c / b.dimshuffle(0, "x", "x"), axis=1),
            sum(c / b.dimshuffle(0, "x", "x"), axis=2),
            sum(c / b.dimshuffle(0, "x", "x"), axis=(1, 2)),
            sum(sum(c, axis=0) / b, axis=0),
            sum(sum(c, axis=1) / b, axis=0),
        ]

        rng = np.random.default_rng(utt.fetch_seed())
        a_val = rng.standard_normal((2, 2)).astype(config.floatX)
        b_val = rng.standard_normal(2).astype(config.floatX)
        c_val = rng.standard_normal((2, 2, 2)).astype(config.floatX)
        d_val = np.asarray(rng.standard_normal(), config.floatX)

        for i, s in enumerate(sums):
            f = function([a, b, c, d], s, mode=self.mode, on_unused_input="ignore")
            g = f.maker.fgraph.toposort()
            assert isinstance(g[-1].op.scalar_op, ps.basic.TrueDiv)
            f(a_val, b_val, c_val, d_val)

    def test_local_prod_of_div(self):
        a = matrix("a")
        b = vector("b")
        c = tensor3("c")
        e = matrix("e")
        d = scalar("d")
        prods = [
            prod(a / d),
            prod(a / d.dimshuffle("x", "x")),
            prod(a / d.dimshuffle("x", "x"), axis=0),
            prod(a / d.dimshuffle("x", "x"), axis=1),
            prod(b / d),
            prod(b / d.dimshuffle("x")),
            prod(c / d),
            prod(c / d.dimshuffle("x", "x", "x")),
            prod(c / d.dimshuffle("x", "x", "x"), axis=0),
            prod(c / d.dimshuffle("x", "x", "x"), axis=1),
            prod(c / d.dimshuffle("x", "x", "x"), axis=2),
            prod(a / b, axis=0),
            prod(a / b.dimshuffle(0, "x"), axis=1),
            prod(a.dimshuffle(0, 1) / b.dimshuffle(0, "x"), axis=1),
            prod(a.dimshuffle(1, 0) / b.dimshuffle(0, "x"), axis=1),
            prod(c / a, axis=0),
            prod(c / a.dimshuffle(1, 0), axis=0),
            prod(c / a.dimshuffle(0, "x", 1), axis=1),
            prod(c / a.dimshuffle(1, "x", 0), axis=1),
            prod(c / a.dimshuffle(0, 1, "x"), axis=2),
            prod(c / a.dimshuffle(1, 0, "x"), axis=2),
            prod(c / b, axis=0),
            prod(c / b, axis=1),
            prod(c / b, axis=(0, 1)),
            prod(c / b.dimshuffle(0, "x"), axis=0),
            prod(c / b.dimshuffle(0, "x"), axis=2),
            prod(c / b.dimshuffle(0, "x"), axis=(0, 2)),
            prod(c / b.dimshuffle(0, "x", "x"), axis=1),
            prod(c / b.dimshuffle(0, "x", "x"), axis=2),
            prod(c / b.dimshuffle(0, "x", "x"), axis=(1, 2)),
            prod(c / b.dimshuffle(0, "x", "x"), axis=(0, 1)),
            prod(c / b.dimshuffle(0, "x", "x"), axis=(1, 0)),
            prod(prod(c, axis=0) / b, axis=0),
            prod(prod(c, axis=1) / b, axis=0),
        ]

        rng = np.random.default_rng(utt.fetch_seed())
        a_val = rng.standard_normal((2, 2)).astype(config.floatX)
        b_val = rng.standard_normal(2).astype(config.floatX)
        c_val = rng.standard_normal((2, 2, 2)).astype(config.floatX)
        d_val = np.asarray(rng.standard_normal(), config.floatX)

        default_mode = get_default_mode()
        # `FusionOptimizer` is included to make sure that `expected_outer_operator`
        # remains the same for all rewrite modes.
        mode_with_rewrite = default_mode.including(
            "local_sum_prod_of_mul_or_div", "FusionOptimizer"
        )
        mode_without_rewrite = default_mode.excluding("local_sum_prod_of_mul_or_div")

        # Numerical tests: tests whether the numerical values with and without
        # rewrites are equal or not.
        for i, s in enumerate(prods):
            f = function(
                [a, b, c, d], s, on_unused_input="ignore", mode=mode_without_rewrite
            )
            g = function(
                [a, b, c, d], s, on_unused_input="ignore", mode=mode_with_rewrite
            )

            utt.assert_allclose(
                f(a_val, b_val, c_val, d_val), g(a_val, b_val, c_val, d_val)
            )

        # Logical tests: tests whether the rewrite has been appplied or not
        # by checking graph structure.
        prods = [
            prod(a / e),
            prod(a / d),
            prod(a / d.dimshuffle("x", "x")),
            prod(c / d.dimshuffle("x", "x", "x"), axis=1),
            prod(a.dimshuffle(1, 0) / b.dimshuffle(0, "x"), axis=1),
            prod(c / b.dimshuffle(0, "x", "x"), axis=(1, 0)),
            prod(prod(c, axis=1) / b, axis=0),
            prod(prod(c, axis=(1, 2)) / b, axis=0),
        ]

        expected_outer_operator = [
            ps.basic.Mul,
            ps.basic.Composite,
            ps.basic.Composite,
            ps.basic.TrueDiv,
            ps.basic.Composite,
            ps.basic.Mul,
            ps.basic.Composite,
            ps.basic.Mul,
        ]

        for i, s in enumerate(prods):
            g = function(
                [a, b, c, d, e], s, on_unused_input="ignore", mode=mode_with_rewrite
            )
            assert isinstance(
                g.maker.fgraph.toposort()[-1].op.scalar_op, expected_outer_operator[i]
            )


class TestLocalReduce:
    def setup_method(self):
        self.mode = get_default_mode().including(
            "canonicalize", "specialize", "uncanonicalize"
        )

    def test_local_reduce_broadcast_all_0(self):
        for fct in [
            pt_sum,
            pt_all,
            pt_any,
            prod,
            pt_max,
            pt_min,
        ]:
            x = TensorType("int64", shape=(1, 1, 1))()
            f = function([x], [fct(x)], mode=self.mode)
            assert not any(
                isinstance(node.op, CAReduce) for node in f.maker.fgraph.toposort()
            )

    def test_local_reduce_broadcast_all_1(self):
        for fct in [
            pt_sum,
            pt_all,
            pt_any,
            prod,
            pt_max,
            pt_min,
        ]:
            x = TensorType("int64", shape=(1, 1))()
            f = function([x], [fct(x, axis=[0, 1])], mode=self.mode)
            assert not any(
                isinstance(node.op, CAReduce) for node in f.maker.fgraph.toposort()
            )

    def test_local_reduce_broadcast_some_0(self):
        for fct in [
            pt_sum,
            pt_all,
            pt_any,
            prod,
            pt_max,
            pt_min,
        ]:
            x = TensorType("int64", shape=(1, None, 1))()
            f = function([x], [fct(x, axis=[0, 1])], mode=self.mode)

            order = f.maker.fgraph.toposort()
            assert 1 == sum(isinstance(node.op, CAReduce) for node in order)

            node = next(node for node in order if isinstance(node.op, CAReduce))

            op = node.op
            assert isinstance(op, CAReduce)
            # The leading broadcastable dimension has been dropped by the
            # `local_reduce_broadcastable` rewrite.  Now, summation is over
            # the original `x`'s dimension 1.
            assert node.inputs[0].ndim == 2, node
            assert op.axis == (0,), op.axis

    def test_local_reduce_broadcast_some_1(self):
        for fct in [
            pt_sum,
            pt_all,
            pt_any,
            prod,
            pt_max,
            pt_min,
        ]:
            x = TensorType("int64", shape=(1, 1, 1))()
            f = function([x], [fct(x, axis=[0, 2])], mode=self.mode)
            assert not any(
                isinstance(node.op, CAReduce) for node in f.maker.fgraph.toposort()
            )


class TestReduceJoin:
    def setup_method(self):
        self.mode = get_default_mode().including(
            "canonicalize", "specialize", "uncanonicalize"
        )

    @pytest.mark.parametrize(
        "op, nin", [(pt_sum, 3), (pt_max, 2), (pt_min, 2), (prod, 3)]
    )
    def test_local_reduce_join(self, op, nin):
        vx = matrix()
        vy = matrix()
        vz = matrix()
        x = np.asarray([[1, 0], [3, 4]], dtype=config.floatX)
        y = np.asarray([[4, 0], [2, 1]], dtype=config.floatX)
        z = np.asarray([[5, 0], [1, 2]], dtype=config.floatX)

        inputs = (vx, vy, vz)[:nin]
        test_values = (x, y, z)[:nin]

        out = op(inputs, axis=0)
        f = function(inputs, out, mode=self.mode)
        np.testing.assert_allclose(
            f(*test_values), getattr(np, op.__name__)(test_values, axis=0)
        )
        topo = f.maker.fgraph.toposort()
        assert len(topo) <= 2
        assert isinstance(topo[-1].op, Elemwise)

    def test_type(self):
        # Test different axis for the join and the reduction
        # We must force the dtype, of otherwise, this tests will fail
        # on 32 bit systems
        A = shared(np.array([1, 2, 3, 4, 5], dtype="int64"))

        f = function([], pt_sum(pt.stack([A, A]), axis=0), mode=self.mode)
        np.testing.assert_allclose(f(), [2, 4, 6, 8, 10])
        topo = f.maker.fgraph.toposort()
        assert isinstance(topo[-1].op, Elemwise)

        # Test a case that was bugged in a old PyTensor bug
        f = function([], pt_sum(pt.stack([A, A]), axis=1), mode=self.mode)

        np.testing.assert_allclose(f(), [15, 15])
        topo = f.maker.fgraph.toposort()
        assert not isinstance(topo[-1].op, Elemwise)

        # This case could be rewritten
        A = shared(np.array([1, 2, 3, 4, 5]).reshape(5, 1))
        f = function([], pt_sum(pt.concatenate((A, A), axis=1), axis=1), mode=self.mode)
        np.testing.assert_allclose(f(), [2, 4, 6, 8, 10])
        topo = f.maker.fgraph.toposort()
        assert not isinstance(topo[-1].op, Elemwise)

        A = shared(np.array([1, 2, 3, 4, 5]).reshape(5, 1))
        f = function([], pt_sum(pt.concatenate((A, A), axis=1), axis=0), mode=self.mode)
        np.testing.assert_allclose(f(), [15, 15])
        topo = f.maker.fgraph.toposort()
        assert not isinstance(topo[-1].op, Elemwise)

    def test_not_supported_axis_none(self):
        # Test that the rewrite does not crash in one case where it
        # is not applied.  Reported at
        # https://groups.google.com/d/topic/theano-users/EDgyCU00fFA/discussion
        vx = matrix()
        vy = matrix()
        vz = matrix()
        x = np.asarray([[1, 0], [3, 4]], dtype=config.floatX)
        y = np.asarray([[4, 0], [2, 1]], dtype=config.floatX)
        z = np.asarray([[5, 0], [1, 2]], dtype=config.floatX)

        out = pt_sum([vx, vy, vz], axis=None)
        f = function([vx, vy, vz], out, mode=self.mode)
        np.testing.assert_allclose(f(x, y, z), np.sum([x, y, z]))

    def test_not_supported_unequal_shapes(self):
        # Not the same shape along the join axis
        vx = matrix(shape=(1, 3))
        vy = matrix(shape=(2, 3))
        x = np.asarray([[1, 0, 1]], dtype=config.floatX)
        y = np.asarray([[4, 0, 1], [2, 1, 1]], dtype=config.floatX)
        out = pt_sum(join(0, vx, vy), axis=0)

        f = function([vx, vy], out, mode=self.mode)
        np.testing.assert_allclose(
            f(x, y), np.sum(np.concatenate([x, y], axis=0), axis=0)
        )

    def test_non_ds_inputs(self):
        """Make sure rewrite works when inputs to join are not the usual DimShuffle.

        Sum{axis=1} [id A] <Vector(float64, shape=(3,))>
         └─ Join [id B] <Matrix(float64, shape=(3, 3))>
            ├─ 1 [id C] <Scalar(int8, shape=())>
            ├─ ExpandDims{axis=1} [id D] <Matrix(float64, shape=(3, 1))>
            ├─ Sub [id E] <Matrix(float64, shape=(3, 1))>
            └─ Sub [id F] <Matrix(float64, shape=(3, 1))>
        """
        x = vector("x")
        out = join(0, exp(x[None]), log(x[None])).sum(axis=0)

        fg = FunctionGraph([x], [out], clone=False)
        [rewritten_out] = local_reduce_join.transform(fg, out.owner)
        expected_out = add(exp(x), log(x))
        assert equal_computations([rewritten_out], [expected_out])


def test_local_useless_adds():
    default_mode = get_default_mode()

    # Test for all zeros
    a = scalar()
    s = add(pt.zeros_like(a))
    mode_with_rewrite = default_mode.including("canonicalization", "local_useless_fill")
    f = function([a], s, mode=mode_with_rewrite)
    assert not any(node.op == add for node in f.maker.fgraph.apply_nodes)

    # test of non-zero dimension
    a = vector()
    s = add(pt.zeros_like(a))
    mode_with_rewrite = default_mode.including(
        "canonicalization", "local_useless_elemwise"
    )
    f = function([a], s, mode=mode_with_rewrite)
    assert not any(node.op == add for node in f.maker.fgraph.apply_nodes)

    # test of 0-d
    a = scalar()
    s = add(pt.zeros_like(a))
    mode_with_rewrite = default_mode.including(
        "canonicalization", "local_useless_fill", "local_useless_elemwise"
    )
    f = function([a], s, mode=mode_with_rewrite)
    assert not any(node.op == add for node in f.maker.fgraph.apply_nodes)

    # Test when the 0 input is forcing upcasting
    a = pt.constant(0, dtype="int64")
    b = pt.constant(1, dtype="int32")
    s = a + b
    mode_with_rewrite = default_mode.including(
        "canonicalization", "local_add_canonizer"
    )
    f = function([], s, mode=mode_with_rewrite)
    transformed = f.maker.fgraph.outputs[0]
    assert not any(node.op == add for node in f.maker.fgraph.apply_nodes)
    assert transformed.type == s.type


def test_local_div_to_reciprocal():
    # XXX TODO: This does *not* test `local_div_to_reciprocal`!
    num_len_s = lscalar("num_len")
    denom_s = scalar("denom")

    num_v = pt.alloc(1, num_len_s)
    denom_m = denom_s.dimshuffle("x", "x")

    out = num_v / denom_m
    assert out.broadcastable == (True, False)

    f = function([num_len_s, denom_s], out)
    out_val = f(3, 2.0)
    assert out_val.shape == (1, 3)
    utt.assert_allclose(out_val, 0.5)


class TestIntDivByOne:
    def setup_method(self):
        self.mode = get_default_mode()
        self.mode = self.mode.including("local_intdiv_by_one")

    def test_remove_floor(self):
        """Tests removing the extra floor_div by 1 introduced by `local_subtensor_merge` rewrite."""

        y = tensor4("y")
        self.mode = self.mode.excluding("fusion")
        f = function([y], y[::-1][::-1], mode=self.mode)

        graph = f.maker.fgraph.toposort()
        divs = [
            node
            for node in graph
            if isinstance(node.op, Elemwise)
            and isinstance(node.op.scalar_op, ps.IntDiv)
        ]
        assert len(divs) == 0

    def test2(self):
        # Simple test case for removing dividing by 1
        y = tensor4("y")
        z = y // 1
        f = function([y], z, mode=self.mode)
        graph = f.maker.fgraph.toposort()
        divs = [
            node
            for node in graph
            if isinstance(node.op, Elemwise)
            and isinstance(node.op.scalar_op, ps.IntDiv)
        ]
        assert len(divs) == 0

    def test3(self):
        # Simple test case for removing dividing by a tensor of ones
        y = tensor4("y")
        z = y // np.ones((2, 2, 2, 2))
        f = function([y], z, mode=self.mode)
        graph = f.maker.fgraph.toposort()
        divs = [
            node
            for node in graph
            if isinstance(node.op, Elemwise)
            and isinstance(node.op.scalar_op, ps.IntDiv)
        ]
        assert len(divs) == 0


@pytest.mark.parametrize("t", [scalar, ivector, ftensor4])
@pytest.mark.parametrize("op", [int_div, true_div])
def test_local_zero_div(t, op):
    """Test the canonicalization ``0/x -> 0``."""
    x = t("x")
    y = op(0, x)
    g = rewrite(FunctionGraph([x], [y]))
    # The division should be gone
    divs = [
        node
        for node in g.toposort()
        if isinstance(node.op, Elemwise)
        and isinstance(node.op.scalar_op, type(op.scalar_op))
    ]
    assert len(divs) == 0
    # The output type should match the un-rewritten one
    output = g.outputs[0]
    assert output.ndim == y.ndim
    assert output.type == y.type
    # The output should be zero
    if output.owner and isinstance(output.owner.op, Alloc):
        out_var = output.owner.inputs[0]
    else:
        out_var = output

    assert out_var.data == 0


def test_local_sumsqr2dot():
    G = matrix("G")
    W = matrix("W")

    y = sqr(W.dimshuffle("x", 0, 1) * G.dimshuffle(0, "x", 1)).sum(axis=(1, 2))
    MODE = get_default_mode().including("local_sumsqr2dot")

    f = function([W, G], y, mode=MODE)

    w_val = np.random.random((4, 3)).astype(config.floatX)
    g_val = np.random.random((5, 3)).astype(config.floatX)

    f_val = f(w_val, g_val)
    f_test = np.dot(np.square(g_val), np.square(w_val).sum(axis=0))

    utt.assert_allclose(f_val, f_test)
    assert any(
        isinstance(
            n.op,
            Dot | Dot22 | Gemv | CGemv,
        )
        for n in f.maker.fgraph.toposort()
    )


def test_local_mul_exp_to_exp_add():
    # Default and FAST_RUN modes put a Composite op into the final graph,
    # whereas FAST_COMPILE doesn't.  To unify the graph the test cases analyze across runs,
    # we'll avoid the insertion of Composite ops in each mode by skipping Fusion rewrites
    mode = get_default_mode().excluding("fusion").including("local_mul_exp_to_exp_add")

    x = scalar("x")
    y = scalar("y")
    z = scalar("z")
    w = scalar("w")
    expx = exp(x)
    expy = exp(y)
    expz = exp(z)
    expw = exp(w)

    # e^x * e^y * e^z * e^w = e^(x+y+z+w)
    op = expx * expy * expz * expw
    f = function([x, y, z, w], op, mode)
    utt.assert_allclose(f(3, 4, 5, 6), np.exp(3 + 4 + 5 + 6))
    graph = f.maker.fgraph.toposort()
    assert all(isinstance(n.op, Elemwise) for n in graph)
    assert any(isinstance(n.op.scalar_op, ps.Add) for n in graph)
    assert not any(isinstance(n.op.scalar_op, ps.Mul) for n in graph)

    # e^x * e^y * e^z / e^w = e^(x+y+z-w)
    op = expx * expy * expz / expw
    f = function([x, y, z, w], op, mode)
    utt.assert_allclose(f(3, 4, 5, 6), np.exp(3 + 4 + 5 - 6))
    graph = f.maker.fgraph.toposort()
    assert all(isinstance(n.op, Elemwise) for n in graph)
    assert any(isinstance(n.op.scalar_op, ps.Add) for n in graph)
    assert any(isinstance(n.op.scalar_op, ps.Sub) for n in graph)
    assert not any(isinstance(n.op.scalar_op, ps.Mul) for n in graph)
    assert not any(isinstance(n.op.scalar_op, ps.TrueDiv) for n in graph)

    # e^x * e^y / e^z * e^w = e^(x+y-z+w)
    op = expx * expy / expz * expw
    f = function([x, y, z, w], op, mode)
    utt.assert_allclose(f(3, 4, 5, 6), np.exp(3 + 4 - 5 + 6))
    graph = f.maker.fgraph.toposort()
    assert all(isinstance(n.op, Elemwise) for n in graph)
    assert any(isinstance(n.op.scalar_op, ps.Add) for n in graph)
    assert any(isinstance(n.op.scalar_op, ps.Sub) for n in graph)
    assert not any(isinstance(n.op.scalar_op, ps.Mul) for n in graph)
    assert not any(isinstance(n.op.scalar_op, ps.TrueDiv) for n in graph)

    # e^x / e^y / e^z = (e^x / e^y) / e^z = e^(x-y-z)
    op = expx / expy / expz
    f = function([x, y, z], op, mode)
    utt.assert_allclose(f(3, 4, 5), np.exp(3 - 4 - 5))
    graph = f.maker.fgraph.toposort()
    assert all(isinstance(n.op, Elemwise) for n in graph)
    assert any(isinstance(n.op.scalar_op, ps.Sub) for n in graph)
    assert not any(isinstance(n.op.scalar_op, ps.TrueDiv) for n in graph)

    # e^x * y * e^z * w = e^(x+z) * y * w
    op = expx * y * expz * w
    f = function([x, y, z, w], op, mode)
    utt.assert_allclose(f(3, 4, 5, 6), np.exp(3 + 5) * 4 * 6)
    graph = f.maker.fgraph.toposort()
    assert all(isinstance(n.op, Elemwise) for n in graph)
    assert any(isinstance(n.op.scalar_op, ps.Add) for n in graph)
    assert any(isinstance(n.op.scalar_op, ps.Mul) for n in graph)

    # expect same for matrices as well
    mx = matrix("mx")
    my = matrix("my")
    f = function([mx, my], exp(mx) * exp(my), mode, allow_input_downcast=True)
    M1 = np.array([[1.0, 2.0], [3.0, 4.0]])
    M2 = np.array([[5.0, 6.0], [7.0, 8.0]])
    utt.assert_allclose(f(M1, M2), np.exp(M1 + M2))
    graph = f.maker.fgraph.toposort()
    assert all(isinstance(n.op, Elemwise) for n in graph)
    assert any(isinstance(n.op.scalar_op, ps.Add) for n in graph)
    assert not any(isinstance(n.op.scalar_op, ps.Mul) for n in graph)

    # checking whether further rewrites can proceed after this one as one would expect
    # e^x * e^(-x) = e^(x-x) = e^0 = 1
    f = function([x], expx * exp(neg(x)), mode)
    utt.assert_allclose(f(42), 1)
    graph = f.maker.fgraph.toposort()
    assert isinstance(graph[0].inputs[0], TensorConstant)

    # e^x / e^x = e^(x-x) = e^0 = 1
    f = function([x], expx / expx, mode)
    utt.assert_allclose(f(42), 1)
    graph = f.maker.fgraph.toposort()
    assert isinstance(graph[0].inputs[0], TensorConstant)


def test_local_mul_pow_to_pow_add():
    # Default and FAST_RUN modes put a Composite op into the final graph,
    # whereas FAST_COMPILE doesn't.  To unify the graph the test cases analyze across runs,
    # we'll avoid the insertion of Composite ops in each mode by skipping Fusion rewrites
    mode = (
        get_default_mode()
        .excluding("fusion")
        .including("local_mul_exp_to_exp_add")
        .including("local_mul_pow_to_pow_add")
    )

    x = scalar("x")
    y = scalar("y")
    z = scalar("z")
    w = scalar("w")
    v = scalar("v")
    u = scalar("u")
    t = scalar("t")
    s = scalar("s")
    a = scalar("a")
    b = scalar("b")
    c = scalar("c")

    # 2^x * 2^y * 2^z * 2^w = 2^(x+y+z+w)
    op = 2**x * 2**y * 2**z * 2**w
    f = function([x, y, z, w], op, mode)
    utt.assert_allclose(f(3, 4, 5, 6), 2 ** (3 + 4 + 5 + 6))
    graph = f.maker.fgraph.toposort()
    assert all(isinstance(n.op, Elemwise) for n in graph)
    assert any(isinstance(n.op.scalar_op, ps.Add) for n in graph)
    assert not any(isinstance(n.op.scalar_op, ps.Mul) for n in graph)

    # 2^x * a^y * 2^z * b^w * c^v * a^u * s * b^t = 2^(x+z) * a^(y+u) * b^(w+t) * c^v * s
    op = 2**x * a**y * 2**z * b**w * c**v * a**u * s * b**t
    f = function([x, y, z, w, v, u, t, s, a, b, c], op, mode)
    utt.assert_allclose(
        f(4, 5, 6, 7, 8, 9, 10, 11, 2.5, 3, 3.5),
        2 ** (4 + 6) * 2.5 ** (5 + 9) * 3 ** (7 + 10) * 3.5**8 * 11,
    )
    graph = f.maker.fgraph.toposort()
    assert all(isinstance(n.op, Elemwise) for n in graph)
    assert len([True for n in graph if isinstance(n.op.scalar_op, ps.Add)]) == 3
    assert len([True for n in graph if isinstance(n.op.scalar_op, ps.Pow)]) == 4
    assert any(isinstance(n.op.scalar_op, ps.Mul) for n in graph)

    # (2^x / 2^y) * (a^z / a^w) = 2^(x-y) * a^(z-w)
    op = 2**x / 2**y * (a**z / a**w)
    f = function([x, y, z, w, a], op, mode)
    utt.assert_allclose(f(3, 5, 6, 4, 7), 2 ** (3 - 5) * 7 ** (6 - 4))
    graph = f.maker.fgraph.toposort()
    assert all(isinstance(n.op, Elemwise) for n in graph)
    assert len([True for n in graph if isinstance(n.op.scalar_op, ps.Sub)]) == 2
    assert any(isinstance(n.op.scalar_op, ps.Mul) for n in graph)

    # a^x * a^y * exp(z) * exp(w) = a^(x+y) * exp(z+w)
    op = a**x * a**y * exp(z) * exp(w)
    f = function([x, y, z, w, a], op, mode)
    utt.assert_allclose(f(3, 4, 5, 6, 2), 2 ** (3 + 4) * np.exp(5 + 6))
    graph = f.maker.fgraph.toposort()
    assert all(isinstance(n.op, Elemwise) for n in graph)
    assert len([True for n in graph if isinstance(n.op.scalar_op, ps.Add)]) == 2
    assert any(isinstance(n.op.scalar_op, ps.Mul) for n in graph)


def test_local_expm1():
    x = matrix("x")
    u = scalar("u")

    y = exp(x) - 1.0
    z = exp(x) - 2.0
    t = exp(x) - x
    s = exp(u) - np.ones((4, 3)).astype(config.floatX)
    MODE = get_default_mode().including("local_expm1")
    f = function([x], y, mode=MODE)
    g = function([x], z, mode=MODE)
    h = function([x], t, mode=MODE)
    r = function([u], s, mode=MODE)
    x_val = np.random.random((4, 3)).astype(config.floatX)
    f_val = f(x_val)
    f_test = function([x], expm1(x), mode=MODE)

    utt.assert_allclose(f_val, f_test(x_val))

    assert any(
        isinstance(n.op, Elemwise) and isinstance(n.op.scalar_op, ps.basic.Expm1)
        for n in f.maker.fgraph.toposort()
    )

    assert not any(
        isinstance(n.op, Elemwise) and isinstance(n.op.scalar_op, ps.basic.Expm1)
        for n in g.maker.fgraph.toposort()
    )

    assert not any(
        isinstance(n.op, Elemwise) and isinstance(n.op.scalar_op, ps.basic.Expm1)
        for n in h.maker.fgraph.toposort()
    )

    assert any(
        isinstance(n.op, Elemwise) and isinstance(n.op.scalar_op, ps.basic.Expm1)
        for n in r.maker.fgraph.toposort()
    )


def compile_graph_log_sum_exp(x, axis, dimshuffle_op=None):
    sum_exp = pt_sum(exp(x), axis=axis)
    if dimshuffle_op:
        sum_exp = dimshuffle_op(sum_exp)
    y = log(sum_exp)
    MODE = get_default_mode().including("local_log_sum_exp")
    return function([x], y, mode=MODE)


def check_max_log_sum_exp(x, axis, dimshuffle_op=None):
    f = compile_graph_log_sum_exp(x, axis, dimshuffle_op)

    fgraph = f.maker.fgraph.toposort()
    for node in fgraph:
        if (
            hasattr(node.op, "scalar_op")
            and node.op.scalar_op == ps.basic.scalar_maximum
        ):
            return

        # In mode FAST_COMPILE, the rewrites don't replace the
        # `Max` `Op`.
        if isinstance(node.op, Max):
            return

    # TODO FIXME: Refactor this test so that it makes a direct assertion and
    # nothing more.
    raise AssertionError("No maximum detected after log_sum_exp rewrite")


def test_local_log_sum_exp_maximum():
    """Test that the rewrite is applied by checking the presence of the maximum."""
    x = tensor3("x")
    check_max_log_sum_exp(x, axis=(0,), dimshuffle_op=None)
    check_max_log_sum_exp(x, axis=(1,), dimshuffle_op=None)
    check_max_log_sum_exp(x, axis=(2,), dimshuffle_op=None)
    check_max_log_sum_exp(x, axis=(0, 1), dimshuffle_op=None)
    check_max_log_sum_exp(x, axis=(0, 1, 2), dimshuffle_op=None)

    # If a transpose is applied to the sum
    transpose_op = DimShuffle(input_ndim=2, new_order=(1, 0))
    check_max_log_sum_exp(x, axis=2, dimshuffle_op=transpose_op)

    # If the sum is performed with keepdims=True
    x = TensorType(dtype="floatX", shape=(None, 1, None))("x")
    sum_keepdims_op = x.sum(axis=(0, 1), keepdims=True).owner.op
    check_max_log_sum_exp(x, axis=(0, 1), dimshuffle_op=sum_keepdims_op)


def test_local_log_sum_exp_near_one():
    """Test that the rewritten result is correct around 1.0."""

    x = tensor3("x")
    x_val = 1.0 + np.random.random((4, 3, 2)).astype(config.floatX) / 10.0

    f = compile_graph_log_sum_exp(x, axis=(1,))
    naive_ret = np.log(np.sum(np.exp(x_val), axis=1))
    rewritten_ret = f(x_val)
    assert np.allclose(naive_ret, rewritten_ret)

    # If a transpose is applied
    transpose_op = DimShuffle(input_ndim=2, new_order=(1, 0))
    f = compile_graph_log_sum_exp(x, axis=(1,), dimshuffle_op=transpose_op)
    naive_ret = np.log(np.sum(np.exp(x_val), axis=1).T)
    rewritten_ret = f(x_val)
    assert np.allclose(naive_ret, rewritten_ret)


def test_local_log_sum_exp_large():
    """Test that the rewrite result is correct for extreme value 100."""
    x = vector("x")
    f = compile_graph_log_sum_exp(x, axis=0)

    x_val = np.array([-100.0, 100.0]).astype(config.floatX)

    rewritten_ret = f(x_val)
    assert np.allclose(rewritten_ret, 100.0)


def test_local_log_sum_exp_inf():
    """Test that when max = +-inf, the rewritten output still works correctly."""
    x = vector("x")
    f = compile_graph_log_sum_exp(x, axis=0)

    assert f([-np.inf, -np.inf]) == -np.inf
    assert f([np.inf, np.inf]) == np.inf
    assert f([-np.inf, np.inf]) == np.inf


def test_local_reciprocal_1_plus_exp():
    x = vector("x")
    y = pt.reciprocal(1 + exp(x))
    z = rewrite_graph(y, include=["canonicalization", "stabilize", "specialize"])
    assert z.owner.op == sigmoid


class TestSigmoidRewrites:
    def get_mode(self, excluding=None):
        """
        Return appropriate mode for the tests.

        Parameters
        ----------
        excluding
            List of rewrites to exclude.

        Returns
        -------
        The current default mode unless the `config.mode` option is
        set to 'FAST_COMPILE' (in which case it is replaced by the 'FAST_RUN'
        mode), without the rewrites specified in `excluding`.
        """
        if excluding is None:
            excluding = []
        m = config.mode
        if m == "FAST_COMPILE":
            mode = pytensor.compile.mode.get_mode("FAST_RUN")
        else:
            mode = pytensor.compile.mode.get_default_mode()
        if excluding:
            return mode.excluding(*excluding)
        else:
            return mode

    def test_exp_over_1_plus_exp(self):
        m = self.get_mode(excluding=["local_elemwise_fusion"])

        x = vector()
        data = np.random.random(54).astype(config.floatX)

        # tests exp_over_1_plus_exp
        f = pytensor.function([x], exp(x) / (1 + exp(x)), mode=m)
        assert [node.op for node in f.maker.fgraph.toposort()] == [sigmoid]
        f(data)
        f = pytensor.function([x], exp(x) / (2 + exp(x)), mode=m)
        assert [node.op for node in f.maker.fgraph.toposort()] != [sigmoid]
        f(data)
        f = pytensor.function([x], exp(x) / (1 - exp(x)), mode=m)
        assert [node.op for node in f.maker.fgraph.toposort()] != [sigmoid]
        f(data)
        f = pytensor.function([x], exp(x + 1) / (1 + exp(x)), mode=m)
        assert [node.op for node in f.maker.fgraph.toposort()] != [sigmoid]
        f(data)

        # tests inv_1_plus_exp
        f = pytensor.function([x], pt.fill(x, 1.0) / (1 + exp(-x)), mode=m)
        # todo: solve issue #4589 first
        # assert check_stack_trace(f, ops_to_check=sigmoid)
        assert [node.op for node in f.maker.fgraph.toposort()] == [sigmoid]
        f(data)
        f = pytensor.function([x], pt.fill(x, 1.0) / (2 + exp(-x)), mode=m)
        assert [node.op for node in f.maker.fgraph.toposort()] != [sigmoid]
        f(data)
        f = pytensor.function([x], pt.fill(x, 1.0) / (1 - exp(-x)), mode=m)
        assert [node.op for node in f.maker.fgraph.toposort()] != [sigmoid]
        f(data)
        f = pytensor.function([x], pt.fill(x, 1.1) / (1 + exp(-x)), mode=m)
        assert [node.op for node in f.maker.fgraph.toposort()] != [sigmoid]
        f(data)

        # tests inv_1_plus_exp with neg
        f = pytensor.function([x], pt.fill(x, -1.0) / (1 + exp(-x)), mode=m)
        # todo: solve issue #4589 first
        # assert check_stack_trace(
        #     f, ops_to_check=[sigmoid, neg_inplace])
        assert [node.op for node in f.maker.fgraph.toposort()] == [
            sigmoid,
            inplace.neg_inplace,
        ]
        f(data)
        f = pytensor.function([x], pt.fill(x, -1.0) / (1 - exp(-x)), mode=m)
        assert [node.op for node in f.maker.fgraph.toposort()] != [
            sigmoid,
            inplace.neg_inplace,
        ]
        f(data)
        f = pytensor.function([x], pt.fill(x, -1.0) / (2 + exp(-x)), mode=m)
        assert [node.op for node in f.maker.fgraph.toposort()] != [
            sigmoid,
            inplace.neg_inplace,
        ]
        f(data)
        f = pytensor.function([x], pt.fill(x, -1.1) / (1 + exp(-x)), mode=m)
        assert [node.op for node in f.maker.fgraph.toposort()] != [
            sigmoid,
            inplace.neg_inplace,
        ]
        f(data)

        # tests double inv_1_plus_exp with neg
        # (-1)(exp(x)) / (1+exp(x))(1+exp(-x))
        # = (-1)/(1+exp(-x)) * exp(x)/(1+exp(x))
        # = - (sigm(x) * sigm(x))
        f = pytensor.function(
            [x],
            (pt.fill(x, -1.0) * exp(x)) / ((1 + exp(x)) * (1 + exp(-x))),
            mode=m,
        )
        # todo: solve issue #4589 first
        # assert check_stack_trace(f, ops_to_check=[sigmoid, mul])
        assert [node.op for node in f.maker.fgraph.toposort()] == [sigmoid, mul]
        f(data)
        f = pytensor.function(
            [x],
            (pt.fill(x, -1.1) * exp(x)) / ((1 + exp(x)) * (1 + exp(-x))),
            mode=m,
        )
        assert [node.op for node in f.maker.fgraph.toposort()] != [
            sigmoid,
            mul,
            inplace.neg_inplace,
        ]
        f(data)
        f = pytensor.function(
            [x],
            (pt.fill(x, -1.0) * exp(x)) / ((2 + exp(x)) * (1 + exp(-x))),
            mode=m,
        )
        assert [node.op for node in f.maker.fgraph.toposort()] != [
            sigmoid,
            mul,
            inplace.neg_inplace,
        ]
        f(data)
        f = pytensor.function(
            [x],
            (pt.fill(x, -1.0) * exp(x)) / ((1 + exp(x)) * (2 + exp(-x))),
            mode=m,
        )
        assert [node.op for node in f.maker.fgraph.toposort()] != [
            sigmoid,
            mul,
            inplace.neg_inplace,
        ]
        f(data)
        f = pytensor.function(
            [x],
            (pt.fill(x, -1.0) * exp(x)) / ((1 + exp(x)) * (1 + exp(x))),
            mode=m,
        )
        assert [node.op for node in f.maker.fgraph.toposort()] != [
            sigmoid,
            mul,
            inplace.neg_inplace,
        ]
        f(data)
        f = pytensor.function(
            [x],
            (pt.fill(x, -1.0) * exp(x)) / ((1 + exp(x)) * (2 + exp(-x))),
            mode=m,
        )
        assert [node.op for node in f.maker.fgraph.toposort()] != [
            sigmoid,
            mul,
            inplace.neg_inplace,
        ]
        f(data)

    def test_local_1msigmoid(self):
        m = self.get_mode(excluding=["fusion", "inplace"])
        x = fscalar()
        xd = dscalar()

        # Test `exp_over_1_plus_exp`
        f = pytensor.function([x], 1 - exp(x) / (1 + exp(x)), mode=m)
        # FIXME: PatternNodeRewriter does not copy stack trace
        #  (see https://github.com/Theano/Theano/issues/4581)
        # assert check_stack_trace(f, ops_to_check=[neg, sigmoid])
        assert equal_computations(f.maker.fgraph.outputs, [sigmoid(-x)])

        # Test `inv_1_plus_exp`
        f = pytensor.function([x], 1 - pt.fill(x, 1.0) / (1 + exp(-x)), mode=m)
        # assert check_stack_trace(f, ops_to_check=[neg, sigmoid])
        assert equal_computations(f.maker.fgraph.outputs, [sigmoid(-x)])

        # Test float constant
        for out, expected in [
            (np.array(1.0, "float32") - sigmoid(x), sigmoid(-x)),
            (np.array(1.0, "float64") - pt.sigmoid(x), cast(sigmoid(-x), "float64")),
            (np.array(1.0, "float32") - sigmoid(xd), sigmoid(-xd)),
            (np.array(1.0, "float64") - sigmoid(xd), sigmoid(-xd)),
            (np.sum(1 / np.array([2, 3, 6], "float32")) - sigmoid(x), sigmoid(-x)),
            (np.sum(1 / np.array([2, 3, 6], "float64")) - sigmoid(xd), sigmoid(-xd)),
            (np.float32(1 - 9e-6) - sigmoid(x), np.float32(1 - 9e-6) - sigmoid(x)),
            (np.float64(1 - 1e-9) - sigmoid(xd), np.float64(1 - 1e-9) - sigmoid(xd)),
        ]:
            rewritten = rewrite_graph(
                out, include=["canonicalize", "specialize", "stabilize"]
            )
            utt.assert_equal_computations([rewritten], [expected], original=out)

    def test_local_sigm_times_exp(self):
        """
        exp(x) * sigm(-x) -> sigm(x)
        exp(-x) * sigm(x) -> sigm(-x)
        """

        def match(func, ops):
            # print [node.op.scalar_op for node in func.maker.fgraph.toposort()]
            assert [node.op for node in func.maker.fgraph.toposort()] == ops

        m = self.get_mode(excluding=["local_elemwise_fusion", "inplace"])
        x, y = vectors("x", "y")

        f = pytensor.function([x], sigmoid(-x) * exp(x), mode=m)
        match(f, [sigmoid])
        assert check_stack_trace(f, ops_to_check=sigmoid)

        f = pytensor.function([x], sigmoid(x) * exp(-x), mode=m)
        match(f, [neg, sigmoid])
        assert check_stack_trace(f, ops_to_check=sigmoid)

        f = pytensor.function([x], -(-(-(sigmoid(x)))) * exp(-x), mode=m)
        match(f, [neg, sigmoid, neg])
        # assert check_stack_trace(f, ops_to_check=sigmoid)

        f = pytensor.function(
            [x, y],
            (sigmoid(x) * sigmoid(-y) * -exp(-x) * exp(x * y) * exp(y)),
            mode=m,
        )
        topo = f.maker.fgraph.toposort()
        for op, nb in [(sigmoid, 2), (mul, 2), (neg, 1), (exp, 1)]:
            assert sum(n.op == op for n in topo) == nb
        # assert check_stack_trace(f, ops_to_check=[sigmoid, mul,
        #                                           exp])

    def test_perform_sigm_times_exp(self):
        """Test the core function doing the `sigm_times_exp` rewrite.

        It is easier to test different graph scenarios this way than by
        compiling an PyTensor function.
        """

        x, y, z, t = vectors("x", "y", "z", "t")
        exp_op = exp

        def check(expr1, expr2):
            trees = [parse_mul_tree(e) for e in (expr1, expr2)]
            perform_sigm_times_exp(trees[0])
            trees[0] = simplify_mul(trees[0])
            good = is_same_graph(compute_mul(trees[0]), compute_mul(trees[1]))
            # if not good:
            #     print(trees[0])
            #     print(trees[1])
            #     print("***")
            #     pytensor.printing.debugprint(compute_mul(trees[0]))
            #     print("***")
            #     pytensor.printing.debugprint(compute_mul(trees[1]))
            assert good

        check(sigmoid(x) * exp_op(-x), sigmoid(-x))
        check(
            -x * sigmoid(x) * (y * (-1 * z) * exp_op(-x)),
            -x * sigmoid(-x) * (y * (-1 * z)),
        )
        check(
            -sigmoid(-x)
            * (
                exp_op(y)
                * (-exp_op(-z) * 3 * -exp_op(x))
                * (y * 2 * (-sigmoid(-y) * (z + t) * exp_op(z)) * sigmoid(z))
            )
            * -sigmoid(x),
            sigmoid(x)
            * (-sigmoid(y) * (-sigmoid(-z) * 3) * (y * 2 * ((z + t) * exp_op(z))))
            * (-sigmoid(x)),
        )
        check(
            exp_op(-x) * -exp_op(-x) * (-sigmoid(x) * -sigmoid(x)),
            -sigmoid(-x) * sigmoid(-x),
        )
        check(-exp_op(x) * -sigmoid(-x) * -exp_op(-x), -sigmoid(-x))

    def test_grad_log1msigm(self):
        # At some point, this returned nan, because (1 - sigm(x)) was
        # on both the numerator and the denominator of a fraction,
        # but the two nodes in question had not been merged.
        x = matrix("x")
        lr = scalar("lr")

        s = sigmoid(x)
        l = log(1 - s)
        c = l.mean()
        ux = x - lr * pytensor.grad(c, x)

        # Before the rewriting, inf and NaN will be produced in the graph,
        # and DebugMode will complain. Everything is fine afterwards.
        mode = self.get_mode()
        if not isinstance(mode, pytensor.compile.debugmode.DebugMode):
            f = pytensor.function([x, lr], ux, mode=mode)
            ux_v = f([[50]], 0.1)
            assert not np.isnan(ux_v)


class TestSoftplusRewrites:
    def setup_method(self):
        if pytensor.config.mode == "FAST_COMPILE":
            m = pytensor.compile.mode.get_mode("FAST_RUN").excluding(
                "local_elemwise_fusion"
            )
        else:
            m = pytensor.compile.mode.get_default_mode().excluding(
                "local_elemwise_fusion"
            )
        self.m = m

    def test_logsigm_to_softplus(self):
        x = vector()

        out = log(sigmoid(x))
        f = pytensor.function([x], out, mode=self.m)

        # Fix ticket #4581 first
        # assert check_stack_trace(
        #     f, ops_to_check=(pytensor.scalar.Neg,
        #                      ScalarSoftplus))
        topo = f.maker.fgraph.toposort()
        assert len(topo) == 3
        assert isinstance(topo[0].op.scalar_op, pytensor.scalar.Neg)
        assert isinstance(topo[1].op.scalar_op, pytensor.scalar.Softplus)
        assert isinstance(topo[2].op.scalar_op, pytensor.scalar.Neg)
        f(np.random.random(54).astype(config.floatX))

    def test_log1msigm_to_softplus(self):
        x = matrix()

        out = log(1 - sigmoid(x))
        f = pytensor.function([x], out, mode=self.m)
        topo = f.maker.fgraph.toposort()
        assert len(topo) == 2
        assert isinstance(topo[0].op.scalar_op, pytensor.scalar.Softplus)
        assert isinstance(topo[1].op.scalar_op, pytensor.scalar.Neg)
        # assert check_stack_trace(f, ops_to_check='all')
        f(np.random.random((54, 11)).astype(config.floatX))

        # Test close to 1
        x_dtype = np.dtype(x.dtype).type
        out = log(np.nextafter(x_dtype(1), x_dtype(2)) - sigmoid(x))
        f = pytensor.function([x], out, mode=self.m)
        topo = f.maker.fgraph.toposort()
        assert len(topo) == 2
        assert isinstance(topo[0].op.scalar_op, pytensor.scalar.Softplus)
        assert isinstance(topo[1].op.scalar_op, pytensor.scalar.Neg)

        # Same test with a flatten
        out = log(1 - pt.flatten(sigmoid(x)))
        f = pytensor.function([x], out, mode=self.m)

        # assert check_stack_trace(f, ops_to_check='all')
        topo = f.maker.fgraph.toposort()
        assert len(topo) == 3
        assert pt.is_flat(topo[0].outputs[0])
        assert isinstance(topo[1].op.scalar_op, pytensor.scalar.Softplus)
        assert isinstance(topo[2].op.scalar_op, pytensor.scalar.Neg)
        f(np.random.random((54, 11)).astype(config.floatX))

        # Same test with a reshape
        out = log(1 - sigmoid(x).reshape([x.size]))
        f = pytensor.function([x], out, mode=self.m)
        topo = f.maker.fgraph.toposort()
        # assert len(topo) == 3
        assert any(isinstance(node.op, Reshape) for node in topo)
        assert any(
            isinstance(
                getattr(node.op, "scalar_op", None),
                pytensor.scalar.Softplus,
            )
            for node in topo
        )
        f(np.random.random((54, 11)).astype(config.floatX))

    def test_log1pexp_to_softplus(self):
        m = pytensor.config.mode
        if m == "FAST_COMPILE":
            m = "FAST_RUN"

        x = vector()

        out = log(1 + exp(x))
        f = pytensor.function([x], out, mode=self.m)

        # Fix ticket #4581 first
        # assert check_stack_trace(f, ops_to_check='all')
        topo = f.maker.fgraph.toposort()
        assert len(topo) == 1
        assert isinstance(topo[0].op.scalar_op, pytensor.scalar.Softplus)
        f(np.random.random(54).astype(config.floatX))

    def test_log1p_neg_sigmoid_to_softpuls(self):
        x = scalar()
        out = log1p(-sigmoid(x))
        f = pytensor.function([x], out, mode=self.m)

        topo = f.maker.fgraph.toposort()
        assert len(topo) == 2
        assert isinstance(topo[0].op.scalar_op, pytensor.scalar.Softplus)
        assert isinstance(topo[1].op.scalar_op, pytensor.scalar.Neg)

        # This value would underflow to -inf without rewrite
        assert np.isclose(f(37.0), -37.0)


class TestSigmoidUtils:
    """Test utility functions used in the rewrites for `sigmoid`/`softplus` expressions."""

    def test_compute_mul(self):
        x, y, z = vectors("x", "y", "z")
        tree = (x * y) * -z
        mul_tree = parse_mul_tree(tree)
        assert parse_mul_tree(compute_mul(mul_tree)) == mul_tree
        assert is_same_graph(compute_mul(parse_mul_tree(tree)), tree)

    def test_parse_mul_tree(self):
        x, y, z = vectors("x", "y", "z")
        assert parse_mul_tree(x * y) == [False, [[False, x], [False, y]]]
        assert parse_mul_tree(-(x * y)) == [True, [[False, x], [False, y]]]
        assert parse_mul_tree(-x * y) == [False, [[True, x], [False, y]]]
        assert parse_mul_tree(-x) == [True, x]
        assert parse_mul_tree((x * y) * -z) == [
            False,
            [[False, [[False, x], [False, y]]], [True, z]],
        ]

    def test_is_1pexp(self):
        x = vector("x")
        exp_op = exp
        assert is_1pexp(1 + exp_op(x), False) == (False, x)
        assert is_1pexp(exp_op(x) + 1, False) == (False, x)
        for neg_, exp_arg in (
            is_1pexp(x, only_process_constants=False)
            for x in [(1 + exp_op(-x)), (exp_op(-x) + 1)]
        ):
            assert not neg_ and is_same_graph(exp_arg, -x)
        assert is_1pexp(1 - exp_op(x), False) is None
        assert is_1pexp(2 + exp_op(x), False) is None
        assert is_1pexp(exp_op(x) + 2, False) is None
        assert is_1pexp(exp_op(x) - 1, False) is None
        assert is_1pexp(-1 + exp_op(x), False) is None
        assert is_1pexp(1 + 2 * exp_op(x), False) is None


def test_local_logit_sigmoid():
    """Test that graphs of the form ``logit(sigmoid(x))`` and ``sigmoid(logit(x))`` get rewritten to ``x``."""

    def logit_fn(x):
        return log(x / (1 - x))

    x = fmatrix()

    out = sigmoid(logit_fn(x))
    fg = rewrite(FunctionGraph([x], [out]))
    assert not list(fg.toposort())
    assert fg.inputs[0] is fg.outputs[0]

    out = logit_fn(sigmoid(x))
    fg = rewrite(FunctionGraph([x], [out]))
    assert not list(fg.toposort())
    assert fg.inputs[0] is fg.outputs[0]


def test_local_useless_conj():
    default_mode = get_default_mode()

    # Test for all zeros
    x = scalar()
    s = _conj(x)
    mode_with_rewrite = default_mode.including("canonicalization", "local_useless_conj")
    f = function([x], s, mode=mode_with_rewrite)
    assert not any(node.op == _conj for node in f.maker.fgraph.apply_nodes)

    x = zscalar()
    s = _conj(x)
    mode_with_rewrite = default_mode.including("canonicalization", "local_useless_conj")
    f = function([x], s, mode=mode_with_rewrite)
    assert any(node.op == _conj for node in f.maker.fgraph.apply_nodes)


def test_local_sub_neg_to_add():
    x = scalar("x")
    y = vector("y")

    f = function([x, y], x - (-y), mode=Mode("py"))

    nodes = [
        node.op
        for node in f.maker.fgraph.toposort()
        if not isinstance(node.op, DimShuffle)
    ]
    assert nodes == [pt.add]

    x_test = np.full((), 1.0, dtype=config.floatX)
    y_test = np.full(5, 2.0, dtype=config.floatX)
    assert np.allclose(f(x_test, y_test), x_test - (-y_test))


def test_local_sub_neg_to_add_const():
    # This rewrite is achieved by the local_add_canonizer
    x = vector("x")
    const = 5.0

    f = function([x], x - (-const), mode=Mode("py"))

    nodes = [
        node.op
        for node in f.maker.fgraph.toposort()
        if not isinstance(node.op, DimShuffle)
    ]
    assert nodes == [pt.add]

    x_test = np.array([3, 4], dtype=config.floatX)
    assert np.allclose(f(x_test), x_test - (-const))


@pytest.mark.parametrize("first_negative", (True, False))
def test_local_add_neg_to_sub(first_negative):
    x = scalar("x")
    y = vector("y")
    out = -x + y if first_negative else x + (-y)

    f = function([x, y], out, mode=Mode("py"))

    nodes = [
        node.op
        for node in f.maker.fgraph.toposort()
        if not isinstance(node.op, DimShuffle)
    ]
    assert nodes == [pt.sub]

    x_test = np.full((), 1.0, dtype=config.floatX)
    y_test = np.full(5, 2.0, dtype=config.floatX)
    exp = -x_test + y_test if first_negative else x_test + (-y_test)
    assert np.allclose(f(x_test, y_test), exp)


@pytest.mark.parametrize(
    "op_name",
    ["log_1_minus_exp", "log1p_minus_exp", "log_minus_expm1", "log_minus_exp_minus_1"],
)
def test_log1mexp_stabilization(op_name):
    mode = Mode("py").including("stabilize")

    x = vector()
    if op_name == "log_1_minus_exp":
        f = function([x], log(1 - exp(x)), mode=mode)
    elif op_name == "log1p_minus_exp":
        f = function([x], log1p(-exp(x)), mode=mode)
    elif op_name == "log_minus_expm1":
        f = function([x], log(-expm1(x)), mode=mode)
    elif op_name == "log_minus_exp_minus_1":
        f = function([x], log(-(exp(x) - 1)), mode=mode)

    nodes = [node.op for node in f.maker.fgraph.toposort()]
    assert nodes == [pt.log1mexp]

    # Check values that would under or overflow without rewriting
    assert f([-(2.0**-55)]) != -np.inf
    overflow_value = -500.0 if config.floatX == "float64" else -100.0
    assert f([overflow_value]) < 0

    # Check values around the switch point np.log(0.5)
    assert np.allclose(
        f(np.array([-0.8, -0.6], dtype=config.floatX)),
        np.log(1 - np.exp([-0.8, -0.6])),
    )


def test_logdiffexp():
    rng = np.random.default_rng(3559)
    mode = Mode("py").including("stabilize").excluding("fusion")

    x = fmatrix("x")
    y = fmatrix("y")
    f = function([x, y], log(exp(x) - exp(y)), mode=mode)

    graph = f.maker.fgraph.toposort()
    assert (
        len(
            [
                node
                for node in graph
                if isinstance(node.op, Elemwise)
                and isinstance(node.op.scalar_op, ps.Exp | ps.Log)
            ]
        )
        == 0
    )
    assert (
        len(
            [
                node
                for node in graph
                if isinstance(node.op, Elemwise)
                and isinstance(node.op.scalar_op, ps.Log1mexp)
            ]
        )
        == 1
    )

    y_test = rng.normal(size=(3, 2)).astype("float32")
    x_test = rng.normal(size=(3, 2)).astype("float32") + y_test.max()
    np.testing.assert_almost_equal(
        f(x_test, y_test), np.log(np.exp(x_test) - np.exp(y_test))
    )


def test_polygamma_specialization():
    x = vector("x")

    y1 = polygamma(0, x)
    y2 = polygamma(1, x)
    y3 = polygamma(2, x)

    fn = pytensor.function(
        [x], [y1, y2, y3], mode=get_default_mode().including("specialize")
    )
    fn_outs = fn.maker.fgraph.outputs
    assert isinstance(fn_outs[0].owner.op.scalar_op, Psi)
    assert isinstance(fn_outs[1].owner.op.scalar_op, TriGamma)
    assert isinstance(fn_outs[2].owner.op.scalar_op, PolyGamma)


@pytest.mark.skipif(
    config.mode == "FAST_COMPILE",
    reason="Rewrite is only relevant in FAST_RUN",
)
def test_local_batched_matmul_to_core_matmul():
    rng = np.random.default_rng(seed=4433)

    # x is batched but not y
    x = pt.tensor("x", shape=(None, 3, 2), dtype="float64")
    y = pt.tensor("y", shape=(2, 2), dtype="float64")
    out = x @ y
    assert isinstance(out.owner.op, Blockwise)

    fn = pytensor.function([x, y], out)
    assert not any(
        isinstance(node.op, Blockwise) for node in fn.maker.fgraph.apply_nodes
    )

    x_test = rng.normal(size=(5, 3, 2))
    y_test = rng.normal(size=(2, 2))
    np.testing.assert_allclose(fn(x_test, y_test), x_test @ y_test)

    # y is batched but not x
    x = pt.tensor("x", shape=(1, 3, 2), dtype="float64")
    y = pt.tensor("y", shape=(5, 2, 2), dtype="float64")
    out = x @ y
    assert isinstance(out.owner.op, Blockwise)

    fn = pytensor.function([x, y], out)
    assert not any(
        isinstance(node.op, Blockwise) for node in fn.maker.fgraph.apply_nodes
    )

    x_test = rng.normal(size=(1, 3, 2))
    y_test = rng.normal(size=(5, 2, 2))
    np.testing.assert_allclose(fn(x_test, y_test), x_test @ y_test)

    # Both x and y are batched, rewrite does not apply
    x = pt.tensor("x", shape=(None, 3, 2), dtype="float64")
    y = pt.tensor("y", shape=(5, 2, 2), dtype="float64")
    out = x @ y

    fn = pytensor.function([x, y], out)
    x_test = rng.normal(size=(5, 3, 2))
    y_test = rng.normal(size=(5, 2, 2))
    np.testing.assert_allclose(fn(x_test, y_test), x_test @ y_test)


@pytest.mark.parametrize(
    "mat_shape, vec_shape",
    [
        [(1, 2, 2), (5, 2)],
        [(5, 2, 2), (1, 2)],
        [(1, 1, 2, 2), (7, 5, 2)],
        [(7, 5, 2, 2), (1, 1, 5, 2)],
        [(1, 5, 1, 2, 2), (7, 5, 7, 2)],
        [(7, 5, 7, 2, 2), (1, 5, 1, 2)],
        [(5, 1, 3, 1, 2, 2), (1, 7, 3, 7, 2)],
        [(1, 7, 3, 7, 2, 2), (5, 1, 3, 1, 2)],
    ],
    ids=str,
)
@pytest.mark.parametrize("func", ("matvec", "vecmat", "vecdot"))
def test_batch_matvec_to_matmul(func, mat_shape, vec_shape):
    def count_matvec_nodes(graph):
        # Counts how many matmul nodes actually correspond to matvec or vecmat
        return len(
            [
                var
                for var in ancestors([graph])
                if (
                    var.owner is not None
                    and var.owner.op == _matmul
                    and (
                        (var.owner.inputs[0].type.shape[-2] == 1)
                        or (var.owner.inputs[1].type.shape[-1] == 1)
                    )
                )
            ]
        )

    mat = pt.tensor("mat", shape=mat_shape, dtype="float64")
    vec = pt.tensor("vec", shape=vec_shape, dtype="float64")

    if func == "matvec":
        out = pt.matvec(mat, vec)
    elif func == "vecmat":
        out = pt.vecmat(vec, mat)
    elif func == "vecdot":
        out = pt.vecdot(mat[..., 0], vec)
    else:
        raise NotImplementedError(func)

    assert count_matvec_nodes(out) == 1

    rewritten_out = rewrite_graph(
        out,
        include=(
            "canonicalize",
            "specialize",
        ),
        exclude=(
            "local_eager_useless_unbatched_blockwise",
            "specialize_matmul_to_batched_dot",
        ),
    )
    # No `matvec` in the rewritten out if one of the vector can be treated as a matrix
    expected = not any(
        mat_dim == 1 and vec_dim != 1
        for vec_dim, mat_dim in zip(vec_shape[:-1], mat_shape[:-2])
    )
    if not expected and func == "vecdot":
        # In this case there are two vectors, so we may still end up with a `matvec` unless the second vec can also be treated as matrix
        expected = not any(
            mat_dim != 1 and vec_dim == 1
            for vec_dim, mat_dim in zip(vec_shape[:-1], mat_shape[:-2])
        )

    assert count_matvec_nodes(rewritten_out) == expected

    rng = np.random.default_rng(mat_shape + vec_shape)
    eval_dict = {mat: rng.random(mat.type.shape), vec: rng.random(vec.type.shape)}
    # Evaluate results are correct without further rewrites
    no_optimization = Mode(linker="py", optimizer=None)
    np.testing.assert_allclose(
        rewritten_out.eval(eval_dict, mode=no_optimization),
        out.eval(eval_dict, mode=no_optimization),
    )


def test_log_kv_stabilization():
    x = pt.scalar("x")
    out = log(kv(4.5, x))

    # Expression would underflow to -inf without rewrite
    mode = get_default_mode().including("stabilize")
    # Reference value from mpmath
    # mpmath.log(mpmath.besselk(4.5, 1000.0))
    np.testing.assert_allclose(
        out.eval({x: 1000.0}, mode=mode),
        -1003.2180912984705,
    )


@pytest.mark.parametrize("shape", [(), (4, 5, 6)], ids=["scalar", "tensor"])
def test_pow_1_rewrite(shape):
    x = pt.tensor("x", shape=shape)
    z = 1**x

    assert isinstance(z.owner.op, Elemwise) and isinstance(
        z.owner.op.scalar_op, ps.basic.Pow
    )

    f = pytensor.function([x], z)
    assert not any(
        isinstance(node.op, Elemwise) and isinstance(node.op.scalar_op, ps.basic.Pow)
        for node in f.maker.fgraph.toposort()
    )

    x_val = np.random.random(shape).astype(config.floatX)
    np.testing.assert_allclose(z.eval({x: x_val}), f(x_val))


@pytest.mark.parametrize(
    "a_shape,b_shape",
    [
        ((1,), (1,)),
        ((3, 1), (1,)),
        ((1,), (1, 3)),
        ((3, 1), (1, 3)),
    ],
    ids=str,
)
@pytest.mark.parametrize("batched", (False, True))
def test_local_dot_to_mul(batched, a_shape, b_shape):
    a = tensor("a", shape=a_shape)
    b = tensor("b", shape=b_shape)

    out = dot(a, b)
    if batched:
        batch_a = tensor("batch_a", shape=(2, 1, 5, *a_shape))
        batch_b = tensor("batch_b", shape=(2, 7, 1, *b_shape))
        out = vectorize_graph(out, {a: batch_a, b: batch_b})
        a = batch_a
        b = batch_b

    assert (
        sum(
            isinstance(var.owner.op, (Blockwise | Dot))
            for var in ancestors([out])
            if var.owner
        )
        == 1
    )

    # For now we do not rewrite only the case of unbatched outer
    core_outer = (not batched) and (a_shape == (3, 1)) and (b_shape == (1, 3))
    rewritten_out = rewrite_graph(out)
    assert rewritten_out.type.shape == out.type.shape
    assert sum(
        isinstance(var.owner.op, (Blockwise | Dot))
        for var in ancestors([rewritten_out])
        if var.owner
    ) == (1 if core_outer else 0)

    a_test = np.random.normal(size=a.type.shape).astype(a.type.dtype)
    b_test = np.random.normal(size=b.type.shape).astype(b.type.dtype)
    test_mode = Mode(linker="py", optimizer=None)
    np.testing.assert_allclose(
        out.eval({a: a_test, b: b_test}, mode=test_mode),
        rewritten_out.eval({a: a_test, b: b_test}, mode=test_mode),
    )
