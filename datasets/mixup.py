import cv2
import librosa
import numpy as np
import pandas as pd
import torch.utils.data as torchdata

from pathlib import Path

from .constants import N_CLASSES, CLIP_DURATION, CLASS_MAP


def normalize_melspec(X: np.ndarray):
    eps = 1e-6
    mean = X.mean()
    X = X - mean
    std = X.std()
    Xstd = X / (std + eps)
    norm_min, norm_max = Xstd.min(), Xstd.max()
    if (norm_max - norm_min) > eps:
        V = Xstd
        V[V < norm_min] = norm_min
        V[V > norm_max] = norm_max
        V = 255 * (V - norm_min) / (norm_max - norm_min)
        V = V.astype(np.uint8)
    else:
        # Just zero
        V = np.zeros_like(Xstd, dtype=np.uint8)
    return V


class WaveformMixupDataset(torchdata.Dataset):
    def __init__(self, df: pd.DataFrame, tp: pd.DataFrame, fp: pd.DataFrame, datadir: Path,
                 waveform_transforms=None, spectrogram_transforms=None,
                 melspectrogram_parameters={},
                 pcen_parameters={},
                 sampling_rate=32000,
                 img_size=224,
                 duration=10,
                 mixup_prob=0.5,
                 mixup_alpha=5,
                 float_label=True):
        unique_recording_id = df.recording_id.unique().tolist()
        unique_tp_recordin_id = tp.recording_id.unique().tolist()
        intersection = set(unique_recording_id).intersection(unique_tp_recordin_id)
        self.df = df[df.recording_id.isin(intersection)].reset_index(drop=True)
        self.tp = tp[tp.recording_id.isin(intersection)].reset_index(drop=True)
        self.fp = fp  # unused
        self.datadir = datadir
        self.waveform_transforms = waveform_transforms
        self.spectrogram_transforms = spectrogram_transforms
        self.melspectrogram_parameters = melspectrogram_parameters
        self.pcen_parameters = pcen_parameters
        self.sampling_rate = sampling_rate
        self.img_size = img_size
        self.duration = duration
        self.mixup_prob = mixup_prob
        self.mixup_alpha = mixup_alpha
        self.float_label = float_label

        if len(list(datadir.glob("*.flac"))) == 0:
            self.suffix = ".wav"
        else:
            self.suffix = ".flac"

    def __len__(self):
        return len(self.tp)

    def __getitem__(self, idx: int):
        sample = self.tp.loc[idx, :]
        index = sample["index"]
        flac_id = sample["recording_id"]

        t_min = sample["t_min"]
        t_max = sample["t_max"]

        call_duration = t_max - t_min
        if call_duration > self.duration:
            offset = np.random.choice(np.arange(max(t_min - call_duration / 2, 0), t_min + call_duration / 2, 0.1))
            offset = min(CLIP_DURATION - self.duration, offset)
        else:
            offset = np.random.choice(np.arange(max(t_max - self.duration, 0), t_min, 0.1))
            offset = min(CLIP_DURATION - self.duration, offset)

        y, sr = librosa.load(self.datadir / f"{flac_id}{self.suffix}",
                             sr=self.sampling_rate,
                             mono=True,
                             offset=offset,
                             duration=self.duration)
        if self.waveform_transforms:
            y = self.waveform_transforms(y).astype(np.float32)

        melspec = librosa.feature.melspectrogram(y, sr=sr, **self.melspectrogram_parameters)

        use_mixup = False
        if np.random.rand() < self.mixup_prob:
            use_mixup = True
            while True:
                mixup_sample = self.tp.sample(1).reset_index(drop=True).loc[0]
                if mixup_sample["index"] != index:
                    break
            mixup_flac_id = mixup_sample["recording_id"]
            mixup_t_min = mixup_sample["t_min"]
            mixup_t_max = mixup_sample["t_max"]

            mixup_offset = np.random.choice(np.arange(
                max(mixup_t_max - self.duration, 0), mixup_t_min, 0.1))
            mixup_offset = min(CLIP_DURATION - self.duration, mixup_offset)

            y_mixup, _ = librosa.load(self.datadir / f"{mixup_flac_id}{self.suffix}",
                                      sr=self.sampling_rate,
                                      mono=True,
                                      offset=mixup_offset,
                                      duration=self.duration)
            if self.waveform_transforms:
                y_mixup = self.waveform_transforms(y_mixup).astype(np.float32)
            y_mixup = librosa.util.normalize(y_mixup)

            lam = np.random.beta(self.mixup_alpha, self.mixup_alpha)
            y_mixed = lam * y + (1 - lam) * y_mixup
            melspec = librosa.feature.melspectrogram(y_mixed, sr=sr, **self.melspectrogram_parameters)

        pcen = librosa.pcen(melspec, sr=sr, **self.pcen_parameters)
        clean_mel = librosa.power_to_db(melspec ** 1.5)
        melspec = librosa.power_to_db(melspec)

        if self.spectrogram_transforms:
            melspec = self.spectrogram_transforms(image=melspec)["image"]
            pcen = self.spectrogram_transforms(image=pcen)["image"]
            clean_mel = self.spectrogram_transforms(image=clean_mel)["image"]
        else:
            pass

        norm_melspec = normalize_melspec(melspec)
        norm_pcen = normalize_melspec(pcen)
        norm_clean_mel = normalize_melspec(clean_mel)
        image = np.stack([norm_melspec, norm_pcen, norm_clean_mel], axis=-1)

        height, width, _ = image.shape
        image = cv2.resize(image, (int(width * self.img_size / height), self.img_size))
        image = np.moveaxis(image, 2, 0)
        image = (image / 255.0).astype(np.float32)

        tail = offset + self.duration
        query_string = f"recording_id == '{flac_id}' & "
        query_string += f"t_min < {tail} & t_max > {offset}"
        all_tp_events = self.tp.query(query_string)

        if use_mixup:
            mixup_tail = mixup_offset + self.duration
            query_string = f"recording_id == '{mixup_flac_id}' & "
            query_string += f"t_min < {mixup_tail} & t_max > {mixup_offset}"
            mixup_tp_events = self.tp.query(query_string)

        label = np.zeros(N_CLASSES, dtype=np.float32)

        n_frames = image.shape[2]
        seconds_per_frame = self.duration / n_frames
        strong_label = np.zeros((n_frames, N_CLASSES), dtype=np.float32)

        for species_id in all_tp_events["species_id"].unique():
            if self.float_label and use_mixup:
                label[int(species_id)] = lam
            else:
                label[int(species_id)] = 1.0

        for _, row in all_tp_events.iterrows():
            t_min = row.t_min
            t_max = row.t_max
            species_id = row.species_id

            start_index = int((t_min - offset) / seconds_per_frame)
            end_index = int((t_max - offset) / seconds_per_frame)

            if self.float_label and use_mixup:
                strong_label[start_index:end_index, species_id] = lam
            else:
                strong_label[start_index:end_index, species_id] = 1.0

        if use_mixup:
            for species_id in mixup_tp_events["species_id"].unique():
                if self.float_label:
                    label[int(species_id)] = (1 - lam)
                else:
                    label[int(species_id)] = 1.0

            for _, row in mixup_tp_events.iterrows():
                t_min = row.t_min
                t_max = row.t_max
                species_id = row.species_id

                start_index = int((t_min - mixup_offset) / seconds_per_frame)
                end_index = int((t_max - mixup_offset) / seconds_per_frame)

                if self.float_label:
                    strong_label[start_index:end_index, species_id] = 1 - lam
                else:
                    strong_label[start_index:end_index, species_id] = 1.0

        return {
            "recording_id": flac_id,
            "image": image,
            "targets": {
                "weak": label,
                "strong": strong_label
            },
            "index": index
        }


class LogmelMixupDataset(torchdata.Dataset):
    def __init__(self, df: pd.DataFrame, tp: pd.DataFrame, fp: pd.DataFrame, datadir: Path,
                 waveform_transforms=None, spectrogram_transforms=None,
                 melspectrogram_parameters={},
                 pcen_parameters={},
                 sampling_rate=32000,
                 img_size=224,
                 duration=10,
                 mixup_prob=0.5,
                 mixup_alpha=5,
                 float_label=False,
                 no_lambda=False):
        unique_recording_id = df.recording_id.unique().tolist()
        unique_tp_recordin_id = tp.recording_id.unique().tolist()
        intersection = set(unique_recording_id).intersection(unique_tp_recordin_id)
        self.df = df[df.recording_id.isin(intersection)].reset_index(drop=True)
        self.tp = tp[tp.recording_id.isin(intersection)].reset_index(drop=True)
        self.fp = fp  # unused
        self.datadir = datadir
        self.waveform_transforms = waveform_transforms
        self.spectrogram_transforms = spectrogram_transforms
        self.melspectrogram_parameters = melspectrogram_parameters
        self.pcen_parameters = pcen_parameters
        self.sampling_rate = sampling_rate
        self.img_size = img_size
        self.duration = duration
        self.mixup_prob = mixup_prob
        self.mixup_alpha = mixup_alpha
        self.float_label = float_label
        self.no_lambda = no_lambda

        if len(list(datadir.glob("*.flac"))) == 0:
            self.suffix = ".wav"
        else:
            self.suffix = ".flac"

    def __len__(self):
        return len(self.tp)

    def __getitem__(self, idx: int):
        sample = self.tp.loc[idx, :]
        index = sample["index"]
        flac_id = sample["recording_id"]

        t_min = sample["t_min"]
        t_max = sample["t_max"]

        call_duration = t_max - t_min
        if call_duration > self.duration:
            offset = np.random.choice(np.arange(max(t_min - call_duration / 2, 0), t_min + call_duration / 2, 0.1))
            offset = min(CLIP_DURATION - self.duration, offset)
        else:
            offset = np.random.choice(np.arange(max(t_max - self.duration, 0), t_min, 0.1))
            offset = min(CLIP_DURATION - self.duration, offset)

        y, sr = librosa.load(self.datadir / f"{flac_id}{self.suffix}",
                             sr=self.sampling_rate,
                             mono=True,
                             offset=offset,
                             duration=self.duration)
        if self.waveform_transforms:
            y = self.waveform_transforms(y).astype(np.float32)

        melspec = librosa.feature.melspectrogram(y, sr=sr, **self.melspectrogram_parameters)

        use_mixup = False
        if np.random.rand() < self.mixup_prob:
            use_mixup = True
            while True:
                mixup_sample = self.tp.sample(1).reset_index(drop=True).loc[0]
                if mixup_sample["index"] != index:
                    break
            mixup_flac_id = mixup_sample["recording_id"]
            mixup_t_min = mixup_sample["t_min"]
            mixup_t_max = mixup_sample["t_max"]

            mixup_offset = np.random.choice(np.arange(
                max(mixup_t_max - self.duration, 0), mixup_t_min, 0.1))
            mixup_offset = min(CLIP_DURATION - self.duration, mixup_offset)

            y_mixup, _ = librosa.load(self.datadir / f"{mixup_flac_id}{self.suffix}",
                                      sr=self.sampling_rate,
                                      mono=True,
                                      offset=mixup_offset,
                                      duration=self.duration)
            if self.waveform_transforms:
                y_mixup = self.waveform_transforms(y_mixup).astype(np.float32)
            mixup_melspec = librosa.feature.melspectrogram(
                y_mixup, sr=self.sampling_rate, **self.melspectrogram_parameters)

            if self.no_lambda:
                melspec = melspec + mixup_melspec
            else:
                lam = np.random.beta(self.mixup_alpha, self.mixup_alpha)
                melspec = lam * melspec + (1 - lam) * mixup_melspec

        pcen = librosa.pcen(melspec, sr=sr, **self.pcen_parameters)
        clean_mel = librosa.power_to_db(melspec ** 1.5)
        melspec = librosa.power_to_db(melspec)

        if self.spectrogram_transforms:
            melspec = self.spectrogram_transforms(image=melspec)["image"]
            pcen = self.spectrogram_transforms(image=pcen)["image"]
            clean_mel = self.spectrogram_transforms(image=clean_mel)["image"]
        else:
            pass

        norm_melspec = normalize_melspec(melspec)
        norm_pcen = normalize_melspec(pcen)
        norm_clean_mel = normalize_melspec(clean_mel)
        image = np.stack([norm_melspec, norm_pcen, norm_clean_mel], axis=-1)

        height, width, _ = image.shape
        if isinstance(self.img_size, int):
            image = cv2.resize(image, (int(width * self.img_size / height), self.img_size))
        else:
            image = cv2.resize(image, tuple(self.img_size))
        image = np.moveaxis(image, 2, 0)
        image = (image / 255.0).astype(np.float32)

        tail = offset + self.duration
        query_string = f"recording_id == '{flac_id}' & "
        query_string += f"t_min < {tail} & t_max > {offset}"
        all_tp_events = self.tp.query(query_string)

        if use_mixup:
            mixup_tail = mixup_offset + self.duration
            query_string = f"recording_id == '{mixup_flac_id}' & "
            query_string += f"t_min < {mixup_tail} & t_max > {mixup_offset}"
            mixup_tp_events = self.tp.query(query_string)

        label = np.zeros(N_CLASSES, dtype=np.float32)
        n_frames = image.shape[2]
        seconds_per_frame = self.duration / n_frames
        strong_label = np.zeros((n_frames, N_CLASSES), dtype=np.float32)

        for species_id in all_tp_events["species_id"].unique():
            if self.float_label and use_mixup and not self.no_lambda:
                label[int(species_id)] = lam
            else:
                label[int(species_id)] = 1.0

        for _, row in all_tp_events.iterrows():
            t_min = row.t_min
            t_max = row.t_max
            species_id = row.species_id

            start_index = int((t_min - offset) / seconds_per_frame)
            end_index = int((t_max - offset) / seconds_per_frame)

            if self.float_label and use_mixup and not self.no_lambda:
                strong_label[start_index:end_index, species_id] = lam
            else:
                strong_label[start_index:end_index, species_id] = 1.0

        if use_mixup:
            for species_id in mixup_tp_events["species_id"].unique():
                if self.float_label and not self.no_lambda:
                    label[int(species_id)] = (1 - lam)
                else:
                    label[int(species_id)] = 1.0

            for _, row in mixup_tp_events.iterrows():
                t_min = row.t_min
                t_max = row.t_max
                species_id = row.species_id

                start_index = int((t_min - mixup_offset) / seconds_per_frame)
                end_index = int((t_max - mixup_offset) / seconds_per_frame)

                if self.float_label and not self.no_lambda:
                    strong_label[start_index:end_index, species_id] = 1 - lam
                else:
                    strong_label[start_index:end_index, species_id] = 1.0

        return {
            "recording_id": flac_id,
            "image": image,
            "targets": {
                "weak": label,
                "strong": strong_label
            },
            "index": index
        }


class LogmelMixupWithFPDataset(torchdata.Dataset):
    def __init__(self, df: pd.DataFrame, tp: pd.DataFrame, fp: pd.DataFrame, datadir: Path,
                 waveform_transforms=None, spectrogram_transforms=None,
                 melspectrogram_parameters={},
                 pcen_parameters={},
                 sampling_rate=32000,
                 img_size=224,
                 duration=10,
                 mixup_prob=0.5,
                 mixup_alpha=5):
        unique_recording_id = df.recording_id.unique().tolist()
        unique_tp_recording_id = tp.recording_id.unique().tolist()
        unique_fp_recording_id = fp.recording_id.unique().tolist()
        intersection_tp = set(unique_recording_id).intersection(unique_tp_recording_id)
        intersection_fp = set(unique_recording_id).intersection(unique_fp_recording_id)
        intersection_fp_without_tp = intersection_fp - intersection_tp
        self.tp = tp[tp.recording_id.isin(intersection_tp)].reset_index(drop=True)
        self.fp = fp[fp.recording_id.isin(intersection_fp_without_tp)].reset_index(drop=True)
        self.datadir = datadir
        self.waveform_transforms = waveform_transforms
        self.spectrogram_transforms = spectrogram_transforms
        self.melspectrogram_parameters = melspectrogram_parameters
        self.pcen_parameters = pcen_parameters
        self.sampling_rate = sampling_rate
        self.img_size = img_size
        self.duration = duration
        self.mixup_prob = mixup_prob
        self.mixup_alpha = mixup_alpha

    def __len__(self):
        return len(self.tp)

    def __getitem__(self, idx: int):
        sample = self.tp.loc[idx, :]
        index = sample["index"]
        flac_id = sample["recording_id"]

        t_min = sample["t_min"]
        t_max = sample["t_max"]

        call_duration = t_max - t_min
        if call_duration > self.duration:
            offset = np.random.choice(np.arange(max(t_min - call_duration / 2, 0), t_min + call_duration / 2, 0.1))
            offset = min(CLIP_DURATION - self.duration, offset)
        else:
            offset = np.random.choice(np.arange(max(t_max - self.duration, 0), t_min, 0.1))
            offset = min(CLIP_DURATION - self.duration, offset)

        y, sr = librosa.load(self.datadir / f"{flac_id}.wav",
                             sr=self.sampling_rate,
                             mono=True,
                             offset=offset,
                             duration=self.duration)
        if self.waveform_transforms:
            y = self.waveform_transforms(y).astype(np.float32)

        melspec = librosa.feature.melspectrogram(y, sr=sr, **self.melspectrogram_parameters)

        if np.random.rand() < self.mixup_prob:
            while True:
                mixup_sample = self.fp.sample(1).reset_index(drop=True).loc[0]
                if mixup_sample["index"] != index:
                    break
            mixup_flac_id = mixup_sample["recording_id"]

            mixup_offset = np.random.choice(np.arange(
                0, CLIP_DURATION - self.duration, 0.1))

            y_mixup, _ = librosa.load(self.datadir / f"{mixup_flac_id}.wav",
                                      sr=self.sampling_rate,
                                      mono=True,
                                      offset=mixup_offset,
                                      duration=self.duration)
            if self.waveform_transforms:
                y_mixup = self.waveform_transforms(y_mixup).astype(np.float32)
            mixup_melspec = librosa.feature.melspectrogram(
                y_mixup, sr=self.sampling_rate, **self.melspectrogram_parameters)

            lam = np.random.beta(self.mixup_alpha, self.mixup_alpha)
            melspec = lam * melspec + (1 - lam) * mixup_melspec

        pcen = librosa.pcen(melspec, sr=sr, **self.pcen_parameters)
        clean_mel = librosa.power_to_db(melspec ** 1.5)
        melspec = librosa.power_to_db(melspec)

        if self.spectrogram_transforms:
            melspec = self.spectrogram_transforms(image=melspec)["image"]
            pcen = self.spectrogram_transforms(image=pcen)["image"]
            clean_mel = self.spectrogram_transforms(image=clean_mel)["image"]
        else:
            pass

        norm_melspec = normalize_melspec(melspec)
        norm_pcen = normalize_melspec(pcen)
        norm_clean_mel = normalize_melspec(clean_mel)
        image = np.stack([norm_melspec, norm_pcen, norm_clean_mel], axis=-1)

        height, width, _ = image.shape
        image = cv2.resize(image, (int(width * self.img_size / height), self.img_size))
        image = np.moveaxis(image, 2, 0)
        image = (image / 255.0).astype(np.float32)

        tail = offset + self.duration
        query_string = f"recording_id == '{flac_id}' & "
        query_string += f"t_min < {tail} & t_max > {offset}"
        all_tp_events = self.tp.query(query_string)

        label = np.zeros(N_CLASSES, dtype=np.float32)
        songtype_label = np.zeros(N_CLASSES + 2, dtype=np.float32)

        n_frames = image.shape[2]
        seconds_per_frame = self.duration / n_frames
        strong_label = np.zeros((n_frames, N_CLASSES), dtype=np.float32)
        songtype_strong_label = np.zeros((n_frames, N_CLASSES + 2), dtype=np.float32)

        for species_id in all_tp_events["species_id"].unique():
            label[int(species_id)] = 1.0

        for species_id_song_id in all_tp_events["species_id_song_id"].unique():
            songtype_label[CLASS_MAP[species_id_song_id]] = 1.0

        for _, row in all_tp_events.iterrows():
            t_min = row.t_min
            t_max = row.t_max
            species_id = row.species_id
            species_id_song_id = row.species_id_song_id

            start_index = int((t_min - offset) / seconds_per_frame)
            end_index = int((t_max - offset) / seconds_per_frame)

            strong_label[start_index:end_index, species_id] = 1.0
            songtype_strong_label[start_index:end_index, CLASS_MAP[species_id_song_id]] = 1.0

        return {
            "recording_id": flac_id,
            "image": image,
            "targets": {
                "weak": label,
                "strong": strong_label,
                "weak_songtype": songtype_label,
                "strong_songtype": songtype_strong_label
            },
            "index": index
        }
