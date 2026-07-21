# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
import uuid
from unittest.mock import AsyncMock, Mock, patch

import msgspec.msgpack
import pytest
import zmq

from vllm.config import FaultToleranceConfig, ParallelConfig
from vllm.v1.engine import EngineStatusType
from vllm.v1.fault_tolerance.client_sentinel import ClientSentinel
from vllm.v1.fault_tolerance.utils import FaultInfo, FaultToleranceRequest

pytestmark = pytest.mark.skip_global_cleanup


@pytest.fixture
def mock_ft_addresses():
    addresses = Mock()
    addresses.ft_request_addresses = ["tcp://127.0.0.1:5555"]
    addresses.ft_result_addresses = ["tcp://127.0.0.1:5556"]
    addresses.engine_fault_socket_addr = "tcp://127.0.0.1:5557"
    addresses.fault_state_pub_socket_addr = "tcp://127.0.0.1:5558"
    return addresses


@pytest.fixture
def mock_call_utility_async():
    return AsyncMock(
        return_value={"request_id": "request_id", "success": True, "reason": None}
    )


@pytest.fixture
def mock_parallel_config():
    config = Mock(spec=ParallelConfig)
    config.data_parallel_index = 0
    config.data_parallel_size = 2
    config.data_parallel_size_local = 2
    config.local_engines_only = False
    config.gloo_timeout_seconds = 5
    config.fault_tolerance_config = FaultToleranceConfig()
    return config


@pytest.fixture
def client_sentinel(mock_parallel_config, mock_ft_addresses, mock_call_utility_async):
    fault_receiver_socket = AsyncMock()
    with (
        patch(
            "vllm.v1.fault_tolerance.client_sentinel.make_zmq_socket",
            return_value=fault_receiver_socket,
        ),
        patch("vllm.v1.fault_tolerance.client_sentinel.asyncio.create_task"),
    ):
        core_client = Mock()
        core_client.core_engines = [b"engine_0", b"engine_1"]
        core_client.engine_registry = {0: b"engine_0", 1: b"engine_1"}
        sentinel = ClientSentinel(
            parallel_config=mock_parallel_config,
            fault_tolerance_addresses=mock_ft_addresses,
            call_utility_async=mock_call_utility_async,
            core_engines=[b"engine_0", b"engine_1"],
            core_client=core_client,
        )
    return sentinel


class TestClientSentinelInitialization:
    def test_engine_status_dict(self, client_sentinel):
        assert client_sentinel.engine_status_dict == {
            0: {"status": "healthy"},
            1: {"status": "healthy"},
        }

    def test_start_rank(self, client_sentinel):
        assert client_sentinel.start_rank == 0

    def test_sockets_created(self, client_sentinel):
        assert client_sentinel.fault_receiver_socket is not None
        assert client_sentinel.fault_state_pub_socket is not None
        assert len(client_sentinel.ft_request_sockets) == 1
        assert len(client_sentinel.ft_result_sockets) == 1


class TestClientSentinelPause:
    @pytest.mark.asyncio
    async def test_pause_success(self, client_sentinel, mock_call_utility_async):
        mock_call_utility_async.return_value = {
            "request_id": "request_id", "success": True, "reason": None,
        }
        request = FaultToleranceRequest.builder(
            request_id="request_id", instruction="pause", params={"timeout": 3},
        )
        result = await client_sentinel.pause(request)

        assert result.request_id == "request_id"
        assert result.success is True
        assert result.reason is None
        assert mock_call_utility_async.call_count == 2
        for call in mock_call_utility_async.call_args_list:
            assert call.args[0] == "handle_fault"
            assert call.kwargs["engine"] in {b"engine_0", b"engine_1"}

    @pytest.mark.asyncio
    async def test_pause_with_exclude(self, client_sentinel, mock_call_utility_async):
        mock_call_utility_async.return_value = {
            "request_id": "request_id", "success": True, "reason": None,
        }
        request = FaultToleranceRequest.builder(
            request_id="request_id", instruction="pause",
            params={"timeout": 3, "exclude_engine_index": [0]},
        )
        result = await client_sentinel.pause(request)

        assert result.success is True

    @pytest.mark.asyncio
    async def test_pause_timeout(self, client_sentinel, mock_call_utility_async):
        async def slow_response(*args, **kwargs):
            await asyncio.sleep(0.1)
            return {"request_id": "request_id", "success": True, "reason": None}

        mock_call_utility_async.side_effect = slow_response
        request = FaultToleranceRequest.builder(
            request_id="request_id", instruction="pause",
            params={"timeout": 0.01},
        )
        result = await client_sentinel.pause(request)

        assert result.success is False
        assert "Timed out" in result.reason


class TestClientSentinelRetry:
    @pytest.mark.asyncio
    async def test_retry_success(self, client_sentinel, mock_call_utility_async):
        mock_call_utility_async.return_value = {
            "request_id": "request_id", "success": True, "reason": None,
        }
        request = FaultToleranceRequest.builder(
            request_id="request_id", instruction="retry", params={"timeout": 3},
        )
        result = await client_sentinel.retry(request)

        assert result.success is True
        assert not client_sentinel.is_faulted.is_set()

    @pytest.mark.asyncio
    async def test_retry_dead_engine(self, client_sentinel):
        client_sentinel.engine_status_dict[1] = {"status": "dead"}
        request = FaultToleranceRequest.builder(
            request_id="request_id", instruction="retry", params={"timeout": 3},
        )
        result = await client_sentinel.retry(request)

        assert result.success is False
        assert "dead" in result.reason.lower()


class TestClientSentinelScaleDown:
    def test_get_mapping(self):
        mapping = ClientSentinel.get_mapping([0, 1, 2, 3], [1])
        assert mapping == {0: 0, 2: 1, 3: 2}

    def test_get_mapping_multiple(self):
        mapping = ClientSentinel.get_mapping([0, 1, 2, 3], [0, 2])
        assert mapping == {1: 0, 3: 1}

    def test_update_config(self, client_sentinel):
        client_sentinel.update_config(exclude_dp_ranks=[1], original_to_new={0: 0})
        assert 1 not in client_sentinel.engine_status_dict
        assert client_sentinel.engine_identity_to_index == {b"engine_0": 0}
        assert client_sentinel.engine_identities == [b"engine_0"]

    @pytest.mark.asyncio
    async def test_scale_down_success(self, client_sentinel, mock_call_utility_async):
        mock_call_utility_async.return_value = {
            "request_id": "request_id", "success": True, "reason": None,
        }
        client_sentinel.is_faulted.set()
        request = FaultToleranceRequest.builder(
            request_id="request_id", instruction="scale_down",
            params={"timeout": 3, "exclude_dp_ranks": [1]},
        )
        with patch.object(client_sentinel, "terminate_scale_down_engines",
                          return_value=FaultToleranceResult("r", True)):
            result = await client_sentinel.scale_down(request)

        assert result.success is True
        assert not client_sentinel.is_faulted.is_set()


class TestClientSentinelFaultReporting:
    @pytest.mark.asyncio
    async def test_fault_updates_status(self, client_sentinel):
        fault_info = FaultInfo(
            engine_id="0", type="EngineDeadError", message="dead",
            engine_status=EngineStatusType.DEAD,
            engine_identity=b"engine_0",
        )
        client_sentinel.fault_receiver_socket.recv_multipart = AsyncMock(
            side_effect=[
                [b"", b"", msgspec.msgpack.encode(fault_info)],
                zmq.ZMQError(),
            ]
        )
        await client_sentinel.run()

        assert client_sentinel.engine_status_dict[0]["status"] == "dead"
        client_sentinel.fault_state_pub_socket.send_multipart.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fault_triggers_pause(self, client_sentinel):
        fault_info = FaultInfo(
            engine_id="0", type="RuntimeError", message="error",
            engine_status=EngineStatusType.UNHEALTHY,
            engine_identity=b"engine_0",
        )
        client_sentinel.fault_receiver_socket.recv_multipart = AsyncMock(
            side_effect=[
                [b"", b"", msgspec.msgpack.encode(fault_info)],
                zmq.ZMQError(),
            ]
        )
        with patch.object(client_sentinel, "pause", new_callable=AsyncMock):
            await client_sentinel.run()
            assert client_sentinel.is_faulted.is_set()

    def test_get_mapping_static(self):
        result = ClientSentinel.get_mapping([0, 1, 2, 3, 4], [2, 4])
        assert result == {0: 0, 1: 1, 3: 2}

    @pytest.mark.asyncio
    async def test_pub_engine_status(self, client_sentinel):
        await client_sentinel._pub_engine_status()
        client_sentinel.fault_state_pub_socket.send_multipart.assert_awaited_once()


class TestClientSentinelShutdown:
    def test_shutdown_sets_flag(self, client_sentinel):
        with patch("vllm.v1.fault_tolerance.client_sentinel.close_sockets"):
            client_sentinel.shutdown()
        assert client_sentinel.sentinel_dead is True
