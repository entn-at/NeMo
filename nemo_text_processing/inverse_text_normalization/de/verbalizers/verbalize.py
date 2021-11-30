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

from nemo_text_processing.inverse_text_normalization.de.graph_utils import GraphFst
from nemo_text_processing.inverse_text_normalization.de.taggers.cardinal import CardinalFst as CardinalTagger
from nemo_text_processing.inverse_text_normalization.de.verbalizers.cardinal import CardinalFst
from nemo_text_processing.inverse_text_normalization.de.verbalizers.decimal import DecimalFst
from nemo_text_processing.inverse_text_normalization.de.verbalizers.electronic import ElectronicFst
from nemo_text_processing.inverse_text_normalization.de.verbalizers.measure import MeasureFst
from nemo_text_processing.inverse_text_normalization.de.verbalizers.money import MoneyFst
from nemo_text_processing.inverse_text_normalization.de.verbalizers.telephone import TelephoneFst
from nemo_text_processing.inverse_text_normalization.de.verbalizers.time import TimeFst
from nemo_text_processing.inverse_text_normalization.de.verbalizers.whitelist import WhiteListFst
from nemo_text_processing.text_normalization.de.taggers.cardinal import CardinalFst as TNCardinalTagger
from nemo_text_processing.text_normalization.de.verbalizers.cardinal import CardinalFst as TNCardinalFst
from nemo_text_processing.text_normalization.de.verbalizers.decimal import DecimalFst as TNDecimalFst


class VerbalizeFst(GraphFst):
    """
    Composes other verbalizer grammars.
    For deployment, this grammar will be compiled and exported to OpenFst Finate State Archiv (FAR) File. 
    More details to deployment at NeMo/tools/text_processing_deployment.
    """

    def __init__(self):
        super().__init__(name="verbalize", kind="verbalize")
        tn_cardinal_verbalizer = TNCardinalFst(deterministic=False)
        tn_decimal_verbalizer = TNDecimalFst(deterministic=False)
        tn_cardinal_tagger = TNCardinalTagger(deterministic=False)
        cardinal_tagger = CardinalTagger(tn_cardinal=tn_cardinal_tagger)

        cardinal = CardinalFst(tn_cardinal=tn_cardinal_verbalizer)
        cardinal_graph = cardinal.fst
        decimal = DecimalFst(tn_decimal=tn_decimal_verbalizer)
        decimal_graph = decimal.fst
        measure_graph = MeasureFst(decimal=decimal, cardinal=cardinal).fst
        money_graph = MoneyFst(decimal=decimal).fst
        time_graph = TimeFst().fst
        whitelist_graph = WhiteListFst().fst
        telephone_graph = TelephoneFst().fst
        electronic_graph = ElectronicFst().fst
        graph = (
            time_graph
            | money_graph
            | measure_graph
            # | fraction_graph
            | decimal_graph
            | cardinal_graph
            | whitelist_graph
            | telephone_graph
            | electronic_graph
        )
        self.fst = graph
