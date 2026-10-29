# Architecture

This repository is a controller around ComfyUI. ComfyUI still performs every
MiniMax H3 diffusion, audio decode, and video encode.

For each segment, the controller:

1. Reads the segment prompt, duration, and two-pass flag from `config.json`.
2. Builds a fresh ComfyUI API graph.
3. Supplies the previous segment's saved last image as `first_frame` when the
   segment is not the first one.
4. Uses the generated full-body opening image as both boundaries of the first
   segment when `stabilize_full_body` is enabled.
5. Submits the graph to `/prompt`, waits for `/history`, and records the result
   in `.state.json`.
6. Uses FFmpeg to normalize and concatenate the completed segment videos.

The first segment deliberately has no fixed `first_frame` from
`reference_images`. A short H3 draft is generated first, its first frame is
saved as a new composition anchor, and the final first segment starts from that
generated anchor.

The two-pass path mirrors the source ComfyUI workflow:

`AV latent -> split video/audio -> 2x video latent upscale -> recombine -> MMH3 Ultimate Upscale`

The third-party upscaler can produce two striped frames at the start. The
controller replaces those frames with the segment's first-frame anchor before
recording the segment in the chain.

The ComfyUI UI does not natively track arbitrary segment prompts, durations,
per-segment two-pass switches, and upstream result hashes in one reusable
graph. Those orchestration concerns stay in Python so each segment remains a
normal ComfyUI execution and remains inspectable in ComfyUI history.

