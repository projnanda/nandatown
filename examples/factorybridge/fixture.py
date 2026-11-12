"""Public synthetic order; not a benchmark or held-out case."""
ORDER = {
    "order_id": "fixture-integration-only", "deadline_day": 8,
    "budget_usd": 100.0, "assembly_days": 1,
    "parts": [
        {"part_id": "fixture-spacer", "kind": "print", "qty": 2,
         "material": "PLA", "volume_cm3": 1, "max_dim_mm": 10, "tolerance_mm": 0.2},
        {"part_id": "fixture-board", "kind": "pcb", "qty": 1,
         "layers": 2, "size_mm": [10, 10]},
        {"part_id": "fixture-screw", "kind": "purchased", "qty": 2, "sku": "fixture-screw"},
    ],
    "injected_events": [{"day": 3, "supplier_kind": "print",
                         "type": "delay", "days": 2}],
}
