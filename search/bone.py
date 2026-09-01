# MIT License

# Copyright (c) 2021 ByteDance Inc. All rights reserved.
# Copyright (c) 2021 Duke University. All rights reserved.

# See LICENSE for license information


# Bone is what Collie likes! (Say, the anomaly of the RDMA NIC)

import os
import subprocess
import time


class BaseBoneMon(object):
    '''
        BoneMon should collects throughput and pause framesor any other anomaly signal.
        @bps_bar: the throughput threshold in bits per sec. (float)
        @pps_bar: the throughput threshold in pkts per sec. (float)
        @intf: the interface BoneMon to monitor. (str)

    '''

    def __init__(self, bps_bar, pps_bar):
        super(BaseBoneMon, self).__init__()
        self._bps_bar = float(bps_bar)
        self._pps_bar = float(pps_bar)

    def monitor(self, intf):
        raise NotImplementedError

    def check_bone(self, result, point=None):
        raise NotImplementedError


class MlnxBoneMon(BaseBoneMon):
    '''
        Mellanox Bone Monitor
    '''

    def __init__(self, bps_bar, pps_bar, tx_pfc_bar, rx_pfc_bar):
        super(MlnxBoneMon, self).__init__(bps_bar, pps_bar)
        self._tx_pfc_bar = float(tx_pfc_bar)
        self._rx_pfc_bar = float(rx_pfc_bar)
        self._metrics = [
            # bits per second (TX)      in Mbps
            "tx_vport_rdma_unicast_bytes",
            # bits per second (RX)      in Mbps
            "rx_vport_rdma_unicast_bytes",
            # packets per second (TX)   in pps
            "tx_vport_rdma_unicast_packets",
            # packets per second (RX)   in pps
            "rx_vport_rdma_unicast_packets",
            "tx_prio3_pause_duration",  # pfc duration per second (TX)  in us
            "rx_prio3_pause_duration"   # pfc duration per second (RX)  in us
        ]

    def monitor(self, intf):
        result = {key: 0.0 for key in self._metrics}
        cmd = "mlnx_perf -i {} -c 1".format(intf)
        try:
            output = subprocess.check_output(cmd, shell=True)
        except Exception as e:
            print(e)
            return {key: -1.0 for key in self._metrics}
        output = output.decode().split('\n')
        for line in output:
            for metric in self._metrics:
                if metric in line:
                    line = line.strip(' ').split(' ')
                    if "bytes" in metric:
                        result[metric] = float(
                            line[-2].replace(',', '')) / 1000.0
                    else:
                        result[metric] = float(line[-1].replace(',', ''))
        return result

    def check_bone(self, result, point=None):
        # @point is accepted for signature parity with SysfsBoneMon (upstream
        # mlnx path keys off PFC pause duration, so it is ignored here).
        # First, we check pause duration
        if (result["tx_prio3_pause_duration"] > self._tx_pfc_bar or
                result["rx_prio3_pause_duration"] > self._rx_pfc_bar):
            return -1

        if (result["tx_vport_rdma_unicast_bytes"] < self._bps_bar and
                result["rx_vport_rdma_unicast_bytes"] < self._bps_bar):
            # bps does not achieve, chk with pps
            # However, pps_bar is pretty hard to set accurately.
            # In production, we chk with bps for most scenarios.
            if (result["tx_vport_rdma_unicast_packets"] < self._pps_bar and
                    result["rx_vport_rdma_unicast_packets"] < self._pps_bar):
                return -2
        return 0


# ---------------------------------------------------------------------------
# SysfsBoneMon: monitor for clusters where the vendor tooling is unavailable.
#
# Upstream MlnxBoneMon needs `mlnx_perf` (MFT/OFED, root-only on most sites)
# and keys its anomaly signal off PFC pause duration.  Neither is usable on an
# unprivileged HPC node:
#   * no mlnx_perf / NeoHost / mst  -> counters must come from ethtool + sysfs
#   * PFC disabled on the fabric    -> pause duration is identically zero, so
#                                      the primary anomaly signal is inert.
#
# This class replaces both halves using only unprivileged sources:
#   ethtool -S <netdev>
#   /sys/class/infiniband/<ibdev>/ports/<port>/hw_counters/*
#   /sys/class/infiniband/<ibdev>/ports/<port>/counters/*
# and replaces the pause objective with an explicit backpressure/loss signal.
# ---------------------------------------------------------------------------

# Throughput proxies.  On internal-HCA loopback only the rx_ side of the vport
# counters advances (tx_vport_* and every *_phy counter stay at 0 because the
# frames never reach the wire), so throughput must be read as tx+rx rather than
# as "tx or rx" the way upstream does it.
BPS_KEYS = ["tx_vport_rdma_unicast_bytes", "rx_vport_rdma_unicast_bytes"]
PPS_KEYS = ["tx_vport_rdma_unicast_packets", "rx_vport_rdma_unicast_packets"]

# Counters whose *delta over the measurement window* means the NIC dropped,
# retransmitted or backpressured something.  Any nonzero => anomaly.
#   rx_out_of_buffer / out_of_buffer : receiver ran out of posted WQEs
#   rx_discards_phy                  : port-level drop
#   packet_seq_err / out_of_sequence : PSN gap -> loss inside the NIC/fabric
#   roce_adp_retrans                 : adaptive retransmission fired
#   *_pause_ctrl_phy                 : global pause frames (inert while PFC is
#                                      off, kept so the signal still works at a
#                                      PFC-enabled site)
ANOMALY_KEYS = [
    "rx_out_of_buffer",
    "out_of_buffer",
    "rx_discards_phy",
    "packet_seq_err",
    "out_of_sequence",
    "roce_adp_retrans",
    "rx_pause_ctrl_phy",
    "tx_pause_ctrl_phy",
]

# Collected and logged, but never enough on their own to declare an anomaly.
# outbound_pci_stalled_* is the closest unprivileged stand-in for the NDA
# diagnostic counters Collie's paper drives the search with, so it is exposed
# here as a selectable maximisation target (see hardware.SysfsHwMon).
DIAG_KEYS = [
    "outbound_pci_stalled_rd",
    "outbound_pci_stalled_wr",
    "outbound_pci_stalled_rd_events",
    "outbound_pci_stalled_wr_events",
    "local_ack_timeout_err",
    "np_cnp_sent",
    "np_ecn_marked_roce_packets",
    "rp_cnp_handled",
    "duplicate_request",
    "implied_nak_seq_err",
    "rnr_nak_retry_err",
    "req_cqe_error",
    "resp_cqe_error",
    "req_remote_access_errors",
    "req_remote_invalid_request",
    "resp_local_length_error",
    "tx_discards_phy",
    "port_xmit_wait",
]

# Which source each counter comes from.  ethtool is authoritative when a name
# exists in both places (e.g. rx_out_of_buffer) because it is the vport view.
ETHTOOL_KEYS = BPS_KEYS + PPS_KEYS + [
    "rx_out_of_buffer", "rx_discards_phy", "tx_discards_phy",
    "rx_pause_ctrl_phy", "tx_pause_ctrl_phy",
    "outbound_pci_stalled_rd", "outbound_pci_stalled_wr",
    "outbound_pci_stalled_rd_events", "outbound_pci_stalled_wr_events",
    "rx_bytes_phy", "tx_bytes_phy", "rx_packets_phy", "tx_packets_phy",
]
HW_COUNTER_KEYS = [
    "out_of_buffer", "packet_seq_err", "out_of_sequence", "roce_adp_retrans",
    "roce_adp_retrans_to", "local_ack_timeout_err", "np_cnp_sent",
    "np_ecn_marked_roce_packets", "rp_cnp_handled", "duplicate_request",
    "implied_nak_seq_err", "rnr_nak_retry_err", "req_cqe_error",
    "req_cqe_flush_error", "resp_cqe_error", "req_remote_access_errors",
    "req_remote_invalid_request", "resp_local_length_error",
]
PORT_COUNTER_KEYS = [
    "port_xmit_data", "port_rcv_data", "port_xmit_packets", "port_rcv_packets",
    "port_xmit_wait", "port_xmit_discards", "port_rcv_errors",
]


# ---------------------------------------------------------------------------
# Per-QP-type throughput bars.
#
# A single bps bar derived from 64KiB-1MiB RC WRITE (~166 Gb/s here) is
# UNREACHABLE by UD: UD caps every message at the path MTU (<=4 KiB), so its
# loopback ceiling is ~129 Gb/s and *every* UD point looked like "reduced
# throughput" under one RC-WRITE bar.  Fix: give each transport that can appear
# its own measured ceiling and compare each point against the bar(s) for the
# transport(s) it actually contains (see SysfsBoneMon._bars_for_point).
#
# QP-type ints follow search/space.py: 0=UD, 1=UC, 2=RC.
_QP_UD = 0

def bar_key(qp_type, opcode):
    '''Canonical throughput-bar key for one (QP-type, opcode) pair.

    UD is MTU-capped and only does SEND -> its own low ceiling (UD_SEND).
    UC and RC segment large messages and reach the same connected ceiling,
    which depends on the opcode; UC cannot READ, so it borrows the RC bars.
    '''
    if qp_type == _QP_UD:            # UD -> SEND only
        return "UD_SEND"
    if opcode == 'w':
        return "RC_WRITE"
    if opcode == 'r':
        return "RC_READ"
    return "RC_SEND"                 # 's' on RC/UC


class SysfsBoneMon(BaseBoneMon):
    '''
        Unprivileged bone monitor (ethtool -S + infiniband sysfs).

        @bps_bar: expected throughput in Gbit/s   (tx+rx of the vport counters)
        @pps_bar: expected throughput in pkt/s    (tx+rx of the vport counters)
        @margin:  how far under the bar counts as "reduced throughput".  Collie
                  flags a point when bps AND pps are both more than 20% below
                  the bar, hence the default of 0.20.
        @window:  seconds of steady-state traffic each point is measured over.
        @settle:  seconds to wait after the QPs reach RTS before the first
                  snapshot, so connection setup is not charged to the point.
        @tx_pfc_bar/@rx_pfc_bar: accepted for config compatibility with
                  MlnxBoneMon.  Pause frames are folded into ANOMALY_KEYS
                  instead of being their own objective, because PFC is off here.
        @type_bars: optional {bar_key: {"bps": Gbit/s, "pps": pkt/s}} of
                  per-transport ceilings (keys from bar_key(): "RC_WRITE",
                  "RC_SEND", "RC_READ", "UD_SEND").  When present, the reduced-
                  throughput (-2) test compares a point against the transport(s)
                  it contains instead of the single scalar bps_bar/pps_bar,
                  which removes the UD false positives.  bps_bar/pps_bar remain
                  the fallback when type_bars is absent or a point exposes no
                  recognised transport.
    '''

    def __init__(self, bps_bar, pps_bar, tx_pfc_bar=0.0, rx_pfc_bar=0.0,
                 ibdev="mlx5_0", ib_port=1, margin=0.20, window=10.0,
                 settle=2.0, min_pps=1000.0, type_bars=None):
        super(SysfsBoneMon, self).__init__(bps_bar, pps_bar)
        self._tx_pfc_bar = float(tx_pfc_bar)
        self._rx_pfc_bar = float(rx_pfc_bar)
        # Normalise the per-transport ceilings to {key: {"bps": f, "pps": f}}.
        self._type_bars = {}
        for k, v in (type_bars or {}).items():
            try:
                self._type_bars[k] = {"bps": float(v["bps"]),
                                      "pps": float(v["pps"])}
            except (TypeError, KeyError, ValueError):
                continue
        self._ibdev = str(ibdev)
        self._ib_port = int(ib_port)
        self._margin = float(margin)
        self._window = float(window)
        self._settle = float(settle)
        # Below this packet rate we assume the traffic never really started
        # (setup race, bind failure, ...) and report "dead", not "anomaly".
        self._min_pps = float(min_pps)
        self._hw_path = "/sys/class/infiniband/{}/ports/{}/hw_counters".format(
            self._ibdev, self._ib_port)
        self._port_path = "/sys/class/infiniband/{}/ports/{}/counters".format(
            self._ibdev, self._ib_port)
        self._last = {}

    # ---- raw collection -------------------------------------------------

    def _read_ethtool(self, intf):
        out = {}
        try:
            raw = subprocess.check_output(
                ["ethtool", "-S", intf], stderr=subprocess.DEVNULL).decode()
        except Exception as e:
            print("ethtool -S {} failed: {}".format(intf, e))
            return out
        wanted = set(ETHTOOL_KEYS)
        for line in raw.split('\n'):
            if ':' not in line:
                continue
            k, _, v = line.partition(':')
            k = k.strip()
            if k in wanted:
                try:
                    out[k] = int(v.strip().replace(',', ''))
                except ValueError:
                    pass
        return out

    def _read_sysfs_dir(self, path, keys):
        out = {}
        for k in keys:
            try:
                with open(os.path.join(path, k), "r") as fh:
                    out[k] = int(fh.read().strip())
            except Exception:
                # Counter absent on this firmware/kernel: skip silently.  The
                # anomaly test only looks at keys that were actually collected.
                continue
        return out

    def snapshot(self, intf):
        snap = {}
        snap.update(self._read_sysfs_dir(self._hw_path, HW_COUNTER_KEYS))
        snap.update(self._read_sysfs_dir(self._port_path, PORT_COUNTER_KEYS))
        # ethtool last so its vport view wins any name collision
        snap.update(self._read_ethtool(intf))
        return snap

    # ---- windowed measurement ------------------------------------------

    def monitor(self, intf):
        '''
            Let the traffic settle, then take a delta over self._window.
            Returns rates (Gbit/s, pkt/s) plus the per-window delta of every
            counter, prefixed with "d_".
        '''
        if self._settle > 0:
            time.sleep(self._settle)
        before = self.snapshot(intf)
        t0 = time.time()
        time.sleep(self._window)
        after = self.snapshot(intf)
        elapsed = max(time.time() - t0, 1e-6)

        result = {"window_s": round(elapsed, 3)}
        if not before or not after:
            # Collection itself failed; mirror upstream's -1.0 sentinel style.
            result["bps"] = -1.0
            result["pps"] = -1.0
            return result

        delta = {}
        for k in after:
            if k in before:
                d = after[k] - before[k]
                # 64-bit counters do not realistically wrap in a 10s window; a
                # negative delta means the counter was reset, so treat it as 0.
                delta[k] = d if d >= 0 else 0

        tx_bytes = delta.get(BPS_KEYS[0], 0)
        rx_bytes = delta.get(BPS_KEYS[1], 0)
        tx_pkts = delta.get(PPS_KEYS[0], 0)
        rx_pkts = delta.get(PPS_KEYS[1], 0)

        result["tx_bps"] = tx_bytes * 8.0 / elapsed / 1e9
        result["rx_bps"] = rx_bytes * 8.0 / elapsed / 1e9
        result["bps"] = result["tx_bps"] + result["rx_bps"]
        result["tx_pps"] = tx_pkts / elapsed
        result["rx_pps"] = rx_pkts / elapsed
        result["pps"] = result["tx_pps"] + result["rx_pps"]
        for k in sorted(delta):
            result["d_" + k] = delta[k]
        self._last = result
        return result

    # ---- objective ------------------------------------------------------

    def anomaly_counters(self, result):
        '''Names of backpressure/loss counters that moved during the window.'''
        hits = {}
        for k in ANOMALY_KEYS:
            v = result.get("d_" + k, 0)
            if v:
                hits[k] = v
        return hits

    def diag_counters(self, result):
        '''Diagnostic counter deltas, usable as an SA maximisation target.'''
        return {k: result.get("d_" + k, 0) for k in DIAG_KEYS
                if ("d_" + k) in result}

    def _bars_for_point(self, point):
        '''
            Per-QP-type (bps_bar, pps_bar) for one point.

            Each transport is judged against its OWN measured loopback ceiling
            (see bar_key / type_bars), so a UD point is never compared to the
            RC-WRITE bandwidth bar it physically cannot reach.

            Mix rule: a point that contains more than one transport is held to
            the MINIMUM applicable bar (the slowest transport it carries), for
            both bps and pps.  Rationale: a point spends its fixed QP/process
            budget across the transports it mixes, so it cannot be expected to
            reach the ceiling of its fastest transport once part of that budget
            is tied up in a slow one (UD).  The min bar is therefore the
            conservative threshold -- it never false-flags a point merely for
            *containing* a slow transport, while a point that falls below even
            its weakest transport's ceiling still trips -2.  Falls back to the
            scalar bars when no per-type bars are configured or the point
            exposes no recognised transport.
        '''
        if not self._type_bars or point is None:
            return self._bps_bar, self._pps_bar
        traffics = getattr(point, "_traffics", None)
        if not traffics:
            return self._bps_bar, self._pps_bar
        bps_bars, pps_bars = [], []
        for tr in traffics:
            qp_type = getattr(tr, "_qp_type", 2)
            # Opcodes present in this traffic (handles mixed-opcode traffics);
            # a req string is like "w_1_65536", so its first char is the op.
            ops = set()
            for req in getattr(tr, "_reqs", []) or []:
                s = str(req).strip()
                if s:
                    ops.add(s[0])
            if not ops:
                ops.add(getattr(tr, "_opcode", "w"))
            for op in ops:
                bar = self._type_bars.get(bar_key(qp_type, op))
                if bar:
                    bps_bars.append(bar["bps"])
                    pps_bars.append(bar["pps"])
        if not bps_bars:
            return self._bps_bar, self._pps_bar
        return min(bps_bars), min(pps_bars)

    def check_bone(self, result, point=None):
        '''
             0  clean
            -1  backpressure / loss anomaly (replaces upstream's PFC signal)
            -2  reduced throughput: bps AND pps both >margin under their bar
            -3  no traffic observed -> failed setup, not a NIC anomaly

            @point: the space.Point being measured; when given together with
                    per-type bars it selects the applicable bar(s) so UD points
                    are compared to the UD ceiling, not the RC-WRITE one.  The
                    -1 backpressure/loss signal below is unchanged and stays the
                    primary anomaly indicator.
        '''
        if result.get("bps", -1.0) < 0.0:
            return -3
        if result.get("pps", 0.0) < self._min_pps:
            return -3
        if self.anomaly_counters(result):
            return -1
        # Per-QP-type reduced-throughput test (was: single scalar bps/pps bar).
        bps_bar, pps_bar = self._bars_for_point(point)
        floor = 1.0 - self._margin
        if (result["bps"] < bps_bar * floor and
                result["pps"] < pps_bar * floor):
            return -2
        return 0
