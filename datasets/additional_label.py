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


class AdditionalLabellDataset(torchdata.Dataset):
    def __init__(self, df: pd.DataFrame, tp: pd.DataFrame, fp: pd.DataFrame, datadir: Path,
                 waveform_transforms=None, spectrogram_transforms=None,
                 melspectrogram_parameters={},
                 pcen_parameters={},
                 sampling_rate=32000,
                 img_size=224,
                 duration=10,
                 centering=False,
                 additional_label_path=None,
                 additional_label_value=0.8):
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
        self.centering = centering
        if self.melspectrogram_parameters.get("hop_length") is not None:
            self.hop_length = self.melspectrogram_parameters["hop_length"]
        else:
            self.hop_length = 512
        if additional_label_path is not None:
            self.additional_labels = pd.read_csv(additional_label_path)
        self.additional_label_value = additional_label_value

    def __len__(self):
        return len(self.tp)

    def __getitem__(self, idx: int):
        sample = self.tp.loc[idx, :]
        index = sample["index"]
        flac_id = sample["recording_id"]

        t_min = sample["t_min"]
        t_max = sample["t_max"]

        if not self.centering:
            offset = np.random.choice(np.arange(max(t_max - self.duration, 0), t_min, 0.1))
            offset = min(CLIP_DURATION - self.duration, offset)
        else:
            call_duration = t_max - t_min
            relative_offset = (self.duration - call_duration) / 2
            offset = min(max(0, t_min - relative_offset), CLIP_DURATION - self.duration)
        y, sr = librosa.load(self.datadir / f"{flac_id}.flac",
                             sr=self.sampling_rate,
                             mono=True,
                             offset=offset,
                             duration=self.duration,
                             res_type="kaiser_fast")
        if self.waveform_transforms:
            y = self.waveform_transforms(y).astype(np.float32)

        melspec = librosa.feature.melspectrogram(y, sr=sr, **self.melspectrogram_parameters)
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

        if self.additional_labels is not None:
            additional_label = self.additional_labels.query(f"filename == '{flac_id}'").reset_index(drop=True)
            for _, row in additional_label.iterrows():
                label[int(row.species)] = self.additional_label_value

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
