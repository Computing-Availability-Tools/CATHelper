# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
import socket
from unittest.mock import Mock, patch

import pytest
import torch
import zmq
from msgspec import msgpack

from vllm.config import FaultToleranceConfig, ParallelConfig
from vllm.v1.fault_tolerance.utils import FaultToleranceRequest, FaultToleranceResult

from vllm_ascend.worker.sentinel.npu_worker_sentinel import (
    NPUWorkerSentinel,
    get_pause_event,
)

pytestmark = pytest.mark.skip_global_cleanup


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def addr_dict():
    return {"worker_cmd_addr": f"tcp://127.0.0.1:{_find_free_port()}"}


@pytest.fixture
def mock_parallel_config():
    config = Mock(spec=ParallelConfig)
    config.data_parallel_rank = 0
    config.data_parallel_size = 2
    config.data_parallel_master_ip = "127.0.0.1"
    config.data_parallel_master_port = 12345
    config.tensor_parallel_size = 1
    config.pipeline_parallel_size = 1
    config.gloo_timeout_seconds = 30
    config.fault_tolerance_config = FaultToleranceConfig()
    return config


@pytest.fixture
def mock_worker():
    worker = Mock()
    worker.model_loaded = True
    worker.num_logical_expert = 64
    worker.ep2dp_map = {i: i // 8 for i in range(16)}
    worker.vllm_config.parallel_config.enable_fault_tolerance = True
    worker.vllm_config.parallel_config.data_parallel_size = 2
    worker.vllm_config.parallel_config.data_parallel_rank = 0
    worker.vllm_config.parallel_config.gloo_timeout_seconds = 30
    worker.vllm_config.parallel_config.fault_tolerance_config = (
        FaultToleranceConfig()
    )
    worker.parallel_config = worker.vllm_config.parallel_config
    worker.model_runner.shared_dict = {
        "moe_load": None,
        "expert_maps": torch.zeros((2, 64), dtype=torch.long),
        "num_add_experts_per_rank": 0,
    }
    worker.model_runner.eplb_process = Mock()
    worker.model_runner.input_batch = Mock()
    worker.model_runner.input_batch.req_id_to_index = {}
    worker.model_runner.input_batch.remove_request = Mock()
    worker.model_runner.input_batch.condense = Mock()
    worker.model_runner.input_batch.refresh_metadata = Mock()
    worker.model_runner.async_output_copy_stream = Mock()
    worker.model_runner.prepare_inputs_event = Mock()
    worker.weight_name_to_tensor = {}
    return worker


@pytest.fixture
def sentinel(mock_parallel_config, addr_dict, mock_worker):
    mock_tp = Mock()
    mock_tp.rank_in_group = 0
    mock_pp = Mock()
    mock_pp.rank_in_group = 0
    mock_dp = Mock()
    mock_dp.cpu_group = Mock()
    with (
        patch("vllm_ascend.worker.sentinel.npu_worker_sentinel.make_zmq_socket"),
        patch("vllm_ascend.worker.sentinel.npu_worker_sentinel.get_tp_group",
              return_value=mock_tp),
        patch("vllm_ascend.worker.sentinel.npu_worker_sentinel.get_pp_group",
              return_value=mock_pp),
        patch("vllm_ascend.worker.sentinel.npu_worker_sentinel.get_dp_group",
              return_value=mock_dp),
        patch.object(NPUWorkerSentinel, "set_dp_gloo_timeout"),
        patch("torch.accelerator.set_device_index"),
    ):
        sentinel = NPUWorkerSentinel(
            parallel_config=mock_parallel_config,
            device=torch.device("npu", 0),
            worker_cmd_addr=addr_dict["worker_cmd_addr"],
            worker=mock_worker,
        )
    return sentinel


class TestNPUWorkerSentinelInitialization:
    def test_dp_rank(self, sentinel):
        assert sentinel.dp_rank == 0

    def test_dp_size(self, sentinel):
        assert sentinel.dp_size == 2

    def test_device(self, sentinel):
        assert sentinel.device.index == 0

    def test_identity(self, sentinel):
        assert sentinel.identity == b"PP0_TP0"

    def test_sentinel_tag(self, sentinel):
        assert "0_PP0_TP0" in sentinel.sentinel_tag


class TestNPUWorkerSentinelPause:
    def test_pause_sets_global_event(self, sentinel):
        get_pause_event().clear()
        request = FaultToleranceRequest("1", "pause", {})
        with patch("torch_npu.npu.stop_device", return_value=0):
            result = sentinel.pause(request)
        assert result.success is True
        assert get_pause_event().is_set()

    def test_pause_stop_device_fail(self, sentinel):
        request = FaultToleranceRequest("1", "pause", {})
        with patch("torch_npu.npu.stop_device", return_value=1):
            result = sentinel.pause(request)
        assert result.success is False

    def test_pause_stop_device_error(self, sentinel):
        request = FaultToleranceRequest("1", "pause", {})
        with patch("torch_npu.npu.stop_device", return_value=2):
            try:
                sentinel.pause(request)
                pytest.fail("Expected ValueError")
            except ValueError:
                pass


class TestNPUWorkerSentinelRetry:
    def test_retry_clears_global_event(self, sentinel):
        get_pause_event().set()
        with (
            patch.object(sentinel, "clean_states",
                         side_effect=lambda: get_pause_event().clear()),
            patch("vllm_ascend.worker.sentinel.npu_worker_sentinel.stateless_init_torch_distributed_process_group"),
            patch("vllm_ascend.worker.sentinel.npu_worker_sentinel._set_pg_timeout"),
            patch("vllm_ascend.worker.sentinel.npu_worker_sentinel.get_dp_group") as mock_get_dp,
        ):
            mock_dp = Mock()
            mock_dp.cpu_group = Mock()
            mock_get_dp.return_value = mock_dp
            request = FaultToleranceRequest(
                "1", "retry", {"new_stateless_dp_group_port": 23456},
            )
            result = sentinel.retry(request)
        assert result.success is True
        assert not get_pause_event().is_set()

    def test_retry_reinit_dp_group(self, sentinel):
        get_pause_event().set()
        with (
            patch.object(sentinel, "clean_states") as mock_clean,
            patch("vllm_ascend.worker.sentinel.npu_worker_sentinel.stateless_init_torch_distributed_process_group") as mock_init,
            patch("vllm_ascend.worker.sentinel.npu_worker_sentinel._set_pg_timeout"),
            patch("vllm_ascend.worker.sentinel.npu_worker_sentinel.get_dp_group") as mock_get_dp,
        ):
            mock_dp = Mock()
            mock_dp.cpu_group = Mock()
            mock_get_dp.return_value = mock_dp
            request = FaultToleranceRequest(
                "1", "retry", {"new_stateless_dp_group_port": 23456},
            )
            sentinel.retry(request)
            mock_clean.assert_called_once()
            mock_init.assert_called_once_with(
                "127.0.0.1", 23456, 0, 2, backend="gloo", group_name=mock_init.call_args[1]["group_name"],
            )


class TestNPUWorkerSentinelScaleDown:
    def test_scale_down_success(self, sentinel):
        with (
            patch.object(sentinel, "clean_states") as mock_clean,
            patch.object(sentinel, "scale_down_worker") as mock_scale,
            patch.object(sentinel.worker, "execute_dummy_batch"),
            patch.object(sentinel, "_coord_store_port", 54321, create=True),
            patch("torch.npu.synchronize"),
            patch("vllm_ascend.worker.sentinel.npu_worker_sentinel.get_cached_tcp_store_client"),
        ):
            request = FaultToleranceRequest(
                "1", "scale_down",
                {
                    "timeout": 30,
                    "exclude_ep_ranks": [0],
                    "vllm_config_update_dict": {"rank_mapping": {0: 0}},
                    "coord_store_port": 54321,
                },
            )
            result = sentinel.scale_down(request)
        assert result.success is True
        mock_clean.assert_called_once()

    def test_clean_states(self, sentinel):
        get_pause_event().set()
        with (
            patch("torch_npu.npu.stop_device"),
            patch("torch_npu.npu.restart_device"),
            patch("torch_npu.distributed.reinit_process_group"),
            patch("torch.npu.synchronize"),
            patch("vllm_ascend.worker.sentinel.npu_worker_sentinel.NPUPlatform.set_device"),
            patch("torch.cuda.Stream"),
        ):
            sentinel.clean_states()
        assert not get_pause_event().is_set()


class TestNPUWorkerSentinelShutdown:
    def test_shutdown(self, sentinel):
        with patch("vllm_ascend.worker.sentinel.npu_worker_sentinel.close_sockets"):
            sentinel.shutdown()
        assert sentinel.sentinel_dead is True


class TestGlobalPauseEvent:
    def test_get_pause_event_singleton(self):
        assert get_pause_event() is get_pause_event()

    def test_get_pause_event_default_cleared(self):
        get_pause_event().clear()
        assert not get_pause_event().is_set()
