

Prior-session holdings are excluded from automatic exit management, including
engine-originated holdings. Entry dates are compared in America/New_York,
falling back to entry history when local entry metadata is absent. Restarting
does not re-arm their stops, run software stop/trailing/breakeven/EOD exits,
or accept pending automated sell approvals for them. Existing broker orders
are left unchanged and can still execute independently. Same-day positions
continue to use their configured exit controls.
