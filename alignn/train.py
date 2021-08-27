"""Ignite training script.

from the repository root, run
`PYTHONPATH=$PYTHONPATH:. python alignn/train.py`
then `tensorboard --logdir tb_logs/test` to monitor results...
"""
import json
import os
import pickle as pk
import pprint
from functools import partial

# from pathlib import Path
from typing import Any, Dict, Union

import ignite
import numpy as np
import torch
from ignite.contrib.handlers import TensorboardLogger
from ignite.contrib.handlers.stores import EpochOutputStore
from ignite.contrib.handlers.tensorboard_logger import global_step_from_engine
from ignite.contrib.handlers.tqdm_logger import ProgressBar
from ignite.contrib.metrics import ROC_AUC, RocCurve
from ignite.engine import (
    Events,
    create_supervised_evaluator,
    create_supervised_trainer,
)
from ignite.handlers import (
    Checkpoint,
    DiskSaver,
    EarlyStopping,
    TerminateOnNan,
    Timer,
)
from ignite.metrics import (
    Accuracy,
    ConfusionMatrix,
    Loss,
    MeanAbsoluteError,
    Precision,
    Recall,
)
from jarvis.db.jsonutils import dumpjson
from sklearn.metrics import mean_absolute_error, roc_auc_score
from torch import nn

from alignn import models
from alignn.config import TrainingConfig
from alignn.data import get_train_val_loaders
from alignn.models.alignn import ALIGNN

from alignn.models.alignn_layernorm import ALIGNN as ALIGNN_LN
from alignn.models.modified_cgcnn import CGCNN
from alignn.models.alignn_cgcnn import ACGCNN
from alignn.models.dense_alignn import DenseALIGNN
from alignn.models.densegcn import DenseGCN
from alignn.models.icgcnn import iCGCNN
from alignn.models.modified_cgcnn import CGCNN

# from sklearn.decomposition import PCA, KernelPCA
# from sklearn.preprocessing import StandardScaler

# torch config
torch.set_default_dtype(torch.float32)

device = "cpu"
if torch.cuda.is_available():
    device = torch.device("cuda")


def activated_output_transform(output):
    """Exponentiate output."""
    y_pred, y = output
    y_pred = torch.exp(y_pred)
    y_pred = y_pred[:, 1]
    return y_pred, y


def make_standard_scalar_and_pca(output, datafile="sc.pkl"):
    """Use standard scalar and PCS for multi-output data."""
    sc = pk.load(open(datafile, "rb"))
    y_pred, y = output
    y_pred = torch.tensor(sc.transform(y_pred.cpu().numpy()), device=device)
    y = torch.tensor(sc.transform(y.cpu().numpy()), device=device)
    # pc = pk.load(open("pca.pkl", "rb"))
    # y_pred = torch.tensor(pc.transform(y_pred), device=device)
    # y = torch.tensor(pc.transform(y), device=device)

    # y_pred = torch.tensor(pca_sc.inverse_transform(y_pred),device=device)
    # y = torch.tensor(pca_sc.inverse_transform(y),device=device)
    # print (y.shape,y_pred.shape)
    return y_pred, y


def thresholded_output_transform(output):
    """Round off output."""
    y_pred, y = output
    y_pred = torch.round(torch.exp(y_pred))
    # print ('output',y_pred)
    return y_pred, y


def group_decay(model):
    """Omit weight decay from bias and batchnorm params."""
    decay, no_decay = [], []

    for name, p in model.named_parameters():
        if "bias" in name or "bn" in name or "norm" in name:
            no_decay.append(p)
        else:
            decay.append(p)

    return [
        {"params": decay},
        {"params": no_decay, "weight_decay": 0},
    ]


def setup_optimizer(params, config: TrainingConfig):
    """Set up optimizer for param groups."""
    if config.optimizer == "adamw":
        optimizer = torch.optim.AdamW(
            params,
            lr=config.learning_rate,
            weight_decay=config.weight_decay,
        )
    elif config.optimizer == "sgd":
        optimizer = torch.optim.SGD(
            params,
            lr=config.learning_rate,
            momentum=0.9,
            weight_decay=config.weight_decay,
        )
    return optimizer


def train_dgl(
    config: Union[TrainingConfig, Dict[str, Any]],
    # checkpoint_dir: Path = Path("./"),
    train_val_test_loaders=[],
):
    """Training entry point for DGL networks.

    `config` should conform to alignn.conf.TrainingConfig, and
    if passed as a dict with matching keys, pydantic validation is used
    """

    if isinstance(config, dict):
        try:
            print(config)
            config = TrainingConfig(**config)
        except Exception as exp:
            print("Check", exp)

    os.makedirs(config.output_dir, exist_ok=True)

    if config.tune:
        from ray import tune

    print("config:")
    pprint.pprint(config.dict(), sort_dicts=False)

    with open(config.output_dir / "fullconfig.json", "w") as f:
        json.dump(json.loads(config.json()), f, indent=2)

    if config.classification_threshold is not None:
        classification = True
    else:
        classification = False

    if config.random_seed is not None:
        deterministic = True
        ignite.utils.manual_seed(config.random_seed)
    else:
        deterministic = False

    line_graph = False
    alignn_models = {
        "alignn",
        "dense_alignn",
        "alignn_cgcnn",
        "alignn_layernorm",
    }
    if config.model.name == "clgn":
        line_graph = True
    if config.model.name == "cgcnn":
        line_graph = True
    if config.model.name == "icgcnn":
        line_graph = True
    if config.model.name in alignn_models and config.model.alignn_layers > 0:
        line_graph = True
    # print ('output_dir train', config.output_dir)
    if not train_val_test_loaders:
        # use input standardization for all real-valued feature sets
        (
            train_loader,
            val_loader,
            test_loader,
            prepare_batch,
        ) = get_train_val_loaders(
            dataset=config.dataset,
            target=config.target,
            n_train=config.n_train,
            n_val=config.n_val,
            n_test=config.n_test,
            train_ratio=config.train_ratio,
            val_ratio=config.val_ratio,
            test_ratio=config.test_ratio,
            shuffle_train_val=config.shuffle_train_val,
            batch_size=config.batch_size,
            atom_features=config.atom_features,
            neighbor_strategy=config.neighbor_strategy,
            standardize=config.atom_features != "cgcnn",
            line_graph=line_graph,
            id_tag=config.id_tag,
            pin_memory=config.pin_memory,
            workers=config.num_workers,
            save_dataloader=config.save_dataloader,
            use_canonize=config.use_canonize,
            filename=config.filename,
            cutoff=config.cutoff,
            max_neighbors=config.max_neighbors,
            output_features=config.model.output_features,
            classification_threshold=config.classification_threshold,
            target_multiplication_factor=config.target_multiplication_factor,
            standard_scalar_and_pca=config.standard_scalar_and_pca,
            keep_data_order=config.keep_data_order,
            output_dir=config.output_dir,
            cache_dir=config.cache_dir,
        )
    else:
        # dataloaders explicitly passed as list
        (
            train_loader,
            val_loader,
            test_loader,
            prepare_batch,
        ) = train_val_test_loaders

    prepare_batch = partial(prepare_batch, device=device)

    if classification:
        config.model.classification = True

    # define network, optimizer, scheduler
    _model = {
        "cgcnn": CGCNN,
        "icgcnn": iCGCNN,
        "densegcn": DenseGCN,
        "alignn": ALIGNN,
        "dense_alignn": DenseALIGNN,
        "alignn_cgcnn": ACGCNN,
        "alignn_layernorm": ALIGNN_LN,
    }
    model = _model.get(config.model.name)(config.model)
    model.to(device)

    if config.distributed:
        import torch.distributed as dist

        def setup(rank, world_size):
            os.environ["MASTER_ADDR"] = "localhost"
            os.environ["MASTER_PORT"] = "12355"

            # initialize the process group
            dist.init_process_group("gloo", rank=rank, world_size=world_size)

        def cleanup():
            dist.destroy_process_group()

        setup(2, 2)
        model = torch.nn.parallel.DistributedDataParallel(model)

    # group parameters to skip weight decay for bias and batchnorm
    params = group_decay(model)
    optimizer = setup_optimizer(params, config)

    if config.scheduler == "none":
        # always return multiplier of 1 (i.e. do nothing)
        scheduler = torch.optim.lr_scheduler.LambdaLR(
            optimizer, lambda epoch: 1.0
        )

    elif config.scheduler == "onecycle":
        steps_per_epoch = len(train_loader)
        # pct_start = config.warmup_steps / (config.epochs * steps_per_epoch)
        scheduler = torch.optim.lr_scheduler.OneCycleLR(
            optimizer,
            max_lr=config.learning_rate,
            epochs=config.epochs,
            steps_per_epoch=steps_per_epoch,
            # pct_start=pct_start,
            pct_start=0.3,
        )
    elif config.scheduler == "step":
        # pct_start = config.warmup_steps / (config.epochs * steps_per_epoch)
        scheduler = torch.optim.lr_scheduler.StepLR(
            optimizer,
        )

    # select configured loss function
    criteria = {
        "mse": nn.MSELoss(),
        "l1": nn.L1Loss(),
        "poisson": nn.PoissonNLLLoss(log_input=False, full=True),
        "zig": models.modified_cgcnn.ZeroInflatedGammaLoss(),
    }
    criterion = criteria[config.criterion]

    # set up training engine and evaluators
    metrics = {"loss": Loss(criterion), "mae": MeanAbsoluteError()}
    if config.model.output_features > 1 and config.standard_scalar_and_pca:
        # metrics = {"loss": Loss(criterion), "mae": MeanAbsoluteError()}
        scaler_transform = partial(
            make_standard_scalar_and_pca,
            datafile=os.path.join(config.output_dir, "sc.pkl"),
        )
        metrics = {
            "loss": Loss(criterion, output_transform=scaler_transform),
            "mae": MeanAbsoluteError(output_transform=scaler_transform),
        }

    if config.criterion == "zig":

        def zig_prediction_transform(x):
            output, y = x
            return criterion.predict(output), y

        metrics = {
            "loss": Loss(criterion),
            "mae": MeanAbsoluteError(
                output_transform=zig_prediction_transform
            ),
        }

    if classification:
        criterion = nn.NLLLoss()

        metrics = {
            "accuracy": Accuracy(
                output_transform=thresholded_output_transform
            ),
            "precision": Precision(
                output_transform=thresholded_output_transform
            ),
            "recall": Recall(output_transform=thresholded_output_transform),
            "rocauc": ROC_AUC(output_transform=activated_output_transform),
            "roccurve": RocCurve(output_transform=activated_output_transform),
            "confmat": ConfusionMatrix(
                output_transform=thresholded_output_transform, num_classes=2
            ),
        }
    trainer = create_supervised_trainer(
        model,
        optimizer,
        criterion,
        prepare_batch=prepare_batch,
        device=device,
        deterministic=deterministic,
        # output_transform=make_standard_scalar_and_pca,
    )

    evaluator = create_supervised_evaluator(
        model,
        metrics=metrics,
        prepare_batch=prepare_batch,
        device=device,
        # output_transform=make_standard_scalar_and_pca,
    )

    train_evaluator = create_supervised_evaluator(
        model,
        metrics=metrics,
        prepare_batch=prepare_batch,
        device=device,
        # output_transform=make_standard_scalar_and_pca,
    )

    test_evaluator = create_supervised_evaluator(
        model,
        metrics=metrics,
        prepare_batch=prepare_batch,
        device=device,
    )

    # ignite event handlers:
    trainer.add_event_handler(Events.EPOCH_COMPLETED, TerminateOnNan())

    # apply learning rate scheduler
    trainer.add_event_handler(
        Events.ITERATION_COMPLETED, lambda engine: scheduler.step()
    )

    # training timer
    train_timer = Timer(average=True)
    train_timer.attach(
        trainer,
        start=Events.EPOCH_STARTED,
        resume=Events.EPOCH_STARTED,
        pause=Events.EPOCH_COMPLETED,
        step=Events.EPOCH_COMPLETED,
    )

    if config.write_checkpoint:
        # model checkpointing
        to_save = {
            "model": model,
            "optimizer": optimizer,
            "lr_scheduler": scheduler,
            "trainer": trainer,
        }
        handler = Checkpoint(
            to_save,
            DiskSaver(config.output_dir, create_dir=True, require_empty=False),
            n_saved=2,
            global_step_transform=lambda *_: trainer.state.epoch,
        )
        trainer.add_event_handler(Events.EPOCH_COMPLETED, handler)

    if config.progress:
        pbar = ProgressBar()
        pbar.attach(trainer, output_transform=lambda x: {"loss": x})
        # pbar.attach(evaluator,output_transform=lambda x: {"mae": x})

    history = {
        "train": {m: [] for m in metrics.keys()},
        "validation": {m: [] for m in metrics.keys()},
    }
    history["train"]["epoch_time"] = []

    if config.store_outputs:
        # log_results handler will save epoch output
        # in history["EOS"]
        eos = EpochOutputStore()
        eos.attach(evaluator)
        train_eos = EpochOutputStore()
        train_eos.attach(train_evaluator)

    @trainer.on(Events.COMPLETED)
    def log_test_results(engine):
        """Log test set performance."""
        test_evaluator.run(test_loader)
        torch.save(
            test_evaluator.state.metrics, config.output_dir / "test_metrics.pt"
        )

    # collect evaluation performance
    @trainer.on(Events.EPOCH_COMPLETED)
    def log_results(engine):
        """Print training and validation metrics to console."""
        train_evaluator.run(train_loader)
        evaluator.run(val_loader)

        if config.tune:
            # report validation set metrics to ray tune
            tune_metrics = evaluator.state.metrics
            tune_metrics["epoch_time"] = train_timer.value()
            tune.report(**tune_metrics)

        tmetrics = train_evaluator.state.metrics
        vmetrics = evaluator.state.metrics
        for metric in metrics.keys():
            tm = tmetrics[metric]
            vm = vmetrics[metric]
            if metric == "roccurve":
                tm = [k.tolist() for k in tm]
                vm = [k.tolist() for k in vm]
            if isinstance(tm, torch.Tensor):
                tm = tm.cpu().numpy().tolist()
                vm = vm.cpu().numpy().tolist()

            history["train"][metric].append(tm)
            history["validation"][metric].append(vm)

        history["train"]["epoch_time"].append(train_timer.value())

        if config.store_outputs:
            history["EOS"] = eos.data
            history["trainEOS"] = train_eos.data
            dumpjson(
                filename=os.path.join(config.output_dir, "history_val.json"),
                data=history["validation"],
            )
            dumpjson(
                filename=os.path.join(config.output_dir, "history_train.json"),
                data=history["train"],
            )
        # if config.progress:
        #     pbar = ProgressBar()
        #     if not classification:
        #         pbar.log_message(f"Val_MAE: {vmetrics['mae']:.4f}")
        #         pbar.log_message(f"Train_MAE: {tmetrics['mae']:.4f}")
        #     else:
        #         pbar.log_message(f"Train ROC AUC: {tmetrics['rocauc']:.4f}")
        #         pbar.log_message(f"Val ROC AUC: {vmetrics['rocauc']:.4f}")

    if config.n_early_stopping is not None:
        if classification:
            my_metrics = "accuracy"
        else:
            my_metrics = "mae"

        def default_score_fn(engine):
            score = engine.state.metrics[my_metrics]
            return score

        es_handler = EarlyStopping(
            patience=config.n_early_stopping,
            score_function=default_score_fn,
            trainer=trainer,
        )
        evaluator.add_event_handler(Events.EPOCH_COMPLETED, es_handler)
    # optionally log results to tensorboard
    if config.log_tensorboard:

        tb_logger = TensorboardLogger(
            log_dir=os.path.join(config.output_dir, "tb_logs", "test")
        )
        for tag, evaluator in [
            ("training", train_evaluator),
            ("validation", evaluator),
        ]:
            tb_logger.attach_output_handler(
                evaluator,
                event_name=Events.EPOCH_COMPLETED,
                tag=tag,
                metric_names=["loss", "mae"],
                global_step_transform=global_step_from_engine(trainer),
            )

    # train the model!
    trainer.run(train_loader, max_epochs=config.epochs)

    if config.log_tensorboard:
        test_loss = evaluator.state.metrics["loss"]
        tb_logger.writer.add_hparams(config, {"hparam/test_loss": test_loss})
        tb_logger.close()

    if config.write_predictions and classification:
        model.eval()
        with open(
            os.path.join(config.output_dir, "prediction_results_test_set.csv"),
            "w",
        ) as f:
            print("id, target, prediction", file=f)
            targets = []
            predictions = []
            with torch.no_grad():
                ids = test_loader.dataset.ids  # [test_loader.dataset.indices]
                for dat, id in zip(test_loader, ids):
                    g, lg, target = dat
                    out_data = model([g.to(device), lg.to(device)])

                    top_p, top_class = torch.topk(torch.exp(out_data), k=1)
                    target = int(target.cpu().numpy().flatten().tolist()[0])

                    f.write("%s, %d, %d\n" % (id, (target), (top_class)))
                    targets.append(target)
                    predictions.append(
                        top_class.cpu().numpy().flatten().tolist()[0]
                    )

        print("predictions", predictions)
        print("targets", targets)
        print(
            "Test ROCAUC:",
            roc_auc_score(np.array(targets), np.array(predictions)),
        )

    if (
        config.write_predictions
        and not classification
        and config.model.output_features > 1
    ):
        model.eval()
        mem = []
        with torch.no_grad():
            ids = test_loader.dataset.ids  # [test_loader.dataset.indices]
            for dat, id in zip(test_loader, ids):
                g, lg, target = dat
                out_data = model([g.to(device), lg.to(device)])
                out_data = out_data.cpu().numpy().tolist()
                if config.standard_scalar_and_pca:
                    sc = pk.load(open("sc.pkl", "rb"))
                    out_data = list(
                        sc.transform(np.array(out_data).reshape(1, -1))[0]
                    )  # [0][0]
                target = target.cpu().numpy().flatten().tolist()
                info = {}
                info["id"] = id
                info["target"] = target
                info["predictions"] = out_data
                mem.append(info)
        dumpjson(
            filename=os.path.join(
                config.output_dir, "multi_out_predictions.json"
            ),
            data=mem,
        )
    if (
        config.write_predictions
        and not classification
        and config.model.output_features == 1
    ):
        model.eval()
        f = open(
            os.path.join(config.output_dir, "prediction_results_test_set.csv"),
            "w",
        )
        f.write("id,target,prediction\n")
        targets = []
        predictions = []
        with torch.no_grad():
            ids = test_loader.dataset.ids  # [test_loader.dataset.indices]
            for dat, id in zip(test_loader, ids):
                g, lg, target = dat
                out_data = model([g.to(device), lg.to(device)])
                out_data = out_data.cpu().numpy().tolist()
                if config.standard_scalar_and_pca:
                    sc = pk.load(
                        open(os.path.join(config.output_dir, "sc.pkl"), "rb")
                    )
                    out_data = sc.transform(np.array(out_data).reshape(-1, 1))[
                        0
                    ][0]
                target = target.cpu().numpy().flatten().tolist()
                if len(target) == 1:
                    target = target[0]
                f.write("%s, %6f, %6f\n" % (id, target, out_data))
                targets.append(target)
                predictions.append(out_data)
        f.close()

        print(
            "Test MAE:",
            mean_absolute_error(np.array(targets), np.array(predictions)),
        )
        if config.store_outputs and not classification:
            x = []
            y = []
            for i in history["EOS"]:
                x.append(i[0].cpu().numpy().tolist())
                y.append(i[1].cpu().numpy().tolist())
            x = np.array(x, dtype="float").flatten()
            y = np.array(y, dtype="float").flatten()
            f = open(
                os.path.join(
                    config.output_dir, "prediction_results_train_set.csv"
                ),
                "w",
            )
            # TODO: Add IDs
            f.write("target,prediction\n")
            for i, j in zip(x, y):
                f.write("%6f, %6f\n" % (j, i))
                line = str(i) + "," + str(j) + "\n"
                f.write(line)
            f.close()

    # TODO: Fix IDs for train loader
    """
    if config.write_train_predictions:
        model.eval()
        f = open("train_prediction_results.csv", "w")
        f.write("id,target,prediction\n")
        with torch.no_grad():
            ids = train_loader.dataset.dataset.ids[
                train_loader.dataset.indices
            ]
            print("lens", len(ids), len(train_loader.dataset.dataset))
            x = []
            y = []

            for dat, id in zip(train_loader, ids):
                g, lg, target = dat
                out_data = model([g.to(device), lg.to(device)])
                out_data = out_data.cpu().numpy().tolist()
                target = target.cpu().numpy().flatten().tolist()
                for i, j in zip(out_data, target):
                    x.append(i)
                    y.append(j)
            for i, j, k in zip(ids, x, y):
                f.write("%s, %6f, %6f\n" % (i, j, k))
        f.close()

    """
    if not config.tune:
        torch.save(history, config.output_dir / "metrics.pt")
        return history


if __name__ == "__main__":
    config = TrainingConfig(
        random_seed=123,
        epochs=10,
        n_train=32,
        n_val=32,
        n_test=32,
        batch_size=16,
        output_dir="test",
    )
    history = train_dgl(config)
