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
                 clock: Callable[[], float] = time.monotonic):
        self.daemon = daemon
        self.groups = list(groups)
        self.load_fn = load_fn
        self.available = available or (lambda g: True)
        self.hysteresis = float(hysteresis)
        self.min_dwell_s = float(min_dwell_s)
        self.challenge_s = float(challenge_s)
        self._clock = clock
        self._last_swap = -1e18
        self._challenger: Optional[str] = None
        self._challenge_since = 0.0
        #: (group name, port) -> rules of that group that could fire there.
        #: Pure function of rule text + the site's variable table, so it is
        #: computed once per pair and reused.
        self._fire_cache: Dict[tuple, int] = {}

    # -- scoring -----------------------------------------------------------
    def rules_that_could_fire(self, group, port: int) -> int:
        """How many of ``group``'s rules could fire on a flow to ``port``.

        A rule counts only if its OWN destination-port token admits the
        port under the daemon's SR13 variable table — not merely if the
        group's coarse port class does.  That distinction is the whole
        correction: a ``literal``-class group holds rules for ports
        21/25/445 that can never fire on a port-80 flow, while every
        ``any``-token rule fires on every port.
        """
        key = (group.name, port)
        hit = self._fire_cache.get(key)
        if hit is None:
            vt = self.daemon.vars
            hit = 0
            for slot in group.slots:
                if slot.tombstone:
                    continue
                for r in slot.rules:
                    if vt.port_holds(r.dst_port, port):
                        hit += 1
            self._fire_cache[key] = hit
        return hit

    def scores(self) -> Dict[str, float]:
        """``group name -> expected value`` under the observed traffic.

        V(g) = Σ_ports  decayed_bytes[port] × rules_of_g_that_fire_on(port)

        i.e. the prefilter coverage that having ``g`` resident would have
        bought over the traffic just seen.  This replaces the pre-2026-07-29
        heuristic (bytes per port class, then round-robin within the winning
        class), which was measured to be **worse than pinning a single
        group** — it elected ``literal``-class groups worth 0.7–1.6%
        coverage over universal groups worth 27.9%, because matching the
        traffic's port class says nothing about how many of a group's rules
        can actually fire (docs/studies/a5-working-set.md).
        """
        mix = self.daemon.histogram.snapshot()
        out: Dict[str, float] = {}
        for g in self.groups:
            if not self.available(g):
                continue
            v = 0.0
            for port, nbytes in mix.items():
                if nbytes <= 0:
                    continue
                v += nbytes * self.rules_that_could_fire(g, port)
            out[g.name] = v
        return out

    def _resident_name(self) -> Optional[str]:
        pipe = self.daemon._pipeline
        return pipe.group.name if pipe is not None else None

    def _group_by_name(self, name: str):
        for g in self.groups:
            if g.name == name:
                return g
        return None

    # -- the decision ------------------------------------------------------
    def tick(self) -> Optional[str]:
        """Evaluate the mix; maybe swap.  Returns the newly resident group
        name when a swap happened, else None.

        Note what this does NOT do any more: it does not rotate among the
        groups of a dominant class.  Rotation was introduced so the whole
        class would eventually get coverage, but at single-tenant residency
        every rotation costs a ~16 s blind window and swaps 256 resident
        rules for a different 256 — instantaneous coverage is unchanged and
        the blind window is pure loss (a.k.a. the measured result that the
        old scheduler underperformed a static pin).  A swap now has to be
        justified by the value function or it does not happen.
        """
        now = self._clock()
        scores = self.scores()
        if not scores:
            return None
        best_name = max(scores, key=lambda n: scores[n])
        res_name = self._resident_name()

        if res_name is None:
            # Unfiltered: any positive-value candidate is worth loading
            # immediately (there is no blind window to weigh — already blind).
            if scores[best_name] <= 0:
                return None
            return self._swap(best_name, now)

        if best_name == res_name:
            self._challenger = None
            return None

        # A different group leads: demand a sustained, decisive lead.
        resident_score = scores.get(res_name, 0.0)
        if scores[best_name] < self.hysteresis * max(resident_score, 1e-9):
            self._challenger = None
            return None
        if self._challenger != best_name:
            self._challenger = best_name
            self._challenge_since = now
            return None
        if now - self._challenge_since < self.challenge_s:
            return None
        if now - self._last_swap < self.min_dwell_s:
            return None
        return self._swap(best_name, now)

    def _swap(self, name: str, now: float) -> Optional[str]:
        group = self._group_by_name(name)
        if group is None or not self.available(group):
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
