"""Generate build/icon.ico for the Windows exe (CI build step; needs Pillow).
A simple paper-plane-on-teal mark - no external assets."""
from pathlib import Path

from PIL import Image, ImageDraw

out = Path(__file__).resolve().parent.parent / "build"
out.mkdir(exist_ok=True)

img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([8, 8, 248, 248], radius=56, fill=(14, 116, 144, 255))
# paper plane
d.polygon([(52, 138), (208, 66), (150, 200)], fill=(255, 255, 255, 255))
d.polygon([(52, 138), (208, 66), (118, 148)], fill=(227, 241, 245, 255))
d.polygon([(118, 148), (126, 196), (146, 166)], fill=(200, 224, 232, 255))

img.save(out / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", out / "icon.ico")
