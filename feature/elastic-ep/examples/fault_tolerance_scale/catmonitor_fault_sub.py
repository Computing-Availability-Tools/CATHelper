#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the CATHelper project
"""CATMonitor fault-event subscriber for the Elastic-EP fault manager.

This module replaces the old DCMI-polling path in ``scale_down_demo.py``.
Instead of calling ``libdcmi.so`` directly, it subscribes to CATMonitor's
fault-subscription API:

  1. On start it POSTs a subscription to CATMonitor REST ``/faultsub/
     subscriptions``, declaring which fault types / NPUs to receive and the
     callback URL CATMonitor should POST events back to.
  2. It runs a lightweight ``http.server.ThreadingHTTPServer`` that receives
     ``POST /fault_event`` (JSON ``FaultEvent``) from CATMonitor.
   3. On each event it maps the NPU id to a DP rank (deployment topology is
      local knowledge) and issues ``scale_down`` (or ``retry`` for recovery) to
      vLLM's ``/fault_tolerance/apply`` REST API.
  4. On stop it DELETEs its subscription so CATMonitor stops pushing.

Only the Python standard library + ``requests`` are used (no DCMI / ZMQ
dependency for this path). The vLLM engine-health ZMQ SUB path stays in
``scale_down_demo.py`` unchanged.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

import requests

# Fault types emitted by CATMonitor's FaultDetector (see features/faultsub/
# event.go). The subscriber can ask for a subset via --fault-types.
ALL_FAULT_TYPES = [
    "card_drop",
    "npu_health",
    "npu_error_code",
    "hbm_uce",
    "ddr_uce",
    "roce_link_down",
    "driver_unhealthy",
]

# Fault types that warrant an irreversible scale_down (hardware gone).
SCALE_DOWN_TYPES = {"card_drop", "npu_health", "npu_error_code", "hbm_uce", "ddr_uce"}


def parse_fault_types(raw: str) -> List[str]:
    """Parse a comma-separated fault-type list, validating against the set
    CATMonitor can emit. Exits (SystemExit) on an unknown type."""
    types = [t.strip() for t in raw.split(",") if t.strip()]
    unknown = [t for t in types if t not in ALL_FAULT_TYPES]
    if unknown:
        sys.stderr.write(
            f"Error: unknown fault type(s): {unknown}. "
            f"Available: {','.join(ALL_FAULT_TYPES)}\n"
        )
        raise SystemExit(2)
    return types


def parse_npu_ids(raw: str) -> List[int]:
    """Parse ``0-3`` (range) or ``0,1,5`` (list) into an int list."""
    if "-" in raw and "," not in raw:
        lo, hi = raw.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(x) for x in raw.split(",") if x.strip() != ""]


@dataclass
class FaultSubscriberConfig:
    vllm_host: str = "localhost"
    vllm_port: int = 8006
    catmonitor_host: str = "localhost"
    catmonitor_rest_port: int = 9101
    callback_host: str = "0.0.0.0"
    callback_port: int = 9102
    advertise_url: str = "http://localhost:9102/fault_event"
    fault_types: List[str] = field(
        default_factory=lambda: [
            "card_drop",
            "npu_error_code",
            "hbm_uce",
            "roce_link_down",
        ]
    )
    npu_ids: List[int] = field(default_factory=lambda: list(range(16)))
    debounce_ms: int = 0
    min_severity: str = "warning"
    recovery_timeout: int = 120
    # DCMI error codes considered benign (default: bus error 0x80f38003, which
    # appears after a business abort and does not affect vLLM). An
    # npu_error_code event is ignored only when ALL its error codes are in this
    # list; any other error code still triggers scale_down.
    ignore_error_codes: List[str] = field(default_factory=lambda: ["0x80f38003"])


class CatMonitorFaultSubscriber:
    """Subscribes to CATMonitor fault events and drives vLLM fault tolerance.

    NPU (DIE) id -> DP rank list mapping is supplied by the caller (deployment
    topology is local knowledge; CATMonitor does not know about vLLM DP ranks).

    When the deployment topology (``visible_devices`` / ``dp_size`` /
    ``npu_per_die`` / ``npu_ids``) is provided, the mapping is DYNAMIC: after
    each successful ``scale_down`` the faulted DIE's physical cards are dropped
    from the deployment and the map is rebuilt. vLLM renumbers the surviving
    engines to contiguous ranks 0..N-1 in their original order, so the
    surviving deployed cards (in order) become the new rank order. Dies whose
    cards are all removed leave the map and later events for them are skipped.
    Without the topology arguments the map is static (caller-owned) and no
    rebuild happens.
    """

    def __init__(
        self,
        cfg: FaultSubscriberConfig,
        npu_to_dp: Dict[int, List[int]],
        *,
        visible_devices: Optional[List[int]] = None,
        dp_size: Optional[int] = None,
        npu_per_die: Optional[int] = None,
        npu_ids: Optional[List[int]] = None,
    ):
        self.cfg = cfg
        self.npu_to_dp = dict(npu_to_dp)
        self._dynamic = all(
            x is not None for x in (visible_devices, dp_size, npu_per_die)
        )
        # Deployed physical cards in current DP-rank order (option A state).
        self._deployed: Optional[List[int]] = (
            list(visible_devices[:dp_size]) if self._dynamic else None
        )
        self._npu_per_die = npu_per_die if self._dynamic else None
        self._npu_ids = sorted(npu_ids) if npu_ids is not None else sorted(npu_to_dp)
        self.dp_size = len(self._deployed) if self._dynamic else dp_size
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._subscription_id: Optional[str] = None
        self._active_faults: Dict[str, str] = {}  # npu_id -> fault type (dedup)
        self._lock = threading.Lock()
        # npu_ids currently mid scale_down. ThreadingHTTPServer handles each
        # event in its own thread and the dedup above only matches the SAME
        # fault type, so two different fault types for one DIE arriving close
        # together used to start a second, duplicate scale_down for the same
        # (possibly already removed) ranks. This set serializes per NPU.
        self._handling: set = set()

    # ---- vLLM control ----

    def _vllm_apply(self, instruction: str, params: dict, timeout: int = 300) -> bool:
        url = f"http://{self.cfg.vllm_host}:{self.cfg.vllm_port}/fault_tolerance/apply"
        payload = {"instruction": instruction, "params": params}
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            print(
                f"[faultsub] {instruction} -> {resp.status_code}: {resp.text[:200]}"
            )
            return resp.status_code == 200
        except requests.RequestException as exc:
            print(f"[faultsub] {instruction} request failed: {exc}")
            return False

    def scale_down(self, exclude_dp_ranks: List[int]) -> bool:
        return self._vllm_apply(
            "scale_down",
            {"timeout": self.cfg.recovery_timeout, "exclude_dp_ranks": exclude_dp_ranks},
        )

    def pause(self, exclude_dp_ranks: Optional[List[int]] = None) -> bool:
        """Manually pause all healthy engines.

        vLLM only auto-pauses when it observes the fault itself; when it is
        idle (no in-flight requests) the EngineCores stay ``healthy`` and the
        external manager must drive the pause. The faulted ranks are excluded
        via ``exclude_engine_index`` so we never block on them.
        """
        params: dict = {"timeout": self.cfg.recovery_timeout}
        if exclude_dp_ranks:
            params["exclude_engine_index"] = exclude_dp_ranks
        return self._vllm_apply("pause", params)

    def retry(self, dp_ranks: Optional[List[int]] = None) -> bool:
        params: dict = {"timeout": self.cfg.recovery_timeout}
        if dp_ranks:
            params["exclude_dp_ranks"] = dp_ranks
        return self._vllm_apply("retry", params)

    def _rebuild_npu_to_dp(self) -> Dict[int, List[int]]:
        """Recompute the NPU->DP map from the current deployed card list.

        vLLM renumbers the surviving engines to contiguous ranks 0..N-1 in
        their original order after a successful scale_down, so the surviving
        deployed cards (in order) become the new rank order.
        """
        ranks_by_die: Dict[int, List[int]] = {}
        for rank, phys in enumerate(self._deployed):
            ranks_by_die.setdefault(phys // self._npu_per_die, []).append(rank)
        self.dp_size = len(self._deployed)
        wanted = set(self._npu_ids)
        return {
            d: sorted(ranks)
            for d, ranks in ranks_by_die.items()
            if d in wanted
        }

    def _on_scale_down_success(self, npu_id: str) -> None:
        """Drop the scaled-down DIE's physical cards from the deployment and
        rebuild the NPU->DP map after a successful vLLM scale_down.

        Dies whose cards are all removed leave the map; later CATMonitor events
        for them map to no ranks and are skipped (no more scale_down / retry).
        """
        if not self._dynamic:
            return
        with self._lock:
            die = int(npu_id)
            removed = [p for p in self._deployed if p // self._npu_per_die == die]
            self._deployed = [
                p for p in self._deployed if p // self._npu_per_die != die
            ]
            self.npu_to_dp = self._rebuild_npu_to_dp()
            self._active_faults.pop(npu_id, None)
            print(
                f"[faultsub] NPU {npu_id} scaled down: removed cards {removed}, "
                f"remaining deployed={self._deployed}, dp_size={self.dp_size}, "
                f"new map {self.npu_to_dp}"
            )

    def _wait_for_pause(self, exclude_dp_ranks: List[int], poll_interval: int = 2, max_wait: int = 120) -> bool:
        """Poll vLLM status until all retained (non-excluded) ranks are
        paused/unhealthy/dead.

        vLLM's ``/fault_tolerance/status`` returns ``{"total_engines": ...,
        "engines": [{"id": ..., "status": ...}]}`` where ``status`` is one of
        ``healthy``/``dead``/``unhealthy``/``paused`` (EngineStatusType name,
        lower-cased). vLLM's scale_down validation only rejects when a RETAINED
        engine is still ``healthy``, so ``paused``, ``dead`` and ``unhealthy``
        all satisfy the precondition. Excluded (failed) ranks are being removed
        and their state is irrelevant, and they are skipped in the check.

        vLLM only auto-pauses when it observes the fault itself; when it is
        idle the EngineCores stay ``healthy``. In that case we drive the pause
        manually, once, on the retained engines only (the faulted ranks are
        excluded via ``exclude_engine_index`` so we never block on a device
        that may have failed).
        """
        url = f"http://{self.cfg.vllm_host}:{self.cfg.vllm_port}/fault_tolerance/status"
        print(f"[faultsub] waiting for retained ranks to pause... excluded_ranks={exclude_dp_ranks}")
        waited = 0
        pause_sent = False
        while waited < max_wait:
            try:
                resp = requests.get(url, timeout=5)
                if resp.status_code != 200:
                    time.sleep(poll_interval)
                    waited += poll_interval
                    continue
                status_data = resp.json()
                engines = status_data.get("engines", [])
                if not engines:
                    print(
                        f"[faultsub] WARNING: no engines in status response: "
                        f"{status_data}"
                    )
                    time.sleep(poll_interval)
                    waited += poll_interval
                    continue
                all_paused = True
                retained_healthy = False
                for rank in engines:
                    rank_id = rank.get("id", -1)
                    rank_status = rank.get("status", "")
                    if rank_id in exclude_dp_ranks:
                        # Faulted ranks reported by CATMonitor are treated as
                        # unhealthy regardless of their EngineCore state: they
                        # are being removed, and may stay "healthy" when vLLM
                        # is idle (no error ever propagated to the EngineCore).
                        continue
                    if rank_status not in ("paused", "dead", "unhealthy"):
                        all_paused = False
                    if rank_status == "healthy":
                        retained_healthy = True
                if all_paused:
                    return True
                if retained_healthy and not pause_sent:
                    self.pause(exclude_dp_ranks)
                    pause_sent = True
            except requests.RequestException:
                pass
            time.sleep(poll_interval)
            waited += poll_interval
        print(f"[faultsub] WARNING: pause did not complete within {max_wait}s for retained ranks (excluded={exclude_dp_ranks})")
        return False

    # ---- event handling ----

    def _error_codes_from_event(self, event: dict) -> List[str]:
        """Extract the DCMI error codes from an event's detail, normalized to
        lowercase hex (e.g. "0X80F38003" -> "0x80f38003")."""
        detail = event.get("detail") or {}
        raw = detail.get("error_codes", "")
        return [c.strip().lower() for c in raw.split(",") if c.strip()]

    def _is_benign_error_codes(self, event: dict) -> bool:
        """True when every error code in the event is on the ignore list. An
        event with no parseable codes is NOT benign (fail closed)."""
        codes = self._error_codes_from_event(event)
        if not codes:
            return False
        ignored = {c.lower() for c in self.cfg.ignore_error_codes}
        return all(c in ignored for c in codes)

    def _handle_event(self, event: dict) -> None:
        ev_type = event.get("type", "")
        npu_id = str(event.get("npu_id", ""))
        recovered = bool(event.get("recovered", False))
        dp_ranks = self.npu_to_dp.get(int(npu_id), [])
        print(
            f"[faultsub] event type={ev_type} npu={npu_id} dp={dp_ranks} "
            f"recovered={recovered} detail={event.get('detail')}"
        )
        if not dp_ranks:
            print(f"[faultsub] NPU {npu_id} not in dp map, skip")
            return

        if recovered:
            # A recovery that arrives while this DIE is still being scaled down
            # must be ignored: the engine it refers to is being (or already has
            # been) removed. Popping the fault here would let a stale retry fire
            # at ranks that are being removed (vLLM then times out on the
            # non-existent engine and the whole scale_down stalls until retry
            # itself fails). scale_down completion clears _active_faults itself.
            with self._lock:
                if npu_id in self._handling:
                    print(
                        f"[faultsub] recovery {ev_type} for NPU {npu_id} while "
                        f"scale_down in progress, skip"
                    )
                    return
            # Only retry faults we actually acted on (i.e. tracked as active).
            # Faults that were ignored (benign npu_error_code) were never
            # paused, so their recovery must not send retry to healthy engines.
            with self._lock:
                tracked = self._active_faults.pop(npu_id, None)
            if tracked is None:
                print(
                    f"[faultsub] recovery {ev_type} for NPU {npu_id} not tracked, skip retry"
                )
                return
            self.retry(dp_ranks)
            return

        # npu_error_code whose codes are all benign (e.g. 0x80f38003) is
        # ignored and not tracked, so a later genuine error code on the same
        # NPU is still acted on.
        if ev_type == "npu_error_code" and self._is_benign_error_codes(event):
            print(
                f"[faultsub] npu_error_code on NPU {npu_id} ignored "
                f"(all codes benign: {self._error_codes_from_event(event)})"
            )
            return

        # Dedup: a persistent fault may be re-sent by CATMonitor on restart;
        # only act when newly seen.
        with self._lock:
            prev = self._active_faults.get(npu_id)
            if prev == ev_type:
                print(f"[faultsub] duplicate {ev_type} for NPU {npu_id}, skip")
                return
            self._active_faults[npu_id] = ev_type

        if ev_type in SCALE_DOWN_TYPES:
            # Guard: only one scale_down per NPU at a time. Different fault
            # types for the same DIE (e.g. non-benign npu_error_code followed
            # by card_drop) can arrive while the first _wait_for_pause is still
            # blocking; without this guard each would issue its own scale_down,
            # and the stale duplicate would target ranks already removed by the
            # first successful scale_down (vLLM then times out on non-existent
            # engines).
            with self._lock:
                if npu_id in self._handling:
                    print(
                        f"[faultsub] scale_down already in progress for NPU "
                        f"{npu_id}, skip duplicate event {ev_type}"
                    )
                    return
                self._handling.add(npu_id)
            try:
                # No unconditional pause here: vLLM auto-pauses when it observes
                # the fault itself. Only when EngineCores stay "healthy" (idle
                # vLLM that never saw the fault) does _wait_for_pause drive a
                # manual pause, and only then on the retained engines.
                if self._wait_for_pause(dp_ranks):
                    if self.scale_down(dp_ranks):
                        self._on_scale_down_success(npu_id)
                    else:
                        print(
                            f"[faultsub] scale_down failed for NPU {npu_id}, mapping kept"
                        )
                else:
                    print(
                        f"[faultsub] scale_down skipped for NPU {npu_id} — pause incomplete"
                    )
            finally:
                with self._lock:
                    self._handling.discard(npu_id)
        else:
            # roce_link_down / driver_unhealthy (not recovered): log only. The
            # vLLM fault-tolerance framework auto-pauses internally; the
            # external manager must not scale_down for these.
            print(f"[faultsub] {ev_type} on NPU {npu_id}: log only, no scale_down")

    # ---- HTTP callback server ----

    def _make_handler(self):
        subscriber = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # silence default stderr noise
                pass

            def do_POST(self):
                if self.path != "/fault_event":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length) if length else b"{}"
                    event = json.loads(body.decode("utf-8") or "{}")
                except (ValueError, json.JSONDecodeError) as exc:
                    self.send_error(400, f"bad json: {exc}")
                    return
                try:
                    subscriber._handle_event(event)
                except Exception as exc:  # never let a handler crash the server
                    print(f"[faultsub] handler error: {exc}")
                self.send_response(200)
                self.end_headers()

        return _Handler

    # ---- subscription lifecycle ----

    def _rest_url(self, path: str) -> str:
        return (
            f"http://{self.cfg.catmonitor_host}:{self.cfg.catmonitor_rest_port}{path}"
        )

    def _register(self) -> Optional[str]:
        body = {
            "types": list(self.cfg.fault_types),
            "components": ["npu"],
            "npu_ids": [str(n) for n in self.cfg.npu_ids],
            "delivery": "webhook",
            "endpoint": self.cfg.advertise_url,
            "debounce_ms": self.cfg.debounce_ms,
            "min_severity": self.cfg.min_severity,
        }
        try:
            resp = requests.post(
                self._rest_url("/faultsub/subscriptions"), json=body, timeout=10
            )
            if resp.status_code != 201:
                print(
                    f"[faultsub] register failed {resp.status_code}: {resp.text[:300]}"
                )
                return None
            return resp.json().get("id")
        except requests.RequestException as exc:
            print(f"[faultsub] register request failed: {exc}")
            return None

    def _unregister(self) -> None:
        if not self._subscription_id:
            return
        try:
            requests.delete(
                self._rest_url(f"/faultsub/subscriptions/{self._subscription_id}"),
                timeout=10,
            )
            print(f"[faultsub] unregistered {self._subscription_id}")
        except requests.RequestException:
            pass

    # ---- public lifecycle ----

    def start(self, *, block: bool = False) -> None:
        self._server = ThreadingHTTPServer(
            (self.cfg.callback_host, self.cfg.callback_port), self._make_handler()
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="FaultSubServer", daemon=True
        )
        self._thread.start()
        print(
            f"[faultsub] listening {self.cfg.callback_host}:{self.cfg.callback_port} "
            f"({self.cfg.advertise_url})"
        )

        # Retry registration: CATMonitor may start after this process.
        for _ in range(30):
            sid = self._register()
            if sid:
                self._subscription_id = sid
                print(f"[faultsub] subscribed as {sid}")
                break
            time.sleep(2)
        if not self._subscription_id:
            print("[faultsub] WARNING: not registered with CATMonitor")

        if block:
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                self.stop()

    def stop(self) -> None:
        self._unregister()
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
        print("[faultsub] stopped")


def build_npu_to_dp_ranks(
    npu_ids: List[int],
    npu_per_die: int,
    visible_devices: List[int],
    dp_size: int,
) -> Dict[int, List[int]]:
    """Map each CATMonitor ``npu_id`` (a DIE on A3) to the vLLM DP ranks of
    the physical NPU cards it hosts.

    A3 topology: one DIE = ``npu_per_die`` physical cards; physical card ``p``
    belongs to DIE ``p // npu_per_die`` (so DIE 5 hosts cards 10 and 11 when
    ``npu_per_die=2``). vLLM DP rank ``r`` binds ``visible_devices[r]``, so a
    fault on DIE ``d`` must exclude every rank whose physical card falls in
    ``[d*npu_per_die, d*npu_per_die + npu_per_die)``. Dies that have no card
    inside ``visible_devices[:dp_size]`` (not part of the deployment) map to no
    ranks and their events are skipped.

    ``npu_ids`` is the subscription scope (DIE ids reported by CATMonitor);
    ``visible_devices`` is ASCEND_RT_VISIBLE_DEVICES in DP-rank order.
    """
    if dp_size < 1 or dp_size > len(visible_devices):
        raise ValueError(
            f"dp_size={dp_size} out of range "
            f"(visible devices: {len(visible_devices)})"
        )
    ranks_by_die: Dict[int, List[int]] = {}
    for rank, phys in enumerate(visible_devices[:dp_size]):
        ranks_by_die.setdefault(phys // npu_per_die, []).append(rank)
    wanted = set(npu_ids)
    return {d: sorted(ranks) for d, ranks in ranks_by_die.items() if d in wanted}
