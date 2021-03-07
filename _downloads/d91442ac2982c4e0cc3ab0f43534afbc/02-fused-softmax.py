"""
Fused Softmax
=================
In this tutorial, you will write a fused softmax operation (that outperforms PyTorch) and learn about:

- The benefits of kernel fusion for bandwidth-bound operations.
- The syntax and usage of reduction operators in Triton.
- The automatic vectorization capabilities of the Triton compiler.
"""

# %%
# Motivations
# ------------
# Custom GPU kernels for elementwise additions are educationally valuable but won't get you very far in practice.
# Let us consider instead the case of a simple (numerically stabilized) softmax operation:

import torch


# Compute the row-wise softmax of x
def naive_softmax(x):
    # read  MN elements ; write M  elements
    x_max = torch.max(x, axis=1)[0]
    # read 2MN elements ; write MN elements
    z = x - x_max[:, None]
    # read  MN elements ; write MN elements
    numerator = torch.exp(x)
    # read  MN elements ; write M  elements
    denominator = torch.sum(numerator, axis=1)
    # read 2MN elements ; write MN elements
    ret = numerator / denominator[:, None]
    # in total: read 7MN elements ; wrote 3MN + 2M elements
    return ret


# %%
# When implemented naively in pytorch, computing :code:`y = naive_softmax(x)` for :math:`x \in R^{M \times N}` requires reading :math:`7MN` elements from DRAM and writing back :math:`3MN + 2M` elements.
# This is obviously wasteful; we'd prefer to have a custom "fused" kernel that only reads X once and does all the necessary computations on-chip.
# In this case, we would be reading and writing back only :math:`MN` bytes, so we could expect a theoretical speed-up of ~5x (i.e., :math:`(10MN + 2M) / 2MN`).
# In practice, though, we would be getting a bit less as our kernel computes exponentials and internally moves data around in shared memory.

# %%
# Compute Kernel
# ----------------
# Our softmax kernel works as follows: each program loads a row of the input X, normalizes it and writes back the result to the output Y.
# Note that one important limitation of Triton is that each block must have a power-of-two number of elements,
# so we need to internally "pad" tiles and guard the memory operations properly if we want to handle any possible input shapes:
#
#  .. code-block:: C
#
#    __global__ void softmax(float* Y, float* X, int stride_xm, int stride_ym, int M, int N){
#      // row index
#      int    m             = get_program_id(0);
#      // column indices
#      int    n    [BLOCK] = 0 ... BLOCK;
#      // the memory address of all the elements
#      // that we want to load can be computed as follows
#      float* px   [BLOCK] = X + m*stride_xm + n;
#      // because BLOCK has to be a power of two
#      // (per Triton-C specs), it is important
#      // to guard each memory operation with predicates
#      // or we will read out of bounds
#      bool   check[BLOCK] = n < N;
#      float  x    [BLOCK] = check ? *px : -F32_INFINITY;
#      // syntax for reduction in Triton is:
#      // x[:, :, OPERATOR, :, :]
#      //            ^
#      //           index
#      // where operator is in {min, max, +}
#      // for 1D vectors, this is just x[OPERATOR].
#      float  z    [BLOCK] = x - x[max];
#      // Note that exponentials in Triton are fast
#      // but approximate (i.e., think __expf in CUDA)
#      float  num  [BLOCK] = exp(z);
#      float  denom         = num[+];
#      // The result of the reduction is now stored in y
#      float  y    [BLOCK] = num / denom;
#      // We write it back
#      float* py   [BLOCK] = Y + m*stride_ym + n;
#      *?(check)py = y;
#    }

# %%
# Torch Bindings
# ---------------
# Here our torch bindings is quite similar to that of the vector addition mentioned in the previous tutorial.
# We just need to make sure that BLOCK is the smallest power of two greater than the number of columns N of the input matrix.
# This means that different values of BLOCK will result in different kernels

import torch
import triton

# Source code for the Triton kernel
_src = """
__global__ void softmax(float* Y, float* X, int stride_ym, int stride_xm, int M, int N){
    int    m             = get_program_id(0);
    int    n    [BLOCK] = 0 ... BLOCK;
    float* px   [BLOCK] = X + m*stride_xm + n;
    bool   check[BLOCK] = n < N;
    float  x    [BLOCK] = check ? *px : -F32_INFINITY;
    float  z    [BLOCK] = x - x[max];
    float  num  [BLOCK] = exp(z);
    float  denom        = num[+];
    float  y    [BLOCK] = num / denom;
    float* py   [BLOCK] = Y + m*stride_ym + n;
    *?(check)py = y; 
}
"""


# helper function to get the smaller power-of-two larger than a given number
def next_power_of_2(n):
    n -= 1
    n |= n >> 1
    n |= n >> 2
    n |= n >> 4
    n |= n >> 8
    n |= n >> 16
    n += 1
    return n


# kernel caching mechanism
def make_kernel(N, device):
    cache = make_kernel.cache
    # Now are kernels are indexed not only by the provided device but also
    # by the rounded number of columns in the input matrix
    BLOCK = next_power_of_2(N)
    key = (BLOCK, device)
    if key not in cache:
        defines = {'BLOCK': BLOCK}
        cache[key] = triton.kernel(_src, device=device, defines=defines)
    return cache[key]


make_kernel.cache = dict()


class _softmax(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        # constraints of the op
        assert x.dtype == torch.float32
        y = torch.empty_like(x)
        # The launch grid is simple: we have one kernel instance per row of the input matrix
        M, N = y.shape
        grid = lambda opt: (M, )
        # Launch kernel
        kernel = make_kernel(N, y.device)
        kernel(y.data_ptr(), x.data_ptr(), y.stride(0), x.stride(0), M, N, grid=grid)
        return y


softmax = _softmax.apply

# %%
# We can use the above softmax function to compute the row-wise softmax of a given matrix.

# %%
# Unit Test
# ----------

# %%
# We make sure that we test our kernel on a matrix with an irregular number of rows and columns.
# This will allow us to verify that our padding mechanism works.

torch.manual_seed(0)
x = torch.randn(1823, 781, device='cuda')
y_tri = softmax(x)
y_ref = torch.softmax(x, axis=1)
print(torch.allclose(y_tri, y_ref))

#%%
# As expected, the results are identical.

# %%
# Benchmarking
# -------------
# Here we will benchmark our operation as a function of the number of columns in the input matrix -- assuming 4096 rows.
# We will then compare its performance against (1) :code:`torch.softmax` and (2) the :code:`naive_softmax` defined above.

import matplotlib.pyplot as plt

M = 4096
Ns = [256 * i for i in range(2, 50)]
tri_bw = []
ref_bw = []
def_bw = []
for N in Ns:
    x = torch.randn(M, N, device='cuda', dtype=torch.float32)
    gbps = lambda ms: x.nelement() * x.element_size() * 1e-9 / (ms * 1e-3)
    do_bench = lambda fn: gbps(triton.testing.do_bench(fn, warmup=10, rep=100, clear_l2=True))
    tri_bw += [do_bench(lambda: softmax(x))]
    ref_bw += [do_bench(lambda: torch.softmax(x, axis=1))]
    def_bw += [do_bench(lambda: naive_softmax(x))]
plt.xlabel('N')
plt.ylabel('Bandwidth (GB/s)')
plt.plot(Ns, tri_bw, label='Triton')
plt.plot(Ns, ref_bw, label='Torch')
plt.plot(Ns, def_bw, label='Naive')
plt.legend()
plt.show()

# %%
# In the above plot, we can see that:
#
#  - Triton is 4-5x faster than the naive implementation, which is consistent with our theoretical predictions.
#  - Triton is significantly faster than :code:`torch.softmax` for very large input matrices. My guess from looking at the source-code of the `PyTorch kernel <https://github.com/pytorch/pytorch/blob/9409a3a39b7149bb2d833a89e0c944109bef7c27/caffe2/operators/softmax_ops.cu#L240>`_ is that PyTorch only partially fuses the computation of the softmax.
#    This means that -- when temporary data is too large to fit entirely in the GPU's cache -- it transfers almost twice the amount of data necessary.
#    Note that our Triton kernel is not only faster than PyTorch's CUDA kernel, it is also **easier to read, understand and maintain**.