"""FPGA functionality scheduling — the research surface.

This subpackage is apparatus for the actual research question: **can OS
scheduler techniques manage FPGA functionality at run time?**  It is
deliberately separate from ``pyro.snort`` (one workload) and ``pyro.re``
(another), because the scheduling question is about *managing tenants*,
not about any one tenant's job.

The vocabulary is the operating-system one, and the mapping is literal:

===========================  =========================================
FPGA                         OS
===========================  =========================================
``pyro_rp`` region           physical memory / a CPU
a loaded circuit             a resident process (or page)
JTAG partial reconfig        a context switch (measured: ~13.6 s)
an overlay table write       a *cheap* context switch (not built yet)
observed traffic             the demand signal that generates faults
SR5 "a miss is safe"         a page fault with no correctness penalty
===========================  =========================================

Two properties make this more than an analogy exercise, and they are why
the classic results may not transfer unchanged:

1. **The switch is partially preemptible.**  Partial reconfiguration
   replaces the whole region; an overlay table write can replace *part* of
   the functionality while the rest keeps running.  That is page- versus
   process-granularity preemption, a distinction CPU schedulers never have
   to make.
2. **A miss is benign.**  Snort sees all traffic regardless (SR5), so a
   "fault" costs coverage, never correctness.  Almost no cache in the OS
   literature has that property, and it licenses speculation that would be
   reckless elsewhere.

:mod:`pyro.sched.tenants` defines the schedulable units.
"""

from __future__ import annotations

from .gang import (  # noqa: F401
    GangTenant,
    build_pipeline,
    header_prefilter_pipeline,
    view_meet,
)
from .deadline import (  # noqa: F401
    DeadlineTenant,
    host_path_tenant,
    inline_filter_tenant,
    partition_report,
)
from .tenants import (  # noqa: F401
    FRAME_OFFSETS,
    Tenant,
    TenantDemand,
    build_all_tenants,
    header_match_tenant,
    ip_match_tenant,
    matched_treatment,
    pyro_regex_tenant,
    snortpf_tenants,
)

__all__ = [
    "FRAME_OFFSETS",
    "GangTenant",
    "build_pipeline",
    "header_prefilter_pipeline",
    "view_meet",
    "Tenant",
    "TenantDemand",
    "DeadlineTenant",
    "build_all_tenants",
    "homogeneity",
    "host_path_tenant",
    "inline_filter_tenant",
    "partition_report",
    "matched_treatment",
    "same_kind_control",
    "header_match_tenant",
    "ip_match_tenant",
    "pyro_regex_tenant",
    "snortpf_tenants",
]
