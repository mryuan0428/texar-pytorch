# Copyright 2019 The Texar Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""RNN helpers for PyTorch models."""

import torch

from texar.utils.shapes import mask_sequences


def dynamic_rnn(cell, inputs, sequence_length=None, initial_state=None,
                time_major=False):
    """Creates a recurrent neural network specified by RNNCell `cell`.

    Performs fully dynamic unrolling of `inputs`.

      Args:
        cell: An instance of RNNCell.
        inputs: The RNN inputs.
          If `time_major == False` (default), this must be a `Tensor` of shape:
            `[batch_size, max_time, ...]`, or a nested tuple of such
            elements.
          If `time_major == True`, this must be a `Tensor` of shape:
            `[max_time, batch_size, ...]`, or a nested tuple of such
            elements.
          This may also be a (possibly nested) tuple of Tensors satisfying
          this property.  The first two dimensions must match across all the
          inputs, but otherwise the ranks and other shape components may differ.
          In this case, input to `cell` at each time-step will replicate the
          structure of these tuples, except for the time dimension (from which
          the time is taken).
          The input to `cell` at each time step will be a `Tensor` or (possibly
          nested) tuple of Tensors each with dimensions `[batch_size, ...]`.
        sequence_length: (optional) An int32/int64 vector sized `[batch_size]`.
          Used to copy-through state and zero-out outputs when past a batch
          element's sequence length.  So it's more for performance than
          correctness.
        initial_state: (optional) An initial state for the RNN.
          If `cell.state_size` is an integer, this must be
          a `Tensor` of appropriate type and shape
          `[batch_size, cell.state_size]`. If `cell.state_size` is a tuple,
          this should be a tuple of tensors having shapes `[batch_size, s] for
          s in cell.state_size`.
        time_major: The shape format of the `inputs` and `outputs` Tensors.
          If true, these `Tensors` must be shaped
          `[max_time, batch_size, depth]`. If false, these `Tensors` must be
          shaped `[batch_size, max_time, depth]`.
          Using `time_major = True` is a bit more efficient because it avoids
          transposes at the beginning and end of the RNN calculation.  However,
          most TensorFlow data is batch-major, so by default this function
          accepts input and emits output in batch-major form.

      Returns:
        A pair (outputs, state) where:

        outputs: The RNN output `Tensor`.

          If time_major == False (default), this will be a `Tensor` shaped:
            `[batch_size, max_time, cell.output_size]`.

          If time_major == True, this will be a `Tensor` shaped:
            `[max_time, batch_size, cell.output_size]`.

          Note, if `cell.output_size` is a (possibly nested) tuple of integers
          or `TensorShape` objects, then `outputs` will be a tuple having the
          same structure as `cell.output_size`, containing Tensors having shapes
          corresponding to the shape data in `cell.output_size`.

        state: The final state.  If `cell.state_size` is an int, this
          will be shaped `[batch_size, cell.state_size]`.  If it is a
          `TensorShape`, this will be shaped `[batch_size] + cell.state_size`.
          If it is a (possibly nested) tuple of ints or `TensorShape`, this will
          be a tuple having the corresponding shapes. If cells are `LSTMCells`
          `state` will be a tuple containing a `LSTMStateTuple` for each cell.

      Raises:
        TypeError: If `cell` is not an instance of RNNCell.
        ValueError: If inputs is None or an empty list.
      """
    # By default, time_major==False and inputs are batch-major: shaped
    #   [batch, time, depth]
    # For internal calculations, we transpose to [time, batch, depth]
    if not time_major:
        # (B,T,D) => (T,B,D)
        inputs = inputs.permute(1, 0, 2)

    time_steps = inputs.shape[0]
    batch_size = inputs.shape[1]

    if sequence_length is not None:
        if not isinstance(sequence_length, torch.Tensor):
            sequence_length = torch.tensor(sequence_length)

        sequence_length = sequence_length.type(torch.int32)
        if sequence_length.dim() != 1:
            raise ValueError(
                "sequence_length must be a vector of length batch_size, "
                "but saw shape: %s" % sequence_length.shape)
        if sequence_length.shape != torch.Size([batch_size]):
            raise ValueError("Expected shape for Tensor sequence_length is %s"
                             % batch_size, " but saw shape: %s"
                             % sequence_length.shape)
    else:
        sequence_length = torch.tensor([time_steps]*batch_size)

    if initial_state is not None:
        state = initial_state
    else:
        state = cell.zero_state(batch_size=batch_size)

    (outputs, final_state) = _dynamic_rnn_loop(cell,
                                               inputs,
                                               state,
                                               sequence_length=sequence_length)

    # Outputs of _dynamic_rnn_loop are always shaped [time, batch, depth].
    # If we are performing batch-major calculations, transpose output back
    # to shape [batch, time, depth]
    if not time_major:
        # (T,B,D) => (B,T,D)
        outputs = outputs.permute(1, 0, 2)

    return outputs, final_state


def _dynamic_rnn_loop(cell,
                      inputs,
                      initial_state,
                      sequence_length=None):

    state = initial_state
    time_steps = inputs.shape[0]

    is_tuple = isinstance(state, tuple)

    all_outputs = []
    if is_tuple:
        all_state = ([], [])
    else:
        all_state = []

    for i in range(time_steps):
        output, state = cell(inputs[i], state)
        all_outputs.append(output)
        if is_tuple:
            all_state[0].append(state[0])
            all_state[1].append(state[1])
        else:
            all_state.append(state)

    # TODO: Do not compute everything regardless of sequence_length

    final_outputs = torch.stack(all_outputs, dim=0)
    final_outputs = mask_sequences(final_outputs,
                                   sequence_length=sequence_length,
                                   time_major=True)
    if is_tuple:
        final_state = ([], [])
    else:
        final_state = []

    for batch_idx, time_idx in enumerate(sequence_length.tolist()):
        if time_idx > 0:
            if is_tuple:
                final_state[0].append(all_state[0][time_idx-1][batch_idx])
                final_state[1].append(all_state[1][time_idx-1][batch_idx])
            else:
                final_state.append(all_state[time_idx-1][batch_idx])
        else:
            if is_tuple:
                final_state[0].append(initial_state[0][batch_idx])
                final_state[1].append(initial_state[1][batch_idx])
            else:
                final_state.append(initial_state[batch_idx])

    if is_tuple:
        final_state = (torch.stack(final_state[0], dim=0),
                       torch.stack(final_state[1], dim=0))
    else:
        final_state = torch.stack(final_state, dim=0)

    return final_outputs, final_state
