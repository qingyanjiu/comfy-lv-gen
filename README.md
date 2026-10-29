# MiniMax H3 Chained Long Video

This repository turns a MiniMax H3 ComfyUI workflow into a resumable long-video
pipeline. Each segment has its own prompt, duration, and one-pass/two-pass
switch. The controller automatically chains the saved last frame of one segment
to the first frame of the next.

The controller is Python-based; ComfyUI still executes every diffusion and
audio step through its HTTP API. The repository includes the source ComfyUI UI
workflow and generated API graphs for both one-pass and two-pass execution.

## Included

- `run_h3_long_video.py`: stable long-video controller and ComfyUI graph builder
- `config.json`: the current 10-segment street-walk configuration
- `examples/dorm_bus_shopping_60s.json`: 12 × 5-second dorm, bus, and shopping example
- `example_workflows/minimax_h3_ui_source.json`: original ComfyUI UI workflow
- `api_workflows/segment_one_pass_api.json`: generated one-pass API graph
- `api_workflows/segment_two_pass_api.json`: generated two-pass API graph
- `run_test.bat`: render one missing segment without final stitching
- `run_full.bat`: render the complete missing chain and make the final video
- `open_config.bat`: edit prompts and per-segment settings in Notepad
- `H3LongVideoDirector`: a ComfyUI node that starts the same pipeline from a graph
- `example_workflows/H3-Long-Video-Director.json`: graph with reference images and storyboard
- `docs/CONFIGURATION.zh-CN.md`: complete Chinese configuration reference
- `docs/ARCHITECTURE.md`: execution and caching model

## ComfyUI requirements

The ComfyUI instance must already contain:

- MiniMax H3 model, CLIP, video VAE, audio VAE, and the TaoMate LoRA used in
  `config.json`
- `VRGDG_MiniMaxH3ImageReferenceToVideo`
- `MinimaxH3LatentUpscaler3D`
- `MMH3UltimateUpscale` and `MMH3TemporalSplitParams`
- VideoHelperSuite nodes used by the graph
- FFmpeg, or `imageio-ffmpeg`

Install the small controller dependencies in the ComfyUI Python environment:

```powershell
python -m pip install -r requirements.txt
```

The default batch scripts use:

`D:\Comfy-Desktop\ComfyUI-Installs\ComfyUI\ComfyUI\.venv\Scripts\python.exe`

If your environment is elsewhere, set `COMFYUI_PYTHON` before using the batch
files.

## Configuration

Double-click `open_config.bat`, then edit `segments` in `config.json`:

```json
{
  "seconds": 6.0,
  "two_pass": false,
  "prompt": "Description of this exact segment"
}
```

`seconds` and `prompt` are required. `two_pass` can be omitted to use
`two_pass_default`.

Important settings:

- `reference_images`: one to nine images for identity, outfit, and expression
- `stabilize_full_body`: keeps the complete head-to-shoes composition
- `opening_draft_seconds`: draft length used to create the first composition
  anchor
- `two_pass_steps` and `two_pass_denoise`: refinement strength
- `two_pass_fix_opening_frames`: striped frames replaced after 2x upscaling
- `final_width` and `final_height`: final normalized output size

The first segment is not copied from a reference image. The controller generates
a new 2-second H3 draft from the first prompt, extracts its first frame, and uses
that generated image as the full-body anchor. Segment two and later use the
previous segment's actual saved final frame.

## Run

Start ComfyUI first. Then:

1. Double-click `run_test.bat` to render one missing segment.
2. Inspect the segment or use the full-body and two-pass previews from the
   original local setup.
3. Double-click `run_full.bat` to finish the chain and create:

`D:/Comfy-Desktop/ComfyUI-Installs/ComfyUI/ComfyUI/output/h3_long_video/long_video.mp4`

The final file is not created early from an incomplete chain. Completed segments
are reused only when their generation settings and upstream result are unchanged.

## Run from a ComfyUI graph

Run `install_custom_node.bat` once, then restart ComfyUI. Open:

`example_workflows/H3-Long-Video-Director.json`

The graph contains:

- one required reference image input and up to eight optional inputs
- a `reference_notes` field that explains what every numbered reference image
  controls
- one-pass steps, two-pass steps, sampler, and scheduler controls
- a global two-pass selector: follow each segment, force all one-pass, or force
  all two-pass
- one storyboard widget containing arbitrary `seconds`, `two_pass`, and `prompt`
  entries
- an advanced override JSON widget for global settings

The sampler and scheduler controls apply to both passes. The second pass uses
the same scheduler and keeps temporal chunking enabled by default.

Connect or replace the reference images, edit the storyboard entries, then queue
the graph. The Director node returns a job ID immediately, and a background
process continues submitting the actual segment graphs to ComfyUI after the
outer graph finishes. This avoids the deadlock that would happen if one ComfyUI
prompt tried to wait for another prompt on the same server.

Job state and logs are written under:

`jobs/<job_id>/`

The Director still calls the same Python controller described above. The ComfyUI
graph is the control panel; each segment remains a normal local ComfyUI
execution.

The Director node panel polls the job in the background and displays:

- current status and completed segment count
- one playable video card for every completed segment
- the final merged video and a download link when stitching finishes
- the exact log path when a job fails

## CLI

```powershell
python run_h3_long_video.py --config config.json
python run_h3_long_video.py --config config.json --limit 1 --no-stitch
python run_h3_long_video.py --config config.json --force
python run_h3_long_video.py --config config.json --dump-graphs api_workflows
```

## Aspect ratio and resolution

The original `9:16 (Portrait Widescreen)` preset is preserved. Because the H3
node aligns dimensions to a 32-pixel grid, the one-pass base is 352×608 and the
two-pass/final output is 704×1216. Change those dimensions only if you
intentionally want a different output resolution.

## Privacy

Reference images, generated state, videos, and preview images are intentionally
not included in this repository.
