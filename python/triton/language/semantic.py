from . import core
from typing import List, Tuple, Optional
from triton._C.libtriton.triton import ir



## Create custom exception that prints message "hello"
class IncompatibleTypeErrorimpl(Exception):
  def __init__(self, type_a, type_b):
    self.type_a = type_a
    self.type_b = type_b
    self.message = "invalid operands of type " + self.type_a.repr() + " and " + self.type_b.repr()
    super(IncompatibleTypeErrorimpl, self).__init__(self.message)


##===----------------------------------------------------------------------===##
##                              Programming Model
##===----------------------------------------------------------------------===##

def program_id(axis, builder):
  return builder.create_get_program_id(axis)

def num_programs(axis, builder):
  return builder.create_get_num_programs(axis)

#===----------------------------------------------------------------------===//
#                               Implicit Casting Utilities
#===----------------------------------------------------------------------===//

def integer_promote_impl(a_ty: core.dtype, b_ty: core.dtype) -> core.dtype:
  a_rank = a_ty.int_bitwidth
  b_rank = b_ty.int_bitwidth
  a_sn = a_ty.int_signedness
  b_sn = b_ty.int_signedness
  # Rules for signedness taken from "Usual arithmetic conversions" on
  # https://en.cppreference.com/w/c/language/conversion.
  if a_sn == b_sn:
    return a_ty if a_rank > b_rank else b_ty
  elif a_sn == core.dtype.SIGNEDNESS.UNSIGNED:
    return a_ty if a_rank >= b_rank else b_ty
  elif b_sn == core.dtype.SIGNEDNESS.UNSIGNED:
    return b_ty if b_rank >= a_rank else a_ty
  else:
    assert False
  

def computation_type_impl(a_ty: core.dtype, b_ty: core.dtype, div_or_mod: bool) -> core.dtype:
  # 1) if one operand is double, the other is implicitly
  #    converted to double
  if a_ty.is_fp64() or b_ty.is_fp64():
    return core.float64
  # 2) if one operand is float, the other is implicitly
  #    converted to float
  if a_ty.is_fp32() or b_ty.is_fp32():
    return core.float32
  # 3 ) if one operand is half, the other is implicitly converted to half
  #     unless we're doing / or %, which do not exist natively in PTX for fp16.
  if a_ty.is_fp16() or b_ty.is_fp16():
    if div_or_mod:
      return core.float32
    else:
      return core.float16
  if not a_ty.is_int() or not b_ty.is_int():
    assert False
  # 4 ) both operands are integer and undergo
  #    integer promotion
  if div_or_mod and a_ty.int_signedness != b_ty.int_signedness:
    raise ValueError("Cannot use /, #, or % with " + a_ty.repr() + " and " + b_ty.repr() + " because they have different signedness;" 
                        "this is unlikely to result in a useful answer. Cast them to the same signedness.")
  return integer_promote_impl(a_ty, b_ty)

#===----------------------------------------------------------------------===//
#                               Binary Operators
#===----------------------------------------------------------------------===//

def check_ptr_type_impl(type_a: core.dtype, type_b: core.dtype, allow_ptr_a: bool):
  if type_a.is_ptr():
    if not allow_ptr_a:
      raise IncompatibleTypeErrorimpl(type_a, type_b)
    # T* + U* with T != U
    if type_b.is_ptr() and (type_a != type_b):
      raise IncompatibleTypeErrorimpl(type_a, type_b)
    # T* + float
    if type_b.is_floating():
      raise IncompatibleTypeErrorimpl(type_a, type_b)

def binary_op_type_checking_impl(lhs: core.block,
                                 rhs: core.block,
                                 builder: ir.builder,
                                 allow_lhs_ptr = False, allow_rhs_ptr = False,
                                 arithmetic_check = True, div_or_mod = False
                                ) -> Tuple[core.block, core.block]:
  # implicit broadcasting
  lhs, rhs = broadcast_impl_value(lhs, rhs, builder)
  # implicit typecasting
  lhs_sca_ty = lhs.type.scalar
  rhs_sca_ty = rhs.type.scalar
  check_ptr_type_impl(lhs_sca_ty, rhs_sca_ty, allow_lhs_ptr)
  check_ptr_type_impl(rhs_sca_ty, lhs_sca_ty, allow_rhs_ptr)
  if arithmetic_check and not lhs_sca_ty.is_ptr() and not rhs_sca_ty.is_ptr():
    ret_sca_ty = computation_type_impl(lhs_sca_ty, rhs_sca_ty, div_or_mod)
    lhs = cast(lhs, ret_sca_ty, builder)
    rhs = cast(rhs, ret_sca_ty, builder)
  return lhs, rhs
  

def add(input: core.block, 
        other: core.block, 
        builder: ir.builder) -> core.block:
  input, other = binary_op_type_checking_impl(input, other, builder, True, True)
  input_scalar_ty = input.type.scalar
  other_scalar_ty = other.type.scalar
  # offset + ptr
  # ptr + offset
  if other_scalar_ty.is_ptr() and not input_scalar_ty.is_ptr():
    input, other = other, input
  if input_scalar_ty.is_ptr():
    return core.block(builder.create_gep(input, [other]), input.type)
  # float + float
  elif input_scalar_ty.is_floating():
    return core.block(builder.create_fadd(input, other), input.type)
  # int + int
  elif input_scalar_ty.is_int():
    return core.block(builder.create_add(input, other), input.type)
  assert False

def sub(input: core.block,
        other: core.block,
        builder: ir.builder) -> core.block:
  input, other = binary_op_type_checking_impl(input, other, builder, True, False)
  scalar_ty = input.type.scalar
  # ptr - offset
  if scalar_ty.is_ptr():
    return core.block(builder.create_gep(input.handle, [minus(other, builder).handle]),
                      input.type)
  # float - float
  if scalar_ty.is_floating():
    return core.block(builder.create_fsub(input.handle, other.handle), input.type)
  # int - int
  elif scalar_ty.is_int():
    return core.block(builder.create_sub(input.handle, other.handle), input.type)
  assert False

def mul(input: core.block,
        other: core.block,
        builder: ir.builder) -> core.block:
  input, other = binary_op_type_checking_impl(input, other, builder)
  scalar_ty = input.type.scalar
  # float * float
  if scalar_ty.is_floating():
    return core.block(builder.create_fmul(input.handle, other.handle), input.type)
  # * int
  elif scalar_ty.is_int():
    return core.block(builder.create_mul(input.handle, other.handle), input.type)
  assert False

def truediv(input: core.block,
            other: core.block,
            builder: ir.builder) -> core.block:
  input, other = binary_op_type_checking_impl(input, other, builder, False, False, True, True)
  input_scalar_ty = input.type.scalar
  other_scalar_ty = other.type.scalar
  # float / int
  if input_scalar_ty.is_floating() and other_scalar_ty.is_int():
    other = cast(other, input_scalar_ty, builder)
  # int / float
  elif input_scalar_ty.is_int() and other_scalar_ty.is_floating():
    input = cast(input, other_scalar_ty, builder)
  # int / int (cast to float32)
  elif input_scalar_ty.is_int() and other_scalar_ty.is_int():
    input = cast(input, core.float32, builder)
    other = cast(other, core.float32, builder)
  # float / float (cast to highest exponent type)
  elif input_scalar_ty.is_floating() and other_scalar_ty.is_floating():
    if input_scalar_ty.get_fp_mantissa_width() > other_scalar_ty.get_fp_mantissa_width():
      other = cast(other, input_scalar_ty, builder)
    else:
      input = cast(input, other_scalar_ty, builder)
  # unreachable
  else:
    assert False
  return core.block(builder.create_fdiv(input.handle, other.handle), input.type)

def floordiv(input: core.block,
            other: core.block,
            builder: ir.builder) -> core.block:
  input, other = binary_op_type_checking_impl(input, other, builder, False, False, True, True)
  input_scalar_ty = input.type.scalar
  other_scalar_ty = other.type.scalar
  if input_scalar_ty.is_int() and other_scalar_ty.is_int():
    ret_ty = integer_promote_impl(input_scalar_ty, other_scalar_ty)
    input = cast(input, ret_ty, builder)
    other = cast(other, ret_ty, builder)
    if ret_ty.is_int_signed():
      return core.block(builder.create_sdiv(input.handle, other.handle), input.type)
    else:
      return core.block(builder.create_udiv(input.handle, other.handle), input.type)
  assert False

def fdiv(input: core.block,
         other: core.block,
         ieee_rounding: bool,
         builder: ir.builder) -> core.block:
  input_scalar_ty = input.type.scalar
  other_scalar_ty = other.type.scalar
  if not input_scalar_ty.is_floating() or not other_scalar_ty.is_floating():
    raise ValueError("both operands of fdiv must have floating poscalar type")
  input, other = binary_op_type_checking_impl(input, other, builder, False, False, False, True)
  ret = builder.create_fdiv(input, other)
  ret.set_fdiv_ieee_rounding(ieee_rounding.value)
  return core.block(ret, input.type)

def mod(input: core.block,
        other: core.block,
        builder: ir.builder) -> core.block:
  input, other = binary_op_type_checking_impl(input, other, builder, False, False, True, True)
  scalar_ty = input.type.scalar
  other_scalar_ty = other.type.scalar
  # float % float
  if scalar_ty.is_floating():
    return core.block(builder.create_frem(input.handle, other.handle), input.type)
  # % int
  elif scalar_ty.is_int():
    if scalar_ty.int_signedness != other_scalar_ty.int_signedness:
      raise ValueError("Cannot mod " + scalar_ty.repr() + " by " + other_scalar_ty.repr() + \
                       " because they have different signedness;"
                       "this is unlikely to result in a useful answer. Cast them to the same signedness.")
    if scalar_ty.is_int_signed():
      return core.block(builder.create_srem(input.handle, other.handle), input.type)
    else:
      return core.block(builder.create_urem(input.handle, other.handle), input.type)
  assert False

##############
# bitwise ops
##############
def bitwise_op_type_checking_impl(input: core.block,
                                  other: core.block,
                                  builder: ir.builder) -> Tuple[core.block, core.block]:
  input, other = binary_op_type_checking_impl(input, other, builder, False, False, False)
  input_sca_ty = input.type.scalar
  other_sca_ty = other.type.scalar
  if not input_sca_ty.is_int() or not other_sca_ty.is_int():
    raise IncompatibleTypeErrorimpl(input_sca_ty, other_sca_ty)
  ret_sca_ty = integer_promote_impl(input_sca_ty, other_sca_ty)
  if ret_sca_ty != input_sca_ty:
    input = cast(input, ret_sca_ty, builder)
  if ret_sca_ty != other_sca_ty:
    other = cast(other, ret_sca_ty, builder)
  return input, other

def and_(input: core.block,
         other: core.block,
         builder: ir.builder) -> core.block:
  input, other = bitwise_op_type_checking_impl(input, other, builder)
  return core.block(builder.create_and(input.handle, other.handle), input.type)

def or_(input: core.block,
         other: core.block,
         builder: ir.builder) -> core.block:
  input, other = bitwise_op_type_checking_impl(input, other, builder)
  return core.block(builder.create_or(input.handle, other.handle), input.type)


def xor_(input: core.block,
         other: core.block,
         builder: ir.builder) -> core.block:
  input, other = bitwise_op_type_checking_impl(input, other, builder)
  return core.block(builder.create_xor(input.handle, other.handle), input.type)


def lshr(input: core.block,
         other: core.block,
         builder: ir.builder) -> core.block:
  input, other = bitwise_op_type_checking_impl(input, other, builder)
  return core.block(builder.create_lshr(input.handle, other.handle), input.type)


def shl(input: core.block,
         other: core.block,
         builder: ir.builder) -> core.block:
  input, other = bitwise_op_type_checking_impl(input, other, builder)
  return core.block(builder.create_shl(input.handle, other.handle), input.type)

#===----------------------------------------------------------------------===//
#                               Unary Operators
#===----------------------------------------------------------------------===//

def plus(input: core.block) -> core.block:
  return input

def minus(input: core.block,
          builder: core.block) -> core.block:
  input_sca_ty = input.type.scalar
  if input_sca_ty.is_ptr():
    raise ValueError("wrong type argument to unary minus (" + input_sca_ty.repr() + ")")
  _0 = core.block(ir.constant.get_null_value(input_sca_ty.to_ir(builder)), input_sca_ty)
  return sub(_0, input, builder)

def invert(input: core.block,
           builder: core.block) -> core.block:
  input_sca_ty = input.type.scalar
  if input_sca_ty.is_ptr() or input_sca_ty.is_floating():
    raise ValueError("wrong type argument to unary invert (" + input_sca_ty.repr() + ")")
  _1 = core.block(ir.constant.get_all_ones_value(input_sca_ty.to_ir(builder)), input_sca_ty)
  return xor_(input, _1, builder)


#===----------------------------------------------------------------------===//
#                               Comparison Operators
#===----------------------------------------------------------------------===//

def greater_than(input: core.block,
               other: core.block,
               builder: ir.builder) -> core.block:
  input, other = binary_op_type_checking_impl(input, other, builder)
  scalar_ty = input.type.scalar
  # float > float
  if scalar_ty.is_floating():
    return builder.create_fcmpOGT(input, other)
  # > int
  elif scalar_ty.is_int():
    if scalar_ty.is_int_signed():
      return builder.create_icmpSGT(input, other)
    else:
      return builder.create_icmpUGT(input, other)
  assert False

def greater_equal(input: core.block,
               other: core.block,
               builder: ir.builder) -> core.block:
  input, other = binary_op_type_checking_impl(input, other, builder)
  scalar_ty = input.type.scalar
  # float >= float
  if scalar_ty.is_floating():
    return builder.create_fcmpOGE(input, other)
  # >= int
  elif scalar_ty.is_int():
    if scalar_ty.is_int_signed():
      return builder.create_icmpSGE(input, other)
    else:
      return builder.create_icmpUGE(input, other)
  assert False

def less_than(input: core.block,
               other: core.block,
               builder: ir.builder) -> core.block:
  input, other = binary_op_type_checking_impl(input, other, builder)
  scalar_ty = input.type.scalar
  # float < float
  if scalar_ty.is_floating():
    return core.block(builder.create_fcmpOLT(input.handle, other.handle), input.type)
  # < int
  elif scalar_ty.is_int():
    if scalar_ty.is_int_signed():
      return core.block(builder.create_icmpSLT(input.handle, other.handle), input.type)
    else:
      return core.block(builder.create_icmpULT(input.handle, other.handle), input.type)
  assert False

def less_equal(input: core.block,
               other: core.block,
               builder: ir.builder) -> core.block:
  input, other = binary_op_type_checking_impl(input, other, builder)
  scalar_ty = input.type.scalar
  # float < float
  if scalar_ty.is_floating():
    return core.block(builder.create_fcmpOLE(input.handle, other.handle), input.type)
  # < int
  elif scalar_ty.is_int():
    if scalar_ty.is_int_signed():
      return core.block(builder.create_icmpSLE(input.handle, other.handle), input.type)
    else:
      return core.block(builder.create_icmpULE(input.handle, other.handle), input.type)
  assert False

def equal(input: core.block,
          other: core.block,
          builder: ir.builder) -> core.block:
  input, other = binary_op_type_checking_impl(input, other, builder)
  scalar_ty = input.type.scalar
  # float == float
  if scalar_ty.is_floating():
    return core.block(builder.create_fcmpOEQ(input.handle, other.handle), input.type)
  # == int
  elif scalar_ty.is_int():
    return core.block(builder.create_icmpEQ(input.handle, other.handle), input.type)
  assert False

def not_equal(input: core.block,
              other: core.block,
              builder: ir.builder) -> core.block:
  input, other = binary_op_type_checking_impl(input, other, builder)
  scalar_ty = input.type.scalar
  # float == float
  if scalar_ty.is_floating():
    return core.block(builder.create_fcmpUNE(input.handle, other.handle), input.type)
  # == int
  elif scalar_ty.is_int():
    return core.block(builder.create_icmpNE(input.handle, other.handle), input.type)
  assert False

#===----------------------------------------------------------------------===//
#                               Block Creation
#===----------------------------------------------------------------------===//

def arange(start: int, end: int, builder: ir.builder) -> core.block:
  return core.block(builder.get_range(start, end), core.int32)

def zeros(shape: List[int], dtype: core.dtype, builder: ir.builder) -> core.block:
  _0 = ir.constant.get_null_value(dtype.to_ir(builder))
  ret_ty = core.block_type(dtype, shape)
  return core.block(builder.create_splat(_0, shape), ret_ty)

#===----------------------------------------------------------------------===//
#                               Shape Manipulation
#===----------------------------------------------------------------------===//

def reshape(input: core.block,
            dst_shape: List[int],
            builder: ir.builder) -> core.block:
  numel = 1
  for s in dst_shape: 
    numel *= s
  if input.type.numel != numel:
    raise ValueError("cannot reshape block of different shape")
  ret_ty = core.block_type(input.type.scalar, dst_shape)
  return core.block(builder.create_reshape(input.handle, dst_shape), ret_ty)

def cat(lhs: core.block, rhs: core.block, builder: ir.builder) -> core.block:
  # TODO: check types
  return core.block(builder.create_cat(lhs.handle, rhs.handle), lhs.type)

def broadcast_impl_shape(input: core.block,
                         shape: List[int],
                         builder: ir.builder) -> core.block:
  if not input.type.is_block():
    return builder.create_splat(input, shape)
  src_shape = input.type.get_block_shapes()
  if len(src_shape) != len(shape):
    raise ValueError("Cannot broadcast")
  if shape == src_shape:
    return input
  return builder.create_broadcast(input, shape)

def broadcast_impl_value(lhs: core.block,
                         rhs: core.block,
                         builder: ir.builder) -> core.block:
  lhs_ty = lhs.type
  rhs_ty = rhs.type

  # make_shape_compatible(block, scalar)
  if lhs_ty.is_block() and not rhs_ty.is_block():
    ret_ty = lhs_ty
    rhs = core.block(builder.create_splat(rhs.handle, lhs_ty.get_block_shapes()), ret_ty)
  # make_shape_compatible(scalar, block)
  elif not lhs_ty.is_block() and rhs_ty.is_block():
    ret_ty = rhs_ty
    lhs = core.block(builder.create_splat(lhs.handle, rhs_ty.get_block_shapes()), ret_ty)
  # make_shape_compatible(block, block)
  elif lhs_ty.is_block() and rhs_ty.is_block():
    lhs_shape = lhs_ty.get_block_shapes()
    rhs_shape = rhs_ty.get_block_shapes()
    if len(lhs_shape) != len(rhs_shape):
      raise ValueError("Cannot make_shape_compatible: blocks must have the same rank")
    ret_shape = []
    for i in range(len(lhs_shape)):
      left = lhs_shape[i]
      right = rhs_shape[i]
      if left == 1:
        ret_shape.append(right)
      elif right == 1:
        ret_shape.append(left)
      elif left == right:
        ret_shape.append(left)
      else:
        raise ValueError("Cannot make_shape_compatible: incompatible dimensions at index " + str(i) +
                                 ": " + str(left) + " and " + str(right))
    if lhs_shape != ret_shape:
      ret_ty = core.block_type(lhs_ty.scalar, ret_shape)
      lhs = core.block(builder.create_broadcast(lhs.handle, ret_shape), ret_ty)
    if rhs_shape != ret_shape:
      ret_ty = core.block_type(rhs_ty.scalar, ret_shape)
      rhs = core.block(builder.create_broadcast(rhs.handle, ret_shape), ret_ty)
  # (scalar, scalar) => returns original blocks
  return lhs, rhs

#######
# cast
#######
def bitcast(input: core.block,
            dst_ty: core.dtype,
            builder: ir.builder) -> core.block:
  src_ty = input.type
  if src_ty.is_block():
    dst_ty = core.block_type(dst_ty, input.type.get_block_shapes())
  if src_ty == dst_ty:
    return input
  src_sca_ty = src_ty.scalar
  dst_sca_ty = dst_ty.scalar
  if src_sca_ty.is_ptr() or dst_sca_ty.is_ptr():
    return cast(input, dst_ty, builder)
  # Bitcast
  src_bits = src_sca_ty.primitive_bitwidth
  dst_bits = dst_sca_ty.primitive_bitwidth
  if src_bits != dst_bits:
    raise ValueError("Cannot bitcast data-type of size " + str(src_bits) +
                             "to data-type of size " + str(dst_bits))
  return core.block(builder.create_bitcast(input.handle, dst_ty.to_ir(builder)),
                    dst_ty)

def cast(input: core.block,
         dst_ty: core.dtype,
         builder: ir.builder) -> core.block:
  src_ty = input.type
  if src_ty.is_block():
    dst_ty = core.block_type(dst_ty, input.type.get_block_shapes())
  if src_ty == dst_ty:
    return input
  src_sca_ty = src_ty.scalar
  dst_sca_ty = dst_ty.scalar

  # bf16 <=> (not fp32)
  if (src_sca_ty.is_bf16() and not dst_sca_ty.is_fp32()) or \
     (dst_sca_ty.is_bf16() and not src_sca_ty.is_fp32()):
    return case(cast(input, core.float32, builder), dst_sca_ty, builder)

  # FP Truncation
  truncate_fp = src_sca_ty.is_floating() and \
                dst_sca_ty.is_floating() and \
                src_sca_ty.get_fp_mantissa_width() > dst_sca_ty.get_fp_mantissa_width()
  if truncate_fp:
    return core.block(builder.create_fp_trunc(input.handle, 
                                              dst_ty.to_ir(builder)),
                      dst_ty)

  # FP Extension
  ext_fp = src_sca_ty.is_floating() and \
                dst_sca_ty.is_floating() and \
                src_sca_ty.get_fp_mantissa_width() < dst_sca_ty.get_fp_mantissa_width()
  if ext_fp:
    return core.block(builder.create_fp_ext(input.handle,
                                            dst_ty.to_ir(builder)),
                      dst_ty)

  # Int cast
  if src_sca_ty.is_int() and dst_sca_ty.is_int() and \
    (src_sca_ty.int_bitwidth != dst_sca_ty.int_bitwidth or
     src_sca_ty.int_signedness != dst_sca_ty.int_signedness):
    sign_extend = src_sca_ty.is_int_signed() and src_sca_ty != builder.get_int1_ty()
    return core.block(builder.create_int_cast(input.handle,
                                              dst_ty.to_ir(builder), sign_extend),
                      dst_ty)

  # Float to Int
  if src_sca_ty.is_floating() and dst_sca_ty.is_int():
    # TODO: is this correct?
    if dst_sca_ty.is_bool():
      return core.block(builder.create_fp_to_ui(input.handle,
                                                dst_ty.to_ir(builder)),
                        dst_ty)
    else:
      return core.block(builder.create_fp_to_si(input.handle,
                                                dst_ty.to_ir(builder)),
                        dst_ty)

  # int => float
  if src_sca_ty.is_int() and dst_sca_ty.is_floating():
    if src_sca_ty.is_bool() or not src_sca_ty.is_int_signed():
      return core.block(builder.create_ui_to_fp(input.handle,
                                                dst_ty.to_ir(builder)),
                        dst_ty)
    else:
      return core.block(builder.create_si_to_fp(input.handle,
                                                dst_ty.to_ir(builder)),
                        dst_ty)

  # ptr => int
  if src_sca_ty.is_ptr() and dst_sca_ty.is_int():
    bitwidth = dst_sca_ty.int_bitwidth
    if bitwidth == 64:
      return core.block(builder.create_cast(ir.PtrToInt, input.handle, dst_ty.to_ir(builder)),
                        dst_ty)
    if bitwidth == 1:
      return not_equal(cast(input, core.int64, builder),
                       core.block(builder.get_int64(0), core.int64),
                       builder)

  if not src_sca_ty.is_ptr() and dst_sca_ty.is_ptr():
    return core.block(builder.create_int_to_ptr(input.handle, dst_ty.to_ir(builder)), dst_ty)
  # Ptr . Ptr
  if src_sca_ty.is_ptr() and dst_sca_ty.is_ptr():
    return core.block(builder.create_bitcast(input.handle, dst_ty.to_ir(builder)), dst_ty)
  # * . Bool
  if dst_sca_ty.is_bool():
    if src_sca_ty.is_ptr():
      input = cast(input, core.int64, builder)
    other = builder.get_int64(0)
    if src_ty.is_bool():
      other = builder.create_splat(other, src_ty.get_block_shapes())
    return core.block(builder.create_icmpNE(input.handle, other), dst_ty)
  assert False

#===----------------------------------------------------------------------===//
#                               Memory Operators
#===----------------------------------------------------------------------===//

def load(ptr: core.block,
         mask: Optional[core.block],
         other: Optional[core.block],
         cache_modifier: str,
         eviction_policy: str,
         is_volatile: bool,
         builder: ir.builder) -> core.block:
  if not ptr.type.scalar.is_ptr():
    raise ValueError("Pointer argument of load instruction is " + ptr.type.repr())
  if ptr.type.is_block():
    if mask:
      mask = broadcast_impl_shape(mask, ptr.type.get_block_shapes(), builder)
    if other:
      other = broadcast_impl_shape(other, ptr.type.get_block_shapes(), builder)
  
  if other:
    other = cast(other, ptr.type.scalar.element, builder)
  ptr_ty = ptr.type.scalar
  elt_ty = ptr_ty.element
  # treat bool* as int8*
  if elt_ty == core.int1:
    elt_ty = core.int8
    ptr_ty = core.pointer_type(elt_ty, ptr_ty.address_space)
    ptr = cast(ptr, ptr_ty, builder)
  
  # cache modifier
  cache = ir.CACHE_MODIFIER.NONE; # default
  if cache_modifier:
    if cache_modifier == ".ca":
      cache = ir.CACHE_MODIFIER.CA
    elif cache_modifier == ".cg":
      cache = ir.CACHE_MODIFIER.CG
    else:
      raise ValueError(f"Cache modifier {cache_modifier} not supported")
  
  # eviction policy
  eviction = ir.EVICTION_POLICY.NORMAL; #default
  if eviction_policy:
    if eviction_policy == "evict_last":
        eviction = ir.EVICTION_POLICY.EVICT_LAST
    elif eviction_policy == "evict_first":
        eviction = ir.EVICTION_POLICY.EVICT_FIRST
    else:
        raise ValueError(f"Eviction policy {eviction_policy} not supported")

  assert ptr.type.is_block()
  shape = ptr.type.get_block_shapes()
  dst_ty = core.block_type(elt_ty, shape)
  if not mask and not other:
    return core.block(builder.create_load(ptr.handle, cache, eviction, is_volatile),
                      dst_ty)
  if not mask:
    raise ValueError("`other` cannot be provided without `mask`")
  
  if not other:
    other_ir = ir.undef.get(elt_ty.to_ir(builder))
    if ptr.type.is_block():
      other_ir = builder.create_splat(other_ir, ptr.type.get_block_shapes())
    other = core.block(other_it, dst_ty)
  
  return core.block(builder.create_masked_load(ptr.handle,
                                               mask.handle,
                                               other.handle,
                                               cache, eviction, is_volatile),
                    dst_ty)

def store(ptr: core.block,
          val: core.block,
          mask: Optional[core.block],
          builder: ir.builder) -> core.block:
  if not ptr.type.scalar.is_ptr():
    raise ValueError("Pointer argument of store instruction is " + ptr.type.repr())
  if ptr.type.is_block():
    val = broadcast_impl_shape(val, ptr.type.get_block_shapes(), builder)
  if mask:
    mask = broadcast_impl_shape(mask, ptr.type.get_block_shapes(), builder)
  ptr_ty = ptr.type.scalar
  elt_ty = ptr_ty.element
  # treat bool* as int8*
  if elt_ty == core.int1:
    elt_ty = core.int8
    ptr_ty = core.pointer_type(elt_ty, ptr_ty.address_space)
    ptr = cast(ptr, ptr_ty, builder)
  
  # cast to target data-type
  val = cast(val, elt_ty, builder)
  if not mask:
    return core.block(builder.create_store(ptr.handle, val.handle))
  if not mask.type.scalar.is_bool():
    raise ValueError("Mask must have boolean scalar type")
  return core.block(builder.create_masked_store(ptr.handle, val.handle, mask.handle))

#########
# atomic
#########
def atomic_cas(ptr: core.block,
               cmp: core.block,
               val: core.block,
               builder: ir.builder) -> core.block:
  # TODO: type checking
  return core.block(builder.create_atomic_cas(ptr.handle, cmp.handle, val.handle), val.type)

def atom_red_typechecking_impl(ptr: core.block,
                               val: core.block,
                               mask: core.block,
                               builder: ir.builder) -> Tuple[core.block, core.block, core.block]:
  if not ptr.type.scalar.is_ptr():
    raise ValueError("Pointer argument of store instruction is " + ptr.type.repr())
  if ptr.type.is_block():
    if mask:
      mask = broadcast_impl_shape(mask, ptr.type.get_block_shapes(), builder)
    if val:
      val = broadcast_impl_shape(val, ptr.type.get_block_shapes(), builder)
  val = cast(val, ptr.type.scalar.element, builder)
  if not mask:
    mask_ir = builder.get_int1(True)
    if ptr.type.is_block():
      mask_ir = builder.create_splat(mask_ir, ptr.type.get_block_shapes())
    mask = core.block(mask_ir)
  return ptr, val, mask
  

def atomic_max(ptr: core.block,
               val: core.block,
               mask: core.block,
               builder: ir.builder) -> core.block:
  ptr, val, mask = atom_red_typechecking_impl(ptr, val, mask, builder)
  sca_ty = val.type.scalar
  # direct call to atomic_max for integers
  if sca_ty.is_int():
    if sca_ty.is_int_signed():
      return core.block(builder.create_atomic_rmw(ir.ATOMIC_OP.MAX, 
                                                  ptr.handle,
                                                  val.handle,
                                                  mask.handle),
                        val.type)
    else:
      return core.block(builder.create_atomic_rmw(ir.ATOMIC_OP.UMAX,
                                                  ptr.handle,
                                                  val.handle,
                                                  mask.handle),
                        val.type)
  # for float
  # return atomic_smax(i_ptr, i_val) if val >= 0
  # return atomic_umin(i_ptr, i_val) if val < 0
  i_val = bitcast(val, core.int32, builder)
  i_ptr = bitcast(ptr, core.pointer_type(core.int32, 1), builder)
  pos = greater_equal(val, core.block(ir.constant_float.get(sca_ty, 0), sca_ty), builder)
  neg = less_than(val, core.block(ir.constant_float.get(sca_ty, 0), sca_ty), builder)
  pos_ret = builder.create_atomic_rmw(ir.ATOMIC_OP.MAX, i_ptr, i_val, and_(mask, pos, builder).handle)
  neg_ret = builder.create_atomic_rmw(ir.ATOMIC_OP.UMIN, i_ptr, i_val, and_(mask, neg, builder).handle)
  return where(pos, pos_ret, neg_ret, builder)

def atomic_min(ptr: core.block,
               val: core.block,
               mask: core.block,
               builder: ir.builder) -> core.block:
  ptr, val, mask = atom_red_typechecking_impl(ptr, val, mask, builder)
  sca_ty = val.type.scalar
  # direct call to atomic_min for integers
  if sca_ty.is_int():
    if sca_ty.is_int_signed():
      return core.block(builder.create_atomic_rmw(ir.ATOMIC_OP.MIN,
                                                  ptr.handle,
                                                  val.handle,
                                                  mask.handle),
                        val.type)
    else:
      return core.block(builder.create_atomic_rmw(ir.ATOMIC_OP.UMIN,
                                                  ptr.handle,
                                                  val.handle,
                                                  mask.handle),
                        val.type)
  # for float
  # return atomic_smin(i_ptr, i_val) if val >= 0
  # return atomic_umax(i_ptr, i_val) if val < 0
  i_val = bitcast(val, builder.get_int32_ty(), builder)
  i_ptr = bitcast(ptr, ir.type.make_ptr(builder.get_int32_ty(), 1), builder)
  pos = greater_equal(val, ir.constant_float.get(sca_ty, 0), builder)
  neg = less_than(val, ir.constant_float.get(sca_ty, 0), builder)
  pos_ret = core.block(builder.create_atomic_rmw(ir.ATOMIC_OP.MIN, 
                                                 i_ptr.handle,
                                                 i_val.handle,
                                                 and_(mask, pos, builder).handle),
                       i_val.type)
  neg_ret = core.block(builder.create_atomic_rmw(ir.ATOMIC_OP.UMAX,
                                                 i_ptr.handle,
                                                 i_val.handle,
                                                 and_(mask, neg, builder).handle),
                       i_val.type)
  return where(pos, pos_ret, neg_ret, builder)

def atomic_add(ptr: core.block,
               val: core.block,
               mask: core.block,
               builder: ir.builder) -> core.block:
  ptr, val, mask = atom_red_typechecking_impl(ptr, val, mask, builder)
  sca_ty = val.type.scalar
  op = ir.ATOMIC_OP.FADD if sca_ty.is_floating() else ir.ATOMIC_OP.ADD
  return core.block(builder.create_atomic_rmw(op, ptr.handle, val.handle, mask.handle), val.type)

def atomic_and(ptr: core.block,
               val: core.block,
               mask: core.block, 
               builder: ir.builder) -> core.block:
  ptr, val, mask = atom_red_typechecking_impl(ptr, val, mask, builder)
  return core.block(builder.create_atomic_rmw(ir.ATOMIC_OP.AND, ptr.handle, val.handle, mask.handle), val.type)

def atomic_or(ptr: core.block,
              val: core.block,
              mask: core.block, 
              builder: ir.builder) -> core.block:
  ptr, val, mask = atom_red_typechecking_impl(ptr, val, mask, builder)
  return core.block(builder.create_atomic_rmw(ir.ATOMIC_OP.OR, ptr.handle, val.handle, mask.handle), val.type)

def atomic_xor(ptr: core.block,
               val: core.block,
               mask: core.block, 
               builder: ir.builder) -> core.block:
  ptr, val, mask = atom_red_typechecking_impl(ptr, val, mask, builder)
  return core.block(builder.create_atomic_rmw(ir.ATOMIC_OP.XOR, ptr.handle, val.handle, mask.handle), val.type)

def atomic_xchg(ptr: core.block,
                val: core.block,
                mask: core.block, 
                builder: ir.builder) -> core.block:
  ptr, val, mask = atom_red_typechecking_impl(ptr, val, mask, builder)
  return core.block(builder.create_atomic_rmw(ir.ATOMIC_OP.XCHG, ptr.handle, val.handle, mask.handle), val.type)

#===----------------------------------------------------------------------===//
#                               Linear Algebra
#===----------------------------------------------------------------------===//

def dot(lhs: core.block,
        rhs: core.block,
        allow_tf32: bool,
        builder: ir.builder) -> core.block:
  if lhs.type.is_int_or_tileint():
    _0 = builder.get_int32(0)
  else:
    _0 = builder.get_float32(0)
  M = lhs.type.shape[0]
  N = rhs.type.shape[1]
  _0 = builder.create_splat(_0, [M, N])
  _allow_tf32 = allow_tf32.value != 0
  ret_ty = core.block_type(lhs.type.scalar, [M, N])
  return core.block(builder.create_dot(lhs.handle, rhs.handle, _0, _allow_tf32),
                    ret_ty)


#===----------------------------------------------------------------------===//
#                               Indexing
#===----------------------------------------------------------------------===//

def where(condition: core.block,
          x: core.block,
          y: core.block,
          builder: core.block) -> core.block:
  condition = cast(condition, core.int1, builder)
  if condition.type.is_block():
    x = broadcast_impl_shape(x, condition.type.get_block_shapes(), builder)
    y = broadcast_impl_shape(y, condition.type.get_block_shapes(), builder)
  
  x_ty = x.type.scalar
  y_ty = y.type.scalar
  ty = computation_type_impl(x_ty, y_ty, div_or_mod=False)
  x = cast(x, ty, builder)
  y = cast(y, ty, builder)
  return core.block(builder.create_select(condition.handle, x.handle, y.handle), ty)


#===----------------------------------------------------------------------===//
#                               Reductions
#===----------------------------------------------------------------------===//

def reduce_impl(input: core.block, axis: int, builder: ir.builder, name: str, 
                FLOAT_OP: ir.REDUCE_OP, INT_OP: ir.REDUCE_OP) -> core.block:
  scalar_ty = input.type.scalar
  # input is extended to 32-bits if necessary
  # this increases numerical accuracy and can be done pretty much for free
  # on GPUs
  if scalar_ty.is_int() and scalar_ty.int_bitwidth <= 32:
    input = cast(input, core.int32, builder)
  if scalar_ty.is_floating():
    return builder.create_reduce(input, FLOAT_OP, axis)
  elif scalar_ty.is_int():
    return builder.create_reduce(input, INT_OP, axis)
  assert False

def min(input: core.block, axis: int, builder: ir.builder) -> core.block:
  return reduce_impl(input, axis, builder, "min", ir.REDUCE_OP.FMIN, ir.REDUCE_OP.MIN)

def max(input: core.block, axis: int, builder: ir.builder) -> core.block:
  return reduce_impl(input, axis, builder, "max", ir.REDUCE_OP.FMAX, ir.REDUCE_OP.MAX)

def sum(input: core.block, axis: int, builder: ir.builder) -> core.block:
  return reduce_impl(input, axis, builder, "sum", ir.REDUCE_OP.FADD, ir.REDUCE_OP.ADD)

def xor_sum(input: core.block, axis: int, builder: ir.builder) -> core.block:
  scalar_ty = input.type.scalar
  if not scalar_ty.is_int():
    raise ValueError("xor_sum only supported for integers")
  return reduce_impl(input, axis, builder, "sum", ir.REDUCE_OP.XOR, ir.REDUCE_OP.XOR)


#===----------------------------------------------------------------------===//
#                               Math
#===----------------------------------------------------------------------===//

def umulhi(x,  y, builder):
  binary_op_type_checking_impl(x, y, builder)
  return builder.insert(ir.umulhi_inst.create(x, y))

def exp(x: core.block, builder: ir.builder) -> core.block:
  return core.block(builder.create_exp(x.handle), x.type)

def log(x: core.block, builder: ir.builder) -> core.block:
  return core.block(builder.create_log(x.handle), x.type)

def cos(x: core.block, builder: ir.builder) -> core.block:
  return core.block(builder.create_cos(x.handle), x.type)

def sin(x: core.block, builder: ir.builder) -> core.block:
  return core.block(builder.create_sin(x.handle), x.type)

def sqrt(x: core.block, builder: ir.builder) -> core.block:
  return core.block(builder.create_sqrt(x.handle), x.type)


##

def multiple_of(x, value):
  i = x
  if not i:
    assert False
  i.set_metadata(ir.metadata.multiple_of, value)
  return i

def max_contiguous(x, value):
  i = x
  if not i:
    assert False
  i.set_metadata(ir.metadata.max_contiguous, value)
  return i

def debug_barrier(builder):
  return builder.create_barrier()


