"""Dispose hidden Tk fixtures on the thread that created their interpreter."""
import gc


def close_tk_app(case):
    app = case.app
    try:
        if not app.closed:
            app.close()
        case.assertTrue(app.closed)
        case.assertFalse(app.controller.worker.is_alive(), "Controller outlived its Tk window")
    finally:
        # destroy() removes Tcl widgets, but Python widget/variable cycles can
        # survive. Do not let a later controller worker collect those cycles.
        case.app = None
        case.root = None
        del app
        gc.collect()
