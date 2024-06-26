import math
import time
from typing import Any, Optional, Tuple, Union

import psycopg
from psycopg import Connection
from psycopg.errors import QueryCanceled

from tune.protox.env.logger import Logger
from tune.protox.env.space.primitive.knob import CategoricalKnob, Knob
from tune.protox.env.space.state.space import StateSpace
from tune.protox.env.types import (
    BestQueryRun,
    KnobSpaceAction,
    KnobSpaceContainer,
    QueryRun,
    QueryType,
)


def _force_statement_timeout(
    connection: psycopg.Connection[Any], timeout_ms: float
) -> None:
    retry = True
    while retry:
        retry = False
        try:
            connection.execute(f"SET statement_timeout = {timeout_ms}")
        except QueryCanceled:
            retry = True


def _time_query(
    logger: Optional[Logger],
    prefix: str,
    connection: psycopg.Connection[Any],
    query: str,
    timeout: float,
) -> Tuple[float, bool, Any]:
    has_timeout = False
    has_explain = "EXPLAIN" in query
    explain_data = None

    try:
        start_time = time.time()
        cursor = connection.execute(query)
        qid_runtime = (time.time() - start_time) * 1e6

        if has_explain:
            c = [c for c in cursor][0][0][0]
            assert "Execution Time" in c
            qid_runtime = float(c["Execution Time"]) * 1e3
            explain_data = c

        if logger:
            logger.get_logger(__name__).debug(
                f"{prefix} evaluated in {qid_runtime/1e6}"
            )

    except QueryCanceled:
        if logger:
            logger.get_logger(__name__).debug(
                f"{prefix} exceeded evaluation timeout {timeout}"
            )
        qid_runtime = timeout * 1e6
        has_timeout = True
    except Exception as e:
        assert False, print(e)
    # qid_runtime is in microseconds.
    return qid_runtime, has_timeout, explain_data


def _acquire_metrics_around_query(
    logger: Optional[Logger],
    prefix: str,
    connection: psycopg.Connection[Any],
    query: str,
    pqt: float = 0.0,
    obs_space: Optional[StateSpace] = None,
) -> Tuple[float, bool, Any, Any]:
    _force_statement_timeout(connection, 0)
    if obs_space and obs_space.require_metrics():
        initial_metrics = obs_space.construct_online(connection)

    if pqt > 0:
        _force_statement_timeout(connection, pqt * 1000)

    qid_runtime, did_timeout, explain_data = _time_query(
        logger, prefix, connection, query, pqt
    )

    # Wipe the statement timeout.
    _force_statement_timeout(connection, 0)
    if obs_space and obs_space.require_metrics():
        final_metrics = obs_space.construct_online(connection)
        diff = obs_space.state_delta(initial_metrics, final_metrics)
    else:
        diff = None

    # qid_runtime is in microseconds.
    return qid_runtime, did_timeout, explain_data, diff


def execute_variations(
    connection: psycopg.Connection[Any],
    runs: list[QueryRun],
    query: str,
    pqt: float = 0,
    logger: Optional[Logger] = None,
    sysknobs: Optional[KnobSpaceAction] = None,
    obs_space: Optional[StateSpace] = None,
) -> BestQueryRun:

    # Initial timeout.
    timeout_limit = pqt
    # Best run invocation.
    best_qr = BestQueryRun(None, None, True, None, None)

    for qr in runs:
        # Attach the specific per-query knobs.
        pqk_query = (
            "/*+ "
            + " ".join(
                [
                    knob.resolve_per_query_knob(
                        value,
                        all_knobs=sysknobs if sysknobs else KnobSpaceContainer({}),
                    )
                    for knob, value in qr.qknobs.items()
                ]
            )
            + " */"
            + query
        )
        # Log the query plan.
        pqk_query = "EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF) " + pqk_query

        # Log out the knobs that we are using.
        pqkk = [(knob.name(), val) for knob, val in qr.qknobs.items()]
        if logger:
            logger.get_logger(__name__).debug(f"{qr.prefix_qid} executing with {pqkk}")

        runtime, did_timeout, explain_data, metric = _acquire_metrics_around_query(
            logger=logger,
            prefix=qr.prefix_qid,
            connection=connection,
            query=pqk_query,
            pqt=timeout_limit,
            obs_space=obs_space,
        )

        if not did_timeout:
            new_timeout_limit = math.ceil(runtime / 1e3) / 1.0e3
            if new_timeout_limit < timeout_limit:
                timeout_limit = new_timeout_limit

        if best_qr.runtime is None or runtime < best_qr.runtime:
            assert qr
            best_qr = BestQueryRun(
                qr,
                runtime,
                did_timeout,
                explain_data,
                metric,
            )

        if logger:
            # Log how long we are executing each query + mode.
            logger.record(qr.prefix_qid, runtime / 1e6)

    return best_qr
