"""Antialiased nine-slice ttk surfaces, without rasterizing button text.

Only the small rounded border is a PNG. Native ttk labels retain text scaling,
keyboard activation, disabled-state behaviour and accessibility information.
"""
import base64
from functools import lru_cache
import struct
import tkinter as tk
import zlib


def rgb(colour):
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


@lru_cache(maxsize=192)
def rounded_png(fill, background, radius, outline=None, stroke=1):
    """Supersampled opaque corner pixels blend into the widget's parent."""
    radius = max(2, int(radius))
    # Tk tiles (rather than scales) the centre. A broad solid tile avoids
    # thousands of tiny GDI blits on every button repaint at high DPI.
    size = radius * 2 + 64
    fg, bg = rgb(fill), rgb(background)
    edge = rgb(outline or fill)
    sample = 4

    def inside(x, y, inset):
        r = max(0, radius - inset)
        if not inset <= x < size - inset or not inset <= y < size - inset:
            return False
        cx = min(max(x, radius), size - radius)
        cy = min(max(y, radius), size - radius)
        return (x - cx) ** 2 + (y - cy) ** 2 <= r ** 2

    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG filter: none.
        for x in range(size):
            if radius <= x < size - radius or radius <= y < size - radius:
                colour = edge if min(x, y, size - 1 - x, size - 1 - y) < stroke else fg
                rows.extend(colour)
                continue
            totals = [0, 0, 0]
            for sy in range(sample):
                for sx in range(sample):
                    px, py = x + (sx + .5) / sample, y + (sy + .5) / sample
                    colour = (fg if inside(px, py, stroke) else edge) if inside(px, py, 0) else bg
                    for channel in range(3):
                        totals[channel] += colour[channel]
            rows.extend(round(value / sample ** 2) for value in totals)

    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(rows), 9)) + chunk(b'IEND', b''))


def install_rounded_surfaces(root, style, scale, *, surface, background, gold):
    images = []
    radius = round(9 * scale)

    def element(name, parent, normal, active, pressed, disabled, selected=None):
        def image(colour, outline=None):
            png = rounded_png(colour, parent, radius, outline, max(1, round(scale)))
            bitmap = tk.PhotoImage(master=root, data=base64.b64encode(png))
            images.append(bitmap)
            return bitmap

        default = image(normal)
        states = [('disabled', image(disabled)),
                  ('pressed', '!disabled', image(pressed)),
                  ('focus', 'selected', '!disabled', image(selected or normal, gold)),
                  ('focus', '!disabled', image(normal, gold)),
                  ('active', '!disabled', image(active))]
        if selected:
            states.append(('selected', image(selected)))
        style.element_create(name, 'image', default, *states,
                             border=radius + 1, padding=0, sticky='nsew', width=0, height=0)
        return name

    def button_layout(name):
        return [(name, {'sticky': 'nswe', 'children': [
            ('Button.padding', {'sticky': 'nswe', 'children': [
                ('Button.label', {'sticky': 'nswe'})]})]})]

    variants = (
        ('TButton', surface, '#244765', '#315b7c', '#1b3955', '#18334d'),
        ('Accent.TButton', surface, gold, '#ffe0a1', '#d4a543', '#314a63'),
        ('Quiet.TButton', surface, surface, '#244765', '#1b3955', surface),
        ('Header.TButton', background, background, '#244765', '#1b3955', background),
        ('Danger.TButton', background, '#ae344b', '#c3455d', '#8d253c', '#543449'),
    )
    for index, (style_name, parent, normal, active, pressed, disabled) in enumerate(variants):
        name = element(f'Neiroslopper{index}.surface', parent, normal, active, pressed, disabled)
        style.layout(style_name, button_layout(name))

    mode = element('Neiroslopper.mode', surface, surface, '#244765', '#1b3955', surface, '#244765')
    style.layout('Mode.TRadiobutton', [(mode, {'sticky': 'nswe', 'children': [
        ('Radiobutton.padding', {'sticky': 'nswe', 'children': [
            ('Radiobutton.label', {'sticky': 'nswe'})]})]})])
    tab = element('Neiroslopper.tab', surface, surface, '#244765', '#1b3955', surface, '#244765')
    style.layout('TNotebook.Tab', [(tab, {'sticky': 'nswe', 'children': [
        ('Notebook.padding', {'sticky': 'nswe', 'children': [
            ('Notebook.label', {'sticky': 'nswe'})]})]})])
    root._neiroslopper_theme_images = images
