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


import argparse

from batchgenerators.utilities.file_and_folder_operations import *
from nnunet.paths import default_plans_identifier
from nnunet.run.default_configuration import get_default_configuration
from nnunet.training.cascade_stuff.predict_next_stage import predict_next_stage
from nnunet.training.network_training.nnUNetTrainer import nnUNetTrainer
from nnunet.training.network_training.nnUNetTrainerCascadeFullRes import (
    nnUNetTrainerCascadeFullRes,
)
from nnunet.utilities.task_name_id_conversion import convert_id_to_task_name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("network")
    parser.add_argument("network_trainer")
    parser.add_argument("task", help="can be task name or task id")
    parser.add_argument("fold", help="0, 1, ..., 5 or 'all'")
    parser.add_argument(
        "-val",
        "--validation_only",
        help="use this if you want to only run the validation",
        action="store_true",
    )
    parser.add_argument(
        "-c",
        "--continue_training",
        help="use this if you want to continue a training",
        action="store_true",
    )
    parser.add_argument(
        "-p",
        help=(
            "plans identifier. Only change this if you created a custom experiment"
            " planner"
        ),
        default=default_plans_identifier,
        required=False,
    )
    parser.add_argument(
        "--use_compressed_data",
        default=False,
        action="store_true",
        help=(
            "If you set use_compressed_data, the training cases will not be"
            " decompressed. Reading compressed data is much more CPU and RAM intensive"
            " and should only be used if you know what you are doing"
        ),
        required=False,
    )
    parser.add_argument(
        "--deterministic",
        help=(
            "Makes training deterministic, but reduces training speed substantially. I"
            " (Fabian) think this is not necessary. Deterministic training will make"
            " you overfit to some random seed. Don't use that."
        ),
        required=False,
        default=False,
        action="store_true",
    )
    parser.add_argument("-gpus", help="number of gpus", required=True, type=int)
    parser.add_argument(
        "--dbs",
        required=False,
        default=False,
        action="store_true",
        help=(
            "distribute batch size. If "
            "True then whatever "
            "batch_size is in plans will "
            "be distributed over DDP "
            "models, if False then each "
            "model will have batch_size "
            "for a total of "
            "GPUs*batch_size"
        ),
    )
    parser.add_argument(
        "--npz",
        required=False,
        default=False,
        action="store_true",
        help=(
            "if set then nnUNet will "
            "export npz files of "
            "predicted segmentations "
            "in the vlaidation as well. "
            "This is needed to run the "
            "ensembling step so unless "
            "you are developing nnUNet "
            "you should enable this"
        ),
    )
    parser.add_argument(
        "--valbest", required=False, default=False, action="store_true", help=""
    )
    parser.add_argument(
        "--find_lr", required=False, default=False, action="store_true", help=""
    )
    parser.add_argument(
        "--fp32",
        required=False,
        default=False,
        action="store_true",
        help="disable mixed precision training and run old school fp32",
    )
    parser.add_argument(
        "--val_folder",
        required=False,
        default="validation_raw",
        help="name of the validation folder. No need to use this for most people",
    )
    parser.add_argument(
        "--disable_saving",
        required=False,
        action="store_true",
        help=(
            "If set nnU-Net will not save any parameter files. Useful for development"
            " when you are only interested in the results and want to save some disk"
            " space"
        ),
    )
    # parser.add_argument("--interp_order", required=False, default=3, type=int,
    #                     help="order of interpolation for segmentations. Testing purpose only. Hands off")
    # parser.add_argument("--interp_order_z", required=False, default=0, type=int,
    #                     help="order of interpolation along z if z is resampled separately. Testing purpose only. "
    #                          "Hands off")
    # parser.add_argument("--force_separate_z", required=False, default="None", type=str,
    #                     help="force_separate_z resampling. Can be None, True or False. Testing purpose only. Hands off")

    args = parser.parse_args()

    task = args.task
    fold = args.fold
    network = args.network
    network_trainer = args.network_trainer
    validation_only = args.validation_only
    plans_identifier = args.p

    use_compressed_data = args.use_compressed_data
    decompress_data = not use_compressed_data

    deterministic = args.deterministic
    valbest = args.valbest
    find_lr = args.find_lr
    num_gpus = args.gpus
    fp32 = args.fp32
    val_folder = args.val_folder
    # interp_order = args.interp_order
    # interp_order_z = args.interp_order_z
    # force_separate_z = args.force_separate_z

    if not task.startswith("Task"):
        task_id = int(task)
        task = convert_id_to_task_name(task_id)

    if fold == "all":
        pass
    else:
        fold = int(fold)

    # if force_separate_z == "None":
    #     force_separate_z = None
    # elif force_separate_z == "False":
    #     force_separate_z = False
    # elif force_separate_z == "True":
    #     force_separate_z = True
    # else:
    #     raise ValueError("force_separate_z must be None, True or False. Given: %s" % force_separate_z)

    (
        plans_file,
        output_folder_name,
        dataset_directory,
        batch_dice,
        stage,
        trainer_class,
    ) = get_default_configuration(network, task, network_trainer, plans_identifier)

    if trainer_class is None:
        raise RuntimeError("Could not find trainer class")

    if network == "3d_cascade_fullres":
        assert issubclass(trainer_class, nnUNetTrainerCascadeFullRes), (
            "If running 3d_cascade_fullres then your "
            "trainer class must be derived from "
            "nnUNetTrainerCascadeFullRes"
        )
    else:
        assert issubclass(
            trainer_class, nnUNetTrainer
        ), "network_trainer was found but is not derived from nnUNetTrainer"

    trainer = trainer_class(
        plans_file,
        fold,
        output_folder=output_folder_name,
        dataset_directory=dataset_directory,
        batch_dice=batch_dice,
        stage=stage,
        unpack_data=decompress_data,
        deterministic=deterministic,
        distribute_batch_size=args.dbs,
        num_gpus=num_gpus,
        fp16=not fp32,
    )

    if args.disable_saving:
        trainer.save_latest_only = False  # if false it will not store/overwrite _latest but separate files each
        trainer.save_intermediate_checkpoints = (
            False  # whether or not to save checkpoint_latest
        )
        trainer.save_best_checkpoint = False  # whether or not to save the best checkpoint according to self.best_val_eval_criterion_MA
        trainer.save_final_checkpoint = (
            False  # whether or not to save the final checkpoint
        )

    trainer.initialize(not validation_only)

    if find_lr:
        trainer.find_lr()
    else:
        if not validation_only:
            if args.continue_training:
                trainer.load_latest_checkpoint()
            trainer.run_training()
        else:
            if valbest:
                trainer.load_best_checkpoint(train=False)
            else:
                trainer.load_latest_checkpoint(train=False)

        trainer.network.eval()

        # predict validation
        trainer.validate(save_softmax=args.npz, validation_folder_name=val_folder)

        if network == "3d_lowres":
            print("predicting segmentations for the next stage of the cascade")
            predict_next_stage(
                trainer,
                join(
                    dataset_directory, trainer.plans["data_identifier"] + "_stage%d" % 1
                ),
            )


if __name__ == "__main__":
    main()
