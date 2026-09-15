"""Synthetic-only browser preview: python -m tests.video_browser_harness --port 9081.

Uses temporary storage and fake relay/model fixtures. Never loads user config or
starts a relay connection. Build frontend first; choose synthetic local media.
"""
import argparse
from copy import deepcopy
from pathlib import Path
import tempfile
from unittest.mock import patch


def serve(port=9081):
    from tests.story_preparation_browser_harness import import_main_without_default_app
    main = import_main_without_default_app()
    from tests.test_game_loop_timeline import make_game_loop_for_test
    from tests.test_session_endpoints import make_endpoint_state
    import uvicorn

    with tempfile.TemporaryDirectory(prefix='coyote-video-preview-') as directory:
        harness = make_game_loop_for_test(Path(directory))
        harness.safety.max_step = 10
        harness.safety.user_caps.update(A=40, B=40)
        harness.cfg['app']['dry_run'] = True
        harness.safety.dry_run = True
        for name, frames in (('Synthetic Wave A', ['01', '02', '03']),
                             ('Synthetic Wave B', ['03', '02', '01'])):
            harness.safety.presets[name] = {**deepcopy(harness.safety.presets['呼吸']), 'frames': frames}
        state = make_endpoint_state(harness)
        with patch.object(main, 'load_config', return_value=harness.cfg), patch.object(main, 'AppState', return_value=state):
            app = main.make_app()

        @app.get('/__synthetic_check')
        async def synthetic_check():
            return {'dry_run': harness.safety.dry_run,
                    'sent_frames': len(harness.relay.sent_frames),
                    'strength': dict(harness.safety.current)}

        uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=9081)
    serve(parser.parse_args().port)
