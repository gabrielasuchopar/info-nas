# TODO napsat že source je arch2vec s úpravama víceméně
import os

import numpy as np
import random
import torch
import torch.backends.cudnn
from info_nas.eval import mean_losses, eval_epoch, init_stats_dict
from torch.utils.tensorboard import SummaryWriter

from arch2vec.models.model import VAEReconstructed_Loss
from info_nas.models.utils import save_extended_vae, get_optimizer
from torch import nn

from arch2vec.extensions.get_nasbench101_model import get_arch2vec_model
from arch2vec.utils import preprocessing, save_checkpoint_vae
from arch2vec.models.configs import configs

from info_nas.datasets.io.semi_dataset import get_train_valid_datasets
from info_nas.models.io_model import model_dict
from info_nas.config import local_model_cfg, load_json_cfg
from info_nas.models.losses import losses_dict


def train(labeled, unlabeled, nasbench, checkpoint_dir, transforms=None, valid_transforms=None,
          use_reference_model=False, model_config=None, device=None,
          batch_size=32, seed=1, epochs=8, writer=None, verbose=2, print_frequency=1000,
          batch_len_labeled=4, torch_deterministic=False, cudnn_deterministic=False):

    config, model_config = _init_config_and_seeds(model_config, seed, torch_deterministic, cudnn_deterministic)

    # TODO finish writer
    if writer is not None:
        writer = SummaryWriter(writer)

    # init dataset
    train_dataset, valid_labeled, valid_unlabeled = get_train_valid_datasets(
        labeled, unlabeled, batch_size=batch_size, labeled_transforms=transforms,
        labeled_val_transforms=valid_transforms, **model_config['dataset_config']
    )
    dataset_len = len(train_dataset)

    # init models
    if not labeled['train']['use_reference']:
        in_channels = labeled['train']['inputs'].shape[1]
    else:
        in_channels = labeled['train']['dataset'].shape[1]

    # init models
    model, optimizer = get_arch2vec_model(device=device)
    model_labeled, optimizer_labeled = _initialize_labeled_model(model, in_channels, device=device,
                                                                 model_config=model_config)

    if use_reference_model:
        model_ref, optimizer_ref = get_arch2vec_model(device=device)
        model_ref.load_state_dict(model.state_dict())
    else:
        model_ref = None

    # init losses and logs
    loss_func_vae = VAEReconstructed_Loss(**config['loss'])
    loss_func_labeled = losses_dict[model_config['loss']]

    # stats for all three model variants (labeled, unlabeled, reference)
    loss_lists_total = init_stats_dict('loss')
    metrics_total = init_stats_dict('metrics')

    for epoch in range(epochs):
        model.train()
        model_labeled.train()

        n_labeled_batches, n_unlabeled_batches = 0, 0
        loss_lists_epoch = init_stats_dict('loss')
        Z = init_stats_dict()

        for i, batch in enumerate(train_dataset):
            # determine if labeled/unlabeled batch
            if len(batch) == 2:
                extended_model = model
                extended_optim = optimizer
                loss_list = loss_lists_epoch['unlabeled']
                Z_list = Z['unlabeled']
                is_labeled = False

                n_unlabeled_batches += 1

            elif len(batch) == batch_len_labeled:
                extended_model = model_labeled
                extended_optim = optimizer_labeled
                loss_list = loss_lists_epoch['labeled']
                Z_list = Z['labeled']
                is_labeled = True

                n_labeled_batches += 1
            else:
                raise ValueError(f"Invalid dataset - batch has {len(batch)} items, supported is 2 or "
                                 f"{batch_len_labeled}.")

            # train models
            _train_on_batch(extended_model, batch, extended_optim, device, config, loss_func_vae, loss_func_labeled,
                            loss_list, Z_list, eval_labeled=is_labeled)
            if use_reference_model:
                _train_on_batch(model_ref, batch, optimizer_ref, device, config, loss_func_vae, loss_func_labeled,
                                loss_lists_epoch['reference'], Z['reference'], eval_labeled=False)

            # batch stats
            if verbose > 0 and i % print_frequency == 0:
                print(f'epoch {epoch}: batch {i} / {dataset_len}: ')
                for key, losses in loss_lists_epoch.items():
                    losses = ", ".join([f"{k}: {v}" for k, v in mean_losses(losses).items()])
                    print(f"\t {key}: {losses}")

                print(f'\t labeled batches: {n_labeled_batches}, unlabeled batches: {n_unlabeled_batches}')

        # epoch stats
        make_checkpoint = 'checkpoint' in model_config and epoch % model_config['checkpoint'] == 0
        if epoch == epochs - 1 or make_checkpoint:
            save_extended_vae(checkpoint_dir, model_labeled, optimizer_labeled, epoch,
                              model_config['model_class'], model_config['model_kwargs'])

            # keep the original function signature, save what I need
            orig_path = os.path.join(checkpoint_dir, f"model_orig_epoch-{epoch}.pt")
            save_checkpoint_vae(model, optimizer, epoch, None, None, None, None, None, f_path=orig_path)

            if use_reference_model:
                orig_path = os.path.join(checkpoint_dir, f"model_ref_epoch-{epoch}.pt")
                save_checkpoint_vae(model_ref, optimizer_ref, epoch, None, None, None, None, None, f_path=orig_path)

            # TODO checkpoint metrics
            #  - save to pandas

        eval_epoch(model, model_labeled, model_ref, metrics_total, Z, loss_lists_total, loss_lists_epoch, epoch,
                   device, nasbench, valid_unlabeled, valid_labeled, config, loss_func_labeled, verbose=verbose)

        # TODO tensorboard?

    # TODO lepší zaznamenání výsledků
    return model_labeled, metrics_total, loss_lists_total


def _initialize_labeled_model(model, in_channels, model_config=None, device=None):
    model_class = model_dict[model_config['model_class']]

    model = model_class(model, in_channels, model_config['out_channels'], **model_config['model_kwargs'])
    if device is not None:
        model = model.to(device)

    optimizer = get_optimizer(model, **model_config['optimizer'])

    return model, optimizer


def _forward_batch(model, adj, ops, inputs=None):
    # forward
    if inputs is None:
        # unlabeled (original model)
        model_out = model(ops, adj.to(torch.long))
    else:
        # labeled (extended model)
        model_out = model(ops, adj.to(torch.long), inputs)

    return model_out


def _eval_batch(model_out, adj, ops, prep_reverse, loss, loss_labeled, loss_history, outputs=None):
    ops_recon, adj_recon, mu, logvar = model_out[:4]

    adj_recon, ops_recon = prep_reverse(adj_recon, ops_recon)
    adj, ops = prep_reverse(adj, ops)

    if outputs is not None:
        assert len(model_out) == 6  # TODO could differ
        outs_recon = model_out[-1]

        labeled_out = loss_labeled(outs_recon, outputs)
    else:
        labeled_out = None

    vae_out = loss((ops_recon, adj_recon), (ops, adj), mu, logvar)
    total_out = vae_out + labeled_out if labeled_out is not None else vae_out

    loss_history['total'].append(total_out.item())
    loss_history['unlabeled'].append(vae_out.item())
    if labeled_out is not None:
        loss_history['labeled'].append(labeled_out.item())

    return total_out


def _train_on_batch(model, batch, optimizer, device, config, loss_func_vae, loss_func_labeled, loss_list, Z,
                    eval_labeled=False):

    optimizer.zero_grad()

    # adj, ops preprocessing
    adj, ops = batch[0], batch[1]
    adj, ops = adj.to(device), ops.to(device)
    adj, ops, prep_reverse = preprocessing(adj, ops, **config['prep'])

    # labeled vs unlabeled batches
    if eval_labeled:
        inputs, outputs = batch[2].to(device), batch[3].to(device)
    else:
        inputs, outputs = None, None

    # forward
    model_out = _forward_batch(model, adj, ops, inputs=inputs)
    mu = model_out[2]
    Z.append(mu.cpu())

    loss_out = _eval_batch(model_out, adj, ops, prep_reverse, loss_func_vae, loss_func_labeled,
                           loss_list, outputs=outputs)

    loss_out.backward()

    nn.utils.clip_grad_norm_(model.parameters(), 5)
    optimizer.step()


def _init_config_and_seeds(model_config, seed, torch_deterministic, cudnn_deterministic):
    # io model config
    if model_config is None:
        model_config = local_model_cfg
    elif isinstance(model_config, str):
        model_config = load_json_cfg(model_config)

    # arch2vec config
    config = configs[model_config['arch2vec_config']]

    if torch_deterministic:
        torch.use_deterministic_algorithms(True)

    if cudnn_deterministic:
        torch.backends.cudnn.deterministic = True

    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    np.random.seed(seed)

    return config, model_config
