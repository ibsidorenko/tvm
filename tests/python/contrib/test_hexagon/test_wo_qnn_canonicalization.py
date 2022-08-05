import pytest
import numpy as np

import tvm.testing
from tvm import relay
from tvm.contrib.hexagon.session import Session
from tvm.contrib import graph_executor
from tvm.relay.backend import Executor


@tvm.testing.requires_hexagon
def test_no_qnn_pass():
    x = relay.var("x", shape=(4, 8), dtype="float32")
    op0 = relay.qnn.op.quantize(x, relay.const(2.0), relay.const(10), out_dtype="uint8")
    op1 = relay.qnn.op.dequantize(op0, relay.const(0.5), relay.const(5))
    mod = tvm.IRModule.from_expr(op1)

    target_hexagon = tvm.target.hexagon("v68")
    # Default compilation flow
    with tvm.transform.PassContext(opt_level=3):
        opt_mod_1, _ = relay.optimize(mod, tvm.target.Target(target_hexagon, host=target_hexagon))

    # Disable QNN legalization and canonicalization passes
    with tvm.transform.PassContext(opt_level=3, disabled_pass=["Legalize"]):
        opt_mod_2, _ = relay.optimize(mod, tvm.target.Target(target_hexagon, host=target_hexagon))

    # Check that during Default compilation flow we do not call qnn::canonicalization pass.
    tvm.ir.assert_structural_equal(opt_mod_1, opt_mod_2)


def execute(executor, data_np, weight_np, bias_np = None):
    executor.set_input("data", data_np)
    executor.set_input("weight", weight_np)
    if bias_np is not None:
        executor.set_input("bias", bias_np)
    executor.run()
    return executor.get_output(0)


@tvm.testing.requires_hexagon
def test_qnn_conv2d_rq(hexagon_session: Session):
    data_shape = [1, 64, 64, 64]
    weight_shape = [64, 64, 3, 3]
    data = relay.var("data", shape=data_shape, dtype="float32")
    weight = relay.var("weight", shape=weight_shape, dtype="float32")
    op0 = relay.qnn.op.quantize(data, relay.const(0.078), relay.const(0), out_dtype="int8")
    op1 = relay.qnn.op.quantize(weight, relay.const(0.07), relay.const(0), out_dtype="int8")
    op2 = relay.qnn.op.conv2d(op0,
                              op1,
                              input_zero_point=relay.const(0),
                              kernel_zero_point=relay.const(0),
                              input_scale=relay.const(0.078),
                              kernel_scale=relay.const(0.07),
                              padding=[0, 0, 0, 0],
                              channels=64,
                              kernel_size=[3, 3])
    op5 = relay.qnn.op.requantize(op2,
                                  input_scale=relay.const(0.05),
                                  input_zero_point=relay.const(0),
                                  output_scale=relay.const(0.21),
                                  output_zero_point=relay.const(61),
                                  out_dtype="int8")
    relay_mod = tvm.IRModule.from_expr(op5)

    target_hexagon = tvm.target.hexagon("v68")
    target_llvm = tvm.target.Target("llvm")
    executor = Executor("graph", {"link-params": True})
    with tvm.transform.PassContext(opt_level=3):
        hexagon_lowered = tvm.relay.build(
            relay_mod,
            tvm.target.Target(target_hexagon, host=target_hexagon),
            executor=executor,
        )

        llvm_lowered = tvm.relay.build(
            relay_mod,
            tvm.target.Target(target_llvm, host=target_llvm),
            executor=executor,
        )
    
    data_np = np.random.rand(*data_shape) - 0.5
    weight_np = np.random.rand(*weight_shape) - 0.5

    hx_m = hexagon_session.get_executor_from_factory(hexagon_lowered)
    hexagon_output = execute(hx_m, data_np, weight_np)

    dev = tvm.cpu(0)
    llvm_m = graph_executor.GraphModule(llvm_lowered["default"](dev))
    llvm_out = execute(llvm_m, data_np, weight_np)

    np.testing.assert_equal(hexagon_output.numpy(), llvm_out.numpy())


@tvm.testing.requires_hexagon
def test_qnn_dense_bias_rq(hexagon_session: Session):
    data_shape = [8, 8]
    weight_shape = [16, 8]
    bias_shape = [16]
    data = relay.var("data", shape=data_shape, dtype="float32")
    weight = relay.var("weight", shape=weight_shape, dtype="float32")
    bias = relay.var("bias", shape=bias_shape, dtype="float32")

    op0 = relay.qnn.op.quantize(data, relay.const(0.08), relay.const(0), out_dtype="int8")
    op1 = relay.qnn.op.quantize(weight, relay.const(0.07), relay.const(0), out_dtype="int8")
    op2 = relay.qnn.op.dense(op0,
                             op1,
                             input_zero_point=relay.const(0),
                             kernel_zero_point=relay.const(0),
                             input_scale=relay.const(0.08),
                             kernel_scale=relay.const(0.07),
                             units=None)
    op3 = relay.qnn.op.quantize(bias, relay.const(0.5), relay.const(0), out_dtype="int32")
    op4 = relay.nn.bias_add(op2, op3)
    op5 = relay.qnn.op.requantize(op4,
                                  input_scale=relay.const(0.05),
                                  input_zero_point=relay.const(0),
                                  output_scale=relay.const(0.212),
                                  output_zero_point=relay.const(10),
                                  out_dtype="int8")
    relay_mod = tvm.IRModule.from_expr(op5)

    target_hexagon = tvm.target.hexagon("v68")
    target_llvm = tvm.target.Target("llvm")
    executor = Executor("graph", {"link-params": True})
    with tvm.transform.PassContext(opt_level=3):
        hexagon_lowered = tvm.relay.build(
            relay_mod,
            tvm.target.Target(target_hexagon, host=target_hexagon),
            executor=executor,
        )

        llvm_lowered = tvm.relay.build(
            relay_mod,
            tvm.target.Target(target_llvm, host=target_llvm),
            executor=executor,
        )
    
    data_np = np.random.rand(*data_shape) - 0.5
    weight_np = np.random.rand(*weight_shape) - 0.5
    bias_np = np.random.rand(*bias_shape)

    hx_m = hexagon_session.get_executor_from_factory(hexagon_lowered)
    hexagon_output = execute(hx_m, data_np, weight_np, bias_np)

    dev = tvm.cpu(0)
    llvm_m = graph_executor.GraphModule(llvm_lowered["default"](dev))
    llvm_out = execute(llvm_m, data_np, weight_np, bias_np)

    np.testing.assert_equal(hexagon_output.numpy(), llvm_out.numpy())


if __name__ == "__main__":
    tvm.testing.main()
