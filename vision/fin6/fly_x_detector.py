#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import cv2
import numpy as np

ImageInput = Union[str, Path, np.ndarray]
Point = Tuple[int, int]


def load_image_input(image_input: ImageInput, flags: int = cv2.IMREAD_COLOR) -> np.ndarray:
    """
    Accept either:
      - a filesystem path (str / Path)
      - a numpy array already in memory

    This makes the module usable both from CLI uploads and from another script
    that captures camera frames and passes arrays directly.
    """
    if isinstance(image_input, np.ndarray):
        image = image_input.copy()
        if flags == cv2.IMREAD_GRAYSCALE and image.ndim == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        elif flags == cv2.IMREAD_COLOR and image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        return image

    path = str(image_input)
    image = cv2.imread(path, flags)
    if image is None:
        raise FileNotFoundError(f"Could not read image: {path}")
    return image


def click_two_points(image_bgr: np.ndarray, window_name: str = "Click left point, then right point") -> Tuple[Point, Point]:
    points: List[Point] = []
    preview = image_bgr.copy()

    def on_mouse(event, x, y, flags, param):
        nonlocal preview, points
        if event == cv2.EVENT_LBUTTONDOWN and len(points) < 2:
            points.append((int(x), int(y)))
            cv2.circle(preview, (x, y), 4, (0, 255, 0), -1)
            if len(points) == 2:
                cv2.line(preview, points[0], points[1], (0, 255, 0), 1, cv2.LINE_AA)
            cv2.imshow(window_name, preview)

    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window_name, on_mouse)

    help_text = (
        "Left click the LEFT end of the channel axis, then the RIGHT end. "
        "Press ENTER to accept or R to reset."
    )
    while True:
        display = preview.copy()
        cv2.putText(display, help_text, (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 0), 2, cv2.LINE_AA)
        cv2.imshow(window_name, display)
        key = cv2.waitKey(20) & 0xFF
        if key in (13, 10) and len(points) == 2:
            break
        if key in (ord("r"), ord("R")):
            points = []
            preview = image_bgr.copy()
        if key == 27:
            cv2.destroyWindow(window_name)
            raise KeyboardInterrupt("Calibration cancelled.")

    cv2.destroyWindow(window_name)
    return points[0], points[1]


def save_calibration(
    path: Union[str, Path],
    left_pt: Point,
    right_pt: Point,
    channel_length_mm: float,
    crop_x_pad: Optional[int] = None,
    crop_above_px: Optional[int] = None,
    crop_below_px: Optional[int] = None,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "left_point_px": [int(left_pt[0]), int(left_pt[1])],
        "right_point_px": [int(right_pt[0]), int(right_pt[1])],
        "channel_length_mm": float(channel_length_mm),
    }
    if crop_x_pad is not None:
        data["crop_x_pad"] = int(crop_x_pad)
    if crop_above_px is not None:
        data["crop_above_px"] = int(crop_above_px)
    if crop_below_px is not None:
        data["crop_below_px"] = int(crop_below_px)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


def load_calibration_data(path: Union[str, Path]) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


def load_calibration(path: Union[str, Path]) -> Tuple[Point, Point, float]:
    data = load_calibration_data(path)
    left_pt = tuple(map(int, data["left_point_px"]))
    right_pt = tuple(map(int, data["right_point_px"]))
    channel_length_mm = float(data["channel_length_mm"])
    return left_pt, right_pt, channel_length_mm


def align_frame_to_background(bg_gray: np.ndarray, frame_gray: np.ndarray, frame_color: Optional[np.ndarray] = None):
    small_bg = cv2.resize(bg_gray, None, fx=0.5, fy=0.5)
    small_fr = cv2.resize(frame_gray, None, fx=0.5, fy=0.5)

    warp_small = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 1500, 1e-6)

    try:
        cv2.findTransformECC(
            small_bg.astype(np.float32) / 255.0,
            small_fr.astype(np.float32) / 255.0,
            warp_small,
            cv2.MOTION_EUCLIDEAN,
            criteria,
            None,
            5,
        )
    except cv2.error:
        warp_small = np.eye(2, 3, dtype=np.float32)

    warp_full = warp_small.copy()
    warp_full[:, 2] *= 2.0

    aligned_gray = cv2.warpAffine(
        frame_gray,
        warp_full,
        (bg_gray.shape[1], bg_gray.shape[0]),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
    )

    aligned_color = None
    if frame_color is not None:
        aligned_color = cv2.warpAffine(
            frame_color,
            warp_full,
            (bg_gray.shape[1], bg_gray.shape[0]),
            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        )

    return aligned_gray, aligned_color, warp_full


def build_channel_roi(shape, left_pt: Point, right_pt: Point, band_half_width: int):
    p0 = np.array(left_pt, dtype=np.float32)
    p1 = np.array(right_pt, dtype=np.float32)
    v = p1 - p0
    length_px = float(np.linalg.norm(v))
    if length_px < 1.0:
        raise ValueError("Calibration points are too close together.")

    u = v / length_px

    h, w = shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    rx = xx - p0[0]
    ry = yy - p0[1]

    t = rx * u[0] + ry * u[1]
    perp = np.abs(rx * (-u[1]) + ry * u[0])

    roi = (t >= -10) & (t <= length_px + 10) & (perp <= band_half_width)
    return roi, u, length_px


def detect_flies(
    bg_gray: np.ndarray,
    frame_gray: np.ndarray,
    left_pt: Point,
    right_pt: Point,
    channel_length_mm: float,
    band_half_width: int = 35,
    blackhat_ksize: int = 31,
    score_thresh: int = 20,
    min_area: int = 20,
    max_area: int = 1200,
    small_area_percentile: float = 10.0,
    merge_distance_px: int = 18,
):
    roi, axis_unit, axis_length_px = build_channel_roi(frame_gray.shape, left_pt, right_pt, band_half_width)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (blackhat_ksize, blackhat_ksize))
    blackhat_frame = cv2.morphologyEx(frame_gray, cv2.MORPH_BLACKHAT, kernel)
    blackhat_bg = cv2.morphologyEx(bg_gray, cv2.MORPH_BLACKHAT, kernel)
    score = cv2.subtract(blackhat_frame, blackhat_bg)

    mask = ((score >= score_thresh) & roi).astype(np.uint8) * 255

    k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    k5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k3)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k5)

    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    candidate_areas: List[int] = []
    for i in range(1, count):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if min_area <= area <= max_area:
            candidate_areas.append(area)

    effective_min_area = int(min_area)
    if len(candidate_areas) >= 2 and small_area_percentile > 0:
        percentile_floor = int(np.ceil(np.percentile(candidate_areas, small_area_percentile)))
        effective_min_area = max(effective_min_area, percentile_floor)

    detections: List[Dict[str, Any]] = []
    for i in range(1, count):
        x, y, w, h, area = stats[i]
        if not (effective_min_area <= area <= max_area):
            continue
        aspect = max(w, h) / max(1, min(w, h))
        if aspect > 8:
            continue

        cx, cy = centroids[i]
        dist_px = (cx - left_pt[0]) * axis_unit[0] + (cy - left_pt[1]) * axis_unit[1]
        if not (-5 <= dist_px <= axis_length_px + 5):
            continue

        dist_px = float(np.clip(dist_px, 0, axis_length_px))
        dist_mm = dist_px * channel_length_mm / axis_length_px

        detections.append(
            {
                "bbox": [int(x), int(y), int(w), int(h)],
                "center_xy_px": [float(cx), float(cy)],
                "x_along_channel_px": dist_px,
                "x_along_channel_mm": float(dist_mm),
                "area_px": int(area),
            }
        )

    detections.sort(key=lambda d: d["x_along_channel_px"])

    merged: List[Dict[str, Any]] = []
    for det in detections:
        if not merged or abs(det["x_along_channel_px"] - merged[-1]["x_along_channel_px"]) > merge_distance_px:
            merged.append(det.copy())
            continue

        prev = merged[-1]
        a1 = prev["area_px"]
        a2 = det["area_px"]

        x1 = min(prev["bbox"][0], det["bbox"][0])
        y1 = min(prev["bbox"][1], det["bbox"][1])
        x2 = max(prev["bbox"][0] + prev["bbox"][2], det["bbox"][0] + det["bbox"][2])
        y2 = max(prev["bbox"][1] + prev["bbox"][3], det["bbox"][1] + det["bbox"][3])

        cx = (prev["center_xy_px"][0] * a1 + det["center_xy_px"][0] * a2) / (a1 + a2)
        cy = (prev["center_xy_px"][1] * a1 + det["center_xy_px"][1] * a2) / (a1 + a2)
        dist_px = (prev["x_along_channel_px"] * a1 + det["x_along_channel_px"] * a2) / (a1 + a2)
        dist_mm = (prev["x_along_channel_mm"] * a1 + det["x_along_channel_mm"] * a2) / (a1 + a2)

        prev.update(
            {
                "bbox": [int(x1), int(y1), int(x2 - x1), int(y2 - y1)],
                "center_xy_px": [float(cx), float(cy)],
                "x_along_channel_px": float(dist_px),
                "x_along_channel_mm": float(dist_mm),
                "area_px": int(a1 + a2),
            }
        )

    for idx, det in enumerate(merged, start=1):
        det["index"] = idx

    return merged, mask, score


def transform_points(points: Sequence[Sequence[float]], matrix_2x3: np.ndarray) -> np.ndarray:
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
    return cv2.transform(pts, matrix_2x3).reshape(-1, 2)


def rotate_image(image: np.ndarray, angle_deg: float, border_value=0):
    h, w = image.shape[:2]
    center = (w / 2.0, h / 2.0)
    rot = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    rotated = cv2.warpAffine(
        image,
        rot,
        (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border_value,
    )
    return rotated, rot


def transform_bbox(bbox: Sequence[int], matrix_2x3: np.ndarray) -> List[int]:
    x, y, w, h = bbox
    corners = np.array(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
        dtype=np.float32,
    )
    tc = transform_points(corners, matrix_2x3)
    min_xy = np.floor(tc.min(axis=0)).astype(int)
    max_xy = np.ceil(tc.max(axis=0)).astype(int)
    return [int(min_xy[0]), int(min_xy[1]), int(max_xy[0] - min_xy[0]), int(max_xy[1] - min_xy[1])]


def estimate_channel_crop_from_background(
    background_gray: np.ndarray,
    left_pt: Point,
    right_pt: Point,
    crop_x_pad: Optional[int] = None,
    profile_threshold_fraction: float = 0.25,
    profile_margin_px: int = 8,
) -> Tuple[int, int, int, Dict[str, Any]]:
    """
    Auto-estimate the crop around the manually selected channel axis.

    The user only clicks the axis endpoints on the empty/background image.
    The script rotates the background so the axis is horizontal, builds a
    vertical intensity profile across the axis span, and finds the bright
    channel band containing the axis.
    """
    if background_gray.ndim != 2:
        background_gray = cv2.cvtColor(background_gray, cv2.COLOR_BGR2GRAY)

    axis_vec = np.array([right_pt[0] - left_pt[0], right_pt[1] - left_pt[1]], dtype=np.float32)
    axis_length_px = float(np.linalg.norm(axis_vec))
    if axis_length_px < 1.0:
        raise ValueError("Calibration points are too close together.")

    if crop_x_pad is None:
        crop_x_pad = max(12, int(round(axis_length_px * 0.02)))

    angle_deg = float(np.degrees(np.arctan2(axis_vec[1], axis_vec[0])))
    rotated_bg, rot = rotate_image(background_gray, angle_deg, border_value=0)

    pts_rot = transform_points([left_pt, right_pt], rot)
    left_rot, right_rot = pts_rot[0], pts_rot[1]
    x0 = int(np.floor(min(left_rot[0], right_rot[0]) - crop_x_pad))
    x1 = int(np.ceil(max(left_rot[0], right_rot[0]) + crop_x_pad))
    x0 = max(0, x0)
    x1 = min(rotated_bg.shape[1], x1)

    if x1 <= x0 + 5:
        fallback_above = max(60, int(round(axis_length_px * 0.18)))
        fallback_below = max(120, int(round(axis_length_px * 0.28)))
        meta = {
            "auto_crop_used": False,
            "fallback_reason": "axis span too small after rotation",
            "rotation_angle_deg": angle_deg,
        }
        return crop_x_pad, fallback_above, fallback_below, meta

    profile = rotated_bg[:, x0:x1].mean(axis=1).astype(np.float32)
    ksize = max(21, int(round(axis_length_px * 0.06)))
    if ksize % 2 == 0:
        ksize += 1
    profile_smooth = cv2.GaussianBlur(profile.reshape(-1, 1), (1, ksize), 0).ravel()

    y_ref = int(round((left_rot[1] + right_rot[1]) / 2.0))
    h = rotated_bg.shape[0]
    edge_band = min(120, max(20, h // 10))
    bg_level = float(np.median(np.concatenate([profile_smooth[:edge_band], profile_smooth[-edge_band:]])))
    peak_level = float(profile_smooth.max())
    thresh = bg_level + profile_threshold_fraction * max(1.0, peak_level - bg_level)

    ys = np.where(profile_smooth >= thresh)[0]
    row_top = None
    row_bottom = None
    if ys.size > 0:
        splits = np.where(np.diff(ys) > 1)[0] + 1
        groups = np.split(ys, splits)
        for g in groups:
            if g.size and g[0] <= y_ref <= g[-1]:
                row_top = int(g[0])
                row_bottom = int(g[-1])
                break

    if row_top is None or row_bottom is None:
        row_top = max(0, y_ref - max(60, int(round(axis_length_px * 0.18))))
        row_bottom = min(h - 1, y_ref + max(120, int(round(axis_length_px * 0.28))))
        auto_used = False
        reason = "no bright band crossing axis"
    else:
        auto_used = True
        reason = None

    crop_above_px = int(max(10, y_ref - row_top + profile_margin_px))
    crop_below_px = int(max(10, row_bottom - y_ref + profile_margin_px))

    meta = {
        "auto_crop_used": auto_used,
        "fallback_reason": reason,
        "rotation_angle_deg": angle_deg,
        "profile_threshold": float(thresh),
        "profile_background_level": bg_level,
        "profile_peak_level": peak_level,
        "profile_row_top": int(row_top),
        "profile_row_bottom": int(row_bottom),
        "axis_y_rotated": int(y_ref),
        "crop_x_span_rotated": [int(x0), int(x1)],
    }
    return int(crop_x_pad), crop_above_px, crop_below_px, meta


def compute_channel_crop(
    image_shape: Tuple[int, ...],
    left_pt: Point,
    right_pt: Point,
    crop_x_pad: int = 25,
    crop_above_px: Optional[int] = None,
    crop_below_px: Optional[int] = None,
):
    axis_vec = np.array([right_pt[0] - left_pt[0], right_pt[1] - left_pt[1]], dtype=np.float32)
    axis_length_px = float(np.linalg.norm(axis_vec))
    if axis_length_px < 1.0:
        raise ValueError("Calibration points are too close together.")

    if crop_above_px is None:
        crop_above_px = max(60, int(round(axis_length_px * 0.18)))
    if crop_below_px is None:
        crop_below_px = max(120, int(round(axis_length_px * 0.28)))

    angle_deg = float(np.degrees(np.arctan2(axis_vec[1], axis_vec[0])))

    border_value = (0, 0, 0) if len(image_shape) == 3 else 0
    dummy = np.zeros(image_shape[:2], dtype=np.uint8)
    _, rot = rotate_image(dummy, angle_deg, border_value=0)

    pts_rot = transform_points([left_pt, right_pt], rot)
    left_rot, right_rot = pts_rot[0], pts_rot[1]
    y_ref = (left_rot[1] + right_rot[1]) / 2.0

    x0 = int(np.floor(min(left_rot[0], right_rot[0]) - crop_x_pad))
    x1 = int(np.ceil(max(left_rot[0], right_rot[0]) + crop_x_pad))
    y0 = int(np.floor(y_ref - crop_above_px))
    y1 = int(np.ceil(y_ref + crop_below_px))

    h, w = image_shape[:2]
    x0 = max(0, x0)
    y0 = max(0, y0)
    x1 = min(w, x1)
    y1 = min(h, y1)

    crop_rect = (x0, y0, x1, y1)
    return angle_deg, rot, crop_rect, int(crop_above_px), int(crop_below_px), float(axis_length_px)


def annotate_and_crop_channel(
    image_bgr: np.ndarray,
    mask: np.ndarray,
    detections: List[Dict[str, Any]],
    left_pt: Point,
    right_pt: Point,
    crop_x_pad: int = 25,
    crop_above_px: Optional[int] = None,
    crop_below_px: Optional[int] = None,
):
    angle_deg, rot, crop_rect, crop_above_px, crop_below_px, axis_length_px = compute_channel_crop(
        image_bgr.shape,
        left_pt,
        right_pt,
        crop_x_pad=crop_x_pad,
        crop_above_px=crop_above_px,
        crop_below_px=crop_below_px,
    )

    rotated_img, _ = rotate_image(image_bgr, angle_deg, border_value=(0, 0, 0))
    rotated_mask, _ = rotate_image(mask, angle_deg, border_value=0)

    x0, y0, x1, y1 = crop_rect
    cropped_img = rotated_img[y0:y1, x0:x1].copy()
    cropped_mask = rotated_mask[y0:y1, x0:x1].copy()

    blue = (255, 0, 0)
    white = (255, 255, 255)
    black = (0, 0, 0)

    transformed_detections: List[Dict[str, Any]] = []
    for det in detections:
        bbox_rot = transform_bbox(det["bbox"], rot)
        bx, by, bw, bh = bbox_rot
        bx -= x0
        by -= y0

        det_copy = dict(det)
        det_copy["bbox_cropped_px"] = [int(bx), int(by), int(bw), int(bh)]
        transformed_detections.append(det_copy)

        cv2.rectangle(cropped_img, (bx, by), (bx + bw, by + bh), blue, 1)
        label = f"x={det['x_along_channel_mm']:.1f} mm"
        text_x = bx
        text_y = by - 5 if by > 18 else by + bh + 13
        cv2.putText(cropped_img, label, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.38, white, 2, cv2.LINE_AA)
        cv2.putText(cropped_img, label, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.38, blue, 1, cv2.LINE_AA)

    status = "YES" if detections else "NO"
    header = f"Fly remaining: {status}   Count: {len(detections)}"
    cv2.putText(cropped_img, header, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, white, 3, cv2.LINE_AA)
    cv2.putText(cropped_img, header, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, black, 1, cv2.LINE_AA)

    crop_meta = {
        "rotation_angle_deg": float(angle_deg),
        "crop_rect_rotated_xyxy": [int(x0), int(y0), int(x1), int(y1)],
        "crop_above_px": int(crop_above_px),
        "crop_below_px": int(crop_below_px),
        "crop_x_pad": int(crop_x_pad),
        "axis_length_px": float(axis_length_px),
    }

    return cropped_img, cropped_mask, transformed_detections, crop_meta


def process_fly_detection(
    background: ImageInput,
    frame: ImageInput,
    calibration_path: Optional[Union[str, Path]] = None,
    left_pt: Optional[Point] = None,
    right_pt: Optional[Point] = None,
    channel_mm: float = 111.0,
    band_half_width: int = 35,
    score_thresh: int = 20,
    no_align: bool = False,
    crop_x_pad: Optional[int] = None,
    crop_above_px: Optional[int] = None,
    crop_below_px: Optional[int] = None,
) -> Tuple[Dict[str, Any], np.ndarray, np.ndarray]:
    """
    Main programmatic API.

    Example from another script:
        result, annotated, mask = process_fly_detection(bg_frame, live_frame, left_pt=(430,475), right_pt=(1030,440))

    background / frame may each be either a path or a numpy array.
    """
    bg_color = load_image_input(background, cv2.IMREAD_COLOR)
    frame_color = load_image_input(frame, cv2.IMREAD_COLOR)

    crop_auto_meta: Dict[str, Any] = {}

    if calibration_path is not None:
        cal = load_calibration_data(calibration_path)
        if left_pt is None:
            left_pt = tuple(map(int, cal["left_point_px"]))
        if right_pt is None:
            right_pt = tuple(map(int, cal["right_point_px"]))
        channel_mm = float(cal.get("channel_length_mm", channel_mm))
        if crop_x_pad is None:
            crop_x_pad = cal.get("crop_x_pad")
        if crop_above_px is None:
            crop_above_px = cal.get("crop_above_px")
        if crop_below_px is None:
            crop_below_px = cal.get("crop_below_px")

    if left_pt is None or right_pt is None:
        raise ValueError("Provide left_pt and right_pt, or provide calibration_path.")

    bg_gray = cv2.cvtColor(bg_color, cv2.COLOR_BGR2GRAY)
    frame_gray = cv2.cvtColor(frame_color, cv2.COLOR_BGR2GRAY)

    if crop_x_pad is None or crop_above_px is None or crop_below_px is None:
        est_x_pad, est_above, est_below, crop_auto_meta = estimate_channel_crop_from_background(
            bg_gray,
            left_pt,
            right_pt,
            crop_x_pad=crop_x_pad,
        )
        if crop_x_pad is None:
            crop_x_pad = est_x_pad
        if crop_above_px is None:
            crop_above_px = est_above
        if crop_below_px is None:
            crop_below_px = est_below

    if no_align:
        aligned_gray = frame_gray
        aligned_color = frame_color
        warp = np.eye(2, 3, dtype=np.float32)
    else:
        aligned_gray, aligned_color, warp = align_frame_to_background(bg_gray, frame_gray, frame_color)

    detections, mask, _ = detect_flies(
        bg_gray=bg_gray,
        frame_gray=aligned_gray,
        left_pt=left_pt,
        right_pt=right_pt,
        channel_length_mm=channel_mm,
        band_half_width=band_half_width,
        score_thresh=score_thresh,
    )

    annotated_cropped, cropped_mask, transformed_detections, crop_meta = annotate_and_crop_channel(
        aligned_color,
        mask,
        detections,
        left_pt,
        right_pt,
        crop_x_pad=int(crop_x_pad),
        crop_above_px=int(crop_above_px),
        crop_below_px=int(crop_below_px),
    )

    result = {
        "fly_remaining": bool(detections),
        "count": len(detections),
        "x_positions_mm": [round(d["x_along_channel_mm"], 3) for d in detections],
        "x_positions_px": [round(d["x_along_channel_px"], 3) for d in detections],
        "detections": transformed_detections,
        "left_point_px": [int(left_pt[0]), int(left_pt[1])],
        "right_point_px": [int(right_pt[0]), int(right_pt[1])],
        "channel_length_mm": float(channel_mm),
        "alignment_warp_2x3": warp.tolist(),
        "cropped_output": crop_meta,
        "auto_crop_from_background": crop_auto_meta,
        "input_shapes": {
            "background_bgr": [int(bg_color.shape[0]), int(bg_color.shape[1]), int(bg_color.shape[2])],
            "frame_bgr": [int(frame_color.shape[0]), int(frame_color.shape[1]), int(frame_color.shape[2])],
            "background_gray": [int(bg_gray.shape[0]), int(bg_gray.shape[1])],
            "frame_gray": [int(frame_gray.shape[0]), int(frame_gray.shape[1])],
            "aligned_gray": [int(aligned_gray.shape[0]), int(aligned_gray.shape[1])],
            "aligned_color": [int(aligned_color.shape[0]), int(aligned_color.shape[1]), int(aligned_color.shape[2])],
            "mask": [int(mask.shape[0]), int(mask.shape[1])],
            "annotated_cropped": [int(annotated_cropped.shape[0]), int(annotated_cropped.shape[1]), int(annotated_cropped.shape[2])],
            "cropped_mask": [int(cropped_mask.shape[0]), int(cropped_mask.shape[1])],
        },
        "detection_debug": {
            "detections_before_annotation": int(len(detections)),
            "annotation_output_mode": "cropped_channel_roi",
        },
    }

    return result, annotated_cropped, cropped_mask


def main():
    parser = argparse.ArgumentParser(description="Detect fruit flies and report x-position along a calibrated 111 mm channel.")
    parser.add_argument("--background", required=True, help="Path to background / empty-channel image")
    parser.add_argument("--frame", required=True, help="Path to image containing flies")
    parser.add_argument("--calibration", help="Path to calibration JSON. If missing and --interactive is used, it will be created.")
    parser.add_argument("--interactive", action="store_true", help="Click the left and right ends of the channel axis on the background image")
    parser.add_argument("--channel-mm", type=float, default=111.0, help="Known channel length in mm")
    parser.add_argument("--band-half-width", type=int, default=35, help="Half-width of the search band around the calibrated axis")
    parser.add_argument("--score-thresh", type=int, default=20, help="Detection threshold")
    parser.add_argument("--no-align", action="store_true", help="Skip ECC frame-to-background alignment")
    parser.add_argument("--crop-x-pad", type=int, default=None, help="Optional manual left/right crop padding. Leave unset to auto-estimate during calibration.")
    parser.add_argument("--crop-above-px", type=int, default=None, help="Optional manual crop height above the selected axis. Leave unset to auto-estimate.")
    parser.add_argument("--crop-below-px", type=int, default=None, help="Optional manual crop height below the selected axis. Leave unset to auto-estimate.")
    parser.add_argument("--out-image", default="annotated_detection.png", help="Output annotated cropped image path")
    parser.add_argument("--out-json", default="fly_results.json", help="Output JSON path")
    parser.add_argument("--out-mask", default="fly_mask.png", help="Output cropped binary mask path")
    args = parser.parse_args()

    bg_color = load_image_input(args.background, cv2.IMREAD_COLOR)
    bg_gray = cv2.cvtColor(bg_color, cv2.COLOR_BGR2GRAY)

    if args.interactive:
        left_pt, right_pt = click_two_points(bg_color)

        if args.crop_x_pad is None or args.crop_above_px is None or args.crop_below_px is None:
            est_x_pad, est_above, est_below, _ = estimate_channel_crop_from_background(
                bg_gray,
                left_pt,
                right_pt,
                crop_x_pad=args.crop_x_pad,
            )
            if args.crop_x_pad is None:
                args.crop_x_pad = est_x_pad
            if args.crop_above_px is None:
                args.crop_above_px = est_above
            if args.crop_below_px is None:
                args.crop_below_px = est_below

        if args.calibration:
            save_calibration(
                args.calibration,
                left_pt,
                right_pt,
                args.channel_mm,
                crop_x_pad=args.crop_x_pad,
                crop_above_px=args.crop_above_px,
                crop_below_px=args.crop_below_px,
            )
    elif args.calibration:
        cal = load_calibration_data(args.calibration)
        left_pt = tuple(map(int, cal["left_point_px"]))
        right_pt = tuple(map(int, cal["right_point_px"]))
        args.channel_mm = float(cal.get("channel_length_mm", args.channel_mm))
        if args.crop_x_pad is None:
            args.crop_x_pad = cal.get("crop_x_pad")
        if args.crop_above_px is None:
            args.crop_above_px = cal.get("crop_above_px")
        if args.crop_below_px is None:
            args.crop_below_px = cal.get("crop_below_px")
    else:
        raise ValueError("Use either --calibration or --interactive --calibration.")

    result, annotated, mask = process_fly_detection(
        background=args.background,
        frame=args.frame,
        left_pt=left_pt,
        right_pt=right_pt,
        channel_mm=args.channel_mm,
        band_half_width=args.band_half_width,
        score_thresh=args.score_thresh,
        no_align=args.no_align,
        crop_x_pad=args.crop_x_pad,
        crop_above_px=args.crop_above_px,
        crop_below_px=args.crop_below_px,
    )

    Path(args.out_image).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_mask).parent.mkdir(parents=True, exist_ok=True)

    cv2.imwrite(args.out_image, annotated)
    cv2.imwrite(args.out_mask, mask)

    result["annotated_image"] = str(Path(args.out_image).resolve())
    result["mask_image"] = str(Path(args.out_mask).resolve())

    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
