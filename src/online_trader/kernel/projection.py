"""Read-only protocol projections: unknown money remains unknown."""


def projection_batch(snapshot: dict, *, include_orders=True) -> dict:
    state="MAINTENANCE" if snapshot.get("maintenance_state") not in {None,"NONE","COMPLETE","FAILED","RELEASED"} else "DEGRADED" if snapshot["storage_failed"] or not snapshot["recovery_required"] else "RECONCILING"
    online=snapshot.get('execution_scope')=='ONLINE_SIMULATION'
    if online and not snapshot['storage_failed'] and not snapshot['recovery_required']:state='READY'
    revision=snapshot["durable_seq"]
    records=[{"record_type":"ACCOUNT","record_id":snapshot["account_key"],"record_revision":revision,
        "kernel_state":state,"entry_authority":"ARMED" if snapshot["armed"] else "DISARMED",
        "equity":None,"available":None,"margin":None,"fees":None,"funds_quality":"UNKNOWN","source_epoch":"0","fact_barrier_id":None}]
    if snapshot.get('funds') is not None:records[0].update(snapshot['funds'])
    for cycle in snapshot["cycles"]:
        p,s=cycle["plan"],cycle["protection"]
        records.append({"record_type":"CYCLE","record_id":cycle["position_cycle_id"],"record_revision":str(cycle["revision"]),
            "position_cycle_id":cycle["position_cycle_id"],"contract_id":p["contract_id"],"direction":p["direction"],"cycle_state":cycle["lifecycle"],
            "confirmed_open_quantity":s["entry_filled_qty"],"confirmed_close_quantity":s["exit_filled_qty"],"remaining_quantity":s["owned_qty"],
            "target_remaining_quantity":s.get("target_remaining_qty"),"plan_id":p["plan_id"],"plan_revision":p["plan_revision"],
            "protection_mode":s["mode"],"protection_quality":s["quality"],"protection_persistence":"DURABLE","takeover_state":cycle.get("management_state")})
        for order in cycle["orders"] if include_orders else ():
            records.append({"record_type":"ORDER","record_id":order["child_order_id"],"record_revision":str(order["revision"]),
                "child_order_id":order["child_order_id"],"intent_id":order["intent_id"],"position_cycle_id":cycle["position_cycle_id"],"contract_id":p["contract_id"],
                "origin":cycle.get("entry_origin","MANUAL") if order["effect"]=="OPEN" else "PROTECTION","order_quantity":order["original_qty"],"confirmed_fill_quantity":order["filled_qty"],
                "broker_order_key":cycle.get('native_order_keys',{}).get(order['child_order_id']),"attempt_state":order["send_state"],"broker_order_state":order["broker_state"],"cancel_state":order["cancel_status"]})
    records.append({"record_type":"BLOCK","record_id":"online-simulation-test-scope" if online else "g1-no-broker-adapter","record_revision":revision,
        "reason_code":"BOUNDED_ONLINE_SIMULATION_ONLY" if online else "NO_BROKER_ADAPTER","scope":{"scope_type":"ACCOUNT","scope_ids":[]},"active":True,"since_event_seq":"0","release_evidence_id":None})
    return {"records":records}
