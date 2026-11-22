# SPDX-License-Identifier: Apache-2.0
"""Draws the Oaken Core mark, an oak log lying on its side with its cut face showing (original art, not the oaken-mail logo):
python make_mark.py OUTDIR. Needs Pillow.

Bark, a thin cambium, pale sapwood, darker heartwood, uneven growth rings, medullary rays and a
couple of radial checks (drying cracks). Seeded, so the output is the same every run."""
import math
import random
import sys

from PIL import Image, ImageDraw

S = 6  # supersample
BARK, BARK_LT = (74, 49, 28), (112, 79, 48)
CAMBIUM = (196, 150, 98)
SAP, HEART = (228, 190, 132), (170, 112, 58)
RING = (128, 80, 40)


def wob(rng, base_amp):
    """A smooth closed radius wobble: a few low harmonics with random phase."""
    ph = [rng.uniform(0, math.tau) for _ in range(4)]
    amp = [base_amp * a for a in (1.0, 0.6, 0.35, 0.2)]
    return lambda t: 1 + sum(a * math.sin((k + 2) * t + p) for k, (a, p) in enumerate(zip(amp, ph)))


def poly(cx, cy, r, f, n=360, jit=None):
    pts = []
    for i in range(n):
        t = math.tau * i / n
        rr = r * f(t) * (1 + (jit[i] if jit else 0))
        pts.append((cx + rr * math.cos(t), cy + rr * math.sin(t)))
    return pts


def log(size: int, bg=None) -> Image.Image:
    rng = random.Random(7)
    N = size * S
    im = Image.new("RGBA", (N, N), bg or (0, 0, 0, 0))
    d = ImageDraw.Draw(im, "RGBA")
    cx, cy, R = N / 2, N / 2, N * 0.47
    ring_w = max(1, int(N * 0.006))
    # bark: ragged edge
    jit = [rng.uniform(-0.018, 0.018) for _ in range(360)]
    for i in range(1, 360):  # smooth the jitter a little so the edge is rough, not hairy
        jit[i] = 0.6 * jit[i - 1] + 0.4 * jit[i]
    shape = wob(rng, 0.025)
    d.polygon(poly(cx, cy, R, shape, jit=jit), fill=BARK)
    # bark fissures: short darker/lighter strokes running radially near the edge
    for _ in range(46):
        t = rng.uniform(0, math.tau)
        r0, r1 = R * rng.uniform(0.90, 0.93), R * rng.uniform(0.96, 0.99)
        d.line([(cx + r0 * math.cos(t), cy + r0 * math.sin(t)), (cx + r1 * math.cos(t + rng.uniform(-.03, .03)), cy + r1 * math.sin(t))], fill=BARK_LT, width=ring_w)
    # wood disk, off-centre pith
    wood_r = R * 0.88
    px, py = cx + N * 0.035, cy - N * 0.02
    wood = wob(rng, 0.03)
    d.polygon(poly(cx, cy, wood_r, wood), fill=CAMBIUM)
    # concentric growth rings: radii uneven (narrow late rings, wide early ones), centre drifts to the pith
    radii, r = [], 0.0
    while r < 0.97:
        r += rng.uniform(0.045, 0.11) * (1.0 - 0.45 * r)
        radii.append(min(r, 0.97))
    base = wob(rng, 0.035)  # rings share one outline and differ only a little, so they nest and never cross
    own = [wob(rng, 0.008) for _ in radii]
    shapes = [(lambda t, o=o: base(t) * o(t)) for o in own]
    for x, f in reversed(list(zip(radii, shapes))):
        k = min(1.0, max(0.0, (x - 0.35) / 0.4))  # heartwood to sapwood across the middle third
        col = tuple(int(HEART[i] + (SAP[i] - HEART[i]) * k) for i in range(3))
        ccx, ccy = px + (cx - px) * x, py + (cy - py) * x
        d.polygon(poly(ccx, ccy, wood_r * x, f), fill=col)
    for x, f in zip(radii, shapes):
        ccx, ccy = px + (cx - px) * x, py + (cy - py) * x
        d.polygon(poly(ccx, ccy, wood_r * x, f), outline=RING + (200,), width=ring_w)
    # medullary rays: thin pale lines from near the pith to the cambium
    for _ in range(30):
        t = rng.uniform(0, math.tau)
        a, b = rng.uniform(0.25, 0.6), rng.uniform(0.75, 0.96)
        d.line([(px + wood_r * a * math.cos(t), py + wood_r * a * math.sin(t)), (cx + wood_r * b * math.cos(t), cy + wood_r * b * math.sin(t))], fill=(255, 238, 205, 70), width=max(1, ring_w // 2))
    # radial checks: two tapered wedges from the edge toward the pith
    for t, ln in ((math.radians(28), 0.62), (math.radians(205), 0.5)):
        w = 0.05
        tip = (px + wood_r * (1 - ln) * math.cos(t), py + wood_r * (1 - ln) * math.sin(t))
        a = (cx + wood_r * 1.02 * math.cos(t - w), cy + wood_r * 1.02 * math.sin(t - w))
        b = (cx + wood_r * 1.02 * math.cos(t + w), cy + wood_r * 1.02 * math.sin(t + w))
        d.polygon([tip, a, b], fill=BARK)
    # pith: a small dark knot of tight rings, off the geometric centre
    pr = N * 0.016
    d.ellipse([px - pr, py - pr, px + pr, py + pr], fill=RING)
    return im.resize((size, size), Image.LANCZOS)


def lying_log(w: int, h: int, bg=None) -> Image.Image:
    """The log on its side: bark body running left, the cross section on the right as a foreshortened ellipse."""
    rng = random.Random(11)
    Z = 4
    W, H = w * Z, h * Z
    im = Image.new("RGBA", (W, H), bg or (0, 0, 0, 0))
    d = ImageDraw.Draw(im, "RGBA")
    fh = int(H * 0.94)  # face height
    fw = int(fh * 0.68)  # foreshortened width
    length = int(W * 0.5)  # body length from the face back to the far end
    fcx = W - fw // 2 - int(W * 0.03)
    cy = H // 2
    top, bot = cy - fh // 2, cy + fh // 2
    x0 = fcx - length
    # far end: a bark ellipse, then the body as a rectangle joining it to the face
    d.ellipse([x0 - fw // 2, top, x0 + fw // 2, bot], fill=BARK)
    d.rectangle([x0, top, fcx, bot], fill=BARK)
    # shading and furrows run on their own layer, clipped to the body so nothing spills past its edge
    body = im
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im, "RGBA")
    for i in range(22):
        y = top + (bot - top) * (i + rng.uniform(0.2, 0.8)) / 22
        xa = x0 + rng.uniform(0, length * 0.15)
        xb = fcx - rng.uniform(0, length * 0.2)
        pts = [(x, y + math.sin(x / (W * 0.05) + i) * H * 0.006) for x in range(int(xa), int(xb), max(2, Z * 2))]
        if len(pts) > 1:
            d.line(pts, fill=BARK_LT + (200,) if i % 3 else (40, 26, 14, 200), width=max(1, Z))
    for k in range(40):  # darker light/shadow bands: lit from the top
        a = int(110 * (1 - k / 40))
        d.line([(x0, top + k), (fcx, top + k)], fill=(255, 235, 200, a // 4), width=1)
        d.line([(x0, bot - k), (fcx, bot - k)], fill=(20, 12, 6, a), width=1)
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse([x0 - fw // 2, top, x0 + fw // 2, bot], fill=255)
    md.rectangle([x0, top, fcx, bot], fill=255)
    im.putalpha(Image.composite(im.getchannel("A"), Image.new("L", (W, H), 0), mask))
    body.alpha_composite(im)
    im = body
    d = ImageDraw.Draw(im, "RGBA")
    d.arc([x0 - fw // 2, top, x0 + fw // 2, bot], 90, 270, fill=(40, 26, 14, 255), width=max(2, Z))
    # a stub where a branch was cut flush
    kx, ky, kr = x0 + int(length * 0.38), top + int(fh * 0.28), int(fh * 0.07)
    d.ellipse([kx - kr, ky - kr, kx + kr, ky + kr], fill=(40, 26, 14, 255), outline=BARK_LT, width=max(1, Z))
    face = log(fh // Z).resize((fw, fh), Image.LANCZOS)
    im.alpha_composite(face, (fcx - fw // 2, top))
    return im.resize((w, h), Image.LANCZOS)


if __name__ == "__main__":
    out = sys.argv[1]
    lying_log(320, 212).save(f"{out}/logo.png")
    f = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    f.alpha_composite(lying_log(124, 82), (2, 23))
    f.save(f"{out}/favicon.png")
    t = Image.new("RGBA", (180, 180), (251, 251, 248, 255))
    t.alpha_composite(lying_log(164, 108), (8, 36))
    t.convert("RGB").save(f"{out}/apple-touch-icon.png")
