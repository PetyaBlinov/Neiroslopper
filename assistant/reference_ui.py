"""Compact reference workspace. All network/image jobs receive plain data only."""
import io
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .reference_workflow import (reference_request, reference_defaults, ANCHORS,
                                 run_job, ReviewBudget, selected_plan)
from ui_theme import BACKGROUND, FIELD, INK, GOLD, SURFACE


class ReferenceWindow:
    def __init__(self, owner, prompt=''):
        self.owner = owner
        self.closed = self.busy = False
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.token = object()
        self.worker = self.timer = None
        self.asset = self.result = self.before = self.report = None
        self.scene = self.selected = None
        self.report_key = None
        self.hits, self.variants = (), ()
        self.budget = ReviewBudget()
        self.choice_cache = {}
        self.subject_box = self.drag_start = self.image_layout = None
        self.photos = {}
        self.root = tk.Toplevel(owner.root)
        self.root.title('Neiroslopper · Рисунок по образцу')
        self.root.configure(bg=BACKGROUND)
        from branding import apply_app_icon
        from display_scaling import ui_scale, scale_widget_tree
        apply_app_icon(self.root)
        scale = ui_scale(self.root)
        width = min(round(1160 * scale), self.root.winfo_screenwidth() - round(60 * scale))
        height = min(round(760 * scale), self.root.winfo_screenheight() - round(100 * scale))
        self.root.geometry(f'{width}x{height}')
        self.root.minsize(min(width, round(900 * scale)), min(height, round(600 * scale)))
        self.root._neiroslopper_geometry_scaled = True
        self.query = tk.StringVar(self.root, reference_request(prompt) or prompt[:180])
        self.sites = tk.BooleanVar(self.root, bool(re.search(r'официальн|\b(?:мфти|mipt)\b', prompt, re.I)))
        self.status = tk.StringVar(self.root, 'Выберите образец')
        self.detail = tk.StringVar(self.root, 'Сбалансированный')
        width, anchor = reference_defaults(prompt)
        self.width = tk.StringVar(self.root, width)
        self.anchor = tk.StringVar(self.root, anchor)
        self.hatching = tk.BooleanVar(self.root, False)
        self.region = tk.StringVar(self.root, 'Весь образец')
        self.build_ui()
        scale_widget_tree(self.root)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind('<Escape>', self.cancel_job)
        self.timer = self.root.after(60, self.pump)
        # Opening this window deliberately does not import CV or access a network.

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=16, style='App.TFrame')
        outer.pack(fill='both', expand=True)
        header = ttk.Frame(outer, style='App.TFrame')
        header.pack(fill='x', pady=(0, 12))
        ttk.Label(header, text='Рисунок по образцу', style='Title.TLabel').pack(side='left')
        ttk.Button(header, text='Закрыть', command=self.close, style='Header.TButton').pack(side='right')
        search = ttk.Frame(outer, padding=10)
        search.pack(fill='x')
        self.query_entry = ttk.Entry(search, textvariable=self.query)
        self.query_entry.pack(side='left', fill='x', expand=True)
        self.query_entry.bind('<Return>', lambda event: self.search())
        self.search_button = ttk.Button(search, text='Найти', command=self.search)
        self.search_button.pack(side='left', padx=8)
        self.file_button = ttk.Button(search, text='Открыть файл…', command=self.open_file)
        self.file_button.pack(side='left')
        ttk.Checkbutton(search, text='Сайты · Codex', variable=self.sites).pack(side='left', padx=(12, 0))
        middle = ttk.Frame(outer, style='App.TFrame')
        middle.pack(fill='both', expand=True, pady=12)
        from editor_tools.layout import ScrollablePage
        self.sidebar = ScrollablePage(middle)
        self.sidebar.pack(side='left', fill='y', padx=(0, 12))
        self.sidebar.canvas.configure(width=270, background=SURFACE)
        left = self.sidebar.content
        ttk.Label(left, text='Образцы', style='Section.TLabel').pack(anchor='w')
        self.gallery = tk.Listbox(left, height=6, width=25, bg=FIELD, fg=INK,
                                  selectbackground='#315274', selectforeground=INK,
                                  borderwidth=0, highlightthickness=0, exportselection=False)
        self.gallery.pack(fill='x', pady=8)
        self.gallery.bind('<<ListboxSelect>>', self.show_hit)
        self.gallery.bind('<Double-1>', lambda event: self.open_hit())
        self.source = ttk.Label(left, text='', wraplength=235, style='Muted.TLabel')
        self.source.pack(fill='x', pady=(0, 8))
        self.open_button = ttk.Button(left, text='Открыть образец', command=self.open_hit)
        self.open_button.pack(fill='x')
        ttk.Separator(left).pack(fill='x', pady=12)
        row = ttk.Frame(left)
        row.pack(fill='x')
        ttk.Label(row, text='Ширина, мм').pack(side='left')
        self.width_entry = ttk.Entry(row, textvariable=self.width, width=7)
        self.width_entry.pack(side='right')
        ttk.Combobox(left, textvariable=self.anchor, values=tuple(ANCHORS),
                     state='readonly', width=22).pack(fill='x', pady=(6, 0))
        ttk.Checkbutton(left, text='Тональная штриховка', variable=self.hatching).pack(anchor='w', pady=6)
        ttk.Label(left, textvariable=self.region, style='Muted.TLabel').pack(anchor='w')
        ttk.Button(left, text='Сбросить область', command=self.clear_region, style='Quiet.TButton').pack(fill='x')
        self.build_button = ttk.Button(left, text='Построить варианты', command=self.build_variants,
                                       style='Accent.TButton')
        self.build_button.pack(fill='x', pady=(8, 8))
        self.picker = ttk.Combobox(left, textvariable=self.detail, state='readonly', width=22,
                                  values=('Лаконичный', 'Сбалансированный', 'Подробный'))
        self.picker.pack(fill='x')
        self.picker.bind('<<ComboboxSelected>>', self.pick_variant)
        self.choose_button = ttk.Button(left, text='Выбрать с Codex…', command=self.choose)
        self.choose_button.pack(fill='x', pady=(8, 0))
        self.review_button = ttk.Button(left, text='Проверить с Codex…', command=self.review)
        self.review_button.pack(fill='x', pady=(8, 0))
        self.repair_button = ttk.Button(left, text='Доработать области', command=self.repair)
        self.repair_button.pack(fill='x', pady=(8, 0))
        right = ttk.Frame(middle, style='App.TFrame')
        right.pack(side='left', fill='both', expand=True)
        canvases = ttk.Frame(right, style='App.TFrame')
        canvases.pack(fill='both', expand=True)
        canvases.columnconfigure((0, 1), weight=1)
        canvases.rowconfigure(1, weight=1)
        for column, label in enumerate(('Оригинал', 'Будущий рисунок')):
            ttk.Label(canvases, text=label, style='Subtitle.TLabel').grid(row=0, column=column, sticky='w', pady=(0, 6))
        self.original = tk.Canvas(canvases, bg=FIELD, highlightthickness=0, width=320, height=330)
        self.preview = tk.Canvas(canvases, bg=FIELD, highlightthickness=0, width=320, height=330)
        self.original.grid(row=1, column=0, sticky='nsew', padx=(0, 6))
        self.preview.grid(row=1, column=1, sticky='nsew', padx=(6, 0))
        for canvas in (self.original, self.preview):
            canvas.bind('<Configure>', self.redraw)
        self.original.bind('<ButtonPress-1>', self.start_region)
        self.original.bind('<B1-Motion>', self.drag_region)
        self.original.bind('<ButtonRelease-1>', self.finish_region)
        self.explanation = tk.Text(right, height=6, wrap='word', bg=FIELD, fg=INK,
                                   borderwidth=0, highlightthickness=0, padx=12, pady=10, state='disabled')
        self.explanation.pack(fill='x', pady=(12, 0))
        bottom = ttk.Frame(outer, style='App.TFrame')
        bottom.pack(fill='x')
        ttk.Label(bottom, textvariable=self.status, wraplength=460, style='Status.TLabel').pack(side='left', fill='x', expand=True)
        self.propose_button = ttk.Button(bottom, text='Предложить эскиз', command=self.propose,
                                         style='Accent.TButton')
        self.propose_button.pack(side='right')
        self.cancel_button = ttk.Button(bottom, text='Отменить', command=self.cancel_job)
        self.cancel_button.pack(side='right', padx=8)
        self.sidebar.enable_wheel()
        self.refresh_controls()

    def explain(self, text):
        self.explanation.configure(state='normal')
        self.explanation.delete('1.0', 'end')
        self.explanation.insert('1.0', text)
        self.explanation.configure(state='disabled')

    def refresh_controls(self):
        def state(widget, enabled):
            widget.configure(state='normal' if enabled else 'disabled')
        for button in (self.search_button, self.file_button):
            state(button, not self.busy)
        state(self.open_button, bool(self.hits) and not self.busy)
        state(self.build_button, self.asset is not None and not self.busy)
        raster = self.result is not None and self.result.kind != 'vector'
        state(self.choose_button, raster and not self.busy)
        state(self.review_button, raster and not self.busy)
        state(self.repair_button, self.report is not None and bool(self.report.issues) and not self.busy)
        state(self.propose_button, self.result is not None and not self.busy)
        self.picker.configure(state='readonly' if self.variants and not self.busy else 'disabled')
        state(self.cancel_button, self.busy)

    def start_job(self, kind, payload, status):
        if self.closed or self.busy:
            return
        self.cancel = threading.Event()
        self.token = object()
        self.busy = True
        self.status.set(status)
        self.refresh_controls()
        self.worker = threading.Thread(target=run_job,
            args=(self.events, self.token, self.cancel, kind, payload), daemon=True,
            name='reference-' + kind)
        self.worker.start()

    def cancel_job(self, event=None):
        self.cancel.set()
        self.token = object()
        self.busy = False
        if not self.closed:
            self.status.set('Операция отменена')
            self.refresh_controls()

    def search(self):
        query = self.query.get().strip()
        if not 2 <= len(query) <= 180:
            self.status.set('Введите название образца: 2–180 символов')
            return
        self.start_job('search', (query, self.sites.get()), 'Ищу образцы…')

    def open_file(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.root,
            filetypes=(('Изображения', '*.png *.jpg *.jpeg *.svg *.webp *.bmp *.tif *.tiff'), ('Все файлы', '*.*')))
        if path:
            self.start_job('file', path, 'Открываю образец…')

    def show_hit(self, event=None):
        selected = self.gallery.curselection()
        if not selected or selected[0] >= len(self.hits):
            return
        hit = self.hits[selected[0]]
        candidate = hit.candidate
        self.source.configure(text=f'{candidate.title}\n{candidate.page}\n{candidate.license}'[:650])
        if self.asset is None and hit.thumbnail:
            from PIL import Image
            self.draw_image(self.original, Image.open(io.BytesIO(hit.thumbnail)).convert('RGB'), 'original')

    def open_hit(self):
        selected = self.gallery.curselection()
        if selected:
            self.start_job('download', self.hits[selected[0]].candidate, 'Загружаю выбранный образец…')

    def build_variants(self):
        if self.asset is None or self.busy:
            return
        try:
            from geometry import Sheet
            from .image_trace import Options
            before = self.owner.current_snapshot()
            self.selected = self.owner.selected_object() if hasattr(self.owner, 'selected_object') else None
            self.scene = self.owner.current_scene() if self.selected is not None else None
            sheet = Sheet(before.state['sheet_width'], before.state['sheet_height'])
            width = float(self.width.get().replace(',', '.'))
            options = Options(width_mm=width, anchor=ANCHORS[self.anchor.get()], high_detail=True, subject_box=self.subject_box,
                              hatching=self.hatching.get(), local_contrast=True, weak_edges=True)
            options.check()
            self.before = before
            self.result = self.report = None
            self.variants = ()
            self.report_key = None
            self.budget = ReviewBudget()
            self.choice_cache = {}
            self.start_job('build', (self.asset, sheet, options), 'Строю локальные варианты…')
        except (ValueError, TypeError) as exc:
            self.status.set(str(exc))

    def selected_result(self):
        from .art_direction import LABELS
        return next((result for key, result in self.variants if LABELS.get(key, 'Контуры SVG') == self.detail.get()), None)

    def pick_variant(self, event=None):
        if self.busy:
            return
        self.result = self.selected_result()
        self.report = None
        self.report_key = None
        self.redraw()
        self.refresh_controls()

    def consent(self, operation):
        return messagebox.askyesno('Отправка изображений в Codex',
            f'{operation}\n\nВ Codex будут отправлены оригинал и контуры без имени файла и метаданных. Продолжить?',
            parent=self.root)

    def choose(self):
        if not self.variants or self.busy:
            return
        from .trace_review import fingerprint
        key = (tuple((name, fingerprint(result)) for name, result in self.variants), self.query.get()[:180])
        if key in self.choice_cache:
            self.accept_choice(self.choice_cache[key])
        elif self.consent('Сравнить варианты и выбрать наиболее похожий?'):
            self.choice_key = key
            self.start_job('choose', (self.variants, self.query.get()[:180]), 'Codex сравнивает варианты…')

    def accept_choice(self, value):
        from .art_direction import LABELS
        if value.winner:
            self.detail.set(LABELS.get(value.winner, 'Контуры SVG'))
            self.pick_variant()
        self.explain(value.reason)

    def review(self):
        if self.result is None or self.busy:
            return
        from .trace_review import fingerprint
        key = fingerprint(self.result)
        if key in self.budget.cache:
            self.accept_review(self.budget.cache[key])
        elif self.budget.calls >= 2:
            self.status.set('Для этого набора уже выполнены две проверки')
        elif self.consent('Проверить сходство и до четырёх отдельных областей?'):
            self.start_job('review', (self.budget, self.result), 'Codex проверяет рисунок…')

    def accept_review(self, report):
        from .trace_review import fingerprint
        self.report, self.report_key = report, fingerprint(self.result)
        lines = [report.summary]
        lines.extend(f'• {item["description"]}' for item in report.issues)
        lines.extend(report.uncertainties)
        self.explain('\n'.join(lines))
        self.redraw()
        self.refresh_controls()

    def repair(self):
        if self.report is None or self.busy:
            return
        from .trace_review import fingerprint
        if fingerprint(self.result) != self.report_key:
            self.status.set('Сначала проверьте текущий вариант')
            return
        self.start_job('repair', (self.asset, self.result, self.report), 'Дорабатываю выбранные области локально…')

    def propose(self):
        if self.result is None or self.busy:
            return
        try:
            plan = selected_plan(self.result, self.scene, self.selected)
            if self.owner.offer_external_plan(self.before, plan, 'Рисунок по образцу'):
                self.status.set('Предложение передано в помощник')
                self.owner.root.lift()
            else:
                self.status.set('Эскиз изменился. Постройте варианты заново')
        except (ValueError, TypeError) as exc:
            self.status.set(str(exc))

    def pump(self):
        if self.closed:
            return
        while True:
            try:
                token, kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if token is not self.token:
                continue
            self.busy = False
            if kind == 'error':
                self.status.set(value[:220])
            elif kind == 'search':
                self.hits = value
                self.gallery.delete(0, 'end')
                for hit in value:
                    self.gallery.insert('end', hit.candidate.title)
                self.status.set(f'Найдено образцов: {len(value)}')
                if value:
                    self.gallery.selection_set(0)
                    self.show_hit()
            elif kind in ('file', 'download'):
                self.asset = value
                self.result = self.report = None
                self.before = None
                self.variants = ()
                self.subject_box = None
                self.region.set('Весь образец')
                self.status.set(value.name)
                self.explain('')
            elif kind == 'build':
                from .art_direction import LABELS
                self.variants = value
                self.picker.configure(values=tuple(LABELS.get(key, 'Контуры SVG') for key, _ in value))
                selected = self.selected_result()
                if selected is None:
                    self.detail.set(LABELS.get(value[0][0], 'Контуры SVG'))
                self.result = self.selected_result()
                self.status.set(f'Готово вариантов: {len(value)}')
                self.explain('Выберите вариант и предложите его помощнику.')
            elif kind == 'choose':
                self.choice_cache[self.choice_key] = value
                self.accept_choice(value)
                self.status.set('Сравнение завершено')
            elif kind == 'review':
                self.accept_review(value)
                self.status.set('Проверка завершена')
            elif kind == 'repair':
                self.result = value
                from .art_direction import LABELS
                self.variants = tuple((key, value if LABELS.get(key, 'Контуры SVG') == self.detail.get() else result)
                                      for key, result in self.variants)
                self.report = None
                self.report_key = None
                self.status.set('Области доработаны. Проверьте новый результат')
            self.redraw()
            self.refresh_controls()
        self.timer = self.root.after(60, self.pump)

    def draw_image(self, canvas, image, key):
        from PIL import Image, ImageTk
        width, height = max(32, canvas.winfo_width()), max(32, canvas.winfo_height())
        image = image.copy()
        original_size = image.size
        image.thumbnail((width - 12, height - 12), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image, master=self.root)
        self.photos[key] = photo
        canvas.delete('all')
        x, y = (width - image.width) / 2, (height - image.height) / 2
        canvas.create_image(x, y, image=photo, anchor='nw')
        if key == 'original':
            self.image_layout = (x, y, image.width / original_size[0], original_size)
            if self.subject_box:
                a, b, c, d = self.subject_box
                s = self.image_layout[2]
                canvas.create_rectangle(x+a*s, y+b*s, x+c*s, y+d*s, outline=GOLD, width=2)

    def redraw(self, event=None):
        if self.closed:
            return
        if self.asset is not None:
            from PIL import Image, ImageDraw
            self.draw_image(self.original, Image.fromarray(self.asset.image), 'original')
        if self.result is None:
            self.preview.delete('all')
            return
        from PIL import Image, ImageDraw
        from .trace_review import preview_image
        if self.result.kind == 'vector':
            from editor_tools.image_trace import vector_preview
            image = Image.fromarray(vector_preview(self.result.paths))
        else:
            image = preview_image(self.result)
            if self.report:
                draw = ImageDraw.Draw(image)
                for issue in self.report.issues:
                    box = [round(v*size/1000) for v, size in zip(issue['box'], image.size*2)]
                    draw.rectangle(box, outline='#c28924', width=max(2, image.width//400))
        self.draw_image(self.preview, image, 'preview')

    def start_region(self, event):
        if self.asset is not None and not self.busy and not self.asset.vector_paths:
            self.drag_start = (event.x, event.y)

    def drag_region(self, event):
        if self.drag_start:
            self.original.delete('selection')
            self.original.create_rectangle(*self.drag_start, event.x, event.y, outline=GOLD, width=2, tags='selection')

    def finish_region(self, event):
        if not self.drag_start or not self.image_layout:
            return
        x, y, scale, size = self.image_layout
        a, b = self.drag_start
        self.drag_start = None
        left, right = sorted((max(0, min(size[0], round((p-x)/scale))) for p in (a, event.x)))
        top, bottom = sorted((max(0, min(size[1], round((p-y)/scale))) for p in (b, event.y)))
        if min(right-left, bottom-top) >= 8:
            self.subject_box = (left, top, right, bottom)
            self.region.set('Выделена важная область')
            self.redraw()

    def clear_region(self):
        if not self.busy:
            self.subject_box = None
            self.region.set('Весь образец')
            self.redraw()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancel.set()
        self.token = object()
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.photos.clear()
        self.root.destroy()
        if self.owner.reference_window is self:
            self.owner.reference_window = None
