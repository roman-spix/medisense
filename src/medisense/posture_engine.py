"""Background camera posture engine (runs in its own Python environment).

Started by MediSense / MediSense Posture as a separate process with the
.venv-posture interpreter (MediaPipe 0.10.21 + NumPy 1.26 + OpenCV 4.9: the
last versions with Intel-Mac support for macOS 11). It must not import the
rest of the medisense package, which uses NumPy 2.

It reads the camera at a few frames per second, runs MediaPipe Pose and
prints one JSON line per frame on stdout:

    {"type": "ready", "camera": 0, "w": 640, "h": 480, "fps": 4}
    {"type": "pose", "t": <unix time>, "lm": [[x, y, z, visibility] * 33], "world": [[x, y, z] * 33], "aspect": w/h}
    {"type": "nopose", "t": ...}
    {"type": "preview", "jpg": "<base64>"}   (only while preview is switched on)
    {"type": "error", "message": "..."}

Commands on stdin, one per line: "preview on", "preview off", "fps <n>", "quit".
Video frames never leave this process except as the optional small preview,
which is only sent to the local page while you look at it, and never stored.
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--check", action="store_true", help="only check that the libraries load (installer, doctor)")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--fps", type=float, default=4.0)
    ap.add_argument("--image", help="analyse this image instead of the camera (testing)")
    ap.add_argument("--frames", type=int, default=0, help="stop after this many frames (testing)")
    args = ap.parse_args()

    # MediaPipe's top-level import pulls in drawing helpers that need
    # matplotlib; pose detection never uses them, so a placeholder will do.
    for name in ("matplotlib", "matplotlib.pyplot"):
        sys.modules.setdefault(name, types.ModuleType(name))
    try:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision
    except Exception as e:  # pragma: no cover - depends on the install
        emit({"type": "error", "message": f"Posture engine could not start: {e}"})
        return 2
    if args.check:
        emit({"type": "ok", "mediapipe": mp.__version__, "opencv": cv2.__version__})
        return 0
    if not args.model:
        emit({"type": "error", "message": "no pose model given"})
        return 2

    try:
        landmarker = vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=args.model),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
        ))
    except Exception as e:
        emit({"type": "error", "message": f"Could not load the pose model: {e}"})
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
            emit({"type": "error", "message": f"Could not read {args.image}"})
            return 2
    else:
        backend = cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY
        cap = cv2.VideoCapture(args.camera, backend)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        ok, frame = (cap.read() if cap.isOpened() else (False, None))
        if not ok:
            emit({"type": "error", "message":
                  "The camera is not available. On a Mac: System Preferences → Security & Privacy → "
                  "Camera, allow Terminal; and close other apps using the camera."})
            return 3

    h, w = (still.shape[:2] if still is not None else (int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480,
                                                        int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640))
    emit({"type": "ready", "camera": args.camera, "w": w, "h": h, "fps": state["fps"]})

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
                    emit({"type": "error", "message": "The camera stopped delivering pictures."})
                    return 3
                time.sleep(0.1)
                continue
            failures = 0
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        ts_ms = int((time.monotonic() - t0) * 1000)
        res = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts_ms)
        now = time.time()
        if res.pose_landmarks:
            p, wl = res.pose_landmarks[0], res.pose_world_landmarks[0]
            emit({
                "type": "pose", "t": now, "aspect": rgb.shape[1] / rgb.shape[0],
                "lm": [[round(q.x, 4), round(q.y, 4), round(q.z, 4), round(q.visibility, 3)] for q in p],
                "world": [[round(q.x, 4), round(q.y, 4), round(q.z, 4)] for q in wl],
            })
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
