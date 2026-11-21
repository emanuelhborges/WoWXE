import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageChops
import sys
from pathlib import Path
# usage: python tools/make_icon.py <2560x720 picture with the WoW Forever logo in the middle>
ROOT = Path(__file__).resolve().parent.parent
src = Image.open(sys.argv[1] if len(sys.argv) > 1 else ROOT / "widget" / "resources" / "standby.jpg").convert("RGB")
CX, CY = 860 + 416, 365          # medallion centre in the source picture
H = 335
box = (CX - H, CY - H, CX + H, CY + H)
im = src.crop(box)               # 670 x 670
w = im.size[0]
# rebuild the bottom half from the clean top half (the word FOREVER covers the lower ring)
top = im.crop((0, 0, w, H))
out = Image.new("RGB", (w, w))
out.paste(top, (0, 0))
out.paste(top.transpose(Image.FLIP_TOP_BOTTOM), (0, H))
# paint the interior of the disc (text removed)
R_IN = 238
yy, xx = np.mgrid[0:w, 0:w]
d = np.hypot(xx - H, yy - H)
t = np.clip(d / R_IN, 0, 1)
ang = (yy - H) / R_IN
base_c = np.array([46, 168, 200], float)   # light teal (centre/lower)
base_e = np.array([12, 78, 112], float)    # darker teal (edge)
g = (1 - t[..., None] ** 1.6) * base_c + t[..., None] ** 1.6 * base_e
g *= (1.0 - 0.18 * np.clip(-ang, 0, 1))[..., None]          # a bit darker towards the top
# faint swirl texture
rng = np.random.default_rng(3)
noise = Image.fromarray((rng.random((w // 6, w // 6)) * 255).astype("uint8")).resize((w, w), Image.BICUBIC).filter(ImageFilter.GaussianBlur(6))
g *= (0.94 + 0.12 * (np.asarray(noise, float) / 255))[..., None]
disc = Image.fromarray(np.clip(g, 0, 255).astype("uint8"))
mask = Image.new("L", (w * 4, w * 4), 0)
ImageDraw.Draw(mask).ellipse([(H - R_IN) * 4, (H - R_IN) * 4, (H + R_IN) * 4, (H + R_IN) * 4], fill=255)
mask = mask.resize((w, w), Image.LANCZOS)
out.paste(disc, (0, 0), mask)
# inner shadow ring for depth
sh = Image.new("L", (w, w), 0)
ImageDraw.Draw(sh).ellipse([H - R_IN, H - R_IN, H + R_IN, H + R_IN], outline=255, width=10)
sh = sh.filter(ImageFilter.GaussianBlur(7))
dark = Image.new("RGB", (w, w), (4, 30, 48))
out.paste(dark, (0, 0), sh.point(lambda v: int(v * 0.55)))
# "XE" lettering in the style of the logo (cream with a soft bronze edge + shadow)
font = None
for f in ("/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf", "timesbd.ttf", "georgiab.ttf"):
    try:
        font = ImageFont.truetype(f, 235)
        break
    except OSError:
        pass
text = "XE"
tw = font.getbbox(text)
tx = H - (tw[0] + tw[2]) / 2
ty = H - (tw[1] + tw[3]) / 2 - 4
lay = Image.new("L", (w, w), 0)
ImageDraw.Draw(lay).text((tx, ty), text, font=font, fill=255)
shadow = lay.filter(ImageFilter.GaussianBlur(10)).point(lambda v: int(v * 0.8))
out.paste(Image.new("RGB", (w, w), (2, 22, 36)), (6, 12), shadow)
edge = lay.filter(ImageFilter.MaxFilter(7))
out.paste(Image.new("RGB", (w, w), (150, 104, 56)), (0, 0), edge)
grad = np.zeros((w, w, 3), float)
v = np.clip((yy - (ty + 30)) / 200, 0, 1)[..., None]
grad[:] = (1 - v) * np.array([255, 252, 240]) + v * np.array([222, 205, 170])
out.paste(Image.fromarray(grad.astype("uint8")), (0, 0), lay)
hl = lay.filter(ImageFilter.GaussianBlur(1.2))
# alpha: everything that is not the dark navy background
a = np.asarray(out, float)
bg = np.asarray(src.crop((box[0] - 10, box[1], box[0] + 10, box[1] + 20)).resize((1, 1), Image.BOX), float).reshape(3)
dist = np.sqrt(((a - bg) ** 2).sum(-1))
alpha = np.clip((dist - 22) / 26, 0, 1)
alpha_img = Image.fromarray((alpha * 255).astype("uint8"))
# keep the silhouette solid: fill holes inside the ring/disc
full = ImageDraw.Draw(alpha_img)
disc_m = Image.new("L", (w, w), 0); ImageDraw.Draw(disc_m).ellipse([H - 262, H - 262, H + 262, H + 262], fill=255)
alpha_img = ImageChops.lighter(alpha_img, disc_m)
rgba = out.convert("RGBA"); rgba.putalpha(alpha_img)
bbox = rgba.getbbox(); rgba = rgba.crop(bbox)
s = max(rgba.size); sq = Image.new("RGBA", (s, s), (0, 0, 0, 0)); sq.paste(rgba, ((s - rgba.size[0]) // 2, (s - rgba.size[1]) // 2))
big = sq.resize((512, 512), Image.LANCZOS)
big.save(str(ROOT / "assets" / "wowxe.png"))
big.save(str(ROOT / "assets" / "wowxe.ico"), sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
