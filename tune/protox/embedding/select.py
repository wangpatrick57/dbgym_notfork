import json
import os
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import tqdm

from tune.protox.embedding.analyze import RANGES_FNAME, STATS_FNAME


class DotDict(dict):
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


def select_best_embeddings(cfg, generic_args, select_args):
    data = _load_data(cfg, select_args)

    if generic_args.dataset_path is not None and os.path.exists(
        generic_args.dataset_path
    ):
        raw_data = pd.read_parquet(generic_args.dataset_path)
        data = _attach(data, raw_data, select_args.idx_limit)

    data.to_csv(
        os.path.join(cfg.dbgym_this_run_path, "curated_results.csv"), index=False
    )

    if (cfg.dbgym_this_run_path / "curated").exists():
        shutil.rmtree(cfg.dbgym_this_run_path / "curated")
    os.mkdir(cfg.dbgym_this_run_path / "curated")

    if "idx_class_total_error" in data:
        data["elbo"] = data.elbo + data.idx_class_total_error

    if select_args.allow_all:
        df = data.sort_values(by=["elbo"]).iloc[: select_args.num_curate]
    else:
        df = (
            data.sort_values(by=["elbo"])
            .groupby(by=["root"])
            .head(1)
            .iloc[: select_args.num_curate]
        )

    if select_args.flatten_idx == -1:
        for tup in df.itertuples():
            shutil.copytree(
                tup.path,
                f"{cfg.dbgym_this_run_path}/curated/{tup.path}",
                dirs_exist_ok=True,
            )
            shutil.copy(
                Path(tup.root) / "config",
                f"{cfg.dbgym_this_run_path}/curated/{tup.root}/config",
            )
    else:
        idx = select_args.flatten_idx
        Path(f"{cfg.dbgym_this_run_path}/curated").mkdir(parents=True, exist_ok=True)
        info_txt = open(f"{cfg.dbgym_this_run_path}/curated/info.txt", "w")

        for tup in df.itertuples():
            epoch = int(str(tup.path).split("epoch")[-1])
            shutil.copytree(tup.path, f"{cfg.dbgym_this_run_path}/curated/model{idx}")
            shutil.copy(
                Path(tup.root) / "config",
                f"{cfg.dbgym_this_run_path}/curated/model{idx}/config",
            )

            info_txt.write(f"model{idx}/embedder_{epoch}.pth\n")
            idx += 1

        info_txt.close()


def _load_data(cfg, select_args):
    data = []
    stats = [s for s in cfg.dbgym_this_run_path.rglob(STATS_FNAME)]
    for stat in stats:
        if "curated" in str(stat):
            continue

        info = {}
        # don't use open_and_save() because we generated stat in this run
        with open(stat, "r") as f:
            stat_dict = json.load(f)
            info["recon"] = stat_dict["recon_accum"]
            info["metric"] = stat_dict["metric_accum"]
            info["elbo"] = info["recon"]
            info["elbo_metric"] = info["recon"] + info["metric"]
            info["all_loss"] = info["recon"] + info["metric"]

            if select_args.recon is not None and select_args.recon < info["recon"]:
                # Did not pass reconstruction threshold.
                continue

            info["path"] = str(stat.parent)
            info["root"] = str(stat.parent.parent.parent)

        # don't use open_and_save() because we generated config in this run
        with open(stat.parent.parent.parent / "config", "r") as f:
            config = json.load(f)

            def recurse_set(source, target):
                for k, v in source.items():
                    if isinstance(v, dict):
                        recurse_set(v, target)
                    else:
                        target[k] = v

            recurse_set(config, info)
            if select_args.latent_dim is not None:
                if info["latent_dim"] != select_args.latent_dim:
                    continue

            output_scale = config["metric_loss_md"]["output_scale"]
            bias_sep = config["metric_loss_md"]["bias_separation"]

            if select_args.bias_sep is not None:
                if select_args.bias_sep != bias_sep:
                    continue

            info["ranges_file"] = str(Path(stat).parent / RANGES_FNAME)

        data.append(info)

    data = pd.DataFrame(data)
    data = data.loc[:, ~(data == data.iloc[0]).all()]
    if "output_scale" not in data:
        data["output_scale"] = output_scale

    if "bias_separation" not in data:
        data["bias_separation"] = bias_sep
    return data


def _attach(data, raw_data, num_limit=0):
    # As the group index goes up, the perf should go up (i.e., bounds should tighten)
    filtered_data = {}
    new_data = []
    for tup in tqdm.tqdm(data.itertuples(), total=data.shape[0]):
        tup = DotDict({k: getattr(tup, k) for k in data.columns})
        if raw_data is not None and Path(tup.ranges_file).exists():

            def compute_dist_score(current_dists, base, upper):
                nonlocal filtered_data
                key = (base, upper)
                if key not in filtered_data:
                    data_range = raw_data[
                        (raw_data.quant_mult_cost_improvement >= base)
                        & (raw_data.quant_mult_cost_improvement < upper)
                    ]
                    filtered_data[key] = data_range
                    if data_range.shape[0] == 0:
                        return 0
                else:
                    data_range = filtered_data[key]

                error = 0
                if "real_idx_class" in data_range:
                    data_dists = (
                        data_range.real_idx_class.value_counts() / data_range.shape[0]
                    )
                else:
                    data_dists = (
                        data_range.idx_class.value_counts() / data_range.shape[0]
                    )

                for key, dist in zip(data_dists.index, data_dists):
                    if str(key) not in current_dists:
                        error += dist
                    else:
                        error += abs(current_dists[str(key)] - dist)
                return error

            # don't use open_and_save() because we generated ranges in this run
            with open(tup.ranges_file, "r") as f:
                errors = []
                drange = (None, None)
                current_dists = {}

                for line in f:
                    if "Generating range" in line:
                        if len(current_dists) > 0:
                            assert drange[0] is not None
                            errors.append(
                                compute_dist_score(current_dists, drange[0], drange[1])
                            )
                            if num_limit > 0 and len(errors) >= num_limit:
                                current_dists = {}
                                break

                        if drange[0] is None:
                            drange = (1.0 - tup.bias_separation, 1.01)
                        else:
                            drange = (drange[0] - tup.bias_separation, drange[0])
                        current_dists = {}

                    else:
                        ci = line.split(": ")[0]
                        dist = float(line.strip().split(": ")[-1])
                        current_dists[ci] = dist

                if len(current_dists) > 0:
                    # Put the error in.
                    errors.append(
                        compute_dist_score(current_dists, 0.0, tup.bias_separation)
                    )

                tup["idx_class_errors"] = ",".join(
                    [str(np.round(e, 2)) for e in errors]
                )
                for i, e in enumerate(errors):
                    tup[f"idx_class_error{i}"] = np.round(e, 2)

                if len(errors) > 0:
                    tup["idx_class_mean_error"] = np.mean(errors)
                    tup["idx_class_total_error"] = np.sum(errors)
                    tup["idx_class_min_error"] = np.min(errors)
                    tup["idx_class_max_error"] = np.max(errors)
        new_data.append(dict(tup))
    return pd.DataFrame(new_data)