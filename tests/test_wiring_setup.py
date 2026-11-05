"""Regression checks for the actual three-driver / TTL32 wiring change."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fluidnc_setup import generate_config, generate_yaml, new_wiring_defaults


def commissioned_parameters(**overrides):
    # These are synthetic test values, not defaults for the user's machine.
    values = new_wiring_defaults()
    values.update(x_steps_per_mm=80, y_steps_per_mm=40,
                  x_max_rate=1200, y_max_rate=900,
                  x_acceleration=100, y_acceleration=60,
                  x_travel=300, y_travel=240,
                  invert_x=False, invert_y=True, invert_y2=False,
                  relay_active_low=True, pins_verified=True,
                  parameters_verified=True, endstops_verified=True,
                  x_limit_positive=False, y_limit_positive=False)
    values.update(overrides)
    return values


class NewWiringSetupTests(unittest.TestCase):
    def test_topology_is_preselected_but_machine_calibration_is_not_invented(self):
        values = new_wiring_defaults()
        self.assertEqual(values['relay_pin'], '32')
        self.assertEqual(values['endstops'], 'nc')
        self.assertTrue(values['dual_y'])
        self.assertIsNone(values['relay_active_low'])
        self.assertIsNone(values['invert_x'])
        self.assertEqual(values['x_steps_per_mm'], '')
        with self.assertRaises(ValueError):
            generate_config(values)

    def test_carriage_and_both_portal_drivers_use_agreed_physical_sockets(self):
        config = generate_config(commissioned_parameters())
        axes = config['axes']
        self.assertEqual(axes['x']['motor0']['stepstick']['step_pin'], 'i2so.5')
        self.assertEqual(axes['x']['motor0']['stepstick']['direction_pin'], 'i2so.6')
        self.assertEqual(axes['y']['motor0']['stepstick']['step_pin'], 'i2so.1')
        self.assertEqual(axes['y']['motor0']['stepstick']['direction_pin'], 'i2so.2:low')
        self.assertEqual(axes['y']['motor1']['stepstick']['step_pin'], 'i2so.3')
        self.assertEqual(axes['y']['motor1']['stepstick']['direction_pin'], 'i2so.4')
        self.assertNotIn('motor1', axes['x'])
        self.assertNotIn('z', axes)

    def test_ttl32_is_coolant_only_never_laser_pwm(self):
        config = generate_config(commissioned_parameters())
        self.assertEqual(config['coolant']['flood_pin'], 'gpio.32:low')
        self.assertFalse(any(key.lower() in ('laser', 'pwm', 'relay') for key in config))
        high = generate_config(commissioned_parameters(relay_active_low=False))
        self.assertEqual(high['coolant']['flood_pin'], 'gpio.32')
        with self.assertRaises(ValueError):
            generate_config(commissioned_parameters(relay_pin='4'))

    def test_two_nc_switches_map_to_logical_roles_without_fake_autosquare(self):
        config = generate_config(commissioned_parameters(hard_limits=True))
        self.assertEqual(config['axes']['x']['motor0']['limit_neg_pin'], 'gpio.36')
        self.assertEqual(config['axes']['y']['motor0']['limit_neg_pin'], 'gpio.35')
        self.assertEqual(config['axes']['y']['motor1']['limit_neg_pin'], 'NO_PIN')
        self.assertTrue(config['axes']['x']['motor0']['hard_limits'])
        self.assertTrue(config['axes']['y']['motor0']['hard_limits'])
        self.assertFalse(config['axes']['y']['motor1']['hard_limits'])
        self.assertTrue(config['start']['check_limits'])
        self.assertFalse(config['start']['must_home'])
        self.assertNotIn('homing', config['axes']['x'])

    def test_positive_endstop_side_is_explicit_and_has_no_wrong_internal_pull(self):
        config = generate_config(commissioned_parameters(x_limit_positive=True))
        motor = config['axes']['x']['motor0']
        self.assertEqual(motor['limit_pos_pin'], 'gpio.36')
        self.assertEqual(motor['limit_neg_pin'], 'NO_PIN')
        self.assertNotIn(':pu', motor['limit_pos_pin'])
        with self.assertRaises(ValueError):
            generate_config(commissioned_parameters(x_limit_positive=None))

    def test_homing_requires_real_parameters_and_identical_portal_pull_offs(self):
        values = commissioned_parameters(homing_enabled=True)
        with self.assertRaises(ValueError):
            generate_config(values)
        for axis in ('x', 'y'):
            values.update({axis + '_homing_seek': 300, axis + '_homing_feed': 60,
                           axis + '_homing_pulloff': 2.5, axis + '_homing_mpos': 0})
        config = generate_config(values)
        self.assertEqual(config['axes']['x']['homing']['cycle'], 1)
        self.assertEqual(config['axes']['y']['homing']['cycle'], 2)
        self.assertFalse(config['axes']['y']['homing']['positive_direction'])
        self.assertEqual(config['axes']['y']['motor0']['pulloff_mm'], 2.5)
        self.assertEqual(config['axes']['y']['motor1']['pulloff_mm'], 2.5)
        self.assertTrue(config['start']['must_home'])
        self.assertFalse(config['axes']['x']['soft_limits'])
        values['y_homing_feed'] = 301
        with self.assertRaises(ValueError):
            generate_config(values)

    def test_no_endstops_mode_is_still_explicit_and_excludes_homing(self):
        values = commissioned_parameters(endstops='none', no_endstops_confirmed=True)
        config = generate_config(values)
        self.assertEqual(config['axes']['x']['motor0']['limit_neg_pin'], 'NO_PIN')
        self.assertEqual(config['axes']['y']['motor0']['limit_neg_pin'], 'NO_PIN')
        with self.assertRaises(ValueError):
            generate_config(dict(values, no_endstops_confirmed=False))
        with self.assertRaises(ValueError):
            generate_config(dict(values, homing_enabled=True))

    def test_verification_flags_cannot_be_bypassed_by_default_selection(self):
        for key in ('pins_verified', 'parameters_verified', 'endstops_verified'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                generate_yaml(commissioned_parameters(**{key: False}))
        text = generate_yaml(commissioned_parameters(pins_verified=False,
                             parameters_verified=False, endstops_verified=False),
                             require_confirmation=False)
        self.assertIn('gpio.32:low', text)

    def test_export_preserves_input_and_explains_nc_and_no_pwm(self):
        values = commissioned_parameters()
        before = deepcopy(values)
        text = generate_yaml(values)
        self.assertEqual(values, before)
        self.assertIn('COM→G, NC→S', text)
        self.assertIn('без Laser/PWM', text)
        self.assertIn('Внешние подтяжки', text)


if __name__ == '__main__':
    unittest.main()
