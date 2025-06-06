from setuptools import find_namespace_packages, setup

setup(
    name="nnunet",
    packages=find_namespace_packages(include=["nnunet", "nnunet.*"]),
    version="1.6.6",
    description="nnU-Net. Framework for out-of-the box biomedical image segmentation.",
    url="https://github.com/MIC-DKFZ/nnUNet",
    author="Division of Medical Image Computing, German Cancer Research Center",
    author_email="f.isensee@dkfz-heidelberg.de",
    license="Apache License Version 2.0, January 2004",
    install_requires=[
        "torch>=1.6.0a",
        "tqdm==4.56.0",
        "dicom2nifti==2.3.2",
        "scikit-image==0.18.1",
        "medpy",
        "scipy==1.6.0",
        "batchgenerators==0.21",
        "numpy==1.21.5",
        "sklearn",
        "SimpleITK==2.0.2",
        "pandas==1.2.1",
        "requests",
        "nibabel==3.2.1",
        "tifffile",
        "imageio==2.9.0",
        "pydicom==2.1.2",
        "matplotlib==3.3.3",
        "Pillow==8.1.0",
    ],
    entry_points={
        "console_scripts": [
            "nnUNet_convert_decathlon_task ="
            " nnunet.experiment_planning.nnUNet_convert_decathlon_task:main",
            "nnUNet_plan_and_preprocess ="
            " nnunet.experiment_planning.nnUNet_plan_and_preprocess:main",
            "nnUNet_train = nnunet.run.run_training:main",
            "nnUNet_train_DP = nnunet.run.run_training_DP:main",
            "nnUNet_train_DDP = nnunet.run.run_training_DDP:main",
            "nnUNet_predict = nnunet.inference.predict_simple:main",
            "nnUNet_ensemble = nnunet.inference.ensemble_predictions:main",
            "nnUNet_find_best_configuration ="
            " nnunet.evaluation.model_selection.figure_out_what_to_submit:main",
            "nnUNet_print_available_pretrained_models ="
            " nnunet.inference.pretrained_models.download_pretrained_model:print_available_pretrained_models",
            "nnUNet_print_pretrained_model_info ="
            " nnunet.inference.pretrained_models.download_pretrained_model:print_pretrained_model_requirements",
            "nnUNet_download_pretrained_model ="
            " nnunet.inference.pretrained_models.download_pretrained_model:download_by_name",
            "nnUNet_download_pretrained_model_by_url ="
            " nnunet.inference.pretrained_models.download_pretrained_model:download_by_url",
            "nnUNet_determine_postprocessing ="
            " nnunet.postprocessing.consolidate_postprocessing_simple:main",
            "nnUNet_export_model_to_zip ="
            " nnunet.inference.pretrained_models.collect_pretrained_models:export_entry_point",
            "nnUNet_install_pretrained_model_from_zip ="
            " nnunet.inference.pretrained_models.download_pretrained_model:install_from_zip_entry_point",
            "nnUNet_change_trainer_class = nnunet.inference.change_trainer:main",
            "nnUNet_evaluate_folder ="
            " nnunet.evaluation.evaluator:nnunet_evaluate_folder",
        ],
    },
    keywords=[
        "deep learning",
        "image segmentation",
        "medical image analysis",
        "medical image segmentation",
        "nnU-Net",
        "nnunet",
    ],
)
