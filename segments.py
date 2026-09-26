"""航段计算模块：把航线拆成连续航段，并逐段核对限制区的水平范围与高度区间。

本模块只负责航线/航段的校验和几何判断，不接触数据库与 HTTP，
由计划接口层(app.py)调用，做到航段计算与计划接口分离。

航线点支持两种形式：
- [经度, 纬度]：该点高度缺省，取计划申报的 max_altitude；
- [经度, 纬度, 高度]：显式航点高度，用于表达爬升和下降。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

Point = list[float]
BBox = list[float]  # [最小经度, 最小纬度, 最大经度, 最大纬度]


class RouteError(ValueError):
    """航线输入无效；code 供接口层映射为 ApiError 错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def validate_route(route: Any, max_altitude: float | None = None) -> list[Point]:
    """校验航线，返回归一化的航点列表；航点显式高度不得超过申报最大高度。"""
    if not isinstance(route, list) or len(route) < 2:
        raise RouteError("invalid_route", "航线至少需要两个经纬度点")
    normalized: list[Point] = []
    for point in route:
        if not isinstance(point, list) or len(point) not in (2, 3) or not all(isinstance(v, (int, float)) for v in point):
            raise RouteError("invalid_route_point", "每个航线点必须是 [经度,纬度] 或 [经度,纬度,高度]")
        lon, lat = float(point[0]), float(point[1])
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise RouteError("invalid_coordinates", "经纬度超出范围")
        if len(point) == 3:
            altitude = float(point[2])
            if altitude <= 0:
                raise RouteError("invalid_waypoint_altitude", "航点高度必须为正数")
            if max_altitude is not None and altitude > max_altitude:
                raise RouteError("invalid_waypoint_altitude", "航点高度不能超过申报的最大高度")
            normalized.append([lon, lat, altitude])
        else:
            normalized.append([lon, lat])
    return normalized


def waypoint_altitude(point: Sequence[float], default_altitude: float) -> float:
    return float(point[2]) if len(point) > 2 else float(default_altitude)


def segment_bbox(start: Sequence[float], end: Sequence[float]) -> BBox:
    return [min(start[0], end[0]), min(start[1], end[1]), max(start[0], end[0]), max(start[1], end[1])]


def route_bbox(route: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in route]
    ys = [float(point[1]) for point in route]
    return min(xs), min(ys), max(xs), max(ys)


def boxes_overlap(a: Sequence[float], b: Sequence[float], buffer: float = 0.0) -> bool:
    return a[0] <= b[2] + buffer and a[2] + buffer >= b[0] and a[1] <= b[3] + buffer and a[3] + buffer >= b[1]


def altitude_ranges_overlap(a_min: float, a_max: float, b_min: float, b_max: float) -> bool:
    """两个高度闭区间是否有重叠（恰好相切不算）。"""
    return a_min < b_max and b_min < a_max


def compute_segments(route: Sequence[Sequence[float]], default_altitude: float) -> list[dict[str, Any]]:
    """把航线拆为连续航段，每段记录起止点和起止高度。"""
    segments: list[dict[str, Any]] = []
    for index in range(len(route) - 1):
        start = [float(route[index][0]), float(route[index][1])]
        end = [float(route[index + 1][0]), float(route[index + 1][1])]
        start_altitude = waypoint_altitude(route[index], default_altitude)
        end_altitude = waypoint_altitude(route[index + 1], default_altitude)
        segments.append({
            "index": index,
            "start": start,
            "end": end,
            "start_altitude": start_altitude,
            "end_altitude": end_altitude,
            "min_altitude": min(start_altitude, end_altitude),
            "max_altitude": max(start_altitude, end_altitude),
            "bbox": segment_bbox(start, end),
        })
    return segments


def _orientation(a: Sequence[float], b: Sequence[float], c: Sequence[float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Sequence[float], b: Sequence[float], point: Sequence[float], eps: float = 1e-9) -> bool:
    return (min(a[0], b[0]) - eps <= point[0] <= max(a[0], b[0]) + eps
            and min(a[1], b[1]) - eps <= point[1] <= max(a[1], b[1]) + eps)


def _lines_intersect(p1: Sequence[float], p2: Sequence[float], p3: Sequence[float], p4: Sequence[float]) -> bool:
    d1, d2 = _orientation(p3, p4, p1), _orientation(p3, p4, p2)
    d3, d4 = _orientation(p1, p2, p3), _orientation(p1, p2, p4)
    if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0):
        return True
    eps = 1e-9
    if abs(d1) <= eps and _on_segment(p3, p4, p1): return True
    if abs(d2) <= eps and _on_segment(p3, p4, p2): return True
    if abs(d3) <= eps and _on_segment(p1, p2, p3): return True
    if abs(d4) <= eps and _on_segment(p1, p2, p4): return True
    return False


def point_in_box(point: Sequence[float], box: Sequence[float]) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def segment_crosses_box(start: Sequence[float], end: Sequence[float], box: Sequence[float]) -> bool:
    """航段直线是否与限制区水平范围（经纬度矩形）相交。

    相比整航线包围盒判断，可排除“包围盒重叠但航段实际绕过”的误报。
    """
    if point_in_box(start, box) or point_in_box(end, box):
        return True
    corners = [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]
    return any(_lines_intersect(start, end, corners[i], corners[(i + 1) % 4]) for i in range(4))


def overlap_bbox(a: Sequence[float], b: Sequence[float]) -> BBox | None:
    """两个矩形的交集，用于在协调台上标注冲突位置。"""
    box = [max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])]
    return box if box[0] <= box[2] and box[1] <= box[3] else None


def find_airspace_conflicts(segments: Sequence[Mapping[str, Any]], restrictions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """逐段核对限制区的水平范围和高度区间，返回包含航段编号和限制名称的冲突列表。"""
    conflicts: list[dict[str, Any]] = []
    for segment in segments:
        for restriction in restrictions:
            box = [restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"]]
            if not segment_crosses_box(segment["start"], segment["end"], box):
                continue
            if not altitude_ranges_overlap(segment["min_altitude"], segment["max_altitude"],
                                           restriction["min_altitude"], restriction["max_altitude"]):
                continue
            conflicts.append({
                "code": "airspace_restriction",
                "segment_index": segment["index"],
                "restriction_id": restriction["id"],
                "name": restriction["name"],
                "kind": restriction["kind"],
                "reason": restriction["reason"],
                "message": f"航段 {segment['index'] + 1} 与限制「{restriction['name']}」的水平范围和高度区间重叠",
                "segment_altitude_range": [segment["min_altitude"], segment["max_altitude"]],
                "restriction_altitude_range": [restriction["min_altitude"], restriction["max_altitude"]],
                "overlap_bbox": overlap_bbox(segment["bbox"], box),
            })
    return conflicts
