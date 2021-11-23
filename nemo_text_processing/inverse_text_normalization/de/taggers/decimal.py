# Copyright (c) 2021, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from nemo_text_processing.inverse_text_normalization.de.utils import get_abs_path
from nemo_text_processing.text_normalization.de.taggers.decimal import get_quantity
from nemo_text_processing.text_normalization.en.graph_utils import (
    NEMO_DIGIT,
    GraphFst,
    delete_extra_space,
    delete_space,
)

try:
    import pynini
    from pynini.lib import pynutil

    PYNINI_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    PYNINI_AVAILABLE = False


class DecimalFst(GraphFst):
    """
    Finite state transducer for classifying decimal
        e.g. minus elf komma zwei null null sechs billionen -> decimal { negative: "true" integer_part: "11"  fractional_part: "2006" quantity: "billionen" }
        e.g. eine billion -> decimal { integer_part: "1" quantity: "billion" }
    Args:
        cardinal: CardinalFst
    """

    def __init__(self, cardinal: GraphFst, tn_decimal: GraphFst, deterministic: bool = True):
        super().__init__(name="decimal", kind="classify")

        cardinal_graph = cardinal.graph_no_exception
        self.graph = tn_decimal.graph.invert().optimize()

        delete_point = pynutil.delete(" komma")

        graph_fractional = pynutil.insert("fractional_part: \"") + self.graph + pynutil.insert("\"")
        graph_integer = pynutil.insert("integer_part: \"") + cardinal.graph_no_exception + pynutil.insert("\"")
        final_graph_wo_sign = graph_integer + delete_point + pynini.accep(" ") + graph_fractional

        self.final_graph_wo_negative = (
            final_graph_wo_sign
            | get_quantity(final_graph_wo_sign, cardinal.graph_hundred_component_at_least_one_none_zero_digit)
        ).optimize()
        final_graph = cardinal.optional_minus_graph + self.final_graph_wo_negative
        final_graph += pynutil.insert(" preserve_order: true")
        final_graph = self.add_tokens(final_graph)
        self.fst = final_graph.optimize()
