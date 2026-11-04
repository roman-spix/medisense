"""Background camera posture engine (runs in its own Python environment).

Started by MediSense / MediSense Posture as a separate process with the
.venv-posture interpreter. It must not import the rest of the medisense
package. It reads the camera at a few frames per second, finds the body's
landmarks and prints one JSON line per frame on stdout:

    {"type": "ready", "camera": 0, "w": 640, "h": 480, "fps": 4, "backend": "vision"}
    {"type": "pose", "t": <unix time>, "lm": [[x, y, z, visibility] * 33], "world": [[x, y, z] * 33] | null, "aspect": w/h}
    {"type": "nopose", "t": ...}
    {"type": "preview", "jpg": "<base64>"}   (only while preview is switched on)
    {"type": "error", "kind": "engine" | "camera", "message": "..."}

Two ways to find the body, same output (MediaPipe's 33-point layout):

* macOS: Apple's Vision framework (VNDetectHumanBodyPoseRequest, built into
  macOS 11 and later) through PyObjC. Every MediaPipe build still on PyPI
  needs macOS 12.3 or newer (it imports C++ library functions macOS 11
  lacks), so on a Mac the engine uses what the system already has. Vision
  gives 2-D points only ("world" is null): the scorer then judges distance
  from the camera by the size of the face.
* elsewhere (development, CI): MediaPipe Pose Landmarker (full model).

The camera is read with OpenCV (AVFoundation on a Mac). Commands on stdin,
one per line: "preview on", "preview off", "fps <n>", "quit". Video frames
never leave this process except as the optional small preview, which is
only sent to the local page while you look at it, and never stored.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
import time
import types


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class VisionDetector:
    """Apple Vision body pose (macOS 11+), mapped onto MediaPipe's landmark indices."""

    name = "vision"
    # MediaPipe index -> Vision joint (the subject's own left/right, like MediaPipe)
    JOINTS = {0: "Nose", 2: "LeftEye", 5: "RightEye", 7: "LeftEar", 8: "RightEar",
              11: "LeftShoulder", 12: "RightShoulder", 13: "LeftElbow", 14: "RightElbow",
              15: "LeftWrist", 16: "RightWrist", 23: "LeftHip", 24: "RightHip",
              25: "LeftKnee", 26: "RightKnee", 27: "LeftAnkle", 28: "RightAnkle"}

    def __init__(self):
        import Foundation  # noqa: F401  (PyObjC)
        import Vision

        self.Vision = Vision
        self.NSData = Foundation.NSData
        self.joints = {i: getattr(Vision, "VNHumanBodyPoseObservationJointName" + n) for i, n in self.JOINTS.items()}
        Vision.VNDetectHumanBodyPoseRequest.alloc().init()  # fails before macOS 11
        self.version = "Apple Vision (macOS)"

    def detect(self, jpeg: bytes, ts_ms: int):
        V = self.Vision
        handler = V.VNImageRequestHandler.alloc().initWithData_options_(
            self.NSData.dataWithBytes_length_(jpeg, len(jpeg)), {})
        req = V.VNDetectHumanBodyPoseRequest.alloc().init()
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            raise RuntimeError(f"Vision: {err}")
        results = list(req.results() or [])
        if not results:
            return None, None
        obs = max(results, key=lambda o: float(o.confidence()))
        lm = [[0.0, 0.0, 0.0, 0.0] for _ in range(33)]
        for i, joint in self.joints.items():
            pt, _err = obs.recognizedPointForJointName_error_(joint, None)
            if pt is None:
                continue
            conf = float(pt.confidence())
            if conf <= 0:
                continue
            loc = pt.location()
            # Vision: normalised, origin at the bottom left. Confidence 0.3
            # and above counts as visible (the scorer's 0.5 visibility line).
            lm[i] = [round(float(loc.x), 4), round(1.0 - float(loc.y), 4), 0.0, round(min(1.0, conf / 0.6), 3)]
        return lm, None


class MediaPipeDetector:
    """MediaPipe Pose Landmarker (Linux and newer Macs)."""

    name = "mediapipe"

    def __init__(self, model: str):
        # MediaPipe's top-level import pulls in drawing helpers that need
        # matplotlib; pose detection never uses them, so a placeholder will do.
        for n in ("matplotlib", "matplotlib.pyplot"):
            sys.modules.setdefault(n, types.ModuleType(n))
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        self.mp = mp
        self.version = f"MediaPipe {mp.__version__}"
        self.landmarker = None
        if model:
            self.landmarker = vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=model),
                running_mode=vision.RunningMode.VIDEO,
                num_poses=1,
            ))

    def detect_rgb(self, rgb, ts_ms: int):
        res = self.landmarker.detect_for_video(self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb), ts_ms)
        if not res.pose_landmarks:
            return None, None
        p, wl = res.pose_landmarks[0], res.pose_world_landmarks[0]
        return ([[round(q.x, 4), round(q.y, 4), round(q.z, 4), round(q.visibility, 3)] for q in p],
                [[round(q.x, 4), round(q.y, 4), round(q.z, 4)] for q in wl])


def make_detector(backend: str, model: str | None):
    if backend == "vision" or (backend == "auto" and sys.platform == "darwin"):
        return VisionDetector()
    return MediaPipeDetector(model or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("auto", "vision", "mediapipe"), default="auto")
    ap.add_argument("--model", help="MediaPipe pose model (.task); not needed for Apple Vision")
    ap.add_argument("--check", action="store_true", help="only check that the engine works here (installer, doctor)")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--fps", type=float, default=4.0)
    ap.add_argument("--image", help="analyse this image instead of the camera (testing)")
    ap.add_argument("--frames", type=int, default=0, help="stop after this many frames (testing)")
    args = ap.parse_args()

    try:
        import cv2
        import numpy as np
        det = make_detector(args.backend, args.model)
    except Exception as e:  # pragma: no cover - depends on the install
        emit({"type": "error", "kind": "engine", "message": f"Posture engine could not start: {e}"})
        return 2
    if args.check:
        try:
            if det.name == "vision":  # run it once on a blank picture
                ok, blank = cv2.imencode(".jpg", np.zeros((120, 160, 3), dtype=np.uint8))
                det.detect(blank.tobytes(), 0)
        except Exception as e:
            emit({"type": "error", "kind": "engine", "message": f"Posture engine check failed: {e}"})
            return 2
        emit({"type": "ok", "backend": det.name, "engine": det.version, "opencv": cv2.__version__})
        return 0
    if det.name == "mediapipe" and det.landmarker is None:
        emit({"type": "error", "kind": "engine", "message": "no pose model given"})
        return 2

    state = {"preview": False, "fps": max(0.5, min(15.0, args.fps)), "quit": False}

    def read_commands() -> None:
        for line in sys.stdin:
            cmd = line.strip().split()
            if not cmd:
                continue
            if cmd[0] == "quit":
                state["quit"] = True
                return
            if cmd[0] == "preview":
                state["preview"] = len(cmd) > 1 and cmd[1] == "on"
            elif cmd[0] == "fps" and len(cmd) > 1:
                try:
                    state["fps"] = max(0.5, min(15.0, float(cmd[1])))
                except ValueError:
                    pass
        if not args.frames:
            state["quit"] = True  # the app went away (not in test runs)

    threading.Thread(target=read_commands, daemon=True).start()

    cap = None
    still = None
    if args.image:
        still = cv2.imread(args.image)
        if still is None:
            emit({"type": "error", "kind": "engine", "message": f"Could not read {args.image}"})
            return 2
    else:
        cam_backend = cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY
        cap = cv2.VideoCapture(args.camera, cam_backend)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        ok, frame = (cap.read() if cap.isOpened() else (False, None))
        if not ok:
            emit({"type": "error", "kind": "camera", "message":
                  "The camera is not available. On a Mac: System Preferences → Security & Privacy → "
                  "Camera, allow Terminal; and close other apps using the camera."})
            return 3

    h, w = (still.shape[:2] if still is not None else (int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480,
                                                        int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640))
    emit({"type": "ready", "camera": args.camera, "w": w, "h": h, "fps": state["fps"], "backend": det.name})

    t0 = time.monotonic()
    n = 0
    last_preview = 0.0
    failures = 0
    while not state["quit"]:
        start = time.monotonic()
        if still is not None:
            frame = still
        else:
            ok, frame = cap.read()
            if not ok:
                failures += 1
                if failures > 30:
                    emit({"type": "error", "kind": "camera", "message": "The camera stopped delivering pictures."})
                    return 3
                time.sleep(0.1)
                continue
            failures = 0
        ts_ms = int((time.monotonic() - t0) * 1000)
        try:
            if det.name == "vision":
                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                lm, world = det.detect(jpg.tobytes(), ts_ms) if ok else (None, None)
            else:
                lm, world = det.detect_rgb(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), ts_ms)
        except Exception as e:  # a bad frame: report, carry on
            emit({"type": "nopose", "t": time.time(), "note": str(e)[:200]})
            lm = None
        now = time.time()
        if lm is not None:
            emit({"type": "pose", "t": now, "aspect": frame.shape[1] / frame.shape[0], "lm": lm, "world": world})
        else:
            emit({"type": "nopose", "t": now})
        if state["preview"] and start - last_preview > 0.5:
            last_preview = start
            small = cv2.resize(frame, (320, int(320 * frame.shape[0] / frame.shape[1])))
            ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 60])
            if ok:
                emit({"type": "preview", "jpg": base64.b64encode(jpg.tobytes()).decode()})
        n += 1
        if args.frames and n >= args.frames:
            break
        time.sleep(max(0.0, 1.0 / state["fps"] - (time.monotonic() - start)))
    if cap is not None:
        cap.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
