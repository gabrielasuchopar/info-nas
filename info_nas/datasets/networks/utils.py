import os
import torch
from nasbench import api
from nasbench_pytorch.model import Network as NBNetwork


def get_net_from_hash(hash, nasbench):
    m = nasbench.get_metrics_from_hash(hash)

    ops = m[0]['module_operations']
    adjacency = m[0]['module_adjacency']

    return ops, adjacency


def load_nasbench(nasbench_path, include_metrics=False):
    """
    Load all networks in the nasbench dataset along with their hashes.

    Args:
        nasbench_path: Path to the nasbench dataset.
        include_metrics: Include also accuracy and other metrics (not implemented yet).

    Returns: The loaded dataset.

    """
    if include_metrics:
        raise NotImplementedError("Metrics are not supported yet.")

    nasbench = api.NASBench(nasbench_path)

    data = []

    for _, h in enumerate(nasbench.hash_iterator()):
        ops, adjacency = get_net_from_hash(h, nasbench)
        data.append((h, ops, adjacency))

    return data


def is_net_pretrained(net_hash, dir_path='./checkpoints/'):
    return os.path.exists(os.path.join(dir_path, f'{net_hash}.tar'))


def save_trained_net(net_hash, net, dir_path='./checkpoints/', info=None, net_args=None, net_kwargs=None):
    if not os.path.exists(dir_path):
        os.mkdir(dir_path)

    checkpoint_dict = {
        'hash': net_hash,
        'model_state_dict': net.state_dict(),
        'args': net_args,
        'kwargs': net_kwargs,
        'info': info
    }

    torch.save(checkpoint_dict, os.path.join(dir_path, f'{net_hash}.tar'))


def load_trained_net(net_path, nasbench, device=None):
    checkpoint = torch.load(net_path, map_location=device)

    net_m = nasbench.get_metrics_from_hash(checkpoint['hash'])

    ops = net_m[0]['module_operations']
    adjacency = net_m[0]['module_adjacency']

    args = checkpoint['args'] if checkpoint['args'] is not None else []
    kwargs = checkpoint['kwargs'] if checkpoint['kwargs'] is not None else {}

    net = NBNetwork((adjacency, ops), *args, **kwargs)
    net.load_state_dict(checkpoint['model_state_dict'])
    net = net.to(device)

    return checkpoint['hash'], net, checkpoint['info']
