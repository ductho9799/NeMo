# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

import argparse
import json
import os
import time
from pathlib import Path

import torch
from scipy.io import wavfile

from nemo.collections import asr as nemo_asr
from nemo.collections.asr.metrics.wer import WER
from nemo.utils import logging

parser = argparse.ArgumentParser(description="Cut audio on the segments based on segments")
parser.add_argument("--output_dir", default='output', type=str, help='Path to output directory')
parser.add_argument(
    "--alignment",
    type=str,
    required=True,
    help='Path to a data directory with alignments or a single .txt file with timestamps - result of the ctc-segmentation',
)
parser.add_argument("--threshold", type=float, default=-5, help='Minimum score value accepted')
parser.add_argument(
    '--model',
    type=str,
    default='/home/ebakhturina/nemo_ckpts/quartznet/QuartzNet15x5-Ru-e512-wer14.45.nemo',
    help='Path to model checkpoint or pretrained model name',
)
# TO DO:
# check the debug in logging
parser.add_argument('--debug', action='store_true', help='Set to True for debugging')
parser.add_argument("--batch_size", type=int, default=64)


def add_transcript_to_manifest(manifest_original, manifest_updated, asr_model, batch_size):
    transcripts = get_transcript(manifest_original, asr_model, batch_size)
    with open(manifest_original, 'r', encoding='utf8') as f:
        with open(manifest_updated, 'w', encoding='utf8') as f_updated:
            for i, line in enumerate(f):
                info = json.loads(line)
                info['transcript'] = transcripts[i]
                json.dump(info, f_updated, ensure_ascii=False)
                f_updated.write('\n')


def get_transcript(manifest_path, asr_model, batch_size):
    # batch inference
    try:
        from torch.cuda.amp import autocast
    except ImportError:
        from contextlib import contextmanager

        @contextmanager
        def autocast(enabled=None):
            yield

    torch.set_grad_enabled(False)
    asr_model.setup_test_data(
        test_data_config={
            'sample_rate': 16000,
            'manifest_filepath': manifest_path,
            'labels': asr_model.decoder.vocabulary,
            'batch_size': batch_size,
            'normalize_transcripts': False,
        }
    )
    asr_model.eval()
    wer = WER(vocabulary=asr_model.decoder.vocabulary)
    hypotheses = []
    for test_batch in asr_model.test_dataloader():
        if torch.cuda.is_available():
            test_batch = [x.cuda() for x in test_batch]
        with autocast():
            log_probs, encoded_len, greedy_predictions = asr_model(
                input_signal=test_batch[0], input_signal_length=test_batch[1]
            )
        hypotheses += wer.ctc_decoder_predictions_tensor(greedy_predictions)
        del test_batch
        torch.cuda.empty_cache()
    return hypotheses


def process_alignment(alignment_file, args):
    """ Cut original audio file into audio segments based on alignment_file

    Args:
        alignment_file: path to the file with segmented text and corresponding time stamps.
            The first line of the file contains the path to the original audio file
        args: main script args
    """
    if not os.path.exists(alignment_file):
        raise ValueError(f'{alignment_file} not found')

    # read the segments, note the first line contins the path to the original audio
    segments = []
    ref_text_processed = []
    ref_text_no_preprocessing = []
    with open(alignment_file, 'r') as f:
        for line in f:
            line = line.split('|')
            # read audio file name from the first line
            if len(line) == 1:
                audio_file = line[0].strip()
                continue
            ref_text_processed.append(line[1].strip())
            ref_text_no_preprocessing.append(line[2].strip())
            line = line[0].split()
            segments.append((float(line[0]), float(line[1]), float(line[2])))

    # cut the audio into segments
    # create manifest in /tmp directory first, then update transcript values with batch inference results
    # save the final minifests at output_dir
    sampling_rate, signal = wavfile.read(audio_file)
    original_duration = len(signal) / sampling_rate
    logging.info(f'Cutting {audio_file} based on {alignment_file}')
    logging.info(f'Original duration: {round(original_duration)}s or ~{round(original_duration / 60)}min')

    # create directories to store high score .wav audio fragments, low scored once, and deleted
    args.output_dir = os.path.abspath(args.output_dir)
    os.makedirs(args.output_dir, exist_ok=True)
    fragments_dir = os.path.join(args.output_dir, "high_score_clips")
    del_fragments = os.path.join(args.output_dir, 'deleted_clips')
    low_score_segments_dir = os.path.join(args.output_dir, "low_score_clips")

    os.makedirs(fragments_dir, exist_ok=True)
    os.makedirs(low_score_segments_dir, exist_ok=True)
    os.makedirs(del_fragments, exist_ok=True)

    base_name = os.path.basename(alignment_file).replace('_segments.txt', '')
    high_score_manifest = f'{base_name}_high_score_manifest.json'
    low_score_manifest = f'{base_name}_low_score_manifest.json'
    del_manifest = f'{base_name}_del_manifest.json'
    manifests_dir = os.path.join(args.output_dir, 'manifests')
    os.makedirs(manifests_dir, exist_ok=True)
    tmp_dir = '/tmp'

    low_score_dur = 0
    high_score_dur = 0
    with open(os.path.join(tmp_dir, high_score_manifest), 'w', encoding='utf8') as f:
        with open(os.path.join(tmp_dir, low_score_manifest), 'w', encoding='utf8') as low_score_f:
            for i, (st, end, score) in enumerate(segments):
                segment = signal[round(st * sampling_rate) : round(end * sampling_rate)]
                duration = len(segment) / sampling_rate
                if duration > 0:
                    text_processed = ref_text_processed[i]
                    text_no_preprocessing = ref_text_no_preprocessing[i]
                    if score > args.threshold:
                        high_score_dur += duration
                        audio_filepath = os.path.join(fragments_dir, f'{base_name}_{i:04}.wav')
                        file_to_write = f
                    else:
                        low_score_dur += duration
                        audio_filepath = os.path.join(low_score_segments_dir, f'{base_name}_{i:04}.wav')
                        file_to_write = low_score_f

                    wavfile.write(audio_filepath, sampling_rate, segment)

                    transcript = 'n/a'  # asr_model.transcribe(paths2audio_files=[audio_filepath], batch_size=1)[0]
                    info = {
                        'audio_filepath': audio_filepath,
                        'duration': duration,
                        'text': text_processed,
                        'text_no_preprocessing': text_no_preprocessing,
                        'score': round(score, 2),
                        'transcript': transcript.strip(),
                    }
                    json.dump(info, file_to_write, ensure_ascii=False)
                    file_to_write.write('\n')

    add_transcript_to_manifest(
        os.path.join(tmp_dir, high_score_manifest),
        os.path.join(manifests_dir, high_score_manifest),
        asr_model,
        args.batch_size,
    )
    add_transcript_to_manifest(
        os.path.join(tmp_dir, low_score_manifest),
        os.path.join(manifests_dir, low_score_manifest),
        asr_model,
        args.batch_size,
    )
    logging.info(
        f'High score files duration: {round(high_score_dur)}s or ~{round(high_score_dur/60)}min at {manifests_dir}'
    )
    logging.info(
        f'Low score files duration: {round(low_score_dur)}s or ~{round(low_score_dur/60)}min saved at {manifests_dir}'
    )

    # save deleted segments along with manifest
    deleted = []
    del_duration = 0
    begin = 0
    with open(os.path.join(manifests_dir, del_manifest), 'w', encoding='utf8') as f:
        for i, (st, end, _) in enumerate(segments):
            if st - begin > 0.01:
                segment = signal[int(begin * sampling_rate) : int(st * sampling_rate)]
                audio_filepath = os.path.join(del_fragments, f'del_{base_name}_{i:04}.wav')
                wavfile.write(audio_filepath, sampling_rate, segment)
                duration = len(segment) / sampling_rate
                del_duration += duration
                deleted.append((begin, st))
                transcript = 'n/a'  # asr_model.transcribe(paths2audio_files=[audio_filepath], batch_size=1)[0]
                info = {'audio_filepath': audio_filepath, 'duration': duration, 'text': transcript}
                json.dump(info, f, ensure_ascii=False)
                f.write('\n')
            begin = end

        segment = signal[int(begin * sampling_rate) :]
        audio_filepath = os.path.join(del_fragments, f'del_{base_name}_{i+1:04}.wav')
        wavfile.write(audio_filepath, sampling_rate, segment)
        duration = len(segment) / sampling_rate
        del_duration += duration
        deleted.append((begin, original_duration))

        info = {'audio_filepath': audio_filepath, 'duration': duration, 'text': 'n/a'}
        json.dump(info, f)
        f.write('\n')

    # add_transcript_to_manifest(os.path.join(tmp_dir, del_manifest),
    #                            os.path.join(args.output_dir, del_manifest), asr_model, 1)
    logging.info(
        f'Saved DEL files duration: {round(del_duration)}s or ~ {round(del_duration/60)}min at {del_fragments}'
    )
    missing_audio = original_duration - high_score_dur - del_duration - low_score_dur
    if missing_audio > 15:
        raise ValueError(f'{round(missing_audio)}s or ~ {round(missing_audio/60)}min is missing. Check the args')

    stats = f'{args.output_dir}\t{base_name}\t{round(original_duration)}\t{round(high_score_dur)}\t{round(low_score_dur)}\t{round(del_duration)}\n'
    return stats


if __name__ == '__main__':
    start_time = time.time()
    args = parser.parse_args()

    if os.path.exists(args.model):
        asr_model = nemo_asr.models.EncDecCTCModel.restore_from(args.model, strict=False)
    elif args.model in nemo_asr.models.EncDecCTCModel.get_available_model_names():
        asr_model = nemo_asr.models.EncDecCTCModel.from_pretrained(args.model, strict=False)
    else:
        raise ValueError(
            f'Provide path to the pretrained checkpoint or choose from {nemo_asr.models.EncDecCTCModel.list_available_models()}'
        )

    alignment_files = Path(args.alignment)
    if os.path.isdir(args.alignment):
        alignment_files = alignment_files.glob("*.txt")
    else:
        alignment_files = [Path(alignment_files)]

    with open(os.path.join(args.output_dir, 'stats.tsv'), 'w') as f:
        f.write('Folder\tSegment\tOriginal dur (s)\tHigh quality dur (s)\tLow quality dur (s)\tDeleted dur (s)\n')

        for alignment_file in alignment_files:
            stats = process_alignment(alignment_file, args)
            f.write(stats)

        total_time = time.time() - start_time
        logging.info(f'Total execution time: ~{round(total_time / 60)}min')
        print(f'Total execution time: ~{round(total_time / 60)}min')