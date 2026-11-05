"""Explicit, offline FluidNC YAML draft generator and a small Tk setup window.

No serial/network calls, flashing or uploading are performed by this module.
The I2S layout is a DLC32-family template; v2.2 pin mapping must be checked.
"""
from copy import deepcopy
import math
from pathlib import Path
from machine_profiles import AXIS_PARAMETER_LIMITS

LAYOUT_NOTICE = ('I2S семейства MKS DLC32: BCK16, WS17, DATA21. В новой схеме '
                 'каретка X подключена к Y1 (5/6), портал Y — к X (1/2) и Z (3/4). '
                 'Для ревизии v2.2 эти соединения нужно сверить до загрузки. '
                 'Экспорт файла сам по себе ничего не меняет на плате.')
LEGACY_LAYOUT_NOTICE = ('Шаблон семейства MKS DLC32: BCK16, WS17, DATA21; '
                        'X STEP/DIR 1/2, Y 5/6, необязательный второй X на Z 3/4. '
                        'Ревизия v2.2 и проводка требуют проверки.')

WIRING_PROFILE = 'neiroslopper_ttl'
WIRING_LABEL = 'Новая схема: каретка Y1; портал X + Z; реле TTL32'


def new_wiring_defaults():
    """Return topology defaults, never unmeasured motion or polarity values.

    The names X/Y below are logical coordinates: carriage/portal respectively.
    The motor sockets on the PCB retain their physical X/Y1/Z labels.
    """
    result = dict(wiring_profile=WIRING_PROFILE, name='Neiroslopper — новая схема',
                  dual_y=True, relay_pin='32', relay_active_low=None,
                  endstops='nc', homing_enabled=False, hard_limits=False,
                  pins_verified=False, parameters_verified=False,
                  endstops_verified=False, no_endstops_confirmed=False,
                  invert_x=None, invert_y=None, invert_y2=None)
    for axis in ('x', 'y'):
        for key in ('steps_per_mm', 'max_rate', 'acceleration', 'travel',
                    'homing_feed', 'homing_seek', 'homing_pulloff', 'homing_mpos'):
            result[axis + '_' + key] = ''
        result[axis + '_limit_positive'] = None
    return result


def _number(parameters, key, config_key):
    value = parameters.get(key)
    if isinstance(value, bool):
        raise ValueError('Параметр ' + key + ' должен быть положительным числом.')
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('Введите числовое значение ' + key + '.') from exc
    minimum, maximum = AXIS_PARAMETER_LIMITS[config_key]
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'Параметр {key} должен быть от {minimum:g} до {maximum:g} (ограничения FluidNC v4.1.1).')
    return value


def _boolean(parameters, key):
    value = parameters.get(key)
    if type(value) is not bool:
        raise ValueError('Явно выберите параметр ' + key + '.')
    return value


def _legacy_config(parameters, *, require_confirmation=True):
    """Return a new stock FluidNC config dict from explicit physical parameters.

    Required keys: x/y_steps_per_mm, x/y_max_rate, x/y_acceleration,
    x/y_travel, invert_x, invert_y, dual_x, invert_x2, relay_pin
    (empty/None = no relay), relay_active_low, pins_verified,
    parameters_verified, no_endstops_confirmed. No pin is chosen for the relay.
    ``require_confirmation=False`` is for preview only, never automatic export.
    """
    if not isinstance(parameters, dict):
        raise ValueError('Нужен объект параметров конфигурации.')
    flags = {name: _boolean(parameters, name) for name in
             ('invert_x', 'invert_y', 'dual_x', 'invert_x2', 'relay_active_low',
              'pins_verified', 'parameters_verified', 'no_endstops_confirmed')}
    if require_confirmation and not all(flags[name] for name in
            ('pins_verified', 'parameters_verified', 'no_endstops_confirmed')):
        raise ValueError('Перед экспортом подтвердите сверку выводов v2.2, параметров и режим без концевиков.')
    if 'relay_pin' not in parameters:
        raise ValueError('Явно укажите relay_pin; пустое значение означает отсутствие реле в конфигурации.')
    pin = parameters['relay_pin']
    pin = '' if pin is None else str(pin).strip().lower()
    if pin.isdigit():
        pin = 'gpio.' + pin
    if pin and pin != 'no_pin':
        import re
        match = re.fullmatch(r'gpio\.(\d+)', pin)
        allowed = {2, 4, 5, 12, 13, 14, 15, 18, 19, 22, 23, 25, 26, 27, 32, 33}
        if not match or int(match[1]) not in allowed:
            raise ValueError('Укажите проверенный выходной GPIO реле; занятые USB/flash/I2S и входные пины недопустимы.')
        pin += ':low' if flags['relay_active_low'] else ''
    else:
        pin = 'NO_PIN'
    axes = {'shared_stepper_disable_pin': 'i2so.0'}
    for name, step, direction in (('x', 1, 2), ('y', 5, 6)):
        driver = {'step_pin': 'i2so.' + str(step),
                  'direction_pin': 'i2so.' + str(direction) + (':low' if flags['invert_' + name] else ''),
                  'disable_pin': 'NO_PIN'}
        axes[name] = {
            'steps_per_mm': _number(parameters, name + '_steps_per_mm', 'steps_per_mm'),
            'max_rate_mm_per_min': _number(parameters, name + '_max_rate', 'max_rate_mm_per_min'),
            'acceleration_mm_per_sec2': _number(parameters, name + '_acceleration', 'acceleration_mm_per_sec2'),
            'max_travel_mm': _number(parameters, name + '_travel', 'max_travel_mm'),
            'soft_limits': False,
            'motor0': {'limit_neg_pin': 'NO_PIN', 'limit_pos_pin': 'NO_PIN',
                       'limit_all_pin': 'NO_PIN', 'hard_limits': False, 'stepstick': driver},
        }
    if flags['dual_x']:
        axes['x']['motor1'] = {'limit_neg_pin': 'NO_PIN', 'limit_pos_pin': 'NO_PIN',
                              'limit_all_pin': 'NO_PIN', 'hard_limits': False,
                              'stepstick': {'step_pin': 'i2so.3',
                                            'direction_pin': 'i2so.4' + (':low' if flags['invert_x2'] else ''),
                                            'disable_pin': 'NO_PIN'}}
    name = parameters.get('name', 'Мини CNC-плоттер A4')
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise ValueError('Название конфигурации: от 1 до 100 печатных символов.')
    return {
        'board': 'MKS DLC32 v2.2 (verify I2S pin mapping)',
        'name': name.strip(),
        'meta': 'Offline draft; physical parameters and v2.2 wiring require verification',
        'report_inches': False,
        'kinematics': {'Cartesian': {}},
        'start': {'must_home': False, 'check_limits': False, 'deactivate_parking': True},
        'stepping': {'engine': 'I2S_STATIC', 'idle_ms': 255, 'pulse_us': 4,
                     'dir_delay_us': 2, 'disable_delay_us': 0},
        'i2so': {'bck_pin': 'gpio.16', 'ws_pin': 'gpio.17', 'data_pin': 'gpio.21'},
        'axes': axes,
        'coolant': {'flood_pin': pin, 'mist_pin': 'NO_PIN', 'delay_ms': 0},
        'control': {'safety_door_pin': 'NO_PIN', 'reset_pin': 'NO_PIN',
                    'feed_hold_pin': 'NO_PIN', 'cycle_start_pin': 'NO_PIN'},
        'probe': {'pin': 'NO_PIN'},
        'macros': {'startup_line0': '', 'startup_line1': ''},
    }


def _finite_number(parameters, key, minimum=None, maximum=None):
    value = parameters.get(key)
    if isinstance(value, bool):
        raise ValueError('Параметр ' + key + ' должен быть числом.')
    try:
        value = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError('Введите числовое значение ' + key + '.') from exc
    if not math.isfinite(value) or (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ValueError('Недопустимое значение ' + key + '.')
    return value


def _new_wiring_config(parameters, *, require_confirmation=True):
    """Build the agreed three-driver, TTL relay and optional NC switch layout."""
    flags = {key: _boolean(parameters, key) for key in (
        'invert_x', 'invert_y', 'invert_y2', 'dual_y', 'relay_active_low',
        'pins_verified', 'parameters_verified', 'homing_enabled', 'hard_limits')}
    if not flags['dual_y']:
        raise ValueError('В этой схеме у портала два двигателя: физические разъёмы X и Z.')
    if require_confirmation and not (flags['pins_verified'] and flags['parameters_verified']):
        raise ValueError('Перед экспортом подтвердите выводы конкретной v2.2 и реальные параметры движения.')
    relay = str(parameters.get('relay_pin', '')).strip().lower()
    if relay not in ('32', 'gpio.32'):
        raise ValueError('Новая схема использует только S разъёма LASER/TTL, GPIO32; не SCL и не вход PROBE.')
    mode = parameters.get('endstops')
    if mode not in ('nc', 'none'):
        raise ValueError('Выберите два механических NC-концевика или явный режим без концевиков.')
    if mode == 'none':
        if flags['homing_enabled'] or flags['hard_limits']:
            raise ValueError('Без концевиков homing и hard limits недоступны.')
        if require_confirmation and not _boolean(parameters, 'no_endstops_confirmed'):
            raise ValueError('Явно подтвердите вариант без концевиков.')
    elif require_confirmation and not _boolean(parameters, 'endstops_verified'):
        raise ValueError('Подтвердите подключение COM–NC к S/G и проверку состояния обоих концевиков.')

    # Use the already validated legacy numerical fields, then remap the physical
    # channels. This leaves legacy callers and their old dual-X topology intact.
    legacy = dict(parameters, dual_x=False, invert_x2=False,
                  no_endstops_confirmed=True, pins_verified=True,
                  parameters_verified=True)
    config = _legacy_config(legacy, require_confirmation=False)
    config['meta'] = ('Neiroslopper wiring: logical X carriage on Y1; logical Y portal on X+Z; '
                      'TTL32 coolant; v2.2 pin mapping must be verified')
    axes = config['axes']
    axes['x']['motor0']['stepstick'].update(
        step_pin='i2so.5', direction_pin='i2so.6' + (':low' if flags['invert_x'] else ''))
    axes['y']['motor0']['stepstick'].update(
        step_pin='i2so.1', direction_pin='i2so.2' + (':low' if flags['invert_y'] else ''))
    axes['y']['motor1'] = {
        'limit_neg_pin': 'NO_PIN', 'limit_pos_pin': 'NO_PIN',
        'limit_all_pin': 'NO_PIN', 'hard_limits': False,
        'stepstick': {'step_pin': 'i2so.3',
                      'direction_pin': 'i2so.4' + (':low' if flags['invert_y2'] else ''),
                      'disable_pin': 'NO_PIN'}}

    if mode == 'nc':
        for index, axis in enumerate(('x', 'y'), start=1):
            positive = _boolean(parameters, axis + '_limit_positive')
            motor = axes[axis]['motor0']
            # NC closes S to ground when clear, then opens to the board pull-up.
            # GPIO35/36 have no internal pulls: do not invent a :pu attribute.
            motor['limit_pos_pin' if positive else 'limit_neg_pin'] = 'gpio.' + ('36' if axis == 'x' else '35')
            motor['hard_limits'] = flags['hard_limits']
            if flags['homing_enabled']:
                feed = _finite_number(parameters, axis + '_homing_feed', 1.0, 100000.0)
                seek = _finite_number(parameters, axis + '_homing_seek', 1.0, 100000.0)
                pulloff = _finite_number(parameters, axis + '_homing_pulloff', .1, 100000.0)
                mpos = _finite_number(parameters, axis + '_homing_mpos')
                if feed > seek or seek > axes[axis]['max_rate_mm_per_min']:
                    raise ValueError('Для ' + axis.upper() + ': точный подход ≤ поиск ≤ максимальная подача оси.')
                if pulloff >= axes[axis]['max_travel_mm']:
                    raise ValueError('Отход концевика должен быть меньше измеренного хода оси ' + axis.upper() + '.')
                axes[axis]['homing'] = {
                    'cycle': index, 'positive_direction': positive,
                    'mpos_mm': mpos, 'feed_mm_per_min': feed,
                    'seek_mm_per_min': seek}
                motor['pulloff_mm'] = pulloff
                if axis == 'y':
                    # One portal switch stops both channels via FluidNC's dual
                    # switch handling. Equal pull-offs do not imply auto-square.
                    axes[axis]['motor1']['pulloff_mm'] = pulloff
        config['start']['check_limits'] = flags['hard_limits']
        config['start']['must_home'] = flags['homing_enabled']
    # Software travel limits stay off until machine coordinates are commissioned.
    return config


def generate_config(parameters, *, require_confirmation=True):
    """Generate a YAML model without sending anything to a connected device.

    The original API is retained when wiring_profile is omitted. The new
    ``neiroslopper_ttl`` profile is supplied by :func:`new_wiring_defaults`.
    """
    if not isinstance(parameters, dict):
        raise ValueError('Нужен объект параметров конфигурации.')
    profile = parameters.get('wiring_profile', 'legacy_xy')
    if profile == WIRING_PROFILE:
        return _new_wiring_config(parameters, require_confirmation=require_confirmation)
    if profile != 'legacy_xy':
        raise ValueError('Неизвестный профиль проводки.')
    return _legacy_config(parameters, require_confirmation=require_confirmation)


def generate_yaml(parameters, *, require_confirmation=True):
    import yaml
    config = generate_config(deepcopy(parameters), require_confirmation=require_confirmation)
    endstops = parameters.get('wiring_profile') == WIRING_PROFILE and parameters.get('endstops') == 'nc'
    mode_notice = ('# Два NC-концевика: LIMIT X→каретка, LIMIT Y→портал. COM→G, NC→S; 5V не подключать.\n'
                   '# У двух моторов портала один концевик; независимого автоматического выравнивания нет.\n'
                   '# Внешние подтяжки входов платы должны быть проверены; GPIO35/36 без внутренних подтяжек.\n'
                   if endstops else '# Без концевиков, homing и программных границ. Реальные ходы измеряются отдельно.\n')
    layout_notice = LAYOUT_NOTICE if parameters.get('wiring_profile') == WIRING_PROFILE else LEGACY_LAYOUT_NOTICE
    header = ('# ПРОЕКТ КОНФИГУРАЦИИ, НЕ ЗАГРУЖАЕТСЯ АВТОМАТИЧЕСКИ\n'
              '# ' + layout_notice + '\n'
              + mode_notice +
              '# M8 = питание катушки ВКЛ, M9 = ВЫКЛ; направление ручки задаётся в пульте.\n'
              '# Реле через coolant: постоянный цифровой уровень, без Laser/PWM.\n'
              '# Штатная FluidNC не добавляет аппаратный таймер/датчик температуры соленоида.\n')
    return header + yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


def show_setup(parent, data_dir=None):
    """Open a non-modal Tk Toplevel; export a draft only, never touch a board.

    Returns the Toplevel. Optional data_dir selects the initial save directory.
    """
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    window = tk.Toplevel(parent)
    window.title('FluidNC — проект конфигурации платы')
    window.geometry(f'{min(1020, max(790, parent.winfo_screenwidth() - 80))}x'
                    f'{min(850, max(560, parent.winfo_screenheight() - 100))}')
    window.minsize(790, 560)
    defaults = new_wiring_defaults()
    # The setup form can grow when homing is selected; keep the export controls
    # reachable on smaller screens instead of clipping the lower half.
    outer = ttk.Frame(window, padding=12)
    outer.pack(fill='both', expand=True)
    form_container = ttk.Frame(outer)
    form_container.pack(fill='both', expand=True)
    canvas = tk.Canvas(form_container, highlightthickness=0, borderwidth=0)
    form_scroll = ttk.Scrollbar(form_container, orient='vertical', command=canvas.yview)
    canvas.configure(yscrollcommand=form_scroll.set)
    canvas.pack(side='left', fill='both', expand=True)
    form_scroll.pack(side='right', fill='y')
    body = ttk.Frame(canvas, padding=(4, 4, 16, 8))
    form_item = canvas.create_window((0, 0), window=body, anchor='nw')
    body.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>', lambda event: canvas.itemconfigure(form_item, width=event.width))
    window.bind('<MouseWheel>', lambda event: canvas.yview_scroll(-int(event.delta / 120), 'units'))
    ttk.Label(body, text='Конфигурация MKS DLC32 v2.2',
              font=('Segoe UI', 17, 'bold')).pack(anchor='w')
    ttk.Label(body, text=WIRING_LABEL, wraplength=900, font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(4, 6))
    ttk.Label(body, text='X в программе — каретка ручки; Y — весь портал. Физические надписи X/Y/Z на плате не переименовываются.',
              wraplength=900).pack(anchor='w', pady=(0, 6))
    ttk.Label(body, text=LAYOUT_NOTICE, wraplength=850, foreground='#6a4c12').pack(anchor='w', pady=(0, 10))
    variables = {}
    name = tk.StringVar(value=defaults['name'])
    name_row = ttk.Frame(body)
    name_row.pack(fill='x', pady=(0, 8))
    ttk.Label(name_row, text='Название').pack(side='left')
    ttk.Entry(name_row, textvariable=name).pack(side='left', fill='x', expand=True, padx=(10, 0))
    columns = ttk.Frame(body)
    columns.pack(fill='x')
    direction_vars = {}
    side_vars = {}
    for index, axis in enumerate(('x', 'y')):
        role = 'X — каретка, разъём Y1' if axis == 'x' else 'Y — портал, разъёмы X + Z'
        frame = ttk.LabelFrame(columns, text=role, padding=10)
        frame.grid(row=0, column=index, sticky='nsew', padx=(0, 8) if index == 0 else (8, 0))
        columns.columnconfigure(index, weight=1)
        for row, (key, title) in enumerate((('steps_per_mm', 'Шагов / мм'), ('max_rate', 'Подача, мм/мин'),
                                          ('acceleration', 'Ускорение, мм/с²'), ('travel', 'Измеренный ход, мм'))):
            variable = tk.StringVar(value='')
            variables[axis + '_' + key] = variable
            ttk.Label(frame, text=title).grid(row=row, column=0, sticky='w', pady=3)
            ttk.Entry(frame, textvariable=variable, width=15).grid(row=row, column=1, sticky='ew', padx=(10, 0), pady=3)
        frame.columnconfigure(1, weight=1)
        direction = tk.StringVar(value='Не выбрано')
        direction_vars['invert_' + axis] = direction
        ttk.Label(frame, text='Направление мотора').grid(row=4, column=0, sticky='w', pady=4)
        ttk.Combobox(frame, textvariable=direction, values=('Не выбрано', 'Обычное', 'Инвертировать'),
                     state='readonly', width=17).grid(row=4, column=1, sticky='ew', padx=(10, 0), pady=4)
        side = tk.StringVar(value='Не выбрано')
        side_vars[axis] = side
        ttk.Label(frame, text='Сторона концевика').grid(row=5, column=0, sticky='w', pady=4)
        ttk.Combobox(frame, textvariable=side, values=('Не выбрано', axis.upper() + '−', axis.upper() + '+'),
                     state='readonly', width=17).grid(row=5, column=1, sticky='ew', padx=(10, 0), pady=4)
    dual_row = ttk.Frame(body)
    dual_row.pack(fill='x', pady=8)
    ttk.Label(dual_row, text='Мотор портала №2 (разъём Z):').pack(side='left')
    direction_vars['invert_y2'] = tk.StringVar(value='Не выбрано')
    ttk.Combobox(dual_row, textvariable=direction_vars['invert_y2'],
                 values=('Не выбрано', 'Обычное', 'Инвертировать'), state='readonly', width=18).pack(side='left', padx=10)
    ttk.Label(body, text='Шаги/мм включают микрошаг драйвера. Направления обоих моторов портала проверяются отдельно; Y2 платы свободен.',
              wraplength=900).pack(anchor='w', pady=(0, 8))
    relay = ttk.LabelFrame(body, text='Реле подъёма ручки', padding=8)
    relay.pack(fill='x', pady=5)
    relay_level = tk.StringVar(value='Не выбрано')
    ttk.Label(relay, text='LASER/TTL: S → IN, GPIO32').grid(row=0, column=0, columnspan=2, sticky='w')
    ttk.Label(relay, text='Уровень включения').grid(row=0, column=2, padx=(12, 6))
    ttk.Combobox(relay, textvariable=relay_level, values=('Не выбрано', 'HIGH', 'LOW'), state='readonly', width=14).grid(row=0, column=3)
    ttk.Label(relay, text='Постоянный уровень через coolant, без ШИМ. Фиолетовый 5V→VCC, серый G→GND, белый S→IN. Vin лазера не подключать к реле.',
              wraplength=900).grid(row=1, column=0, columnspan=4, sticky='w', pady=(5, 0))
    stops = ttk.LabelFrame(body, text='Концевики и поиск исходного положения', padding=8)
    stops.pack(fill='x', pady=6)
    stop_mode = tk.StringVar(value='Два NC-концевика')
    ttk.Combobox(stops, textvariable=stop_mode, values=('Два NC-концевика', 'Без концевиков'),
                 state='readonly', width=25).pack(anchor='w')
    ttk.Label(stops, text='LIMIT X (GPIO36) — каретка X; LIMIT Y (GPIO35) — портал Y. COM→G, NC→S; 5V не использовать. Входные подтяжки сверить на плате.',
              wraplength=900).pack(anchor='w', pady=5)
    hard_limits = tk.BooleanVar(value=False)
    homing_enabled = tk.BooleanVar(value=False)
    ttk.Checkbutton(stops, text='Останов по концевикам (включать после проверки входов)', variable=hard_limits).pack(anchor='w')
    ttk.Checkbutton(stops, text='Настроить homing X, затем Y (запускается отдельно командой $H)', variable=homing_enabled).pack(anchor='w')
    homing_frame = ttk.Frame(stops)
    for col, axis in enumerate(('x', 'y')):
        hf = ttk.LabelFrame(homing_frame, text='Поиск ' + axis.upper(), padding=8)
        hf.grid(row=0, column=col, sticky='nsew', padx=(0, 8))
        homing_frame.columnconfigure(col, weight=1)
        for row, (key, label) in enumerate((('homing_seek', 'Поиск, мм/мин'), ('homing_feed', 'Точный подход, мм/мин'),
                                           ('homing_pulloff', 'Отход, мм'), ('homing_mpos', 'Координата после поиска, мм'))):
            variable = tk.StringVar(value='')
            variables[axis + '_' + key] = variable
            ttk.Label(hf, text=label).grid(row=row, column=0, sticky='w', pady=2)
            ttk.Entry(hf, textvariable=variable, width=12).grid(row=row, column=1, padx=8, pady=2)
    def show_homing(*_):
        if homing_enabled.get():
            homing_frame.pack(fill='x', pady=6)
        else:
            homing_frame.pack_forget()
    homing_enabled.trace_add('write', show_homing)
    confirmations = {}
    for key, title in (
        ('pins_verified', 'Я сверил назначения I2S и GPIO с конкретной платой v2.2 и проводкой'),
        ('parameters_verified', 'Шаги/мм, микрошаги, подачи, ускорения и реальные ходы введены осознанно'),
        ('endstops_verified', 'NC-концевики и подтяжки проверены; нажатию соответствует активный вход'),
        ('no_endstops_confirmed', 'Только для режима «Без концевиков»: осознанно выбираю отсутствие датчиков и homing')):
        variable = tk.BooleanVar(value=False)
        confirmations[key] = variable
        ttk.Checkbutton(body, text=title, variable=variable).pack(anchor='w', pady=1)
    preview_frame = ttk.Frame(outer)
    preview_frame.pack(fill='x', pady=8)
    preview = tk.Text(preview_frame, height=7, width=70, wrap='none', font=('Consolas', 9), state='disabled')
    scroll = ttk.Scrollbar(preview_frame, command=preview.yview)
    preview.configure(yscrollcommand=scroll.set)
    preview.pack(side='left', fill='both', expand=True)
    scroll.pack(side='right', fill='y')
    status = tk.StringVar(value='Новая схема выбрана. Измеренные параметры, направления и уровень реле нужно заполнить.')
    ttk.Label(outer, textvariable=status, wraplength=940).pack(anchor='w')
    buttons = ttk.Frame(outer)
    buttons.pack(fill='x', pady=(8, 0))

    def values():
        parameters = new_wiring_defaults()
        parameters.update({key: variable.get().replace(',', '.') for key, variable in variables.items()})
        parameters.update({key: variable.get() for key, variable in confirmations.items()})
        if relay_level.get() not in ('HIGH', 'LOW'):
            raise ValueError('Выберите проверенный уровень включения реле HIGH или LOW.')
        for key, variable in direction_vars.items():
            if variable.get() not in ('Обычное', 'Инвертировать'):
                raise ValueError('Явно выберите проверенное направление: ' + key + '.')
            parameters[key] = variable.get() == 'Инвертировать'
        for axis, variable in side_vars.items():
            choice = variable.get()
            parameters[axis + '_limit_positive'] = (choice.endswith('+') if choice != 'Не выбрано' else None)
        parameters.update(name=name.get(), endstops='nc' if stop_mode.get() == 'Два NC-концевика' else 'none',
                          hard_limits=hard_limits.get(), homing_enabled=homing_enabled.get(),
                          relay_active_low=relay_level.get() == 'LOW')
        return parameters

    def render(require_confirmation=False):
        text = generate_yaml(values(), require_confirmation=require_confirmation)
        preview.configure(state='normal')
        preview.delete('1.0', 'end')
        preview.insert('1.0', text)
        preview.configure(state='disabled')
        return text

    def show_preview():
        try:
            render()
            status.set('Предпросмотр проекта. На плату ничего не отправлено.')
        except ValueError as exc:
            messagebox.showerror('Параметры FluidNC', str(exc), parent=window)

    def export():
        try:
            text = render(require_confirmation=True)
        except ValueError as exc:
            messagebox.showerror('Параметры FluidNC', str(exc), parent=window)
            return
        directory = Path(data_dir) if data_dir is not None else Path(__file__).resolve().parent
        while not directory.exists() and directory != directory.parent:
            directory = directory.parent
        filename = filedialog.asksaveasfilename(parent=window, title='Сохранить проект конфигурации FluidNC',
                    initialdir=str(directory), initialfile='neiroslopper-ttl.draft.yaml', defaultextension='.yaml',
                    filetypes=[('FluidNC YAML', '*.yaml'), ('Все файлы', '*.*')])
        if not filename:
            return
        try:
            Path(filename).write_text(text, encoding='utf-8')
        except OSError as exc:
            messagebox.showerror('Сохранение файла', str(exc), parent=window)
            return
        status.set('Проект сохранён: ' + filename + '. Загрузка на плату не выполнялась.')

    ttk.Button(buttons, text='Предпросмотр YAML', command=show_preview).pack(side='left')
    ttk.Button(buttons, text='Экспорт проекта…', command=export).pack(side='left', padx=8)
    ttk.Button(buttons, text='Закрыть', command=window.destroy).pack(side='right')
    return window
