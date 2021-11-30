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

from nemo_text_processing.text_normalization.de.taggers.measure import singular_to_plural, unit_singular
from nemo_text_processing.text_normalization.en.graph_utils import (
    NEMO_SIGMA,
    GraphFst,
    convert_space,
    delete_extra_space,
    delete_space,
)

try:
    import pynini
    from pynini.lib import pynutil

    PYNINI_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    PYNINI_AVAILABLE = False


class MeasureFst(GraphFst):
    """
    Finite state transducer for classifying measure. Allows for plural form for unit.
        e.g. minus elf kilogramm -> measure { negative: "true" cardinal { integer: "11" } units: "kg" }
        e.g. drei stunden -> measure { cardinal { integer: "3" } units: "h" }
        e.g. ein halb kilogramm -> measure { fraction { numerator: "1" denominator: "2" } units: "h" }

    Args:
        cardinal: CardinalFst
        decimal: DecimalFst
        fraction: FractionFst
    """

    def __init__(self, cardinal: GraphFst, decimal: GraphFst, fraction: GraphFst):
        super().__init__(name="measure", kind="classify")

        cardinal_graph = (
            pynini.cdrewrite(pynini.cross(pynini.union("ein", "eine"), "eins"), "[BOS]", "[EOS]", NEMO_SIGMA)
            @ cardinal.graph_no_exception
        )

        graph_unit_singular = pynini.invert(unit_singular)  # singular -> abbr
        unit = (pynini.invert(singular_to_plural()) @ graph_unit_singular) | graph_unit_singular  # plural -> abbr
        unit = convert_space(unit)
        graph_unit_singular = convert_space(graph_unit_singular)

        optional_graph_negative = pynini.closure(
            pynutil.insert("negative: ") + pynini.cross("minus", "\"true\"") + delete_extra_space, 0, 1
        )

        unit_misc = pynutil.insert("/") + pynutil.delete("pro") + delete_space + graph_unit_singular

        unit = (
            pynutil.insert("units: \"")
            + (unit | unit_misc | pynutil.add_weight(unit + delete_space + unit_misc, 0.01))
            + pynutil.insert("\"")
        )

        subgraph_decimal = (
            pynutil.insert("decimal { ")
            + optional_graph_negative
            + decimal.final_graph_wo_negative
            + pynutil.insert(" }")
            + delete_extra_space
            + unit
        )

        subgraph_fraction = (
            pynutil.insert("decimal { ")
            + optional_graph_negative
            + pynutil.insert("integer_part: \"")
            + fraction.graph
            + pynutil.insert("\" }")
            + delete_extra_space
            + unit
        )

        subgraph_cardinal = (
            pynutil.insert("cardinal { ")
            + optional_graph_negative
            + pynutil.insert("integer: \"")
            + cardinal_graph
            + pynutil.insert("\"")
            + pynutil.insert(" }")
            + delete_extra_space
            + unit
        )
        final_graph = subgraph_cardinal | subgraph_decimal | subgraph_fraction
        final_graph = self.add_tokens(final_graph)
        self.fst = final_graph.optimize()
