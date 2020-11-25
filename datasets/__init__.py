import pandas as pd
import torch.utils.data as torchdata

import transforms

from pathlib import Path

from .pytorch import WaveformDataset, WaveformValidDataset


__DATASETS__ = {
    "WaveformDataset": WaveformDataset,
    "WaveformValidDataset": WaveformValidDataset
}


def get_metadata(config: dict):
    data_config = config["data"]

    tp = pd.read_csv(data_config["train_tp_path"])
    fp = pd.read_csv(data_config["train_fp_path"])
    train_audio = Path(data_config["train_audio_path"])
    test_audio = Path(data_config["test_audio_path"])

    train_flacs = list(train_audio.glob("*.flac"))
    test_flacs = list(test_audio.glob("*.flac"))
    train_all = pd.DataFrame({
        "recording_id": train_flacs
    })
    test_all = pd.DataFrame({
        "recording_id": test_flacs
    })
    clip_level_tp = tp.groupby("recording_id")["species_id"].apply(list)
    clip_level_fp = tp.groupby("recording_id")["species_id"].apply(list)

    tp["species_id_song_id"] = tp["species_id"].map(str) + "_" + tp["songtype_id"].map(str)
    fp["species_id_song_id"] = fp["species_id"].map(str) + "_" + fp["songtype_id"].map(str)

    clip_level_tp_joint = tp.groupby("recording_id")["species_id_song_id"].apply(list)
    clip_level_fp_joint = fp.groupby("recording_id")["species_id_song_id"].apply(list)

    train_all = train_all.merge(clip_level_tp, on="recording_id", how="left")
    train_all = train_all.merge(clip_level_fp, on="recording_id", how="left")
    train_all = train_all.merge(clip_level_tp_joint, on="recording_id", how="left")
    train_all = train_all.merge(clip_level_fp_joint, on="recording_id", how="left")

    return tp, fp, train_all, test_all, train_audio, test_audio


def get_train_loader(df: pd.DataFrame,
                     tp: pd.DataFrame,
                     fp: pd.DataFrame,
                     datadir: Path,
                     config: dict,
                     phase: str):
    dataset_config = config["dataset"]
    loader_config = config["loader"][phase]
    if dataset_config[phase]["name"] in ["WaveformDataset", "WaveformValidDataset"]:
        transform = transforms.get_waveform_transforms(config, phase)
        params = dataset_config[phase]["params"]

        dataset = __DATASETS__[dataset_config[phase]["name"]](
            df, tp, fp, datadir, transform, **params)
    else:
        raise NotImplementedError
    loader = torchdata.DataLoader(dataset, **loader_config)
    return loader