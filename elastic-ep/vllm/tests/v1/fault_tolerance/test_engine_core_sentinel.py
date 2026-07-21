# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
import queue
import socket
import time
from unittest.mock import Mock, patch

import pytest
import zmq
from msgspec import msgpack

from vllm.config import FaultToleranceConfig, ParallelConfig, VllmConfig
from vllm.v1.engine import EngineCoreRequestType, EngineStatusType
from vllm.v1.engine.exceptions import EngineLoopPausedError
from vllm.v1.fault_tolerance import EngineCoreSentinel
from vllm.v1.fault_tolerance.utils import FaultInfo, FaultToleranceRequest

pytestmark = pytest.mark.skip_global_cleanup


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def addr_dict():
    ports = [_find_free_port() for _ in range(3)]
    return {
        "client_cmd_addr": f"tcp://127.0.0.1:{ports[0]}",
        "worker_cmd_addr": f"tcp://127.0.0.1:{ports[1]}",
        "engine_fault_socket_addr": f"tcp://127.0.0.1:{ports[2]}",
    }


@pytest.fixture
def mock_parallel_config():
    config = Mock(spec=ParallelConfig)
    config.data_parallel_index = 0
    config.data_parallel_size = 2
    config.data_parallel_size_local = 2
    config.tensor_parallel_size = 1
    config.pipeline_parallel_size = 1
    config.local_engines_only = False
    config.gloo_timeout_seconds = 5
    config.fault_tolerance_config = FaultToleranceConfig(engine_recovery_timeout_sec=60)
    return config


def make_sentinel(parallel_config, addr_dict, sentinel_identity=b"engine_sentinel_0"):
    input_queue = queue.Queue()
    engine_core = Mock()
    return EngineCoreSentinel(
        parallel_config,
        engine_index=0,
        engine_input_q=input_queue,
        engine_fault_socket_addr=addr_dict["engine_fault_socket_addr"],
        sentinel_identity=sentinel_identity,
        worker_cmd_addr=addr_dict["worker_cmd_addr"],
        engine_core=engine_core,
    )


class TestEngineCoreSentinelInitialization:
    def test_engine_index(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        assert sentinel.engine_index == 0
        sentinel.shutdown()

    def test_zmq_socket_type(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        assert sentinel.engine_fault_socket.type == zmq.DEALER
        sentinel.shutdown()

    def test_worker_identities(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        assert sentinel.worker_identities == [b"PP0_TP0"]
        sentinel.shutdown()

    def test_fault_signal_q_initialized(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        assert sentinel.fault_signal_q is not None
        assert sentinel.fault_signal_q.empty()
        sentinel.shutdown()


class TestEngineCoreSentinelPollAndReport:
    def test_report_runtime_error(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        ctx = zmq.Context()
        receiver = ctx.socket(zmq.ROUTER)
        receiver.bind(addr_dict["engine_fault_socket_addr"])
        try:
            sentinel.fault_signal_q.put(RuntimeError("test exception"))
            time.sleep(0.2)
            if not receiver.poll(timeout=5000):
                pytest.fail("Timeout")
            parts = receiver.recv_multipart()
            fault_info = msgpack.decode(parts[-1], type=FaultInfo)
            assert fault_info.type == "RuntimeError"
            assert fault_info.message == "test exception"
            assert fault_info.engine_id == "0"
            assert fault_info.engine_status == EngineStatusType.UNHEALTHY
        finally:
            receiver.close(linger=0)
            sentinel.shutdown()
            ctx.term()

    def test_report_engine_loop_paused(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        ctx = zmq.Context()
        receiver = ctx.socket(zmq.ROUTER)
        receiver.bind(addr_dict["engine_fault_socket_addr"])
        try:
            sentinel.fault_signal_q.put(EngineLoopPausedError("paused"))
            time.sleep(0.2)
            if not receiver.poll(timeout=5000):
                pytest.fail("Timeout")
            parts = receiver.recv_multipart()
            fault_info = msgpack.decode(parts[-1], type=FaultInfo)
            assert fault_info.engine_status == EngineStatusType.PAUSED
        finally:
            receiver.close(linger=0)
            sentinel.shutdown()
            ctx.term()

    def test_no_fault_no_report(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        ctx = zmq.Context()
        receiver = ctx.socket(zmq.ROUTER)
        receiver.bind(addr_dict["engine_fault_socket_addr"])
        try:
            time.sleep(0.3)
            assert not receiver.poll(timeout=500)
        finally:
            receiver.close(linger=0)
            sentinel.shutdown()
            ctx.term()


class TestEngineCoreSentinelPause:
    def test_pause_sets_stop_flag(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        request = FaultToleranceRequest("1", "pause", {"timeout": 5})
        result = sentinel.pause(request)
        assert sentinel.stop_busy_loop.is_set()
        sentinel.shutdown()

    def test_pause_puts_wakeup(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        request = FaultToleranceRequest("1", "pause", {"timeout": 5})
        sentinel.pause(request)
        item = sentinel.engine_input_q.get(timeout=1)
        assert item[0] == EngineCoreRequestType.WAKEUP
        sentinel.shutdown()

    def test_pause_result_timeout(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        request = FaultToleranceRequest("1", "pause", {"timeout": 1})
        result = sentinel.pause(request)
        assert result.request_id == "1"
        assert result.success is False
        sentinel.shutdown()


class TestEngineCoreSentinelRetry:
    def test_retry_not_paused(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        request = FaultToleranceRequest("1", "retry", {"timeout": 5})
        result = sentinel.retry(request)
        assert result.success is True
        sentinel.shutdown()

    def test_retry_sets_coord_store_port(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        sentinel.busy_loop_paused.set()
        with patch.object(sentinel, "_execute_command_on_workers",
                          return_value=FaultToleranceRequest("r", "retry", {"timeout": 5})):
            request = FaultToleranceRequest(
                "1", "retry", {"timeout": 5, "coord_store_port": 54321},
            )
            sentinel.retry(request)
        assert mock_parallel_config._coord_store_port == 54321
        sentinel.shutdown()

    def test_retry_clears_stop_flag(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        sentinel.busy_loop_paused.set()
        sentinel.stop_busy_loop.set()
        with patch.object(sentinel, "_execute_command_on_workers",
                          return_value=FaultToleranceResult("1", True)):
            request = FaultToleranceRequest(
                "1", "retry", {"timeout": 5, "coord_store_port": 54321},
            )
            sentinel.retry(request)
        assert not sentinel.stop_busy_loop.is_set()
        sentinel.shutdown()

    def test_retry_paused_dp1(self, addr_dict, mock_parallel_config):
        mock_parallel_config.data_parallel_size = 1
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        sentinel.busy_loop_paused.set()
        with patch.object(sentinel, "_execute_command_on_workers",
                          return_value=FaultToleranceResult("1", True)):
            request = FaultToleranceRequest(
                "1", "retry", {"timeout": 5, "coord_store_port": 54321},
            )
            sentinel.retry(request)
        cmd = sentinel.cmd_q.get(timeout=1)
        assert cmd is None
        sentinel.shutdown()


class TestEngineCoreSentinelScaleDown:
    def test_calculate_exclude_ep_ranks(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        vllm_config = Mock(spec=VllmConfig)
        vllm_config.parallel_config = mock_parallel_config
        result = sentinel._calculate_exclude_ep_ranks([0], vllm_config)
        assert result == [0]

    def test_calculate_parallel_config(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        vllm_config = Mock(spec=VllmConfig)
        vllm_config.parallel_config = mock_parallel_config
        new_ep_size, new_dp_size = sentinel._calculate_parallel_config(
            vllm_config, [1]
        )
        assert new_dp_size == 1
        assert new_ep_size == 1

    def test_build_vllm_config_update_dict(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        result = sentinel._build_vllm_config_update_dict(
            mock_parallel_config, 2, 2, {0: 0},
        )
        assert result["ep_world_size"] == 2
        assert result["data_parallel_size"] == 2

    def test_shutdown_engine_core(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        request = FaultToleranceRequest("1", "shutdown", {})
        result = sentinel.shutdown_engine_core(request)
        assert result.success is True
        cmd = sentinel.cmd_q.get(timeout=1)
        assert cmd.instruction == "shutdown"
        sentinel.shutdown()

    def test_scale_down_timeout(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        sentinel.busy_loop_paused.set()
        request = FaultToleranceRequest(
            "1", "scale_down",
            {
                "timeout": 1,
                "original_to_new": {"0": 0},
                "exclude_dp_ranks": [1],
                "coord_store_port": 54321,
            },
        )
        result = sentinel.scale_down(request)
        assert result.success is False
        sentinel.shutdown()


class TestEngineCoreSentinelShutdown:
    def test_shutdown(self, addr_dict, mock_parallel_config):
        sentinel = make_sentinel(mock_parallel_config, addr_dict)
        sentinel.shutdown()
        assert sentinel.sentinel_dead is True
