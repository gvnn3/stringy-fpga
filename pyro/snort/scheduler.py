"""SR10 residency scheduler: swap the resident group by observed port mix.

Single-tenant slot 1 (PYRO R64/R87): at most one group is resident.  The
scheduler reads the daemon's :class:`~pyro.snort.daemon.PortMixHistogram`,
scores port classes by their decayed byte share, and swaps when — and only
when — a challenger has sustained a decisive lead:

* **Hysteresis** (SF20): a JTAG rotation costs ~14–45 s of blind window
  (partial load + automatic wedge recovery), so a swap must be *rare*
  relative to that cost.  A challenger class must out-score the resident's
  class by ``hysteresis``× (default 2×) continuously for ``challenge_s``
  seconds, and no swap happens within ``min_dwell_s`` of the last one.
* **Within-class rotation**: a class holds up to 8 groups ($HTTP_PORTS);
  when the dominant class stays dominant, its groups take turns
  (round-robin per swap decision at ``min_dwell_s`` cadence), so coverage
  cycles through the whole class instead of pinning group 0.
* **Failure honesty**: a failed load leaves the system *unfiltered*
  (SR10: correctness unaffected, Snort sees all traffic; SR19 counts the
  window) and the scheduler retries on the next tick.

The physical swap is a seam (``load_fn``): tests inject a model-backed
loader; the live loader is :func:`jtag_load_pipeline` (JTAG partial load +
in-band wedge recovery + SR14 identity probe).
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Sequence

from .daemon import FilterDaemon, NominationPipeline


class ResidencyScheduler:
    """Drives which group is resident, from the measured port mix."""

    def __init__(self, daemon: FilterDaemon, groups: Sequence,
                 load_fn: Callable[[object], NominationPipeline],
                 available: Optional[Callable[[object], bool]] = None,
                 hysteresis: float = 2.0,
                 min_dwell_s: float = 300.0,
                 challenge_s: float = 60.0,
                 any_floor: float = 0.25,
                 clock: Callable[[], float] = time.monotonic):
        self.daemon = daemon
        self.groups = list(groups)
        self.load_fn = load_fn
        self.available = available or (lambda g: True)
        self.hysteresis = float(hysteresis)
        self.min_dwell_s = float(min_dwell_s)
        self.challenge_s = float(challenge_s)
        self.any_floor = float(any_floor)
        self._clock = clock
        self._by_class: Dict[str, List] = {}
        for g in self.groups:
            self._by_class.setdefault(g.port_class, []).append(g)
        for cls in self._by_class:
            self._by_class[cls].sort(key=lambda g: g.index)
        self._rotation: Dict[str, int] = {}
        self._last_swap = -1e18
        self._challenger: Optional[str] = None
        self._challenge_since = 0.0

    # -- scoring -----------------------------------------------------------
    def scores(self) -> Dict[str, float]:
        """Byte share per port class that has at least one available group.

        The histogram counts each packet under its most-specific class;
        the catch-all ``any`` class (whose rules apply to all traffic) gets
        a floor of ``any_floor`` × total so it rotates in on a steady mix
        instead of starving — a v1 heuristic, to be recalibrated from the
        SR19 evidence base (spec §10 OQ-1 discipline)."""
        mix = self.daemon.histogram.snapshot()
        total = sum(mix.values())
        out = {cls: mix.get(cls, 0.0) for cls in self._by_class
               if any(self.available(g) for g in self._by_class[cls])}
        if "any" in out:
            out["any"] = max(out["any"], self.any_floor * total)
        return out

    def _resident_class(self) -> Optional[str]:
        pipe = self.daemon._pipeline
        return pipe.group.port_class if pipe is not None else None

    def _next_group(self, cls: str):
        """Round-robin within the class, skipping unavailable groups."""
        members = [g for g in self._by_class[cls] if self.available(g)]
        if not members:
            return None
        i = self._rotation.get(cls, -1) + 1
        self._rotation[cls] = i
        return members[i % len(members)]

    # -- the decision ------------------------------------------------------
    def tick(self) -> Optional[str]:
        """Evaluate the mix; maybe swap.  Returns the newly resident group
        name when a swap happened, else None."""
        now = self._clock()
        scores = self.scores()
        if not scores:
            return None
        best_cls = max(scores, key=lambda c: scores[c])
        res_cls = self._resident_class()

        if res_cls is None:
            # Unfiltered: any traffic at all justifies loading immediately
            # (there is no blind-window cost to weigh — we are already blind).
            if scores[best_cls] <= 0:
                return None
            return self._swap(best_cls, now)

        if best_cls == res_cls:
            self._challenger = None
            # Same class staying dominant: rotate through its groups at
            # dwell cadence so the whole class gets coverage (SR10).
            if (len([g for g in self._by_class[res_cls]
                     if self.available(g)]) > 1
                    and now - self._last_swap >= self.min_dwell_s):
                return self._swap(res_cls, now)
            return None

        # A different class leads: demand a sustained, decisive lead.
        resident_score = scores.get(res_cls, 0.0)
        if scores[best_cls] < self.hysteresis * max(resident_score, 1e-9):
            self._challenger = None
            return None
        if self._challenger != best_cls:
            self._challenger = best_cls
            self._challenge_since = now
            return None
        if now - self._challenge_since < self.challenge_s:
            return None
        if now - self._last_swap < self.min_dwell_s:
            return None
        return self._swap(best_cls, now)

    def _swap(self, cls: str, now: float) -> Optional[str]:
        group = self._next_group(cls)
        if group is None:
            return None
        pipe = self.daemon._pipeline
        if pipe is not None and pipe.group.name == group.name:
            return None
        # The blind window starts now and is SR19-visible: unfiltered until
        # the load completes and the SR14 identity check passes.
        self.daemon.set_pipeline(None)
        try:
            new_pipe = self.load_fn(group)
        except Exception:
            # Unfiltered is a safe state (SR10); retry next tick.
            self._last_swap = now
            return None
        if not new_pipe.identity_ok():
            # SR14: never attribute against a mismatched child.
            self._last_swap = now
            return None
        self.daemon.set_pipeline(new_pipe)
        self.daemon.stats.swaps += 1
        self._last_swap = now
        self._challenger = None
        return group.name


# --------------------------------------------------------------------------
# The live loader (JTAG partial + wedge recovery + identity probe)
# --------------------------------------------------------------------------
def jtag_load_pipeline(group, bitstream_path: str, iface: str,
                       stats=None) -> NominationPipeline:
    """Load ``group``'s partial over JTAG and return a wire-backed pipeline.

    The load wedges the RP by design of the flow; recovery is in-band
    (``scripts/pyro_wedge_recover.sh``: user reset 0x014 + QDMA soft reset
    0x00C + onic reload) and is invoked unconditionally after the load.
    ~14–45 s total (SF20).  Raises on any failure — the scheduler treats
    that as "stay unfiltered and retry".
    """
    import os
    import subprocess

    from .. import device as _device
    from .daemon import Stats, WireTransport

    cfg = _device.DeviceConfig(iface=iface)
    _device.load_partial(cfg, bitstream_path)
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    subprocess.run(["sudo", os.path.join(repo, "scripts",
                                         "pyro_wedge_recover.sh")],
                   check=True, capture_output=True, timeout=120)
    transport = WireTransport(iface, group)
    return NominationPipeline(transport, stats or Stats())
