#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the CATHelper project
"""Unit tests for the CATMonitor fault subscriber (Phase C).

Run: python3 -m unittest test_catmonitor_fault_sub -v

Covers the pure logic: NPU->DP mapping, fault-type parsing, and event
handling with a mocked vLLM REST API (no real vLLM / DCMI / CATMonitor
needed). The HTTP webhook round-trip is exercised end-to-end with a mock
CATMonitor REST server and a mock vLLM.
"""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from catmonitor_fault_sub import (
    CatMonitorFaultSubscriber,
    FaultSubscriberConfig,
    build_npu_to_dp_ranks,
    parse_fault_types,
    parse_npu_ids,
)


class TestParsing(unittest.TestCase):
    def test_build_npu_to_dp_ranks_single_device_per_die(self):
        m = build_npu_to_dp_ranks([0, 1, 2, 3], 1, [0, 1, 2, 3], 4)
        self.assertEqual(m, {0: [0], 1: [1], 2: [2], 3: [3]})

    def test_build_npu_to_dp_ranks_dual_die_a3(self):
        # A3: 8 DIE x 2 physical cards = 16 cards; DIE 5 hosts cards 10, 11.
        m = build_npu_to_dp_ranks(
            list(range(8)), 2, list(range(16)), 16
        )
        self.assertEqual(m[5], [10, 11])
        self.assertEqual(m[0], [0, 1])
        self.assertEqual(m[7], [14, 15])
        self.assertEqual(len(m), 8)

    def test_build_npu_to_dp_ranks_filters_unused_dies(self):
        # dp-size=8 with visible 0-15 -> only physical cards 0-7 are used,
        # i.e. DIE 0-3; DIE 4-7 get no ranks.
        m = build_npu_to_dp_ranks(list(range(8)), 2, list(range(16)), 8)
        self.assertEqual(m, {0: [0, 1], 1: [2, 3], 2: [4, 5], 3: [6, 7]})

    def test_build_npu_to_dp_ranks_respects_npu_ids_scope(self):
        m = build_npu_to_dp_ranks([5], 2, list(range(16)), 16)
        self.assertEqual(m, {5: [10, 11]})

    def test_build_npu_to_dp_ranks_dp_size_out_of_range(self):
        with self.assertRaises(ValueError):
            build_npu_to_dp_ranks([0], 2, list(range(16)), 20)

    def test_parse_npu_ids_range(self):
        self.assertEqual(parse_npu_ids("0-3"), [0, 1, 2, 3])

    def test_parse_npu_ids_list(self):
        self.assertEqual(parse_npu_ids("0,1,5"), [0, 1, 5])

    def test_parse_fault_types_ok(self):
        self.assertEqual(
            parse_fault_types("card_drop,hbm_uce"),
            ["card_drop", "hbm_uce"],
        )

    def test_parse_fault_types_unknown_exits(self):
        with self.assertRaises(SystemExit):
            parse_fault_types("bogus_type")


class TestEventHandler(unittest.TestCase):
    def _make_subscriber(self):
        cfg = FaultSubscriberConfig(
            vllm_host="localhost",
            vllm_port=8006,
            catmonitor_host="localhost",
            catmonitor_rest_port=9101,
            callback_host="127.0.0.1",
            callback_port=0,  # unused; we call _handle_event directly
            advertise_url="http://127.0.0.1:9102/fault_event",
            npu_ids=[0, 1, 2, 3],
        )
        npu_to_dp = build_npu_to_dp_ranks([0, 1, 2, 3], 1, [0, 1, 2, 3], 4)
        return CatMonitorFaultSubscriber(cfg, npu_to_dp)

    def test_card_drop_triggers_scale_down_after_pause(self):
        sub = self._make_subscriber()
        with (
            mock.patch.object(sub, "_vllm_apply", return_value=True) as apply,
            mock.patch.object(sub, "_wait_for_pause", return_value=True) as wait_pause,
        ):
            sub._handle_event(
                {"type": "card_drop", "npu_id": "2", "recovered": False}
            )
            wait_pause.assert_called_once_with([2])
            instructions = [c.args[0] for c in apply.call_args_list]
            self.assertEqual(instructions, ["scale_down"])

    def test_card_drop_dual_die_scales_down_both_ranks(self):
        # A3: DIE 5 = physical cards 10,11 = DP ranks 10,11. A fault on the
        # DIE must exclude BOTH ranks, not a single positional rank.
        cfg = FaultSubscriberConfig(
            vllm_host="localhost",
            vllm_port=8006,
            catmonitor_host="localhost",
            catmonitor_rest_port=9101,
            callback_host="127.0.0.1",
            callback_port=0,
            advertise_url="http://127.0.0.1:9102/fault_event",
            npu_ids=list(range(8)),
        )
        npu_to_dp = build_npu_to_dp_ranks(list(range(8)), 2, list(range(16)), 16)
        sub = CatMonitorFaultSubscriber(cfg, npu_to_dp)
        with (
            mock.patch.object(sub, "_vllm_apply", return_value=True) as apply,
            mock.patch.object(sub, "_wait_for_pause", return_value=True) as wait_pause,
        ):
            sub._handle_event(
                {"type": "card_drop", "npu_id": "5", "recovered": False}
            )
            wait_pause.assert_called_once_with([10, 11])
            scale_call = apply.call_args
            self.assertEqual(scale_call.args[0], "scale_down")
            self.assertEqual(scale_call.args[1]["exclude_dp_ranks"], [10, 11])

    def test_die_not_in_deployment_skipped(self):
        # dp-size=8 with visible 0-15 uses physical cards 0-7 (DIE 0-3); a
        # fault on DIE 5 (cards 10,11) is outside the deployment -> skip.
        cfg = FaultSubscriberConfig(
            vllm_host="localhost",
            vllm_port=8006,
            catmonitor_host="localhost",
            catmonitor_rest_port=9101,
            callback_host="127.0.0.1",
            callback_port=0,
            advertise_url="http://127.0.0.1:9102/fault_event",
            npu_ids=list(range(8)),
        )
        npu_to_dp = build_npu_to_dp_ranks(list(range(8)), 2, list(range(16)), 8)
        sub = CatMonitorFaultSubscriber(cfg, npu_to_dp)
        with mock.patch.object(sub, "_vllm_apply", return_value=True) as apply:
            sub._handle_event(
                {"type": "card_drop", "npu_id": "5", "recovered": False}
            )
            apply.assert_not_called()

    def test_unknown_npu_skipped(self):
        sub = self._make_subscriber()
        with mock.patch.object(sub, "_vllm_apply") as apply:
            sub._handle_event(
                {"type": "card_drop", "npu_id": "99", "recovered": False}
            )
            apply.assert_not_called()

    def test_recovery_triggers_retry(self):
        sub = self._make_subscriber()
        with mock.patch.object(sub, "_vllm_apply", return_value=True) as apply:
            # Track the fault first (non-recovered event), then recovery.
            sub._handle_event(
                {"type": "roce_link_down", "npu_id": "1", "recovered": False}
            )
            sub._handle_event(
                {"type": "roce_link_down", "npu_id": "1", "recovered": True}
            )
            apply.assert_called_once()
            self.assertEqual(apply.call_args.args[0], "retry")

    def test_recovery_untracked_benign_fault_skips_retry(self):
        # Benign npu_error_code is ignored (never tracked); its recovery must
        # not send retry to healthy engines.
        sub = self._make_subscriber()
        with (
            mock.patch.object(sub, "_vllm_apply", return_value=True) as apply,
            mock.patch.object(sub, "_wait_for_pause", return_value=True) as wait_pause,
        ):
            sub._handle_event(
                {
                    "type": "npu_error_code",
                    "npu_id": "1",
                    "recovered": False,
                    "detail": {"error_codes": "0x80f38003"},
                }
            )
            sub._handle_event(
                {"type": "npu_error_code", "npu_id": "1", "recovered": True, "detail": {}}
            )
            apply.assert_not_called()
            wait_pause.assert_not_called()

    def test_recovery_of_acted_npu_error_code_sends_retry(self):
        sub = self._make_subscriber()
        with (
            mock.patch.object(sub, "_vllm_apply", return_value=True) as apply,
            mock.patch.object(sub, "_wait_for_pause", return_value=True) as wait_pause,
        ):
            sub._handle_event(
                {
                    "type": "npu_error_code",
                    "npu_id": "1",
                    "recovered": False,
                    "detail": {"error_codes": "0x12345678"},
                }
            )
            sub._handle_event(
                {"type": "npu_error_code", "npu_id": "1", "recovered": True, "detail": {}}
            )
            instructions = [c.args[0] for c in apply.call_args_list]
            self.assertEqual(instructions, ["scale_down", "retry"])

    def test_npu_error_code_all_benign_ignored(self):
        sub = self._make_subscriber()
        with (
            mock.patch.object(sub, "_vllm_apply", return_value=True) as apply,
            mock.patch.object(sub, "_wait_for_pause", return_value=True) as wait_pause,
        ):
            sub._handle_event(
                {
                    "type": "npu_error_code",
                    "npu_id": "1",
                    "recovered": False,
                    "detail": {"error_codes": "0x80f38003"},
                }
            )
            apply.assert_not_called()
            wait_pause.assert_not_called()

    def test_npu_error_code_ignore_is_case_insensitive(self):
        sub = self._make_subscriber()
        with mock.patch.object(sub, "_vllm_apply", return_value=True) as apply:
            sub._handle_event(
                {
                    "type": "npu_error_code",
                    "npu_id": "1",
                    "recovered": False,
                    "detail": {"error_codes": "0X80F38003"},
                }
            )
            apply.assert_not_called()

    def test_npu_error_code_mixed_codes_scale_down(self):
        sub = self._make_subscriber()
        with (
            mock.patch.object(sub, "_vllm_apply", return_value=True) as apply,
            mock.patch.object(sub, "_wait_for_pause", return_value=True) as wait_pause,
        ):
            sub._handle_event(
                {
                    "type": "npu_error_code",
                    "npu_id": "1",
                    "recovered": False,
                    "detail": {"error_codes": "0x80f38003,0x12345678"},
                }
            )
            wait_pause.assert_called_once_with([1])
            instructions = [c.args[0] for c in apply.call_args_list]
            self.assertEqual(instructions, ["scale_down"])

    def test_npu_error_code_non_benign_scale_down(self):
        sub = self._make_subscriber()
        with (
            mock.patch.object(sub, "_vllm_apply", return_value=True) as apply,
            mock.patch.object(sub, "_wait_for_pause", return_value=True) as wait_pause,
        ):
            sub._handle_event(
                {
                    "type": "npu_error_code",
                    "npu_id": "1",
                    "recovered": False,
                    "detail": {"error_codes": "0x12345678"},
                }
            )
            wait_pause.assert_called_once_with([1])
            instructions = [c.args[0] for c in apply.call_args_list]
            self.assertEqual(instructions, ["scale_down"])

    def test_npu_error_code_empty_ignore_list_scales_down(self):
        cfg = FaultSubscriberConfig(
            vllm_host="localhost",
            vllm_port=8006,
            catmonitor_host="localhost",
            catmonitor_rest_port=9101,
            callback_host="127.0.0.1",
            callback_port=0,
            advertise_url="http://127.0.0.1:9102/fault_event",
            npu_ids=[0, 1, 2, 3],
            ignore_error_codes=[],
        )
        sub = CatMonitorFaultSubscriber(cfg, build_npu_to_dp_ranks([0, 1, 2, 3], 1, [0, 1, 2, 3], 4))
        with (
            mock.patch.object(sub, "_vllm_apply", return_value=True) as apply,
            mock.patch.object(sub, "_wait_for_pause", return_value=True) as wait_pause,
        ):
            sub._handle_event(
                {
                    "type": "npu_error_code",
                    "npu_id": "1",
                    "recovered": False,
                    "detail": {"error_codes": "0x80f38003"},
                }
            )
            wait_pause.assert_called_once_with([1])
            self.assertEqual(apply.call_count, 1)

    def test_roce_link_down_not_recovered_log_only(self):
        sub = self._make_subscriber()
        with mock.patch.object(sub, "_vllm_apply", return_value=True) as apply:
            sub._handle_event(
                {"type": "roce_link_down", "npu_id": "2", "recovered": False}
            )
            apply.assert_not_called()

    def test_duplicate_persistent_fault_skipped(self):
        sub = self._make_subscriber()
        with (
            mock.patch.object(sub, "_vllm_apply", return_value=True) as apply,
            mock.patch.object(sub, "_wait_for_pause", return_value=True),
        ):
            sub._handle_event(
                {"type": "card_drop", "npu_id": "3", "recovered": False}
            )
            sub._handle_event(
                {"type": "card_drop", "npu_id": "3", "recovered": False}
            )
            # First event does wait_for_pause+scale_down (1 call); duplicate adds 0.
            self.assertEqual(apply.call_count, 1)

    def test_concurrent_events_same_die_single_scale_down(self):
        # Two DIFFERENT fault types for the same DIE arriving close together
        # (non-benign npu_error_code then card_drop) used to start TWO
        # concurrent _wait_for_pause+scale_down cycles, because the dedup only
        # matches the same type. The second (stale) scale_down then targeted
        # ranks already removed by the first, and vLLM timed out on them.
        # Regression: the in-progress per-NPU guard allows only one scale_down.
        sub = self._make_subscriber()

        def blocking_wait(exclude):
            time.sleep(0.3)  # keep the first flow's pause-wait window open
            return True

        with (
            mock.patch.object(sub, "_wait_for_pause", side_effect=blocking_wait),
            mock.patch.object(sub, "scale_down", return_value=True) as sd,
        ):
            t1 = threading.Thread(
                target=sub._handle_event,
                args=(
                    {
                        "type": "npu_error_code",
                        "npu_id": "2",
                        "recovered": False,
                        "detail": {"error_codes": "0x40f84e00"},
                    },
                ),
            )
            t2 = threading.Thread(
                target=sub._handle_event,
                args=({"type": "card_drop", "npu_id": "2", "recovered": False},),
            )
            t1.start()
            time.sleep(0.05)  # let thread 1 claim the NPU
            t2.start()
            t1.join()
            t2.join()
        self.assertEqual(sd.call_count, 1)

    def test_concurrent_events_different_die_both_scaled(self):
        # Different DIEs may still be handled in parallel: the per-NPU guard
        # must not serialize unrelated faults.
        sub = self._make_subscriber()

        def blocking_wait(exclude):
            time.sleep(0.3)
            return True

        with (
            mock.patch.object(sub, "_wait_for_pause", side_effect=blocking_wait),
            mock.patch.object(sub, "scale_down", return_value=True) as sd,
        ):
            t1 = threading.Thread(
                target=sub._handle_event,
                args=({"type": "card_drop", "npu_id": "1", "recovered": False},),
            )
            t2 = threading.Thread(
                target=sub._handle_event,
                args=({"type": "card_drop", "npu_id": "2", "recovered": False},),
            )
            t1.start()
            time.sleep(0.05)
            t2.start()
            t1.join()
            t2.join()
        self.assertEqual(sd.call_count, 2)


class TestDynamicMapping(unittest.TestCase):
    """Option A: after a successful scale_down the manager drops the faulted
    DIE's physical cards and rebuilds the NPU->DP map (vLLM renumbers the
    surviving engines to contiguous ranks in their original order)."""

    def _make_dynamic_subscriber(self):
        cfg = FaultSubscriberConfig(
            vllm_host="localhost",
            vllm_port=8006,
            catmonitor_host="localhost",
            catmonitor_rest_port=9101,
            callback_host="127.0.0.1",
            callback_port=0,
            advertise_url="http://127.0.0.1:9102/fault_event",
            npu_ids=list(range(8)),
        )
        npu_to_dp = build_npu_to_dp_ranks(list(range(8)), 2, list(range(16)), 8)
        return CatMonitorFaultSubscriber(
            cfg,
            npu_to_dp,
            visible_devices=list(range(16)),
            dp_size=8,
            npu_per_die=2,
            npu_ids=list(range(8)),
        )

    def test_scale_down_success_removes_die_and_renumbers_ranks(self):
        sub = self._make_dynamic_subscriber()
        self.assertEqual(sub.npu_to_dp[1], [2, 3])
        with (
            mock.patch.object(sub, "_vllm_apply", return_value=True) as apply,
            mock.patch.object(sub, "_wait_for_pause", return_value=True),
        ):
            sub._handle_event(
                {"type": "card_drop", "npu_id": "1", "recovered": False}
            )
        self.assertEqual(apply.call_count, 1)
        # DIE 1's cards (2,3) are gone; surviving DIE 0/2/3 renumber to 0-5.
        self.assertEqual(sub.npu_to_dp, {0: [0, 1], 2: [2, 3], 3: [4, 5]})
        self.assertEqual(sub.dp_size, 6)
        self.assertNotIn(1, sub.npu_to_dp)

    def test_scale_down_failure_keeps_mapping(self):
        sub = self._make_dynamic_subscriber()
        with (
            mock.patch.object(sub, "_vllm_apply", return_value=False) as apply,
            mock.patch.object(sub, "_wait_for_pause", return_value=True),
        ):
            sub._handle_event(
                {"type": "card_drop", "npu_id": "1", "recovered": False}
            )
        self.assertEqual(apply.call_count, 1)
        self.assertEqual(sub.npu_to_dp[1], [2, 3])  # unchanged

    def test_event_on_scaled_down_die_skipped(self):
        sub = self._make_dynamic_subscriber()
        with (
            mock.patch.object(sub, "_vllm_apply", return_value=True) as apply,
            mock.patch.object(sub, "_wait_for_pause", return_value=True),
        ):
            sub._handle_event(
                {"type": "card_drop", "npu_id": "1", "recovered": False}
            )
            sub._handle_event(
                {"type": "npu_health", "npu_id": "1", "recovered": False}
            )
        # DIE 1 is gone from the map, so the second event is skipped.
        self.assertEqual(apply.call_count, 1)

    def test_recovered_event_on_scaled_down_die_skips_retry(self):
        sub = self._make_dynamic_subscriber()
        with (
            mock.patch.object(sub, "_vllm_apply", return_value=True) as apply,
            mock.patch.object(sub, "_wait_for_pause", return_value=True),
        ):
            sub._handle_event(
                {"type": "card_drop", "npu_id": "1", "recovered": False}
            )
            sub._handle_event(
                {"type": "card_drop", "npu_id": "1", "recovered": True}
            )
        # No retry for a DIE that was scaled down and is no longer deployed.
        instructions = [c.args[0] for c in apply.call_args_list]
        self.assertEqual(instructions, ["scale_down"])

    def test_double_scale_down_renumbers_twice(self):
        sub = self._make_dynamic_subscriber()
        with (
            mock.patch.object(sub, "_vllm_apply", return_value=True) as apply,
            mock.patch.object(sub, "_wait_for_pause", return_value=True),
        ):
            sub._handle_event(
                {"type": "card_drop", "npu_id": "1", "recovered": False}
            )
            sub._handle_event(
                {"type": "card_drop", "npu_id": "2", "recovered": False}
            )
        # After DIE 1 removed: {0:[0,1], 2:[2,3], 3:[4,5]} -> DIE 2 maps to
        # [2,3]; after removing DIE 2 the survivors (DIE 0 and DIE 3) renumber.
        self.assertEqual(sub.npu_to_dp, {0: [0, 1], 3: [2, 3]})
        scale_ranks = [c.args[1]["exclude_dp_ranks"] for c in apply.call_args_list]
        self.assertEqual(scale_ranks, [[2, 3], [2, 3]])

    def test_recovered_while_scale_down_in_progress_is_skipped(self):
        # Regression: in the IDLE scenario the recovered event can arrive while
        # _wait_for_pause is still blocking (healthy engines -> manual pause ->
        # long window). The old code popped _active_faults and fired a stale
        # retry at ranks being removed (vLLM then 500s and the in-flight
        # scale_down stalls). Recovery during an in-progress scale_down must be
        # ignored; _on_scale_down_success clears the fault on completion.
        sub = self._make_dynamic_subscriber()

        def blocking_wait(exclude):
            time.sleep(0.3)  # keep the pause-wait window open
            return True

        with (
            mock.patch.object(sub, "_wait_for_pause", side_effect=blocking_wait),
            mock.patch.object(sub, "scale_down", return_value=True) as sd,
            mock.patch.object(sub, "retry", return_value=True) as retry_mock,
        ):
            t1 = threading.Thread(
                target=sub._handle_event,
                args=({"type": "card_drop", "npu_id": "1", "recovered": False},),
            )
            t1.start()
            deadline = time.time() + 2
            while "1" not in sub._handling and time.time() < deadline:
                time.sleep(0.01)
            # CATMonitor reports recovery while the scale_down is in progress.
            sub._handle_event(
                {"type": "card_drop", "npu_id": "1", "recovered": True}
            )
            t1.join()
        sd.assert_called_once()
        retry_mock.assert_not_called()
        # scale_down succeeded -> _on_scale_down_success cleared the fault and
        # removed the DIE from the deployment.
        self.assertEqual(sub._active_faults, {})
        self.assertNotIn(1, sub.npu_to_dp)


class TestWaitForPause(unittest.TestCase):
    """Regression: _wait_for_pause must parse vLLM's real /fault_tolerance/status
    shape ({"total_engines":..., "engines":[{"id","status"}]}), wait for every
    retained rank to be paused/dead, and keep polling while any is healthy."""

    def _make_subscriber(self):
        cfg = FaultSubscriberConfig(
            vllm_host="localhost",
            vllm_port=8006,
            catmonitor_host="localhost",
            catmonitor_rest_port=9101,
            callback_host="127.0.0.1",
            callback_port=0,
            npu_ids=[0, 1, 2, 3],
        )
        return CatMonitorFaultSubscriber(cfg, build_npu_to_dp_ranks([0, 1, 2, 3], 1, [0, 1, 2, 3], 4))

    def test_returns_true_once_retained_ranks_paused(self):
        sub = self._make_subscriber()

        def fake_get(url, timeout=5):
            r = mock.Mock()
            r.status_code = 200
            r.json.return_value = {
                "total_engines": 4,
                "engines": [
                    {"id": 0, "status": "paused"},
                    {"id": 1, "status": "paused"},
                    {"id": 2, "status": "dead"},     # excluded rank (failed)
                    {"id": 3, "status": "paused"},
                ],
            }
            return r

        with mock.patch("requests.get", side_effect=fake_get) as get:
            self.assertTrue(sub._wait_for_pause([2], poll_interval=1, max_wait=5))
        # One poll was enough; the excluded (dead) rank does not block us.
        self.assertEqual(get.call_count, 1)

    def test_retained_ranks_unhealthy_satisfies_pause(self):
        # vLLM scale_down rejects only retained engines that are still healthy;
        # retained "unhealthy" (e.g. failed pause) is acceptable. The old code
        # only accepted "paused"/"dead" and timed out here.
        sub = self._make_subscriber()

        def fake_get(url, timeout=5):
            r = mock.Mock()
            r.status_code = 200
            r.json.return_value = {
                "total_engines": 4,
                "engines": [
                    {"id": 0, "status": "unhealthy"},
                    {"id": 1, "status": "unhealthy"},
                    {"id": 2, "status": "dead"},     # excluded rank (failed)
                    {"id": 3, "status": "unhealthy"},
                ],
            }
            return r

        with mock.patch("requests.get", side_effect=fake_get) as get:
            self.assertTrue(sub._wait_for_pause([2], poll_interval=1, max_wait=5))
        self.assertEqual(get.call_count, 1)

    def test_keeps_polling_until_healthy_rank_pauses(self):
        sub = self._make_subscriber()

        responses = [
            {"total_engines": 4, "engines": [
                {"id": 0, "status": "paused"},
                {"id": 1, "status": "healthy"},   # still pausing
                {"id": 2, "status": "dead"},
                {"id": 3, "status": "paused"},
            ]},
            {"total_engines": 4, "engines": [
                {"id": 0, "status": "paused"},
                {"id": 1, "status": "paused"},     # now paused
                {"id": 2, "status": "dead"},
                {"id": 3, "status": "paused"},
            ]},
        ]

        def fake_get(url, timeout=5):
            r = mock.Mock()
            r.status_code = 200
            r.json.return_value = responses.pop(0)
            return r

        with (
            mock.patch("requests.get", side_effect=fake_get) as get,
            mock.patch.object(sub, "pause", return_value=True),
        ):
            self.assertTrue(sub._wait_for_pause([2], poll_interval=1, max_wait=5))
        self.assertEqual(get.call_count, 2)

    def test_empty_engines_is_not_treated_as_paused(self):
        # Empty/missing engines means the pause report is not ready yet; the
        # old buggy code treated it as "all paused" and returned True at once.
        sub = self._make_subscriber()

        responses = [
            {"total_engines": 4, "engines": []},
            {"total_engines": 4, "engines": [
                {"id": 0, "status": "paused"},
                {"id": 1, "status": "paused"},
                {"id": 2, "status": "dead"},
                {"id": 3, "status": "paused"},
            ]},
        ]

        def fake_get(url, timeout=5):
            r = mock.Mock()
            r.status_code = 200
            r.json.return_value = responses.pop(0)
            return r

        with mock.patch("requests.get", side_effect=fake_get) as get:
            self.assertTrue(sub._wait_for_pause([2], poll_interval=1, max_wait=5))
        self.assertEqual(get.call_count, 2)

    def test_wait_for_pause_sends_pause_once_when_engines_idle_healthy(self):
        # vLLM is idle and never observed the fault -> EngineCores stay healthy.
        # The manager must drive a manual pause (once), excluding the faulted
        # rank; the faulted rank may stay healthy and must not block the check.
        sub = self._make_subscriber()

        responses = [
            {"total_engines": 4, "engines": [
                {"id": 0, "status": "healthy"},
                {"id": 1, "status": "healthy"},
                {"id": 2, "status": "healthy"},   # excluded faulted card
                {"id": 3, "status": "healthy"},
            ]},
            {"total_engines": 4, "engines": [
                {"id": 0, "status": "paused"},
                {"id": 1, "status": "paused"},
                {"id": 2, "status": "healthy"},   # excluded, stays healthy
                {"id": 3, "status": "paused"},
            ]},
        ]

        def fake_get(url, timeout=5):
            r = mock.Mock()
            r.status_code = 200
            r.json.return_value = responses.pop(0)
            return r

        with (
            mock.patch("requests.get", side_effect=fake_get),
            mock.patch.object(sub, "pause", return_value=True) as pause_mock,
        ):
            self.assertTrue(sub._wait_for_pause([2], poll_interval=1, max_wait=5))
        pause_mock.assert_called_once_with([2])

    def test_wait_for_pause_no_pause_when_already_non_healthy(self):
        # When vLLM already observed the fault and auto-paused, no manual pause
        # is sent (the scale_down path must not pause unconditionally).
        sub = self._make_subscriber()

        def fake_get(url, timeout=5):
            r = mock.Mock()
            r.status_code = 200
            r.json.return_value = {
                "total_engines": 4,
                "engines": [
                    {"id": 0, "status": "paused"},
                    {"id": 1, "status": "unhealthy"},
                    {"id": 2, "status": "dead"},     # excluded rank (failed)
                    {"id": 3, "status": "unhealthy"},
                ],
            }
            return r

        with (
            mock.patch("requests.get", side_effect=fake_get),
            mock.patch.object(sub, "pause", return_value=True) as pause_mock,
        ):
            self.assertTrue(sub._wait_for_pause([2], poll_interval=1, max_wait=5))
        pause_mock.assert_not_called()


class TestWebhookRoundTrip(unittest.TestCase):
    """End-to-end: a fake CATMonitor POSTs an event to the subscriber's
    webhook; the subscriber calls a fake vLLM. Verifies the full plumbing."""

    @classmethod
    def setUpClass(cls):
        # Fake vLLM: records /fault_tolerance/apply calls.
        cls.vllm_calls = []

        class _VLLMHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                # _wait_for_pause() polls /fault_tolerance/status; report all
                # DP ranks paused so the flow proceeds to scale_down.
                body = json.dumps(
                    {"total_engines": 4, "engines": [
                        {"id": 0, "status": "paused"},
                        {"id": 1, "status": "paused"},
                        {"id": 2, "status": "paused"},
                        {"id": 3, "status": "paused"},
                    ]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                TestWebhookRoundTrip.vllm_calls.append(json.loads(body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

        cls._vllm = ThreadingHTTPServer(("127.0.0.1", 0), _VLLMHandler)
        cls.vllm_port = cls._vllm.server_address[1]
        threading.Thread(target=cls._vllm.serve_forever, daemon=True).start()

        # Fake CATMonitor REST: just acknowledge subscription creation so the
        # subscriber can register; we then POST the event ourselves.
        class _CatmonHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b'{"id":"sub-test-1"}')

            def do_DELETE(self):
                self.send_response(204)
                self.end_headers()

        cls._catmon = ThreadingHTTPServer(("127.0.0.1", 0), _CatmonHandler)
        cls.catmon_port = cls._catmon.server_address[1]
        threading.Thread(target=cls._catmon.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls._vllm.shutdown()
        cls._catmon.shutdown()

    def test_event_webhook_to_vllm(self):
        cfg = FaultSubscriberConfig(
            vllm_host="127.0.0.1",
            vllm_port=self.vllm_port,
            catmonitor_host="127.0.0.1",
            catmonitor_rest_port=self.catmon_port,
            callback_host="127.0.0.1",
            callback_port=0,
            advertise_url="http://127.0.0.1:1/fault_event",  # port set after start
            npu_ids=[0, 1, 2, 3],
        )
        sub = CatMonitorFaultSubscriber(cfg, build_npu_to_dp_ranks([0, 1, 2, 3], 1, [0, 1, 2, 3], 4))
        sub.start(block=False)
        # Give the HTTP server + registration a moment.
        time.sleep(0.3)
        try:
            cb_port = sub._server.server_address[1]
            # POST a fault event as CATMonitor would.
            import requests

            requests.post(
                f"http://127.0.0.1:{cb_port}/fault_event",
                json={"type": "card_drop", "npu_id": "2", "recovered": False},
                timeout=5,
            )
            time.sleep(0.3)
            # vLLM should have received scale_down after pause completes.
            instructions = [c["instruction"] for c in self.vllm_calls]
            self.assertEqual(instructions, ["scale_down"])
            # The scale_down payload should exclude DP rank 2 (NPU 2 -> rank 2).
            scale_call = next(c for c in self.vllm_calls if c["instruction"] == "scale_down")
            self.assertEqual(scale_call["params"]["exclude_dp_ranks"], [2])
        finally:
            sub.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
