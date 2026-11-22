"""Persist quarantine groups and govern whole-group release.

The release ledger is the single source of truth a clearing run consults.  A
batch holding a group writes or refreshes its held rows; a release retires the
overlapping held rows and writes the released rows in one transaction.  A
clearing run reads the ledger in its own snapshot, so it sees a group either
wholly before or wholly after release -- never half of one.
"""

from sqlalchemy import select

from src.domain.analysis import (
    CAMPUS, CANTEEN, HELD, RELEASED, QUARANTINE_LABELS, assess_rows,
)
from src.models.quarantine import Quarantine


def history(db):
    """Read the whole release ledger in one statement for a consistent snapshot."""
    return [{
        "group_id": item.group_id, "signature": item.signature, "keys": item.keys,
        "anchors": item.anchors, "status": item.status, "destination": item.destination,
    } for item in db.scalars(select(Quarantine).order_by(Quarantine.id))]


def snapshot(db):
    """Full release ledger, including reasons and rows, for the review screen."""
    return [{
        "id": item.id, "group_id": item.group_id, "signature": item.signature,
        "keys": item.keys, "anchors": item.anchors, "status": item.status,
        "state_label": QUARANTINE_LABELS.get(item.status, item.status),
        "destination": item.destination, "issues": item.issues, "rows": item.rows,
        "note": item.note, "author": item.author, "dataset_id": item.dataset_id,
        "created_at": item.created_at.isoformat(timespec="seconds") if item.created_at else None,
    } for item in db.scalars(select(Quarantine).order_by(Quarantine.id.desc()).limit(200))]


def capture(db, dataset_id, assessment, actor):
    """Record every group the imported batch holds.

    Matching is by content signature, so a freshly broken copy of an earlier
    released group is quarantined anew rather than silently accepted.  Any older
    held copy of the same group is retired so one group keeps one held row.
    """
    for group in assessment["groups"]:
        if group["status"] != HELD:
            continue
        keys = set(group["keys"]) | set(group["anchors"])
        stale_rows = [stale for stale in db.scalars(select(Quarantine).where(
            Quarantine.status != RELEASED, Quarantine.signature != group["signature"])
            .with_for_update()).all()
            if keys & (set(stale.keys or ()) | set(stale.anchors or ()))]
        item = db.scalar(select(Quarantine).where(Quarantine.signature == group["signature"])
                         .with_for_update())
        if item is None:
            item = Quarantine(group_id=group["group_id"], signature=group["signature"], author=actor,
                              keys=group["keys"], anchors=group["anchors"], status=HELD,
                              issues=[], rows=[])
            db.add(item)
        for stale in stale_rows:
            if stale is not item:
                db.delete(stale)
        item.group_id = group["group_id"]
        item.keys = group["keys"]
        item.anchors = group["anchors"]
        item.status = HELD
        item.destination = None
        item.issues = [{"code": issue["code"], "message": issue["message"]} for issue in group["issues"]]
        item.rows = group["rows"]
        item.dataset_id = dataset_id
        item.author = actor


def _group_for(assessment, group_id):
    """Every component the quarantined group resolved into for this batch.

    A corrected batch can break one quarantined group into several components
    (for example once a refund is re-pointed at its real original).  All of them
    must validate before any of them is released.
    """
    return [group for group in assessment["groups"] if group["group_id"] == group_id] or None


def _blockers(group, assessment):
    """Every whole-group check the corrected copy must pass before release."""
    blockers = [issue["message"] for issue in group["issues"]]
    ledgers = {}
    for row in assessment["rows"]:
        ledgers.setdefault(row["ledger"], {})[row["transaction_key"]] = row
    labels = (("kind", "记账类型"), ("original_key", "原核销流水号"), ("zone", "校区"),
              ("merchant_group", "档口组"), ("amount_cents", "金额"))
    for key in group["keys"]:
        sides = [ledgers.get(ledger, {}).get(key) for ledger in (CAMPUS, CANTEEN)]
        if sides[0] is None or sides[1] is None:
            present = CAMPUS if sides[0] is not None else CANTEEN if sides[1] is not None else "两账"
            blockers.append(f"流水 {key} 只出现在{present}，整组放行前须双账齐全")
            continue
        fields = [label for field, label in labels if sides[0][field] != sides[1][field]]
        if fields:
            blockers.append(f"流水 {key} 两账在{'、'.join(fields)}不一致")
    return blockers


def release(db, dataset_id, rows, group_id, actor, destination, note):
    """Release one corrected group after validating every part of it again.

    A corrected batch may split the quarantined group into several components;
    all of them must pass before release.  All writes happen in one transaction:
    the held rows overlapping the corrected parts are retired and the released
    rows are written together, so a clearing run reading the ledger in one
    statement sees the group wholly before or wholly after release.  Releasing an
    identical corrected group twice is idempotent and writes nothing.
    """
    assessment = assess_rows(rows, history(db))
    parts = _group_for(assessment, group_id)
    if not parts:
        raise ValueError("当前批次中没有该扣留组，请确认补正数据已导入")
    blockers = list(dict.fromkeys(
        blocker for part in parts for blocker in _blockers(part, assessment)))
    if blockers:
        raise ValueError("整组校验未通过，暂不能放行：" + "；".join(blockers))

    signatures = [part["signature"] for part in parts]
    key_set = set().union(*(set(part["keys"]) | set(part["anchors"]) for part in parts))
    held_rows = [item for item in db.scalars(
        select(Quarantine).where(Quarantine.status != RELEASED).with_for_update()).all()
        if key_set & (set(item.keys or ()) | set(item.anchors or ()))]
    existing = {item.signature: item for item in db.scalars(
        select(Quarantine).where(Quarantine.signature.in_(signatures),
                                 Quarantine.status == RELEASED).with_for_update())}
    records = []
    created = False
    for part in parts:
        item = existing.get(part["signature"])
        if item is None:
            item = next((entry for entry in held_rows if entry.signature == part["signature"]), None)
        if item is None:
            item = Quarantine(group_id=group_id, signature=part["signature"], author=actor,
                              keys=part["keys"], anchors=part["anchors"], status=HELD,
                              issues=[], rows=[])
            db.add(item)
            created = True
        item.group_id = group_id
        item.keys = part["keys"]
        item.anchors = part["anchors"]
        item.status = RELEASED
        item.destination = destination
        item.issues = [{"code": issue["code"], "message": issue["message"]} for issue in part["issues"]]
        item.rows = part["rows"]
        item.note = note
        item.author = actor
        item.dataset_id = dataset_id
        records.append(item)
    for stale in held_rows:
        if stale not in records:
            db.delete(stale)
    db.flush()
    return records, created
