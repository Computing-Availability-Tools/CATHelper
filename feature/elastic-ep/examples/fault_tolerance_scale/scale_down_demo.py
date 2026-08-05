#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""External fault manager for vLLM Elastic-EP fault tolerance.

This is the integration entry point. It runs two fault-detection paths:

  Path 1 (replaces the old DCMI polling):
      A ``CatMonitorFaultSubscriber`` subscribes to CATMonitor's fault
      subscription API. CATMonitor collects NPU health/error codes/ECC/roce
      link state and pushes ``FaultEvent`` JSON via HTTP webhook to this
      process, which maps the faulted DIE to its DP ranks and issues
      ``scale_down`` (or ``retry`` on recovery) to vLLM.

  Path 2 (unchanged, EEP-internal boundary):
      ``start_monitor_engine_status`` subscribes via ZMQ SUB to vLLM's engine
      health broadcast (port ``--fault-port``) and issues ``scale_down`` when
      an engine is reported dead. This is the engine-level reporting from the
      vLLM fault-tolerance patch and is independent of CATMonitor.

Usage: see README.md §使用. The DCMI library is no longer required.
"""

import argparse
import json
import os
import sys
import threading
import time
from contextlib import suppress
from typing import List

import requests
import zmq
import msgspec

from catmonitor_fault_sub import (
    ALL_FAULT_TYPES,
    CatMonitorFaultSubscriber,
    FaultSubscriberConfig,
    build_npu_to_dp_ranks,
    parse_fault_types,
    parse_npu_ids,
)

Per_device_card = 2

# ---- Path 2: engine health via ZMQ SUB (unchanged from the original demo) ----

_fault_event_context = None
_fault_event_socket = None
_fault_event_endpoint = None


def listen_fault_event(host, port):
    global _fault_event_context, _fault_event_socket, _fault_event_endpoint
    if _fault_event_context is None:
        _fault_event_context = zmq.Context()
    if _fault_event_socket is None:
        _fault_event_socket = _fault_event_context.socket(zmq.SUB)
        _fault_event_socket.setsockopt_string(zmq.SUBSCRIBE, "vllm_fault")
    endpoint = f"tcp://{host}:{port}"
    if _fault_event_endpoint != endpoint:
        if _fault_event_endpoint is not None:
            with suppress(zmq.ZMQError):
                _fault_event_socket.disconnect(_fault_event_endpoint)
        _fault_event_socket.connect(endpoint)
        _fault_event_endpoint = endpoint
    frames = _fault_event_socket.recv_multipart()
    decoder = msgspec.msgpack.Decoder()
    msg = decoder.decode(frames[-1])
    engines = msg.get("engines", [])
    dead_engine = [int(e["id"]) for e in engines if e.get("status") == "dead"]
    return dead_engine


def scale(host, port, timeout, exclude_dp_ranks):
    url = f"http://{host}:{port}/fault_tolerance/apply"
    payload = {
        "instruction": "scale_down",
        "params": {"timeout": timeout, "exclude_dp_ranks": exclude_dp_ranks},
    }
    headers = {"Content-Type": "application/json"}
    print(f"Sending scale request to {url}")
    print(f"Payload: {json.dumps(payload, indent=2)}")
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=300)
        print(f"Status Code: {response.status_code}")
        print(f"Response: {response.text}")
        return response.status_code == 200
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
        return False


def wait_for_pause(host, port, exclude_dp_ranks, poll_interval=2, max_wait=120):
    url = f"http://{host}:{port}/fault_tolerance/status"
    waited = 0
    print(f"Waiting for pause to complete on all ranks (excluding {exclude_dp_ranks})...")
    while waited < max_wait:
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200:
                time.sleep(poll_interval)
                waited += poll_interval
                continue
            status_data = resp.json()
            # /fault_tolerance/status returns {"total_engines": ..., "engines": [{"id": ..., "status": ...}]}
            engines = status_data.get("engines", [])
            if not engines:
                print(f"WARNING: no engines in status response: {status_data}")
                time.sleep(poll_interval)
                waited += poll_interval
                continue
            all_paused = True
            for rank in engines:
                rank_id = rank.get("id", -1)
                rank_status = rank.get("status", "")
                if rank_id in exclude_dp_ranks:
                    continue
                # vLLM's scale_down rejects only retained engines that are
                # still "healthy"; paused/dead/unhealthy all satisfy it.
                if rank_status not in ("paused", "dead", "unhealthy"):
                    all_paused = False
                    break
            if all_paused:
                print("All ranks paused.")
                return True
        except requests.RequestException:
            pass
        time.sleep(poll_interval)
        waited += poll_interval
    print(f"WARNING: pause did not complete within {max_wait}s")
    return False


def start_monitor_engine_status(host, port, timeout, external_fault_notify_port):
    scaled_down_ranks: set[int] = set()
    while True:
        exclude_dp_ranks = listen_fault_event(host, external_fault_notify_port)
        new_ranks = [r for r in exclude_dp_ranks if r not in scaled_down_ranks]
        if not new_ranks:
            continue
        print(f"Engine health event: dead ranks {new_ranks}")
        if wait_for_pause(host, port, new_ranks):
            if scale(host, port, timeout, new_ranks):
                scaled_down_ranks.update(new_ranks)


def parse_error_codes(raw: str) -> List[str]:
    """Parse a comma-separated DCMI error-code list into lowercase hex codes."""
    return [c.strip().lower() for c in raw.split(",") if c.strip()]


def default_visible_devices() -> List[int]:
    """Visible physical NPU card IDs in DP-rank order, taken from
    ``ASCEND_RT_VISIBLE_DEVICES`` when set (fall back to ``0-15`` on a
    16-card A3)."""
    raw = os.environ.get("ASCEND_RT_VISIBLE_DEVICES")
    if raw:
        try:
            parsed = parse_npu_ids(raw)
            if parsed:
                return parsed
        except ValueError:
            pass
    return list(range(16))


def main():
    parser = argparse.ArgumentParser(
        description="External fault manager for vLLM Elastic-EP fault tolerance "
        "(subscribes to CATMonitor fault events + vLLM engine health)"
    )
    parser.add_argument("--host", default="localhost", help="vLLM API server host")
    parser.add_argument("--port", type=int, default=8006, help="vLLM API server port")
    parser.add_argument(
        "--recovery-timeout",
        type=int,
        default=120,
        help="Fault recovery timeout (seconds) for pause/scale_down/retry",
    )
    parser.add_argument(
        "--external-fault-notify-port",
        type=int,
        default=22867,
        help="vLLM engine-health ZMQ SUB port (must match vLLM --fault-port)",
    )
    parser.add_argument(
        "--npu-ids",
        type=parse_npu_ids,
        default=None,
        help="CATMonitor/DIE device IDs to subscribe for fault events "
             "(A3: 0-7 for 8 DIE). Default: derived from --visible-devices",
    )
    parser.add_argument(
        "--npu-per-die",
        type=int,
        default=2,
        help="Physical NPU cards per DIE (A3: 2; DIE d hosts physical cards "
             "d*npu-per-die .. d*npu-per-die+npu-per-die-1)",
    )
    parser.add_argument(
        "--visible-devices",
        type=parse_npu_ids,
        default=default_visible_devices(),
        help="Physical NPU card IDs visible to vLLM "
             "(ASCEND_RT_VISIBLE_DEVICES, used when set), in DP-rank order "
             "(default: 0-15 on a 16-card A3)",
    )
    parser.add_argument(
        "--dp-size",
        type=int,
        default=None,
        help="vLLM data-parallel size (number of DP ranks; must be <= "
             "len(visible-devices)). Default: len(visible-devices)",
    )
    # CATMonitor subscription (Path 1) arguments.
    parser.add_argument(
        "--catmonitor-host", default="localhost", help="CATMonitor daemon host"
    )
    parser.add_argument(
        "--catmonitor-rest-port",
        type=int,
        default=9101,
        help="CATMonitor fault-subscription REST API port",
    )
    parser.add_argument(
        "--callback-host",
        default="0.0.0.0",
        help="Local webhook listen address (bind a reachable NIC for cross-host)",
    )
    parser.add_argument(
        "--callback-port",
        type=int,
        default=9102,
        help="Local webhook listen port for CATMonitor fault events",
    )
    parser.add_argument(
        "--advertise-url",
        default="http://localhost:9102/fault_event",
        help="Callback URL registered with CATMonitor (cross-host: http://<reachable-ip>:9102/fault_event)",
    )
    parser.add_argument(
        "--fault-types",
        type=parse_fault_types,
        default=["card_drop", "npu_error_code", "hbm_uce", "roce_link_down"],
        help="Comma-separated CATMonitor fault types to subscribe to",
    )
    parser.add_argument(
        "--ignore-error-codes",
        type=parse_error_codes,
        default=["0x80f38003"],
        help="Comma-separated DCMI error codes to ignore; npu_error_code events "
             "whose codes are ALL in this list are not scaled down "
             "(default: 0x80f38003, a benign bus error after business abort)",
    )
    parser.add_argument(
        "--debounce-ms", type=int, default=0, help="Per-subscription debounce (ms)"
    )
    parser.add_argument(
        "--min-severity",
        default="warning",
        choices=["warning", "critical"],
        help="Minimum severity to receive",
    )
    args = parser.parse_args()

    dp_size = args.dp_size if args.dp_size is not None else len(args.visible_devices)
    if dp_size < 1 or dp_size > len(args.visible_devices):
        parser.error(
            f"--dp-size {dp_size} out of range "
            f"(visible devices: {len(args.visible_devices)})"
        )

    # CATMonitor reports faults per DIE (npu_id = DIE id, e.g. 0-7 on A3);
    # vLLM ranks are per physical card. Map each DIE to the DP ranks of the
    # physical cards it hosts so scale_down excludes the right ranks.
    npu_ids = args.npu_ids
    if npu_ids is None:
        npu_ids = sorted(
            {p // args.npu_per_die for p in args.visible_devices[:dp_size]}
        )
    npu_to_dp = build_npu_to_dp_ranks(
        npu_ids, args.npu_per_die, args.visible_devices, dp_size
    )
    print(
        f"NPU->DP mapping: {npu_to_dp} "
        f"({len(npu_ids)} DIE subscribed, dp-size={dp_size}, "
        f"visible-devices={args.visible_devices}, "
        f"fault types={args.fault_types}, "
        f"ignore-error-codes={args.ignore_error_codes})"
    )

    # Path 1: CATMonitor fault-event subscription.
    cfg = FaultSubscriberConfig(
        vllm_host=args.host,
        vllm_port=args.port,
        catmonitor_host=args.catmonitor_host,
        catmonitor_rest_port=args.catmonitor_rest_port,
        callback_host=args.callback_host,
        callback_port=args.callback_port,
        advertise_url=args.advertise_url,
        fault_types=args.fault_types,
        npu_ids=npu_ids,
        debounce_ms=args.debounce_ms,
        min_severity=args.min_severity,
        recovery_timeout=args.recovery_timeout,
        ignore_error_codes=args.ignore_error_codes,
    )
    subscriber = CatMonitorFaultSubscriber(
        cfg,
        npu_to_dp,
        visible_devices=list(args.visible_devices),
        dp_size=dp_size,
        npu_per_die=args.npu_per_die,
        npu_ids=list(npu_ids),
    )
    subscriber.start(block=False)

    # Path 2: vLLM engine-health ZMQ SUB (unchanged EEP-internal boundary).
    monitor_thread = threading.Thread(
        target=start_monitor_engine_status,
        daemon=True,
        args=(
            args.host,
            args.port,
            args.recovery_timeout,
            args.external_fault_notify_port,
        ),
        name="EngineHealthThread",
    )
    monitor_thread.start()

    print("Fault manager running. Ctrl-C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nShutting down fault manager...")
        subscriber.stop()


if __name__ == "__main__":
    main()
