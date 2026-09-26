import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow
import segments as seg


class SegmentLogicTest(unittest.TestCase):
    def test_build_segments_records_altitudes(self):
        points = seg.normalize_points([[116.1, 39.8, 0], [116.2, 39.8, 120], [116.3, 39.8, 60]], 120)
        legs = seg.build_segments(points)
        self.assertEqual([leg["index"] for leg in legs], [1, 2])
        self.assertEqual(legs[0]["from"]["altitude"], 0)
        self.assertEqual(legs[0]["to"]["altitude"], 120)
        self.assertEqual((legs[0]["min_altitude"], legs[0]["max_altitude"]), (0, 120))
        self.assertEqual(legs[1]["min_altitude"], 60)

    def test_two_dimensional_points_default_to_ceiling(self):
        points = seg.normalize_points([[116.1, 39.8], [116.2, 39.9]], 100)
        self.assertEqual([p[2] for p in points], [100.0, 100.0])

    def test_climb_and_descent_checked_segment_by_segment(self):
        legs = seg.build_segments([[116.0, 39.9, 120], [116.01, 39.9, 120], [116.05, 39.9, 40], [116.09, 39.9, 40], [116.1, 39.9, 120]])
        zone = {"id": 1, "name": "高塔临时限制", "kind": "temporary_limit", "reason": "施工",
                "min_lon": 116.02, "min_lat": 39.89, "max_lon": 116.08, "max_lat": 39.91,
                "min_altitude": 60, "max_altitude": 120}
        conflicts = seg.segment_restriction_conflicts(legs, [zone])
        # 航段2 在水平范围内下降穿过 60-120m，航段4 在范围内但全程 40m，航段3 为跨越段同样命中
        self.assertIn(2, [c["segment"] for c in conflicts])
        self.assertNotIn(4, [c["segment"] for c in conflicts])
        self.assertTrue(all(c["name"] == "高塔临时限制" for c in conflicts))

    def test_boundary_altitude_touch_is_not_overlap(self):
        legs = seg.build_segments([[116.03, 39.9, 60], [116.07, 39.9, 60]])
        zone = {"id": 1, "name": "顶高限制", "kind": "no_fly", "reason": "x",
                "min_lon": 116.02, "min_lat": 39.89, "max_lon": 116.08, "max_lat": 39.91,
                "min_altitude": 60, "max_altitude": 120}
        self.assertEqual(seg.segment_restriction_conflicts(legs, [zone]), [])

    def test_changed_segment_indexes(self):
        old = seg.normalize_points([[116.1, 39.8, 0], [116.2, 39.8, 100], [116.3, 39.8, 100]], 100)
        new = seg.normalize_points([[116.1, 39.8, 0], [116.2, 39.8, 80], [116.35, 39.8, 100]], 100)
        self.assertEqual(seg.changed_segment_indexes(old, new), [1, 2])


class SegmentApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db"); self.start = utcnow() + timedelta(hours=2)

    def tearDown(self): self.tmp.cleanup()

    def plan(self, callsign="S200", route=None, altitude=120, risk=1):
        return self.svc.create_plan("op-user", "operator", "OP1", {"callsign": callsign, "drone_model": "M400", "payload_kg": 5,
            "route": route or [[116.0, 39.9, 120], [116.01, 39.9, 120], [116.05, 39.9, 40], [116.09, 39.9, 40], [116.1, 39.9, 120]],
            "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)), "max_altitude": altitude,
            "population_risk": risk, "emergency_plan": "返回起降点", "region": "BJ"})

    def restriction(self, name="高塔临时限制", kind="temporary_limit", lo=60, hi=120):
        self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": name, "kind": kind,
            "min_lon": 116.02, "min_lat": 39.89, "max_lon": 116.08, "max_lat": 39.91,
            "min_altitude": lo, "max_altitude": hi,
            "starts_at": iso(self.start - timedelta(minutes=30)), "ends_at": iso(self.start + timedelta(hours=2)), "reason": "施工"})

    def test_segments_endpoint_and_segment_level_conflict(self):
        plan = self.plan(); self.restriction()
        data = self.svc.get_segments(plan["id"], "airspace_reviewer", "")
        self.assertEqual(len(data["segments"]), 4)
        self.assertEqual(data["segments"][0]["from"]["altitude"], 120)
        check = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        hit = {c["segment"] for c in check["blocking_conflicts"] if c["code"] == "airspace_restriction"}
        self.assertIn(2, hit); self.assertNotIn(4, hit)
        self.assertFalse(check["approvable"])
        self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "seg-1", "reason": "审核"})
        self.assertEqual(ctx.exception.code, "airspace_conflict")
        self.assertTrue(any(d.get("segment") == 2 for d in ctx.exception.details["blocking_conflicts"]))

    def test_descending_below_zone_is_approvable(self):
        plan = self.plan(route=[[116.03, 39.9, 40], [116.07, 39.9, 40]])
        self.restriction(lo=60, hi=120)
        check = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        self.assertTrue(check["approvable"])

    def test_altitude_change_invalidates_approval_back_to_submission(self):
        plan = self.plan()
        submitted = self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})["plan"]
        approved = self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "seg-2", "reason": "通过"})
        self.assertEqual(approved["plan"]["status"], "approved")
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1",
                                  {"expected_revision": 1, "route": [[116.0, 39.9, 120], [116.01, 39.9, 100], [116.05, 39.9, 40], [116.09, 39.9, 40], [116.1, 39.9, 120]]})
        self.assertEqual(changed["status"], "draft"); self.assertEqual(changed["revision"], 2)
        # 回到提交流程：未重新提交不能批准
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 2, "offline_id": "seg-3", "reason": "再批"})
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        note = self.svc.notifications("op-user", "operator", "OP1")["notifications"][0]
        self.assertEqual(note["kind"], "approval_invalidated")
        self.assertIn("航段", note["message"])
        self.assertEqual(submitted["id"], changed["id"])


if __name__ == "__main__": unittest.main()
