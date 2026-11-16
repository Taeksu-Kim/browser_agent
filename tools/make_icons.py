"""확장 아이콘(속눈썹 달린 눈) 만들기 -- 128px 로 그려서 줄인다.  python tools/make_icons.py"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "extension" / "icons"
S = 512                     # 크게 그린 뒤 줄여서 계단 현상 줄이기
BG, FG, PUPIL = (61, 90, 254), (255, 255, 255), (29, 33, 39)


def draw() -> Image.Image:
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=S // 5, fill=BG)
    cx, cy, w, h = S / 2, S * 0.58, S * 0.78, S * 0.42
    # 아몬드 모양 눈: 위·아래 호를 다각형으로
    top = [(cx - w / 2 + w * t, cy - h / 2 * math.sin(math.pi * t)) for t in [i / 60 for i in range(61)]]
    bot = [(cx + w / 2 - w * t, cy + h / 2 * math.sin(math.pi * t)) for t in [i / 60 for i in range(61)]]
    d.polygon(top + bot, fill=FG)
    d.ellipse([cx - h * 0.36, cy - h * 0.36, cx + h * 0.36, cy + h * 0.36], fill=PUPIL)
    d.ellipse([cx - h * 0.12 + h * 0.12, cy - h * 0.2, cx + h * 0.05 + h * 0.12, cy - h * 0.03], fill=FG)
    # 속눈썹 5개: 윗눈꺼풀에서 바깥으로
    for t in (0.18, 0.34, 0.5, 0.66, 0.82):
        x, y = cx - w / 2 + w * t, cy - h / 2 * math.sin(math.pi * t)
        ang = math.pi / 2 + (t - 0.5) * 1.6
        L = S * 0.13
        d.line([(x, y), (x - L * math.cos(ang), y - L * math.sin(ang))], fill=FG, width=int(S * 0.035))
    return im


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw()
    for n in (16, 32, 48, 128):
        big.resize((n, n), Image.LANCZOS).save(OUT / f"icon{n}.png")
    print("wrote", sorted(p.name for p in OUT.glob("*.png")))
