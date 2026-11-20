"""Source-country determination under pending-amount apportionment.

Each pending amount may be apportioned to any one of its candidate countries
(in whole cents).  A country that ranks strictly first under *every* feasible
apportionment is unique if it exists, and it exists iff

    low(c) > high(d)   for every country d != c

where low(c)  = confirmed cents of c + pending amounts whose only candidate is c
      high(d) = confirmed cents of d + every pending amount listing d

The worst case for the pair (c, d) -- c takes only what it must while d takes
everything it may -- is reached by one single apportionment, so the condition
is necessary and sufficient.

When no country qualifies, the evidence returned is either one apportionment
with a tied first place or two apportionments with different leaders.  Take
c* = argmax low and d* = argmax high over d != c* and build the vertex
apportionment V1 that minimises total(c*) - total(d*): every pending amount
listing d* goes to d*, amounts forced to c* go to c*, anything else goes to a
non-c* candidate.  "Undetermined" guarantees total(c*) <= total(d*) in V1, so
c* does not lead V1: if V1 ties at the top it is the tie evidence; otherwise
its unique leader e != c* is not a determined leader either, so repeating the
construction for (e, argmax high) yields V2 where e does not lead -- V2 either
ties at the top or has a unique leader f != e, and the pair (V1, V2) is the
different-leaders evidence.
"""
from __future__ import annotations


def determine_year(confirmed_totals, pending):
    """Determine the country that always ranks first for one year.

    ``confirmed_totals`` maps country -> confirmed cents; ``pending`` is a
    list of {"code", "amount_cents", "candidates"} items.  Returns a dict
    with status "determined" (leader plus its worst-case margin) or
    "undetermined" (evidence apportionments for analysts).
    """
    countries = sorted(
        set(confirmed_totals) | {c for item in pending for c in item["candidates"]}
    )
    low = {c: confirmed_totals.get(c, 0) for c in countries}
    high = dict(low)
    for item in pending:
        amount = item["amount_cents"]
        candidates = item["candidates"]
        if len(candidates) == 1:
            low[candidates[0]] += amount
        for country in candidates:
            high[country] += amount

    for country in countries:
        if all(low[country] > high[other] for other in countries if other != country):
            runner_up = max(
                (high[other] for other in countries if other != country), default=0
            )
            return {
                "status": "determined",
                "leader": country,
                "leader_min_cents": low[country],
                "runner_up_max_cents": runner_up,
            }
    return {
        "status": "undetermined",
        "evidence": _evidence(confirmed_totals, pending, countries, low, high),
    }


def _apportionment(confirmed_totals, pending, countries, focus_low, focus_high):
    """Vertex apportionment minimising total(focus_low) - total(focus_high)."""
    totals = {c: confirmed_totals.get(c, 0) for c in countries}
    assignments = []
    for item in pending:
        candidates = item["candidates"]
        amount = item["amount_cents"]
        if focus_high in candidates:
            chosen = focus_high
        elif candidates == [focus_low]:
            chosen = focus_low
        elif focus_low in candidates:
            chosen = next(c for c in candidates if c != focus_low)
        else:
            chosen = candidates[0]
        totals[chosen] = totals.get(chosen, 0) + amount
        assignments.append(
            {
                "code": item["code"],
                "amount_cents": amount,
                "candidates": list(candidates),
                "assigned": chosen,
            }
        )
    return {"totals": totals, "leaders": _leaders(totals), "assignments": assignments}


def _leaders(totals):
    top = max(totals.values())
    return sorted(c for c, v in totals.items() if v == top)


def _evidence(confirmed_totals, pending, countries, low, high):
    first = max(countries, key=lambda c: low[c])
    rival = max((d for d in countries if d != first), key=lambda d: high[d])
    v1 = _apportionment(confirmed_totals, pending, countries, first, rival)
    if len(v1["leaders"]) > 1:
        return {"type": "tie", "apportionments": [v1]}
    leader1 = v1["leaders"][0]  # != first, since total(first) <= total(rival)
    rival2 = max((d for d in countries if d != leader1), key=lambda d: high[d])
    v2 = _apportionment(confirmed_totals, pending, countries, leader1, rival2)
    if len(v2["leaders"]) > 1:
        return {"type": "tie", "apportionments": [v2]}
    # leader1 does not lead v2 either, so the two leaders differ.
    return {"type": "different_leaders", "apportionments": [v1, v2]}


def build_origin_result(snapshot):
    """Assemble the per-year determination from one consistent snapshot."""
    group = snapshot["group"]
    years = snapshot["years"]
    results = []
    for year in years:
        codes = snapshot["members"][year]
        total = (
            group["old_total_cents"] if year == years[0] else group["new_total_cents"]
        )
        entry = {"year": year, "codes": codes, "group_total_cents": total}
        if total == 0:
            results.append({**entry, "status": "no_imports"})
            continue
        missing = [
            {"code": code, "amount_cents": snapshot["amounts"][(year, code)]}
            for code in codes
            if snapshot["amounts"][(year, code)] > 0
            and (year, code) not in snapshot["details"]
        ]
        if missing:
            results.append({**entry, "status": "incomplete", "missing": missing})
            continue
        confirmed_totals: dict[str, int] = {}
        pending = []
        for code in codes:
            detail = snapshot["details"].get((year, code))
            if detail is None:
                continue  # zero-amount code without details: nothing to apportion
            for country, cents in detail["confirmed"].items():
                confirmed_totals[country] = confirmed_totals.get(country, 0) + cents
            for item in detail["pending"]:
                pending.append(
                    {
                        "code": code,
                        "amount_cents": item["amount_cents"],
                        "candidates": item["candidates"],
                    }
                )
        results.append({**entry, **determine_year(confirmed_totals, pending)})
    return {
        "group_id": group["group_id"],
        "version": snapshot["version"],
        "years": years,
        "results": results,
    }
