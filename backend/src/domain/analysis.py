"""Match the two ledgers by event key and reconcile each ledger's signed cents.

A bad refund no longer rejects the whole import.  The wrong original redemption,
every flow attached to it in either ledger and cross-day refunds are quarantined
as one group, while unrelated vouchers keep clearing.  A group only leaves
quarantine after the entire group validates and a reviewer releases it.
"""

from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
import re

from .spec import CAMPUS, CANTEEN, MAX_CENTS, PARTIAL, REDEEM, REVERSE, SPEC


_MATCH = "匹配"
_MISSING_CAMPUS = "校园券账缺失"
_MISSING_CANTEEN = "食堂核销账缺失"
_DIFFERENCE = "差异"
_HELD_LABEL = "扣留"
_STATUSES = (_MATCH, _MISSING_CAMPUS, _MISSING_CANTEEN, _DIFFERENCE, _HELD_LABEL)

HELD = "held"
PENDING = "pending"
RELEASED = "released"
QUARANTINE_LABELS = {HELD: "扣留", PENDING: "待放行", RELEASED: "已放行"}
_DEST_HELD = "补正后整组放行至结算入账"
_DEST_PENDING = "待复核放行至结算入账"
_DEST_RELEASED = "已放行，随本批次结算入账"
_DEST_CLEAR = "结算入账"

_COMPARE = (
    ("kind", "记账类型"), ("original_key", "原核销流水号"), ("zone", "校区"),
    ("merchant_group", "档口组"), ("amount_cents", "金额"),
)
_SUMMARY_COLUMNS = (
    ("campus_count", "校园券账行数"), ("canteen_count", "食堂核销账行数"),
    ("campus_cents", "校园券账净额（分）"), ("canteen_cents", "食堂核销账净额（分）"),
    ("delta_cents", "双账差额（分）"),
)
_ZONE = ("zone", "校区")
_GROUP = ("merchant_group", "档口组")


def _day(value):
    try:
        day = date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValueError("日期须为 YYYY-MM-DD") from None
    if day != value:
        raise ValueError("日期须为 YYYY-MM-DD")
    return day


def _text(field, value):
    if isinstance(value, str):
        value = value.strip()
    if value in (None, "") and not field["required"]:
        return None
    if not isinstance(value, str) or not value or len(value) > field.get("max_length", 300):
        raise ValueError(f'{field["label"]}缺失或格式不正确')
    if field["type"] == "select" and value not in field["options"]:
        raise ValueError(f'{field["label"]}取值不受支持')
    return value


def _cents(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,13}", value.strip()):
        value = int(value)
    if type(value) is not int or not 1 <= value <= MAX_CENTS:
        raise ValueError(f"金额须为 1 至 {MAX_CENTS} 的整数分")
    return value


def _row(raw):
    if not isinstance(raw, dict):
        raise ValueError("流水须为字段对象")
    row = {}
    for field in SPEC["fields"]:
        key = field["key"]
        value = raw.get(key)
        if key == "posting_date":
            row[key] = _day(value)
        elif key == "amount_cents":
            row[key] = _cents(value)
        else:
            row[key] = _text(field, value)
    return row


def validate_rows(rows):
    """Normalize each ledger cell and reject only unusable structural rows.

    Ledger-entry faults (a refund pointing at the wrong original, an over-refund
    and the like) are not raised here: they are isolated by :func:`assess_rows`
    so the rest of the batch can still clear.
    """
    ledgers = {CAMPUS: {}, CANTEEN: {}}
    validated = []
    for index, raw in enumerate(rows, 1):
        try:
            row = _row(raw)
            key = row["transaction_key"]
            ledger_rows = ledgers[row["ledger"]]
            if key in ledger_rows:
                raise ValueError(f'{row["ledger"]}流水号 {key} 重复')
            ledger_rows[key] = row
            validated.append(row)
        except ValueError as error:
            raise ValueError(f"明细第 {index} 行 · {error}") from None
    if not validated:
        raise ValueError("至少需要一行餐券流水")
    return validated


def _issue(code, key, message, keys, rows):
    ordered = []
    for item in rows:
        if item not in ordered:
            ordered.append(item)
    return {"code": code, "key": key, "message": message,
            "keys": sorted(set(keys)), "rows": ordered}


def _ledger_issues(ledger, rows):
    """Return every ledger-entry fault for one ledger without raising."""
    issues = []
    adjustments = defaultdict(list)
    for key in sorted(rows):
        row = rows[key]
        if row["kind"] == REDEEM:
            if row["original_key"] is not None:
                issues.append(_issue("redeem_has_original", key, f"{ledger}核销流水 {key} 不应填写原核销流水号",
                                     [key], [(ledger, key)]))
            continue
        original_key = row["original_key"]
        if original_key is None:
            issues.append(_issue("adjustment_missing_original", key,
                                 f"{ledger}流水 {key} 为{row['kind']}，须引用原核销流水号",
                                 [key], [(ledger, key)]))
            continue
        original = rows.get(original_key)
        if original is None or original["kind"] != REDEEM:
            issues.append(_issue("adjustment_unknown_original", key,
                                 f"{ledger}流水 {key} 引用的原核销 {original_key} 不在同账或不是核销",
                                 [key, original_key], [(ledger, key)]))
            continue
        if row["posting_date"] < original["posting_date"]:
            issues.append(_issue("adjustment_before_original", key,
                                 f"{ledger}流水 {key} 的入账日早于原核销 {original_key}",
                                 [key, original_key], [(ledger, key)]))
        if (row["zone"], row["merchant_group"]) != (original["zone"], original["merchant_group"]):
            issues.append(_issue("adjustment_mismatch", key,
                                 f"{ledger}流水 {key} 的校区或档口组与原核销 {original_key} 不一致",
                                 [key, original_key], [(ledger, key)]))
        adjustments[original_key].append(row)

    for original_key in sorted(adjustments):
        entries = adjustments[original_key]
        original = rows[original_key]
        partials = [row for row in entries if row["kind"] == PARTIAL]
        reversals = [row for row in entries if row["kind"] == REVERSE]
        involved = [original_key] + [row["transaction_key"] for row in entries]
        positions = [(ledger, key) for key in involved]
        remaining = original["amount_cents"] - sum(row["amount_cents"] for row in partials)
        if remaining <= 0:
            issues.append(_issue("over_refund", original_key,
                                 f"{ledger}原核销 {original_key} 累计部分退扣须小于原核销金额",
                                 involved, positions))
        if len(reversals) > 1:
            issues.append(_issue("multiple_reversals", original_key,
                                 f"{ledger}原核销 {original_key} 最多允许一笔全额冲正",
                                 involved, positions))
        if reversals:
            reversal = reversals[0]
            if any(row["posting_date"] > reversal["posting_date"] for row in partials):
                issues.append(_issue("partial_after_reversal", original_key,
                                     f"{ledger}原核销 {original_key} 冲正日后仍有部分退扣",
                                     involved, positions))
            if reversal["amount_cents"] != remaining:
                issues.append(_issue("reversal_amount_mismatch", original_key,
                                     f"{ledger}原核销 {original_key} 冲正金额须等于部分退扣后的剩余金额 {remaining} 分",
                                     involved, positions))
    return issues


def _cross_issues(ledgers):
    """A refund may not hang a different original on each ledger."""
    issues = []
    campus, canteen = ledgers[CAMPUS], ledgers[CANTEEN]
    for key in sorted(campus.keys() & canteen.keys()):
        left, right = campus[key], canteen[key]
        left_adjust, right_adjust = left["kind"] != REDEEM, right["kind"] != REDEEM
        if left_adjust and right_adjust:
            if left["original_key"] != right["original_key"]:
                keys = [key] + [item for item in (left["original_key"], right["original_key"]) if item]
                issues.append(_issue(
                    "cross_ledger_original_mismatch", key,
                    f"流水 {key} 在两账分别引用原核销 {left['original_key'] or '空'} 与 {right['original_key'] or '空'}，两笔原单均须扣留",
                    keys, [(CAMPUS, key), (CANTEEN, key)]))
        elif left_adjust != right_adjust:
            original = left["original_key"] if left_adjust else right["original_key"]
            keys = [key] + ([original] if original else [])
            issues.append(_issue(
                "cross_ledger_kind_mismatch", key,
                f"流水 {key} 在校园券账为{left['kind']}、在食堂核销账为{right['kind']}，记账类型不一致",
                keys, [(CAMPUS, key), (CANTEEN, key)]))
    return issues


def _components(ledgers):
    parent = {}

    def find(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    for rows in ledgers.values():
        for key, row in rows.items():
            find(key)
            if row["original_key"]:
                find(row["original_key"])
                union(key, row["original_key"])
    components = defaultdict(set)
    for key in parent:
        components[find(key)].add(key)
    return sorted(components.values(), key=lambda keys: sorted(keys))


def _signature(entries):
    payload = sorted(json.dumps({"ledger": ledger, **row}, ensure_ascii=False, sort_keys=True)
                     for ledger, row in entries)
    return hashlib.sha256("\n".join(payload).encode()).hexdigest()


def _group_id(keys):
    return "QG-" + hashlib.sha1("|".join(sorted(keys)).encode()).hexdigest()[:8].upper()


def _group_rows(ledgers, keys):
    entries = []
    for key in sorted(keys):
        for ledger in (CAMPUS, CANTEEN):
            row = ledgers[ledger].get(key)
            if row is not None:
                entries.append((ledger, row))
    return entries


def _anchor_keys(entries):
    return sorted({row["original_key"] for _, row in entries if row.get("original_key")})


def assess_rows(rows, history=()):
    """Return normalized rows and every quarantine group with its resolved state.

    ``history`` holds persisted group records (any batch, oldest first).  A group
    is reported when it has at least one ledger-entry fault, when its content was
    already released, or when a corrected copy overlaps a held group and awaits
    release.  A pending copy inherits the held group's id so the reviewer can
    release exactly the group that was quarantined.
    """
    records = [record for record in history if isinstance(record, dict)]
    validated = validate_rows(rows)
    ledgers = {CAMPUS: {}, CANTEEN: {}}
    for row in validated:
        ledgers[row["ledger"]][row["transaction_key"]] = row

    issues = []
    for ledger in (CAMPUS, CANTEEN):
        issues.extend(_ledger_issues(ledger, ledgers[ledger]))
    issues.extend(_cross_issues(ledgers))

    released = {record.get("signature"): record for record in records if record.get("status") == RELEASED}
    held_records = [record for record in records if record.get("status") in (HELD, PENDING)]

    def inheriting(keys):
        """Held record a corrected component descends from, matching on any key."""
        key_set = set(keys)
        for record in held_records:
            if key_set & (set(record.get("keys") or ()) | set(record.get("anchors") or ())):
                return record
        return None

    groups = []
    for component in _components(ledgers):
        entries = _group_rows(ledgers, component)
        if not entries:
            continue
        keys = sorted({row["transaction_key"] for _, row in entries})
        key_set = set(keys)
        group_issues = [issue for issue in issues if key_set & set(issue["keys"])]
        signature = _signature(entries)
        anchors = _anchor_keys(entries)
        inheritor = None
        if group_issues:
            status = HELD
        elif signature in released:
            status = RELEASED
            inheritor = released[signature]
        else:
            inheritor = inheriting(keys)
            if inheritor is None:
                continue
            status = PENDING
        groups.append({
            "group_id": (inheritor.get("group_id") if inheritor else None) or _group_id(keys),
            "keys": keys, "anchors": anchors, "signature": signature, "status": status,
            "issues": group_issues,
            "destination": inheritor.get("destination") if status == RELEASED else None,
            "rows": [{"ledger": ledger, **row} for ledger, row in entries],
        })
    return {"rows": validated, "groups": groups}


def group_reason(group, key):
    messages = [issue["message"] for issue in group["issues"] if key in issue["keys"]]
    if messages:
        return "；".join(messages)
    if group["anchors"]:
        return f"牵连扣留：同组原核销 {'、'.join(group['anchors'])}"
    return "同组流水牵连扣留"


def destination(group):
    if group["status"] == RELEASED:
        return group.get("destination") or _DEST_RELEASED
    if group["status"] == PENDING:
        return _DEST_PENDING
    return _DEST_HELD


def _signed(row):
    return row["amount_cents"] if row["kind"] == REDEEM else -row["amount_cents"]


def _drilldown(campus, canteen, posting_days, tracked):
    """Per-key comparison; rows inside an unresolved group are marked 扣留."""
    entries = []
    for key in sorted(campus.keys() | canteen.keys()):
        left, right = campus.get(key), canteen.get(key)
        group = tracked.get(key)
        unresolved = group is not None and group["status"] != RELEASED
        hold_state = "正常" if group is None else QUARANTINE_LABELS[group["status"]]
        hold_reason = group_reason(group, key) if unresolved else None
        if unresolved:
            status = _HELD_LABEL
            reason = hold_reason
        elif left is None or right is None:
            ledger = CAMPUS if left is None else CANTEEN
            status = _MISSING_CAMPUS if left is None else _MISSING_CANTEEN
            other_day = posting_days.get((ledger, key))
            reason = f"{ledger}入账日为 {other_day}" if other_day else f"请查{ledger}是否漏传"
        else:
            differences = [label for field, label in _COMPARE if left[field] != right[field]]
            status = _DIFFERENCE if differences else _MATCH
            reason = "、".join(differences)
        entry = {
            "transaction_key": key, "status": status, "reason": reason,
            "hold_state": hold_state, "hold_reason": hold_reason,
            "release_destination": destination(group) if group is not None else _DEST_CLEAR,
        }
        for prefix, row in (("campus", left), ("canteen", right)):
            for field, _ in _COMPARE[:-1]:
                entry[f"{prefix}_{field}"] = row[field] if row else None
            entry[f"{prefix}_cents"] = _signed(row) if row else None
        entry["delta_cents"] = (entry["campus_cents"] or 0) - (entry["canteen_cents"] or 0)
        entries.append(entry)
    return entries


def _summaries(rows, keys):
    groups = defaultdict(Counter)
    for row in rows:
        total = groups[tuple(row[key] for key in keys)]
        prefix = "campus" if row["ledger"] == CAMPUS else "canteen"
        total[f"{prefix}_count"] += 1
        total[f"{prefix}_cents"] += _signed(row)
    return [dict(zip(keys, values), **{key: totals[key] for key, _ in _SUMMARY_COLUMNS[:-1]},
                 delta_cents=totals["campus_cents"] - totals["canteen_cents"])
            for values, totals in sorted(groups.items())]


def _margin_matches(cells, margins, dimension):
    keys = [key for key, _ in _SUMMARY_COLUMNS]
    sums = defaultdict(Counter)
    for cell in cells:
        for key in keys:
            sums[cell[dimension]][key] += cell[key]
    expected = {row[dimension]: {key: row[key] for key in keys} for row in margins}
    return sums == expected


def _table(title, columns, rows):
    return {"title": title, "columns": [{"key": key, "label": label} for key, label in columns], "rows": rows}


def _quarantine_table(groups):
    rows = []
    for group in groups:
        for record in group["rows"]:
            key = record["transaction_key"]
            rows.append({
                "ledger": record["ledger"], "transaction_key": key,
                "posting_date": record["posting_date"], "kind": record["kind"],
                "original_key": record["original_key"], "zone": record["zone"],
                "merchant_group": record["merchant_group"], "amount_cents": _signed(record),
                "hold_state": QUARANTINE_LABELS[group["status"]],
                "hold_reason": group_reason(group, key) if group["status"] != RELEASED else "整组复核通过并已放行",
                "release_destination": destination(group),
            })
    return rows


def _group_map(groups, statuses):
    mapped = {}
    for group in groups:
        if group["status"] in statuses:
            for key in group["keys"]:
                mapped[key] = group
    return mapped


def analyze(rows, normalized_params, history=()):
    """Return the standard batch report for the selected posting date.

    ``history`` is the release ledger read in the same snapshot as ``rows`` so a
    clearing run sees each group wholly before or wholly after release.
    """
    if not isinstance(normalized_params, dict) or normalized_params.keys() - {"posting_date"}:
        raise ValueError("日终清分仅支持结算日参数")
    day = _day(normalized_params.get("posting_date", SPEC["params"][0]["default"]))
    assessment = assess_rows(rows, history)
    validated, groups = assessment["rows"], assessment["groups"]
    held = _group_map(groups, (HELD, PENDING))
    released = _group_map(groups, (RELEASED,))
    posting_days = {(row["ledger"], row["transaction_key"]): row["posting_date"] for row in validated}
    selected = [row for row in validated if row["posting_date"] == day]
    tracked = {**held, **released}
    cleared = [row for row in selected if row["transaction_key"] not in held]
    held_selected = [row for row in selected if row["transaction_key"] in held]
    campus_all = {row["transaction_key"]: row for row in selected if row["ledger"] == CAMPUS}
    canteen_all = {row["transaction_key"]: row for row in selected if row["ledger"] == CANTEEN}
    campus = {row["transaction_key"]: row for row in cleared if row["ledger"] == CAMPUS}
    canteen = {row["transaction_key"]: row for row in cleared if row["ledger"] == CANTEEN}
    drilldown = _drilldown(campus_all, canteen_all, posting_days, tracked)
    display_counts = Counter(row["status"] for row in drilldown)
    counts = Counter(row["status"] for row in drilldown
                     if row["hold_state"] not in (QUARANTINE_LABELS[HELD], QUARANTINE_LABELS[PENDING]))
    missing = counts[_MISSING_CAMPUS] + counts[_MISSING_CANTEEN]
    differences = counts[_DIFFERENCE]
    selected_keys = {row["transaction_key"] for row in selected}
    held_keys = sorted({key for key in held if key in selected_keys})
    # Only groups that touch the selected day block that day's clearing.  A group
    # confined to another day is isolated there and leaves this day untouched.
    scoped = [group for group in groups if set(group["keys"]) & selected_keys]
    unresolved = sorted({group["group_id"] for group in scoped if group["status"] in (HELD, PENDING)})

    cells = _summaries(cleared, ("zone", "merchant_group"))
    zones = _summaries(cleared, ("zone",))
    group_totals = _summaries(cleared, ("merchant_group",))
    campus_cents = sum(map(_signed, campus.values()))
    canteen_cents = sum(map(_signed, canteen.values()))
    totals = {"campus_count": len(campus), "canteen_count": len(canteen), "campus_cents": campus_cents,
              "canteen_cents": canteen_cents, "delta_cents": campus_cents - canteen_cents}
    crossfoot = (all(sum(cell[key] for cell in cells) == value for key, value in totals.items())
                 and _margin_matches(cells, zones, "zone") and _margin_matches(cells, group_totals, "merchant_group"))
    quarantine_detail = (
        f"{len(unresolved)} 组扣留未放行：{'、'.join(unresolved)}。"
        if unresolved else "没有待放行的扣留流水。")
    checks = (
        ("当日入账", bool(cleared), f"当日清分 {len(cleared)} 行，扣留 {len(held_selected)} 行未计入。"),
        ("双账齐全", missing == 0, f"{missing} 笔清分流水只出现在一侧。"),
        ("逐笔一致", differences == 0, f"{differences} 笔双边清分流水存在差异。"),
        ("交叉勾稽", crossfoot, "核对校区小计、档口组小计及全账行数与净额。"),
        ("扣留隔离", not unresolved, quarantine_detail),
    )
    ready = all(passed for _, passed, _ in checks)
    gate = "通过" if ready else "阻断"
    check_rows = [{"check": label, "status": "通过" if passed else "阻断", "detail": detail}
                  for label, passed, detail in checks]
    check_rows.append({"check": "结算放行", "status": gate, "detail": "所有检查通过后可确认入账。"})
    guidance = "核对小计后可将清分报告交给财务入账。"
    if unresolved:
        guidance = "无关餐券已照常清分；请补正扣留组并整组放行后重跑本结算日。"
    elif not ready:
        guidance = "请按明细中的流水号追查缺失、差异或跨日补传，订正后沿用本结算日重跑。"
    if not selected:
        guidance = "当前结算日没有入账流水，请核对日期及双账导出范围后重跑。"
    entry_columns = (
        ("transaction_key", "流水号"), ("status", "核对状态"), ("reason", "追查字段或原因"),
        *((f"{prefix}_{key}", f"{name} · {label}")
          for prefix, name in (("campus", CAMPUS), ("canteen", CANTEEN)) for key, label in _COMPARE[:-1]),
        *_SUMMARY_COLUMNS[2:],
        ("hold_state", "扣留状态"), ("hold_reason", "扣留原因"), ("release_destination", "放行去向"),
    )
    quarantine_rows = _quarantine_table(scoped)
    return {
        "state": "ok" if ready else "warning",
        "ready": ready,
        "headline": f"{day} 结算检查{gate}，{counts[_MATCH]} 笔匹配、{missing} 笔单边缺失、"
                    f"{differences} 笔差异、{len(held_keys)} 笔扣留。",
        "note": "按入账日匹配，冲正与退扣按负分记账；扣留组整组隔离，补正放行后才计入结算。差额为校园券账减食堂核销账。",
        "metrics": [
            {"label": "校园券账净额（分）", "value": campus_cents, "detail": f"{len(campus)} 行清分"},
            {"label": "食堂核销账净额（分）", "value": canteen_cents, "detail": f"{len(canteen)} 行清分"},
            {"label": "双账差额（分）", "value": totals["delta_cents"], "detail": "逐笔异常影响结算放行"},
            {"label": "扣留组数", "value": len(unresolved), "detail": f"{len(held_selected)} 行未计入本日清分"},
        ],
        "tables": [
            _table("结算检查", (("check", "检查项"), ("status", "结果"), ("detail", "说明")), check_rows),
            _table("流水核对明细", entry_columns, drilldown),
            _table("扣留与放行明细", (
                ("ledger", "记账来源"), ("transaction_key", "流水号"), ("posting_date", "入账日"),
                ("kind", "记账类型"), ("original_key", "原核销流水号"), ("zone", "校区"),
                ("merchant_group", "档口组"), ("amount_cents", "金额（分）"),
                ("hold_state", "扣留状态"), ("hold_reason", "扣留原因"), ("release_destination", "放行去向"),
            ), quarantine_rows),
            _table("校区 × 档口组勾稽", (_ZONE, _GROUP) + _SUMMARY_COLUMNS, cells),
            _table("校区小计", (_ZONE,) + _SUMMARY_COLUMNS, zones),
            _table("档口组小计", (_GROUP,) + _SUMMARY_COLUMNS, group_totals),
            _table("双账合计", _SUMMARY_COLUMNS, [totals]),
        ],
        "charts": [{"title": "流水核对状态", "kind": "bar",
                    "series": [{"label": status, "value": display_counts[status]} for status in _STATUSES]}],
        "sections": [{"title": "财务复核", "text": guidance}],
        "quarantine": [
            {"group_id": group["group_id"], "status": group["status"], "keys": group["keys"],
             "anchors": group["anchors"], "signature": group["signature"], "destination": group["destination"],
             "rows": [{**record} for record in group["rows"]],
             "issues": [{"code": issue["code"], "message": issue["message"]} for issue in group["issues"]]}
            for group in scoped
        ],
    }


def annotate(assessment, rows):
    """Per-ledger-row hold label, reason and release destination for display."""
    tracked = {}
    for group in assessment["groups"]:
        for key in group["keys"]:
            tracked[key] = group
    out = []
    for row in rows:
        group = tracked.get(row["transaction_key"])
        if group is None:
            out.append({"ledger": row["ledger"], "transaction_key": row["transaction_key"],
                        "hold_state": "正常", "hold_reason": None,
                        "release_destination": _DEST_CLEAR})
            continue
        released = group["status"] == RELEASED
        out.append({
            "ledger": row["ledger"], "transaction_key": row["transaction_key"],
            "hold_state": QUARANTINE_LABELS[group["status"]],
            "hold_reason": "整组复核通过并已放行" if released else group_reason(group, row["transaction_key"]),
            "release_destination": destination(group),
        })
    return out
