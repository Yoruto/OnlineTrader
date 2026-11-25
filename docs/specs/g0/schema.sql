-- OnlineTrader G0 ledger schema v1. Specification artifact; not a product database.
-- One file contains exactly one environment/broker/account scope.
-- Every connection MUST enable and read back foreign_keys; file-backed writer also
-- sets and reads back journal_mode=WAL and synchronous=FULL before accepting work.
-- Decimal TEXT is parsed/compared with Decimal by the owner, never SQL REAL/CAST.
PRAGMA foreign_keys = ON;
BEGIN;

CREATE TABLE ledger_scope (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    account_key TEXT NOT NULL UNIQUE,
    environment TEXT NOT NULL CHECK (environment IN ('ONLINE_SIM','LIVE')),
    broker_identity TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at_utc TEXT NOT NULL
) STRICT;

CREATE TABLE kernel_boots (
    kernel_boot_id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL REFERENCES ledger_scope(account_key),
    kernel_epoch INTEGER NOT NULL UNIQUE CHECK (kernel_epoch > 0),
    executable_hash TEXT NOT NULL CHECK (length(executable_hash) = 64),
    started_at_utc TEXT NOT NULL,
    stopped_at_utc TEXT,
    UNIQUE (kernel_boot_id, kernel_epoch)
) STRICT;

CREATE TABLE event_journal (
    event_seq INTEGER PRIMARY KEY CHECK (event_seq > 0),
    tx_id TEXT NOT NULL UNIQUE,
    tx_kind TEXT NOT NULL CHECK (tx_kind IN
      ('TX_AUTH','TX_INTENT','TX_FILL','TX_PROTECT','TX_SWITCH','TX_TAKEOVER',
       'TX_MAINTENANCE','TX_RECONCILE','TX_CANCEL','TX_ORDER','TX_CONFIG',
       'TX_OBSERVATION','TX_COUNTER','TX_OUTBOX','TX_BOOT')),
    kernel_boot_id TEXT NOT NULL REFERENCES kernel_boots(kernel_boot_id),
    ingress_seq INTEGER CHECK (ingress_seq IS NULL OR ingress_seq >= 0),
    gateway_identity TEXT,
    source_epoch INTEGER CHECK (source_epoch IS NULL OR source_epoch >= 0),
    source_key TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
    observed_at_utc TEXT NOT NULL,
    committed_at_utc TEXT NOT NULL
) STRICT;
CREATE UNIQUE INDEX journal_source_dedup
    ON event_journal(gateway_identity, source_key) WHERE source_key IS NOT NULL;
CREATE TRIGGER journal_no_update BEFORE UPDATE ON event_journal BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER journal_no_delete BEFORE DELETE ON event_journal BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;

CREATE TABLE rule_sets (
    rule_set_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
    created_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq)
) STRICT;
CREATE TABLE rule_decisions (
    rule_set_id TEXT NOT NULL REFERENCES rule_sets(rule_set_id),
    b_code TEXT NOT NULL CHECK (b_code IN ('B01','B02','B03','B04','B05','B06','B07','B08','B09','B10',
      'B11','B12','B13','B14','B15','B16','B17','B18','B19','B20')),
    decision_revision INTEGER NOT NULL CHECK (decision_revision > 0),
    status TEXT NOT NULL CHECK (status IN ('PROPOSED','APPROVED','REVOKED')),
    final_rule_json TEXT NOT NULL CHECK (json_valid(final_rule_json)),
    approver_reference TEXT,
    evidence_hash TEXT CHECK (evidence_hash IS NULL OR length(evidence_hash) = 64),
    effective_scope_json TEXT NOT NULL CHECK (json_valid(effective_scope_json)),
    recorded_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    CHECK (status != 'APPROVED' OR (approver_reference IS NOT NULL AND evidence_hash IS NOT NULL)),
    PRIMARY KEY (rule_set_id, b_code, decision_revision)
) STRICT;
CREATE TABLE config_revisions (
    config_revision INTEGER PRIMARY KEY CHECK (config_revision > 0),
    rule_set_id TEXT NOT NULL REFERENCES rule_sets(rule_set_id),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
    effective_boundary TEXT NOT NULL CHECK (effective_boundary IN ('NEXT_CYCLE','EXPLICIT_PLAN_MIGRATION','NON_TRADING')),
    created_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    UNIQUE (config_revision, rule_set_id)
) STRICT;
CREATE TABLE approval_manifests (
    approval_record_id TEXT PRIMARY KEY,
    rule_set_id TEXT NOT NULL REFERENCES rule_sets(rule_set_id),
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
    manifest_hash TEXT NOT NULL CHECK (length(manifest_hash) = 64),
    recorded_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    UNIQUE (approval_record_id, rule_set_id)
) STRICT;
CREATE TABLE approval_manifest_items (
    approval_record_id TEXT NOT NULL,
    rule_set_id TEXT NOT NULL,
    b_code TEXT NOT NULL,
    decision_revision INTEGER NOT NULL,
    FOREIGN KEY (approval_record_id, rule_set_id) REFERENCES approval_manifests(approval_record_id, rule_set_id),
    FOREIGN KEY (rule_set_id, b_code, decision_revision) REFERENCES rule_decisions(rule_set_id, b_code, decision_revision),
    PRIMARY KEY (approval_record_id, b_code)
) STRICT;
CREATE TABLE policy_revisions (
    policy_id TEXT PRIMARY KEY,
    policy_kind TEXT NOT NULL CHECK (policy_kind IN ('MARKET_QUALITY','CALENDAR','EXECUTION','EMERGENCY','AUTHORITY','RESUME','COUNTERS')),
    rule_set_id TEXT NOT NULL REFERENCES rule_sets(rule_set_id),
    policy_json TEXT NOT NULL CHECK (json_valid(policy_json)),
    policy_hash TEXT NOT NULL CHECK (length(policy_hash) = 64),
    recorded_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq)
) STRICT;

CREATE TABLE strategy_instances (
    strategy_epoch INTEGER PRIMARY KEY CHECK (strategy_epoch > 0),
    instance_id TEXT NOT NULL UNIQUE,
    executable_hash TEXT NOT NULL CHECK (length(executable_hash) = 64),
    status TEXT NOT NULL CHECK (status IN ('SHADOW','ACTIVE','DRAINING','REVOKED')),
    rule_set_id TEXT NOT NULL REFERENCES rule_sets(rule_set_id),
    config_revision INTEGER NOT NULL REFERENCES config_revisions(config_revision),
    barrier_event_seq INTEGER REFERENCES event_journal(event_seq),
    state_revision INTEGER NOT NULL CHECK (state_revision > 0),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    FOREIGN KEY (config_revision, rule_set_id) REFERENCES config_revisions(config_revision, rule_set_id)
) STRICT;
CREATE UNIQUE INDEX one_active_strategy ON strategy_instances(status) WHERE status = 'ACTIVE';
CREATE TABLE authority (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    kernel_boot_id TEXT NOT NULL REFERENCES kernel_boots(kernel_boot_id),
    kernel_epoch INTEGER NOT NULL CHECK (kernel_epoch > 0),
    strategy_epoch INTEGER REFERENCES strategy_instances(strategy_epoch),
    new_open_status TEXT NOT NULL CHECK (new_open_status IN ('DISARMED','ARMED')),
    grant_id TEXT,
    grant_policy_json TEXT NOT NULL CHECK (json_valid(grant_policy_json)),
    config_revision INTEGER NOT NULL REFERENCES config_revisions(config_revision),
    state_revision INTEGER NOT NULL CHECK (state_revision > 0),
    risk_revision INTEGER NOT NULL CHECK (risk_revision > 0),
    authority_revision INTEGER NOT NULL CHECK (authority_revision > 0),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    FOREIGN KEY (kernel_boot_id, kernel_epoch) REFERENCES kernel_boots(kernel_boot_id, kernel_epoch),
    CHECK (new_open_status != 'ARMED' OR grant_id IS NOT NULL)
) STRICT;

CREATE TABLE position_cycles (
    position_cycle_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('LONG','SHORT')),
    trading_day TEXT NOT NULL CHECK (length(trading_day) = 10),
    ownership TEXT NOT NULL CHECK (ownership IN ('MANAGED','EXTERNAL_PENDING','TAKEN_OVER')),
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('ENTRY_PENDING','HOLDING','EXIT_PENDING','FLAT_PENDING_CONFIRM','CLOSED')),
    confirmed_open_qty INTEGER NOT NULL CHECK (confirmed_open_qty >= 0),
    confirmed_close_qty INTEGER NOT NULL CHECK (confirmed_close_qty >= 0),
    allocation_adjustment_qty INTEGER NOT NULL,
    remaining_qty INTEGER NOT NULL CHECK (remaining_qty >= 0),
    actual_average_entry_price TEXT,
    state_revision INTEGER NOT NULL CHECK (state_revision > 0),
    created_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    CHECK (remaining_qty = confirmed_open_qty - confirmed_close_qty + allocation_adjustment_qty)
) STRICT;
CREATE TABLE protection_plans (
    plan_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL CHECK (plan_revision > 0),
    position_cycle_id TEXT NOT NULL REFERENCES position_cycles(position_cycle_id),
    rule_set_id TEXT NOT NULL REFERENCES rule_sets(rule_set_id),
    config_revision INTEGER NOT NULL REFERENCES config_revisions(config_revision),
    plan_json TEXT NOT NULL CHECK (json_valid(plan_json) AND json_type(plan_json) = 'object'),
    plan_hash TEXT NOT NULL CHECK (length(plan_hash) = 64),
    migration_approval_reference TEXT,
    created_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    FOREIGN KEY (config_revision, rule_set_id) REFERENCES config_revisions(config_revision, rule_set_id),
    PRIMARY KEY (plan_id, plan_revision),
    UNIQUE (position_cycle_id, plan_id, plan_revision),
    UNIQUE (position_cycle_id, plan_revision)
) STRICT;

CREATE TABLE reconciliation_runs (
    reconciliation_id TEXT PRIMARY KEY,
    gateway_identity TEXT NOT NULL,
    source_epoch INTEGER NOT NULL CHECK (source_epoch > 0),
    trading_day TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('IN_PROGRESS','COMPLETE','FAILED','INCOMPLETE','INVALIDATED')),
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
    start_ingress_seq INTEGER NOT NULL CHECK (start_ingress_seq >= 0),
    barrier_event_seq INTEGER REFERENCES event_journal(event_seq),
    difference_count INTEGER NOT NULL CHECK (difference_count >= 0),
    created_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    completed_event_seq INTEGER REFERENCES event_journal(event_seq),
    CHECK (status != 'COMPLETE' OR (barrier_event_seq IS NOT NULL AND completed_event_seq IS NOT NULL))
) STRICT;
CREATE TABLE reconciliation_parts (
    reconciliation_id TEXT NOT NULL REFERENCES reconciliation_runs(reconciliation_id),
    part_kind TEXT NOT NULL CHECK (part_kind IN ('FUNDS','POSITIONS','ORDERS','TRADES')),
    status TEXT NOT NULL CHECK (status IN ('UNREQUESTED','REQUESTED','IN_PROGRESS','COMPLETE','INVALIDATED','FAILED','UNSUPPORTED')),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    completion_evidence_json TEXT CHECK (completion_evidence_json IS NULL OR json_valid(completion_evidence_json)),
    last_ingress_seq INTEGER CHECK (last_ingress_seq IS NULL OR last_ingress_seq >= 0),
    snapshot_json TEXT CHECK (snapshot_json IS NULL OR json_valid(snapshot_json)),
    CHECK (status != 'COMPLETE' OR completion_evidence_json IS NOT NULL),
    PRIMARY KEY (reconciliation_id, part_kind)
) STRICT;
CREATE TABLE reconciliation_differences (
    difference_id TEXT PRIMARY KEY,
    reconciliation_id TEXT NOT NULL REFERENCES reconciliation_runs(reconciliation_id),
    difference_kind TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    status TEXT NOT NULL CHECK (status IN ('OPEN','EXPLAINED','RESOLVED')),
    resolution_json TEXT CHECK (resolution_json IS NULL OR json_valid(resolution_json)),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq)
) STRICT;

CREATE TABLE protection_state (
    position_cycle_id TEXT PRIMARY KEY REFERENCES position_cycles(position_cycle_id),
    plan_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('NORMAL','PROTECTED')),
    max_favorable_move TEXT NOT NULL,
    protected_event_seq INTEGER REFERENCES event_journal(event_seq),
    quality TEXT NOT NULL CHECK (quality IN ('CONTIGUOUS','DATA_GAP','UNKNOWN')),
    phase1_status TEXT NOT NULL CHECK (phase1_status IN ('NOT_TRIGGERED','WAITING_ENTRY_TERMINAL','TARGET_COMMITTED','EXECUTING','DONE','NO_OP','SUPPRESSED_PROTECTED')),
    phase2_status TEXT NOT NULL CHECK (phase2_status IN ('NOT_TRIGGERED','WAITING_ENTRY_TERMINAL','TARGET_COMMITTED','EXECUTING','DONE','NO_OP','SUPPRESSED_PROTECTED')),
    q0 INTEGER CHECK (q0 IS NULL OR q0 > 0),
    q0_reconciliation_id TEXT REFERENCES reconciliation_runs(reconciliation_id),
    phase1_confirmed_reduction_qty INTEGER NOT NULL CHECK (phase1_confirmed_reduction_qty >= 0),
    phase2_confirmed_reduction_qty INTEGER NOT NULL CHECK (phase2_confirmed_reduction_qty >= 0),
    break_even_status TEXT NOT NULL CHECK (break_even_status IN ('OFF','WAITING_FILL','ACTIVE','SUPPRESSED_PROTECTED')),
    trailing_activated_event_seq INTEGER REFERENCES event_journal(event_seq),
    trailing_floor_move TEXT,
    last_observed_event_seq INTEGER REFERENCES event_journal(event_seq),
    target_remaining_qty INTEGER CHECK (target_remaining_qty IS NULL OR target_remaining_qty >= 0),
    state_revision INTEGER NOT NULL CHECK (state_revision > 0),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    FOREIGN KEY (position_cycle_id, plan_id, plan_revision)
      REFERENCES protection_plans(position_cycle_id, plan_id, plan_revision),
    CHECK (mode != 'PROTECTED' OR protected_event_seq IS NOT NULL),
    CHECK (q0 IS NULL OR q0_reconciliation_id IS NOT NULL)
) STRICT;
CREATE TRIGGER protected_cannot_clear BEFORE UPDATE OF mode ON protection_state
    WHEN OLD.mode = 'PROTECTED' AND NEW.mode != 'PROTECTED'
    BEGIN SELECT RAISE(ABORT,'PROTECTION_LATCH_CANNOT_CLEAR'); END;

CREATE TABLE order_intents (
    intent_id TEXT PRIMARY KEY,
    position_cycle_id TEXT NOT NULL REFERENCES position_cycles(position_cycle_id),
    decision_key TEXT,
    effect TEXT NOT NULL CHECK (effect IN ('OPEN','REDUCE')),
    origin TEXT NOT NULL CHECK (origin IN ('STRATEGY','MANUAL','PROTECTION','SESSION','EMERGENCY')),
    requested_qty INTEGER CHECK (requested_qty IS NULL OR requested_qty > 0),
    desired_remaining_qty INTEGER CHECK (desired_remaining_qty IS NULL OR desired_remaining_qty >= 0),
    status TEXT NOT NULL CHECK (status IN ('PREPARED','ACTIVE','UNKNOWN','DONE','ABORTED')),
    reason_codes_json TEXT NOT NULL CHECK (json_valid(reason_codes_json)),
    kernel_epoch INTEGER NOT NULL CHECK (kernel_epoch > 0),
    strategy_epoch INTEGER REFERENCES strategy_instances(strategy_epoch),
    state_revision INTEGER NOT NULL CHECK (state_revision > 0),
    created_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    CHECK ((effect = 'OPEN' AND decision_key IS NOT NULL AND requested_qty IS NOT NULL AND desired_remaining_qty IS NULL)
       OR (effect = 'REDUCE' AND desired_remaining_qty IS NOT NULL AND requested_qty IS NULL))
) STRICT;
CREATE UNIQUE INDEX one_entry_opportunity ON order_intents(decision_key) WHERE effect = 'OPEN';
CREATE TABLE child_orders (
    child_order_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    offset_kind TEXT NOT NULL CHECK (offset_kind IN ('OPEN','CLOSE','CLOSE_TODAY','CLOSE_YESTERDAY')),
    limit_price TEXT NOT NULL,
    original_qty INTEGER NOT NULL CHECK (original_qty > 0),
    confirmed_fill_qty INTEGER NOT NULL CHECK (confirmed_fill_qty >= 0),
    broker_state TEXT NOT NULL CHECK (broker_state IN ('NOT_SEEN','WORKING','PARTIALLY_FILLED','FILLED','CANCELLED','REJECTED')),
    terminal_evidence_json TEXT CHECK (terminal_evidence_json IS NULL OR json_valid(terminal_evidence_json)),
    state_revision INTEGER NOT NULL CHECK (state_revision > 0),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    UNIQUE (intent_id, ordinal),
    UNIQUE (child_order_id, intent_id),
    CHECK (confirmed_fill_qty <= original_qty)
) STRICT;
CREATE TABLE broker_order_links (
    broker_order_key TEXT PRIMARY KEY,
    child_order_id TEXT NOT NULL UNIQUE REFERENCES child_orders(child_order_id),
    gateway_identity TEXT NOT NULL,
    first_source_epoch INTEGER NOT NULL CHECK (first_source_epoch > 0),
    identity_evidence_json TEXT NOT NULL CHECK (json_valid(identity_evidence_json)),
    linked_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq)
) STRICT;
CREATE TABLE send_attempts (
    attempt_id TEXT PRIMARY KEY,
    child_order_id TEXT NOT NULL REFERENCES child_orders(child_order_id),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    send_state TEXT NOT NULL CHECK (send_state IN ('PREPARED','SEND_POSSIBLE','CALL_RETURNED','DEFINITELY_NOT_SENT','UNKNOWN','CONFIRMED')),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    kernel_boot_id TEXT NOT NULL REFERENCES kernel_boots(kernel_boot_id),
    call_result_json TEXT CHECK (call_result_json IS NULL OR json_valid(call_result_json)),
    prepared_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    UNIQUE (child_order_id, ordinal)
) STRICT;
CREATE TABLE cancel_actions (
    cancel_id TEXT PRIMARY KEY,
    child_order_id TEXT NOT NULL REFERENCES child_orders(child_order_id),
    status TEXT NOT NULL CHECK (status IN ('REQUESTED','SEND_POSSIBLE','CALL_RETURNED','CONFIRMED','REJECTED','UNKNOWN','NOT_NEEDED')),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    attempt_count INTEGER NOT NULL CHECK (attempt_count > 0),
    created_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq)
) STRICT;
CREATE TABLE fill_facts (
    fill_id TEXT PRIMARY KEY,
    trade_key TEXT NOT NULL UNIQUE,
    broker_order_key TEXT,
    gateway_identity TEXT NOT NULL,
    source_epoch INTEGER NOT NULL CHECK (source_epoch > 0),
    trading_day TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    offset_kind TEXT NOT NULL CHECK (offset_kind IN ('OPEN','CLOSE','CLOSE_TODAY','CLOSE_YESTERDAY')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price TEXT NOT NULL,
    raw_fact_json TEXT NOT NULL CHECK (json_valid(raw_fact_json)),
    fact_hash TEXT NOT NULL CHECK (length(fact_hash) = 64),
    recorded_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq)
) STRICT;
CREATE TRIGGER fill_no_update BEFORE UPDATE ON fill_facts BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER fill_no_delete BEFORE DELETE ON fill_facts BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TABLE fill_allocations (
    fill_id TEXT NOT NULL REFERENCES fill_facts(fill_id),
    position_cycle_id TEXT NOT NULL REFERENCES position_cycles(position_cycle_id),
    effect TEXT NOT NULL CHECK (effect IN ('OPEN','REDUCE')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    ownership_evidence_json TEXT NOT NULL CHECK (json_valid(ownership_evidence_json)),
    recorded_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    PRIMARY KEY (fill_id, position_cycle_id)
) STRICT;
CREATE TABLE fee_observations (
    fee_observation_id TEXT PRIMARY KEY,
    fill_id TEXT NOT NULL REFERENCES fill_facts(fill_id),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('BROKER','STATEMENT','ESTIMATE')),
    quality TEXT NOT NULL CHECK (quality IN ('CONFIRMED','ESTIMATED','UNKNOWN')),
    amount TEXT,
    currency TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    recorded_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    CHECK ((quality = 'UNKNOWN' AND amount IS NULL) OR (quality != 'UNKNOWN' AND amount IS NOT NULL)),
    UNIQUE (fill_id, source_kind, source_reference)
) STRICT;

CREATE TABLE reservations (
    reservation_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
    child_order_id TEXT REFERENCES child_orders(child_order_id),
    reservation_kind TEXT NOT NULL CHECK (reservation_kind IN ('RISK_BUDGET','MARGIN','CLOSE_TODAY','CLOSE_YESTERDAY','INSTRUMENT_SLOT','ORDER_COUNT')),
    amount TEXT,
    quantity INTEGER CHECK (quantity IS NULL OR quantity >= 0),
    status TEXT NOT NULL CHECK (status IN ('HELD','UNKNOWN','CONSUMED','RELEASED')),
    state_revision INTEGER NOT NULL CHECK (state_revision > 0),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    FOREIGN KEY (child_order_id, intent_id) REFERENCES child_orders(child_order_id, intent_id),
    CHECK ((amount IS NOT NULL AND quantity IS NULL) OR (amount IS NULL AND quantity IS NOT NULL))
) STRICT;
CREATE TABLE exit_targets (
    position_cycle_id TEXT PRIMARY KEY REFERENCES position_cycles(position_cycle_id),
    desired_remaining_qty INTEGER NOT NULL CHECK (desired_remaining_qty >= 0),
    reason_codes_json TEXT NOT NULL CHECK (json_valid(reason_codes_json)),
    state_revision INTEGER NOT NULL CHECK (state_revision > 0),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq)
) STRICT;
CREATE TABLE day_blocks (
    block_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    release_condition_json TEXT NOT NULL CHECK (json_valid(release_condition_json)),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','CLEARED')),
    created_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    cleared_event_seq INTEGER REFERENCES event_journal(event_seq),
    CHECK (status != 'CLEARED' OR cleared_event_seq IS NOT NULL)
) STRICT;
CREATE UNIQUE INDEX one_active_block ON day_blocks(scope_key,trading_day,reason_code) WHERE status = 'ACTIVE';
CREATE TABLE business_counters (
    scope_key TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    counter_name TEXT NOT NULL,
    integer_value INTEGER,
    decimal_value TEXT,
    quality TEXT NOT NULL CHECK (quality IN ('CONFIRMED','UNKNOWN')),
    state_revision INTEGER NOT NULL CHECK (state_revision > 0),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    CHECK ((integer_value IS NOT NULL AND decimal_value IS NULL) OR
           (integer_value IS NULL AND decimal_value IS NOT NULL) OR
           (quality = 'UNKNOWN' AND integer_value IS NULL AND decimal_value IS NULL)),
    PRIMARY KEY (scope_key,trading_day,counter_name)
) STRICT;
CREATE TABLE strategy_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    strategy_epoch INTEGER NOT NULL REFERENCES strategy_instances(strategy_epoch),
    event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    state_schema_version INTEGER NOT NULL CHECK (state_schema_version > 0),
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    state_hash TEXT NOT NULL CHECK (length(state_hash) = 64),
    observation_coverage_json TEXT NOT NULL CHECK (json_valid(observation_coverage_json))
) STRICT;
CREATE TABLE observation_windows (
    observation_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_epoch INTEGER NOT NULL CHECK (source_epoch > 0),
    observation_epoch INTEGER NOT NULL CHECK (observation_epoch > 0),
    quality TEXT NOT NULL CHECK (quality IN ('CONTIGUOUS','DATA_GAP','UNKNOWN')),
    observed_from_utc TEXT NOT NULL,
    observed_to_utc TEXT NOT NULL,
    path_state_json TEXT NOT NULL CHECK (json_valid(path_state_json)),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq)
) STRICT;

CREATE TABLE operator_commands (
    command_id TEXT PRIMARY KEY,
    context_id TEXT NOT NULL,
    authenticated_peer_reference TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    status TEXT NOT NULL CHECK (status IN ('RECEIVED','REJECTED','ACCEPTED_PENDING_COMMIT','COMMITTED','EXECUTING','SUCCEEDED','FAILED','NEEDS_RECONCILIATION','SUPERSEDED')),
    expected_kernel_boot_id TEXT NOT NULL,
    expected_state_revision INTEGER NOT NULL CHECK (expected_state_revision > 0),
    receipt_revision INTEGER NOT NULL CHECK (receipt_revision > 0),
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    created_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq)
) STRICT;
CREATE TABLE strategy_proposals (
    proposal_id TEXT PRIMARY KEY,
    proposal_kind TEXT NOT NULL CHECK (proposal_kind IN ('DECISION','STATE_UPDATE')),
    decision_key TEXT,
    strategy_epoch INTEGER NOT NULL REFERENCES strategy_instances(strategy_epoch),
    context_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    status TEXT NOT NULL CHECK (status IN ('REJECTED','ACCEPTED_PENDING_COMMIT','COMMITTED','NEEDS_RECONCILIATION')),
    intent_id TEXT REFERENCES order_intents(intent_id),
    receipt_json TEXT NOT NULL CHECK (json_valid(receipt_json)),
    recorded_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    CHECK ((proposal_kind = 'DECISION' AND decision_key IS NOT NULL) OR
           (proposal_kind = 'STATE_UPDATE' AND decision_key IS NULL AND intent_id IS NULL))
) STRICT;
CREATE TABLE takeover_records (
    takeover_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE REFERENCES operator_commands(command_id),
    status TEXT NOT NULL CHECK (status IN ('TAKEOVER_PENDING','TAKEN_OVER','CANCELLED','FAILED')),
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
    residual_risk_manifest_json TEXT NOT NULL CHECK (json_valid(residual_risk_manifest_json)),
    confirmed_by_reference TEXT,
    reconciliation_id TEXT REFERENCES reconciliation_runs(reconciliation_id),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    CHECK (status != 'TAKEN_OVER' OR (confirmed_by_reference IS NOT NULL AND reconciliation_id IS NOT NULL))
) STRICT;
CREATE TABLE update_transactions (
    update_id TEXT PRIMARY KEY,
    command_id TEXT UNIQUE REFERENCES operator_commands(command_id),
    update_level TEXT NOT NULL CHECK (update_level IN ('U1','U2','U3','U4','U5')),
    status TEXT NOT NULL CHECK (status IN ('PREPARING','SHADOW_READY','QUIESCING','COMMITTED','VERIFIED','ABORTED','BLOCKED','NEEDS_RECONCILIATION')),
    old_strategy_epoch INTEGER,
    new_strategy_epoch INTEGER,
    barrier_event_seq INTEGER REFERENCES event_journal(event_seq),
    manifest_hash TEXT NOT NULL CHECK (length(manifest_hash) = 64),
    maintenance_token TEXT,
    detail_json TEXT NOT NULL CHECK (json_valid(detail_json)),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq)
) STRICT;
CREATE TABLE maintenance_locks (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    maintenance_token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('REQUESTED','QUIESCING','SAFE_TO_STOP','STOP_COMMITTED','UPDATING','STARTUP_RECONCILE','COMPLETE','FAILED')),
    command_id TEXT NOT NULL REFERENCES operator_commands(command_id),
    reconciliation_id TEXT REFERENCES reconciliation_runs(reconciliation_id),
    barrier_event_seq INTEGER REFERENCES event_journal(event_seq),
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    CHECK (status != 'SAFE_TO_STOP' OR (reconciliation_id IS NOT NULL AND barrier_event_seq IS NOT NULL))
) STRICT;
CREATE TABLE alert_outbox (
    alert_id TEXT PRIMARY KEY,
    event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    channel TEXT NOT NULL CHECK (channel IN ('SOUND','DINGTALK','HEALTH_ENDPOINT')),
    severity TEXT NOT NULL CHECK (severity IN ('INFO','ATTENTION','TAKEOVER_REQUIRED')),
    fact_identity TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    delivery_status TEXT NOT NULL CHECK (delivery_status IN ('PENDING','IN_FLIGHT','DELIVERED','RETRY','FAILED')),
    recipient_ack_status TEXT NOT NULL CHECK (recipient_ack_status IN ('UNACKNOWLEDGED','ACKNOWLEDGED')),
    risk_status TEXT NOT NULL CHECK (risk_status IN ('OPEN','RESOLVED','NOT_APPLICABLE')),
    delivery_attempts INTEGER NOT NULL CHECK (delivery_attempts >= 0),
    delivery_lease_id TEXT,
    next_attempt_at_utc TEXT,
    updated_event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
    UNIQUE (fact_identity, channel)
) STRICT;
CREATE INDEX unresolved_intents ON order_intents(status) WHERE status IN ('PREPARED','ACTIVE','UNKNOWN');
CREATE INDEX uncertain_attempts ON send_attempts(send_state) WHERE send_state IN ('SEND_POSSIBLE','UNKNOWN');
CREATE INDEX held_reservations ON reservations(status,reservation_kind) WHERE status IN ('HELD','UNKNOWN');
CREATE INDEX fills_by_contract_day ON fill_facts(contract_id,trading_day);
CREATE INDEX alert_delivery_queue ON alert_outbox(delivery_status,next_attempt_at_utc);
CREATE TRIGGER scope_no_update BEFORE UPDATE ON ledger_scope BEGIN SELECT RAISE(ABORT,'SCOPE_IMMUTABLE'); END;
CREATE TRIGGER scope_no_delete BEFORE DELETE ON ledger_scope BEGIN SELECT RAISE(ABORT,'SCOPE_IMMUTABLE'); END;
CREATE TRIGGER rule_sets_no_update BEFORE UPDATE ON rule_sets BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER rule_sets_no_delete BEFORE DELETE ON rule_sets BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER decisions_no_update BEFORE UPDATE ON rule_decisions BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER decisions_no_delete BEFORE DELETE ON rule_decisions BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER configs_no_update BEFORE UPDATE ON config_revisions BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER configs_no_delete BEFORE DELETE ON config_revisions BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER plans_no_update BEFORE UPDATE ON protection_plans BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER plans_no_delete BEFORE DELETE ON protection_plans BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER approval_no_update BEFORE UPDATE ON approval_manifests BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER approval_no_delete BEFORE DELETE ON approval_manifests BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER approval_items_no_update BEFORE UPDATE ON approval_manifest_items BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER approval_items_no_delete BEFORE DELETE ON approval_manifest_items BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER policies_no_update BEFORE UPDATE ON policy_revisions BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER policies_no_delete BEFORE DELETE ON policy_revisions BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;

COMMIT;
