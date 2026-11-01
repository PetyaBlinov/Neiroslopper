"""Profiles for Arduino GRBL and official FluidNC; inspection never writes hardware.

FluidNC YAML is the source of truth. Integer settings are an internal compatibility
view for the existing sender, not legacy settings sent back to an ESP32.
"""
import json
import math
import re
from pathlib import Path

UNO = 'uno_grbl11'
DLC32 = 'mks_dlc32_fluidnc'
PROFILE_LABELS = {UNO: 'Arduino Uno / GRBL 1.1', DLC32: 'MKS DLC32 v2.2 / FluidNC'}
FLUIDNC_CONFIG_NAME = 'Mini_CNC_Plotter_A4'
PROFILE_KEY = '_verified_machine_profile'
# FluidNC v4.1.1, Machine/Axis.cpp handler.item bounds (inclusive).
AXIS_PARAMETER_LIMITS = {
    'steps_per_mm': (0.001, 100000.0),
    'max_rate_mm_per_min': (0.001, 250000.0),
    'acceleration_mm_per_sec2': (0.001, 100000.0),
    'max_travel_mm': (0.1, 10000000.0),
}


def load_selected_profile(directory):
    if directory is None:
        return DLC32
    try:
        value = json.loads((Path(directory) / 'machine-profile.json').read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or set(value) != {'version', 'machine_profile'} or
                type(value['version']) is not int or value['version'] != 1):
            return DLC32
        return profile_id(value['machine_profile'])
    except (OSError, ValueError, TypeError):
        return DLC32


def save_selected_profile(directory, selected):
    selected = profile_id(selected)
    if directory is None:
        return
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / 'machine-profile.json'
    temporary = target.with_suffix('.json.tmp')
    temporary.write_text(json.dumps({'version': 1, 'machine_profile': selected}, indent=2), encoding='utf-8')
    temporary.replace(target)


def pen_settings_directory(directory, selected):
    if directory is None:
        return None
    return Path(directory) if profile_id(selected) == UNO else Path(directory) / DLC32


def profile_id(value=UNO):
    if value in PROFILE_LABELS:
        return value
    for key, label in PROFILE_LABELS.items():
        if value == label:
            return key
    # The former label is accepted only as a UI migration, not as hardware proof.
    if value == 'MKS DLC32 V2.0 / FluidNC':
        return DLC32
    raise ValueError('Выберите поддерживаемую плату: Uno или MKS DLC32 / FluidNC.')


def app_profile(app):
    variable = getattr(app, 'machine_profile', None)
    return profile_id(variable.get()) if variable is not None else UNO


def assistant_facts(selected):
    selected = profile_id(selected)
    return {
        'machine_profile': selected,
        'controller_board': 'MKS DLC32 v2.2' if selected == DLC32 else 'Arduino Uno + CNC Shield V3',
        'controller_firmware': 'Official FluidNC' if selected == DLC32 else 'GRBL 1.1',
        'motor_socket_roles': 'Read actual config.yaml; optional dual-X requires explicit wiring verification',
        'pen_output_profile': 'Actual coolant/flood_pin and polarity from config.yaml; relay wiring must be checked',
        'holding_configuration': 'stepping/idle_ms in config.yaml' if selected == DLC32 else 'GRBL $1',
        'endstop_configuration': 'Read actual config.yaml; no assumed switches or homing',
        'dwin_connection': 'Custom JD-DGUS firmware required; not included in official FluidNC',
    }


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def settings_ready(settings, selected=UNO):
    if not isinstance(settings, dict):
        return False
    if profile_id(selected) == UNO:
        from controller import REQUIRED
        return all(settings.get(k) == v for k, v in REQUIRED.items())
    return (settings.get('_fluidnc_ready') is True and settings.get(PROFILE_KEY) == DLC32 and
            settings.get(13) == 0 and
            all(_positive(settings.get(k)) for k in (100, 101, 110, 111, 120, 121)))


def fluidnc_identity(lines):
    return any(re.match(r'\[VER:\d+\.\d+\s+FluidNC\s+v\d+\.\d+', line, re.I) for line in lines)


def fluidnc_pen_profile(settings=None):
    value = settings.get('_fluidnc_pen') if isinstance(settings, dict) else None
    return dict(value) if isinstance(value, dict) else None


def pen_matches(firmware, settings, selected=UNO):
    if profile_id(selected) == UNO:
        from pen_lift import firmware_profile
        return firmware == firmware_profile(settings.active_low, settings.continuous)
    return (isinstance(firmware, dict) and firmware.get('backend') == 'fluidnc' and
            type(firmware.get('active_low')) is bool and
            type(settings.active_low) is bool and
            firmware['active_low'] == settings.active_low and
            bool(firmware.get('flood_pin')))


def _load_yaml(lines):
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            if self.check_event(yaml.AliasEvent):
                raise ValueError('Ссылки YAML запрещены: для проверки нужна явная конфигурация без aliases.')
            return super().compose_node(parent, index)

    def mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if not isinstance(key, (str, int, float, bool, type(None))):
                raise ValueError('Некорректный ключ YAML FluidNC.')
            if key in result:
                raise ValueError('Повторяющееся поле в конфигурации FluidNC: ' + str(key))
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    if isinstance(lines, str):
        lines = lines.splitlines()
    text = '\n'.join(line for line in lines
                     if not line.startswith(('[MSG:', '[GC:', '[OPT:')) and line.strip() != 'ok')
    if len(text) > 120000:
        raise ValueError('Слишком большая конфигурация FluidNC.')
    try:
        result = yaml.load(text, Loader=UniqueLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError('Не удалось прочитать YAML FluidNC.') from exc
    if not isinstance(result, dict):
        raise ValueError('Конфигурация FluidNC должна быть YAML-объектом.')
    return result


def _pin(value):
    """Return (base, flags); unknown syntax remains invalid rather than guessed."""
    if value is None or str(value).strip().upper() == 'NO_PIN':
        return 'no_pin', ()
    if not isinstance(value, str):
        return '', ()
    parts = value.strip().lower().split(':')
    if not re.fullmatch(r'(?:gpio|i2so)\.\d+', parts[0]):
        return '', ()
    if any(flag not in ('high', 'low', 'pu', 'pd') for flag in parts[1:]):
        return '', ()
    if len(set(parts[1:])) != len(parts[1:]) or {'high', 'low'} <= set(parts[1:]):
        return '', ()
    return parts[0], tuple(parts[1:])


def _pin_is(value, expected, *, output=True):
    base, flags = _pin(value)
    return base == expected and (not output or not set(flags) - {'high', 'low'})


def _motor_active(motor):
    if not isinstance(motor, dict):
        return bool(motor)
    passive = {'null_motor', 'limit_neg_pin', 'limit_pos_pin', 'limit_all_pin',
               'hard_limits', 'pulloff_mm'}
    return (bool(set(motor) - passive) or
            any(_pin(motor.get(key, 'NO_PIN'))[0] != 'no_pin'
                for key in ('limit_neg_pin', 'limit_pos_pin', 'limit_all_pin')))


def fluidnc_configuration(lines, legacy=None):
    """Inspect a stock FluidNC dump and independently decide if XY motion is ready.

    Valid but unsupported/test configurations stay connected for diagnostics.
    Invalid YAML raises ValueError. No names, UARTs, DWIN or NC switches are required.
    This verifies the DLC32 I2S family topology; it cannot prove v2.2 PCB wiring.
    """
    config = _load_yaml(lines)
    result = {key: value for key, value in dict(legacy or {}).items()
              if not (isinstance(key, str) and key.startswith('_'))}
    result.update({'_fluidnc_ready': False, '_fluidnc_reason': '', '_fluidnc_details': [],
                   '_fluidnc_config': config, '_fluidnc_pen': None})
    reasons = []

    def reject(message):
        if message not in reasons:
            reasons.append(message)

    axes = config.get('axes')
    axes = axes if isinstance(axes, dict) else {}
    if not axes:
        reject('Не заданы физические оси X и Y.')
    if config.get('report_inches', False) is not False:
        reject('Для пульта нужны миллиметры: report_inches: false.')
    result[13] = int(config.get('report_inches', False) is not False)
    kine = config.get('kinematics') or {'Cartesian': {}}
    if not isinstance(kine, dict) or set(kine) != {'Cartesian'}:
        reject('Поддерживается только декартова кинематика Cartesian.')
    start = config.get('start') or {}
    if not isinstance(start, dict):
        reject('Некорректный раздел start.')
    elif start.get('must_home', False) is not False:
        reject('В этой версии используется ручная установка листа; start/must_home должен быть false.')
    macros = config.get('macros') or {}
    if not isinstance(macros, dict):
        reject('Некорректный раздел macros.')
    elif any(str(key).lower().startswith(('startup', 'after_homing', 'after_reset', 'after_unlock')) and value not in (None, '')
             for key, value in macros.items()):
        reject('Обнаружен автоматический макрос запуска: сначала проверьте его отдельно.')
    if any(str(key).lower().startswith(('startup', 'after_homing', 'after_reset', 'after_unlock')) and value not in (None, '')
           for key, value in config.items()):
        reject('Обнаружена автоматическая команда вне раздела macros.')

    stepping = config.get('stepping') or {}
    if not isinstance(stepping, dict) or str(stepping.get('engine', '')).upper() != 'I2S_STATIC':
        reject('Физические драйверы DLC32 требуют проверенного I2S_STATIC, а не тестовых null_motor.')
    result[1] = stepping.get('idle_ms', 0) if isinstance(stepping, dict) else 0
    i2s = config.get('i2so') or {}
    if not isinstance(i2s, dict) or any(not _pin_is(i2s.get(key), value) or
            set(_pin(i2s.get(key))[1]) - {'high'}
            for key, value in (('bck_pin', 'gpio.16'), ('ws_pin', 'gpio.17'), ('data_pin', 'gpio.21'))):
        reject('I2S должен соответствовать семейству DLC32: BCK16, WS17, DATA21; ревизию v2.2 сверяют отдельно.')
    disable_base, disable_flags = _pin(axes.get('shared_stepper_disable_pin'))
    if disable_base != 'i2so.0' or set(disable_flags) - {'high'}:
        reject('Общий ENABLE драйверов DLC32 должен быть i2so.0 (активный HIGH).')

    hard_limits = False
    soft_limits = False
    homing_enabled = False
    actual_motors = 0
    for index, name in enumerate(('x', 'y')):
        axis = axes.get(name)
        if not isinstance(axis, dict):
            reject('Не найдена физическая ось ' + name.upper() + '.')
            continue
        for source, destination in (('steps_per_mm', 100), ('max_rate_mm_per_min', 110),
                                    ('acceleration_mm_per_sec2', 120)):
            number = axis.get(source)
            minimum, maximum = AXIS_PARAMETER_LIMITS[source]
            if _positive(number) and minimum <= number <= maximum:
                result[destination + index] = float(number)
            else:
                result.pop(destination + index, None)
                reject('Параметр ' + name + '/' + source + f' должен быть от {minimum:g} до {maximum:g}.')
        travel = axis.get('max_travel_mm')
        travel_minimum, travel_maximum = AXIS_PARAMETER_LIMITS['max_travel_mm']
        if not _positive(travel) or not travel_minimum <= travel <= travel_maximum:
            reject(f'Измеренный max_travel_mm оси {name.upper()} должен быть от {travel_minimum:g} до {travel_maximum:g}.')
        if type(axis.get('soft_limits', False)) is not bool:
            reject('soft_limits должен быть true или false.')
        soft_limits |= axis.get('soft_limits', False) is True
        homing = axis.get('homing') or {}
        homing_enabled |= isinstance(homing, dict) and type(homing.get('cycle')) is int and homing['cycle'] > 0
        expected = [('i2so.1', 'i2so.2'), ('i2so.3', 'i2so.4')] if name == 'x' else [('i2so.5', 'i2so.6')]
        for motor_index, (step, direction) in enumerate(expected):
            motor = axis.get('motor' + str(motor_index))
            if motor_index and not _motor_active(motor):
                continue
            if not isinstance(motor, dict) or not isinstance(motor.get('stepstick'), dict):
                reject('Ось ' + name.upper() + '/motor' + str(motor_index) + ': нужен физический stepstick.')
                continue
            driver = motor['stepstick']
            if not _pin_is(driver.get('step_pin'), step) or not _pin_is(driver.get('direction_pin'), direction):
                reject('Неверная пара STEP/DIR ' + name.upper() + '/motor' + str(motor_index) + '.')
            if _pin(driver.get('disable_pin', 'NO_PIN'))[0] != 'no_pin':
                reject('Для stepstick используется общий ENABLE, отдельный disable_pin должен быть NO_PIN.')
            actual_motors += 1
            limits_present = any(_pin(motor.get(key, 'NO_PIN'))[0] != 'no_pin'
                                 for key in ('limit_neg_pin', 'limit_pos_pin', 'limit_all_pin'))
            if type(motor.get('hard_limits', False)) is not bool:
                reject('hard_limits должен быть true или false.')
            elif motor.get('hard_limits') is True:
                hard_limits = True
                if not limits_present:
                    reject('hard_limits включён без назначенного концевика.')
            for key in ('limit_neg_pin', 'limit_pos_pin', 'limit_all_pin'):
                pin, flags = _pin(motor.get(key, 'NO_PIN'))
                if pin != 'no_pin' and (pin not in ('gpio.34', 'gpio.35', 'gpio.36') or set(flags) - {'high', 'low'}):
                    reject('Неподдерживаемый контакт концевика ' + name.upper() + '/' + key + '.')
        for key, motor in axis.items():
            if re.fullmatch(r'motor\d+', str(key)) and int(key[5:]) >= len(expected) and _motor_active(motor):
                reject('Лишний активный мотор оси ' + name.upper() + '.')
    for name, axis in axes.items():
        if name in ('x', 'y', 'shared_stepper_disable_pin', 'shared_stepper_reset_pin', 'homing_runs'):
            continue
        if isinstance(axis, dict) and any(_motor_active(motor) for key, motor in axis.items()
                                         if re.fullmatch(r'motor\d+', str(key))):
            reject('Неподдерживаемая активная ось ' + str(name).upper() + '; соленоид не является шаговой осью Z.')
        elif not isinstance(axis, dict) and axis not in (None, False, ''):
            reject('Некорректная или неизвестная активная ось ' + str(name).upper() + '.')
    reset_pin = axes.get('shared_stepper_reset_pin', 'NO_PIN')
    if _pin(reset_pin)[0] != 'no_pin':
        reject('Неожиданный общий RESET драйверов: проверьте схему отдельно.')

    coolant = config.get('coolant') or {}
    if not isinstance(coolant, dict):
        reject('Некорректный раздел coolant.')
    else:
        base, flags = _pin(coolant.get('flood_pin', 'NO_PIN'))
        if base != 'no_pin':
            # ESP32 pins 34..39 are input-only; GPIO0 is a boot strap, GPIO1/3
            # own the USB UART, GPIO6..11 own flash; GPIO16/17/21 own I2S here.
            allowed = {2, 4, 5, 12, 13, 14, 15, 18, 19, 22, 23, 25, 26, 27, 32, 33}
            match = re.fullmatch(r'gpio\.(\d+)', base)
            if not match or int(match[1]) not in allowed or set(flags) - {'high', 'low'}:
                reject('Реле требует проверенного выходного GPIO, не занятого USB, flash, I2S или входами.')
            else:
                full_pin = base + (':low' if 'low' in flags else '')
                result['_fluidnc_pen'] = {'backend': 'fluidnc', 'active_low': 'low' in flags,
                                          'flood_pin': full_pin, 'hardware_watchdog': False}
                def other_pin_claims(node, path=()):
                    if isinstance(node, dict):
                        for key, value in node.items():
                            child = path + (str(key),)
                            if child == ('coolant', 'flood_pin'):
                                continue
                            if str(key).endswith('_pin') and _pin(value)[0] == base:
                                yield '/'.join(child)
                            yield from other_pin_claims(value, child)
                    elif isinstance(node, list):
                        for index, value in enumerate(node):
                            yield from other_pin_claims(value, path + (str(index),))
                claims = list(other_pin_claims(config))
                if claims:
                    reject('GPIO реле также занят другим узлом: ' + ', '.join(claims) + '.')
        if _pin(coolant.get('mist_pin', 'NO_PIN'))[0] != 'no_pin':
            reject('Дополнительный выход M7/mist не поддерживается этим пультом.')
    result.update({20: int(soft_limits), 21: int(hard_limits), 22: int(homing_enabled)})
    if actual_motors < 2:
        reject('Подключение доступно для диагностики. Сейчас нет полной физической конфигурации X/Y.')
    result['_fluidnc_details'] = list(reasons)
    result['_fluidnc_reason'] = ('Загружена тестовая конфигурация без двигателей; подготовьте config.yaml плоттера.'
                                if actual_motors == 0 else ' '.join(reasons))
    if not reasons:
        result['_fluidnc_ready'] = True
        result[PROFILE_KEY] = DLC32
    return result
