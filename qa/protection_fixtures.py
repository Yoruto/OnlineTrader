"""G0 proposed development fixtures; excluded from the production wheel.
No reference software, broker account or credentials are loaded at runtime.
"""

import json

SOURCE_SPEC_SHA256 = "093e35ce223113a1d1d514fd57a5decfc1de5272a1ba0ae1ca990aa1a1646a70"

BASE_PLAN = json.loads(r'''{
  "schema_version": "1.0",
  "plan_id": "plan-dev-001",
  "plan_revision": "1",
  "account_key": "DEV:fixture:account-1",
  "position_cycle_id": "cycle-dev-001",
  "contract_id": "DEV.TEST001",
  "trading_day": "2026-11-09",
  "direction": "LONG",
  "theoretical_entry_price": "1000",
  "initial_open_quantity": 5,
  "rule_set_id": "rules-proposed-fixture-1",
  "config_revision": "1",
  "config_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "execution_scope": "DEVELOPMENT",
  "approval_record_id": null,
  "market_quality_policy_id": "quality-fixture-1",
  "price_field": "LAST",
  "fixed_stop_loss": {
    "enabled": true,
    "distance_points": "20"
  },
  "fixed_take_profit": {
    "enabled": true,
    "distance_points": "100"
  },
  "profit_protection": {
    "enabled": true,
    "activation_ratio": "0.8",
    "floor_ratio": "0.5",
    "block_new_for_trading_day": true
  },
  "trailing_stop": {
    "enabled": false
  },
  "partial_reduction": {
    "enabled": true,
    "first_trigger_ratio": "0.4",
    "quantity_rule": "CEIL_Q0_DIV_2_AND_4",
    "q0_freeze_policy": "ENTRY_TERMINAL_AND_RECONCILED",
    "second_stage": {
      "enabled": true,
      "trigger_ratio": "0.6"
    },
    "break_even": {
      "enabled": true,
      "anchor": "THEORETICAL_ENTRY",
      "activate_after": "FIRST_STAGE_CONFIRMED_REDUCTION"
    }
  },
  "close_session": {
    "enabled": true,
    "schedule_policy_id": "schedule-fixture-1",
    "calendar_version": "calendar-fixture-1",
    "advance_seconds": 20
  },
  "slippage_exit": {
    "enabled": false
  },
  "data_gap_exit": {
    "enabled": false
  },
  "emergency_exit": {
    "enabled": false
  },
  "execution_policy": {
    "policy_id": "exit-fixture-1",
    "first_price_mode": "OPPONENT_LIMIT",
    "cancel_check_interval_ms": 5000,
    "max_child_order_attempts": 3,
    "price_steps": [
      {
        "after_check_count": 1,
        "price_mode": "OFFSET_TICKS",
        "offset_ticks": 1
      },
      {
        "after_check_count": 2,
        "price_mode": "EXCHANGE_LIMIT"
      }
    ],
    "close_offset_policy_id": "offset-fixture-1"
  }
}''')

GOLDEN_CASES = json.loads(r'''[
  {
    "case_id": "G0-PP-001",
    "overrides": {
      "partial_reduction": {
        "enabled": false
      }
    },
    "events": [
      {
        "type": "FILL",
        "quantity": 1,
        "price": "1000"
      },
      {
        "type": "QUOTE",
        "price": "1080"
      },
      {
        "type": "QUOTE",
        "price": "1060"
      },
      {
        "type": "QUOTE",
        "price": "1050"
      }
    ],
    "expect": {
      "mode": "PROTECTED",
      "max_favorable_move": "80",
      "target_remaining_qty": 0,
      "block_new_for_trading_day": true
    }
  },
  {
    "case_id": "G0-PP-002",
    "overrides": {
      "direction": "SHORT",
      "partial_reduction": {
        "enabled": false
      }
    },
    "events": [
      {
        "type": "FILL",
        "quantity": 1,
        "price": "1000"
      },
      {
        "type": "QUOTE",
        "price": "920"
      },
      {
        "type": "QUOTE",
        "price": "950"
      }
    ],
    "expect": {
      "mode": "PROTECTED",
      "target_remaining_qty": 0
    }
  },
  {
    "case_id": "G0-PP-003",
    "overrides": {
      "initial_open_quantity": 1
    },
    "events": [
      {
        "type": "FILL",
        "quantity": 1,
        "price": "1000"
      },
      {
        "type": "ENTRY_TERMINAL_RECONCILED",
        "owned_quantity": 1
      },
      {
        "type": "QUOTE",
        "price": "1040"
      },
      {
        "type": "QUOTE",
        "price": "1060"
      }
    ],
    "expect": {
      "target_remaining_qty": 1,
      "exit_submit_count": 0,
      "phase1_status": "NO_OP",
      "phase2_status": "NO_OP"
    }
  },
  {
    "case_id": "G0-PP-004",
    "overrides": {
      "initial_open_quantity": 2
    },
    "events": [
      {
        "type": "FILL",
        "quantity": 2,
        "price": "1000"
      },
      {
        "type": "ENTRY_TERMINAL_RECONCILED",
        "owned_quantity": 2
      },
      {
        "type": "QUOTE",
        "price": "1040"
      },
      {
        "type": "EXIT_FILL",
        "quantity": 1,
        "price": "1040"
      },
      {
        "type": "QUOTE",
        "price": "1060"
      }
    ],
    "expect": {
      "phase1_target": 1,
      "phase2_target": 1,
      "additional_phase2_exit_quantity": 0,
      "break_even_status": "ACTIVE"
    }
  },
  {
    "case_id": "G0-PP-005",
    "overrides": {
      "initial_open_quantity": 3
    },
    "events": [
      {
        "type": "FILL",
        "quantity": 3,
        "price": "1000"
      },
      {
        "type": "ENTRY_TERMINAL_RECONCILED",
        "owned_quantity": 3
      },
      {
        "type": "QUOTE",
        "price": "1040"
      },
      {
        "type": "EXIT_FILL",
        "quantity": 1,
        "price": "1040"
      },
      {
        "type": "QUOTE",
        "price": "1060"
      }
    ],
    "expect": {
      "phase1_target": 2,
      "phase2_target": 1,
      "additional_phase2_exit_quantity": 1
    }
  },
  {
    "case_id": "G0-PP-006",
    "overrides": {},
    "events": [
      {
        "type": "FILL",
        "quantity": 5,
        "price": "1000"
      },
      {
        "type": "ENTRY_TERMINAL_RECONCILED",
        "owned_quantity": 5
      },
      {
        "type": "QUOTE",
        "price": "1040"
      },
      {
        "type": "EXIT_FILL",
        "quantity": 2,
        "price": "1040"
      },
      {
        "type": "QUOTE",
        "price": "1060"
      }
    ],
    "expect": {
      "phase1_target": 3,
      "phase2_target": 2,
      "additional_phase2_exit_quantity": 1
    }
  },
  {
    "case_id": "G0-PP-007",
    "overrides": {},
    "events": [
      {
        "type": "FILL",
        "quantity": 5,
        "price": "1000"
      },
      {
        "type": "ENTRY_TERMINAL_RECONCILED",
        "owned_quantity": 5
      },
      {
        "type": "QUOTE",
        "price": "1030"
      },
      {
        "type": "QUOTE",
        "price": "1085"
      }
    ],
    "expect": {
      "mode": "PROTECTED",
      "half_exit_submit_count": 0,
      "phase1_status": "SUPPRESSED_PROTECTED",
      "phase2_status": "SUPPRESSED_PROTECTED"
    }
  },
  {
    "case_id": "G0-PP-008",
    "overrides": {},
    "events": [
      {
        "type": "FILL",
        "quantity": 5,
        "price": "1000"
      },
      {
        "type": "ENTRY_TERMINAL_RECONCILED",
        "owned_quantity": 5
      },
      {
        "type": "QUOTE",
        "price": "1100"
      },
      {
        "type": "SESSION_CLOSE_DUE"
      }
    ],
    "expect": {
      "mode": "PROTECTED",
      "target_remaining_qty": 0,
      "half_exit_submit_count": 0,
      "independent_full_exit_targets": 1
    }
  },
  {
    "case_id": "G0-PP-009",
    "overrides": {
      "partial_reduction": {
        "enabled": false
      }
    },
    "events": [
      {
        "type": "FILL",
        "quantity": 1,
        "price": "1000"
      },
      {
        "type": "QUOTE",
        "price": "1080"
      },
      {
        "type": "COMMIT_ACK"
      },
      {
        "type": "KILL_STRATEGY"
      },
      {
        "type": "RESTART_GUI"
      },
      {
        "type": "QUOTE",
        "price": "1050"
      }
    ],
    "expect": {
      "mode": "PROTECTED",
      "target_remaining_qty": 0,
      "plan_identity_unchanged": true
    }
  },
  {
    "case_id": "G0-PP-010",
    "overrides": {
      "profit_protection": {
        "enabled": false
      },
      "partial_reduction": {
        "enabled": false
      },
      "trailing_stop": {
        "enabled": true,
        "activation_ratio": "0.6",
        "drawdown_mode": "POINTS",
        "drawdown_value": "20"
      }
    },
    "events": [
      {
        "type": "FILL",
        "quantity": 1,
        "price": "1000"
      },
      {
        "type": "QUOTE",
        "price": "1060"
      },
      {
        "type": "QUOTE",
        "price": "1090"
      },
      {
        "type": "QUOTE",
        "price": "1070"
      }
    ],
    "expect": {
      "mode": "NORMAL",
      "trailing_floor_move": "70",
      "target_remaining_qty": 0
    }
  },
  {
    "case_id": "G0-PP-011",
    "overrides": {
      "partial_reduction": {
        "enabled": false
      }
    },
    "events": [
      {
        "type": "FILL",
        "quantity": 1,
        "price": "1000"
      },
      {
        "type": "QUOTE",
        "price": "1080"
      },
      {
        "type": "KILL_KERNEL_BEFORE_COMMIT_ACK"
      },
      {
        "type": "RESTART_KERNEL_WITH_GAP"
      }
    ],
    "expect": {
      "new_entry_allowed": false,
      "missing_latch_must_not_be_claimed_durable": true,
      "reconciliation_required": true
    }
  },
  {
    "case_id": "G0-PP-012",
    "overrides": {
      "partial_reduction": {
        "enabled": false
      }
    },
    "events": [
      {
        "type": "FILL",
        "quantity": 1,
        "price": "1000"
      },
      {
        "type": "QUOTE",
        "price": "1080"
      },
      {
        "type": "QUOTE",
        "price": "1050"
      },
      {
        "type": "CANCEL_ENTRY_REQUEST"
      },
      {
        "type": "LATE_ENTRY_FILL",
        "quantity": 2,
        "price": "1000"
      }
    ],
    "expect": {
      "target_remaining_qty": 0,
      "owned_remaining_after_new_fill": 3,
      "all_remaining_covered_by_original_plan": true,
      "cancel_request_does_not_release_unknown": true
    }
  }
]''')
