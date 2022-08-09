# Copyright 2022 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""
RNN Decoder modules
"""
# pylint: disable=abstract-method

import mindspore.nn as nn
import mindspore.ops as ops
import mindspore.numpy as mnp

from text.abc import DecoderBase
from text.modules import DotAttention


class RNNDecoder(DecoderBase):
    r"""
    RNN Decoder.

    Apply RNN layer with :math:`\tanh` or :math:`\text{ReLU}` non-linearity to the input.

    For each element in the input sequence, each layer computes the following function:

    .. math::
        h_t = activation(W_{ih} x_t + b_{ih} + W_{hh} h_{(t-1)} + b_{hh})

    Here :math:`h_t` is the hidden state at time `t`, :math:`x_t` is
    the input at time `t`, and :math:`h_{(t-1)}` is the hidden state of the
    previous layer at time `t-1` or the initial hidden state at time `0`.
    If :attr:`nonlinearity` is ``'relu'``, then :math:`\text{ReLU}` is used instead of :math:`\tanh`.

    Args:
        vocab_size (int): Size of the dictionary of embeddings.
        embedding_size (int): The size of each embedding vector.
        hidden_size (int):  Number of features of hidden layer.
        num_layers (int): Number of layers of stacked LSTM . Default: 1.
        has_bias (bool): Whether the cell has bias `b_ih` and `b_hh`. Default: True.
        dropout (float, int): If not 0, append `Dropout` layer on the outputs of each
            LSTM layer except the last layer. Default 0. The range of dropout is [0.0, 1.0).
        attention (bool): Whether to use attention. Default: True.
        encoder_output_units (int): Number of features of encoder output.

    Inputs:
        - **prev_output_tokens** (Tensor) - Output tokens for teacher forcing with shape [batch, tgt_len].
        - **encoder_out** (Tensor) - Output of encoder.

    Outputs:
        Tuple, a tuple contains (`output`, `attn_scores`).

        - **output** (Tensor) - Tensor of shape (batch, `tgt_len`, `vocab_size`).
        - **attn_scores** (Tensor) - Tensor of shape (`tgt_len`, batch, `embedding_size`)
            if attention=True otherwise None.

    Examples:
        >>> import numpy as np
        >>> import mindspore
        >>> from mindspore import Tensor
        >>> from text.modules import RNNDecoder
        >>> rnn_decoder = RNNDecoder(1000, 32, 16, num_layers=2, has_bias=True,
        ...                          dropout=0.1, attention=True, encoder_output_units=16)
        >>> tgt_tokens = Tensor(np.ones([8, 16]), mindspore.int32)
        >>> encoder_output = Tensor(np.ones([8, 16, 16]), mindspore.int32)
        >>> hiddens_n = Tensor(np.ones([2, 8, 16]), mindspore.float32)
        >>> mask = Tensor(np.ones([8, 16]), mindspore.int32)
        >>> output, attn_scores = rnn_decoder(tgt_tokens, (encoder_output, hiddens_n, mask))
        >>> print(output.shape)
        >>> print(attn_scores.shape)
        (8, 16, 1000)
        (8, 16, 16)
    """

    def __init__(self, vocab_size, embedding_size, hidden_size, num_layers=1, has_bias=True,
                 dropout=0, attention=True, encoder_output_units=512):
        super().__init__()

        self.embedding = nn.Embedding(vocab_size, embedding_size)
        self.dropout = nn.Dropout(1 - dropout)
        self.rnn = nn.RNN(embedding_size, hidden_size, num_layers=num_layers, has_bias=has_bias,
                          batch_first=True, dropout=dropout)
        if attention:
            self.attention = DotAttention()
            self.input_proj = nn.Dense(hidden_size, encoder_output_units)
            self.output_proj = nn.Dense(hidden_size + encoder_output_units, hidden_size)
        else:
            self.attention = None
        self.fc_out = nn.Dense(hidden_size, vocab_size)

    def construct(self, prev_output_tokens, encoder_out=None):
        output, attn_scores = self.extract_features(prev_output_tokens, encoder_out)
        output = self.output_layer(output)
        return output, attn_scores

    def _attention_layer(self, hidden, encoder_output, mask):
        """
        Attention method
        """
        # hidden: [batch, hidden_size]
        # encoder_output: [batch, src_len, encoder_output_units]
        # mask: [batch, src_len]
        src_len = encoder_output.shape[1]
        query = self.input_proj(hidden)  # [batch, encoder_output_units]
        query = mnp.tile(ops.expand_dims(query, 1), (1, src_len, 1))  # [batch, src_len, encoder_output_units]
        attn_mask = mnp.tile(ops.expand_dims(mask, 1), (1, src_len, 1))  # [batch, src_len, src_len]

        # output: [batch, src_len, encoder_output_units]
        # attn_scores: [batch, src_len, src_len]
        output, attn_scores = self.attention(query, encoder_output, encoder_output, attn_mask)

        attn_scores = ops.reduce_sum(attn_scores, 1)  # [batch, src_len]
        output = ops.reduce_sum(output, 1)  # [batch, encoder_output_units]
        output = self.output_proj(ops.concat((hidden, output), axis=1))  # [batch, hidden_size]

        return output, attn_scores

    def extract_features(self, prev_output_tokens, encoder_out=None):
        """
        Extract features of encoder output
        """
        # get output from encoder
        if encoder_out is not None:
            encoder_output = encoder_out[0]  # [batch_size, src_len, num_directions * hidden_size]
            encoder_hiddens = encoder_out[1]  # [num_directions * num_layers, batch_size, hidden_size]
            encoder_padding_mask = encoder_out[2]  # [batch, src_len]
        else:
            encoder_output = mnp.empty(0)
            encoder_hiddens = mnp.empty(0)
            encoder_padding_mask = mnp.empty(0)
        tgt_len = prev_output_tokens.shape[1]

        # embed the target tokens
        embed_token = self.embedding(prev_output_tokens)  # [batch, tgt_len, embedding_size]
        embed_token = self.dropout(embed_token)

        output, _ = self.rnn(embed_token, encoder_hiddens)  # [batch, tgt_len, hidden_size]

        if self.attention is not None:
            outs = []
            attns = []
            for hidden in ops.split(output, axis=1, output_num=tgt_len):  # [batch, 1, hidden_size]
                # out: [batch, hidden_size]
                # scores: [batch, src_len]
                out, scores = self._attention_layer(ops.squeeze(hidden), encoder_output, encoder_padding_mask)
                outs.append(out)
                attns.append(scores)
            output = ops.stack(outs, 1)  # [batch, tgt_len, hidden_size]
            attn_scores = ops.stack(attns, 2)  # [batch, src_len, tgt_len]

        else:
            attn_scores = None

        return output, attn_scores

    def output_layer(self, features):
        """
        Project features to the vocabulary size
        """
        output = self.fc_out(features)  # [batch, tgt_len, vocab_size]
        return output
