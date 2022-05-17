# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import torch
from torch import nn
from torch.autograd import Function
import torch.nn.functional as F

import lightconv_cuda
from fairseq import utils

from fairseq.modules import LinearSuper

from .quantize import quantize, quantize_grad, QuantMeasure, calculate_qparams

class lightconvFunction(Function):

    @staticmethod
    def forward(ctx, x, weights, padding_l):
        ctx.padding_l = padding_l
        # print('lightconv: ', x.shape, weights.shape, padding_l)
        outputs = lightconv_cuda.forward(x, weights, padding_l)
        variables = [x, weights]
        ctx.save_for_backward(*variables)
        return outputs[0]

    @staticmethod
    def backward(ctx, grad_output):
        outputs = lightconv_cuda.backward(
                grad_output.contiguous(),
                ctx.padding_l,
                *ctx.saved_variables)
        grad_input, grad_weights = outputs
        return grad_input, grad_weights, None


class LightconvLayer(nn.Module):
    def __init__(
            self,
            input_size,
            kernel_size=1,
            padding_l=None,
            weight_softmax=False,
            num_heads=1,
            weight_dropout=0.,
            bias=False,
            with_linear=False,
            out_dim=None,
            num_bits=8,
            num_bits_grad=8):
        super(LightconvLayer, self).__init__()
        self.embed_dim = input_size
        self.input_size = input_size
        self.kernel_size = kernel_size
        self.padding_l = padding_l
        self.num_heads = num_heads
        self.weight_softmax = weight_softmax
        self.weight_dropout = weight_dropout
        self.num_bits = num_bits
        self.num_bits_grad = num_bits_grad
        out_dim = input_size if out_dim is None else out_dim

        self.weight = nn.Parameter(torch.Tensor(num_heads, kernel_size))
        if bias:
            self.bias = nn.Parameter(torch.Tensor(input_size))
        else:
            self.bias = None

        # print(input_size, out_dim)

        # self.linear1 = Linear(input_size, input_size) if with_linear else None
        # self.linear2 = Linear(input_size, out_dim) if with_linear else None

        if with_linear:
            self.linear1 = LinearSuper(super_in_dim=input_size, super_out_dim=input_size, bias=True)
            self.linear2 = LinearSuper(super_in_dim=input_size, super_out_dim=out_dim, bias=True)
        else:
            self.linear1 = self.linear2 = None

        self.with_linear = with_linear

        self.quantize_input = QuantMeasure(shape_measure=(1, 1), flatten_dims=(1, -1), momentum=0.1)

        self.reset_parameters()

    def set_sample_config(self, sample_in_dim, sample_out_dim):
        self.sample_in_dim = sample_in_dim
        self.sample_out_dim = sample_out_dim

        if self.with_linear:
            self.linear1.set_sample_config(sample_in_dim=sample_in_dim, sample_out_dim=sample_out_dim)
            self.linear2.set_sample_config(sample_in_dim=sample_in_dim, sample_out_dim=sample_out_dim)

    def calc_sampled_param_num(self):
        weight_numel = self.weight.numel()

        if self.bias is not None:
            bias_numel = self.bias.numel()
        else:
            bias_numel = 0

        return weight_numel + bias_numel

    def reset_parameters(self):
        nn.init.xavier_uniform_(self.weight)
        if self.bias is not None:
            nn.init.constant_(self.bias, 0.)

    def forward(self, x, incremental_state=None, num_bits=-1, num_bits_grad=-1):
        # print('x shape: ', x.shape)
        if self.linear1 is not None:
            x = self.linear1(x, num_bits=num_bits, num_bits_grad=num_bits_grad)

        # during inference time, incremental BMM is faster
        if incremental_state is not None:
            T, B, C = x.size()
            K, H = self.kernel_size, self.num_heads
            R = C // H
            input_buffer = self._get_input_buffer(incremental_state)
            if input_buffer is None:
                input_buffer = x.new()
            x_unfold = torch.cat([input_buffer, x.unsqueeze(3)], dim=3)
            if self.kernel_size > 1:
                self._set_input_buffer(incremental_state, x_unfold[:, :, :, -self.kernel_size+1:])
            x_unfold = x_unfold.view(T*B*H, R, -1)

            weight = self.weight
            if self.weight_softmax:
                weight = F.softmax(weight.float(), dim=1).type_as(weight)

            weight = weight[:, -x_unfold.size(2):]

            K = weight.size(1)

            weight = weight.view(1, H, K).expand(T*B, H, K).contiguous().view(T*B*H, K, 1)

            weight = F.dropout(weight, self.weight_dropout, training=self.training)
            output = torch.bmm(x_unfold, weight)  # T*B*H x R x 1
            output = output.view(T, B, C)
            if self.linear2 is not None:
                output = self.linear2(output, num_bits=num_bits, num_bits_grad=num_bits_grad)

        # during training time, use CUDA kernel
        else:
            x = x.permute(1, 2, 0).contiguous()
            weight = self.weight
            if self.weight_softmax:
                weight = F.softmax(self.weight, -1)
            if self.weight_dropout:
                weight = F.dropout(weight, self.weight_dropout, training=self.training)

            # output = lightconvFunction.apply(x, weight, self.padding_l).permute(2, 0, 1)

            # quantize input and weight
            if num_bits > 0:
                qx = self.quantize_input(x, num_bits=num_bits)
                weight_qparams = calculate_qparams(weight, num_bits=num_bits, flatten_dims=(1, -1), reduce_dim=None)
                qweight = quantize(weight, qparams=weight_qparams)
                output = lightconvFunction.apply(qx, qweight, self.padding_l).permute(2, 0, 1)
            else:
                output = lightconvFunction.apply(x, weight, self.padding_l).permute(2, 0, 1)

            if num_bits_grad > 0:
                output = quantize_grad(output, num_bits=num_bits_grad)

            if self.linear2 is not None:
                output = self.linear2(output, num_bits=num_bits, num_bits_grad=num_bits_grad)

        return output

    def reorder_incremental_state(self, incremental_state, new_order):
        input_buffer = self._get_input_buffer(incremental_state)
        if input_buffer is not None:
            input_buffer = input_buffer.index_select(1, new_order)
            self._set_input_buffer(incremental_state, input_buffer)

    def _get_input_buffer(self, incremental_state):
        return utils.get_incremental_state(self, incremental_state, 'input_buffer')

    def _set_input_buffer(self, incremental_state, new_buffer):
        return utils.set_incremental_state(self, incremental_state, 'input_buffer', new_buffer)

    def half(self):
        print("HALF")
        return self._apply(lambda t: t.half() if t.is_floating_point() else t)


def Linear(in_features, out_features, bias=True):
    m = nn.Linear(in_features, out_features, bias)
    nn.init.xavier_uniform_(m.weight)
    if bias:
        nn.init.constant_(m.bias, 0.)
    return m