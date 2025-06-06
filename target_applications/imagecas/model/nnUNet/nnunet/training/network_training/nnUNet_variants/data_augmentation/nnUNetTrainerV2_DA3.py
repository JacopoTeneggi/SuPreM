#    Copyright 2020 Division of Medical Image Computing, German Cancer Research Center (DKFZ), Heidelberg, Germany
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
import numpy as np
import torch
from batchgenerators.dataloading import MultiThreadedAugmenter, SingleThreadedAugmenter
from batchgenerators.transforms import (
    BrightnessMultiplicativeTransform,
    BrightnessTransform,
    Compose,
    ContrastAugmentationTransform,
    DataChannelSelectionTransform,
    GammaTransform,
    GaussianBlurTransform,
    GaussianNoiseTransform,
    MirrorTransform,
    SegChannelSelectionTransform,
    SimulateLowResolutionTransform,
)
from batchgenerators.transforms.spatial_transforms import SpatialTransform_2
from batchgenerators.transforms.utility_transforms import (
    NumpyToTensor,
    RemoveLabelTransform,
    RenameTransform,
)
from batchgenerators.utilities.file_and_folder_operations import join
from nnunet.network_architecture.generic_UNet import Generic_UNet
from nnunet.network_architecture.initialization import InitWeights_He
from nnunet.network_architecture.neural_network import SegmentationNetwork
from nnunet.training.data_augmentation.custom_transforms import (
    Convert2DTo3DTransform,
    Convert3DTo2DTransform,
    ConvertSegmentationToRegionsTransform,
    MaskTransform,
)
from nnunet.training.data_augmentation.default_data_augmentation import (
    default_2D_augmentation_params,
    default_3D_augmentation_params,
    get_patch_size,
)
from nnunet.training.data_augmentation.downsampling import (
    DownsampleSegForDSTransform2,
    DownsampleSegForDSTransform3,
)
from nnunet.training.data_augmentation.pyramid_augmentations import (
    ApplyRandomBinaryOperatorTransform,
    MoveSegAsOneHotToData,
    RemoveRandomConnectedComponentFromOneHotEncodingTransform,
)
from nnunet.training.dataloading.dataset_loading import unpack_dataset
from nnunet.training.loss_functions.deep_supervision import MultipleOutputLoss2
from nnunet.training.network_training.nnUNetTrainerV2 import (
    maybe_mkdir_p,
    nnUNetTrainerV2,
)
from nnunet.utilities.nd_softmax import softmax_helper
from torch import nn


def get_insaneDA_augmentation2(
    dataloader_train,
    dataloader_val,
    patch_size,
    params=default_3D_augmentation_params,
    border_val_seg=-1,
    seeds_train=None,
    seeds_val=None,
    order_seg=1,
    order_data=3,
    deep_supervision_scales=None,
    soft_ds=False,
    classes=None,
    pin_memory=True,
    regions=None,
):
    assert (
        params.get("mirror") is None
    ), "old version of params, use new keyword do_mirror"

    tr_transforms = []

    if params.get("selected_data_channels") is not None:
        tr_transforms.append(
            DataChannelSelectionTransform(params.get("selected_data_channels"))
        )

    if params.get("selected_seg_channels") is not None:
        tr_transforms.append(
            SegChannelSelectionTransform(params.get("selected_seg_channels"))
        )

    # don't do color augmentations while in 2d mode with 3d data because the color channel is overloaded!!
    if params.get("dummy_2D") is not None and params.get("dummy_2D"):
        ignore_axes = (0,)
        tr_transforms.append(Convert3DTo2DTransform())
    else:
        ignore_axes = None

    tr_transforms.append(
        SpatialTransform_2(
            patch_size,
            patch_center_dist_from_border=None,
            do_elastic_deform=params.get("do_elastic"),
            deformation_scale=params.get("eldef_deformation_scale"),
            do_rotation=params.get("do_rotation"),
            angle_x=params.get("rotation_x"),
            angle_y=params.get("rotation_y"),
            angle_z=params.get("rotation_z"),
            do_scale=params.get("do_scaling"),
            scale=params.get("scale_range"),
            border_mode_data=params.get("border_mode_data"),
            border_cval_data=0,
            order_data=order_data,
            border_mode_seg="constant",
            border_cval_seg=border_val_seg,
            order_seg=order_seg,
            random_crop=params.get("random_crop"),
            p_el_per_sample=params.get("p_eldef"),
            p_scale_per_sample=params.get("p_scale"),
            p_rot_per_sample=params.get("p_rot"),
            independent_scale_for_each_axis=params.get(
                "independent_scale_factor_for_each_axis"
            ),
            p_independent_scale_per_axis=params.get("p_independent_scale_per_axis"),
        )
    )

    if params.get("dummy_2D"):
        tr_transforms.append(Convert2DTo3DTransform())

    # we need to put the color augmentations after the dummy 2d part (if applicable). Otherwise the overloaded color
    # channel gets in the way
    tr_transforms.append(GaussianNoiseTransform(p_per_sample=0.15))
    tr_transforms.append(
        GaussianBlurTransform(
            (0.5, 1.5),
            different_sigma_per_channel=True,
            p_per_sample=0.2,
            p_per_channel=0.5,
        )
    )
    tr_transforms.append(
        BrightnessMultiplicativeTransform(
            multiplier_range=(0.70, 1.3), p_per_sample=0.15
        )
    )
    tr_transforms.append(
        ContrastAugmentationTransform(contrast_range=(0.65, 1.5), p_per_sample=0.15)
    )
    tr_transforms.append(
        SimulateLowResolutionTransform(
            zoom_range=(0.5, 1),
            per_channel=True,
            p_per_channel=0.5,
            order_downsample=0,
            order_upsample=3,
            p_per_sample=0.25,
            ignore_axes=ignore_axes,
        )
    )
    tr_transforms.append(
        GammaTransform(
            params.get("gamma_range"),
            True,
            True,
            retain_stats=params.get("gamma_retain_stats"),
            p_per_sample=0.15,
        )
    )  # inverted gamma

    if params.get("do_additive_brightness"):
        tr_transforms.append(
            BrightnessTransform(
                params.get("additive_brightness_mu"),
                params.get("additive_brightness_sigma"),
                True,
                p_per_sample=params.get("additive_brightness_p_per_sample"),
                p_per_channel=params.get("additive_brightness_p_per_channel"),
            )
        )

    if params.get("do_gamma"):
        tr_transforms.append(
            GammaTransform(
                params.get("gamma_range"),
                False,
                True,
                retain_stats=params.get("gamma_retain_stats"),
                p_per_sample=params["p_gamma"],
            )
        )

    if params.get("do_mirror") or params.get("mirror"):
        tr_transforms.append(MirrorTransform(params.get("mirror_axes")))

    if params.get("mask_was_used_for_normalization") is not None:
        mask_was_used_for_normalization = params.get("mask_was_used_for_normalization")
        tr_transforms.append(
            MaskTransform(
                mask_was_used_for_normalization, mask_idx_in_seg=0, set_outside_to=0
            )
        )

    tr_transforms.append(RemoveLabelTransform(-1, 0))

    if params.get("move_last_seg_chanel_to_data") is not None and params.get(
        "move_last_seg_chanel_to_data"
    ):
        tr_transforms.append(
            MoveSegAsOneHotToData(
                1, params.get("all_segmentation_labels"), "seg", "data"
            )
        )
        if (
            params.get("cascade_do_cascade_augmentations")
            and not None
            and params.get("cascade_do_cascade_augmentations")
        ):
            if params.get("cascade_random_binary_transform_p") > 0:
                tr_transforms.append(
                    ApplyRandomBinaryOperatorTransform(
                        channel_idx=list(
                            range(-len(params.get("all_segmentation_labels")), 0)
                        ),
                        p_per_sample=params.get("cascade_random_binary_transform_p"),
                        key="data",
                        strel_size=params.get("cascade_random_binary_transform_size"),
                    )
                )
            if params.get("cascade_remove_conn_comp_p") > 0:
                tr_transforms.append(
                    RemoveRandomConnectedComponentFromOneHotEncodingTransform(
                        channel_idx=list(
                            range(-len(params.get("all_segmentation_labels")), 0)
                        ),
                        key="data",
                        p_per_sample=params.get("cascade_remove_conn_comp_p"),
                        fill_with_other_class_p=params.get(
                            "cascade_remove_conn_comp_max_size_percent_threshold"
                        ),
                        dont_do_if_covers_more_than_X_percent=params.get(
                            "cascade_remove_conn_comp_fill_with_other_class_p"
                        ),
                    )
                )

    tr_transforms.append(RenameTransform("seg", "target", True))

    if regions is not None:
        tr_transforms.append(
            ConvertSegmentationToRegionsTransform(regions, "target", "target")
        )

    if deep_supervision_scales is not None:
        if soft_ds:
            assert classes is not None
            tr_transforms.append(
                DownsampleSegForDSTransform3(
                    deep_supervision_scales, "target", "target", classes
                )
            )
        else:
            tr_transforms.append(
                DownsampleSegForDSTransform2(
                    deep_supervision_scales,
                    0,
                    0,
                    input_key="target",
                    output_key="target",
                )
            )

    tr_transforms.append(NumpyToTensor(["data", "target"], "float"))
    tr_transforms = Compose(tr_transforms)

    batchgenerator_train = MultiThreadedAugmenter(
        dataloader_train,
        tr_transforms,
        params.get("num_threads"),
        params.get("num_cached_per_thread"),
        seeds=seeds_train,
        pin_memory=pin_memory,
    )
    # batchgenerator_train = SingleThreadedAugmenter(dataloader_train, tr_transforms)

    val_transforms = []
    val_transforms.append(RemoveLabelTransform(-1, 0))
    if params.get("selected_data_channels") is not None:
        val_transforms.append(
            DataChannelSelectionTransform(params.get("selected_data_channels"))
        )
    if params.get("selected_seg_channels") is not None:
        val_transforms.append(
            SegChannelSelectionTransform(params.get("selected_seg_channels"))
        )

    if params.get("move_last_seg_chanel_to_data") is not None and params.get(
        "move_last_seg_chanel_to_data"
    ):
        val_transforms.append(
            MoveSegAsOneHotToData(
                1, params.get("all_segmentation_labels"), "seg", "data"
            )
        )

    val_transforms.append(RenameTransform("seg", "target", True))

    if regions is not None:
        val_transforms.append(
            ConvertSegmentationToRegionsTransform(regions, "target", "target")
        )

    if deep_supervision_scales is not None:
        if soft_ds:
            assert classes is not None
            val_transforms.append(
                DownsampleSegForDSTransform3(
                    deep_supervision_scales, "target", "target", classes
                )
            )
        else:
            val_transforms.append(
                DownsampleSegForDSTransform2(
                    deep_supervision_scales,
                    0,
                    0,
                    input_key="target",
                    output_key="target",
                )
            )

    val_transforms.append(NumpyToTensor(["data", "target"], "float"))
    val_transforms = Compose(val_transforms)

    batchgenerator_val = MultiThreadedAugmenter(
        dataloader_val,
        val_transforms,
        max(params.get("num_threads") // 2, 1),
        params.get("num_cached_per_thread"),
        seeds=seeds_val,
        pin_memory=pin_memory,
    )
    return batchgenerator_train, batchgenerator_val


class nnUNetTrainerV2_DA3(nnUNetTrainerV2):
    def setup_DA_params(self):
        super().setup_DA_params()
        self.deep_supervision_scales = [[1, 1, 1]] + list(
            list(i)
            for i in 1
            / np.cumprod(np.vstack(self.net_num_pool_op_kernel_sizes), axis=0)
        )[:-1]

        if self.threeD:
            self.data_aug_params = default_3D_augmentation_params
            self.data_aug_params["rotation_x"] = (
                -30.0 / 360 * 2.0 * np.pi,
                30.0 / 360 * 2.0 * np.pi,
            )
            self.data_aug_params["rotation_y"] = (
                -30.0 / 360 * 2.0 * np.pi,
                30.0 / 360 * 2.0 * np.pi,
            )
            self.data_aug_params["rotation_z"] = (
                -30.0 / 360 * 2.0 * np.pi,
                30.0 / 360 * 2.0 * np.pi,
            )
            if self.do_dummy_2D_aug:
                self.data_aug_params["dummy_2D"] = True
                self.print_to_log_file("Using dummy2d data augmentation")
                self.data_aug_params[
                    "elastic_deform_alpha"
                ] = default_2D_augmentation_params["elastic_deform_alpha"]
                self.data_aug_params[
                    "elastic_deform_sigma"
                ] = default_2D_augmentation_params["elastic_deform_sigma"]
                self.data_aug_params["rotation_x"] = default_2D_augmentation_params[
                    "rotation_x"
                ]
        else:
            self.do_dummy_2D_aug = False
            if max(self.patch_size) / min(self.patch_size) > 1.5:
                default_2D_augmentation_params["rotation_x"] = (
                    -180.0 / 360 * 2.0 * np.pi,
                    180.0 / 360 * 2.0 * np.pi,
                )
            self.data_aug_params = default_2D_augmentation_params
        self.data_aug_params["mask_was_used_for_normalization"] = self.use_mask_for_norm

        if self.do_dummy_2D_aug:
            self.basic_generator_patch_size = get_patch_size(
                self.patch_size[1:],
                self.data_aug_params["rotation_x"],
                self.data_aug_params["rotation_y"],
                self.data_aug_params["rotation_z"],
                self.data_aug_params["scale_range"],
            )
            self.basic_generator_patch_size = np.array(
                [self.patch_size[0]] + list(self.basic_generator_patch_size)
            )
            patch_size_for_spatialtransform = self.patch_size[1:]
        else:
            self.basic_generator_patch_size = get_patch_size(
                self.patch_size,
                self.data_aug_params["rotation_x"],
                self.data_aug_params["rotation_y"],
                self.data_aug_params["rotation_z"],
                self.data_aug_params["scale_range"],
            )
            patch_size_for_spatialtransform = self.patch_size

        self.data_aug_params["selected_seg_channels"] = [0]
        self.data_aug_params[
            "patch_size_for_spatialtransform"
        ] = patch_size_for_spatialtransform

        self.data_aug_params["p_rot"] = 0.3

        self.data_aug_params["scale_range"] = (0.65, 1.6)
        self.data_aug_params["p_scale"] = 0.3
        self.data_aug_params["independent_scale_factor_for_each_axis"] = True
        self.data_aug_params["p_independent_scale_per_axis"] = 0.3

        self.data_aug_params["do_elastic"] = True
        self.data_aug_params["p_eldef"] = 0.3
        self.data_aug_params["eldef_deformation_scale"] = (0, 0.25)

        self.data_aug_params["do_additive_brightness"] = True
        self.data_aug_params["additive_brightness_mu"] = 0
        self.data_aug_params["additive_brightness_sigma"] = 0.2
        self.data_aug_params["additive_brightness_p_per_sample"] = 0.3
        self.data_aug_params["additive_brightness_p_per_channel"] = 1

        self.data_aug_params["gamma_range"] = (0.5, 1.6)

        self.data_aug_params["num_cached_per_thread"] = 4

    def initialize(self, training=True, force_load_plans=False):
        if not self.was_initialized:
            maybe_mkdir_p(self.output_folder)

            if force_load_plans or (self.plans is None):
                self.load_plans_file()

            self.process_plans(self.plans)

            self.setup_DA_params()

            ################# Here we wrap the loss for deep supervision ############
            # we need to know the number of outputs of the network
            net_numpool = len(self.net_num_pool_op_kernel_sizes)

            # we give each output a weight which decreases exponentially (division by 2) as the resolution decreases
            # this gives higher resolution outputs more weight in the loss
            weights = np.array([1 / (2**i) for i in range(net_numpool)])

            # we don't use the lowest 2 outputs. Normalize weights so that they sum to 1
            mask = np.array(
                [True]
                + [
                    True if i < net_numpool - 1 else False
                    for i in range(1, net_numpool)
                ]
            )
            weights[~mask] = 0
            weights = weights / weights.sum()
            self.ds_loss_weights = weights
            # now wrap the loss
            self.loss = MultipleOutputLoss2(self.loss, self.ds_loss_weights)
            ################# END ###################

            self.folder_with_preprocessed_data = join(
                self.dataset_directory,
                self.plans["data_identifier"] + "_stage%d" % self.stage,
            )
            if training:
                self.dl_tr, self.dl_val = self.get_basic_generators()
                if self.unpack_data:
                    print("unpacking dataset")
                    unpack_dataset(self.folder_with_preprocessed_data)
                    print("done")
                else:
                    print(
                        "INFO: Not unpacking data! Training may be slow due to that."
                        " Pray you are not using 2d or you will wait all winter for"
                        " your model to finish!"
                    )

                self.tr_gen, self.val_gen = get_insaneDA_augmentation2(
                    self.dl_tr,
                    self.dl_val,
                    self.data_aug_params["patch_size_for_spatialtransform"],
                    self.data_aug_params,
                    deep_supervision_scales=self.deep_supervision_scales,
                    pin_memory=self.pin_memory,
                )
                self.print_to_log_file(
                    "TRAINING KEYS:\n %s" % (str(self.dataset_tr.keys())),
                    also_print_to_console=False,
                )
                self.print_to_log_file(
                    "VALIDATION KEYS:\n %s" % (str(self.dataset_val.keys())),
                    also_print_to_console=False,
                )
            else:
                pass

            self.initialize_network()
            self.initialize_optimizer_and_scheduler()

            assert isinstance(self.network, (SegmentationNetwork, nn.DataParallel))
        else:
            self.print_to_log_file(
                "self.was_initialized is True, not running self.initialize again"
            )
        self.was_initialized = True

    """def run_training(self):
        from batchviewer import view_batch

        a = next(self.tr_gen)
        view_batch(a['data'][:, 0], width=512, height=512)

        import IPython;IPython.embed()"""


class nnUNetTrainerV2_DA3_BN(nnUNetTrainerV2_DA3):
    def initialize_network(self):
        if self.threeD:
            conv_op = nn.Conv3d
            dropout_op = nn.Dropout3d
            norm_op = nn.BatchNorm3d

        else:
            conv_op = nn.Conv2d
            dropout_op = nn.Dropout2d
            norm_op = nn.BatchNorm2d

        norm_op_kwargs = {"eps": 1e-5, "affine": True}
        dropout_op_kwargs = {"p": 0, "inplace": True}
        net_nonlin = nn.LeakyReLU
        net_nonlin_kwargs = {"negative_slope": 1e-2, "inplace": True}
        self.network = Generic_UNet(
            self.num_input_channels,
            self.base_num_features,
            self.num_classes,
            len(self.net_num_pool_op_kernel_sizes),
            self.conv_per_stage,
            2,
            conv_op,
            norm_op,
            norm_op_kwargs,
            dropout_op,
            dropout_op_kwargs,
            net_nonlin,
            net_nonlin_kwargs,
            True,
            False,
            lambda x: x,
            InitWeights_He(1e-2),
            self.net_num_pool_op_kernel_sizes,
            self.net_conv_kernel_sizes,
            False,
            True,
            True,
        )
        if torch.cuda.is_available():
            self.network.cuda()
        self.network.inference_apply_nonlin = softmax_helper
