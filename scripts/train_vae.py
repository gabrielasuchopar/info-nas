import datetime
import json
import os
import pickle

import click
import torch
import torchvision

from info_nas.config import load_json_cfg
from info_nas.datasets.io.transforms import Scaler, IncludeBias, SortByWeights, ToTuple, load_scaler, after_scale_path, \
    get_transforms

from info_nas.datasets.arch2vec_dataset import get_labeled_unlabeled_datasets
from nasbench import api

from info_nas.trainer import train


#@click.option('--nasbench_path', default='../data/nasbench_only108.tfrecord')


@click.command()
@click.option('--train_path', default='../data/train_long.pt')
@click.option('--valid_path', default='../data/valid_long.pt')
@click.option('--scale_path', default='../data/scales/scale-train-include_bias.pickle')
@click.option('--scale_path_val', default='../data/scales/scale-valid-include_bias.pickle')
@click.option('--checkpoint_path', default='../data/vae_checkpoints/')
@click.option('--nasbench_path', default='../data/nasbench.pickle')
@click.option('--model_cfg', default=None)
@click.option('--include_bias/--no_bias', default=True)
@click.option('--normalize/--minmax', default=True)
@click.option('--axis', default=None, type=int)
@click.option('--after_axis', default=None, type=int)
@click.option('--scale_whole/--no_scale_whole', default=False)
@click.option('--use_ref/--no_ref', default=False)
@click.option('--device', default='cuda')
@click.option('--seed', default=1)
@click.option('--batch_size', default=32)
@click.option('--epochs', default=7)
def run(train_path, valid_path, scale_path, scale_path_val, checkpoint_path, nasbench_path, model_cfg, include_bias,
        normalize, axis, after_axis, scale_whole, use_ref, device, seed, batch_size, epochs):

    if nasbench_path.endswith('.pickle'):
        with open(nasbench_path, 'rb') as f:
            nb = pickle.load(f)
    else:
        nb = api.NASBench(nasbench_path)

    device = torch.device(device)

    labeled, unlabeled = get_labeled_unlabeled_datasets(nb, device=device, seed=seed,
                                                        train_pretrained=None,
                                                        valid_pretrained=None,
                                                        train_labeled_path=train_path,
                                                        valid_labeled_path=valid_path)

    transforms = get_transforms(scale_path, include_bias, axis, normalize,
                                scale_whole=scale_whole, axis_whole=after_axis)
    val_transforms = get_transforms(scale_path_val, include_bias, axis, normalize,
                                    scale_whole=scale_whole, axis_whole=after_axis)

    timestamp = datetime.datetime.now().strftime('%Y-%d-%m_%H-%M-%S')
    if not os.path.exists(checkpoint_path):
        os.mkdir(checkpoint_path)

    checkpoint_path = os.path.join(checkpoint_path, timestamp)
    os.mkdir(checkpoint_path)

    if model_cfg is not None:
        model_cfg = load_json_cfg(model_cfg)
        config_path = os.path.join(checkpoint_path, 'config.json')
        with open(config_path, 'w+') as f:
            json.dump(model_cfg, f, indent=4)

    model, metrics, loss = train(labeled, unlabeled, nb, transforms=transforms, valid_transforms=val_transforms,
                                 checkpoint_dir=checkpoint_path, device=device, use_reference_model=use_ref,
                                 batch_len_labeled=4, model_config=model_cfg,
                                 batch_size=batch_size, seed=seed, epochs=epochs)

    with open(os.path.join(checkpoint_path, 'metrics.pickle'), 'wb') as f:
        pickle.dump(metrics, f)

    with open(os.path.join(checkpoint_path, 'losses.pickle'), 'wb') as f:
        pickle.dump(loss, f)

    # TODO deterministic etc


if __name__ == "__main__":
    run()
