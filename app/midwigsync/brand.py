"""The app's name and the places it keeps things. Change APP_NAME here (and rebuild) to rename the app;
output folders don't carry the name, so renaming never changes what you deliver."""
import os

APP_NAME = 'Prep Station'
APP_VERSION = '2.5.4'
BUNDLE_ID = 'com.midwigproductions.prepstation'

OUT_SUFFIX = '_SYNCED'          # <job>_SYNCED next to the sound folders
WORK_DIR = 'WORK FILES (not for the edit)'   # report, corrected-TC sound copies, plans, caches, logs; sorts last
OLD_WORK_DIRS = ('_MIDWIG', '_SYNC_WORK')     # work folders from earlier versions
TIMELINES_DIR = 'TIMELINES'
# inside TIMELINES/: one folder per place the file is used
TL_XML, TL_AAF, TL_ALE, TL_OTIO, TL_CSV = 'PREMIERE RESOLVE XML', 'PRO TOOLS AAF', 'AVID ALE', 'OTIO', 'CLIP LIST CSV'
TRANSCRIPT_DIR = 'TRANSCRIPT'
SOUND_REPORT_DIR = 'SOUND REPORT'

SUPPORT = os.path.expanduser(f'~/Library/Application Support/{APP_NAME}')
CACHE = os.path.expanduser(f'~/Library/Caches/{APP_NAME}')
LOGS = os.path.expanduser(f'~/Library/Logs/{APP_NAME}')
SETTINGS_FILE = os.path.join(SUPPORT, 'settings.json')
ENV_CACHE = os.path.join(CACHE, 'envelopes')
CONVERT_DIR = os.path.join(CACHE, 'converted')
