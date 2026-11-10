---
name: prepare-for-web
description: Prepare photos for a website or blog (resize to a max width, convert to WebP, keep originals). Use when asked to make images web-ready, smaller, or blog-friendly.
argument-hint: "<files or folder> [max width, default 1600]"
---

Request: $ARGUMENTS

1. List the images (jpg, jpeg, png, heic if Pillow can open it) in what the user named.
2. For each one, `image_info`; if wider than the max width (default 1600 px), `image_resize` to that width.
3. `image_convert` the result to `webp` into `workspace/out/web/`, same base name.
4. Report a table: file, original size, new size.
