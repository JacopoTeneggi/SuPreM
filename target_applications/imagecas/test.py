import argparse
import csv
import glob
import os
import time
import warnings
from collections import OrderedDict

import matplotlib.pyplot as plt
import nibabel as nib
import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from tqdm import tqdm

warnings.filterwarnings("ignore")

import torch.distributed as dist
from dataset.dataloader import get_loader, imagecas_taskmap_set
from model import configs, networkarch
from model.SwinUNETR import SwinUNETR
from model.unet3d import UNet3D
from monai.data import DistributedSampler, decollate_batch, load_decathlon_datalist
from monai.inferers import sliding_window_inference
from monai.losses import DiceCELoss
from monai.metrics import DiceMetric
from monai.networks.nets import SegResNet
from monai.transforms import AsDiscrete
from optimizers.lr_scheduler import LinearWarmupCosineAnnealingLR
from tensorboardX import SummaryWriter
from torch.nn.parallel import DistributedDataParallel

from utils.utils_test import (
    NUM_CLASS,
    TEMPLATE,
    check_data,
    dice_score,
    get_key,
    surface_dice,
)

torch.multiprocessing.set_sharing_strategy("file_system")

dice_metric = DiceMetric(
    include_background=True, reduction="mean_batch", get_not_nans=False
)
loss_function = DiceCELoss(to_onehot_y=True, softmax=True)


def validation(model, ValLoader, args):
    model.eval()
    dice_results = []
    nsd_results = []
    selected_class_map = imagecas_taskmap_set[args.map_type]
    post_label = AsDiscrete(to_onehot=args.num_class)
    post_pred = AsDiscrete(argmax=True, to_onehot=args.num_class)
    spacing_dict = {}
    for index, batch in enumerate(tqdm(ValLoader)):
        image, val_labels, name_list = (
            batch["image"].to(args.device),
            batch["label"].to(args.device),
            batch["name"],
        )
        # Loop over each name in name list
        for name in name_list:
            name = (
                name.item() if isinstance(name, torch.Tensor) else name
            )  # Convert to Python str if it's a Tensor
            # Iterate over each selected class
            for class_name in selected_class_map.values():
                # Construct the file path based on the class name and name
                file_path_pattern = os.path.join(
                    args.dataset_path, name, "segmentations", f"*{class_name}*.nii.gz"
                )
                # Use glob to find files matching the constructed file path
                for gt_path in glob.glob(file_path_pattern):
                    # Load the NIfTI file and extract the header information
                    gt_header = nib.load(gt_path).header
                    spacing = [
                        dim.item() for dim in gt_header["pixdim"][1:4]
                    ]  # Convert numpy.float32 to Python native float
                    # Store the spacing information in the dictionary with the compound key
                    spacing_dict[(name, class_name)] = spacing
        with torch.no_grad():
            # val_outputs = sliding_window_inference(image, (args.roi_x, args.roi_y, args.roi_z), 1, model, overlap=args.overlap, mode='gaussian')
            # if the gpu memory is not enough, you can try to use the following code as alternative
            val_outputs = sliding_window_inference(
                image,
                (args.roi_x, args.roi_y, args.roi_z),
                1,
                model,
                overlap=args.overlap,
                mode="gaussian",
                sw_device="cuda",
                device="cpu",
            )

        val_labels_list = decollate_batch(val_labels)
        # val_labels_convert = [post_label(val_label_tensor) for val_label_tensor in val_labels_list]
        # if the gpu memory is not enough, you can try to use the following code as alternative
        val_labels_convert = [
            post_label(val_label_tensor.cpu()) for val_label_tensor in val_labels_list
        ]

        val_outputs_list = decollate_batch(val_outputs)
        val_output_convert = [
            post_pred(val_pred_tensor) for val_pred_tensor in val_outputs_list
        ]

        for lbl, pred in zip(val_labels_convert, val_output_convert):
            case_name = (
                name[0] if isinstance(name, (list, tuple)) else name
            )  # Adjusting for the possibility that name is not a list.
            dice_case_result = {
                "name": case_name,
                "background": 1.0,
            }  # background class with dice 1.0
            nsd_case_result = {
                "name": case_name,
                "background": 1.0,
            }  # background class with dice 1.0
            for i, class_name in enumerate(selected_class_map.values(), start=1):
                # Check if (case_name, class_name) pair exists in spacing_dict, if not skip to next iteration
                if (case_name, class_name) not in spacing_dict:
                    print(
                        f"Skipping {case_name}, {class_name} as it does not exist in"
                        " spacing_dict"
                    )
                    continue
                spacing = spacing_dict[
                    (case_name, class_name)
                ]  # Retrieve the spacing information using the compound key
                dice, _, _ = dice_score(
                    pred[i], lbl[i]
                )  # unpack the returned tuple and only take the dice score
                nsd = surface_dice(
                    pred[i].cpu(), lbl[i].cpu(), spacing, 1
                )  # using retrieved spacing here
                nsd_case_result[class_name] = nsd if torch.sum(lbl[i]) != 0 else np.NaN
                dice = (
                    dice.item() if torch.is_tensor(dice) else dice
                )  # convert tensor to Python native data type if it's a tensor
                dice_case_result[class_name] = (
                    dice if torch.sum(lbl[i]) != 0 else np.NaN
                )
            dice_results.append(dice_case_result)
            nsd_results.append(nsd_case_result)
        dice_metric(y_pred=val_output_convert, y=val_labels_convert)

    output_directory = os.path.join("out", args.log_name)
    os.makedirs(output_directory, exist_ok=True)

    if dice_results:
        with open(
            os.path.join(
                output_directory,
                f"{args.pretraining_method_name}_{args.model_backbone}_{args.map_type}_dice_validation_results.csv",
            ),
            "w",
            newline="",
        ) as file:
            fieldnames = ["name", "background"] + list(selected_class_map.values())
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(dice_results)
    if nsd_results:
        with open(
            os.path.join(
                output_directory,
                f"{args.pretraining_method_name}_{args.model_backbone}_{args.map_type}_nsd_validation_results.csv",
            ),
            "w",
            newline="",
        ) as file:
            fieldnames = ["name", "background"] + list(selected_class_map.values())
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(nsd_results)
    dice_array = dice_metric.aggregate()
    print("This detailed dice array:", dice_array)
    mean_dice_val = dice_metric.aggregate().mean().item()
    print("This is mean dice array:", mean_dice_val)
    dice_metric.reset()

    return mean_dice_val, dice_array


def process(args):
    rank = 0

    dist.init_process_group(backend="nccl", init_method="env://")
    rank = dist.get_rank() % torch.cuda.device_count()
    args.device = torch.device(f"cuda:{rank}")
    torch.cuda.set_device(args.device)

    # prepare the 3D model
    if (
        args.model_backbone == "swintransformer"
        and args.pretraining_method_name == "smit"
    ):
        config = configs.get_SMIT_small_128_bias_True()
        model = networkarch.SMIT_3D_Seg(
            config,
            out_channels=args.num_class,
            pretrain=args.pretrain,
            pretrain_key="net",
        )
        args.roi_x, args.roi_y, args.roi_z = 128, 128, 128
        print(
            f"Use {args.pretraining_method_name} swintransformer backbone pretrained"
            " weights"
        )
    if args.model_backbone == "segresnet":
        model = SegResNet(
            blocks_down=[1, 2, 2, 4],
            blocks_up=[1, 1, 1],
            init_filters=16,
            in_channels=1,
            out_channels=args.num_class,
            dropout_prob=0.0,
        )
        store_dict = model.state_dict()
        model_dict = torch.load(args.pretrain)["net"]
        amount = 0
        for key in model_dict.keys():
            new_key = ".".join(key.split(".")[1:])
            if new_key in store_dict.keys():
                store_dict[new_key] = model_dict[key]
                amount += 1
        print(amount, len(store_dict.keys()))
        model.load_state_dict(store_dict)
        print(f"Load SegResNet transfer learning weights")

    if args.model_backbone == "swinunetr":
        model = SwinUNETR(
            img_size=(args.roi_x, args.roi_y, args.roi_z),
            in_channels=1,
            out_channels=args.num_class,
            feature_size=48,
            drop_rate=0.0,
            attn_drop_rate=0.0,
            dropout_path_rate=0.0,
            use_checkpoint=False,
        )
        store_dict = model.state_dict()
        model_dict = torch.load(args.pretrain)["net"]
        store_dict = model.state_dict()
        amount = 0
        for key in model_dict.keys():
            new_key = ".".join(key.split(".")[1:])
            if new_key in store_dict.keys():
                store_dict[new_key] = model_dict[key]
                amount += 1
        model.load_state_dict(store_dict)
        print(amount, len(store_dict.keys()))
        print(f"Load Swin UNETR transfer learning weights")

    if args.model_backbone == "unet":
        model = UNet3D(n_class=args.num_class)
        model_dict = torch.load(args.pretrain)["net"]
        store_dict = model.state_dict()
        amount = 0
        for key in model_dict.keys():
            new_key = ".".join(key.split(".")[1:])
            if new_key in store_dict.keys():
                store_dict[new_key] = model_dict[key]
                amount += 1
        model.load_state_dict(store_dict)
        print(amount, len(store_dict.keys()))
        print(f"Load Unet transfer learning weights")

    model.to(args.device)
    model = DistributedDataParallel(model, device_ids=[args.device])

    optimizer = torch.optim.AdamW(
        model.parameters(), lr=args.lr, weight_decay=args.weight_decay
    )
    scheduler = LinearWarmupCosineAnnealingLR(
        optimizer, warmup_epochs=args.warmup_epoch, max_epochs=args.max_epoch
    )

    if args.resume:
        checkpoint = torch.load(args.resume)
        model.load_state_dict(checkpoint["net"])
        optimizer.load_state_dict(checkpoint["optimizer"])
        args.epoch = checkpoint["epoch"]
        scheduler.load_state_dict(checkpoint["scheduler"])

        print("success resume from ", args.resume)

    torch.backends.cudnn.benchmark = True

    train_loader, train_sampler, val_loader, test_loader = get_loader(args)

    if rank == 0:
        if not os.path.isdir("out"):
            os.mkdir("out")
        if not os.path.isdir(os.path.join("out", args.log_name)):
            os.mkdir(os.path.join("out", args.log_name))

    mean_dice, std = validation(model, test_loader, args)
    print("Mean dice is:", mean_dice)
    dist.destroy_process_group()
    # assert 0 == 1


def main():
    parser = argparse.ArgumentParser()
    ## for distributed training
    parser.add_argument("--local-rank", type=int)
    parser.add_argument("--device")
    parser.add_argument("--epoch", default=0)
    ## logging
    parser.add_argument(
        "--log_name", default="...", help="The path resume from checkpoint"
    )
    parser.add_argument(
        "--pretraining_method_name", default="suprem", help="name of the model"
    )
    ## model load
    parser.add_argument(
        "--resume", default=None, help="The path resume from checkpoint"
    )
    parser.add_argument("--pretrain", default="...", help="The path of pretrain model")
    ## hyperparameter
    parser.add_argument(
        "--max_epoch", default=1, type=int, help="Number of training epoches"
    )
    parser.add_argument(
        "--warmup_epoch", default=20, type=int, help="number of warmup epochs"
    )
    parser.add_argument("--lr", default=1e-4, type=float, help="Learning rate")
    parser.add_argument("--weight_decay", default=1e-5, help="Weight Decay")
    # change here
    parser.add_argument(
        "--data_txt_path", default="./dataset/dataset_list/", help="data txt path"
    )
    # default =2
    parser.add_argument("--batch_size", default=2, type=int, help="batch size")
    parser.add_argument(
        "--num_workers", default=8, type=int, help="workers numebr for DataLoader"
    )
    parser.add_argument(
        "--a_min", default=-200, type=float, help="a_min in ScaleIntensityRanged"
    )
    parser.add_argument(
        "--a_max", default=800, type=float, help="a_max in ScaleIntensityRanged"
    )
    parser.add_argument(
        "--b_min", default=0.0, type=float, help="b_min in ScaleIntensityRanged"
    )
    parser.add_argument(
        "--b_max", default=1.0, type=float, help="b_max in ScaleIntensityRanged"
    )
    parser.add_argument(
        "--space_x", default=1.5, type=float, help="spacing in x direction"
    )
    parser.add_argument(
        "--space_y", default=1.5, type=float, help="spacing in y direction"
    )
    parser.add_argument(
        "--space_z", default=1.5, type=float, help="spacing in z direction"
    )
    parser.add_argument("--roi_x", default=96, type=int, help="roi size in x direction")
    parser.add_argument("--roi_y", default=96, type=int, help="roi size in y direction")
    parser.add_argument("--roi_z", default=96, type=int, help="roi size in z direction")
    parser.add_argument(
        "--num_samples", default=1, type=int, help="sample number in each ct"
    )
    parser.add_argument("--map_type", default="cas", help="depend on your target task")
    parser.add_argument(
        "--num_class",
        default=2,
        type=int,
        help="count of target task class + 1(background) ",
    )
    parser.add_argument(
        "--overlap",
        default=0.5,
        type=float,
        help="overlap for sliding_window_inference",
    )
    parser.add_argument("--dataset_path", default="...", help="dataset path")
    parser.add_argument(
        "--model_backbone",
        default="unet",
        help="model backbone:unet|swintransformer|segresnet|swinunetr",
    )
    parser.add_argument("--fold", default=1, type=int, help="data fold")
    args = parser.parse_args()

    process(args=args)


if __name__ == "__main__":
    main()
