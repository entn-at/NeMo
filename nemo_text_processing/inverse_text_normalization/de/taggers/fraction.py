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
from nemo_text_processing.text_normalization.en.graph_utils import (
    NEMO_NOT_QUOTE,
    GraphFst,
    convert_space,
    delete_space,
)

try:
    import pynini
    from pynini.lib import pynutil

    PYNINI_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    PYNINI_AVAILABLE = False


class FractionFst(GraphFst):
    """
    Finite state transducer for classifying fraction
        e.g. ein halb -> tokens { fraction { numerator: "1" denominator: "2" } }
        e.g. eineinhalb -> tokens { fraction { integer_part: "1" numerator: "1" denominator: "2" } }
        e.g. drei zwei hundertstel -> tokens { fraction { integer_part: "3" numerator: "2" denominator: "100" } }
    
    Args:
        cardinal: CardinalFst
    """

    def __init__(self, itn_cardinal_tagger: GraphFst, tn_fraction_verbalizer: GraphFst):
        super().__init__(name="fraction", kind="classify")
        # integer_part # numerator # denominator
        tagger = tn_fraction_verbalizer.graph.invert().optimize()

        delete_optional_sign = pynini.closure(pynutil.delete("negative: ") + pynini.cross("\"true\" ", "-"), 0, 1)
        delete_integer_marker = (
            pynutil.delete("integer_part: \"") + pynini.closure(NEMO_NOT_QUOTE, 1) + pynutil.delete("\"")
        ) @ itn_cardinal_tagger.graph_no_exception

        delete_numerator_marker = (
            pynutil.delete("numerator: \"") + pynini.closure(NEMO_NOT_QUOTE, 1) + pynutil.delete("\"")
        ) @ itn_cardinal_tagger.graph_no_exception

        delete_denominator_marker = (
            pynutil.insert('/')
            + (pynutil.delete("denominator: \"") + pynini.closure(NEMO_NOT_QUOTE, 1) + pynutil.delete("\""))
            @ itn_cardinal_tagger.graph_no_exception
        )

        graph = (
            pynini.closure(delete_integer_marker + pynini.accep(" "), 0, 1)
            + delete_numerator_marker
            + delete_space
            + delete_denominator_marker
        ).optimize()
        verbalizer = delete_optional_sign + graph

        self.graph = tagger @ verbalizer

        graph = pynutil.insert("name: \"") + convert_space(self.graph) + pynutil.insert("\"")
        self.fst = graph.optimize()
