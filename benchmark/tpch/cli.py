import logging
import os
import shutil

import click

from misc.utils import DBGymConfig, get_scale_factor_string, workload_name_fn
from util.shell import subprocess_run
from util.pg import *

benchmark_tpch_logger = logging.getLogger("benchmark/tpch")
benchmark_tpch_logger.setLevel(logging.INFO)


@click.group(name="tpch")
@click.pass_obj
def tpch_group(dbgym_cfg: DBGymConfig):
    dbgym_cfg.append_group("tpch")


@tpch_group.command(name="data")
@click.argument("scale-factor", type=float)
@click.pass_obj
# The reason generate-data is separate from create-pgdata is because generate-data is generic
#   to all DBMSs while create-pgdata is specific to Postgres.
def tpch_data(dbgym_cfg: DBGymConfig, scale_factor: float):
    _clone(dbgym_cfg)
    _generate_data(dbgym_cfg, scale_factor)


@tpch_group.command(name="workload")
@click.option("--seed-start", type=int, default=15721, help="A workload consists of queries from multiple seeds. This is the starting seed (inclusive).")
@click.option("--seed-end", type=int, default=15721, help="A workload consists of queries from multiple seeds. This is the ending seed (inclusive).")
@click.option(
    "--query-subset",
    type=click.Choice(["all", "even", "odd"]),
    default="all",
)
@click.option("--scale-factor", type=float, default=1)
@click.pass_obj
def tpch_workload(
    dbgym_cfg: DBGymConfig,
    seed_start: int,
    seed_end: int,
    query_subset: str,
    scale_factor: float,
):
    assert seed_start <= seed_end, f'seed_start ({seed_start}) must be <= seed_end ({seed_end})'
    _clone(dbgym_cfg)
    _generate_queries(dbgym_cfg, seed_start, seed_end, scale_factor)
    _generate_workload(dbgym_cfg, seed_start, seed_end, query_subset, scale_factor)


def _get_queries_dname(seed: int, scale_factor: float) -> str:
    return f"queries_{seed}_sf{get_scale_factor_string(scale_factor)}"


def _clone(dbgym_cfg: DBGymConfig):
    symlink_dir = dbgym_cfg.cur_symlinks_build_path("tpch-kit")
    if symlink_dir.exists():
        benchmark_tpch_logger.info(f"Skipping clone: {symlink_dir}")
        return

    benchmark_tpch_logger.info(f"Cloning: {symlink_dir}")
    real_build_path = dbgym_cfg.cur_task_runs_build_path()
    subprocess_run(
        f"./tpch_setup.sh {real_build_path}", cwd=dbgym_cfg.cur_source_path()
    )
    subprocess_run(
        f"ln -s {real_build_path / 'tpch-kit'} {dbgym_cfg.cur_symlinks_build_path(mkdir=True)}"
    )
    benchmark_tpch_logger.info(f"Cloned: {symlink_dir}")


def _generate_queries(dbgym_cfg: DBGymConfig, seed_start: int, seed_end: int, scale_factor: float):
    build_path = dbgym_cfg.cur_symlinks_build_path()
    assert build_path.exists()

    data_path = dbgym_cfg.cur_symlinks_data_path(mkdir=True)
    benchmark_tpch_logger.info(
        f"Generating queries: {data_path} [{seed_start}, {seed_end}]"
    )
    for seed in range(seed_start, seed_end + 1):
        symlinked_seed = data_path / _get_queries_dname(seed, scale_factor)
        if symlinked_seed.exists():
            continue

        real_dir = dbgym_cfg.cur_task_runs_data_path(_get_queries_dname(seed, scale_factor), mkdir=True)
        for i in range(1, 22 + 1):
            target_sql = (real_dir / f"{i}.sql").resolve()
            subprocess_run(
                f"DSS_QUERY=./queries ./qgen {i} -r {seed} -s {scale_factor} > {target_sql}",
                cwd=build_path / "tpch-kit" / "dbgen",
                verbose=False,
            )
        subprocess_run(f"ln -s {real_dir} {data_path}", verbose=False)
    benchmark_tpch_logger.info(
        f"Generated queries: {data_path} [{seed_start}, {seed_end}]"
    )


def _generate_data(dbgym_cfg: DBGymConfig, scale_factor: float):
    build_path = dbgym_cfg.cur_symlinks_build_path()
    assert build_path.exists()

    data_path = dbgym_cfg.cur_symlinks_data_path(mkdir=True)
    symlink_dir = data_path / f"tables_sf{get_scale_factor_string(scale_factor)}"
    if symlink_dir.exists():
        benchmark_tpch_logger.info(f"Skipping generation: {symlink_dir}")
        return

    benchmark_tpch_logger.info(f"Generating: {symlink_dir}")
    subprocess_run(
        f"./dbgen -vf -s {scale_factor}", cwd=build_path / "tpch-kit" / "dbgen"
    )
    real_dir = dbgym_cfg.cur_task_runs_data_path(f"tables_sf{get_scale_factor_string(scale_factor)}", mkdir=True)
    subprocess_run(f"mv ./*.tbl {real_dir}", cwd=build_path / "tpch-kit" / "dbgen")

    subprocess_run(f"ln -s {real_dir} {data_path}")
    benchmark_tpch_logger.info(f"Generated: {symlink_dir}")


def _generate_workload(
    dbgym_cfg: DBGymConfig,
    seed_start: int,
    seed_end: int,
    query_subset: str,
    scale_factor: float,
):
    symlink_data_dir = dbgym_cfg.cur_symlinks_data_path(mkdir=True)
    workload_name = workload_name_fn(scale_factor, seed_start, seed_end, query_subset)
    workload_symlink_path = symlink_data_dir / workload_name

    benchmark_tpch_logger.info(f"Generating: {workload_symlink_path}")
    real_dir = dbgym_cfg.cur_task_runs_data_path(
        workload_name, mkdir=True
    )

    queries = None
    if query_subset == "all":
        queries = [f"{i}" for i in range(1, 22 + 1)]
    elif query_subset == "even":
        queries = [f"{i}" for i in range(1, 22 + 1) if i % 2 == 0]
    elif query_subset == "odd":
        queries = [f"{i}" for i in range(1, 22 + 1) if i % 2 == 1]

    with open(real_dir / "order.txt", "w") as f:
        for seed in range(seed_start, seed_end + 1):
            for qnum in queries:
                sqlfile = symlink_data_dir / _get_queries_dname(seed, scale_factor) / f"{qnum}.sql"
                assert sqlfile.exists()
                output = ",".join([f"S{seed}-Q{qnum}", str(sqlfile)])
                print(output, file=f)
                # TODO(WAN): add option to deep-copy the workload.
    
    if workload_symlink_path.exists():
        os.remove(workload_symlink_path)
    subprocess_run(f"ln -s {real_dir} {workload_symlink_path}")
    benchmark_tpch_logger.info(f"Generated: {workload_symlink_path}")
