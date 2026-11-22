"""Behavioural checks for quarantine-by-group and whole-group release.

Run with:  cd backend && python -m unittest discover -s tests
Set DATABASE_URL to any importable URL (sqlite:// works) and JWT_SECRET.
"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("JWT_SECRET", "test-secret")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.config.database import Base  # noqa: E402
from src.domain.analysis import (  # noqa: E402
    HELD, PENDING, RELEASED, analyze, assess_rows,
)
from src.domain.demo import demo_rows  # noqa: E402
from src.domain.spec import CAMPUS, CANTEEN, DEMO_DAY, PARTIAL, PRIOR_DAY, REDEEM  # noqa: E402
from src.models.quarantine import Quarantine  # noqa: E402
from src.services import quarantine  # noqa: E402


def _row(ledger, key, day, kind, original, cents, zone="东校区", group="自营档口"):
    return {"ledger": ledger, "transaction_key": key, "posting_date": day, "zone": zone,
            "merchant_group": group, "kind": kind, "original_key": original, "amount_cents": cents}


def _pair(key, day, kind, original, cents, **extra):
    return [_row(CAMPUS, key, day, kind, original, cents, **extra),
            _row(CANTEEN, key, day, kind, original, cents, **extra)]


def _bad_refund():
    rows = demo_rows()
    for row in rows:
        if row["transaction_key"] == "P002":
            row["original_key"] = "C999"
    return rows


def _corrected():
    rows = demo_rows()
    for row in rows:
        if row["transaction_key"] == "P002" and row["ledger"] == CANTEEN:
            row["amount_cents"] = 100
    return rows


class QuarantineTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)

    def _db(self):
        db = Session(self.engine)
        self.addCleanup(db.close)
        return db

    def test_bad_refund_no_longer_rejects_import(self):
        groups = assess_rows(_bad_refund())["groups"]
        self.assertEqual([(g["status"], g["keys"]) for g in groups], [(HELD, ["P002"])])

    def test_unrelated_vouchers_still_clear(self):
        report = analyze(_bad_refund(), {"posting_date": DEMO_DAY})
        self.assertFalse(report["ready"])
        statuses = {row["transaction_key"]: row["status"] for row in report["tables"][1]["rows"]}
        self.assertEqual(statuses["C004"], "匹配")
        self.assertEqual(statuses["P002"], "扣留")
        self.assertEqual(statuses["R003"], "匹配")

    def test_original_and_connected_rows_are_held_together(self):
        rows = _pair("C100", PRIOR_DAY, REDEEM, None, 500) + _pair(
            "P100", DEMO_DAY, PARTIAL, "C100", 900)
        groups = assess_rows(rows)["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["keys"], ["C100", "P100"])
        self.assertEqual(groups[0]["anchors"], ["C100"])

    def test_cross_day_refund_holds_prior_day_original(self):
        report = analyze(_bad_refund(), {"posting_date": PRIOR_DAY})
        # The wrong original C999 does not exist, so nothing on the prior day is
        # connected and the prior day clears.
        self.assertTrue(report["ready"])
        rows = demo_rows()
        for row in rows:
            if row["transaction_key"] == "P001":
                row["amount_cents"] = 9999
        report = analyze(rows, {"posting_date": PRIOR_DAY})
        held = {row["transaction_key"]: row["hold_state"] for row in report["tables"][1]["rows"]}
        self.assertEqual(held["C001"], "扣留")
        self.assertFalse(report["ready"])

    def test_different_originals_on_two_ledgers_hold_both(self):
        rows = demo_rows()
        for row in rows:
            if row["transaction_key"] == "P002":
                row["original_key"] = "C001" if row["ledger"] == CAMPUS else "C002"
        group = assess_rows(rows)["groups"][0]
        self.assertIn("C001", group["keys"])
        self.assertIn("C002", group["keys"])
        self.assertEqual(group["anchors"], ["C001", "C002"])

    def test_release_requires_whole_group_to_validate(self):
        db = self._db()
        quarantine.capture(db, 1, assess_rows(_bad_refund()), "analyst")
        db.commit()
        group_id = quarantine.history(db)[0]["group_id"]
        still_bad = demo_rows()
        for row in still_bad:
            if row["transaction_key"] == "P002":
                row["original_key"] = "C999"
        with self.assertRaises(ValueError):
            quarantine.release(db, 1, still_bad, group_id, "admin", "结算入账", "x")
        db.rollback()
        self.assertEqual(quarantine.history(db)[0]["status"], HELD)

    def test_release_is_idempotent(self):
        db = self._db()
        quarantine.capture(db, 1, assess_rows(_bad_refund()), "analyst")
        db.commit()
        group_id = quarantine.history(db)[0]["group_id"]
        _, created = quarantine.release(db, 2, _corrected(), group_id, "admin", "结算入账", "ok")
        db.commit()
        _, created_again = quarantine.release(db, 2, _corrected(), group_id, "admin", "结算入账", "ok")
        db.commit()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(len(db.scalars(select(Quarantine)).all()), 1)

    def test_released_group_settles_on_rerun(self):
        db = self._db()
        quarantine.capture(db, 1, assess_rows(_bad_refund()), "analyst")
        db.commit()
        group_id = quarantine.history(db)[0]["group_id"]
        quarantine.release(db, 2, _corrected(), group_id, "admin", "结算入账", "ok")
        db.commit()
        report = analyze(_corrected(), {"posting_date": DEMO_DAY}, quarantine.history(db))
        self.assertEqual(report["quarantine"][0]["status"], RELEASED)
        self.assertEqual(report["tables"][2]["rows"][0]["release_destination"], "结算入账")

    def test_fresh_bad_copy_after_release_is_held_again(self):
        db = self._db()
        quarantine.capture(db, 1, assess_rows(_bad_refund()), "analyst")
        db.commit()
        group_id = quarantine.history(db)[0]["group_id"]
        quarantine.release(db, 2, _corrected(), group_id, "admin", "结算入账", "ok")
        db.commit()
        fresh = assess_rows(_bad_refund(), quarantine.history(db))
        self.assertEqual([g["status"] for g in fresh["groups"]], [HELD])

    def test_snapshot_reports_state_and_destination(self):
        db = self._db()
        quarantine.capture(db, 1, assess_rows(_bad_refund()), "analyst")
        db.commit()
        snapshot = quarantine.snapshot(db)
        self.assertEqual(snapshot[0]["state_label"], "扣留")
        self.assertTrue(snapshot[0]["issues"])


if __name__ == "__main__":
    unittest.main()


class SplitGroupReleaseTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)

    def test_split_after_correction_releases_every_part(self):
        db = Session(self.engine)
        self.addCleanup(db.close)
        bad = demo_rows()
        for row in bad:
            if row["transaction_key"] == "P002":
                row["original_key"] = "C001" if row["ledger"] == CAMPUS else "C002"
        quarantine.capture(db, 1, assess_rows(bad), "analyst")
        db.commit()
        group_id = quarantine.history(db)[0]["group_id"]
        corrected = demo_rows()
        for row in corrected:
            if row["transaction_key"] == "P002":
                row["original_key"] = "C002"
                if row["ledger"] == CANTEEN:
                    row["amount_cents"] = 100
        parts = [g for g in assess_rows(corrected, quarantine.history(db))["groups"]
                 if g["group_id"] == group_id]
        self.assertGreater(len(parts), 1)
        records, created = quarantine.release(db, 2, corrected, group_id, "admin", "结算入账", "ok")
        db.commit()
        self.assertTrue(created)
        self.assertEqual(len(records), len(parts))
        self.assertTrue(all(item.status == RELEASED for item in records))
        self.assertEqual(len(db.scalars(select(Quarantine)).all()), len(parts))
