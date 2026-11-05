import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import machine_profiles as profiles
from fluidnc_setup import generate_config, generate_yaml
from pen_lift import PenLiftSettings
import yaml


def parameters(**overrides):
    result = dict(name='Тестовый плоттер',
        x_steps_per_mm=40, y_steps_per_mm=80,
        x_max_rate=1200, y_max_rate=1000,
        x_acceleration=100, y_acceleration=80,
        x_travel=320, y_travel=230,
        invert_x=False, invert_y=True, dual_x=False, invert_x2=False,
        relay_pin='', relay_active_low=False,
        pins_verified=True, parameters_verified=True, no_endstops_confirmed=True)
    result.update(overrides)
    return result


def inspect(config):
    return profiles.fluidnc_configuration(yaml.safe_dump(config, allow_unicode=True).splitlines())


class MachineProfileTests(unittest.TestCase):
    def setUp(self):
        self.config = generate_config(parameters())

    def test_physical_xy_without_endstops_or_relay(self):
        settings = inspect(self.config)
        self.assertTrue(profiles.settings_ready(settings, profiles.DLC32))
        self.assertEqual(settings[100], 40)
        self.assertEqual(settings[101], 80)
        self.assertEqual(settings[21], 0)
        self.assertIsNone(profiles.fluidnc_pen_profile(settings))

    def test_config_dump_canonical_i2s_and_yaml_alias_are_accepted(self):
        for engine in ('I2S', 'I2S_STATIC'):
            with self.subTest(engine=engine):
                config = copy.deepcopy(self.config)
                config['stepping']['engine'] = engine
                settings = inspect(config)
                self.assertTrue(profiles.settings_ready(settings, profiles.DLC32),
                                settings['_fluidnc_details'])

    def test_other_stepping_engines_do_not_enable_physical_motion(self):
        for engine in ('RMT', 'I2S_STREAM', 'UNKNOWN', '', None):
            with self.subTest(engine=engine):
                config = copy.deepcopy(self.config)
                config['stepping']['engine'] = engine
                settings = inspect(config)
                self.assertFalse(settings['_fluidnc_ready'])
                self.assertTrue(any('I2S' in reason for reason in settings['_fluidnc_details']))

    def test_axis_names_that_collide_by_case_are_rejected_in_either_order(self):
        for first, second in (('x', 'X'), ('X', 'x')):
            with self.subTest(first=first):
                config = copy.deepcopy(self.config)
                x_axis = config['axes'].pop('x')
                config['axes'][first] = copy.deepcopy(x_axis)
                config['axes'][second] = copy.deepcopy(x_axis)
                settings = inspect(config)
                self.assertFalse(settings['_fluidnc_ready'])
                self.assertTrue(any('Повторяющаяся ось' in reason
                                    for reason in settings['_fluidnc_details']))

    def test_default_test_drive_stays_diagnostic(self):
        config = {'name': 'Default (Test Drive no I/O)', 'board': 'None',
                  'stepping': {'engine': 'RMT'},
                  'axes': {'x': {'motor0': {'null_motor': {}}},
                           'y': {'motor0': {'null_motor': {}}}}}
        settings = inspect(config)
        self.assertFalse(settings['_fluidnc_ready'])
        self.assertNotIn(profiles.PROFILE_KEY, settings)
        self.assertEqual(settings['_fluidnc_config']['board'], 'None')
        self.assertTrue(settings['_fluidnc_reason'])
        self.assertIsNone(profiles.fluidnc_pen_profile(settings))
        self.assertLess(len(settings['_fluidnc_reason']), 120)
        self.assertGreater(len(settings['_fluidnc_details']), 1)

    def test_arbitrary_config_name_and_board_are_not_hardware_proof(self):
        self.config['name'] = 'Имя без JD и DGUS'
        self.config['board'] = 'User verified custom name'
        self.assertTrue(inspect(self.config)['_fluidnc_ready'])

    def test_optional_dual_x_on_z_socket(self):
        config = generate_config(parameters(dual_x=True, invert_x2=True))
        self.assertEqual(config['axes']['x']['motor1']['stepstick']['direction_pin'], 'i2so.4:low')
        self.assertTrue(inspect(config)['_fluidnc_ready'])
        self.assertNotIn('z', config['axes'])

    def test_step_pin_inversion_is_not_discarded(self):
        self.config['axes']['x']['motor0']['stepstick']['step_pin'] = 'i2so.1:low'
        self.assertTrue(inspect(self.config)['_fluidnc_ready'])

    def test_missing_physical_motor_is_not_ready(self):
        self.config['axes']['y']['motor0'] = {'null_motor': {}}
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_wrong_pin_pair_is_not_ready(self):
        self.config['axes']['y']['motor0']['stepstick']['step_pin'] = 'i2so.3'
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_inverted_i2s_bus_clock_not_ready(self):
        self.config['i2so']['bck_pin'] = 'gpio.16:low'
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_string_soft_limit_flag_not_ready(self):
        self.config['axes']['x']['soft_limits'] = 'false'
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_independent_z_is_not_ready(self):
        self.config['axes']['z'] = copy.deepcopy(self.config['axes']['y'])
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_unknown_active_axis_is_not_ready(self):
        self.config['axes']['u'] = {'motor0': {'stepstick': {'step_pin': 'gpio.27'}}}
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_malformed_unknown_axis_is_not_ready(self):
        self.config['axes']['z'] = True
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_unused_default_z_is_allowed(self):
        self.config['axes']['z'] = {'motor0': {'null_motor': {}, 'hard_limits': False}}
        self.assertTrue(inspect(self.config)['_fluidnc_ready'])

    def test_startup_macro_is_never_silently_accepted(self):
        self.config['macros']['startup_line0'] = 'M8'
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_after_unlock_macro_is_not_silently_run(self):
        self.config['macros']['after_unlock'] = 'M8'
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_top_level_after_unlock_macro_rejected(self):
        self.config['after_unlock'] = 'G1 X20'
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_top_level_startup_is_not_ignored(self):
        self.config['startup_line0'] = 'G1 X20'
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_broken_yaml_raises(self):
        with self.assertRaises(ValueError):
            profiles.fluidnc_configuration(['axes: ['])

    def test_duplicate_nested_keys_raise(self):
        with self.assertRaises(ValueError):
            profiles.fluidnc_configuration(['axes:', '  x:', '    steps_per_mm: 40', '    steps_per_mm: 80'])

    def test_aliases_are_not_expanded(self):
        with self.assertRaises(ValueError):
            profiles.fluidnc_configuration(['axes: &a {x: {}}', 'other: *a'])

    def test_scalar_yaml_raises(self):
        with self.assertRaises(ValueError):
            profiles.fluidnc_configuration(['hello'])

    def test_legacy_verified_marker_is_not_reused(self):
        settings = profiles.fluidnc_configuration(['name: Test'], {profiles.PROFILE_KEY: profiles.DLC32,
            '_fluidnc_ready': True, 100: 40, 101: 40})
        self.assertFalse(settings['_fluidnc_ready'])
        self.assertNotIn(profiles.PROFILE_KEY, settings)

    def test_coolant_actual_gpio_and_low_flag(self):
        config = generate_config(parameters(relay_pin='32', relay_active_low=True))
        settings = inspect(config)
        pen = profiles.fluidnc_pen_profile(settings)
        self.assertEqual(pen['flood_pin'], 'gpio.32:low')
        self.assertTrue(pen['active_low'])
        self.assertFalse(pen['hardware_watchdog'])
        self.assertTrue(profiles.pen_matches(pen, PenLiftSettings(active_low=True, continuous=False), profiles.DLC32))
        self.assertFalse(profiles.pen_matches(pen, PenLiftSettings(active_low=False), profiles.DLC32))

    def test_no_fake_pen_profile_without_settings(self):
        self.assertIsNone(profiles.fluidnc_pen_profile())

    def test_input_only_gpio_rejected(self):
        self.config['coolant']['flood_pin'] = 'gpio.36'
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_shared_uart_and_relay_pin_rejected(self):
        self.config['coolant']['flood_pin'] = 'gpio.23'
        self.config['uart1'] = {'txd_pin': 'gpio.23', 'rxd_pin': 'gpio.33', 'baud': 115200}
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_non_cartesian_is_not_ready(self):
        self.config['kinematics'] = {'CoreXY': {}}
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_hard_limits_without_input_rejected(self):
        self.config['axes']['x']['motor0']['hard_limits'] = True
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_explicit_nc_endstop_allowed(self):
        self.config['axes']['x']['motor0'].update(limit_neg_pin='gpio.36', hard_limits=True)
        self.assertTrue(inspect(self.config)['_fluidnc_ready'])

    def test_invalid_numeric_axis_not_ready(self):
        self.config['axes']['y']['steps_per_mm'] = float('nan')
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_official_axis_range_endpoints_are_accepted(self):
        # Literal primary-source bounds, rather than values copied from the module.
        fields = [('steps_per_mm', .001, 100000),
                  ('max_rate_mm_per_min', .001, 250000),
                  ('acceleration_mm_per_sec2', .001, 100000),
                  ('max_travel_mm', .1, 10000000)]
        for axis in ('x', 'y'):
            for key, minimum, maximum in fields:
                for value in (minimum, maximum):
                    with self.subTest(axis=axis, key=key, value=value):
                        config = copy.deepcopy(self.config)
                        config['axes'][axis][key] = value
                        self.assertTrue(inspect(config)['_fluidnc_ready'])

    def test_axis_values_outside_official_range_are_not_ready(self):
        fields = [('steps_per_mm', .001, 100000),
                  ('max_rate_mm_per_min', .001, 250000),
                  ('acceleration_mm_per_sec2', .001, 100000),
                  ('max_travel_mm', .1, 10000000)]
        for axis in ('x', 'y'):
            for key, minimum, maximum in fields:
                for value in (minimum / 2, maximum + 1):
                    with self.subTest(axis=axis, key=key, value=value):
                        config = copy.deepcopy(self.config)
                        config['axes'][axis][key] = value
                        self.assertFalse(inspect(config)['_fluidnc_ready'])

    def test_no_magic_uart_or_dwin_requirement(self):
        self.assertNotIn('uart1', self.config)
        self.assertTrue(inspect(self.config)['_fluidnc_ready'])

    def test_profile_persistence_and_default(self):
        self.assertEqual(profiles.load_selected_profile(None), profiles.DLC32)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            profiles.save_selected_profile(folder, profiles.UNO)
            self.assertEqual(profiles.load_selected_profile(folder), profiles.UNO)


class NewWiringInspectionTests(unittest.TestCase):
    def setUp(self):
        from test_wiring_setup import commissioned_parameters
        self.config = generate_config(commissioned_parameters(hard_limits=True))

    def test_three_physical_sockets_are_a_two_axis_machine(self):
        settings = inspect(self.config)
        self.assertTrue(settings['_fluidnc_ready'], settings['_fluidnc_details'])
        self.assertEqual(settings['_fluidnc_wiring'], 'neiroslopper_ttl')
        self.assertEqual(settings['_fluidnc_endstops'], {'x': ('gpio.36',), 'y': ('gpio.35',)})
        self.assertEqual(settings[21], 1)
        self.assertEqual(profiles.fluidnc_pen_profile(settings)['flood_pin'], 'gpio.32:low')

    def canonical_dump_config(self):
        config = copy.deepcopy(self.config)
        config['stepping']['engine'] = 'I2S'

        def uppercase_pins(node):
            for key, value in node.items():
                if isinstance(value, dict):
                    uppercase_pins(value)
                elif key.endswith('_pin') and isinstance(value, str):
                    node[key] = value.upper()

        uppercase_pins(config)
        axes = config['axes']
        axes['X'] = axes.pop('x')
        axes['Y'] = axes.pop('y')
        axes['Z'] = {'motor0': {'null_motor': {}, 'hard_limits': False,
                             'limit_neg_pin': 'NO_PIN', 'limit_pos_pin': 'NO_PIN'}}
        return config

    def test_canonical_i2s_dump_preserves_pin_and_three_motor_checks(self):
        config = self.canonical_dump_config()
        before = copy.deepcopy(config)
        settings = inspect(config)
        self.assertTrue(settings['_fluidnc_ready'], settings['_fluidnc_details'])
        self.assertEqual(settings['_fluidnc_wiring'], 'neiroslopper_ttl')
        self.assertIn('X', settings['_fluidnc_config']['axes'])
        self.assertNotIn('x', settings['_fluidnc_config']['axes'])
        self.assertEqual(config, before)

        broken_bus = copy.deepcopy(config)
        broken_bus['i2so']['bck_pin'] = 'GPIO.18'
        self.assertFalse(inspect(broken_bus)['_fluidnc_ready'])

        missing_motor = copy.deepcopy(config)
        del missing_motor['axes']['Y']['motor1']
        self.assertFalse(inspect(missing_motor)['_fluidnc_ready'])

        wrong_socket = copy.deepcopy(config)
        wrong_socket['axes']['Y']['motor1']['stepstick']['step_pin'] = 'I2SO.5'
        self.assertFalse(inspect(wrong_socket)['_fluidnc_ready'])

        active_z = copy.deepcopy(config)
        active_z['axes']['Z']['motor0'] = {'stepstick': {'step_pin': 'I2SO.7'}}
        self.assertFalse(inspect(active_z)['_fluidnc_ready'])

    def test_canonical_commissioning_marker_blocks_motion_until_removed(self):
        config = self.canonical_dump_config()
        config['meta'] = 'NEIROSLOPPER_COMMISSIONING 2026-11-03; unverified machine'
        settings = inspect(config)
        self.assertFalse(profiles.settings_ready(settings, profiles.DLC32))
        self.assertTrue(any('Пусковая конфигурация' in reason
                            for reason in settings['_fluidnc_details']))
        self.assertIn('направления моторов, масштаб и реле', settings['_fluidnc_reason'])

        config['meta'] = 'Physically commissioned machine'
        settings = inspect(config)
        self.assertTrue(profiles.settings_ready(settings, profiles.DLC32),
                        settings['_fluidnc_details'])

    def test_canonical_required_homing_without_cycles_is_still_rejected(self):
        config = self.canonical_dump_config()
        config['start']['must_home'] = True
        settings = inspect(config)
        self.assertFalse(settings['_fluidnc_ready'])
        self.assertTrue(any('Обязательное базирование' in reason
                            for reason in settings['_fluidnc_details']))

    def test_second_portal_motor_cannot_silently_disappear(self):
        del self.config['axes']['y']['motor1']
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_duplicate_socket_between_axes_is_rejected(self):
        self.config['axes']['y']['motor0']['stepstick'] = copy.deepcopy(
            self.config['axes']['x']['motor0']['stepstick'])
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_switch_roles_cannot_be_swapped_with_motor_socket_names(self):
        self.config['axes']['x']['motor0']['limit_neg_pin'] = 'gpio.35'
        self.config['axes']['y']['motor0']['limit_neg_pin'] = 'gpio.36'
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_portal_has_one_switch_not_a_fake_second_copy(self):
        self.config['axes']['y']['motor1']['limit_neg_pin'] = 'gpio.35'
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_ttl_pin_cannot_also_be_a_laser_output(self):
        self.config['Laser'] = {'output_pin': 'gpio.32', 'pwm_hz': 5000}
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def test_new_wiring_does_not_accept_old_scl_relay_assignment(self):
        self.config['coolant']['flood_pin'] = 'gpio.4'
        self.assertFalse(inspect(self.config)['_fluidnc_ready'])

    def homing_config(self):
        config = copy.deepcopy(self.config)
        config['start']['must_home'] = True
        for index, axis in enumerate(('x', 'y'), 1):
            config['axes'][axis]['homing'] = dict(cycle=index, positive_direction=False,
                feed_mm_per_min=60, seek_mm_per_min=300, mpos_mm=0)
            config['axes'][axis]['motor0']['pulloff_mm'] = 2
        config['axes']['y']['motor1']['pulloff_mm'] = 2
        return config

    def test_explicit_homing_configuration_is_inspected_without_executing_it(self):
        settings = inspect(self.homing_config())
        self.assertTrue(settings['_fluidnc_ready'], settings['_fluidnc_details'])
        self.assertEqual(settings[22], 1)

    def test_required_homing_without_switches_is_not_ready(self):
        config = self.homing_config()
        config['axes']['x']['motor0']['limit_neg_pin'] = 'NO_PIN'
        self.assertFalse(inspect(config)['_fluidnc_ready'])

    def test_homing_direction_must_match_switch_side(self):
        config = self.homing_config()
        config['axes']['y']['homing']['positive_direction'] = True
        self.assertFalse(inspect(config)['_fluidnc_ready'])

    def test_shared_portal_switch_requires_equal_pull_offs(self):
        config = self.homing_config()
        config['axes']['y']['motor1']['pulloff_mm'] = 3
        self.assertFalse(inspect(config)['_fluidnc_ready'])

    def test_required_homing_cannot_be_satisfied_by_only_one_axis(self):
        config = self.homing_config()
        del config['axes']['y']['homing']
        self.assertFalse(inspect(config)['_fluidnc_ready'])


class ConfigurationGeneratorTests(unittest.TestCase):
    def test_yaml_round_trip_and_inputs_unchanged(self):
        values = parameters()
        before = copy.deepcopy(values)
        text = generate_yaml(values)
        self.assertIn('ПРОЕКТ КОНФИГУРАЦИИ', text)
        self.assertEqual(values, before)
        self.assertTrue(profiles.fluidnc_configuration(text.splitlines())['_fluidnc_ready'])

    def test_no_default_relay_or_endstops(self):
        config = generate_config(parameters())
        self.assertEqual(config['coolant']['flood_pin'], 'NO_PIN')
        self.assertFalse(config['start']['check_limits'])
        self.assertNotIn('homing', config['axes']['x'])

    def test_explicit_confirmations_required(self):
        for key in ('pins_verified', 'parameters_verified', 'no_endstops_confirmed'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                generate_yaml(parameters(**{key: False}))

    def test_preview_can_precede_confirmation(self):
        text = generate_yaml(parameters(pins_verified=False, parameters_verified=False,
                             no_endstops_confirmed=False), require_confirmation=False)
        self.assertIn('flood_pin: NO_PIN', text)

    def test_values_are_not_invented(self):
        values = parameters()
        del values['x_steps_per_mm']
        with self.assertRaises(ValueError):
            generate_config(values)
        del values['relay_pin']
        with self.assertRaises(ValueError):
            generate_config(values)

    def test_invalid_numbers_and_reserved_pins(self):
        for value in (0, -1, float('inf'), float('nan'), True, 'bad'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                generate_config(parameters(x_travel=value))
        for pin in ('gpio.0', 'gpio.1', 'gpio.6', 'gpio.16', 'gpio.34', 'i2so.7', 'gpio.4:low'):
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                generate_config(parameters(relay_pin=pin))

    def test_generator_official_numeric_bounds(self):
        fields = [('steps_per_mm', .001, 100000), ('max_rate', .001, 250000),
                  ('acceleration', .001, 100000), ('travel', .1, 10000000)]
        for axis in ('x', 'y'):
            for key, minimum, maximum in fields:
                for value in (minimum, maximum):
                    with self.subTest(axis=axis, key=key, value=value):
                        generate_config(parameters(**{axis + '_' + key: value}))
                for value in (minimum / 2, maximum + 1):
                    with self.subTest(axis=axis, key=key, value=value), self.assertRaises(ValueError):
                        generate_config(parameters(**{axis + '_' + key: value}))


if __name__ == '__main__':
    unittest.main()
