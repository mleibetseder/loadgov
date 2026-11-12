"""Effective limits = active profile, overlaid by overrides, capped by the owner ceiling.

Pure: no I/O; the clock is injected. Priority is system > human (owner) > agent:

- anyone may *tighten* any parameter at any time (an agent tighten expires like a loosening);
- a *human restriction* is an owner ceiling, an owner hold/freeze flag, an owner-chosen profile
  tighter than the default profile, or an owner override that tightened the effective value (see
  `restriction()`); while one is active an agent *loosening* (looser than the owner-only
  `baseline()`) is refused, never clamped;
- an agent loosening without a restriction expires (default 2 h, max 12 h) and then resets;
- an owner profile switch, or any owner change that leaves a restriction active, cancels every
  agent override looser than the baseline, judged from the current state.

"Tighter" is per parameter (`config.RESTRICTIVE`): lower for cores, cpu %, load, implementers and
the build limits, but *higher* for `ram_floor_gb`, because a higher floor makes the memory guard
act earlier. A ceiling therefore takes the minimum of the first group and the maximum of the floor.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from loadgov.config import PARAMS, Profile, check_param, loosens, tighten, tighter

OWNER, AGENT, SYSTEM = "owner", "agent", "system"  # system is reserved for the safety guards
PRINCIPALS = (OWNER, AGENT, SYSTEM)
AGENT_TTL_DEFAULT = 2 * 3600
AGENT_TTL_MAX = 12 * 3600


def iso(ts: float | None) -> str | None:
    return None if ts is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@dataclass
class Override:
    id: str
    principal: str
    param: str
    value: float
    reason: str
    created: float
    expires: float | None
    previous: float  # the effective value before this override
    project: str | None = None
    restricts: bool = False  # owner override that tightened the effective value when it was set


@dataclass
class Ceiling:
    profile: str
    reason: str
    principal: str
    created: float
    expires: float | None = None


@dataclass
class Flag:  # owner hold / freeze: modelled now so the restriction rule already covers them
    reason: str
    principal: str
    created: float


@dataclass
class LimitState:
    profile: str
    profile_previous: str | None = None  # restored when profile_expires passes
    profile_expires: float | None = None
    overrides: list[Override] = field(default_factory=list)
    ceiling: Ceiling | None = None
    hold: Flag | None = None
    freeze: Flag | None = None
    next_id: int = 1
    profile_restricts: bool = False  # the owner's profile switch tightened the effective values
    profile_previous_restricts: bool = False  # restored with profile_previous
    profile_reason: str | None = None  # who chose the active profile and why (None: default or restored)
    profile_principal: str | None = None
    profile_set_at: float | None = None


class Limits:
    def __init__(
        self,
        profiles: dict[str, Profile],
        state: LimitState,
        clock: Callable[[], float] = time.time,
        default_profile: str = "balanced",
    ):
        self.profiles, self.state, self.clock = profiles, state, clock
        self.default_profile = default_profile
        self.events: list[dict] = []
        self._commit(self.effective())  # a restored state may violate the rule: fix it at load

    # ---- reading -------------------------------------------------------------------------------

    def _values(self, with_agent: bool) -> dict:
        """The five tunables: profile, then owner/system overrides, then (optionally) agent overrides,
        then the owner ceiling as a cap."""
        st, base = self.state, self.profiles[self.state.profile]
        vals = {k: getattr(base, k) for k in PARAMS}
        for o in st.overrides:  # in creation order: the later override wins
            if o.principal != AGENT:
                vals[o.param] = o.value
        if with_agent:
            for o in st.overrides:
                if o.principal == AGENT:
                    vals[o.param] = o.value
        if st.ceiling:
            c = self.profiles[st.ceiling.profile]
            vals = {k: tighten(k, v, getattr(c, k)) for k, v in vals.items()}
        return vals

    def baseline(self) -> dict:
        """What applies without agent overrides: profile + owner overrides + ceiling."""
        return self._values(False)

    def effective(self) -> dict:
        st, base = self.state, self.profiles[self.state.profile]
        vals = self._values(True)
        cargo, vitest = base.cargo_build_jobs, base.vitest_max_workers
        shard = base.gate_shard_budget
        if st.ceiling:
            c = self.profiles[st.ceiling.profile]
            cargo, vitest = min(cargo, c.cargo_build_jobs), min(vitest, c.vitest_max_workers)
            shard = min(shard, c.gate_shard_budget)
        return {
            **vals,
            "cargo_build_jobs": cargo,
            "load_hold": base.load_hold,  # never derived from load_max
            "impl_max_quiet": min(base.impl_max_quiet, vals["max_implementers"]),
            "vitest_max_workers": vitest,
            "gate_shard_budget": shard,
            "hold_heavy_slot": base.hold_heavy_slot,
        }

    def restriction(self) -> dict | None:
        """The active human restriction, or None: an owner ceiling, an owner hold/freeze, an owner
        profile switch or override that tightened the effective value when it was made (until it
        expires, is replaced or is cleared), or an owner override tighter than the active profile."""
        st = self.state
        if st.ceiling:
            return {"kind": "ceiling", "reason": st.ceiling.reason, "ceiling": st.ceiling.profile}
        for kind in ("freeze", "hold"):
            flag = getattr(st, kind)
            if flag:
                return {"kind": kind, "reason": flag.reason, "ceiling": None}
        base = self.profiles[st.profile]
        if st.profile_restricts:
            return {"kind": "profile", "reason": f"owner profile {st.profile}", "ceiling": None}
        for o in st.overrides:
            if o.principal != AGENT and (o.restricts or tighter(o.param, o.value, getattr(base, o.param))):
                return {"kind": "override", "reason": o.reason, "ceiling": None}
        return None

    def snapshot(self) -> dict:
        st, r = self.state, self.restriction()
        return {
            "profile": st.profile,
            "profile_expires_at": iso(st.profile_expires),
            "effective": self.effective(),
            "overrides": [
                {
                    "id": o.id,
                    "principal": o.principal,
                    "param": o.param,
                    "value": o.value,
                    "reason": o.reason,
                    "project": o.project,
                    "previous": o.previous,
                    "set_at": iso(o.created),
                    "expires_at": iso(o.expires),
                }
                for o in st.overrides
            ],
            "profile_info": None
            if st.profile_reason is None
            else {
                "profile": st.profile,
                "reason": st.profile_reason,
                "principal": st.profile_principal,
                "set_at": iso(st.profile_set_at),
                "expires_at": iso(st.profile_expires),
            },
            "ceiling": None
            if not st.ceiling
            else {
                "profile": st.ceiling.profile,
                "reason": st.ceiling.reason,
                "principal": st.ceiling.principal,
                "set_at": iso(st.ceiling.created),
                "expires_at": iso(st.ceiling.expires),
            },
            "hold": st.hold.reason if st.hold else None,
            "freeze": st.freeze.reason if st.freeze else None,
            "restriction": {"active": r is not None, "by": OWNER if r else None, **(r or {})},
        }

    def drain_events(self) -> list[dict]:
        ev, self.events = self.events, []
        return ev

    # ---- changing ------------------------------------------------------------------------------

    def _event(self, op, principal, result, reason, before, after, **extra) -> None:
        self.events.append(
            {
                "op": op,
                "principal": principal,
                "result": result,
                "reason": reason,
                "before": before,
                "after": after,
                **extra,
            }
        )

    def _expiry(self, principal: str, ttl_s: float | None, until: float | None) -> tuple[float | None, bool]:
        """(expires epoch or None, ttl was capped). Agents always expire; owners may be indefinite."""
        now = self.clock()
        if ttl_s is None and until is not None:
            ttl_s = max(until - now, 1.0)
        if principal == AGENT:
            ttl_s = AGENT_TTL_DEFAULT if ttl_s is None else ttl_s
            if ttl_s > AGENT_TTL_MAX:
                return now + AGENT_TTL_MAX, True
        return (None if ttl_s is None else now + ttl_s), False

    def _commit(self, before: dict, force: bool = False) -> None:
        """The single choke point every state change ends in (set, ceiling, flags, reset, expiries,
        profile restore, state load). While a human restriction is active (or after an owner profile
        switch or restore, `force`), no agent override may be looser than the owner-only baseline:
        cancel those at once, judged from the current state."""
        if not force and not self.restriction():
            return
        base = self.baseline()
        gone = [
            o
            for o in self.state.overrides
            if o.principal == AGENT and loosens(o.param, o.value, base[o.param])
        ]
        self.state.overrides = [o for o in self.state.overrides if o not in gone]
        for o in gone:
            self._event(
                "set", AGENT, "cancelled", "cancelled by owner restriction", before, self.effective(),
                request={"override": o.id},
            )  # fmt: skip

    def set(
        self,
        principal: str,
        changes: dict,
        reason: str,
        ttl_s: float | None = None,
        until: float | None = None,
        profile: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Apply `changes` (param -> value) and/or switch to `profile`. Raises ValueError on bad input."""
        if principal not in PRINCIPALS:
            raise ValueError(f"unknown principal {principal!r}")
        if profile is not None and profile not in self.profiles:
            raise ValueError(f"unknown profile {profile!r}")
        changes = {k: check_param(k, v) for k, v in changes.items()}
        before = self.effective()
        now = self.clock()
        expires, capped = self._expiry(principal, ttl_s, until)
        st = self.state

        if principal == AGENT:
            targets = {k: getattr(self.profiles[profile], k) for k in PARAMS} if profile else {}
            targets.update(changes)
            targets = {k: v for k, v in targets.items() if v != before[k]}
            owner_base = self.baseline()
            if any(loosens(k, v, owner_base[k]) for k, v in targets.items()):
                r = self.restriction()
                if r:
                    self._event("set", principal, "refused", reason, before, before, restriction=r)
                    return {
                        "ok": False,
                        "refused": True,
                        "restrictedBy": OWNER,
                        "reason": r["reason"],
                        "ceiling": r["ceiling"],
                    }
        else:
            targets = changes
            if profile:
                if expires is not None:
                    if st.profile_previous is None:
                        st.profile_previous, st.profile_previous_restricts = st.profile, st.profile_restricts
                else:
                    st.profile_previous = None
                st.profile, st.profile_expires = profile, expires
                st.profile_reason, st.profile_principal, st.profile_set_at = reason, principal, now
                dflt = self.profiles[self.default_profile]
                st.profile_restricts = any(
                    tighter(k, getattr(self.profiles[profile], k), getattr(dflt, k)) for k in PARAMS
                )

        for k, v in targets.items():
            st.overrides = [o for o in st.overrides if not (o.principal == principal and o.param == k)]
            restricts = principal != AGENT and (
                tighter(k, v, before[k]) or tighter(k, v, getattr(self.profiles[st.profile], k))
            )
            st.overrides.append(
                Override(
                    f"o{st.next_id}", principal, k, v, reason, now, expires, before[k], project, restricts
                )
            )
            st.next_id += 1
        self._commit(before, force=principal != AGENT and bool(profile))
        after = self.effective()
        self._event("set", principal, "accepted", reason, before, after)
        return {
            "ok": True,
            "accepted": True,
            "principal": principal,
            "profile": st.profile,
            "expires_at": iso(expires),
            "ttl_s": None if expires is None else round(expires - now),
            "ttl_capped": capped,
            "changed": {k: {"before": before[k], "after": after[k]} for k in after if before[k] != after[k]},
            "effective": after,
        }

    def ceiling_set(
        self,
        profile: str,
        reason: str,
        ttl_s: float | None = None,
        until: float | None = None,
        principal: str = OWNER,
    ) -> dict:
        if profile not in self.profiles:
            raise ValueError(f"unknown profile {profile!r}")
        before, now = self.effective(), self.clock()
        expires, _ = self._expiry(principal, ttl_s, until)
        self.state.ceiling = Ceiling(profile, reason, principal, now, expires)
        self._commit(before)
        after = self.effective()
        self._event("ceiling_set", principal, "accepted", reason, before, after, request={"profile": profile})
        return {
            "ok": True,
            "accepted": True,
            "ceiling": profile,
            "expires_at": iso(expires),
            "effective": after,
        }

    def ceiling_clear(self, principal: str = OWNER, reason: str = "") -> dict:
        before = self.effective()
        had = self.state.ceiling is not None
        self.state.ceiling = None
        self._commit(before)
        after = self.effective()
        if had:
            self._event("ceiling_clear", principal, "accepted", reason, before, after)
        return {"ok": True, "accepted": True, "cleared": had, "effective": after}

    def reset(
        self, reason: str, params: list[str] | None = None, all_: bool = False, principal: str = OWNER
    ) -> dict:
        """Remove owner overrides for `params`, or every override (owner and agent) with `all_`."""
        params = list(params or [])
        for k in params:
            if k not in PARAMS:
                raise ValueError(f"unknown parameter {k!r}")
        if not params and not all_:
            raise ValueError("give params or all")
        before, st = self.effective(), self.state
        gone = [o for o in st.overrides if all_ or (o.principal != AGENT and o.param in params)]
        st.overrides = [o for o in st.overrides if o not in gone]
        self._commit(before)
        after = self.effective()
        self._event(
            "reset", principal, "accepted", reason, before, after,
            request={"params": params, "all": all_, "removed": [o.id for o in gone]},
        )  # fmt: skip
        return {"ok": True, "accepted": True, "removed": [o.id for o in gone], "effective": after}

    def set_flag(self, kind: str, reason: str | None, principal: str = OWNER) -> None:
        """Owner hold/freeze (reason None clears it)."""
        if kind not in ("hold", "freeze"):
            raise ValueError(kind)
        before = self.effective()
        setattr(self.state, kind, None if reason is None else Flag(reason, principal, self.clock()))
        self._commit(before)

    def tick(self) -> bool:
        """Drop expired overrides, ceiling and temporary profile. True when anything changed."""
        now, st, changed, restored = self.clock(), self.state, False, False
        start = self.effective()
        for o in [o for o in st.overrides if o.expires is not None and o.expires <= now]:
            before = self.effective()
            st.overrides.remove(o)
            self._event(
                "set", o.principal, "expired", o.reason, before, self.effective(), request={"override": o.id}
            )
            changed = True
        if st.ceiling and st.ceiling.expires is not None and st.ceiling.expires <= now:
            before, c = self.effective(), st.ceiling
            st.ceiling = None
            self._event("ceiling_set", c.principal, "expired", c.reason, before, self.effective())
            changed = True
        if st.profile_expires is not None and st.profile_expires <= now:
            before = self.effective()
            if st.profile_previous:
                st.profile, st.profile_restricts = st.profile_previous, st.profile_previous_restricts
            st.profile_previous, st.profile_expires, st.profile_previous_restricts = None, None, False
            st.profile_reason = st.profile_principal = st.profile_set_at = None
            self._event("set", OWNER, "expired", "temporary profile", before, self.effective())
            changed = restored = True
        if changed:  # an expiry can leave a restriction active; a restored profile moves the baseline
            self._commit(start, force=restored)
        return changed
