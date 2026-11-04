"""CI: the Apple Vision engine's landmarks on the test portrait look like MediaPipe's.

Same person, same photo: the posture measures from both must agree closely,
and left/right must follow MediaPipe's convention (the person's own left is
on the right of the picture), or every sideways check would be reversed.
"""

import json
import sys

from medisense.posture import measure

lines = [json.loads(x) for x in open(sys.argv[1]) if x.strip()]
ready = next(x for x in lines if x["type"] == "ready")
assert ready["backend"] == "vision", ready
pose = next(x for x in lines if x["type"] == "pose")
lm = pose["lm"]
visible = [i for i, p in enumerate(lm) if p[3] >= 0.5]
print("Vision: visible points", visible)
for i in (0, 2, 5, 7, 8, 11, 12):
    assert i in visible, f"point {i} not found"
assert lm[11][0] > lm[12][0], "the left shoulder must be on the image's right"
assert lm[0][1] < lm[11][1], "the nose must be above the shoulders"

ref = json.load(open(sys.argv[2]))
mv = measure(lm, None, pose["aspect"])
mm = measure(ref["lm"], None, ref["aspect"])
for key, tol in (("shoulder_roll", 4.0), ("head_roll", 6.0), ("head_height", 0.25), ("face_size", 0.25)):
    a, b = mv.get(key), mm.get(key)
    print(f"{key}: vision {a:.3f}  mediapipe {b:.3f}")
    assert a is not None and abs(a - b) <= tol * (abs(b) if key in ("head_height", "face_size") else 1), key
print("Apple Vision engine OK")
