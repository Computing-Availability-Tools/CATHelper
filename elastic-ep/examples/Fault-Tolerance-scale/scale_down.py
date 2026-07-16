#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import argparse
import ctypes
import json
import os
import sys
import threading
import time
from contextlib import suppress
from ctypes import POINTER, byref, c_int, c_uint, cdll

import requests
import zmq

DCMI_LIB_PATH = os.environ.get("DCMI_LIB_PATH", "/usr/local/dcmi/libdcmi.so")
try:
    lib = cdll.LoadLibrary(DCMI_LIB_PATH)
except OSError as exc:
    sys.stderr.write(
        "Error: Failed to load DCMI library. Ensure the DCMI library is installed\n"
        f"and that DCMI_LIB_PATH is set correctly (current value: {DCMI_LIB_PATH}).\n"
        f"Original error: {exc}\n"
    )
    raise SystemExit(1)

DeviceNotReadyErrCode = -8012
CardDropFaultCode = "0x40f84e00"
Per_device_card = 2
lib.dcmi_init.restype = ctypes.c_int

lib.dcmi_get_card_list.argtypes = [POINTER(c_int), POINTER(c_int), c_int]
lib.dcmi_get_card_list.restype = ctypes.c_int

lib.dcmi_get_device_num_in_card.argtypes = [
    c_int,
    POINTER(c_int),
]
lib.dcmi_get_device_num_in_card.restype = ctypes.c_int
lib.dcmi_get_device_errorcode_v2.argtypes = [
    ctypes.c_int,
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_uint),
    ctypes.c_int,
]

lib.dcmi_get_device_errorcode_v2.restype = ctypes.c_int

lib.dcmi_get_device_health.argtypes = [
    c_int,
    c_int,
    POINTER(c_uint),
]
lib.dcmi_get_device_health.restype = c_int

ALL_NPUS = []
active_npus = []
active_npus_lock = threading.Lock()


def get_npu_by_dp(dp_rank):
    return active_npus[dp_rank]


def get_dp_by_npu(npu_id):
    with active_npus_lock:
        try:
            return active_npus.index(npu_id)
        except ValueError:
            return -1


def is_valid_card_id(card_id):
    return 0 <= card_id < 2147483647


def is_valid_device_id(device_id):
    return 0 <= device_id < 4


def is_valid_card_id_and_device_id(card_id, device_id):
    return is_valid_card_id(card_id) and is_valid_device_id(device_id)


def get_device_list():
    card_num = c_int(0)
    card_list_len = 16
    card_list = (ctypes.c_int * card_list_len)()
    card_list_ret_code = lib.dcmi_get_card_list(
        byref(card_num),
        card_list,
        c_int(card_list_len),
    )
    if card_list_ret_code != 0:
        raise Exception(
            f"ERROR: Failed to get card list,error_code is {card_list_ret_code}"
        )

    if not all(x > 0 for x in card_list):
        card_list = [i for i in range(card_num.value)]
    device_num = c_int(0)
    n = 0
    while device_num.value == 0 and n < card_num.value:
        lib.dcmi_get_device_num_in_card(
            c_int(n),
            byref(device_num),
        )
        n += 1

    device_list = []
    for i in range(card_num.value):
        for j in range(device_num.value):
            device_list.append([card_list[i], j])
    return device_list


def get_device_all_error_code(card_id, device_id):
    if not is_valid_card_id_and_device_id(card_id, device_id):
        print(f"ERROR: invalid card_id and device_id: {card_id}, {device_id}")
        return -1, -1

    list_len = 128
    error_count = c_int(0)
    error_code_list = (ctypes.c_uint * list_len)()

    ret_code = lib.dcmi_get_device_errorcode_v2(
        c_int(card_id),
        c_int(device_id),
        byref(error_count),
        error_code_list,
        c_int(list_len),
    )

    health = c_uint(0)
    health_ret_code = lib.dcmi_get_device_health(
        c_int(card_id),
        c_int(device_id),
        byref(health),
    )

    if ret_code != 0 and health_ret_code != DeviceNotReadyErrCode:
        return -1, -1

    error_codes = [code for code in error_code_list if code != 0]
    error_codes_hex = [hex(code) for code in error_codes]

    if health_ret_code == DeviceNotReadyErrCode:
        error_count.value += 1
        error_codes_hex.append(CardDropFaultCode)

    if error_count.value < 0 or error_count.value > 128:
        print("ERROR: invalid error count")
        return -1, -1

    return error_codes_hex, health


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
    message = _fault_event_socket.recv_string()
    json_part = message.split("|")[1]
    status_dict = json.loads(json_part)
    dead_engine = [int(idx) for idx, status in status_dict.items() if status == "Dead"]
    return dead_engine


def pause(host, port, timeout, exclude_dp_rank=None):
    url = f"http://{host}:{port}/fault_tolerance/apply"
    payload = {
        "fault_tolerance_instruction": "pause",
        "fault_tolerance_timeout": timeout,
        "fault_tolerance_params": {
            "soft_pause": False,
            "exclude_index": exclude_dp_rank,
        },
    }

    headers = {"Content-Type": "application/json"}

    print(f"Sending pause request to {url}")
    print(f"Payload: {json.dumps(payload, indent=2)}")

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=300)

        print(f"Status Code: {response.status_code}")
        print(f"Response: {response.text}")

        if response.status_code == 200:
            print("Pause request successful!")
            return True
        else:
            print("Pause request failed!")
            return False

    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
        return False


def scale(host, port, timeout, exclude_dp_ranks):
    url = f"http://{host}:{port}/fault_tolerance/apply"
    payload = {
        "fault_tolerance_instruction": "scale_down",
        "fault_tolerance_timeout": timeout,
        "fault_tolerance_params": {"exclude_dp_ranks": exclude_dp_ranks},
    }
    headers = {"Content-Type": "application/json"}

    print(f"Sending scale request to {url}")
    print(f"Payload: {json.dumps(payload, indent=2)}")

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=300)

        print(f"Status Code: {response.status_code}")
        print(f"Response: {response.text}")

        if response.status_code == 200:
            print("Scale down request successful!")
            return True
        else:
            print("Scale down request failed!")
            return False

    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
        return False


def start_monitor_engine_status(host, port, timeout, external_fault_notify_port):
    global active_npus
    while True:
        exclude_dp_ranks = listen_fault_event(host, external_fault_notify_port)
        scale(host, port, timeout, exclude_dp_ranks)
        with active_npus_lock:
            npus_to_remove = {get_npu_by_dp(dp_rank) for dp_rank in exclude_dp_ranks}
            active_npus = [npu for npu in active_npus if npu not in npus_to_remove]


def monitor_machine_fault(host, port, recover_timeout, interval_time):
    lib.dcmi_init()
    device_list = get_device_list()
    print(device_list)
    global ALL_NPUS, active_npus

    while True:
        exclude_dp_ranks = set()
        failed_npus = set()
        for device in device_list:
            with active_npus_lock:
                current_active_npus = set(active_npus)
            result = get_device_all_error_code(device[0], device[1])
            if (
                not result
                or not isinstance(result, tuple)
                or len(result) < 1
                or not isinstance(result[0], (list, tuple, set))
            ):
                continue
            if CardDropFaultCode in result[0]:
                error_npu = [
                    Per_device_card * device[0] + card_id
                    for card_id in range(Per_device_card)
                ]
                failed_npus.update(
                    [npu for npu in error_npu if npu in current_active_npus]
                )
                print(f"device id: {device[0]} card_id: {device[1]} CardDropFault")

        exclude_dp_ranks.update([get_dp_by_npu(npu) for npu in failed_npus])
        exclude_dp_ranks.discard(-1)

        if exclude_dp_ranks:
            pause(host, port, recover_timeout, list(exclude_dp_ranks))
            scale(host, port, recover_timeout, list(exclude_dp_ranks))
            with active_npus_lock:
                active_npus = [x for x in active_npus if x not in failed_npus]
        time.sleep(interval_time)


def main():
    parser = argparse.ArgumentParser(
        description="Test scale down functionality"
    )
    parser.add_argument("--host", default="localhost", help="API server host")
    parser.add_argument("--port", type=int, default=8006, help="API server port")
    parser.add_argument(
        "--recovery-timeout",
        type=int,
        default=30,
        help="Fault recovery timeout",
    )
    parser.add_argument(
        "--interval-time",
        type=int,
        default=3,
        help="Fault code polling time",
    )
    parser.add_argument(
        "--external-fault-notify-port",
        type=int,
        default=22867,
        help="The port to use for external fault notify",
    )
    parser.add_argument(
        "--npu-ids",
        type=lambda s: [int(item) for item in s.split(",")],
        default=list(range(16)),
        help="The card number used to launch vllm",
    )

    args = parser.parse_args()
    global ALL_NPUS, active_npus
    ALL_NPUS = args.npu_ids
    active_npus = ALL_NPUS.copy()

    monitor_thread = threading.Thread(
        target=start_monitor_engine_status,
        daemon=True,
        args=(
            args.host,
            args.port,
            args.recovery_timeout,
            args.external_fault_notify_port,
        ),
        name="MonitorThread",
    )
    monitor_thread.start()
    monitor_machine_fault(
        args.host, args.port, args.recovery_timeout, args.interval_time
    )


if __name__ == "__main__":
    main()
