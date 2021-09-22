#!/usr/bin/env python
# coding: utf-8

import time
import numpy as np
import pyaudio as pa
import os, time
import nemo
import nemo.collections.asr as nemo_asr
import soundfile as sf
from pyannote.metrics.diarization import DiarizationErrorRate

from scipy.io import wavfile
from scipy.optimize import linear_sum_assignment
import librosa
import ipdb
import datetime
from datetime import datetime as datetime_sub

### From speaker_diarize.py
from omegaconf import OmegaConf
from pytorch_lightning import seed_everything

from nemo.collections.asr.models.classification_models import EncDecClassificationModel
from nemo.collections.asr.models.label_models import ExtractSpeakerEmbeddingsModel
from nemo.collections.asr.parts.mixins.mixins import DiarizationMixin
# from nemo.collections.asr.data.audio_to_label import get_segments_from_slices
from nemo.collections.asr.data.audio_to_label import repeat_signal
from nemo.collections.asr.parts.utils.diarization_utils import ASR_DIAR_OFFLINE, write_txt, WER_TS
from nemo.collections.asr.parts.utils.speaker_utils import audio_rttm_map, perform_diarization, write_rttm2manifest, get_DER
from nemo.collections.asr.parts.utils.speaker_utils import get_contiguous_stamps, merge_stamps, labels_to_pyannote_object, rttm_to_labels, labels_to_rttmfile
from nemo.collections.asr.parts.utils.vad_utils import (
    generate_overlap_vad_seq,
    generate_vad_segment_table,
    get_vad_stream_status,
    prepare_manifest,
)
from nemo.collections.asr.models import ClusteringDiarizer
from sklearn.preprocessing import OneHotEncoder
from nemo.collections.asr.parts.utils.nmse_clustering import (
# from nmse_clustering_enhanced import (
    NMESC,
    _SpectralClustering,
    getEnhancedSpeakerCount,
    COSclustering,
    getCosAffinityMatrix,
    getAffinityGraphMat,
    getLaplacian,
    getLamdaGaplist,
    eigDecompose,
)

from nemo.core.config import hydra_runner
from nemo.utils import logging
import hydra
from typing import List, Optional, Dict
from omegaconf import DictConfig, OmegaConf, open_dict
from omegaconf.listconfig import ListConfig
from omegaconf import OmegaConf
import copy
from nemo.core.classes import IterableDataset
from nemo.core.neural_types import NeuralType, AudioSignal, LengthsType
from nemo.utils import logging, model_utils
import torch
from torch.utils.data import DataLoader
import math

from collections import Counter
from functools import reduce
from sklearn.metrics.pairwise import cosine_similarity, linear_kernel
seed_everything(42)

def isOverlap(rangeA, rangeB):
    start1, end1 = rangeA
    start2, end2 = rangeB
    return end1 > start2 and end2 > start1

def getOverlapRange(rangeA, rangeB):
    assert isOverlap(rangeA, rangeB)
    return [ max(rangeA[0], rangeB[0]), min(rangeA[1], rangeB[1])]


def combine_overlaps(ranges):
    return reduce(
        lambda acc, el: acc[:-1:] + [(min(*acc[-1], *el), max(*acc[-1], *el))]
            if acc[-1][1] >= el[0] - 1
            else acc + [el],
        ranges[1::],
        ranges[0:1],
    )
def fl2int(x):
    return int(x*100)

def int2fl(x):
    return round(float(x/100.0), 2)

def getMergedSpeechLabel(label_list_A, label_list_B):
    if label_list_A == [] and label_list_B != []:
        return label_list_B
    elif label_list_A != [] and label_list_B == []:
        return label_list_A
    else:
        label_list_A = [ [fl2int(x[0]), fl2int(x[1])] for x in label_list_A] 
        label_list_B = [ [fl2int(x[0]), fl2int(x[1])] for x in label_list_B] 

        combined = combine_overlaps(label_list_A + label_list_B)

        return [ [int2fl(x[0]), int2fl(x[1])] for x in combined ]


def getSubRangeList(target_range: List[float], source_list: List) -> List:
    if target_range == []:
        return []

    out_range_list = []
    for s_range in source_list:
        if isOverlap(s_range, target_range):
            ovl_range = getOverlapRange(s_range, target_range)
            out_range_list.append(ovl_range)
    return out_range_list 

def getVADfromRTTM(rttm_fullpath):
    out_list = []
    with open(rttm_fullpath, 'r') as rttm_stamp:
        rttm_stamp_list = rttm_stamp.readlines()
        for line in rttm_stamp_list:
            stt = float(line.split()[3])
            end = float(line.split()[4]) + stt
            out_list.append([stt, end])
    return out_list


def infer_signal(model, signal):
    data_layer = AudioDataLayer(sample_rate=cfg.preprocessor.sample_rate)
    data_loader = DataLoader(data_layer, batch_size=1, collate_fn=data_layer.collate_fn)
    data_layer.set_signal(signal)
    batch = next(iter(data_loader))
    audio_signal, audio_signal_len = batch
    audio_signal, audio_signal_len = audio_signal.to(asr_model.device), audio_signal_len.to(asr_model.device)
    log_probs, encoded_len, predictions = model.forward(
        input_signal=audio_signal, input_signal_length=audio_signal_len
    )
    return log_probs

def get_partial_ref_labels(pred_labels, ref_labels):
    last_pred_time = float(pred_labels[-1].split()[1])
    ref_labels_out = []
    for label in ref_labels:
        start, end, speaker = label.split()
        start, end = float(start), float(end)
        if last_pred_time <= start:
            pass
        elif start < last_pred_time <= end:
            label = f"{start} {last_pred_time} {speaker}"
            ref_labels_out.append(label) 
        elif end < last_pred_time:
            ref_labels_out.append(label) 
    return ref_labels_out 

def read_wav(audio_file):
    with sf.SoundFile(audio_file, 'r') as f:
        sample_rate = f.samplerate
        samples = f.read(dtype='float32')
    samples = samples.transpose()
    return sample_rate, samples


def load_ASR_model(ASR_model_name):
    # Preserve a copy of the full config
    asr_model = nemo_asr.models.EncDecCTCModel.from_pretrained(ASR_model_name)
    cfg = copy.deepcopy(asr_model._cfg)
    print(OmegaConf.to_yaml(cfg))

    # Make config overwrite-able
    OmegaConf.set_struct(cfg.preprocessor, False)

    # some changes for streaming scenario
    cfg.preprocessor.dither = 0.0
    cfg.preprocessor.pad_to = 0
    # cfg.preprocessor.normalize = normalization

    # Disable config overwriting
    OmegaConf.set_struct(cfg.preprocessor, True)
    asr_model.preprocessor = asr_model.from_config_dict(cfg.preprocessor)
    
    # Set model to inference mode
    asr_model.eval();
    asr_model = asr_model.to(asr_model.device)

    return cfg, asr_model

def callback_sim(asr, uniq_key, buffer_counter, sdata, frame_count, time_info, status):
    start_time = time.time()
    asr.buffer_counter = buffer_counter
    sampled_seg_sig = sdata[asr.CHUNK_SIZE*(asr.buffer_counter):asr.CHUNK_SIZE*(asr.buffer_counter+1)]
    asr.uniq_id = uniq_key
    asr.signal = sdata
    text, timestamps, end_stamp, diar_labels = asr.transcribe(sampled_seg_sig)
    if asr.buffer_start >= 0 and (diar_labels != [] and diar_labels != None):
        asr.get_word_ts(text, timestamps, end_stamp)
        string_out = asr.get_speaker_label_per_word(uniq_key, asr.word_seq, asr.word_ts_seq, diar_labels)
        write_txt(f"{asr.diar._out_dir}/online_trans.txt", string_out.strip())
    time.sleep(0.97 - time.time() + start_time)

class OnlineClusteringDiarizer(ClusteringDiarizer, ASR_DIAR_OFFLINE):
    def __init__(self, cfg: DictConfig, params: Dict):
        super().__init__(cfg)
        
        cfg = model_utils.convert_model_config_to_dict_config(cfg)
        
        # Convert config to support Hydra 1.0+ instantiation
        cfg = model_utils.maybe_update_config_version(cfg)
        self._cfg = cfg
        self.params = params
        self._out_dir = self._cfg.diarizer.out_dir
        if not os.path.exists(self._out_dir):
            os.mkdir(self._out_dir)

        self._speaker_manifest_path = self._cfg.diarizer.speaker_embeddings.oracle_vad_manifest
        self.AUDIO_RTTM_MAP = None
        self.paths2audio_files = self._cfg.diarizer.paths2audio_files
        
        self.paths2session_audio_files = []
        self.all_hypothesis = []
        self.all_reference = []
        self.out_rttm_dir = None

        self.embed_seg_len = self._cfg.diarizer.speaker_embeddings.window_length_in_sec
        self.embed_seg_hop = self._cfg.diarizer.speaker_embeddings.shift_length_in_sec
        self.enhanced_count_thres = 80
        self.oracle_num_speakers = None
        self.max_num_speakers = 8
        self._current_buffer_segment_count = 64
        self._history_buffer_segment_count = 64
        self.MINIMUM_CLUS_BUFFER_SIZE = 10
        self.MINIMUM_HIST_BUFFER_SIZE = 32
        self._minimum_segments_per_buffer = int(self._history_buffer_segment_count/self.max_num_speakers)
        self.segment_abs_time_range_list = []
        self.segment_raw_audio_list = []
        self.cumulative_cluster_labels = []
        self.use_online_mat_reduction = True
        self.history_embedding_buffer_emb = np.array([])
        self.history_embedding_buffer_label = np.array([])
        self.history_buffer_seg_start = None
        self.history_buffer_seg_end = None
        self.old_history_buffer_seg_end = None
        self.last_emb_in_length = -float('inf')
        self.frame_index = None
        self.index_dict = {'max_embed_count': 0}
        self.cumulative_speaker_count = {}
        self.embedding_count_history = []

        self.diar_eval_count = 0
        self.der_dict = {}
        self.der_stat_dict = {"avg_DER":0, "avg_CER":0, "max_DER":0, "max_CER":0, "cum_DER":0, "cum_CER":0}
    
    @property 
    def online_diar_buffer_segment_quantity(self, value):
        return self._current_buffer_segment_count

    @online_diar_buffer_segment_quantity.setter
    def online_diar_buffer_segment_quantity(self, value):
        logging.info(f"Setting online diarization buffer to : {value}")
        assert value >= self.MINIMUM_CLUS_BUFFER_SIZE, f"Online diarization clustering buffer should be bigger than {self.MINIMUM_CLUS_BUFFER_SIZE}"
        self._current_buffer_segment_count = value # How many segments we want to use as clustering buffer
    
    @property 
    def online_history_buffer_segment_quantity(self, value):
        return self._current_buffer_segment_count

    @online_history_buffer_segment_quantity.setter
    def online_history_buffer_segment_quantity(self, value):
        logging.info(f"Setting online diarization buffer to : {value}")
        assert value >= self.MINIMUM_HIST_BUFFER_SIZE, f"Online diarization history buffer should be bigger than {self.MINIMUM_HIST_BUFFER_SIZE}"
        self._history_buffer_segment_count = value # How many segments we want to use as history buffer

    def prepare_diarization(self, paths2audio_files: List[str] = None, batch_size: int = 1):
        """
        """
        if paths2audio_files:
            self.paths2audio_files = paths2audio_files
        else:
            if self._cfg.diarizer.paths2audio_files is None:
                raise ValueError("Pass path2audio files either through config or to diarize method")
            else:
                self.paths2audio_files = self._cfg.diarizer.paths2audio_files

        if type(self.paths2audio_files) is str and os.path.isfile(self.paths2audio_files):
            paths2audio_files = []
            with open(self.paths2audio_files, 'r') as path2file:
                for audiofile in path2file.readlines():
                    audiofile = audiofile.strip()
                    paths2audio_files.append(audiofile)

        elif type(self.paths2audio_files) in [list, ListConfig]:
            paths2audio_files = list(self.paths2audio_files)

        else:
            raise ValueError("paths2audio_files must be of type list or path to file containing audio files")

        self.paths2session_audio_files= paths2audio_files

        self.AUDIO_RTTM_MAP = audio_rttm_map(paths2audio_files, self._cfg.diarizer.path2groundtruth_rttm_files)

        # self._extract_embeddings(self._speaker_manifest_path)
        self.out_rttm_dir = os.path.join(self._out_dir, 'pred_rttms')
        os.makedirs(self.out_rttm_dir, exist_ok=True)
    
    @staticmethod 
    def estimateNumofSpeakers(affinity_mat, max_num_speaker, is_cuda=False):
        """
        Estimates the number of speakers using eigen decompose on laplacian Matrix.
        affinity_mat: (array)
            NxN affitnity matrix
        max_num_speaker: (int)
            Maximum number of clusters to consider for each session
        is_cuda: (bool)
            if cuda availble eigh decomposition would be computed on GPUs
        """
        laplacian = getLaplacian(affinity_mat)
        lambdas, _ = eigDecompose(laplacian, is_cuda)
        lambdas = np.sort(lambdas)
        lambda_gap_list = getLamdaGaplist(lambdas)
        num_of_spk = np.argmax(lambda_gap_list[: min(max_num_speaker, len(lambda_gap_list))]) + 1
        return num_of_spk, lambdas, lambda_gap_list

    def getMergeQuantity(self, new_emb_n, before_cluster_labels):
        """
        Determine which embeddings we need to reduce or merge in history buffer.
        We want to merge or remove the embedding in the bigger cluster first.
        At the same time, we keep the minimum number of embedding per cluster
        with the variable named self._minimum_segments_per_buffer.
        The while loop creates a numpy array emb_n_per_cluster.
        that tells us how many embeddings we should remove/merge per cluster.

        Args:
            new_emb_n: (int)
                the quantity of the newly obtained embedding from the stream.

            before_cluster_labels: (np.array)
                the speaker labels of (the history_embedding_buffer_emb) + (the new embeddings to be added)
        """
        targeted_total_n = new_emb_n
        count_dict = Counter(before_cluster_labels)
        spk_freq_count = np.bincount(before_cluster_labels)
        class_vol = copy.deepcopy(spk_freq_count)
        emb_n_per_cluster = np.zeros_like(class_vol).astype(int)
        arg_max_spk_freq = np.argsort(spk_freq_count)[::-1]
        count = 0
        while np.sum(emb_n_per_cluster) < new_emb_n:
            recurr_idx = np.mod(count, len(count_dict))
            curr_idx = arg_max_spk_freq[recurr_idx]
            margin = (spk_freq_count[curr_idx] - emb_n_per_cluster[curr_idx]) - self._minimum_segments_per_buffer
            if margin > 0:
                target_number = min(margin, new_emb_n)
                emb_n_per_cluster[curr_idx] += target_number
                new_emb_n -= target_number
            count += 1
        print("Counter:", count_dict)
        assert sum(emb_n_per_cluster) == targeted_total_n, "emb_n_per_cluster does not match with targeted number new_emb_n."
        return emb_n_per_cluster

    def reduce_emb(self, cmat, tick2d, emb_ndx, cluster_labels, method='avg'):
        LI, RI = tick2d[0, :], tick2d[1, :]
        LI_argdx = tick2d[0].argsort()

        if method == 'drop':
            cmat_sym = cmat + cmat.T
            clus_score = np.vstack((np.sum(cmat_sym[LI], axis=1), np.sum(cmat_sym[RI], axis=1)))
            selected_dx = np.argmax(clus_score, axis=0)
            emb_idx = np.choose(selected_dx, tick2d)
            result_emb = emb_ndx[emb_idx, :]
        elif method == 'avg':
            LI, RI = LI[LI_argdx], RI[LI_argdx]
            result_emb = 0.5*(emb_ndx[LI, :] + emb_ndx[RI, :])
        else:
            raise ValueError(f'Method {method} does not exist. Abort.')
        merged_cluster_labels = cluster_labels[np.array(list(set(LI)))]
        bypass_ndx = np.array(list(set(range(emb_ndx.shape[0])) - set(list(LI)+list(RI)) ) )
        if len(bypass_ndx) > 0:
            result_emb = np.vstack((emb_ndx[bypass_ndx], result_emb))  
            merged_cluster_labels = np.hstack((cluster_labels[bypass_ndx], merged_cluster_labels))
        return result_emb, LI, merged_cluster_labels
    

    def mergeEmbedding(self, emb_in, mat):
        history_n, current_n = self._history_buffer_segment_count, self._current_buffer_segment_count
        add_new_emb_to_history = True

        print("[Streaming diarization with history buffer]: emb_in.shape:", emb_in.shape)
        if len(self.history_embedding_buffer_emb) > 0:
            if emb_in.shape[0] <= self.index_dict['max_embed_count']:
                # If the number of embeddings is decreased compared to the last trial,
                # then skip embedding merging.
                add_new_emb_to_history = False
                hist_curr_boundary = self.history_buffer_seg_end
            else:
                # Since there are new embeddings, we push the same amount (new_emb_n) 
                # of old embeddings to the history buffer.
                # We should also update self.history_buffer_seg_end which is a pointer.
                hist_curr_boundary = emb_in.shape[0] - self._current_buffer_segment_count
                _stt = self.history_buffer_seg_end # The old history-current boundary
                _end = hist_curr_boundary # The new history-current boundary
                new_emb_n = _end - _stt
                assert new_emb_n > 0, "new_emb_n cannot be 0 or a negative number."
                update_to_history_emb = emb_in[_stt:_end]
                update_to_history_label = self.cumulative_cluster_labels[_stt:_end]
                emb = np.vstack((self.history_embedding_buffer_emb, update_to_history_emb))
                before_cluster_labels = np.hstack((self.history_embedding_buffer_label, update_to_history_label))
                self.history_buffer_seg_end = hist_curr_boundary
        else:
            # This else statement is for the very first diarization loop.
            # This is the very first reduction frame.
            hist_curr_boundary = emb_in.shape[0] - self._current_buffer_segment_count
            new_emb_n = mat.shape[0] - (self._current_buffer_segment_count + self._history_buffer_segment_count)
            emb = emb_in[:hist_curr_boundary]
            before_cluster_labels = self.cumulative_cluster_labels[:hist_curr_boundary]
            self.history_buffer_seg_end = hist_curr_boundary
       
        # Update the history/current_buffer boundary cursor
        total_emb, total_cluster_labels = [], []
        
        if add_new_emb_to_history:
            class_target_vol = self.getMergeQuantity(new_emb_n, before_cluster_labels)
            
            # Merge the segments in the history buffer
            for spk_idx, N in enumerate(list(class_target_vol)):
                ndx = np.where(before_cluster_labels == spk_idx)[0]
                if N <= 0:
                    result_emb = emb[ndx]
                    merged_cluster_labels = before_cluster_labels[ndx]
                else:
                    cmat = np.tril(mat[:,ndx][ndx,:])
                    tick2d = self.getIndecesForEmbeddingReduction(cmat, ndx, N)
                    spk_cluster_labels, emb_ndx = before_cluster_labels[ndx], emb[ndx]
                    result_emb, tick_sum, merged_cluster_labels = self.reduce_emb(cmat, tick2d, emb_ndx, spk_cluster_labels, method='avg')
                    assert (ndx.shape[0] - N) == result_emb.shape[0], ipdb.set_trace()
                total_emb.append(result_emb)
                total_cluster_labels.append(merged_cluster_labels)
        
            self.history_embedding_buffer_emb = np.vstack(total_emb)
            self.history_embedding_buffer_label = np.hstack(total_cluster_labels)
            assert self.history_embedding_buffer_emb.shape[0] == history_n, ipdb.set_trace()
        else:
            total_emb.append(self.history_embedding_buffer_emb)
            total_cluster_labels.append(self.history_embedding_buffer_label)

        # Add the current buffer
        total_emb.append(emb_in[hist_curr_boundary:])
        total_cluster_labels.append(self.cumulative_cluster_labels[hist_curr_boundary:])

        history_and_current_emb = np.vstack(total_emb)
        history_and_current_labels = np.hstack(total_cluster_labels)
        assert history_and_current_emb.shape[0] <= (history_n + current_n), ipdb.set_trace()
        
        self.last_emb_in_length = emb_in.shape[0]
        return history_and_current_emb, history_and_current_labels, current_n, add_new_emb_to_history
    
    def getIndecesForEmbeddingReduction(self, cmat, ndx, N):
        """
        Get indeces of the embeddings we want to merge or drop.

        Args:
            cmat: (np.array)
            ndx: (np.array)
            N: (int)
        """
        comb_limit = int(ndx.shape[0]/2)
        assert N <= comb_limit, f" N is {N}: {N} is bigger than comb_limit -{comb_limit}"
        idx2d = np.unravel_index(np.argsort(cmat, axis=None)[::-1], cmat.shape)
        num_of_lower_half = int((cmat.shape[0]**2 - cmat.shape[0])/2)
        idx2d = (idx2d[0][:num_of_lower_half], idx2d[1][:num_of_lower_half])
        cdx, left_set, right_set, total_set = 0, [], [], []
        while len(left_set) <  N and len(right_set) < N:
            Ldx, Rdx = idx2d[0][cdx], idx2d[1][cdx] 
            if (not Ldx in total_set) and (not Rdx in total_set):
                left_set.append(Ldx)
                right_set.append(Rdx)
                total_set = left_set + right_set
            cdx += 1
        tick2d = np.array([left_set, right_set])
        return tick2d

    def getReducedMat(self, mat, emb):
        margin_seg_n = mat.shape[0] - (self._current_buffer_segment_count + self._history_buffer_segment_count)
        if margin_seg_n > 0:
            mat = 0.5*(mat + mat.T)
            np.fill_diagonal(mat, 0)
            merged_emb, cluster_labels, current_n, add_new = self.mergeEmbedding(emb, mat)
        else:
            merged_emb = emb
            current_n = self._current_buffer_segment_count
            cluster_labels, add_new = None, True
        return merged_emb, cluster_labels, add_new
    
    def online_eval_diarization(self, pred_labels, rttm_file, ROUND=2):
        diar_labels, ref_labels_list = [], []
        all_hypotheses, all_references = [], []

        if os.path.exists(rttm_file):
            ref_labels_total = rttm_to_labels(rttm_file)
            ref_labels = get_partial_ref_labels(pred_labels, ref_labels_total)
            reference = labels_to_pyannote_object(ref_labels)
            all_references.append(reference)
        else:
            raise ValueError("No reference RTTM file provided.")

        diar_labels.append(pred_labels)

        est_n_spk = self.get_num_of_spk_from_labels(pred_labels)
        ref_n_spk = self.get_num_of_spk_from_labels(ref_labels)
        hypothesis = labels_to_pyannote_object(pred_labels)
        if ref_labels == [] and pred_labels != []:
            DER, CER, FA, MISS = 1.0, 0, 1.0, 0
            der_dict, der_stat_dict = self.get_stat_DER(DER, CER, FA, MISS)
            return der_dict, der_stat_dict
        else:
            all_hypotheses.append(hypothesis)
            DER, CER, FA, MISS, = get_DER(all_references, all_hypotheses)
            logging.info(
                "Streaming Diar [frame-    {}th    ]: DER:{:.4f} MISS:{:.4f} FA:{:.4f}, CER:{:.4f}".format(
                    self.frame_index, DER, MISS, FA, CER
                )
            )
            der_dict, der_stat_dict = self.get_stat_DER(DER, CER, FA, MISS)
            return der_dict, der_stat_dict
    
    def get_stat_DER(self, DER, CER, FA, MISS, ROUND=2):
        der_dict = {"DER": round(100*DER, ROUND), 
                     "CER": round(100*CER, ROUND), 
                     "FA":  round(100*FA, ROUND), 
                     "MISS": round(100*MISS, ROUND)}
        self.diar_eval_count += 1
        self.der_stat_dict['cum_DER'] += DER
        self.der_stat_dict['cum_CER'] += CER
        self.der_stat_dict['avg_DER'] = round(100*self.der_stat_dict['cum_DER']/self.diar_eval_count, ROUND)
        self.der_stat_dict['avg_CER'] = round(100*self.der_stat_dict['cum_CER']/self.diar_eval_count, ROUND)
        self.der_stat_dict['max_DER'] = round(max(der_dict['DER'], self.der_stat_dict['max_DER']), ROUND)
        self.der_stat_dict['max_CER'] = round(max(der_dict['CER'], self.der_stat_dict['max_CER']), ROUND)
        return der_dict, self.der_stat_dict
    

    def OnlineCOSclustering(self, key, emb, oracle_num_speakers=None, max_num_speaker=8, enhanced_count_thres=80, min_samples_for_NMESC=6, fixed_thres=None, cuda=False):
        """
        Clustering method for speaker diarization based on cosine similarity.

        Parameters:
            key: (str)
                A unique ID for each speaker

            emb: (numpy array)
                Speaker embedding extracted from an embedding extractor

            oracle_num_speaker: (int or None)
                Oracle number of speakers if known else None

            max_num_speaker: (int)
                Maximum number of clusters to consider for each session

            min_samples: (int)
                Minimum number of samples required for NME clustering, this avoids
                zero p_neighbour_lists. Default of 6 is selected since (1/rp_threshold) >= 4
                when max_rp_threshold = 0.25. Thus, NME analysis is skipped for matrices
                smaller than (min_samples)x(min_samples).
        Returns:
            Y: (List[int])
                Speaker label for each segment.
        """
        mat = getCosAffinityMatrix(emb)
        org_mat = copy.deepcopy(mat)
        emb, reduced_labels, add_new = self.getReducedMat(mat, emb)
        
        self.index_dict[self.frame_index] = (org_mat.shape[0], self.history_buffer_seg_end)
        self.index_dict['max_embed_count'] = max(org_mat.shape[0], self.index_dict['max_embed_count'])

        if emb.shape[0] == 1:
            return np.array([0])
        elif emb.shape[0] <= max(enhanced_count_thres, min_samples_for_NMESC) and oracle_num_speakers is None:
            est_num_of_spk_enhanced = getEnhancedSpeakerCount(key, emb, cuda, random_test_count=5, anchor_spk_n=3, anchor_sample_n=10, sigma=100)
        else:
            est_num_of_spk_enhanced = None

        if oracle_num_speakers:
            max_num_speaker = oracle_num_speakers

        mat = getCosAffinityMatrix(emb)
        nmesc = NMESC(
            mat,
            max_num_speaker=max_num_speaker,
            max_rp_threshold=0.25,
            sparse_search=True,
            sparse_search_volume=30,
            fixed_thres=None,
            NME_mat_size=300,
            cuda=cuda,
        )

        if emb.shape[0] > min_samples_for_NMESC:
            est_num_of_spk, p_hat_value, best_g_p_value = nmesc.NMEanalysis()
            affinity_mat = getAffinityGraphMat(mat, p_hat_value)
        else:
            affinity_mat = mat
        
        if oracle_num_speakers:
            est_num_of_spk = oracle_num_speakers
        elif est_num_of_spk_enhanced:
            est_num_of_spk = est_num_of_spk_enhanced
     s 

        spectral_model = _SpectralClustering(n_clusters=est_num_of_spk, cuda=cuda)
        Y = spectral_model.predict(affinity_mat)
       
        if len(self.history_embedding_buffer_emb) != 0:
            # Online clustering mode with history buffer
            update_point = self._history_buffer_segment_count
            Y_matched, cost = self.matchNewOldclusterLabels(self.cumulative_cluster_labels, Y)
            if add_new:
                assert Y_matched[update_point:].shape[0] == self._current_buffer_segment_count, "Update point sync is not correct."
                Y_out = np.hstack((self.cumulative_cluster_labels[:self.history_buffer_seg_end], Y_matched[update_point:]))
                self.cumulative_cluster_labels = Y_out
            else:
                # Do not update cumulative labels since there are no new segments.
                Y_out = self.cumulative_cluster_labels[:org_mat.shape[0]]
            assert len(Y_out) == org_mat.shape[0], ipdb.set_trace()
        else:
            # Regular offline clustering
            Y_out = Y
            self.cumulative_cluster_labels = Y_out
        return Y_out
    
    def matchNewOldclusterLabels(self, cum_labels, Y):
        """
        Run Hungarian algorithm to find the best permuation mapping between
        cumulated labels in history and the new clustering output labels.

        cum_labels (np.array):
            Cumulated diarization labels. This will be concatenated with history embedding speaker label
            then compared with the predicted label Y.

        Y (np.array):
            Contains predicted labels for reduced history embeddings concatenated with the predicted label.
            Permutation is not matched yet.

        """
        spk_count = max(len(set(cum_labels)), len(set(Y)))
        P = np.hstack((self.history_embedding_buffer_label, cum_labels[self.history_buffer_seg_end:]))
        Q = Y
        min_len = min(P.shape[0], Q.shape[0])
        P, Q = P[:min_len], Q[:min_len]
        PuQ = (set(P) | set(Q))
        PiQ = (set(P) & set(Q))
        PmQ, QmP =  set(P) - set(Q),  set(Q) - set(P)

        if len(PiQ) == 0:
            pass
        elif len(PmQ) > 0 or len(QmP) > 0:
            # Only keep common speaker labels.
            keyQ = ~np.zeros_like(Q).astype(bool)
            keyP = ~np.zeros_like(P).astype(bool)
            for spk in list(QmP):
                keyQ[Q == spk] = False
            for spk in list(PmQ):
                keyP[P == spk] = False
            common_key = keyP*keyQ
            P, Q = P[common_key], Q[common_key]

        all_spks = [ [x] for x in range(len(PuQ))]

        if len(PuQ) == 1:
            # When two speaker vectors are exactly the same: No need to encode.
            col_ind = np.array([0, 0])
            cost = None
        else:
            # Use one-hot encodding to find the best match.
            enc = OneHotEncoder(handle_unknown='ignore') 
            enc.fit(all_spks)
            enc_P = enc.transform(P.reshape(-1, 1)).toarray()
            enc_Q = enc.transform(Q.reshape(-1, 1)).toarray()
            stacked = np.hstack((enc_P, enc_Q))
            cost = -1*linear_kernel(stacked.T)[spk_count:, :spk_count]
            row_ind, col_ind = linear_sum_assignment(cost)

        if len(PmQ) > 0 or len(QmP) > 0:
            # If number of are speakers in each vector is not the same
            mapping_array = np.arange(len(set(PuQ))).astype(int)
            for x in range(mapping_array.shape[0]):
                if x in (set(PmQ) | set(QmP)):
                    mapping_array[x] = x
                else:
                    mapping_array[x] = col_ind[x]
        else:
            mapping_array = col_ind

        return mapping_array[Y], cost

# simple data layer to pass audio signal
class AudioDataLayer(IterableDataset):
    @property
    def output_types(self):
        return {
            'audio_signal': NeuralType(('B', 'T'), AudioSignal(freq=self._sample_rate)),
            'a_sig_length': NeuralType(tuple('B'), LengthsType()),
        }

    def __init__(self, sample_rate):
        super().__init__()
        self._sample_rate = sample_rate
        self.output = True
        
    def __iter__(self):
        return self
    
    def __next__(self):
        if not self.output:
            raise StopIteration
        self.output = False
        return torch.as_tensor(self.signal, dtype=torch.float32), torch.as_tensor(self.signal_shape, dtype=torch.int64)
        
    def set_signal(self, signal):
        self.signal = signal.astype(np.float32)/32768.
        self.signal_shape = self.signal.size
        self.output = True

    def __len__(self):
        return 1




class Frame_ASR_DIAR:
    def __init__(self, asr_model, diar, model_definition,
                 frame_len=2, frame_overlap=2.5, 
                 offset=10):
        '''
        Args:
          frame_len: frame's duration, seconds
          frame_overlap: duration of overlaps before and after current frame, seconds
          offset: number of symbols to drop for smooth streaming
        '''

        # For Streaming (Frame) ASR
        self.vocab = list(model_definition['labels'])
        self.vocab.append('_')
        
        self.sr = model_definition['sample_rate']
        self.frame_len = float(frame_len)
        self.n_frame_len = int(frame_len * self.sr)
        self.frame_overlap = float(frame_overlap)
        self.n_frame_overlap = int(frame_overlap * self.sr)
        self.timestep_duration = model_definition['AudioToMelSpectrogramPreprocessor']['window_stride']
        for block in model_definition['JasperEncoder']['jasper']:
            self.timestep_duration *= block['stride'][0] ** block['repeat']
        self.n_timesteps_overlap = int(frame_overlap / self.timestep_duration) - 2
        self.buffer = np.zeros(shape=2*self.n_frame_overlap + self.n_frame_len,
                               dtype=np.float32)
        self.offset = offset
        self.CHUNK_SIZE = int(self.frame_len*self.sr)

        # For diarization
        self.asr_model = asr_model
        self.diar = diar
        self.n_embed_seg_len = int(self.sr * self.diar.embed_seg_len)
        self.n_embed_seg_hop = int(self.sr * self.diar.embed_seg_hop)
        
        self.embs_array = None
        self.frame_index = 0
        self.cumulative_cluster_labels = []
        
        self.nonspeech_threshold = 105  # minimun width to consider non-speech activity 
        self.calibration_offset = -0.18
        self.time_stride = self.timestep_duration
        self.overlap_frames_count = int(self.n_frame_overlap/self.sr)
        self.segment_raw_audio_list = []
        self.segment_abs_time_range_list = []
        self.cumulative_speech_labels = []

        self.frame_start = 0
        self.rttm_file_path = []
        self.word_seq = []
        self.word_ts_seq = []
        self.merged_cluster_labels = []
        self.use_offline_asr = False
        self.offline_logits = None
        self.debug_mode = False
        self.online_diar_label_update_sec = 30
        self.reset()
    
        self.wer_ts = WER_TS(
            vocabulary= self.asr_model.decoder.vocabulary,
            batch_dim_index=0,
            use_cer= self.asr_model._cfg.get('use_cer', False),
            ctc_decode=True,
            dist_sync_on_step=True,
            log_prediction= self.asr_model._cfg.get("log_prediction", False),
        )
         

    def _convert_to_torch_var(self, audio_signal):
        audio_signal = torch.stack(audio_signal).float().to(self.asr_model.device)
        audio_signal_lens = torch.from_numpy(np.array([self.n_embed_seg_len for k in range(audio_signal.shape[0])])).to(self.asr_model.device)
        return audio_signal, audio_signal_lens

    def _process_cluster_labels(self, segment_ranges, cluster_labels):
        # self.cumulative_cluster_labels = list(cluster_labels)
        assert len(cluster_labels) == len(segment_ranges)
        lines = []
        for idx, label in enumerate(cluster_labels):
            tag = 'speaker_' + str(label)
            lines.append(f"{segment_ranges[idx][0]} {segment_ranges[idx][1]} {tag}")
        cont_lines = get_contiguous_stamps(lines)
        string_labels = merge_stamps(cont_lines)
        return string_labels
    
    def get_word_ts(self, text, timestamps, end_stamp):
        if text.strip() == '':
            _trans_words, word_timetamps, _spaces = [], [], []
        elif len(text.split()) == 1:
            _trans_words = [text]
            word_timetamps = [[timestamps[0], end_stamp]]
            _spaces = []
        else:
            trans, timestamps = self.diar.clean_trans_and_TS(text, timestamps)
            _spaces, _trans_words = self.diar._get_spaces(trans, timestamps)
            word_timetamps_middle = [[_spaces[k][1], _spaces[k + 1][0]] for k in range(len(_spaces) - 1)]
            word_timetamps = [[timestamps[0], _spaces[0][0]]] + word_timetamps_middle + [[_spaces[-1][1], end_stamp]]
        
        assert len(_trans_words) == len(word_timetamps)
        self.word_seq.extend(_trans_words)
        self.word_ts_seq.extend(word_timetamps)
  
    def _run_embedding_extractor(self, audio_signal):
        self.diar._speaker_model.eval()
        torch_audio_signal, torch_audio_signal_lens = self._convert_to_torch_var(audio_signal)
        _, torch_embs = self.diar._speaker_model.forward(input_signal=torch_audio_signal, 
                                                         input_signal_length=torch_audio_signal_lens)
        return torch_embs

    def _get_speaker_embeddings(self, embs_array, audio_signal, segment_ranges, online_extraction=True):
        torch.manual_seed(0)
        if online_extraction:
            hop = self.diar._cfg.diarizer.speaker_embeddings.shift_length_in_sec
            if embs_array is None:
                target_segment_count = len(segment_ranges)
                stt, end = 0, len(segment_ranges)
            else:
                target_segment_count = int(min(np.ceil((2*self.frame_overlap + self.frame_len)/hop), len(segment_ranges)))
                stt, end = len(segment_ranges)-target_segment_count, len(segment_ranges)
            torch_embs = self._run_embedding_extractor(audio_signal[stt:end])
            if embs_array is None:
                embs_array = torch_embs.cpu().numpy()
            else:
                embs_array = np.vstack((embs_array[:stt,:], torch_embs.cpu().numpy()))
            assert len(segment_ranges) == embs_array.shape[0], ipdb.set_trace()
            
        else:
            torch_embs = self._run_embedding_extractor(audio_signal)
            embs_array = torch_embs.cpu().numpy()
        return embs_array

    def _online_diarization(self, audio_signal, segment_ranges):
        self.embs_array = self._get_speaker_embeddings(self.embs_array, audio_signal, segment_ranges)

        if self.debug_mode:
            _diarization_function = COSclustering
        else:
            # _diarization_function = COSclustering
            _diarization_function = self.diar.OnlineCOSclustering

        cluster_labels = _diarization_function(
            None, 
            self.embs_array, 
            oracle_num_speakers=self.diar.oracle_num_speakers,
            enhanced_count_thres=self.diar.enhanced_count_thres, 
            max_num_speaker=self.diar.max_num_speaker, 
            cuda=True,
        )
        print("Est num of speakers: ", len(set(cluster_labels)))
        assert len(cluster_labels) == self.embs_array.shape[0]

        string_labels = self._process_cluster_labels(segment_ranges, cluster_labels)
        return string_labels

    @staticmethod 
    def get_mapped_speaker(speaker_mapping, speaker):
        if speaker in speaker_mapping:
            new_speaker = speaker_mapping[speaker]
        else:
            new_speaker = speaker
        return new_speaker

    def _get_ASR_based_VAD_timestamps(self, logits, use_offset_time=True):
        blanks = self._get_silence_timestamps(logits, symbol_idx = 28, state_symbol='blank')
        non_speech = list(filter(lambda x:x[1] - x[0] > self.nonspeech_threshold, blanks))
        if use_offset_time:
            offset_sec = int(self.frame_index - 2*self.overlap_frames_count)
        else:
            offset_sec = 0
        speech_labels = self._get_speech_labels(logits, non_speech, offset_sec)
        return speech_labels

    def _get_silence_timestamps(self, probs, symbol_idx, state_symbol):
        spaces = []
        idx_state = 0
        state = ''
        
        if np.argmax(probs[0]) == symbol_idx:
            state = state_symbol

        for idx in range(1, probs.shape[0]):
            current_char_idx = np.argmax(probs[idx])
            if state == state_symbol and current_char_idx != 0 and current_char_idx != symbol_idx:
                spaces.append([idx_state, idx-1])
                state = ''
            if state == '':
                if current_char_idx == symbol_idx:
                    state = state_symbol
                    idx_state = idx

        if state == state_symbol:
            spaces.append([idx_state, len(probs)-1])
       
        return spaces
   
    def _get_speech_labels(self, probs, non_speech, offset_sec, ROUND=2):
        frame_offset =  float((offset_sec + self.calibration_offset)/self.time_stride)
        speech_labels = []
        
        if non_speech == []: 
            start = (0 + frame_offset)*self.time_stride
            end = (len(probs) -1 + frame_offset)*self.time_stride
            start, end = round(start, ROUND), round(end, ROUND)
            if start != end:
                speech_labels.append([start, end])

        else:
            start = frame_offset * self.time_stride
            first_end = (non_speech[0][0]+frame_offset)*self.time_stride
            start, first_end = round(start, ROUND), round(first_end, ROUND)
            if start != first_end:
                speech_labels.append([start, first_end])

            if len(non_speech) > 1:
                for idx in range(len(non_speech)-1):
                    start = (non_speech[idx][1] + frame_offset)*self.time_stride
                    end = (non_speech[idx+1][0] + frame_offset)*self.time_stride
                    start, end = round(start, ROUND), round(end, ROUND)
                    if start != end:
                        speech_labels.append([start, end])
            
            last_start = (non_speech[-1][1] + frame_offset)*self.time_stride
            last_end = (len(probs) -1 + frame_offset)*self.time_stride

            last_start, last_end = round(last_start, ROUND), round(last_end, ROUND)
            if last_start != last_end:
                speech_labels.append([last_start, last_end])

        return speech_labels
    
    def get_speaker_label_per_word(self, uniq_id, words, word_ts_list, diar_labels):
        der_dict, der_stat_dict = self.diar.online_eval_diarization(diar_labels, self.rttm_file_path)
        params = self.diar.params
        start_point, end_point, speaker = diar_labels[0].split()
        word_pos, idx = 0, 0
        DER, FA, MISS, CER = der_dict['DER'], der_dict['FA'], der_dict['MISS'], der_dict['CER']
        string_out = f'[Session: {uniq_id}, DER: {DER:.2f}%, FA: {FA:.2f}% MISS: {MISS:.2f}% CER: {CER:.2f}%]'
        string_out += f"\n[avg. DER: {der_stat_dict['avg_DER']}% avg. CER: {der_stat_dict['avg_CER']}%]"
        string_out += f"\n[max. DER: {der_stat_dict['max_DER']}% max. CER: {der_stat_dict['max_CER']}%]"
        string_out = self.diar.print_time(string_out, speaker, start_point, end_point, params)
        for j, word_ts_stt_end in enumerate(word_ts_list):
            word_pos = word_ts_stt_end[0] 
            if word_pos < float(end_point):
                string_out = self.diar.print_word(string_out, words[j], params)
            else:
                idx += 1
                idx = min(idx, len(diar_labels)-1)
                start_point, end_point, speaker = diar_labels[idx].split()
                string_out = self.diar.print_time(string_out, speaker, start_point, end_point, params)
                string_out = self.diar.print_word(string_out, words[j], params)

            stt_sec, end_sec = self.diar.get_timestamp_in_sec(word_ts_stt_end, params)
        return string_out 
        
    def _decode_and_cluster(self, frame, offset=0):
        torch.manual_seed(0)
        assert len(frame)==self.n_frame_len
        self.buffer[:-self.n_frame_len] = copy.deepcopy(self.buffer[self.n_frame_len:])
        self.buffer[-self.n_frame_len:] = copy.deepcopy(frame)
    
        self.diar.frame_index = self.frame_index  
        if self.use_offline_asr:
            logits_start = self.frame_index * int(self.frame_len/self.time_stride)
            logits_end = logits_start + int((2*self.frame_overlap+self.frame_len)/self.time_stride)+1
            logits = self.offline_logits[logits_start:logits_end]
        else:
            logits = infer_signal(asr_model, self.buffer).cpu().numpy()[0]

        speech_labels_from_logits = self._get_ASR_based_VAD_timestamps(logits)
       
        if self.debug_mode:
            self.buffer_start, audio_signal, audio_lengths, speech_labels_used = self._get_diar_offline_segments(self.uniq_id)
        else:
            self.buffer_start, audio_signal, audio_lengths = self._get_diar_segments(speech_labels_from_logits)

        if self.buffer_start >= 0 and audio_signal != []:
            logging.info(f"frame {self.frame_index}th, Segment range: {audio_lengths[0][0]}s - {audio_lengths[-1][-1]}s")
            labels = self._online_diarization(audio_signal, audio_lengths)
        else:
            labels = []

        decoded = self._greedy_decoder(
            logits[self.n_timesteps_overlap:-self.n_timesteps_overlap], 
            self.vocab
        )
        self.frame_index += 1
        unmerged = decoded[:len(decoded)-offset]
        return unmerged, labels
    
    def _get_diar_offline_segments(self, uniq_id, ROUND=2):
        use_oracle_VAD = False
        buffer_start = 0.0
        self.buffer_init_time = buffer_start
        
        if use_oracle_VAD:
            user_folder = "/home/taejinp/projects"
            rttm_file_path = f"{user_folder}/NeMo/scripts/speaker_recognition/asr_based_diar/oracle_vad_saved/{uniq_id}.rttm"
            speech_labels = getVADfromRTTM(rttm_file_path)
        else:
            speech_labels = self._get_ASR_based_VAD_timestamps(self.offline_logits[200:], use_offset_time=False)

        speech_labels = [[round(x, ROUND), round(y, ROUND)] for (x, y) in speech_labels ]
        speech_labels[0][0] = 0
        source_buffer = copy.deepcopy(self.signal)
        sigs_list, sig_rangel_list = self._get_segments_from_buffer(buffer_start, 
                                                                    speech_labels,
                                                                    source_buffer)
        return buffer_start, sigs_list, sig_rangel_list, speech_labels


    def _get_diar_segments(self, speech_labels_from_logits, ROUND=2):
        buffer_start = round(float(self.frame_index - 2*self.overlap_frames_count), ROUND)

        if buffer_start >= 0:
            new_start_abs_sec, buffer_end = self._get_update_abs_time(buffer_start)
            self.frame_start = round(buffer_start + int(self.n_frame_overlap/self.sr), ROUND)
            frame_end = self.frame_start + self.frame_len 
            
            if self.diar.segment_raw_audio_list == [] and speech_labels_from_logits != []:
                self.buffer_init_time = self.buffer_start

                speech_labels_from_logits[0][0] = max(speech_labels_from_logits[0][0], 0.0)
                speech_labels_initial = copy.deepcopy(speech_labels_from_logits)
                
                self.cumulative_speech_labels = speech_labels_initial
                
                source_buffer = copy.deepcopy(self.buffer)
                sigs_list, sig_rangel_list = self._get_segments_from_buffer(buffer_start, 
                                                                            speech_labels_initial, 
                                                                            source_buffer)
                self.diar.segment_raw_audio_list = sigs_list
                self.diar.segment_abs_time_range_list = sig_rangel_list
            
            else: 
                # Remove the old segments that overlap with the new frame (self.frame_start)
                # new_start_abs_sec is set to the onset of the t_range popped lastly.
                new_start_abs_sec = self.frame_start
                while True and len(self.diar.segment_raw_audio_list) > 0:
                    t_range = self.diar.segment_abs_time_range_list[-1]

                    mid = np.mean(t_range)
                    if self.frame_start <= t_range[1]:
                        self.diar.segment_abs_time_range_list.pop()
                        self.diar.segment_raw_audio_list.pop()
                        new_start_abs_sec = t_range[0]
                    else:
                        break

                speech_labels_for_update = self._get_speech_labels_for_update(buffer_start, 
                                                                              buffer_end, 
                                                                              self.frame_start,
                                                                              speech_labels_from_logits,
                                                                              new_start_abs_sec)
                
                source_buffer = copy.deepcopy(self.buffer)

                sigs_list, sig_rangel_list = self._get_segments_from_buffer(buffer_start, 
                                                                            speech_labels_for_update, 
                                                                            source_buffer)


                self.diar.segment_raw_audio_list.extend(sigs_list)
                self.diar.segment_abs_time_range_list.extend(sig_rangel_list)
                
        return buffer_start, \
               self.diar.segment_raw_audio_list, \
               self.diar.segment_abs_time_range_list

    def _get_update_abs_time(self, buffer_start):
        new_bufflen_sec = self.n_frame_len / self.sr
        n_buffer_samples = int(len(self.buffer)/self.sr)
        total_buffer_len_sec = n_buffer_samples/self.frame_len
        buffer_end = buffer_start + total_buffer_len_sec
        return (buffer_end - new_bufflen_sec), buffer_end

    def _get_speech_labels_for_update(self, buffer_start, buffer_end, frame_start, speech_labels_from_logits, new_start_abs_sec):
        """
        Bring the new speech labels from the current buffer. Then
        1. Concatenate the old speech labels from self.cumulative_speech_labels for the overlapped region.
            - This goes to new_speech_labels.
        2. Update the new 1 sec of speech label (speech_label_for_new_segments) to self.cumulative_speech_labels.
        3. Return the speech label from new_start_abs_sec to buffer end.

        """
        new_speech_labels = []
        current_range = [frame_start, frame_start + self.frame_len]
        new_coming_range = [frame_start, buffer_end]
        cursor_to_buffer_end_range = [frame_start, buffer_end]
        
        if new_start_abs_sec < frame_start:
            update_overlap_range = [new_start_abs_sec, frame_start]
        else:
            update_overlap_range = []

        new_coming_speech_labels = getSubRangeList(target_range=new_coming_range, 
                                                   source_list=speech_labels_from_logits)

        update_overlap_speech_labels = getSubRangeList(target_range=update_overlap_range, 
                                                       source_list=self.cumulative_speech_labels)
        
        speech_label_for_new_segments = getMergedSpeechLabel(update_overlap_speech_labels, 
                                                             new_coming_speech_labels) 
        
        # For generating self.cumulative_speech_labels        
        current_frame_speech_labels = getSubRangeList(target_range=current_range, 
                                                      source_list=speech_labels_from_logits)

        self.cumulative_speech_labels = getMergedSpeechLabel(self.cumulative_speech_labels, 
                                                             current_frame_speech_labels) 
        return speech_label_for_new_segments

    def _get_segments_from_buffer(self, buffer_start, speech_labels_for_update, source_buffer, ROUND=3):
        sigs_list, sig_rangel_list = [], []
        n_seglen_samples = int(self.diar.embed_seg_len*self.sr)
        n_seghop_samples = int(self.diar.embed_seg_hop*self.sr)
        
        for idx, range_t in enumerate(speech_labels_for_update):
            if range_t[0] < 0:
                continue
            sigs, sig_lens = [], []
            stt_b = int((range_t[0] - buffer_start) * self.sr)
            end_b = int((range_t[1] - buffer_start) * self.sr)
            n_dur_samples = int(end_b - stt_b)
            base = math.ceil((n_dur_samples - n_seglen_samples) / n_seghop_samples)
            slices = 1 if base < 0 else base + 1
            try:
                sigs, sig_lens = self.get_segments_from_slices(slices, 
                                                          torch.from_numpy(source_buffer[stt_b:end_b]),
                                                          n_seglen_samples,
                                                          n_seghop_samples, 
                                                          sigs, 
                                                          sig_lens)
            except:
                ipdb.set_trace()

            sigs_list.extend(sigs)
            segment_offset = range_t[0]
            for seg_idx, sig_len in enumerate(sig_lens):
                seg_len_sec = float(sig_len / self.sr)
                start_abs_sec = round(float(segment_offset + seg_idx*self.diar.embed_seg_hop), ROUND)
                end_abs_sec = round(float(segment_offset + seg_idx*self.diar.embed_seg_hop + seg_len_sec), ROUND)
                sig_rangel_list.append([start_abs_sec, end_abs_sec])
        
        assert len(sigs_list) == len(sig_rangel_list)
        return sigs_list, sig_rangel_list

    def get_segments_from_slices(self, slices, sig, slice_length, shift, audio_signal, audio_lengths):
        """create short speech segments from sclices
        Args:
            slices (int): the number of slices to be created
            slice_length (int): the lenghth of each slice
            shift (int): the amount of slice window shift
            sig (FloatTensor): the tensor that contains input signal

        Returns:
            audio_signal (list): list of sliced input signal
        """
        for slice_id in range(slices):
            start_idx = int(slice_id * shift)
            end_idx = int(start_idx + slice_length)
            signal = sig[start_idx:end_idx]
            audio_lengths.append(len(signal))
            if len(signal) < slice_length:
                signal = repeat_signal(signal, len(signal), slice_length)
            audio_signal.append(signal)
            
        return audio_signal, audio_lengths
    
    @torch.no_grad()
    def transcribe(self, frame=None, merge=True):
        if frame is None:
            frame = np.zeros(shape=self.n_frame_len, dtype=np.float32)
        if len(frame) < self.n_frame_len:
            frame = np.pad(frame, [0, self.n_frame_len - len(frame)], 'constant')
        
        unmerged, diar_labels = self._decode_and_cluster(frame, offset=self.offset)
        text, char_ts, end_stamp = self.greedy_merge_with_ts(unmerged, self.frame_start)
        return text, char_ts, end_stamp, diar_labels
    
    def reset(self):
        '''
        Reset frame_history and decoder's state
        '''
        self.buffer=np.zeros(shape=self.buffer.shape, dtype=np.float32)
        self.prev_char = ''

    @staticmethod
    def _greedy_decoder(logits, vocab):
        s = ''
        for i in range(logits.shape[0]):
            s += vocab[np.argmax(logits[i])]
        return s
    
    def greedy_merge_with_ts(self, s, buffer_start, ROUND=2):
        s_merged = ''
        char_ts = [] 
        for i in range(len(s)):
            if s[i] != self.prev_char:
                self.prev_char = s[i]
                if self.prev_char != '_':
                    s_merged += self.prev_char
                    char_ts.append(round(buffer_start + i*self.time_stride, 2))
        end_stamp = buffer_start + len(s)*self.time_stride
        return s_merged, char_ts, end_stamp

    def greedy_merge(self, s):
        s_merged = ''
        
        for i in range(len(s)):
            if s[i] != self.prev_char:
                self.prev_char = s[i]
                if self.prev_char != '_':
                    s_merged += self.prev_char
        return s_merged


if __name__ == "__main__":
    GT_RTTM_DIR="/disk2/scps/rttm_scps/all_callhome_rttm.scp"
    AUDIO_SCP="/disk2/scps/audio_scps/all_callhome.scp"
    # GT_RTTM_DIR="/disk2/scps/rttm_scps/ami_test_rttm.scp"
    # AUDIO_SCP="/disk2/scps/audio_scps/ami_test_audio.scp"
    reco2num='/disk2/datasets/modified_callhome/RTTMS/reco2num.txt'
    SEG_LENGTH=1.5
    SEG_SHIFT=0.75
    # SPK_EMBED_MODEL="/home/taejinp/gdrive/model/speaker_net/speakerdiarization_speakernet.nemo"
    # SPK_EMBED_MODEL="/disk2/ejrvs/model_comparision/ecapa_tdnn.nemo"
    # SPK_EMBED_MODEL="/disk2/ejrvs/model_comparision/ecapa_tdnn_v2.nemo"
    SPK_EMBED_MODEL="/disk2/ejrvs/model_comparision/contextnet_v1.nemo"
    DIARIZER_OUT_DIR='./'
    reco2num='null'
    session_name = "ES2004d.Mix-Headset" # Easy sample DER:0.2305 MISS:0.0726 FA:0.0291, CER:0.1288
    # session_name = "IS1009c.Mix-Headset" # Hard Sample, high CER after 1300s
    # session_name = "TS3007d.Mix-Headset" # RTTM file is empty until 99s
    # session_name = "IS1009b.Mix-Headset" # 
    # session_name = "ES2004c.Mix-Headset" # Hard sample 
    session_name = "en_4092"  ### [For DEMO] up to 0.0183 , getEnhancedSpeakerCount shows huge difference
    # session_name = "en_0638"  ### [For DEMO] Easy sample offline  DER: 0.2026      MISS 0.0277 FA: 0.1294, CER:0.0455
    # session_name = "en_4325"  ### Hard sample
    # session_name = "en_4065"  ### Hard sample
    # session_name = "en_4145"  ### Easy sample huge MISS 0.4581 FA: 0.0050, CER:0.0075
    # session_name = "en_5208"  ### Hard sample for ASR MISS 0.1925 FA: 0.0020, CER:0.3569 - one speaker volume is really low!
    # session_name = "en_5242"  ### [For DEMO] Easy sample MISS 0.0991 FA: 0.0045, CER:0.0255
    # session_name = "en_6252"  ### Easy 
    # session_name = "en_6274"  ### Easy sample

    overrides = [
    f"diarizer.speaker_embeddings.model_path={SPK_EMBED_MODEL}",
    f"diarizer.path2groundtruth_rttm_files={GT_RTTM_DIR}",
    f"diarizer.paths2audio_files={AUDIO_SCP}",
    f"diarizer.out_dir={DIARIZER_OUT_DIR}",
    f"diarizer.oracle_num_speakers={reco2num}",
    f"diarizer.speaker_embeddings.window_length_in_sec={SEG_LENGTH}",
    f"diarizer.speaker_embeddings.shift_length_in_sec={SEG_SHIFT}",
    ]
    
    params = {
        "time_stride": 0.02,  # This should not be changed if you are using QuartzNet15x5Base.
        "offset": -0.18,  # This should not be changed if you are using QuartzNet15x5Base.
        "round_float": 2,
        "window_length_in_sec": 1.5,
        "shift_length_in_sec": 0.75,
        "print_transcript": False,
        "lenient_overlap_WDER": True,
        "threshold": 45,  # minimun width to consider non-speech activity
        "external_oracle_vad": False,
        "ASR_model_name": 'QuartzNet15x5Base-En',
    }

    hydra.initialize(config_path="conf")
    
    cfg_diar = hydra.compose(config_name="/speaker_diarization.yaml", overrides=overrides)
    
    diar = OnlineClusteringDiarizer(cfg=cfg_diar, params=params)
    diar.nonspeech_threshold = params['threshold']
    diar.online_diar_buffer_segment_quantity = 50
    diar.online_history_buffer_segment_quantity = 150
    diar.enhanced_count_thres = 0
    diar.max_num_speaker = 8
    diar.oracle_num_speakers = None
    diar.prepare_diarization()
    
    cfg, asr_model = load_ASR_model(params['ASR_model_name'])

    SAMPLE_RATE = 16000
    FRAME_LEN = 1.0
    asr = Frame_ASR_DIAR(asr_model, diar,
                         model_definition = {
                               'sample_rate': SAMPLE_RATE,
                               'AudioToMelSpectrogramPreprocessor': cfg.preprocessor,
                               'JasperEncoder': cfg.encoder,
                               'labels': cfg.decoder.vocabulary
                         },
                         frame_len=FRAME_LEN, frame_overlap=2, 
                         offset=4)
    
    asr.reset()
    asr.debug_mode = False
    asr.use_offline_asr = True
    for uniq_key, dcont in diar.AUDIO_RTTM_MAP.items():
        if uniq_key == session_name:
            samplerate, sdata = wavfile.read(dcont['audio_path'])
            asr.curr_uniq_key = uniq_key
            asr.rttm_file_path = dcont['rttm_path']
            
            if asr.use_offline_asr:
                # Infer log prob at once to maximize the ASR accuracy
                asr.offline_logits = asr.asr_model.transcribe([dcont['audio_path']], logprobs=True)[0]

                # Pad zeros to sync with online buffer with incoming frame
                asr.offline_logits = np.vstack((np.zeros((int(4*asr.frame_len/asr.time_stride), asr.offline_logits.shape[1])), asr.offline_logits))
            
            for i in range(int(np.floor(sdata.shape[0]/asr.n_frame_len))):
                callback_sim(asr, uniq_key, i, sdata, frame_count=None, time_info=None, status=None)
