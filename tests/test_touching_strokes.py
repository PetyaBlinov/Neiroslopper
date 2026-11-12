"""Shared endpoints must not introduce lifts or change unrelated contours."""
from copy import deepcopy
import threading
import unittest
from unittest.mock import Mock
from geometry import Axes, join_touching_paths
from controller import Controller
from pen_lift import PenLiftSettings


class TouchingStrokesTests(unittest.TestCase):
    def test_chain_is_joined_without_mutating_source(self):
        paths = [[(1, 1), (2, 2)], [(2, 2), (3, 1)], [(3, 1), (4, 4)]]
        before = deepcopy(paths)
        self.assertEqual(join_touching_paths(paths), [[(1, 1), (2, 2), (3, 1), (4, 4)]])
        self.assertEqual(paths, before)

    def test_gap_reverse_stroke_and_dot_are_not_joined(self):
        paths = [[(1, 1), (2, 2)], [(2.01, 2), (3, 1)], [(4, 4)], [(4, 4), (5, 5)]]
        self.assertEqual(join_touching_paths(paths), paths)

    def test_draw_dispatch_joins_but_startpoint_does_not(self):
        controller = Controller(start_worker=False)
        controller.pen = PenLiftSettings(enabled=True, power_raises=True)
        controller.draw_power_raises = Mock()
        paths = [[(1, 1), (2, 2)], [(2, 2), (3, 1)]]
        controller.draw_with_lift(paths, (1, 1), Axes(), 300, 'draw')
        self.assertEqual(len(controller.draw_power_raises.call_args.args[0]), 1)
        controller.draw_with_lift(paths, (1, 1), Axes(), 300, 'startpoint')
        self.assertEqual(len(controller.draw_power_raises.call_args.args[0]), 2)

    def test_continuous_join_has_one_stop_and_no_raised_transfer(self):
        controller = Controller(start_worker=False)
        controller.pen = PenLiftSettings(enabled=True, power_raises=True)
        session = controller.session = Mock()
        session.synchronize.return_value = (3, 1)
        controller.powered_travel = Mock(side_effect=AssertionError('No raised transfer expected'))
        controller.draw_with_lift([[(1, 1), (2, 2)], [(2, 2), (3, 1)]],
                                  (1, 1), Axes(), 300, 'draw')
        self.assertTrue(all(call.args[0] is True for call in session.set_pen.call_args_list))
        self.assertEqual(sum(call.args[0].startswith('G1 ') for call in session.command.call_args_list), 2)


if __name__ == '__main__':
    unittest.main()
