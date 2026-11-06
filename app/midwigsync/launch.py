"""Entry point used by the app bundle: start the window, and if anything goes wrong before it opens,
say so in a dialog instead of quitting silently."""
import os
import subprocess
import sys
import traceback


def _alert(title, message):
    script = ('on run argv\n set r to display alert (item 1 of argv) message (item 2 of argv) as critical '
              'buttons {"Show Log", "OK"} default button "OK"\n return button returned of r\nend run')
    try:
        r = subprocess.run(['osascript', '-', title, message], input=script, capture_output=True, text=True, timeout=600)
        if 'Show Log' in (r.stdout or '') and os.environ.get('MIDWIG_LOG'):
            subprocess.run(['open', '-R', os.environ['MIDWIG_LOG']], check=False)
    except Exception:  # noqa: BLE001
        pass


def main():
    try:
        from .gui import main as gui_main
        gui_main()
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001
        tb = traceback.format_exc()
        sys.stderr.write(tb)
        sys.stderr.flush()
        last = tb.strip().splitlines()[-1] if tb.strip() else str(e)
        _alert('Midwig Sync couldn’t open its window',
               f'{last[:300]}\n\nThe details are in the log (Show Log). Send it to Claude and it can fix this.')
        sys.exit(1)


if __name__ == '__main__':
    main()
