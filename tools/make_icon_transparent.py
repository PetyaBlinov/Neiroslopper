"""Remove only the original white outer corners; never redraw logo pixels."""
import argparse
import json
from pathlib import Path

from PIL import Image


def transparent_corners(source):
    image = Image.open(source).convert('RGBA')
    pixels = image.load()
    changed = 0
    interior = 0
    for y in range(image.height):
        navy = [x for x in range(image.width)
                if pixels[x, y][0] < 80 and pixels[x, y][1] < 110
                and pixels[x, y][2] < 190]
        if not navy:
            raise ValueError(f'No navy tile found on row {y}; wrong source image?')
        left, right = navy[0], navy[-1]
        interior += right - left + 1
        # Only the two exterior spans may change. White pen/plotter stays intact.
        for span, edge in ((range(left), left),
                           (range(right + 1, image.width), right)):
            background = pixels[edge, y][:3]
            denominator = 255 - min(background)
            for x in span:
                rgba = pixels[x, y]
                alpha = max(0, min(1, (255 - min(rgba[:3])) / denominator))
                if alpha < 0.02:
                    result = (0, 0, 0, 0)
                else:
                    # Remove white matte from antialiased edge pixels.
                    rgb = tuple(max(0, min(255, round((v - 255 * (1 - alpha)) / alpha)))
                                for v in rgba[:3])
                    result = (*rgb, round(alpha * 255))
                pixels[x, y] = result
                changed += result != rgba
    return image, {'changed_exterior_pixels': changed,
                   'untouched_interior_pixels': interior,
                   'corners_alpha': [pixels[x, y][3] for x, y in
                       ((0, 0), (image.width-1, 0),
                        (0, image.height-1), (image.width-1, image.height-1))]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('assets', type=Path)
    args = parser.parse_args()
    image, report = transparent_corners(args.source)
    image.save(args.assets / 'icon.png')
    image.save(args.assets / 'icon.ico', format='ICO',
               sizes=[(16,16), (24,24), (32,32), (48,48), (64,64), (128,128), (256,256)])
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
