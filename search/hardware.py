# MIT License

# Copyright (c) 2021 ByteDance Inc. All rights reserved.
# Copyright (c) 2021 Duke University. All rights reserved.

# See LICENSE for license information


import subprocess


class BaseHwMon(object):
    '''
        HwMon collects diagnostic counters. 
        Users should overwrite this class for their own diagnostic counters collector.
        Collie uses HwMon for Mellanox (works well with CX5/6) 
        and Broadcom (works well with P2100) in RoCEv2 environment

        @binary: the binary helps to collect diag counters (string)
                 should be the absolute address
        @counters: the name list of counters to collect  (list)
        @identity: the identity of the NIC to monitor, e.g., PCIe address, IB name. (string)
    '''

    def __init__(self, binary, counters):
        super(BaseHwMon, self).__init__()
        self._binary = str(binary)
        self._counters = counters

    def monitor(self, identity):
        raise NotImplementedError


'''
    Hardware monitor collects diagnostic counters.
    No diagnostic counters are currently publicly available. We remove them for confidentiality.
'''


class MlnxHwMon(BaseHwMon):
    ''' 
        Mellanox HwMon
        Rely on NeoHost to collect diagnostic counters that indicate anomalies.
    '''

    def __init__(self, binary, counters):
        super(MlnxHwMon, self).__init__(binary, counters)

    def monitor(self, identity):
        return {}


class SysfsHwMon(BaseHwMon):
    '''
        Unprivileged stand-in for MlnxHwMon.

        The NDA diagnostic counters NeoHost exposes are not available here, but
        a few genuinely diagnostic ones are readable without root -- notably
        outbound_pci_stalled_rd/wr(+_events), which report the NIC stalling on
        the PCIe host interface and are the closest public analogue to the
        counters Collie's paper drives its search with.

        This monitor does not sample the hardware itself: it re-uses the
        snapshot SysfsBoneMon already took for this point, so a point still
        costs exactly one measurement window instead of two, and the bone and
        diagnostic views are guaranteed to describe the same window.

        @bonemon:  the bone.SysfsBoneMon instance measuring this run.
        @counters: optional whitelist; empty means "all diagnostic counters".
    '''

    def __init__(self, bonemon, counters=None):
        super(SysfsHwMon, self).__init__("", counters or [])
        self._bonemon = bonemon

    def monitor(self, identity):
        result = self._bonemon.diag_counters(getattr(self._bonemon, "_last", {}))
        if self._counters:
            result = {k: v for k, v in result.items() if k in self._counters}
        return result


'''
    BrcmHwMon Implementation
'''
