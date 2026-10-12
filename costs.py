"""Storage cost model. Prices are approximate USD and meant to be edited."""

GB = 1024 ** 3
TB = 1024 ** 4

# (label, capacity in GB, USD per month) - Google One style tiers; verify current prices.
CLOUD_PLANS = [
    ("Free 15 GB", 15, 0.0),
    ("100 GB", 100, 1.99),
    ("200 GB", 200, 2.99),
    ("2 TB", 2048, 9.99),
    ("5 TB", 5120, 24.99),
]

# Approximate street prices for external drives (USD, one-off).
HDD_OPTIONS = [
    ("1 TB external HDD", 1, 45.0),
    ("2 TB external HDD", 2, 65.0),
    ("4 TB external HDD", 4, 105.0),
]

HDD_LIFESPAN_MONTHS = 60


def cheapest_plan(used_bytes):
    for label, cap_gb, price in CLOUD_PLANS:
        if used_bytes <= cap_gb * GB:
            return {"label": label, "capacityGb": cap_gb, "monthly": price}
    label, cap_gb, price = CLOUD_PLANS[-1]
    return {"label": label, "capacityGb": cap_gb, "monthly": price, "overCapacity": True}


def advise(used_bytes, archive_bytes):
    """Compare staying fully on cloud vs archiving `archive_bytes` to hard disks."""
    now = cheapest_plan(used_bytes)
    after_bytes = max(used_bytes - archive_bytes, 0)
    after = cheapest_plan(after_bytes)

    need_tb = archive_bytes / TB
    # Two disks: one working copy and one backup (3-2-1 rule, minus offsite).
    disk = next((h for h in HDD_OPTIONS if h[1] >= need_tb), HDD_OPTIONS[-1])
    disks_needed = max(1, -(-need_tb // disk[1])) if need_tb else 0
    hdd_cost = disk[2] * int(disks_needed) * 2

    monthly_saving = round(now["monthly"] - after["monthly"], 2)
    break_even = round(hdd_cost / monthly_saving, 1) if monthly_saving > 0 else None
    five_year_cloud = now["monthly"] * HDD_LIFESPAN_MONTHS
    five_year_hybrid = after["monthly"] * HDD_LIFESPAN_MONTHS + hdd_cost

    return {
        "currentPlan": now,
        "planAfterArchive": after,
        "hdd": {"label": disk[0], "count": int(disks_needed) * 2, "oneOffCost": hdd_cost},
        "monthlySaving": monthly_saving,
        "breakEvenMonths": break_even,
        "fiveYearCloud": round(five_year_cloud, 2),
        "fiveYearHybrid": round(five_year_hybrid, 2),
        "worthIt": five_year_hybrid < five_year_cloud,
        "note": "Disks fail. Keep two copies (the advice above already budgets two) and check them yearly.",
    }
