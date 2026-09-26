import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import ApiError, DroneAirspaceService, create_server, iso, utcnow
from segments import RouteError, compute_segments, segment_crosses_box, validate_route


class SegmentGeometryTest(unittest.TestCase):
    def test_segments_record_endpoint_altitudes(self):
        segments = compute_segments([[116.0, 39.0, 30], [116.1, 39.1, 90], [116.2, 39.2, 50]], 120)
        self.assertEqual(len(segments), 2)
        self.assertEqual((segments[0]["start_altitude"], segments[0]["end_altitude"]), (30, 90))
        self.assertEqual((segments[1]["start_altitude"], segments[1]["end_altitude"]), (90, 50))
        self.assertEqual(segments[0]["min_altitude"], 30)
        self.assertEqual(segments[1]["max_altitude"], 90)

    def test_two_dimensional_route_falls_back_to_plan_altitude(self):
        segments = compute_segments([[116.0, 39.0], [116.1, 39.1]], 100)
        self.assertEqual((segments[0]["start_altitude"], segments[0]["end_altitude"]), (100, 100))

    def test_segment_crosses_box(self):
        box = (116.0, 39.0, 116.1, 39.1)
        self.assertTrue(segment_crosses_box([116.05, 39.05], [116.2, 39.2], box))   # 起点在盒内
        self.assertTrue(segment_crosses_box([115.9, 39.05], [116.2, 39.05], box))   # 横穿矩形
        self.assertFalse(segment_crosses_box([115.9, 39.2], [116.2, 39.2], box))    # 完全在外
        # 航线包围盒与矩形重叠，但航段直线实际绕过（对角线擦角）
        self.assertFalse(segment_crosses_box([116.0, 39.0], [116.1, 39.1], (116.09, 39.0, 116.1, 39.005)))

    def test_validate_route_rejects_bad_waypoint_altitude(self):
        with self.assertRaises(RouteError):
            validate_route([[116.0, 39.0, 130], [116.1, 39.1, 100]], 120)
        with self.assertRaises(RouteError):
            validate_route([[116.0, 39.0, 0], [116.1, 39.1, 100]], 120)
        route = validate_route([[116.0, 39.0, 80], [116.1, 39.1]], 120)
        self.assertEqual(route[0], [116.0, 39.0, 80.0])
        self.assertEqual(route[1], [116.1, 39.1])


class SegmentConflictFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db")
        self.start = utcnow() + timedelta(hours=2)

    def tearDown(self):
        self.tmp.cleanup()

    def restriction(self, name="限制区", min_alt=0, max_alt=60, box=(116.15, 39.83, 116.25, 39.93)):
        return self.svc.create_restriction("reviewer", "airspace_reviewer", {
            "name": name, "kind": "no_fly",
            "min_lon": box[0], "min_lat": box[1], "max_lon": box[2], "max_lat": box[3],
            "min_altitude": min_alt, "max_altitude": max_alt,
            "starts_at": iso(self.start - timedelta(minutes=30)),
            "ends_at": iso(self.start + timedelta(hours=2)),
            "reason": "测试",
        })

    def plan(self, callsign="D200", route=None, altitude=120):
        return self.svc.create_plan("op-user", "operator", "OP1", {
            "callsign": callsign, "drone_model": "M400", "payload_kg": 5,
            "route": route if route is not None else [[116.1, 39.8, 30], [116.2, 39.88, 100], [116.3, 39.96, 120]],
            "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)),
            "max_altitude": altitude, "population_risk": 1, "emergency_plan": "返回起降点", "region": "BJ",
        })

    def test_plan_exposes_segments_with_endpoint_altitudes(self):
        plan = self.plan()
        fetched = self.svc.get_plan(plan["id"], "airspace_reviewer")
        self.assertEqual(len(fetched["segments"]), 2)
        self.assertEqual(fetched["segments"][0]["start_altitude"], 30)
        self.assertEqual(fetched["segments"][0]["end_altitude"], 100)
        self.assertEqual(fetched["segments"][1]["end_altitude"], 120)

    def test_conflict_report_names_segment_and_restriction(self):
        self.restriction(name="核心区禁飞", min_alt=0, max_alt=60)
        plan = self.plan()
        self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        report = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        airspace = [c for c in report["blocking_conflicts"] if c["code"] == "airspace_restriction"]
        # 只有第一段（30→100m 爬升穿过 0–60m 限制带）冲突；第二段在 100–120m 高于限制
        self.assertEqual(len(airspace), 1)
        self.assertEqual(airspace[0]["segment_index"], 0)
        self.assertEqual(airspace[0]["name"], "核心区禁飞")
        self.assertIsNotNone(airspace[0]["overlap_bbox"])
        self.assertFalse(report["approvable"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(plan["id"], "reviewer", "airspace_reviewer",
                             {"expected_revision": 1, "offline_id": "seg-1", "reason": "复核"})
        self.assertEqual(ctx.exception.code, "airspace_conflict")
        detail = ctx.exception.details["blocking_conflicts"][0]
        self.assertEqual(detail["segment_index"], 0)
        self.assertEqual(detail["name"], "核心区禁飞")

    def test_segments_above_restriction_altitude_band_are_clear(self):
        # 水平范围完全穿过限制区，但 100m 巡航高于 0–50m 限制带：旧逻辑按最高高度会误报
        self.restriction(min_alt=0, max_alt=50)
        plan = self.plan("D201", route=[[116.16, 39.85], [116.24, 39.92]], altitude=100)
        report = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        self.assertEqual(report["blocking_conflicts"], [])
        self.assertTrue(report["approvable"])

    def test_bbox_overlap_without_segment_crossing_is_clear(self):
        # 限制区落在航线包围盒内，但航段直线并不穿过：旧逻辑按包围盒会误报
        self.restriction(min_alt=0, max_alt=150, box=(116.09, 39.0, 116.1, 39.005))
        plan = self.plan("D202", route=[[116.0, 39.0], [116.1, 39.1]], altitude=100)
        report = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        self.assertTrue(report["approvable"])

    def test_segment_change_after_approval_returns_to_submission_flow(self):
        plan = self.plan("D203")
        self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        self.svc.approve(plan["id"], "reviewer", "airspace_reviewer",
                         {"expected_revision": 1, "offline_id": "seg-2", "reason": "同意"})
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1", {
            "expected_revision": 1,
            "route": [[116.1, 39.8, 30], [116.2, 39.88, 80], [116.3, 39.96, 120]],
        })
        self.assertEqual(changed["status"], "draft")
        self.assertEqual(changed["revision"], 2)
        kinds = [n["kind"] for n in self.svc.notifications("op-user", "operator", "OP1")["notifications"]]
        self.assertIn("approval_invalidated", kinds)
        audit = self.svc.repo.conn.execute("SELECT detail_json FROM audit_log WHERE action='plan_changed'").fetchone()
        self.assertTrue(json.loads(audit["detail_json"])["segments_changed"])
        # 原批准失效：旧版本审核决定不能套用，必须重新提交
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(plan["id"], "reviewer", "airspace_reviewer",
                             {"expected_revision": 1, "offline_id": "seg-3", "reason": "套用旧决定"})
        self.assertEqual(ctx.exception.code, "invalid_transition")
        resubmitted = self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})["plan"]
        self.assertEqual(resubmitted["status"], "submitted")
        reapproved = self.svc.approve(plan["id"], "reviewer", "airspace_reviewer",
                                      {"expected_revision": 2, "offline_id": "seg-4", "reason": "重新审核通过"})
        self.assertEqual(reapproved["plan"]["status"], "approved")

    def test_non_segment_change_marks_segments_unchanged(self):
        plan = self.plan("D204")
        self.svc.change(plan["id"], "op-user", "operator", "OP1",
                        {"expected_revision": 1, "emergency_plan": "原地降落"})
        audit = self.svc.repo.conn.execute("SELECT detail_json FROM audit_log WHERE action='plan_changed'").fetchone()
        self.assertFalse(json.loads(audit["detail_json"])["segments_changed"])

    def test_waypoint_altitude_above_declared_ceiling_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            self.plan("D205", route=[[116.1, 39.8, 130], [116.2, 39.9, 100]], altitude=120)
        self.assertEqual(ctx.exception.code, "invalid_waypoint_altitude")


class StaticConsoleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = create_server(Path(self.tmp.name) / "t.db", "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def fetch(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type", ""), exc.read()

    def test_console_page_and_stylesheet_served_separately(self):
        status, ctype, body = self.fetch("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"/static/console.css", body)
        status, ctype, body = self.fetch("/static/console.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", ctype)
        self.assertIn(b".badge", body)

    def test_static_path_traversal_rejected(self):
        status, _, _ = self.fetch("/static/../app.py")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
