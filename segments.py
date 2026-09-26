"""航段计算模块：把航线拆成连续航段并逐段核对限制区。

只依赖标准库，不涉及 HTTP 与数据库，便于与计划接口分开演进和单独测试。
航点支持两种写法：
- [经度, 纬度]：未给高度时取计划最大高度（平飞）；
- [经度, 纬度, 高度]：记录该航点的真实高度，爬升/下降由相邻航点高度体现。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

SEGMENT_ALTITUDE_LIMIT = 120.0

KIND_LABELS = {"no_fly": "禁飞区", "temporary_limit": "临时限制区"}


def normalize_points(route: Any, default_altitude: Any) -> list[list[float]]:
    """校验航线并返回带高度的航点列表 [[lon, lat, alt], ...]。"""
    if not isinstance(route, list) or len(route) < 2:
        raise ValueError("航线至少需要两个经纬度点")
    try:
        ceiling = float(default_altitude)
    except (TypeError, ValueError) as exc:
        raise ValueError("计划最大高度必须为数字") from exc
    points: list[list[float]] = []
    for point in route:
        if not isinstance(point, list) or len(point) not in (2, 3) or not all(isinstance(v, (int, float)) for v in point):
            raise ValueError("每个航线点必须是 [经度,纬度] 或 [经度,纬度,高度]")
        lon, lat = float(point[0]), float(point[1])
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise ValueError("经纬度超出范围")
        altitude = ceiling if len(point) == 2 else float(point[2])
        if altitude < 0:
            raise ValueError("航点高度不能为负")
        if altitude > ceiling:
            raise ValueError("航点高度不能高于计划最大高度")
        points.append([lon, lat, altitude])
    return points


def build_segments(points: Sequence[Sequence[float]]) -> list[dict[str, Any]]:
    """相邻航点组成连续航段，编号从 1 开始，记录起止高度和高度区间。"""
    legs: list[dict[str, Any]] = []
    for i in range(len(points) - 1):
        start, end = points[i], points[i + 1]
        legs.append({
            "index": i + 1,
            "from": {"lon": float(start[0]), "lat": float(start[1]), "altitude": float(start[2])},
            "to": {"lon": float(end[0]), "lat": float(end[1]), "altitude": float(end[2])},
            "min_altitude": float(min(start[2], end[2])),
            "max_altitude": float(max(start[2], end[2])),
            "bbox": [float(min(start[0], end[0])), float(min(start[1], end[1])),
                     float(max(start[0], end[0])), float(max(start[1], end[1]))],
        })
    return legs


def _boxes_overlap(a: Sequence[float], b: Sequence[float]) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _altitudes_overlap(seg_min: float, seg_max: float, zone_min: float, zone_max: float) -> bool:
    return seg_min < zone_max and zone_min < seg_max


def segment_restriction_conflicts(segments: Sequence[Mapping[str, Any]],
                                  restrictions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """逐段核对限制区水平范围与高度区间，时间过滤由调用方完成。

    每个重叠返回一条记录，明确航段编号与限制名称，顺序按航段编号排列。
    """
    conflicts: list[dict[str, Any]] = []
    for leg in segments:
        box = leg["bbox"]
        for zone in restrictions:
            zone_box = (zone["min_lon"], zone["min_lat"], zone["max_lon"], zone["max_lat"])
            if not _boxes_overlap(box, zone_box):
                continue
            if not _altitudes_overlap(leg["min_altitude"], leg["max_altitude"],
                                      float(zone["min_altitude"]), float(zone["max_altitude"])):
                continue
            label = KIND_LABELS.get(zone["kind"], "限制区")
            conflicts.append({
                "code": "airspace_restriction",
                "segment": leg["index"],
                "restriction_id": zone["id"],
                "name": zone["name"],
                "kind": zone["kind"],
                "reason": zone["reason"],
                "segment_altitude": [leg["min_altitude"], leg["max_altitude"]],
                "restriction_altitude": [float(zone["min_altitude"]), float(zone["max_altitude"])],
                "message": (f"航段 {leg['index']}（高度 {leg['min_altitude']:g}-{leg['max_altitude']:g}m）"
                            f"与{label}「{zone['name']}」（高度 {float(zone['min_altitude']):g}-"
                            f"{float(zone['max_altitude']):g}m）的水平范围和高度区间重叠"),
            })
    return conflicts


def altitude_violations(segments: Sequence[Mapping[str, Any]],
                        limit: float = SEGMENT_ALTITUDE_LIMIT) -> list[dict[str, Any]]:
    """逐段检查高度硬限制，定位到具体航段而不是只看计划最大高度。"""
    violations: list[dict[str, Any]] = []
    for leg in segments:
        if leg["max_altitude"] > limit:
            violations.append({
                "code": "altitude_limit",
                "segment": leg["index"],
                "max_altitude": leg["max_altitude"],
                "message": f"航段 {leg['index']} 最高高度 {leg['max_altitude']:g}m 超过 {limit:g}m 硬限制",
            })
    return violations


def changed_segment_indexes(old_points: Sequence[Sequence[float]],
                            new_points: Sequence[Sequence[float]]) -> list[int]:
    """返回发生改动的航段编号（端点经纬度或高度任一变化即算改动）。"""
    old_legs, new_legs = build_segments(old_points), build_segments(new_points)

    def key(leg: Mapping[str, Any]) -> tuple:
        return (leg["from"]["lon"], leg["from"]["lat"], leg["from"]["altitude"],
                leg["to"]["lon"], leg["to"]["lat"], leg["to"]["altitude"])

    changed = [i + 1 for i, (old, new) in enumerate(zip(old_legs, new_legs)) if key(old) != key(new)]
    changed.extend(range(len(old_legs) + 1, len(new_legs) + 1))
    return changed
