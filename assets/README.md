# assets

Logos used by the workspace READMEs — two formats per module, one directory
each:

| Module | Files (in that module's `public/assets/images/`) | README references |
|---|---|---|
| workspace | `proxytoolpatch.webp` + `proxytoolpatch.png` | root `README.md` → **webp** |
| `proxytool` | `proxytool.webp` + `proxytool.png` | `apps/proxytool/README.md` → **webp** |
| `toolpatch` | `toolpatch.webp` + `toolpatch.png` | `packages/toolpatch/README.md` → **png** |

Format rules:

- **WebP** (~10–18 KB) is referenced by READMEs that render on GitHub, which
  supports webp natively — it is >80% smaller than the PNG equivalent.
- **PNG** (~18–36 KB) is what the `toolpatch` README references, because that
  README ships inside the wheel metadata and renders on PyPI, where WebP
  support is not proven. Keep the PNG and WebP in sync.
- All images have been downscaled to 512×512 and palette-quantized (Pillow).
  Regenerate after replacing a source image:

  ```bash
  uv run --with pillow python - <<'PY'
  from pathlib import Path
  from PIL import Image
  for stem in ("public/assets/images/proxytoolpatch",
               "apps/proxytool/public/assets/images/proxytool",
               "packages/toolpatch/public/assets/images/toolpatch"):
      src = Path(stem + ".png"); img = Image.open(src).convert("RGBA")
      img.thumbnail((512, 512), Image.LANCZOS)
      img.quantize(colors=256, method=Image.FASTOCTREE).save(src, optimize=True)
      img.save(Path(stem + ".webp"), "WEBP", quality=80, method=6)
  PY
  ```

- Name a new module's image after the module (`<module>.png` + `<module>.webp`);
  SVG is fine for authoring, but commit it alongside a raster export since
  some renderers (PyPI project pages, plain markdown readers) can't show it.

Purely presentation assets: they do not appear in the CLI, the wheel
metadata, or runtime — just the README headers (and the library's PyPI page),
where the relative paths resolve inside the repo.
