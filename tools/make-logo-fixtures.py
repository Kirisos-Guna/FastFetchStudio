"""Write a real sixel and block-art ladder, for the tests to draw.

Why this exists: the launcher chooses which file to draw by reading each
sixel's raster header, so a fixture that is not a real sixel only exercises the
failure path - every fit case would report "nothing fits" and the tests would
pass while measuring the wrong branch. This writes exactly what `apply_all()`
writes into a user's config: the same picture at every size in the ladder, each
one encoded at the cell size the tests pin.

    python tools/make-logo-fixtures.py --sixel-dir <dir> [--art-dir <dir>]
                                       --name demo [--cells 28x24]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app            # noqa: E402
import sixel_codec    # noqa: E402
from PIL import Image  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sixel-dir", required=True)
    p.add_argument("--art-dir", default="")
    p.add_argument("--name", required=True)
    p.add_argument("--cells", default="28x24")
    p.add_argument("--color", default="ff00ff", help="a colour no glyph is drawn in")
    args = p.parse_args()

    cells_w, cells_h = (int(v) for v in args.cells.lower().split("x"))
    color = tuple(int(args.color[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
    cell_px = {"cellW": app.CELL_PX_DEFAULT[0], "cellH": app.CELL_PX_DEFAULT[1]}
    # The picture fills the cells it is given, one column short of them, exactly
    # as a user's image does.
    img = Image.new("RGBA", ((cells_w - app.LOGO_SLACK_COLS) * cell_px["cellW"],
                             cells_h * cell_px["cellH"]), color)

    sixel_dir = Path(os.path.expandvars(args.sixel_dir))
    art_dir = Path(os.path.expandvars(args.art_dir)) if args.art_dir else sixel_dir
    sixel_dir.mkdir(parents=True, exist_ok=True)
    if args.art_dir:
        art_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for vw, vh in app.ladder_cells(cells_w, cells_h):
        tag = "" if (vw, vh) == (cells_w, cells_h) else f"-{vw}x{vh}"
        six = sixel_dir / f"{args.name}{tag}.sixel"
        six.write_bytes(sixel_codec.encode_sixel(app.fit_logo_cells(img, vw, vh, cell_px)))
        written.append(six)
        if args.art_dir or art_dir == sixel_dir:
            art = art_dir / f"{args.name}{tag}.art"
            art.write_text("\n".join(sixel_codec.render_ansi_art(img, vw, vh)) + "\n",
                           "utf-8", newline="\n")
            written.append(art)
    for f in written:
        print(f"wrote {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
