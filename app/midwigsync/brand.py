"""The app's name and the places it keeps things. Change APP_NAME here (and rebuild) to rename the app;
output folders don't carry the name, so renaming never changes what you deliver."""
import os

APP_NAME = 'Prep Station'
APP_VERSION = '1.4.1'
BUNDLE_ID = 'com.midwigproductions.prepstation'

OUT_SUFFIX = '_SYNCED'          # <job>_SYNCED next to the sound folders
WORK_DIR = '_SYNC_WORK'         # inside it: caches, plans, logs, job.json
OLD_WORK_DIRS = ('_MIDWIG',)    # output folders from earlier versions

SUPPORT = os.path.expanduser(f'~/Library/Application Support/{APP_NAME}')
CACHE = os.path.expanduser(f'~/Library/Caches/{APP_NAME}')
LOGS = os.path.expanduser(f'~/Library/Logs/{APP_NAME}')
SETTINGS_FILE = os.path.join(SUPPORT, 'settings.json')
ENV_CACHE = os.path.join(CACHE, 'envelopes')
CONVERT_DIR = os.path.join(CACHE, 'converted')
