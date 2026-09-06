# Third-party notices

Memowheel / Trip Video is distributed under the MIT License (`LICENSE`). It
builds on third-party software and data with their own licenses, summarized below.

This file is a **convenience summary, not the authoritative license text.** The
binding terms are those shipped inside each installed package (in the packaged
build, under `_internal/`; from source, in your environment's `site-packages`).
Where a license is marked *(verify)*, confirm the exact terms of the version you
ship before relying on this summary.

## Python dependencies

| Package | License (typical) |
|---|---|
| fastapi | MIT |
| starlette | BSD-3-Clause |
| uvicorn | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| jinja2 | BSD-3-Clause |
| Pillow | HPND (permissive) |
| numpy | BSD-3-Clause |
| opencv-python | Apache-2.0 (also provides the YuNet/SFace face models — see below) |
| moviepy | MIT |
| imageio-ffmpeg | BSD-2-Clause (the bundled ffmpeg binary is separate — see below) |
| openai (SDK) | Apache-2.0 *(verify)* |
| anthropic (SDK) | MIT *(verify)* |
| python-dotenv | BSD-3-Clause |
| keyring | MIT |
| pywin32-ctypes | BSD-3-Clause |
| reverse_geocoder | *(verify — LGPL reported; bundles GeoNames data, see below)* |
| python-bidi | LGPL-3.0 *(verify)* |
| pystray | LGPL-3.0 *(verify)* |

### LGPL components (python-bidi, pystray, ffmpeg)
These are used as separate, dynamically-loaded libraries. If you redistribute the
packaged build, include their license texts (shipped with the packages) and honor
the LGPL's allowance for the user to replace/relink the component.

## Bundled data and models

### ffmpeg (via imageio-ffmpeg)
Video encoding uses an ffmpeg binary provided by `imageio-ffmpeg` (downloaded/
bundled by that package), **not** shipped in this source repository. ffmpeg is
licensed under the **LGPL/GPL**; include ffmpeg's license/notice when you
redistribute the packaged app. Confirm which build `imageio-ffmpeg` provides.

### GeoNames data (via reverse_geocoder)
Offline reverse geocoding uses city data derived from **GeoNames**, licensed
**CC-BY 4.0**. If you redistribute that dataset, provide attribution to GeoNames
(https://www.geonames.org/).

### Face models (YuNet + SFace, via OpenCV Zoo)
Face detection uses **YuNet** (`face_detection_yunet_2023mar.onnx`, **MIT**) and
face recognition uses **SFace** (`face_recognition_sface_2021dec.onnx`,
**Apache-2.0**), both from the [OpenCV Zoo](https://github.com/opencv/opencv_zoo).
The two ONNX files are bundled in this repository under `models/faces/` and run
through OpenCV's dnn module. Both licenses are permissive and place no
non-commercial restriction on the app's output; keep their notices when you
redistribute the models.

## Fonts

Bundled fonts are licensed under the **SIL Open Font License (OFL)**, with their
notices kept alongside the font files:

- Assistant — `review_app/static/fonts/Assistant-OFL.txt`,
  `generate/fonts/OFL-NOTICE.txt`
- Frank Ruhl Libre — `review_app/static/fonts/FrankRuhlLibre-OFL.txt`
- Cutive Mono — `review_app/static/fonts/CutiveMono-OFL.txt`
