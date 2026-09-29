"""Draw the app icon (a card on a green table with a replay arrow) and save it as a
multi-size .ico, using only the standard library (PNG-compressed icon entries)."""
import math
import struct
import sys
import zlib
from pathlib import Path


def render(size: int, ss: int = 4) -> bytes:
    """RGBA pixels, supersampled `ss` times per axis for smooth edges."""
    n = size * ss
    px = [[(0, 0, 0, 0)] * n for _ in range(n)]

    def rrect(x0, y0, x1, y1, r, color, pixels):
        for y in range(max(0, int(y0)), min(n, int(y1) + 1)):
            for x in range(max(0, int(x0)), min(n, int(x1) + 1)):
                cx = min(max(x, x0 + r), x1 - r)
                cy = min(max(y, y0 + r), y1 - r)
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    pixels[y][x] = color

    u = n / 64
    rrect(2 * u, 2 * u, 62 * u, 62 * u, 12 * u, (22, 42, 32, 255), px)            # table
    rrect(17 * u, 9 * u, 47 * u, 55 * u, 4 * u, (217, 179, 91, 255), px)          # card border (gold)
    rrect(20 * u, 12 * u, 44 * u, 52 * u, 2.5 * u, (37, 52, 44, 255), px)         # card face
    # replay arrow: an arc with a head
    cx, cy, rad, w = 32 * u, 32 * u, 8.5 * u, 2.6 * u
    for y in range(n):
        for x in range(n):
            d = math.hypot(x - cx, y - cy)
            ang = math.degrees(math.atan2(y - cy, x - cx)) % 360
            if abs(d - rad) <= w and not (300 <= ang <= 360):
                px[y][x] = (230, 236, 232, 255)
    # arrowhead at the end of the arc (angle 300°), pointing along the direction of travel
    th = math.radians(300)
    ex, ey = cx + rad * math.cos(th), cy + rad * math.sin(th)
    tx, ty = -math.sin(th), math.cos(th)            # tangent (increasing angle)
    rx, ry = math.cos(th), math.sin(th)             # radial
    half = 5.2 * u
    ax, ay = ex + rx * half, ey + ry * half
    bx, by = ex - rx * half, ey - ry * half
    qx, qy = ex + tx * 6.5 * u, ey + ty * 6.5 * u
    for y in range(n):
        for x in range(n):
            def side(x1, y1, x2, y2):
                return (x - x2) * (y1 - y2) - (x1 - x2) * (y - y2)
            s1, s2, s3 = side(ax, ay, bx, by), side(bx, by, qx, qy), side(qx, qy, ax, ay)
            if (s1 <= 0 and s2 <= 0 and s3 <= 0) or (s1 >= 0 and s2 >= 0 and s3 >= 0):
                px[y][x] = (230, 236, 232, 255)
    out = bytearray()
    for Y in range(size):
        out.append(0)                                  # PNG filter: none
        for X in range(size):
            acc = [0, 0, 0, 0]
            for dy in range(ss):
                for dx in range(ss):
                    r, g, b, a = px[Y * ss + dy][X * ss + dx]
                    acc[0] += r * a; acc[1] += g * a; acc[2] += b * a; acc[3] += a
            a = acc[3] / (ss * ss)
            if acc[3]:
                out += bytes((acc[0] // acc[3], acc[1] // acc[3], acc[2] // acc[3], int(a)))
            else:
                out += b"\0\0\0\0"
    return bytes(out)


def png(size: int) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(render(size), 9)) + chunk(b"IEND", b"")


def ico(path: Path, sizes=(16, 24, 32, 48, 64, 128, 256)):
    images = [png(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(header + entries + b"".join(images))


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("icon.ico")
    ico(out)
    print(f"wrote {out}")
